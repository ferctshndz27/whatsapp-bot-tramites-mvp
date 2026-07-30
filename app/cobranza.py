"""Dominio de cobranza. ÚNICO lugar autorizado a mutar cobro.estado.

Regla de oro del proyecto: el bot NUNCA genera un cobro — el link siempre lo
dispara un humano desde el panel. Aquí no hay HTTP ni FastAPI ni commits: las
funciones escriben sobre la sesión que reciben y el que llama decide la
transacción.

La única fuente de verdad local es eventopasarela: toda transición que no
viene de Stripe se escribe también ahí con id sintético, así el replay
(recomputar_estado) reconstruye la fila completa sin inventar nada.
"""

import json
import logging
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.auth import is_admin
from app.config import settings
from app.dinero import formato_monto
from app.models import (
    Alert,
    Cobro,
    EventoPasarela,
    IntentoCobro,
    TicketActivity,
    utcnow,
)

logger = logging.getLogger(__name__)

# Máquina de estados por RANGO: el estado solo avanza a rango mayor; dentro
# del mismo rango desempata ultimo_evento_created. El dinero real (pagado)
# siempre gana sobre el estado local: rechazar un pago verdadero es el fallo
# más caro de la lista.
RANGO = {
    "borrador": 0,
    "pendiente": 1,
    "cancelacion_solicitada": 2,
    "cancelado": 3,
    "vencido": 3,
    "fallido": 3,
    "pagado": 4,
    "reembolsado": 5,
}

# Mientras el estado sea uno de estos existe (o puede existir) un instrumento
# cobrable: el candado llave_cobro_vivo NO se libera.
ESTADOS_VIVOS = {"borrador", "pendiente", "cancelacion_solicitada"}

PAYLOAD_MAX = 32_768
RESPUESTA_MAX = 8_192

MENSAJE_LINK = (
    "🧾 {concepto}\n"
    "Monto: {monto}\n"
    "Paga aquí de forma segura: {url}"
)
MENSAJE_LINK_REEMPLAZO = (
    "🧾 {concepto}\n"
    "Monto: {monto}\n"
    "Este link REEMPLAZA al anterior; el anterior ya no sirve.\n"
    "Paga aquí de forma segura: {url}"
)
MENSAJE_PAGADO = (
    "✅ Recibimos tu pago de {monto} por: {concepto}.\n"
    "¡Gracias! Seguimos con tu trámite."
)


def _epoch_a_dt(epoch: int | float | None) -> datetime | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def llave_vivo(phone: str, service_id: str | None, ticket_id: int | None) -> str:
    """Candado por (teléfono, servicio) — NO por folio: con OXXO el folio se
    cierra y el bot fabrica otro, así que un candado por ticket_id no vería
    nada. Solo por teléfono bloquearía dos trámites legítimos en paralelo."""
    if service_id:
        return f"{phone}:svc:{service_id}"
    if ticket_id:
        return f"{phone}:folio:{ticket_id}"
    return f"{phone}:libre"


def puede_cobrar(user: str | None, ticket, cobro) -> bool:
    """Cobrar es acto de ASESOR (el asignado), no de admin. creado_por entra
    porque un voucher de 3 días sobrevive a release_ticket (assigned_to=None)
    y sin él nadie podría cancelar ni reenviar."""
    if not user:
        return False
    if ticket is not None and ticket.assigned_to == user:
        return True
    if cobro is not None and cobro.creado_por == user:
        return True
    return is_admin(user)


def alerta(session: Session, code: str, title: str, detail: str,
           level: str = "warn", metric_value: float | None = None) -> None:
    """Una fila ACTIVA por code (misma convención de app.alerts): si ya hay
    una activa se actualiza en su lugar — reprocesar eventos no duplica."""
    activa = session.exec(
        select(Alert).where(Alert.code == code).where(Alert.active == True)  # noqa: E712
    ).first()
    if activa:
        activa.detail = detail
        activa.level = level
        activa.metric_value = metric_value
        session.add(activa)
        return
    session.add(Alert(code=code, level=level, title=title, detail=detail,
                      metric_value=metric_value))


def _actividad(session: Session, ticket_id: int | None, username: str, action: str) -> None:
    if ticket_id:
        session.add(TicketActivity(ticket_id=ticket_id, username=username, action=action))


