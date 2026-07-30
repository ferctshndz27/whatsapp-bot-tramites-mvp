"""Panel de cobranza: el asesor genera, reenvía, cancela y concilia.

REGLA DE ORO DEL PROYECTO: el bot NUNCA genera un cobro. El link siempre lo
dispara un humano desde aquí — `app/conversation.py` sigue contestando
"(A cotizar)" y no cambia.

Todo entra por Form(...) y sale con RedirectResponse 303, igual que el resto
del panel. La red a Stripe SIEMPRE ocurre fuera de una transacción abierta de
SQLite: sostener el escritor 10 s tumba el panel con "database is locked".
"""

import csv
import io
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app import cobranza, cobranza_jobs, stripe_client, whatsapp_client
from app.audit import log_admin
from app.auth import is_admin
from app.config import settings
from app.db import get_session
from app.dinero import MONTO_MIN_CENTAVOS, OXXO_MAX_CENTAVOS, formato_monto, parse_monto
from app.models import Cobro, Direction, IntentoCobro, Message, Ticket, utcnow
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

METODOS_VALIDOS = ("card", "oxxo", "spei")
MSI_PLAZOS_VALIDOS = ("3", "6", "9", "12")
# Arriba de este monto se deja rastro en la auditoría de admins: el asesor
# teclea el importe a mano y un cero de más es el riesgo operativo #1.
MONTO_ALTO_CENTAVOS = 2_000_000  # $20,000


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _volver(ticket_id: int | None, **params) -> RedirectResponse:
    destino = f"/tickets/{ticket_id}" if ticket_id else "/cobros"
    query = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
    if query:
        destino = f"{destino}?{query}"
    return RedirectResponse(destino, status_code=303)


def _admin_or_redirect(request: Request):
    user = _current_user(request)
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


# ---------------------------------------------------------------------------
# Generar
# ---------------------------------------------------------------------------


