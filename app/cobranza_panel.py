"""Panel de cobranza: el asesor genera, reenvía, cancela y concilia.

REGLA DE ORO DEL PROYECTO: el bot NUNCA genera un cobro. El link siempre lo
dispara un humano desde aquí.

Ojo con lo que la regla NO dice: el bot sí COTIZA. Con el catálogo de
producción, 51 de los 58 servicios traen un `price_info` numérico que
`app/conversation.py` le lee al cliente ("¿Confirmas 'X' ($800)?"), y solo 7
siguen diciendo "A cotizar". Cotizar y cobrar son verbos distintos: el bot dice
el precio, el humano manda el link. Ese número cotizado se congela en
`monto_cotizado_centavos` y la regla 14 de `app/cobranza_alertas.py` avisa si
el link termina pidiendo más — `landing/terminos.html` §2 promete al cliente
que la cotización va por delante.

Todo entra por Form(...) y sale con RedirectResponse 303, igual que el resto
del panel. La red a Stripe SIEMPRE ocurre fuera de una transacción abierta de
SQLite: sostener el escritor 10 s tumba el panel con "database is locked".
"""

import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, nulls_last, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app import cobranza, cobranza_jobs, messaging, stripe_client, ventana
from app.audit import log_admin
from app.auth import alcance, is_admin
from app.config import settings
from app.credenciales import credencial
from app.db import get_session
from app.utils import ruta_interna
from app.dinero import MONTO_MIN_CENTAVOS, OXXO_MAX_CENTAVOS, formato_monto, parse_monto
from app.models import (
    Cobro,
    Direction,
    IntentoCobro,
    Message,
    OFICINA_TLAXCALA_ID,
    Ticket,
    utcnow,
)
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

METODOS_VALIDOS = ("card", "oxxo", "spei")
MSI_PLAZOS_VALIDOS = ("3", "6", "9", "12")
# Arriba de este monto se deja rastro en la auditoría de admins: el asesor
# teclea el importe a mano y un cero de más es el riesgo operativo #1.
MONTO_ALTO_CENTAVOS = 2_000_000  # $20,000
# A 1000 cobros/mes son 18,000 filas a los 18 meses: sin paginar, cada visita
# a /cobros las materializaba todas como objetos SQLModel.
COBROS_POR_PAGINA = 100


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _volver(ticket_id: int | None, volver: str = "", **params) -> RedirectResponse:
    """Redirige al folio (o a /cobros) tras una acción de cobro.

    `volver` es la lista de ORIGEN (p.ej. /cobros?pagina=2): se reenvía como
    query del folio para que su botón de regreso sobreviva al POST. Validado
    con ruta_interna y urlencoded — viene de un hidden que cualquiera puede
    fabricar.
    """
    destino = f"/tickets/{ticket_id}" if ticket_id else "/cobros"
    if ruta_interna(volver):
        params["volver"] = quote(volver, safe="")
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
    volver: str = Form(""),
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        ticket = session.get(Ticket, ticket_id)
        # Otra oficina responde igual que «no existe». puede_cobrar no acota
        # por oficina y no puede (vive en cobranza.py, que corre sin sesión):
        # la oficina se compone aquí, antes.
        if ticket is None or not vista.alcanza(ticket.office_id):
            return RedirectResponse("/", status_code=303)
        if not cobranza.puede_cobrar(user, ticket, None):
            # No-op silencioso, como el resto del panel: se registra y se
            # devuelve al folio sin pistas de qué falló.
            logger.warning("Cobro rechazado en #%s por %s", ticket_id, user)
            return _volver(ticket_id, volver=volver)
        phone, client_name = ticket.phone, ticket.client_name
        service_id, service_name = ticket.service_id, ticket.service_name
        price_info = ticket.price_info

    try:
        monto_centavos = parse_monto(monto)
    except ValueError:
        return _volver(ticket_id, volver=volver,err="monto")

    metodos = [m for m in metodo if m in METODOS_VALIDOS] or ["card"]
    if monto_centavos < MONTO_MIN_CENTAVOS:
        return _volver(ticket_id, volver=volver,err="monto-minimo")
    if monto_centavos > settings.cobro_monto_maximo_centavos:
        return _volver(ticket_id, volver=volver,err="monto-maximo")
    if "oxxo" in metodos and monto_centavos > OXXO_MAX_CENTAVOS:
        # Se rechaza ANTES de llamar a Stripe: el voucher no se emitiría.
        return _volver(ticket_id, volver=volver,err="monto-oxxo")

    # ponytail: office_id fijo a Tlaxcala. En el PR 5 la oficina sale del
    # ticket, no de una constante.
    # SIN `session=` a propósito: la sesión de :116 ya cerró y la siguiente
    # abre en :176 — este bloque corre fuera de toda transacción, que es lo que
    # el docstring del módulo exige para que la red a Stripe no sostenga un
    # candado de escritura.
    # ponytail: cuando existan filas (PR 5) esta lectura puede levantar
    # CredencialIlegible; entonces se atrapa y se pinta `ilegible` en vez de
    # reventar la vista. Hoy es inalcanzable: con 0 filas siempre hereda.
    if not credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID):
        # El no-op silencioso es del cliente HTTP; aquí sí se avisa: un cobro
        # fantasma es peor que un error visible.
        return _volver(ticket_id, volver=volver,err="sin-stripe")
    if not settings.public_base_url:
        # Stripe rechaza una success_url relativa con 400: sin la URL pública
        # el 100% de los cobros fallaría con un «se puede reintentar» que no
        # dice por qué. Se corta ANTES de escribir nada.
        return _volver(ticket_id, volver=volver,err="sin-url-publica")

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
            return _volver(ticket_id, volver=volver,ok="cobro")
        if "llave_cobro_vivo" in detalle:
            return _volver(ticket_id, volver=volver,err="cobro-vivo")
        logger.exception("No se pudo crear el cobro del folio #%s", ticket_id)
        return _volver(ticket_id, volver=volver,err="interno")

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
        return _volver(ticket_id, volver=volver,err="stripe")

    enviado = _enviar_link(cobro_id, user)
    return _volver(ticket_id, volver=volver,ok="cobro" if enviado else None,
                   err=None if enviado else "ventana")