def crear_cobro(
    session: Session,
    *,
    phone: str,
    concepto: str,
    monto_centavos: int,
    creado_por: str,
    llave_generacion: str,
    token_verificacion: str,
    ticket_id: int | None = None,
    client_name: str | None = None,
    service_id: str | None = None,
    monto_cotizado_centavos: int | None = None,
    monto_origen: str = "manual",
    metodos_ofrecidos: str = "card",
    msi_meses_ofrecidos: str | None = None,
    origen: str = "panel",
    plan_id: int | None = None,
) -> Cobro:
    """Fila en borrador con el candado puesto. NO llama a Stripe y no
    comitea: la transacción es del que llama (corta, sin red adentro)."""
    cobro = Cobro(
        ticket_id=ticket_id,
        phone=phone,
        client_name=client_name,
        service_id=service_id,
        concepto=concepto,
        monto_centavos=monto_centavos,
        monto_cotizado_centavos=monto_cotizado_centavos,
        monto_origen=monto_origen,
        metodos_ofrecidos=metodos_ofrecidos,
        msi_meses_ofrecidos=msi_meses_ofrecidos,
        llave_cobro_vivo=llave_vivo(phone, service_id, ticket_id),
        llave_generacion=llave_generacion,
        token_verificacion=token_verificacion,
        origen=origen,
        plan_id=plan_id,
        creado_por=creado_por,
    )
    session.add(cobro)
    return cobro


def nuevo_intento(
    session: Session,
    cobro: Cobro,
    *,
    operacion: str = "crear_checkout",
    origen: str = "panel",
    creado_por: str = "",
) -> IntentoCobro:
    """Write-ahead log: la fila (con su clave de idempotencia) debe quedar
    COMMITEADA antes de la primera llamada de red. Exige cobro.id (flush)."""
    assert cobro.id, "flush antes de crear el intento: la clave lleva el id"
    ultimo = session.exec(
        select(IntentoCobro)
        .where(IntentoCobro.cobro_id == cobro.id)
        .order_by(IntentoCobro.numero.desc())  # type: ignore[attr-defined]
    ).first()
    numero = (ultimo.numero + 1) if ultimo else 1
    intento = IntentoCobro(
        cobro_id=cobro.id,
        numero=numero,
        clave_idempotencia=f"cobro:{cobro.id}:{numero}",
        operacion=operacion,
        metodos_solicitados=cobro.metodos_ofrecidos,
        msi_meses=cobro.msi_meses_ofrecidos,
        monto_centavos=cobro.monto_centavos,
        moneda=cobro.moneda,
        origen=origen,
        creado_por=creado_por,
    )
    session.add(intento)
    return intento


def registrar_evento_sintetico(
    session: Session,
    *,
    stripe_event_id: str,
    tipo: str,
    cobro: Cobro | None = None,
    intento_id: int | None = None,
    objeto: dict | None = None,
    livemode: bool | None = None,
    stripe_created: int | None = None,
) -> EventoPasarela:
    """Las transiciones que no vienen de Stripe (crear, enviar, cancelar,
    reconciliar) también son eventos: sin esto el replay resucita cobros."""
    created = stripe_created or int(utcnow().timestamp())
    payload = {"id": stripe_event_id, "type": tipo, "created": created,
               "livemode": livemode, "data": {"object": objeto or {}}}
    evento = EventoPasarela(
        stripe_event_id=stripe_event_id,
        tipo=tipo,
        livemode=livemode,
        stripe_created=created,
        objeto_id=(objeto or {}).get("id"),
        monto_centavos=(objeto or {}).get("amount_total"),
        cobro_id=cobro.id if cobro else None,
        intento_id=intento_id,
        payload_json=json.dumps(payload)[:PAYLOAD_MAX],
    )
    session.add(evento)
    return evento


# ---------------------------------------------------------------------------
# Resolución de dos factores
# ---------------------------------------------------------------------------


def _obj_de(payload: dict) -> dict:
    return (payload.get("data") or {}).get("object") or {}


def _pi_de(obj: dict) -> str | None:
    """Id del PaymentIntent sin importar la forma del objeto (cs/pi/ch)."""
    if obj.get("object") == "payment_intent":
        return obj.get("id")
    pi = obj.get("payment_intent")
    if isinstance(pi, dict):
        return pi.get("id")
    return pi or None