@router.post("/tickets/{ticket_id}/cobro")
def generar_cobro(
    request: Request,
    ticket_id: int,
    monto: str = Form(...),
    concepto: str = Form(""),
    token_form: str = Form(...),
    metodo: list[str] = Form(default=[]),
    msi: str = Form(""),
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        if ticket is None:
            return RedirectResponse("/", status_code=303)
        if not cobranza.puede_cobrar(user, ticket, None):
            # No-op silencioso, como el resto del panel: se registra y se
            # devuelve al folio sin pistas de qué falló.
            logger.warning("Cobro rechazado en #%s por %s", ticket_id, user)
            return _volver(ticket_id)
        phone, client_name = ticket.phone, ticket.client_name
        service_id, service_name = ticket.service_id, ticket.service_name
        price_info = ticket.price_info

    try:
        monto_centavos = parse_monto(monto)
    except ValueError:
        return _volver(ticket_id, err="monto")

    metodos = [m for m in metodo if m in METODOS_VALIDOS] or ["card"]
    if monto_centavos < MONTO_MIN_CENTAVOS:
        return _volver(ticket_id, err="monto-minimo")
    if monto_centavos > settings.cobro_monto_maximo_centavos:
        return _volver(ticket_id, err="monto-maximo")
    if "oxxo" in metodos and monto_centavos > OXXO_MAX_CENTAVOS:
        # Se rechaza ANTES de llamar a Stripe: el voucher no se emitiría.
        return _volver(ticket_id, err="monto-oxxo")

    if not settings.stripe_secret_key:
        # El no-op silencioso es del cliente HTTP; aquí sí se avisa: un cobro
        # fantasma es peor que un error visible.
        return _volver(ticket_id, err="sin-stripe")

    msi_plazos = msi if msi in MSI_PLAZOS_VALIDOS else None
    # Lo que el bot le COTIZÓ al cliente es la línea base del override, no el
    # catálogo vivo: el cliente leyó ese número en WhatsApp.
    try:
        cotizado = parse_monto(price_info) if price_info else None
    except ValueError:
        cotizado = None  # "A cotizar" y cualquier texto ambiguo -> NULL
    origen_monto = "manual"
    if cotizado is not None:
        origen_monto = "cotizado" if cotizado == monto_centavos else "override"

    # ---- TRANSACCIÓN 1: corta y SIN red adentro. La clave de idempotencia
    # queda en disco antes de que exista una sola llamada a Stripe.
    try:
        with get_session() as session:
            cobro = cobranza.crear_cobro(
                session,
                phone=phone,
                concepto=(concepto.strip() or f"{service_name} (folio #{ticket_id})")[:300],
                monto_centavos=monto_centavos,
                creado_por=user,
                llave_generacion=f"form:{token_form}",
                token_verificacion=uuid4().hex,
                ticket_id=ticket_id,
                client_name=client_name,
                service_id=service_id,
                monto_cotizado_centavos=cotizado,
                monto_origen=origen_monto,
                metodos_ofrecidos=",".join(metodos),
                msi_meses_ofrecidos=msi_plazos,
            )
            session.flush()
            intento = cobranza.nuevo_intento(session, cobro, creado_por=user)
            session.flush()
            cobranza.registrar_evento_sintetico(
                session,
                stripe_event_id=f"panel:crear:{cobro.id}",
                tipo="cobro.creado",
                cobro=cobro,
                intento_id=intento.id,
                objeto={"cobro_id": cobro.id},
            )
            cobranza._actividad(session, ticket_id, user, "cobro:generado")
            session.commit()
            cobro_id, intento_id = cobro.id, intento.id
    except IntegrityError as exc:
        # OJO: SQLite reporta la COLUMNA ("UNIQUE constraint failed:
        # cobro.llave_generacion"), no el nombre del índice, así que buscar
        # "ux_cobro_generacion" nunca acierta. Se miran los dos por si algún
        # día el mensaje cambia de forma.
        detalle = str(exc.orig) if exc.orig else str(exc)
        if "llave_generacion" in detalle:
            # F5 o doble clic: el cobro ya existe. No es un error.
            return _volver(ticket_id, ok="cobro")
        if "llave_cobro_vivo" in detalle:
            return _volver(ticket_id, err="cobro-vivo")
        logger.exception("No se pudo crear el cobro del folio #%s", ticket_id)
        return _volver(ticket_id, err="interno")

    if origen_monto == "override":
        log_admin(user, "cobro:monto-override",
                  f"#{ticket_id} {cotizado}->{monto_centavos}")
    if monto_centavos >= MONTO_ALTO_CENTAVOS:
        log_admin(user, "cobro:monto-alto", f"#{ticket_id} {monto_centavos}")

    # ---- Red, FUERA de toda transacción.
    with get_session() as session:
        parametros = cobranza.parametros_checkout(
            session.get(Cobro, cobro_id), session.get(IntentoCobro, intento_id)
        )
    respuesta = stripe_client.crear_checkout_session(**parametros)

    # ---- TRANSACCIÓN 2.
    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        intento = session.get(IntentoCobro, intento_id)
        resultado = cobranza.aplicar_respuesta_stripe(session, cobro, intento, respuesta)
        session.commit()

    if resultado != "exitoso":
        # Rechazo: el cobro se queda en borrador y es reintentable.
        # Indeterminado: lo resuelve el barrido de huérfanos con la MISMA
        # clave — jamás se crea otro aquí.
        return _volver(ticket_id, err="stripe")

    enviado = _enviar_link(cobro_id, user)
    return _volver(ticket_id, ok="cobro" if enviado else None,
                   err=None if enviado else "ventana")


# ---------------------------------------------------------------------------
# Envío por WhatsApp (best-effort: nunca cambia el estado del cobro)
# ---------------------------------------------------------------------------


def _enviar_link(cobro_id: int, user: str, reemplazo: bool = False) -> bool:
    """True si salió el mensaje. Que falle NO afecta el estado del cobro: el
    link vive en la fila y el panel ofrece copiarlo."""
    from app.inbox import _window_open

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro is None or not (cobro.url_pago or cobro.voucher_url):
            return False
        ultimo_in = session.exec(
            select(Message)
            .where(Message.phone == cobro.phone, Message.direction == Direction.IN)
            .order_by(Message.created_at.desc())  # type: ignore[union-attr]
        ).first()
        if not _window_open(ultimo_in.created_at if ultimo_in else None):
            logger.info("Sin envío del link a %s: ventana de 24h cerrada", cobro.phone)
            return False
        texto = cobranza.mensaje_link(cobro, reemplazo=reemplazo)
        phone, ticket_id = cobro.phone, cobro.ticket_id

    wamid = whatsapp_client.send_text(phone, texto)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        mensaje = Message(phone=phone, direction=Direction.OUT, body=texto,
                          wa_message_id=wamid, sent_by=user)
        session.add(mensaje)
        session.flush()
        cobro.enviado_at = utcnow()
        session.add(cobro)
        ultimo_intento = session.exec(
            select(IntentoCobro)
            .where(IntentoCobro.cobro_id == cobro_id)
            .order_by(IntentoCobro.numero.desc())  # type: ignore[union-attr]
        ).first()
        if ultimo_intento is not None:
            # Liga el dinero con la conversación: permite probar la entrega
            # mirando Message.wa_status.
            ultimo_intento.wa_message_id = mensaje.id
            session.add(ultimo_intento)
        cobranza._actividad(session, ticket_id, user, "cobro:enviado")
        session.commit()
    return True


def _cargar_para_accion(request: Request, cobro_id: int) -> tuple[str, int | None, bool]:
    """(user, ticket_id, permitido). El permiso incluye a creado_por porque un
    voucher de 3 días sobrevive a que se suelte el folio."""
    user = _current_user(request)
    if not user:
        return "", None, False
    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro is None:
            return user, None, False
        ticket = session.get(Ticket, cobro.ticket_id) if cobro.ticket_id else None
        permitido = cobranza.puede_cobrar(user, ticket, cobro)
        if not permitido:
            logger.warning("Acción de cobro %s rechazada para %s", cobro_id, user)
        return user, cobro.ticket_id, permitido


@router.post("/cobros/{cobro_id}/enviar")
def reenviar_cobro(request: Request, cobro_id: int):
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id)
    enviado = _enviar_link(cobro_id, user)
    return _volver(ticket_id, ok="enviado" if enviado else None,
                   err=None if enviado else "ventana")