# ---------------------------------------------------------------------------
# Envío por WhatsApp (best-effort: nunca cambia el estado del cobro)
# ---------------------------------------------------------------------------


def _enviar_link(cobro_id: int, user: str, reemplazo: bool = False) -> bool:
    """True si salió el mensaje. Que falle NO afecta el estado del cobro: el
    link vive en la fila y el panel ofrece copiarlo."""
    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro is None or not (cobro.url_pago or cobro.voucher_url):
            return False
        if not ventana.abierta_para(session, cobro.phone):
            logger.info("Sin envío del link a %s: ventana de 24h cerrada", cobro.phone)
            return False
        texto = cobranza.mensaje_link(cobro, reemplazo=reemplazo)
        phone, ticket_id = cobro.phone, cobro.ticket_id

    wamid = messaging.send_text(phone, texto)
    if wamid is None:
        # El proveedor rechazó (token vencido, número no habilitado…): el link NO
        # salió. Marcarlo como enviado escondería que el cliente nunca lo vio.
        logger.warning("El link del cobro %s no salió: Meta rechazó el envío", cobro_id)
        return False

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
    vista = alcance(user)
    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro is None:
            return user, None, False
        ticket = session.get(Ticket, cobro.ticket_id) if cobro.ticket_id else None
        # La oficina primero: `creado_por` sobrevive a que su autor cambie de
        # oficina (mover_de_oficina suelta folios, no cobros), y sin esto
        # seguiría cancelando o reenviando cobros de la oficina que dejó.
        permitido = vista.alcanza(cobro.office_id) and cobranza.puede_cobrar(user, ticket, cobro)
        if not permitido:
            logger.warning("Acción de cobro %s rechazada para %s", cobro_id, user)
        return user, cobro.ticket_id, permitido


@router.post("/cobros/{cobro_id}/enviar")
def reenviar_cobro(request: Request, cobro_id: int, volver: str = Form("")):
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id, volver=volver)
    enviado = _enviar_link(cobro_id, user)
    return _volver(ticket_id, volver=volver,ok="enviado" if enviado else None,
                   err=None if enviado else "ventana")


# ---------------------------------------------------------------------------
# Cancelar
# ---------------------------------------------------------------------------