def resolver_cobro(session: Session, payload: dict) -> tuple[Cobro | None, str | None]:
    """(cobro, motivo_de_rechazo). Prueba metadata[cobro_id] → pi → cs → ch,
    y el token de la metadata es el SEGUNDO factor: un respaldo restaurado
    recicla ids, y el evento viejo con cobro_id=137 no debe tocar al 137
    nuevo. NUNCA se consulta con None (== None compila a IS NULL y engancha
    la fila equivocada)."""
    obj = _obj_de(payload)
    meta = obj.get("metadata") or {}
    token = meta.get("token")

    def _verificado(cobro: Cobro | None) -> tuple[Cobro | None, str | None]:
        if cobro is None:
            return None, None
        if token and cobro.token_verificacion != token:
            return None, "metadata no coincide"
        return cobro, None

    cobro_id = meta.get("cobro_id")
    if cobro_id:
        try:
            fila = session.get(Cobro, int(cobro_id))
        except (TypeError, ValueError):
            fila = None
        if fila is not None:
            return _verificado(fila)

    pi = _pi_de(obj)
    if pi:
        fila = session.exec(
            select(Cobro)
            .where(Cobro.stripe_payment_intent_id.is_not(None))  # type: ignore[union-attr]
            .where(Cobro.stripe_payment_intent_id == pi)
        ).first()
        if fila is not None:
            return _verificado(fila)

    cs = obj.get("id") if obj.get("object") == "checkout.session" else None
    if cs:
        fila = session.exec(
            select(Cobro)
            .where(Cobro.stripe_checkout_session_id.is_not(None))  # type: ignore[union-attr]
            .where(Cobro.stripe_checkout_session_id == cs)
        ).first()
        if fila is not None:
            return _verificado(fila)

    ch = obj.get("id") if obj.get("object") == "charge" else None
    if ch:
        fila = session.exec(
            select(Cobro)
            .where(Cobro.stripe_charge_id.is_not(None))  # type: ignore[union-attr]
            .where(Cobro.stripe_charge_id == ch)
        ).first()
        if fila is not None:
            return _verificado(fila)

    return None, None


# ---------------------------------------------------------------------------
# Absorción y transición
# ---------------------------------------------------------------------------


def absorber_datos(cobro: Cobro, obj: dict, evento: EventoPasarela) -> None:
    """Corre SIEMPRE que el evento sea más nuevo, aunque el rango no suba:
    el completed(unpaid) de OXXO no cambia el estado pero trae el
    payment_intent — perderlo por la monotonía deja al voucher invisible."""
    if evento.stripe_created < (cobro.ultimo_evento_created or 0):
        return

    tipo_obj = obj.get("object")
    if tipo_obj == "checkout.session":
        if obj.get("id") and not cobro.stripe_checkout_session_id:
            cobro.stripe_checkout_session_id = obj["id"]
        if obj.get("url"):
            cobro.url_pago = obj["url"]
        if obj.get("expires_at"):
            cobro.url_expira_at = _epoch_a_dt(obj["expires_at"])
    pi = _pi_de(obj)
    # Un cobro tiene exactamente UN instrumento: un pi distinto al ya escrito
    # es un segundo pago (lo maneja _aplicar_pago), nunca un reemplazo.
    if pi and not cobro.stripe_payment_intent_id:
        cobro.stripe_payment_intent_id = pi
    if tipo_obj == "charge" and obj.get("id"):
        cobro.stripe_charge_id = obj["id"]
    latest_charge = obj.get("latest_charge")
    if isinstance(latest_charge, str) and latest_charge:
        cobro.stripe_charge_id = latest_charge
    cliente = obj.get("customer")
    if isinstance(cliente, str) and cliente:
        cobro.stripe_customer_id = cliente
    txn = obj.get("balance_transaction")
    if isinstance(txn, str) and txn:
        cobro.balance_transaction_id = txn
    if obj.get("livemode") is not None:
        cobro.livemode = obj["livemode"]

    # Voucher OXXO / instrucciones SPEI: el reloj que importa.
    oxxo = ((obj.get("next_action") or {}).get("oxxo_display_details") or {})
    if oxxo.get("hosted_voucher_url"):
        cobro.voucher_url = oxxo["hosted_voucher_url"]
        cobro.voucher_expira_at = _epoch_a_dt(oxxo.get("expires_after"))
        cobro.detalle_pendiente_at = None

    tipos = obj.get("payment_method_types") or []
    if len(tipos) == 1 and not cobro.metodo_usado:
        cobro.metodo_usado = tipos[0]
    detalles_pago = (obj.get("payment_method_details") or {})
    if detalles_pago.get("type"):
        cobro.metodo_usado = detalles_pago["type"]
        plan = ((detalles_pago.get("card") or {}).get("installments") or {}).get("plan") or {}
        if plan.get("count"):
            cobro.msi_meses_usado = plan["count"]
    cobro.updated_at = utcnow()