# ---------------------------------------------------------------------------
# Cancelar
# ---------------------------------------------------------------------------


@router.post("/cobros/{cobro_id}/cancelar")
def cancelar_cobro(request: Request, cobro_id: int):
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro.estado not in cobranza.ESTADOS_VIVOS:
            return _volver(ticket_id, err="cobro-no-cancelable")
        pi = cobro.stripe_payment_intent_id
        cs = cobro.stripe_checkout_session_id

    # Matar el PaymentIntent —y NO expirar la sesión— es lo único que mata un
    # voucher de OXXO en requires_action.
    if pi:
        respuesta = stripe_client.cancelar_payment_intent(pi)
        confirmado = respuesta.ok and (respuesta.objeto or {}).get("status") == "canceled"
    elif cs:
        respuesta = stripe_client.expirar_sesion(cs)
        confirmado = respuesta.ok and (respuesta.objeto or {}).get("status") == "expired"
    else:
        # Nunca llegó a existir instrumento: se puede cerrar sin preguntar.
        respuesta, confirmado = None, True

    ahora = utcnow().isoformat()
    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if confirmado:
            objeto = dict((respuesta.objeto if respuesta else None) or {
                "object": "payment_intent", "id": pi or "", "status": "canceled",
            })
            objeto["cancelado_por"] = user
            evento = cobranza.registrar_evento_sintetico(
                session,
                stripe_event_id=f"panel:cancel:{cobro_id}:{ahora}",
                tipo="payment_intent.canceled",
                cobro=cobro, objeto=objeto,
            )
            session.flush()
            cobranza.aplicar_evento(session, evento)
            cobro.cancelado_por = user
            session.add(cobro)
            session.commit()
            return _volver(ticket_id, ok="cancelado")

        # Stripe no confirmó: el instrumento SIGUE cobrable, así que el
        # candado NO se libera. El barrido reintenta cada noche.
        vence = cobro.voucher_expira_at
        motivo = ("no se pudo cancelar el instrumento: sigue cobrable"
                  + (f" hasta el {vence:%d/%m/%Y}" if vence else ""))
        evento = cobranza.registrar_evento_sintetico(
            session,
            stripe_event_id=f"panel:cancel-pendiente:{cobro_id}:{ahora}",
            tipo="cobro.cancelacion_solicitada",
            cobro=cobro,
            objeto={"motivo": motivo, "solicitado_por": user},
        )
        session.flush()
        cobranza.aplicar_evento(session, evento)
        session.commit()
    return _volver(ticket_id, err="cancelacion-pendiente")