@router.post("/cobros/{cobro_id}/cancelar")
def cancelar_cobro(request: Request, cobro_id: int, volver: str = Form("")):
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id, volver=volver)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro.estado not in cobranza.ESTADOS_VIVOS:
            return _volver(ticket_id, volver=volver,err="cobro-no-cancelable")
        pi = cobro.stripe_payment_intent_id
        cs = cobro.stripe_checkout_session_id
        # Un intento en "enviando" es una llamada a Stripe SIN RESPUESTA: el
        # instrumento pudo haberse creado y nosotros no tener el id todavía.
        intento_vivo = session.exec(
            select(IntentoCobro)
            .where(IntentoCobro.cobro_id == cobro_id)
            .where(IntentoCobro.estado == "enviando")
        ).first() is not None

    # Matar el PaymentIntent —y NO expirar la sesión— es lo único que mata un
    # voucher de OXXO en requires_action.
    if pi:
        respuesta = stripe_client.cancelar_payment_intent(pi)
        confirmado = respuesta.ok and (respuesta.objeto or {}).get("status") == "canceled"
    elif cs:
        respuesta = stripe_client.expirar_sesion(cs)
        confirmado = respuesta.ok and (respuesta.objeto or {}).get("status") == "expired"
    elif intento_vivo:
        # Sin pi ni cs PERO con un POST en vuelo: "no hay instrumento" es una
        # deducción falsa. Soltar el candado aquí deja que el barrido de
        # huérfanos recupere después la sesión que Stripe SÍ creó y la pegue a
        # un cobro ya cancelado que ningún barrido vuelve a mirar — dos
        # instrumentos cobrables para el mismo (teléfono, servicio).
        respuesta, confirmado = None, False
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
            return _volver(ticket_id, volver=volver,ok="cancelado")

        # Stripe no confirmó: el instrumento SIGUE cobrable, así que el
        # candado NO se libera. El barrido reintenta cada noche.
        vence = cobro.voucher_expira_at
        if not pi and not cs:
            motivo = ("hay una llamada a Stripe sin respuesta: no se puede "
                      "confirmar que no exista un instrumento cobrable")
        else:
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
    return _volver(ticket_id, volver=volver,err="cancelacion-pendiente")


# ---------------------------------------------------------------------------
# Consultar a Stripe / confirmar por asesor (adenda A.1)
# ---------------------------------------------------------------------------


@router.post("/cobros/{cobro_id}/reconciliar")
def reconciliar_cobro(request: Request, cobro_id: int, volver: str = Form("")):
    """«Consultar a Stripe ahora»: el resultado entra como evento sintético y
    pasa por el MISMO aplicador que el webhook. Stripe sigue siendo la única
    verdad."""
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id, volver=volver)
    try:
        estado = cobranza_jobs._reconciliar_uno(cobro_id)
    except Exception:
        logger.exception("Consulta manual a Stripe falló para el cobro %s", cobro_id)
        estado = "sin-respuesta"
    return _volver(ticket_id, volver=volver,ok="reconciliado" if estado == "aplicado" else None,
                   err=None if estado == "aplicado" else "sin-novedad")


@router.post("/cobros/{cobro_id}/confirmar")
def confirmar_por_asesor(
    request: Request, cobro_id: int, referencia: str = Form(""), volver: str = Form("")
):
    """Bandera paralela: NO escribe estado='pagado'. El rango 4 es exclusivo
    de Stripe; esto solo desbloquea el trabajo y deja rastro de quién se
    adelantó contra el recibo del cliente."""
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id, volver=volver)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro.estado == "pagado":
            return _volver(ticket_id, volver=volver,err="ya-pagado")
        cobro.confirmado_por_asesor = True
        cobro.confirmado_por = user
        cobro.confirmado_at = utcnow()
        # [:100] en el borde: el espejo declara NVARCHAR(100) y una referencia
        # más larga tumbaba el upsert — y con él todo el backfill de cobranza.
        cobro.referencia_recibo = referencia.strip()[:100] or None
        cobro.updated_at = utcnow()
        session.add(cobro)
        cobranza._actividad(session, ticket_id, user, "cobro:confirmado-asesor")
        session.commit()
    log_admin(user, "cobro:confirmado-asesor", f"#{cobro_id} ref={referencia.strip()}")
    return _volver(ticket_id, volver=volver,ok="confirmado")


@router.post("/cobros/{cobro_id}/avisar-pago")
def avisar_pago(request: Request, cobro_id: int, volver: str = Form("")):
    """Aviso al cliente de que su pago entró. Fuera del webhook y
    reintentable: con OXXO la confirmación llega a los días, con la ventana de
    24 h ya cerrada."""
    user, ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if not permitido:
        return _volver(ticket_id, volver=volver)

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        if cobro.estado != "pagado":
            return _volver(ticket_id, volver=volver,err="no-pagado")
        if not ventana.abierta_para(session, cobro.phone):
            return _volver(ticket_id, volver=volver,err="ventana")
        texto = cobranza.mensaje_pagado(cobro)
        phone = cobro.phone

    wamid = messaging.send_text(phone, texto)
    if wamid is None:
        # El proveedor rechazó el envío: marcar el aviso como enviado apagaría la
        # alerta de pago-tras-cierre sin que el cliente recibiera nada.
        return _volver(ticket_id, volver=volver,err="envio-fallo")

    with get_session() as session:
        cobro = session.get(Cobro, cobro_id)
        session.add(Message(phone=phone, direction=Direction.OUT, body=texto,
                            wa_message_id=wamid, sent_by=user))
        cobro.aviso_pago_enviado_at = utcnow()
        session.add(cobro)
        session.commit()
    return _volver(ticket_id, volver=volver,ok="avisado")


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
        request, "cobro_retorno.html",
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
        request, "cobro_retorno.html",
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
    # Fuente única: `rk_live_` (llave restringida) también es dinero real.
    return stripe_client.modo_real()