def transicionar(cobro: Cobro, nuevo_estado: str, evento: EventoPasarela) -> bool:
    """Monotonía por rango. Devuelve True si el estado cambió."""
    rango_nuevo = RANGO[nuevo_estado]
    rango_actual = RANGO[cobro.estado]
    if rango_nuevo < rango_actual:
        return False
    if rango_nuevo == rango_actual:
        empate_mas_nuevo = evento.stripe_created >= (cobro.ultimo_evento_created or 0)
        if nuevo_estado == cobro.estado or not empate_mas_nuevo:
            return False
    cobro.estado = nuevo_estado
    cobro.ultimo_evento_id = evento.stripe_event_id
    cobro.ultimo_evento_created = evento.stripe_created
    cobro.updated_at = utcnow()
    return True


# ---------------------------------------------------------------------------
# El aplicador
# ---------------------------------------------------------------------------


def aplicar_evento(session: Session, evento: EventoPasarela) -> str:
    """Aplica un evento persistido al cobro que resuelva. NO comitea y NO hace
    red (el detalle del voucher lo trae el barrido). Escribe resultado,
    detalle y procesado_at en la fila del evento."""
    try:
        payload = json.loads(evento.payload_json or "{}")
    except ValueError:
        payload = {}

    # livemode del evento contra el modo de la llave configurada: un evento de
    # prueba jamás mueve un cobro real (ni al revés).
    modo_real = settings.stripe_secret_key.startswith("sk_live")
    if evento.livemode is not None and settings.stripe_secret_key and evento.livemode != modo_real:
        alerta(session, "cobro:modo-no-coincide", "Evento de Stripe en modo equivocado",
               f"{evento.stripe_event_id} livemode={evento.livemode} con llave "
               f"{'live' if modo_real else 'test'}")
        return _cerrar(session, evento, "ignorado", "modo no coincide")

    cobro, rechazo = resolver_cobro(session, payload)
    if rechazo:
        alerta(session, "cobro:metadata-no-coincide", "Evento con metadata que no coincide",
               f"{evento.stripe_event_id}: token de verificación distinto")
        return _cerrar(session, evento, "ignorado", rechazo)
    if cobro is None:
        alerta(session, "cobro:evento-huerfano", "Evento de Stripe sin cobro",
               f"{evento.stripe_event_id} ({evento.tipo}) no resolvió a ningún cobro")
        return _cerrar(session, evento, "huerfano", "sin cobro que lo reclame")

    evento.cobro_id = cobro.id
    resultado, detalle = _aplicar_a_cobro(session, cobro, evento, _obj_de(payload))
    session.add(cobro)
    return _cerrar(session, evento, resultado, detalle)


def _cerrar(session: Session, evento: EventoPasarela, resultado: str, detalle: str | None) -> str:
    evento.resultado = resultado
    evento.detalle = detalle
    evento.procesado_at = utcnow()
    session.add(evento)
    return resultado