# ---------------------------------------------------------------------------
# Consultar a Stripe / confirmar por asesor (adenda A.1)
# ---------------------------------------------------------------------------


@router.post("/cobros/{cobro_id}/reconciliar")
def reconciliar_cobro(request: Request, cobro_id: int):
    """«Consultar a Stripe ahora»: el resultado entra como evento sintético y
    pasa por el MISMO aplicador que el webhook. Stripe sigue siendo la única
    verdad."""
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id)
    try:
        estado = cobranza_jobs._reconciliar_uno(cobro_id)
    except Exception:
        logger.exception("Consulta manual a Stripe falló para el cobro %s", cobro_id)
        estado = "sin-respuesta"
    return _volver(ticket_id, ok="reconciliado" if estado == "aplicado" else None,
                   err=None if estado == "aplicado" else "sin-novedad")


@router.post("/cobros/{cobro_id}/confirmar")
def confirmar_por_asesor(request: Request, cobro_id: int, referencia: str = Form("")):
    """Bandera paralela: NO escribe estado='pagado'. El rango 4 es exclusivo
    de Stripe; esto solo desbloquea el trabajo y deja rastro de quién se
    adelantó contra el recibo del cliente."""
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro.estado == "pagado":
            return _volver(ticket_id, err="ya-pagado")
        cobro.confirmado_por_asesor = True
        cobro.confirmado_por = user
        cobro.confirmado_at = utcnow()
        cobro.referencia_recibo = referencia.strip() or None
        cobro.updated_at = utcnow()
        session.add(cobro)
        cobranza._actividad(session, ticket_id, user, "cobro:confirmado-asesor")
        session.commit()
    log_admin(user, "cobro:confirmado-asesor", f"#{cobro_id} ref={referencia.strip()}")
    return _volver(ticket_id, ok="confirmado")


@router.post("/cobros/{cobro_id}/avisar-pago")
def avisar_pago(request: Request, cobro_id: int):
    """Aviso al cliente de que su pago entró. Fuera del webhook y
    reintentable: con OXXO la confirmación llega a los días, con la ventana de
    24 h ya cerrada."""
    from app.inbox import _window_open

    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro.estado != "pagado":
            return _volver(ticket_id, err="no-pagado")
        ultimo_in = session.exec(
            select(Message)
            .where(Message.phone == cobro.phone, Message.direction == Direction.IN)
            .order_by(Message.created_at.desc())  # type: ignore[union-attr]
        ).first()
        if not _window_open(ultimo_in.created_at if ultimo_in else None):
            return _volver(ticket_id, err="ventana")
        texto = cobranza.mensaje_pagado(cobro)
        phone = cobro.phone

    wamid = whatsapp_client.send_text(phone, texto)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        session.add(Message(phone=phone, direction=Direction.OUT, body=texto,
                            wa_message_id=wamid, sent_by=user))
        cobro.aviso_pago_enviado_at = utcnow()
        session.add(cobro)
        session.commit()
    return _volver(ticket_id, ok="avisado")