def _drenar_eventos() -> None:
    """El cron vive en el host y puede no estar dado de alta: abrir /cobros
    también drena la cola. Corre DESPUÉS de responder — son hasta 200
    transacciones de SQLite y no pueden ir en el camino del render."""
    try:
        cobranza_jobs.eventos_pendientes()
    except Exception:
        logger.exception("No se pudo drenar la cola de eventos al abrir /cobros")


def _filtro_modo(todos: int):
    """Al WHERE, no a una comprensión de lista: filtrar en Python obliga a
    materializar la tabla entera antes de descartar."""
    if todos:
        return None
    # Por defecto solo el modo que corresponde a la llave configurada: un
    # cobro de prueba no puede sumar en los totales de producción.
    real = _modo_real()
    return or_(Cobro.livemode.is_(None), Cobro.livemode == real)  # type: ignore[union-attr]


def _visibles(consulta):
    """Solo los cobros que no se ocultaron (soft delete, `habilitado=False`:
    p. ej. los de prueba antes de arrancar). Es un filtro de las VISTAS —
    listados, totales, cartera, CSV—, no de la lógica de cobro: el candado
    `ux_cobro_vivo`, el webhook y los barridos siguen viéndolos, porque un
    evento real de Stripe siempre se aplica. Los payouts tampoco lo usan: la
    suma tiene que cuadrar con el depósito que Stripe hizo de verdad."""
    return consulta.where(Cobro.habilitado == True)  # noqa: E712


def _agregado(*extra):
    """El SELECT de totales por (estado, confirmado). `extra` antepone columnas
    para abrir otra dimensión —hoy el asesor— sin duplicar las cuatro sumas,
    que duplicadas es exactamente como los números de dos vistas empiezan a no
    cuadrar entre sí.

    `confirmado_por_asesor` entra al GROUP BY, y no es adorno: ese cobro sigue
    `pendiente` a propósito (el rango 4 es exclusivo de Stripe), así que sin
    separarlo la cartera le sigue reclamando al asesor dinero que él ya vio
    entrar por otra vía. Nadie usa dos días una lista que le miente.
    """
    return select(
        *extra,
        Cobro.estado,
        Cobro.confirmado_por_asesor,
        func.count().label("n"),
        func.sum(Cobro.monto_centavos).label("pedido"),
        func.sum(func.coalesce(Cobro.monto_pagado_centavos, 0)).label("recibido"),
        func.sum(func.coalesce(Cobro.neto_centavos, 0)).label("neto"),
    )


def _agregado_viejos():
    """El SELECT de la tarjeta «con más de N días»: cuántos y cuánto falta."""
    return select(
        func.count().label("n"),
        func.sum(
            Cobro.monto_centavos - func.coalesce(Cobro.monto_pagado_centavos, 0)
        ).label("centavos"),
    )


def _viejos(session, consulta) -> dict:
    """La cartera atrasada, agregada en SQL sobre TODO, no sobre la página.

    Aquí está la diferencia entre una columna que informa y una que cambia el
    día de alguien. La lista va por `created_at DESC` y pagina de 100 en 100,
    así que la página 1 es por definición lo MÁS NUEVO: los cobros viejos —los
    únicos por los que se pidió ver la antigüedad— viven en la última página,
    donde nadie mira. Este número no depende de la paginación.

    `consulta` llega ya acotada exactamente igual que la lista (oficina, modo
    y, en la cartera personal, dueño). Si contara una población distinta de la
    que se ve, sería la primera cifra que se deja de creer.
    """
    n, centavos = session.exec(
        consulta
        .where(Cobro.estado.in_(tuple(sorted(cobranza.ESTADOS_VIVOS))))  # type: ignore[attr-defined]
        .where(Cobro.created_at < cobranza.corte_viejo())  # type: ignore[operator]
    ).one()
    return {"n": int(n or 0), "centavos": int(centavos or 0),
            "dias": cobranza.COBRO_VIEJO_DIAS}