def _aplicar_a_cobro(
    session: Session, cobro: Cobro, evento: EventoPasarela, obj: dict
) -> tuple[str, str | None]:
    tipo = evento.tipo

    if evento.stripe_event_id and evento.stripe_event_id == cobro.ultimo_evento_id:
        return "duplicado", "evento ya aplicado"

    absorber_datos(cobro, obj, evento)

    if tipo in ("checkout.session.completed",):
        if obj.get("payment_status") == "paid":
            return _aplicar_pago(session, cobro, evento, obj)
        # ESTE EVENTO NO ES UN PAGO (payment_status=unpaid): es el voucher
        # emitido. El bug clásico de OXXO es confundirlo.
        if cobro.stripe_payment_intent_id and not cobro.voucher_url:
            cobro.detalle_pendiente_at = utcnow()
        _actividad(session, cobro.ticket_id, "sistema", "cobro:voucher")
        return "aplicado", "voucher emitido; esperando pago diferido"

    if tipo in ("checkout.session.async_payment_succeeded", "payment_intent.succeeded"):
        return _aplicar_pago(session, cobro, evento, obj)

    if tipo in ("checkout.session.async_payment_failed", "payment_intent.payment_failed"):
        if transicionar(cobro, "fallido", evento):
            cobro.llave_cobro_vivo = None  # Stripe confirmó: el instrumento murió
            error = (obj.get("last_payment_error") or {}).get("message")
            cobro.estado_motivo = (error or "pago fallido")[:500]
            _actividad(session, cobro.ticket_id, "sistema", "cobro:fallido")
            return "aplicado", None
        return "ignorado", "fallo con rango menor al estado actual"

    if tipo == "checkout.session.expired":
        if cobro.stripe_payment_intent_id:
            # La sesión expira a 24 h; el voucher vive 72. Expirar la sesión
            # NO mata el voucher: se ignora y el PI decide.
            return "ignorado", "sesión expirada con voucher vivo"
        if transicionar(cobro, "vencido", evento):
            cobro.llave_cobro_vivo = None
            _actividad(session, cobro.ticket_id, "sistema", "cobro:vencido")
            return "aplicado", None
        return "ignorado", "expiración con rango menor"

    if tipo == "payment_intent.canceled":
        if transicionar(cobro, "cancelado", evento):
            cobro.llave_cobro_vivo = None
            cobro.cancelado_at = _epoch_a_dt(evento.stripe_created)
            if obj.get("cancelado_por"):
                cobro.cancelado_por = obj["cancelado_por"]
            _actividad(session, cobro.ticket_id,
                       cobro.cancelado_por or "sistema", "cobro:cancelado")
            return "aplicado", None
        return "ignorado", "cancelación con rango menor"

    if tipo == "payment_intent.partially_funded":
        recibido = obj.get("amount_received") or 0
        cobro.monto_pagado_centavos = recibido
        cobro.estado_motivo = (
            f"transferencia parcial: recibidos {recibido} de {cobro.monto_centavos}"
        )
        alerta(session, "cobro:pago-parcial", "Transferencia SPEI parcial",
               f"cobro #{cobro.id}: {cobro.estado_motivo}")
        return "aplicado", "pago parcial registrado"

    if tipo in ("charge.refunded", "charge.refund.updated"):
        reembolsado = obj.get("amount_refunded") or 0
        # Acumulado ABSOLUTO desde el charge, no sumando eventos: Stripe
        # reentrega y sumar duplicaría.
        cobro.monto_reembolsado_centavos = reembolsado
        if cobro.monto_pagado_centavos and reembolsado >= cobro.monto_pagado_centavos:
            transicionar(cobro, "reembolsado", evento)
        cobro.updated_at = utcnow()
        return "aplicado", None

    if tipo == "charge.dispute.created":
        cobro.disputado = True  # no cambia estado: el dinero sigue en disputa
        alerta(session, "cobro:disputa", "Disputa de cargo",
               f"cobro #{cobro.id} charge={cobro.stripe_charge_id}", level="critical")
        return "aplicado", None

    if tipo == "cobro.creado":
        return "aplicado", "alta del cobro"

    if tipo == "cobro.enviado":
        if transicionar(cobro, "pendiente", evento):
            _actividad(session, cobro.ticket_id, cobro.creado_por, "cobro:enviado")
            return "aplicado", None
        return "ignorado", "envío con rango menor"

    if tipo == "cobro.cancelacion_solicitada":
        if transicionar(cobro, "cancelacion_solicitada", evento):
            # El candado NO se libera: Stripe no confirmó la muerte del
            # instrumento y el voucher sigue cobrable.
            cobro.estado_motivo = (obj.get("motivo") or cobro.estado_motivo)
            _actividad(session, cobro.ticket_id,
                       obj.get("solicitado_por") or "sistema",
                       "cobro:cancelacion-pendiente")
            return "aplicado", None
        return "ignorado", "solicitud con rango menor"

    return "ignorado", f"tipo no manejado: {tipo}"


