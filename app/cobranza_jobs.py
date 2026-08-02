"""Los cinco barridos de cobranza: la red de seguridad del módulo.

Sin esto, "el webhook nunca llegó" significa perder dinero. Con esto se
degrada a un retraso de una noche, sin que nadie toque la base.

Todos son idempotentes y no tienen orden entre sí. Cada elemento corre en su
propia transacción para que un fallo aislado no arrastre al lote.

REGLA QUE NO SE NEGOCIA: ningún barrido crea un instrumento de cobro a
ciegas. Reintentar el POST con la MISMA clave de idempotencia es seguro
(Stripe devuelve el objeto original); reintentarlo con la clave ya caducada
—más de 24 h— crea uno NUEVO, y ese es el único camino real a un doble cargo.
Pasadas las 24 h solo se busca; si no aparece, se llama a un humano.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, nulls_first, or_
from sqlmodel import select

from app import cobranza, stripe_client
from app.db import get_session
from app.models import Cobro, EventoPasarela, IntentoCobro, utcnow

logger = logging.getLogger(__name__)

# Un intento recién lanzado no es huérfano: margen para que el POST normal
# termine y cierre su propia fila.
INTENTO_HUERFANO_MINUTOS = 2
# A las 24 h la clave de idempotencia CADUCA en Stripe.
CLAVE_VIGENCIA_HORAS = 24
# Tras 3 barridos sin resolver se deja de intentar y se levanta la alerta.
MAX_BARRIDOS = 3
# Un cobro vivo sin novedades en 2 h merece que le preguntemos a Stripe.
RECONCILIAR_HORAS = 2
# Tope por corrida: el barrido nocturno no puede quedarse toda la noche.
LOTE = 200

ESTADOS_VIVOS_RECONCILIABLES = ("pendiente", "cancelacion_solicitada")

# Estado real del PaymentIntent -> evento equivalente. Los que faltan
# (requires_action, processing, requires_payment_method) son instrumentos
# VIVOS: no se traducen, porque ningún reloj local escribe estados terminales.
_ESTADO_PI_A_EVENTO = {
    "succeeded": "payment_intent.succeeded",
    "canceled": "payment_intent.canceled",
}

# Un PI que Stripe canceló por su cuenta es un voucher VENCIDO, no una
# cancelación del asesor: mismo rango, pero el panel y el cliente leen cosas
# distintas ("venció tu ficha" vs "cancelamos tu cobro").
_CANCELACIONES_AUTOMATICAS = {"automatic", "abandoned"}


# ---------------------------------------------------------------------------
# 1. Eventos que llegaron pero no se aplicaron
# ---------------------------------------------------------------------------


def _contar_fallo(evento_id: int) -> None:
    try:
        with get_session() as session:
            evento = session.get(EventoPasarela, evento_id)
            if evento is not None:
                evento.intentos_proceso += 1
                session.add(evento)
                session.commit()
    except Exception:  # pragma: no cover — contar el fallo es best-effort
        logger.exception("No se pudo contar el fallo del evento id=%s", evento_id)


def eventos_pendientes(limite: int = LOTE) -> dict:
    """Cola de trabajo: eventopasarela con procesado_at NULL, en orden de
    (stripe_created, id) — el reloj de Stripe, no el nuestro."""
    with get_session() as session:
        ids = [
            e.id
            for e in session.exec(
                select(EventoPasarela)
                .where(EventoPasarela.procesado_at.is_(None))  # type: ignore[union-attr]
                .order_by(EventoPasarela.stripe_created, EventoPasarela.id)  # type: ignore[arg-type]
                .limit(limite)
            ).all()
        ]

    aplicados = fallidos = 0
    for evento_id in ids:
        try:
            with get_session() as session:
                # Reclamo del ESCRITOR como primera operación de la
                # transacción: el cron y el BackgroundTasks del panel corren
                # este mismo drenado desde procesos distintos, y dos lectores
                # con el mismo snapshot aplicaban el evento dos veces (el
                # segundo pisaba lo del primero). El UPDATE toma el candado de
                # escritura de SQLite desde el arranque y serializa; tras un
                # crash el evento sigue con procesado_at NULL y se retoma.
                filas = session.connection().exec_driver_sql(
                    "UPDATE eventopasarela SET resultado = 'reclamado' "
                    "WHERE id = ? AND procesado_at IS NULL",
                    (evento_id,),
                ).rowcount
                if not filas:
                    continue  # otro drenador ya lo procesó
                evento = session.get(EventoPasarela, evento_id)
                if evento is None or evento.procesado_at is not None:
                    continue
                cobranza.aplicar_evento(session, evento)
                session.commit()
            aplicados += 1
        except Exception:
            logger.exception("No se pudo aplicar el evento id=%s", evento_id)
            _contar_fallo(evento_id)
            fallidos += 1
    return {"pendientes": len(ids), "aplicados": aplicados, "fallidos": fallidos}


# ---------------------------------------------------------------------------
# 2. Intentos huérfanos ("el proceso murió y no sé si Stripe lo creó")
# ---------------------------------------------------------------------------


def _buscar_instrumento(cobro: Cobro, intento: IntentoCobro) -> dict | None:
    """Busca en Stripe el objeto que el POST pudo haber creado.

    La Search API es la única que filtra por metadata —y por eso la metadata
    se manda DOS veces, también dentro de payment_intent_data—; como es
    eventualmente consistente (hasta ~1 min), se respalda con la ventana de
    `created` sobre Checkout Sessions, filtrando la metadata de este lado.
    """
    respuesta = stripe_client.buscar_payment_intents_por_metadata("cobro_id", str(cobro.id))
    if respuesta.ok:
        for pi in (respuesta.objeto or {}).get("data") or []:
            if (pi.get("metadata") or {}).get("token") == cobro.token_verificacion:
                return pi

    solicitado = int(cobranza.aware_utc(intento.solicitado_at).timestamp())
    respuesta = stripe_client.buscar_sesiones_por_ventana(solicitado - 60, solicitado + 300)
    if respuesta.ok:
        for sesion in (respuesta.objeto or {}).get("data") or []:
            if (sesion.get("metadata") or {}).get("token") == cobro.token_verificacion:
                return sesion
    return None


def _sin_resolver(session, intento: IntentoCobro, cobro: Cobro, ya_contado: bool) -> str:
    """Sigue sin saberse si Stripe creó el instrumento. NUNCA se crea otro."""
    if not ya_contado:
        intento.reintentos += 1
        session.add(intento)
    if intento.reintentos >= MAX_BARRIDOS:
        cobranza.alerta(
            session, "cobro:intento-irresoluble",
            "Cobro en limbo: requiere revisión manual",
            f"El intento {intento.clave_idempotencia} (cobro #{cobro.id}) no se "
            f"resolvió tras {intento.reintentos} barridos. Búscalo en el dashboard "
            f"de Stripe por metadata token={cobro.token_verificacion} ANTES de "
            "generar otro link: crear a ciegas es exactamente el cobro duplicado.",
            level="critical",
        )
        session.commit()
        return "irresoluble"
    session.commit()
    return "sin-resolver"


def _resolver_intento(intento_id: int, ahora: datetime) -> str:
    with get_session() as session:
        intento = session.get(IntentoCobro, intento_id)
        if intento is None or intento.estado != "enviando":
            return "resuelto"  # alguien más lo cerró
        cobro = session.get(Cobro, intento.cobro_id)
        if cobro is None:
            # Sin cobro no hay nada que reclamar: se cierra la fila para que
            # ux_intento_vivo no bloquee ese cobro para siempre.
            intento.estado = "abandonado"
            intento.cerrado_at = utcnow()
            session.add(intento)
            session.commit()
            return "resuelto"

        edad = ahora - cobranza.aware_utc(intento.solicitado_at)
        if edad < timedelta(hours=CLAVE_VIGENCIA_HORAS):
            # Clave viva: repetir el MISMO POST es seguro. Stripe devuelve el
            # objeto original; jamás crea un segundo.
            respuesta = stripe_client.crear_checkout_session(
                **cobranza.parametros_checkout(cobro, intento)
            )
        else:
            # Clave caducada: reusarla CREARÍA un objeto nuevo. Solo se busca.
            objeto = _buscar_instrumento(cobro, intento)
            if objeto is None:
                return _sin_resolver(session, intento, cobro, ya_contado=False)
            respuesta = stripe_client.RespuestaStripe(ok=True, objeto=objeto)

        resultado = cobranza.aplicar_respuesta_stripe(session, cobro, intento, respuesta)
        if resultado == "indeterminado":
            # aplicar_respuesta_stripe ya contó este reintento.
            return _sin_resolver(session, intento, cobro, ya_contado=True)
        session.commit()
        return "resuelto"


def intentos_huerfanos() -> dict:
    ahora = utcnow()
    corte = cobranza.naive_utc(ahora - timedelta(minutes=INTENTO_HUERFANO_MINUTOS))
    with get_session() as session:
        ids = [
            i.id
            for i in session.exec(
                select(IntentoCobro)
                .where(IntentoCobro.estado == "enviando")
                .where(IntentoCobro.solicitado_at < corte)
                .limit(LOTE)
            ).all()
        ]

    resueltos = sin_resolver = irresolubles = 0
    for intento_id in ids:
        try:
            estado = _resolver_intento(intento_id, ahora)
        except Exception:
            logger.exception("Barrido de huérfanos falló en el intento id=%s", intento_id)
            estado = "sin-resolver"
        if estado == "resuelto":
            resueltos += 1
        elif estado == "irresoluble":
            irresolubles += 1
        else:
            sin_resolver += 1
    return {"huerfanos": len(ids), "resueltos": resueltos,
            "sin_resolver": sin_resolver, "irresolubles": irresolubles}


# ---------------------------------------------------------------------------
# 3. Detalle del voucher / ficha SPEI (el aplicador no hace red)
# ---------------------------------------------------------------------------


def completar_voucher() -> dict:
    with get_session() as session:
        ids = [
            c.id
            for c in session.exec(
                select(Cobro)
                .where(Cobro.detalle_pendiente_at.is_not(None))  # type: ignore[union-attr]
                .where(Cobro.stripe_payment_intent_id.is_not(None))  # type: ignore[union-attr]
                .limit(LOTE)
            ).all()
        ]

    completados = fallidos = 0
    for cobro_id in ids:
        try:
            with get_session() as session:
                cobro = session.get(Cobro, cobro_id)
                if cobro is None or not cobro.stripe_payment_intent_id:
                    continue
                respuesta = stripe_client.obtener_payment_intent(
                    cobro.stripe_payment_intent_id
                )
                if not respuesta.ok:
                    fallidos += 1
                    continue
                evento = cobranza.registrar_evento_sintetico(
                    session,
                    stripe_event_id=f"cron:voucher:{cobro.id}:{utcnow().isoformat()}",
                    tipo="cobro.detalle",
                    cobro=cobro,
                    objeto=respuesta.objeto,
                    livemode=(respuesta.objeto or {}).get("livemode"),
                )
                session.flush()
                cobranza.aplicar_evento(session, evento)
                if cobro.detalle_pendiente_at is not None:
                    # El PI ya no ofrece ficha que traer (pagó, se canceló o es
                    # tarjeta): sacarlo de la cola o el barrido lo mira para
                    # siempre.
                    cobro.detalle_pendiente_at = None
                    session.add(cobro)
                session.commit()
            completados += 1
        except Exception:
            logger.exception("No se pudo completar el voucher del cobro id=%s", cobro_id)
            fallidos += 1
    return {"pendientes": len(ids), "completados": completados, "fallidos": fallidos}


# ---------------------------------------------------------------------------
# 4. Reconciliación contra Stripe (el webhook que nunca llegó)
# ---------------------------------------------------------------------------


def _tipo_desde_objeto(objeto: dict) -> str | None:
    """Traduce el estado REAL que reporta Stripe al evento equivalente.

    Devuelve None si el instrumento sigue vivo: un voucher en requires_action
    no está vencido aunque nuestro reloj diga que sí.
    """
    clase = objeto.get("object")
    if clase == "payment_intent":
        estado = objeto.get("status") or ""
        if estado == "canceled" and (
            objeto.get("cancellation_reason") in _CANCELACIONES_AUTOMATICAS
        ):
            return "cobro.vencido"
        return _ESTADO_PI_A_EVENTO.get(estado)
    if clase == "checkout.session":
        if objeto.get("payment_status") == "paid":
            return "checkout.session.completed"
        if objeto.get("status") == "expired":
            return "checkout.session.expired"
    return None


def _consultar_instrumento(cobro: Cobro) -> dict | None:
    if cobro.estado == "cancelacion_solicitada" and cobro.stripe_payment_intent_id:
        # Primero se reintenta MATAR el instrumento; solo si Stripe confirma
        # se libera el candado (lo hace el aplicador con el evento).
        respuesta = stripe_client.cancelar_payment_intent(cobro.stripe_payment_intent_id)
        if respuesta.ok:
            return respuesta.objeto
    if cobro.stripe_payment_intent_id:
        respuesta = stripe_client.obtener_payment_intent(cobro.stripe_payment_intent_id)
        return respuesta.objeto if respuesta.ok else None
    if cobro.stripe_checkout_session_id:
        respuesta = stripe_client.obtener_session(cobro.stripe_checkout_session_id)
        return respuesta.objeto if respuesta.ok else None
    return None


def _reconciliar_uno(cobro_id: int) -> str:
    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro is None:
            return "omitido"
        objeto = _consultar_instrumento(cobro)
        # Se sella SIEMPRE, incluso sin respuesta: es lo que rota la cola. Sin
        # esto, un cobro que Stripe no contesta se queda al frente del ORDER BY
        # para siempre y mata de hambre a todos los que vienen detrás.
        cobro.reconciliado_at = utcnow()
        session.add(cobro)
        if objeto is None:
            session.commit()
            return "sin-respuesta"

        tipo = _tipo_desde_objeto(objeto)
        if tipo is None:
            session.commit()
            return "sin-cambio"  # sigue vivo: no se toca el estado

        evento = cobranza.registrar_evento_sintetico(
            session,
            stripe_event_id=f"cron:recon:{cobro.id}:{utcnow().isoformat()}",
            tipo=tipo,
            cobro=cobro,
            objeto=objeto,
            livemode=objeto.get("livemode"),
        )
        session.flush()
        cobranza.aplicar_evento(session, evento)
        session.commit()
        return "aplicado"


def reconciliar_pendientes() -> dict:
    ahora = utcnow()
    corte = cobranza.naive_utc(ahora - timedelta(hours=RECONCILIAR_HORAS))
    ahora_naive = cobranza.naive_utc(ahora)
    with get_session() as session:
        # El filtro va en SQL, NO en Python: con .limit() antes de filtrar,
        # SQLite devuelve las primeras 200 filas por rowid —las más viejas— y
        # el cobro de OXXO generado hoy, cuyo webhook se perdió, no entra al
        # lote nunca. El ORDER BY por reconciliado_at es lo que rota la cola.
        ids = [
            c.id
            for c in session.exec(
                select(Cobro)
                .where(Cobro.estado.in_(ESTADOS_VIVOS_RECONCILIABLES))  # type: ignore[union-attr]
                .where(
                    or_(
                        Cobro.updated_at < corte,
                        and_(
                            Cobro.voucher_expira_at.is_not(None),  # type: ignore[union-attr]
                            Cobro.voucher_expira_at < ahora_naive,
                        ),
                    )
                )
                .order_by(nulls_first(Cobro.reconciliado_at.asc()))  # type: ignore[union-attr]
                .limit(LOTE)
            ).all()
        ]

    aplicados = sin_cambio = fallidos = 0
    for cobro_id in ids:
        try:
            estado = _reconciliar_uno(cobro_id)
        except Exception:
            logger.exception("Reconciliación falló en el cobro id=%s", cobro_id)
            estado = "sin-respuesta"
        if estado == "aplicado":
            aplicados += 1
        elif estado == "sin-cambio":
            sin_cambio += 1
        else:
            fallidos += 1
    return {"revisados": len(ids), "aplicados": aplicados,
            "sin_cambio": sin_cambio, "fallidos": fallidos}


# ---------------------------------------------------------------------------
# 5. Comisión y neto (para cuadrar contra el depósito bancario)
# ---------------------------------------------------------------------------


def conciliar_montos() -> dict:
    with get_session() as session:
        ids = [
            c.id
            for c in session.exec(
                select(Cobro)
                .where(Cobro.estado.in_(("pagado", "reembolsado")))  # type: ignore[union-attr]
                .where(Cobro.neto_centavos.is_(None))  # type: ignore[union-attr]
                .where(Cobro.stripe_charge_id.is_not(None))  # type: ignore[union-attr]
                .limit(LOTE)
            ).all()
        ]

    conciliados = fallidos = 0
    for cobro_id in ids:
        try:
            with get_session() as session:
                cobro = session.get(Cobro, cobro_id)
                if cobro is None or not cobro.stripe_charge_id:
                    continue
                txn_id = cobro.balance_transaction_id
                # Sin balance transaction hay que releer el charge. Es también
                # el camino del reembolso notificado con un objeto `refund`:
                # el aplicador borra balance_transaction_id justo para forzar
                # esta relectura y refrescar `amount_refunded`.
                if not txn_id:
                    charge = stripe_client.obtener_charge(cobro.stripe_charge_id)
                    if not charge.ok:
                        fallidos += 1
                        continue
                    objeto = charge.objeto or {}
                    txn = objeto.get("balance_transaction")
                    txn_id = txn.get("id") if isinstance(txn, dict) else txn
                    if objeto.get("amount_refunded") is not None:
                        cobro.monto_reembolsado_centavos = objeto["amount_refunded"]
                if not txn_id:
                    fallidos += 1
                    continue
                respuesta = stripe_client.obtener_balance_transaction(txn_id)
                if not respuesta.ok:
                    fallidos += 1
                    continue
                bt = respuesta.objeto or {}
                cobro.balance_transaction_id = bt.get("id") or txn_id
                # LEÍDOS de Stripe, jamás calculados con una tarifa: con MSI la
                # comisión no es plana y el plazo lo eligió el cliente.
                cobro.comision_centavos = bt.get("fee")
                neto = bt.get("net")
                if neto is not None:
                    # En México Stripe NO devuelve la comisión al reembolsar,
                    # así que el reembolso se resta del neto original.
                    cobro.neto_centavos = neto - (cobro.monto_reembolsado_centavos or 0)
                cobro.moneda_liquidacion = bt.get("currency")
                payout = bt.get("payout")
                cobro.payout_id = payout.get("id") if isinstance(payout, dict) else payout
                cobro.updated_at = utcnow()
                session.add(cobro)
                session.commit()
            conciliados += 1
        except Exception:
            logger.exception("No se pudo conciliar el cobro id=%s", cobro_id)
            fallidos += 1
    return {"por_conciliar": len(ids), "conciliados": conciliados, "fallidos": fallidos}


# ---------------------------------------------------------------------------


BARRIDOS = (
    eventos_pendientes,
    intentos_huerfanos,
    completar_voucher,
    reconciliar_pendientes,
    conciliar_montos,
)


def barrer_todo() -> dict:
    """Los cinco, best-effort e independientes (mismo patrón que
    `_evaluate_alerts_safe`): que uno falle no tumba a los otros ni al job."""
    resultados: dict[str, dict] = {}
    for barrido in BARRIDOS:
        try:
            resultados[barrido.__name__] = barrido()
        except Exception as exc:
            logger.warning("Barrido %s falló: %s", barrido.__name__, exc)
            resultados[barrido.__name__] = {"error": str(exc)}
    return resultados