def _por_estado(filas_asesor):
    """Pliega las filas (asesor, estado, confirmado) quitándole el asesor.

    Con esto el total global sale de las MISMAS filas que el corte por asesor
    en vez de una segunda consulta, y el «En la calle» de arriba es la suma de
    la columna de abajo *por construcción*, no porque dos SELECT parecidos
    coincidan. De paso /cobros hace un agregado menos.

    Se ordena por el rango de la máquina de estados —el orden en que un cobro
    los recorre— y no por el que devuelva SQLite, que no promete ninguno.
    """
    acumulado: dict[tuple, list] = {}
    for _asesor, estado, confirmado, n, pedido, recibido, neto in filas_asesor:
        fila = acumulado.setdefault((estado, confirmado),
                                    [estado, confirmado, 0, 0, 0, 0])
        fila[2] += n
        fila[3] += int(pedido or 0)
        fila[4] += int(recibido or 0)
        fila[5] += int(neto or 0)
    return [acumulado[k] for k in sorted(
        acumulado, key=lambda k: (cobranza.RANGO.get(k[0], 99), k[0], bool(k[1]))
    )]


def _resumen(filas):
    """Los agregados por estado y los números que resumen una cartera.

    Es UNA función y no dos copias porque `en_la_calle` es el número que el
    admin compara contra la suma de sus asesores: si cada vista lo calculara
    por su cuenta, la primera vez que discrepen nadie sabría cuál miente.

    Se ACUMULA en `totales[estado]` en vez de asignar: llegan hasta dos filas
    por estado (confirmado y no), y asignar se comía la primera en silencio.
    """
    totales: dict[str, dict[str, int]] = {}
    total_n = en_la_calle = recuperado = confirmado_sin_stripe = 0
    for estado, confirmado, n, pedido, recibido, neto in filas:
        pedido, recibido, neto = int(pedido or 0), int(recibido or 0), int(neto or 0)
        total_n += n
        # PEDIDO y RECIBIDO son cosas distintas y el diseño (§6) prohíbe
        # confundirlas: un SPEI parcial escribe monto_pagado dejando el estado
        # en pendiente, así que usar el pagado para los estados vivos hacía que
        # la cartera por cobrar se desplomara justo mientras crecía.
        cobrado = estado in ("pagado", "reembolsado")
        acumula = totales.setdefault(estado, {"n": 0, "centavos": 0, "pedido": 0,
                                              "recibido": 0, "neto": 0,
                                              "confirmado": 0})
        acumula["n"] += n
        acumula["centavos"] += recibido if cobrado else pedido
        acumula["pedido"] += pedido
        acumula["recibido"] += recibido
        acumula["neto"] += neto
        if estado in cobranza.ESTADOS_VIVOS:
            falta = pedido - recibido
            en_la_calle += falta
            if confirmado:
                # NO se descuenta de en_la_calle: hasta que Stripe lo diga, el
                # dinero sigue sin comprobarse. Se reporta aparte para que el
                # asesor sepa cuánto de su cifra ya está en trámite de cerrar.
                confirmado_sin_stripe += falta
                acumula["confirmado"] += falta
        elif estado == "pagado":
            recuperado += recibido
    return totales, total_n, en_la_calle, recuperado, confirmado_sin_stripe