def _aplicar_pago(
    session: Session, cobro: Cobro, evento: EventoPasarela, obj: dict
) -> tuple[str, str | None]:
    pi_evento = _pi_de(obj)

    if cobro.estado == "pagado":
        if pi_evento and cobro.stripe_payment_intent_id and pi_evento != cobro.stripe_payment_intent_id:
            # Segundo pago REAL, no replay: no se pisa nada y nunca se calla.
            cobro.discrepancia = True
            alerta(session, "cobro:doble-pago", "Segundo pago sobre cobro pagado",
                   f"cobro #{cobro.id}: {pi_evento} además de "
                   f"{cobro.stripe_payment_intent_id}", level="critical")
            return "ignorado", "segundo pago sobre cobro pagado"
        return "duplicado", "pago ya aplicado"

    rango_previo = RANGO[cobro.estado]
    if not transicionar(cobro, "pagado", evento):
        return "ignorado", "pago con evento más viejo"

    if rango_previo in (2, 3):
        # Pago tardío: el dinero real gana sobre vencido/cancelado/fallido.
        cobro.discrepancia = True
        cobro.estado_motivo = "pago tardío aceptado"
        alerta(session, "cobro:pago-tardio", "Pago sobre cobro terminal",
               f"cobro #{cobro.id} estaba en rango {rango_previo} y pagó")

    monto = obj.get("amount_total")
    if monto is None:
        monto = obj.get("amount_received") or obj.get("amount")
    moneda_evento = (obj.get("currency") or "").casefold()
    if monto is not None:
        cobro.monto_pagado_centavos = monto
    if moneda_evento:
        cobro.moneda_pagada = moneda_evento
    if monto is not None and monto != cobro.monto_centavos:
        cobro.discrepancia = True
        cobro.estado_motivo = (
            f"monto distinto: esperado {cobro.monto_centavos}, cobrado {monto}"
        )
        alerta(session, "cobro:monto-distinto", "Pago con monto distinto",
               f"cobro #{cobro.id}: esperado {cobro.monto_centavos}, cobrado {monto}")
    if moneda_evento and moneda_evento != cobro.moneda.casefold():
        cobro.discrepancia = True
        cobro.estado_motivo = (
            f"moneda distinta: esperada {cobro.moneda}, cobrada {moneda_evento}"
        )
        alerta(session, "cobro:monto-distinto", "Pago en moneda distinta",
               f"cobro #{cobro.id}: {cobro.moneda} vs {moneda_evento}")

    cobro.pagado_at = _epoch_a_dt(evento.stripe_created) or utcnow()
    cobro.llave_cobro_vivo = None  # el instrumento se consumió
    _actividad(session, cobro.ticket_id, "sistema", "cobro:pagado")
    return "aplicado", None


# ---------------------------------------------------------------------------
# Respuesta del POST de creación (TX2 del flujo del panel)
# ---------------------------------------------------------------------------


def aplicar_respuesta_stripe(session: Session, cobro: Cobro, intento: IntentoCobro,
                             respuesta) -> str:
    """Cierra el intento según la RespuestaStripe del cliente HTTP.

    exitoso → evento sintético cobro.enviado (borrador→pendiente, con url).
    rechazado → intento rechazado, cobro se queda en borrador (reintentable).
    indeterminado → el intento SE QUEDA en 'enviando' (respondido_at NULL =
    "no sé"); ux_intento_vivo impide otro POST hasta que el barrido de
    huérfanos lo resuelva. Crear a ciegas ES el cobro duplicado.
    """
    ahora = utcnow()
    intento.http_status = respuesta.http_status
    intento.stripe_request_id = respuesta.request_id
    intento.livemode = (respuesta.objeto or {}).get("livemode")

    if respuesta.ok:
        objeto = respuesta.objeto or {}
        intento.estado = "exitoso"
        intento.stripe_object = objeto.get("object")
        intento.stripe_id = objeto.get("id")
        intento.respuesta_json = json.dumps(objeto)[:RESPUESTA_MAX]
        intento.respondido_at = ahora
        intento.cerrado_at = ahora
        evento = registrar_evento_sintetico(
            session,
            stripe_event_id=f"panel:sesion:{cobro.id}:{intento.numero}",
            tipo="cobro.enviado",
            cobro=cobro,
            intento_id=intento.id,
            objeto=objeto,
            livemode=objeto.get("livemode"),
        )
        aplicar_evento(session, evento)
        session.add(intento)
        return "exitoso"

    intento.error_codigo = respuesta.error_codigo
    intento.error_mensaje = (respuesta.error_mensaje or "")[:500] or None
    intento.respuesta_json = (respuesta.cuerpo_recortado or "")[:RESPUESTA_MAX] or None
    if respuesta.indeterminado:
        # Ni exitoso ni rechazado: Stripe pudo haber creado la sesión.
        intento.reintentos += 1
        session.add(intento)
        return "indeterminado"
    intento.estado = "rechazado"
    intento.respondido_at = ahora
    intento.cerrado_at = ahora
    session.add(intento)
    return "rechazado"


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