# ---------------------------------------------------------------------------
# Retorno de Stripe (lo ve el CLIENTE, sin sesión)
# ---------------------------------------------------------------------------
# Son las success_url/cancel_url que se le mandan a Stripe. Públicas a
# propósito: las abre el cliente desde su celular. Ninguna afirma que el pago
# se aplicó — eso solo lo dice el webhook, y con OXXO/SPEI el cliente llega
# aquí ANTES de ir a pagar.


@router.get("/cobros/gracias")
def retorno_exito(request: Request):
    return templates.TemplateResponse(
        "cobro_retorno.html",
        {
            "request": request,
            "icono": "✅",
            "titulo": "¡Listo, recibimos tu solicitud de pago!",
            "mensaje": "En cuanto el banco nos confirme el cobro, tu asesor "
                       "continúa con tu trámite.",
            "nota": "Si pagaste en OXXO o por transferencia, la confirmación "
                    "puede tardar unas horas. Ya puedes cerrar esta página.",
        },
    )


@router.get("/cobros/cancelado")
def retorno_cancelado(request: Request):
    return templates.TemplateResponse(
        "cobro_retorno.html",
        {
            "request": request,
            "icono": "↩️",
            "titulo": "No se completó el pago",
            "mensaje": "No te preocupes: no se te cobró nada.",
            "nota": "Tu link sigue funcionando. Si tienes dudas, escríbenos por "
                    "WhatsApp y con gusto te ayudamos.",
        },
    )


# ---------------------------------------------------------------------------
# Vistas de administración
# ---------------------------------------------------------------------------


def _modo_real() -> bool:
    return settings.stripe_secret_key.startswith("sk_live")


@router.get("/cobros")
def listado_cobros(request: Request, todos: int = 0):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    # El cron vive en el host y puede no estar dado de alta: abrir esta página
    # también drena la cola de eventos.
    try:
        cobranza_jobs.eventos_pendientes()
    except Exception:
        logger.exception("No se pudo drenar la cola de eventos al abrir /cobros")

    with get_session() as session:
        cobros = list(session.exec(
            select(Cobro).order_by(Cobro.created_at.desc())  # type: ignore[union-attr]
        ).all())

    if not todos:
        # Por defecto solo el modo que corresponde a la llave configurada: un
        # cobro de prueba no puede sumar en los totales de producción.
        real = _modo_real()
        cobros = [c for c in cobros if c.livemode is None or c.livemode == real]

    totales: dict[str, dict[str, int]] = {}
    for cobro in cobros:
        fila = totales.setdefault(cobro.estado, {"n": 0, "centavos": 0, "neto": 0})
        fila["n"] += 1
        fila["centavos"] += cobro.monto_pagado_centavos or cobro.monto_centavos
        fila["neto"] += cobro.neto_centavos or 0

    return templates.TemplateResponse(
        "cobros.html",
        {
            "request": request, "user": admin, "cobros": cobros, "totales": totales,
            "todos": todos, "modo": "real" if _modo_real() else "prueba",
            "stripe_configurado": bool(settings.stripe_secret_key),
        },
    )