@router.get("/cobros")
def listado_cobros(request: Request, tareas: BackgroundTasks,
                   todos: int = 0, pagina: int = 1):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    pagina = max(1, pagina)
    filtro = _filtro_modo(todos)

    # La oficina se decide UNA vez, aquí, y se aplica a cada consulta: el
    # helper resuelve QUIÉN es y qué alcanza (§5); acotar es de cada consulta,
    # porque un filtro global también taparía al bot, al cron y al espejo.
    vista = alcance(admin)
    with get_session() as session:
        consulta = _visibles(vista.filtrar(select(Cobro), Cobro.office_id))
        # Un solo agregado, abierto por (asesor, estado, confirmado). Por
        # asesor se agrupa por
        # `creado_por` y NO por `assigned_to`, que es la columna que la cartera
        # personal también mira: `assigned_to` se suelta (queda NULL) y se
        # reasigna, así que un corte por él pierde filas y le cambia el pasado
        # a un mes ya cerrado. `creado_por` es permanente y NOT NULL — cada
        # cobro cae en exactamente un asesor y la suma cuadra.
        por_asesor = _visibles(vista.filtrar(_agregado(Cobro.creado_por), Cobro.office_id))
        atrasados = _visibles(vista.filtrar(_agregado_viejos(), Cobro.office_id))
        if filtro is not None:
            consulta = consulta.where(filtro)
            por_asesor = por_asesor.where(filtro)
            atrasados = atrasados.where(filtro)
        viejos = _viejos(session, atrasados)

        cobros = list(session.exec(
            consulta.order_by(Cobro.created_at.desc())  # type: ignore[union-attr]
            .limit(COBROS_POR_PAGINA)
            .offset((pagina - 1) * COBROS_POR_PAGINA)
        ).all())
        # Los totales se agregan en SQL y sobre TODA la cartera, no sobre la
        # página: sumar en Python obligaba a traer las 18,000 filas al heap.
        # (asesor, estado, confirmado) son n_asesores × 16 filas como mucho,
        # no 18,000 — y de ahí se pliega el total global sin otra consulta.
        filas_asesor = session.exec(
            por_asesor.group_by(  # type: ignore[arg-type]
                Cobro.creado_por, Cobro.estado, Cobro.confirmado_por_asesor
            )
        ).all()

    totales, total_n, en_la_calle, recuperado, confirmado_sin_stripe = _resumen(
        _por_estado(filas_asesor)
    )

    agrupado: dict[str, list] = {}
    for asesor, *resto in filas_asesor:
        agrupado.setdefault(asesor or "", []).append(resto)
    asesores = []
    for asesor, propias in agrupado.items():
        _t, n, calle, cobrado, confirmado = _resumen(propias)
        asesores.append({"asesor": asesor, "n": n, "en_la_calle": calle,
                         "recuperado": cobrado, "confirmado": confirmado})
    # Primero quien más dinero trae en la calle: es la lista de a quién
    # preguntarle hoy, no un directorio.
    asesores.sort(key=lambda a: (-a["en_la_calle"], a["asesor"]))

    tareas.add_task(_drenar_eventos)
    return templates.TemplateResponse(
        request, "cobros.html",
        {
            "request": request, "user": admin, "cobros": cobros, "totales": totales,
            "todos": todos, "modo": "real" if _modo_real() else "prueba",
            # Sin `session=`: la de :590 ya cerró en :611.
            # ponytail: cuando existan filas (PR 5) esto puede levantar
            # CredencialIlegible y hay que pintar `ilegible` en vez de reventar
            # la vista principal de cobranza. Hoy con 0 filas siempre hereda.
            # Preexistente, NO se arregla aquí: el modo se resuelve por DOS
            # caminos —_filtro_modo(todos) arma el WHERE y esta línea + :636
            # arman la etiqueta—, así que la lista puede quedar filtrada por un
            # modo y rotulada con otro. Ambos pasan por el mismo credencial(),
            # o sea que este PR no lo empeora; arreglarlo cambia la firma de
            # _filtro_modo y con ella el comportamiento observable.
            "stripe_configurado": bool(
                credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)
            ),
            "en_la_calle": en_la_calle, "recuperado": recuperado,
            "confirmado_sin_stripe": confirmado_sin_stripe,
            "viejos": viejos,
            "asesores": asesores,
            "pagina": pagina, "total_n": total_n,
            "hay_mas": pagina * COBROS_POR_PAGINA < total_n,
        },
    )


def cartera_de(user: str) -> tuple[int, int]:
    """(cuántos cobros vivos, cuánto dinero en la calle) de este colaborador.

    Para la tarjeta de /my, que es la que hace que la cartera exista: una vista
    a la que hay que acordarse de entrar no cambia el comportamiento de nadie.
    Un solo agregado —no la lista— porque /my ya corre cuatro consultas y esta
    se paga en cada carga del panel de trabajo diario.
    """
    filtro = _filtro_modo(0)
    vista = alcance(user)
    consulta = _visibles(cobranza.mios(vista.filtrar(_agregado(), Cobro.office_id), user))
    if filtro is not None:
        consulta = consulta.where(filtro)
    with get_session() as session:
        filas = session.exec(
            consulta.group_by(Cobro.estado, Cobro.confirmado_por_asesor)  # type: ignore[arg-type]
        ).all()
    totales, _n, en_la_calle, _recuperado, _confirmado = _resumen(filas)
    vivos = sum(t["n"] for estado, t in totales.items()
                if estado in cobranza.ESTADOS_VIVOS)
    return vivos, en_la_calle