_CAMPOS_REPLAY = (
    "estado_motivo", "monto_pagado_centavos", "moneda_pagada", "metodo_usado",
    "msi_meses_usado", "stripe_checkout_session_id", "stripe_payment_intent_id",
    "stripe_charge_id", "stripe_customer_id", "url_pago", "url_expira_at",
    "voucher_url", "voucher_expira_at", "detalle_pendiente_at", "pagado_at",
    "cancelado_at", "ultimo_evento_id", "ultimo_evento_created",
)


def recomputar_estado(session: Session, cobro_id: int) -> Cobro:
    """Reconstruye estado, campos absorbidos y llave_cobro_vivo desde el log
    crudo, en la transacción del que llama. Si el candado restaurado chocaría
    con otro cobro vivo, se deja sin candado y se levanta Alert: NUNCA
    duplicar instrumentos cobrables."""
    cobro = session.get(Cobro, cobro_id)
    assert cobro is not None, f"cobro {cobro_id} no existe"

    for campo in _CAMPOS_REPLAY:
        setattr(cobro, campo, None)
    cobro.estado = "borrador"
    cobro.monto_reembolsado_centavos = 0
    cobro.discrepancia = False
    cobro.disputado = False

    eventos = session.exec(
        select(EventoPasarela)
        .where(EventoPasarela.cobro_id == cobro_id)
        .order_by(EventoPasarela.stripe_created, EventoPasarela.id)  # type: ignore[arg-type]
    ).all()
    for evento in eventos:
        if evento.resultado == "ignorado" and evento.detalle == "modo no coincide":
            # Un evento de prueba sobre un cobro real tampoco aplica en replay.
            continue
        try:
            payload = json.loads(evento.payload_json or "{}")
        except ValueError:
            continue
        _aplicar_a_cobro(session, cobro, evento, _obj_de(payload))

    if cobro.estado in ESTADOS_VIVOS:
        llave = llave_vivo(cobro.phone, cobro.service_id, cobro.ticket_id)
        choque = session.exec(
            select(Cobro)
            .where(Cobro.id != cobro.id)
            .where(Cobro.llave_cobro_vivo.is_not(None))  # type: ignore[union-attr]
            .where(Cobro.llave_cobro_vivo == llave)
        ).first()
        if choque is None:
            cobro.llave_cobro_vivo = llave
        else:
            cobro.llave_cobro_vivo = None
            alerta(session, "cobro:candado-en-conflicto",
                   "Replay no pudo restaurar el candado",
                   f"cobro #{cobro.id} choca con #{choque.id} ({llave})")
    else:
        cobro.llave_cobro_vivo = None

    cobro.updated_at = utcnow()
    session.add(cobro)
    return cobro


def mensaje_link(cobro: Cobro, reemplazo: bool = False) -> str:
    plantilla = MENSAJE_LINK_REEMPLAZO if reemplazo else MENSAJE_LINK
    return plantilla.format(
        concepto=cobro.concepto,
        monto=formato_monto(cobro.monto_centavos, cobro.moneda),
        url=cobro.url_pago or cobro.voucher_url or "",
    )


def mensaje_pagado(cobro: Cobro) -> str:
    return MENSAJE_PAGADO.format(
        concepto=cobro.concepto,
        monto=formato_monto(cobro.monto_pagado_centavos or cobro.monto_centavos,
                            cobro.moneda_pagada or cobro.moneda),
    )