def _csv_response(filename: str, header: list[str], rows) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    # BOM UTF-8: sin él Excel (es-MX) muestra mal los acentos.
    return Response(
        content="﻿" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export/cobros.csv")
def export_cobros(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        cobros = session.exec(select(Cobro).order_by(Cobro.created_at)).all()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Montos como ENTEROS CRUDOS y la moneda en su propia columna: un
    # "$1,234.56 MXN" entra a Excel es-MX como texto y SUMA() devuelve 0.
    return _csv_response(
        f"cobros-{stamp}.csv",
        ["id", "folio", "telefono", "cliente", "servicio", "concepto", "moneda",
         "monto_centavos", "monto_pagado_centavos", "monto_reembolsado_centavos",
         "estado", "confirmado_por_asesor", "referencia_recibo", "metodo_usado",
         "msi_meses_usado", "discrepancia", "disputado", "livemode", "creado_por",
         "creado", "pagado"],
        (
            [c.id, c.ticket_id or "", c.phone, c.client_name or "", c.service_id or "",
             c.concepto, c.moneda, c.monto_centavos, c.monto_pagado_centavos or "",
             c.monto_reembolsado_centavos, c.estado, int(c.confirmado_por_asesor),
             c.referencia_recibo or "", c.metodo_usado or "", c.msi_meses_usado or "",
             int(c.discrepancia), int(c.disputado),
             "" if c.livemode is None else int(c.livemode), c.creado_por,
             c.created_at, c.pagado_at or ""]
            for c in cobros
        ),
    )


@router.get("/export/conciliacion.csv")
def export_conciliacion(request: Request):
    """Cruza por stripe_charge_id y payout_id: la suma de neto_centavos de un
    periodo cuadra con el depósito bancario SUMANDO ENTEROS."""
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        cobros = session.exec(
            select(Cobro)
            .where(Cobro.estado.in_(("pagado", "reembolsado")))  # type: ignore[union-attr]
            .order_by(Cobro.pagado_at)
        ).all()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_response(
        f"conciliacion-{stamp}.csv",
        ["id", "folio", "pagado", "charge_id", "balance_transaction_id", "payout_id",
         "moneda", "monto_pagado_centavos", "comision_centavos",
         "monto_reembolsado_centavos", "neto_centavos", "moneda_liquidacion",
         "confirmado_por_asesor", "referencia_recibo", "metodo_usado",
         "msi_meses_usado", "livemode"],
        (
            [c.id, c.ticket_id or "", c.pagado_at or "", c.stripe_charge_id or "",
             c.balance_transaction_id or "", c.payout_id or "",
             c.moneda_pagada or c.moneda, c.monto_pagado_centavos or "",
             c.comision_centavos if c.comision_centavos is not None else "",
             c.monto_reembolsado_centavos,
             c.neto_centavos if c.neto_centavos is not None else "",
             c.moneda_liquidacion or "", int(c.confirmado_por_asesor),
             c.referencia_recibo or "", c.metodo_usado or "", c.msi_meses_usado or "",
             "" if c.livemode is None else int(c.livemode)]
            for c in cobros
        ),
    )


@router.get("/cobros/payouts")
def vista_payouts(request: Request):
    """Un depósito bancario agrupa decenas de cobros ya neteados: sin esta
    vista no hay forma de cuadrar el estado de cuenta contra folios."""
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    with get_session() as session:
        cobros = session.exec(
            select(Cobro)
            .where(Cobro.payout_id.is_not(None))  # type: ignore[union-attr]
            .order_by(Cobro.pagado_at)
        ).all()
        sin_conciliar = len(session.exec(
            select(Cobro)
            .where(Cobro.estado == "pagado")
            .where(Cobro.neto_centavos.is_(None))  # type: ignore[union-attr]
        ).all())

        grupos: dict[str, dict] = {}
        for cobro in cobros:
            grupo = grupos.setdefault(
                cobro.payout_id, {"payout_id": cobro.payout_id, "cobros": [],
                                  "bruto": 0, "comision": 0, "neto": 0}
            )
            grupo["cobros"].append(cobro)
            grupo["bruto"] += cobro.monto_pagado_centavos or 0
            grupo["comision"] += cobro.comision_centavos or 0
            grupo["neto"] += cobro.neto_centavos or 0

        return templates.TemplateResponse(
            "payouts.html",
            {
                "request": request, "user": admin,
                "grupos": sorted(grupos.values(), key=lambda g: g["payout_id"]),
                "sin_conciliar": sin_conciliar,
            },
        )