@router.get("/my/cobros")
def mis_cobros(request: Request, tareas: BackgroundTasks,
               todos: int = 0, pagina: int = 1):
    """La cartera del colaborador: «¿a quién le cobro hoy?».

    NO es admin-only, y esa es toda la idea. Vive aquí y no en `app/my.py`
    porque comparte los agregados con /cobros: la cifra que el asesor ve como
    suya tiene que ser la misma que su jefa ve en el corte por asesor, y eso se
    garantiza compartiendo el código, no repitiéndolo.

    Tampoco es una frontera de seguridad, y conviene decirlo en voz alta: un
    colaborador ya puede abrir cualquier folio de su oficina (`/tickets/{id}`
    solo checa `office_id`) y auto-asignárselo con «Tomar» — la oficina es el
    libro compartido, por diseño. Esto es una COLA DE TRABAJO encima de ese
    libro. Lo que sí cierra la puerta es `alcance()`, que acota por oficina.
    """
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    pagina = max(1, pagina)
    filtro = _filtro_modo(todos)
    vista = alcance(user)

    with get_session() as session:
        # `mios` mete el OUTER JOIN con Ticket. No duplica filas: el join va
        # contra la PK de Ticket, así que cada cobro casa con un renglón o con
        # ninguno — y con ninguno es justo el voucher de OXXO cuyo folio ya
        # cerró, que tiene que seguir apareciéndole a quien lo generó.
        consulta = _visibles(cobranza.mios(vista.filtrar(select(Cobro), Cobro.office_id), user))
        agregado = _visibles(cobranza.mios(vista.filtrar(_agregado(), Cobro.office_id), user))
        # Mismo `mios`: la tarjeta de atrasados tiene que contar la cartera del
        # asesor, no la de la oficina, o le reclama dinero de otro.
        atrasados = _visibles(cobranza.mios(
            vista.filtrar(_agregado_viejos(), Cobro.office_id), user))
        if filtro is not None:
            consulta = consulta.where(filtro)
            agregado = agregado.where(filtro)
            atrasados = atrasados.where(filtro)
        viejos = _viejos(session, atrasados)

        cobros = list(session.exec(
            consulta.order_by(Cobro.created_at.desc())  # type: ignore[union-attr]
            .limit(COBROS_POR_PAGINA)
            .offset((pagina - 1) * COBROS_POR_PAGINA)
        ).all())
        filas = session.exec(
            agregado.group_by(Cobro.estado, Cobro.confirmado_por_asesor)  # type: ignore[arg-type]
        ).all()

    totales, total_n, en_la_calle, recuperado, confirmado_sin_stripe = _resumen(filas)

    tareas.add_task(_drenar_eventos)
    return templates.TemplateResponse(
        request, "mis_cobros.html",
        {
            "request": request, "user": user, "cobros": cobros, "totales": totales,
            "todos": todos, "modo": "real" if _modo_real() else "prueba",
            "en_la_calle": en_la_calle, "recuperado": recuperado,
            "confirmado_sin_stripe": confirmado_sin_stripe,
            "viejos": viejos,
            "pagina": pagina, "total_n": total_n,
            "hay_mas": pagina * COBROS_POR_PAGINA < total_n,
        },
    )


def _csv_stream(filename: str, header: list[str], filas) -> StreamingResponse:
    """Streaming, no un StringIO completo: el CSV de 18,000 cobros se armaba
    entero en memoria antes de mandar el primer byte. `filas` es un generador
    que abre su PROPIA sesión — la del route ya está cerrada cuando el cuerpo
    empieza a producirse.
    """
    def _lineas():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        # BOM UTF-8: sin él Excel (es-MX) muestra mal los acentos.
        yield "﻿"
        for fila in (header, *filas):
            writer.writerow(fila)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

    return StreamingResponse(
        _lineas(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


CSV_LOTE = 500


@router.get("/export/cobros.csv")
def export_cobros(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    vista = alcance(admin)

    def _filas():
        with get_session() as session:
            for c in session.exec(
                _visibles(vista.filtrar(select(Cobro), Cobro.office_id))
                .order_by(Cobro.created_at)  # type: ignore[arg-type]
            ).yield_per(CSV_LOTE):
                yield [
                    c.id, c.ticket_id or "", c.phone, c.client_name or "",
                    c.service_id or "", c.concepto, c.moneda, c.monto_centavos,
                    c.monto_pagado_centavos or "", c.monto_reembolsado_centavos,
                    c.estado, int(c.confirmado_por_asesor), c.referencia_recibo or "",
                    c.metodo_usado or "", c.msi_meses_usado or "",
                    int(c.discrepancia), int(c.disputado),
                    "" if c.livemode is None else int(c.livemode), c.creado_por,
                    c.created_at, c.pagado_at or "",
                ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Montos como ENTEROS CRUDOS y la moneda en su propia columna: un
    # "$1,234.56 MXN" entra a Excel es-MX como texto y SUMA() devuelve 0.
    return _csv_stream(
        f"cobros-{stamp}.csv",
        ["id", "folio", "telefono", "cliente", "servicio", "concepto", "moneda",
         "monto_centavos", "monto_pagado_centavos", "monto_reembolsado_centavos",
         "estado", "confirmado_por_asesor", "referencia_recibo", "metodo_usado",
         "msi_meses_usado", "discrepancia", "disputado", "livemode", "creado_por",
         "creado", "pagado"],
        _filas(),
    )


@router.get("/export/conciliacion.csv")
def export_conciliacion(request: Request):
    """Cruza por stripe_charge_id y payout_id: la suma de neto_centavos de un
    periodo cuadra con el depósito bancario SUMANDO ENTEROS.

    Incluye a los CONFIRMADOS POR ASESOR aunque Stripe no los haya confirmado:
    son justo la población que la adenda A.1 manda distinguir aquí, y filtrando
    solo por estado no aparecía ninguno — la columna `confirmado_por_asesor`
    era letra muerta. Se distinguen por el estado, nunca se suman como iguales.
    """
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    vista = alcance(admin)

    def _filas():
        with get_session() as session:
            consulta = (
                _visibles(vista.filtrar(select(Cobro), Cobro.office_id))
                .where(
                    or_(
                        Cobro.estado.in_(("pagado", "reembolsado")),  # type: ignore[union-attr]
                        Cobro.confirmado_por_asesor == True,  # noqa: E712
                    )
                )
                # Los confirmados por asesor no tienen pagado_at: al final.
                .order_by(nulls_last(Cobro.pagado_at.asc()))  # type: ignore[union-attr]
            )
            for c in session.exec(consulta).yield_per(CSV_LOTE):
                yield [
                    c.id, c.ticket_id or "", c.estado, c.pagado_at or "",
                    c.phone, c.client_name or "", c.concepto, c.creado_por,
                    c.stripe_charge_id or "", c.balance_transaction_id or "",
                    c.payout_id or "", c.moneda_pagada or c.moneda,
                    c.monto_pagado_centavos or "",
                    c.comision_centavos if c.comision_centavos is not None else "",
                    c.monto_reembolsado_centavos,
                    c.neto_centavos if c.neto_centavos is not None else "",
                    c.moneda_liquidacion or "", int(c.confirmado_por_asesor),
                    c.referencia_recibo or "", c.metodo_usado or "",
                    c.msi_meses_usado or "",
                    "" if c.livemode is None else int(c.livemode),
                ]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_stream(
        f"conciliacion-{stamp}.csv",
        ["id", "folio", "estado", "pagado", "telefono", "cliente", "concepto",
         "creado_por", "charge_id", "balance_transaction_id", "payout_id",
         "moneda", "monto_pagado_centavos", "comision_centavos",
         "monto_reembolsado_centavos", "neto_centavos", "moneda_liquidacion",
         "confirmado_por_asesor", "referencia_recibo", "metodo_usado",
         "msi_meses_usado", "livemode"],
        _filas(),
    )


PAYOUTS_DIAS = 90


@router.get("/cobros/payouts")
def vista_payouts(request: Request, dias: int = PAYOUTS_DIAS):
    """Un depósito bancario agrupa decenas de cobros ya neteados: sin esta
    vista no hay forma de cuadrar el estado de cuenta contra folios.

    Acotada por ventana: sin ella la consulta crece sin techo y a los dos años
    son todos los cobros pagados de la historia para pintar los depósitos de
    esta quincena.
    """
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    dias = max(1, min(dias, 1095))
    corte = cobranza.naive_utc(utcnow() - timedelta(days=dias))

    vista = alcance(admin)
    with get_session() as session:
        cobros = session.exec(
            vista.filtrar(select(Cobro), Cobro.office_id)
            .where(Cobro.payout_id.is_not(None))  # type: ignore[union-attr]
            .where(Cobro.pagado_at >= corte)
            .order_by(Cobro.pagado_at)  # type: ignore[arg-type]
        ).all()
        # COUNT en SQL: el len(...all()) traía cada fila al heap para contarlas.
        sin_conciliar = session.exec(
            select(func.count())
            .select_from(Cobro)
            .where(Cobro.estado == "pagado")
            .where(Cobro.neto_centavos.is_(None))  # type: ignore[union-attr]
        ).one()

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
            request, "payouts.html",
            {
                "request": request, "user": admin,
                "grupos": sorted(grupos.values(), key=lambda g: g["payout_id"]),
                "sin_conciliar": sin_conciliar, "dias": dias,
            },
        )
