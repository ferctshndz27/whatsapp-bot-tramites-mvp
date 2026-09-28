"""Ficha del cliente: historial y memoria compartida del equipo.

Todo lo que un colaborador necesita antes de responder: trámites anteriores,
notas internas de otros compañeros ("ya pagó anticipo", "prefiere llamadas")
y acceso directo al chat. La memoria deja de vivir en la cabeza de cada quien.
"""

import logging

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse
from app.templating import templates
from sqlmodel import or_, select

from app import asignacion, cartera
from app.audit import log_admin
from app.auth import alcance, is_admin
from app.db import get_session
from app.codigos import EMAIL_RE
from app.models import Client, ClientNote, Ticket, TicketStatus, User, utcnow

# El select de la ficha en client.html ofrece exactamente estos valores.
_ORIGENES = {"meta", "twilio", "referido", "manual"}
from app.utils import ruta_interna
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_NOMBRE = 200
# Tope de la lista: a este volumen sobra y evita renderizar miles de filas.
# ponytail: paginar si el directorio pasa de unos miles de clientes.
LIMITE_LISTA = 300


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _cliente_visible(session, phone: str, user: str) -> Client | None:
    """El cliente, si existe Y es de una oficina que este usuario alcanza.

    El teléfono viene en la URL: acotar el LISTADO no basta. De otra oficina
    responde igual que «no existe» —distinguirlas confirmaría qué teléfonos
    son clientes—, y eso vale para leer la ficha y para escribirle.
    """
    client = session.get(Client, phone)
    if client is None or not alcance(user).alcanza(client.office_id):
        if client is not None:
            logger.warning("%s intentó abrir un cliente de otra oficina", user)
        return None
    return client


def folio_vigente(folios, user: str | None = None) -> Ticket | None:
    """El folio con el que se mira y se actúa sobre una conversación. `folios`
    viene ordenado del más nuevo al más viejo (`created_at desc`).

    La regla de siempre: el ABIERTO más reciente; si no hay abierto, el más
    reciente. Con `user` (un colaborador que mira), antes que eso SU folio
    abierto más reciente: una conversación puede tener folios de dueños
    distintos —la asignación reparte por servicio—, y cada quien ve y escribe
    sobre el suyo (decisión del operador, 2026-09-25). Su folio CERRADO no
    cuenta: no le devuelve una conversación que ahora atiende otro.

    Una sola definición a propósito: es la que pinta la tarjeta del CRM
    (`crm._agregar`), la que decide quién puede escribirle al cliente
    (`conversacion_escribible`) y la que toma quien contesta (`inbox._pause_bot`).
    Con dos copias, la lectura y la escritura volverían a divergir, que es justo
    el hallazgo AUTHZ-2."""
    if user is not None:
        for t in folios:
            if t.assigned_to == user and t.status != TicketStatus.CERRADO:
                return t
    vigente = None
    for t in folios:
        if vigente is None or (
            vigente.status == TicketStatus.CERRADO and t.status != TicketStatus.CERRADO
        ):
            vigente = t
    return vigente


def _folios_de(session, phone: str) -> list[Ticket]:
    return list(
        session.exec(
            select(Ticket)
            .where(Ticket.phone == phone)
            .where(Ticket.habilitado == True)  # noqa: E712
            # `id` desempata a propósito: dos folios con el mismo created_at
            # —la ventana que abre la carrera de confirmaciones (test_concurrency)—
            # quedaban al orden físico de SQLite, y desde AUTHZ-2 esta elección
            # decide quién puede escribirle al cliente. Misma lección que
            # crm._agregar aplica al último mensaje: el id no depende de un reloj.
            .order_by(Ticket.created_at.desc(), Ticket.id.desc())  # type: ignore[union-attr]
        ).all()
    )


def folio_vigente_de(session, phone: str) -> Ticket | None:
    """El vigente de siempre, sin preferencia por nadie (lo que ve un admin)."""
    return folio_vigente(_folios_de(session, phone))


def folio_de(session, phone: str, user: str, admin: bool | None = None) -> Ticket | None:
    """El folio con el que `user` mira y actúa sobre esta conversación. Un admin
    no tiene preferencia: sigue viendo el vigente de siempre.

    Quien llama con escrituras pendientes pasa `admin` ya resuelto: `is_admin`
    abre su propia sesión, y con el engine de la suite (una sola conexión) su
    cierre deshace lo que la transacción de afuera ya había escrito (lo documenta
    asignacion._por_producto)."""
    if admin is None:
        admin = is_admin(user)
    return folio_vigente(_folios_de(session, phone), None if admin else user)


def conversacion_escribible(session, phone: str, user: str) -> Client | None:
    """El cliente, si este usuario puede ESCRIBIRLE (mandar WhatsApp como el
    negocio, pausar o reactivar su bot, ocultar mensajes, bloquearlo); None si no.

    Corte de oficina como la lectura (`_cliente_visible`), y para quien no es
    admin, SU folio (`folio_vigente` con `user`) tiene que poder ser suyo:
    `asignacion.puede_tomar` —suyo, sin dueño, o provisional de un servicio que
    él atiende o que nadie de la oficina atiende—, porque contestar se lo queda
    (`inbox._pause_bot`). Un folio TOMADO por otro, o ningún folio, cierra la
    puerta: antes bastaba la oficina y un colaborador sin folios del cliente le
    mandaba WhatsApp firmado por el negocio mientras el CRM le negaba el chat
    (auditoría 2026-09, AUTHZ-2). Sin fila Client tampoco.

    La CARTERA no entra aquí, a propósito (decisión del operador, 2026-09-25):
    deja ver la conversación (`crm._conversacion_visible`), nunca escribirle.
    Con ella, el titular podía contestar sobre el trámite que otro ya tomó.

    Más ancha que la lectura del CRM (`crm._atiende`) en un solo punto, el folio
    que se puede tomar: la «cola abierta» del reparto que ya existía —el tablero
    de Solicitudes lista a la oficina los folios NUEVO, y contestar uno se lo
    queda quien contesta—, ahora acotada a quien atiende el servicio para que
    la división por servicio no se deshaga al primer mensaje.
    """
    cliente = _cliente_visible(session, phone, user)
    if cliente is None or is_admin(user):
        return cliente
    folio = folio_vigente(_folios_de(session, phone), user)
    if folio is None:
        return None
    if asignacion.puede_tomar(session, folio, user):
        return cliente
    logger.warning("%s intentó escribirle a un cliente cuyo folio atiende otro", user)
    return None


def _propagar_nombre(session, phone: str, nombre: str) -> int:
    """Reescribe el nombre congelado en los folios de ese teléfono.

    Cada folio guarda `client_name` como copia del nombre que tenía el cliente
    al crearse, así que corregir `Client.name` no se veía en Solicitudes ni en
    Mi día: seguían mostrando el nombre viejo.

    Se actualiza folio por folio con el ORM, NO con un UPDATE masivo: el espejo
    MSSQL se alimenta del evento `after_update` de cada instancia y un UPDATE en
    bloque no lo dispara — la base local quedaría corregida y el espejo con el
    nombre viejo, sin error visible.
    """
    folios = session.exec(select(Ticket).where(Ticket.phone == phone)).all()
    desfasados = [t for t in folios if t.client_name != nombre]
    for folio in desfasados:
        folio.client_name = nombre
        session.add(folio)
    return len(desfasados)


# Los avisos de /clients viajan por la URL como CÓDIGO y se traducen aquí: un
# texto libre en ?error= se pintaba (escapado) dentro del panel y servía para
# suplantar contenido con un enlace (auditoría 2026-09, INJ-5).
_ERRORES = {
    "telefono": "Teléfono inválido: usa 10 dígitos, o 52 + 10 dígitos.",
    "nombre": "El nombre es obligatorio.",
    "otra_oficina": "Ese teléfono ya es cliente de otra oficina.",
}


@router.get("/clients")
def clients_list(request: Request, q: str = "", alta: str = "", error: str = "", mios: str = ""):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    termino = q.strip()
    vista = alcance(user)
    with get_session() as session:
        consulta = vista.filtrar(select(Client), Client.office_id).where(
            Client.habilitado == True  # noqa: E712
        )
        if mios:
            # «Mis clientes»: los de su cartera. En SQL, antes del tope de LIMITE_LISTA.
            consulta = consulta.where(Client.phone.in_(cartera.telefonos_de(user)))
        if termino:
            # Se busca por nombre y por teléfono. El teléfono se normaliza para
            # que buscar "+52 155 0000 0000" encuentre al cliente guardado como
            # "525500000000": si no, el asesor concluye que no existe y lo da de
            # alta otra vez.
            patron = f"%{termino}%"
            canonico = normalize_phone(termino)
            condiciones = [Client.name.ilike(patron), Client.phone.like(patron)]
            if canonico:
                condiciones.append(Client.phone == canonico)
            consulta = consulta.where(or_(*condiciones))
        clientes = session.exec(
            consulta.order_by(Client.created_at.desc()).limit(LIMITE_LISTA)
        ).all()

    return templates.TemplateResponse(
        request, "clients_list.html",
        {
            "request": request,
            "user": user,
            "clients": clientes,
            "q": termino,
            "alta": alta,
            "error": _ERRORES.get(error, ""),
            "truncada": len(clientes) == LIMITE_LISTA,
            "mios": bool(mios),
        },
    )


@router.post("/clients")
def clients_upsert(
    request: Request,
    phone: str = Form(""),
    name: str = Form(""),
    a_mi_cartera: str = Form("", alias="cartera"),
):
    """Alta manual, o actualización del nombre si el teléfono ya existe.

    Deliberadamente NO toca `habilitado`, `created_at` ni la conversación: dar
    de alta a alguien que ya existe actualiza su nombre y nada más. En
    particular no sirve para desbloquear a un cliente bloqueado ni para
    reiniciar el flujo del bot de alguien que va a media conversación.

    La casilla «Agregarlo a mi cartera» (marcada por omisión) mete al cliente en
    la cartera de quien lo registra; si ya existía en su oficina, queda
    compartido. Es explícita para que un admin que captura clientes del equipo
    no se quede con todos: la cartera decide a quién le llegan sus folios.
    """
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    canonico = normalize_phone(phone)
    nombre = name.strip()[:MAX_NOMBRE]
    if not canonico:
        return RedirectResponse(
            "/clients?error=telefono",
            status_code=303,
        )
    if not nombre:
        return RedirectResponse("/clients?error=nombre", status_code=303)

    vista = alcance(user)
    with get_session() as session:
        cliente = session.get(Client, canonico)
        if cliente is not None and not vista.alcanza(cliente.office_id):
            # El teléfono es la llave: un cliente es de UNA oficina. Renombrarlo
            # desde otra cambiaría la ficha y los folios de esa oficina. Aquí sí
            # se dice que existe: el alta no puede fingir que lo creó.
            return RedirectResponse(
                "/clients?error=otra_oficina", status_code=303
            )
        if cliente is None:
            # Nace en la oficina de quien lo da de alta: con el default de la
            # columna (Tlaxcala) el alta de un colaborador de otra oficina le
            # desaparecía de su propia lista.
            session.add(Client(phone=canonico, name=nombre, office_id=vista.office_id))
            accion, resultado = "cliente:alta", "alta"
        elif cliente.name == nombre:
            accion, resultado = None, "sin-cambio"
        else:
            cliente.name = nombre
            session.add(cliente)
            accion, resultado = "cliente:nombre", "actualizado"
        # Un solo commit: el cliente y sus folios cambian de nombre juntos o no
        # cambian. La corrección se aplica también en el alta, por si el
        # teléfono ya tenía folios de antes.
        folios = _propagar_nombre(session, canonico, nombre)
        session.commit()
        if a_mi_cartera:
            # Después del commit: la cartera valida contra la fila Client ya guardada.
            cartera.agregar(session, canonico, user, por=user)

    # Auditado porque a este cliente se le van a cobrar tarjetas: quién le puso
    # ese nombre y cuándo es dato que hace falta el día de una aclaración.
    if accion:
        log_admin(user, accion, f"{canonico} · {nombre}")
        logger.info("Cliente %s (%s) por %s", canonico, resultado, user)
    if folios:
        logger.info("Nombre propagado a %d folio(s) de %s", folios, canonico)
    return RedirectResponse(f"/clients?alta={resultado}&q={canonico}", status_code=303)


@router.get("/clients/{phone}")
def client_profile(
    request: Request,
    phone: str,
    volver: str = "",
    error: str = "",
    aviso_cartera: str = Query("", alias="cartera"),
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    es_admin = is_admin(user)
    with get_session() as session:
        client = _cliente_visible(session, phone, user)
        if client is None:
            # return RedirectResponse("/inbox", status_code=303)  # Bandeja apagada
            return RedirectResponse("/", status_code=303)
        tickets = session.exec(
            select(Ticket).where(Ticket.phone == phone).order_by(Ticket.created_at.desc())
        ).all()
        notes = session.exec(
            select(ClientNote)
            .where(ClientNote.phone == phone)
            .where(ClientNote.habilitado == True)  # noqa: E712
            .order_by(ClientNote.created_at.desc())
        ).all()
        titulares = sorted(cartera.duenos(session, phone))
        # A quién puede meter un admin: gente habilitada de la oficina DEL CLIENTE
        # (la de quien mira puede ser otra, o todas para el admin global).
        candidatos = [] if not es_admin else [
            u for u in session.exec(
                select(User.username)
                .where(User.habilitado == True, User.office_id == client.office_id)  # noqa: E712
                .order_by(User.username)
            ).all()
            if u not in titulares
        ]

    return templates.TemplateResponse(
        request, "client.html",
        {
            "request": request,
            "user": user,
            "client": client,
            "tickets": tickets,
            "notes": notes,
            # Origen del salto a la ficha (folio o chat) para el botón de
            # regreso; validado, un valor externo se descarta.
            "volver": ruta_interna(volver),
            "titulares": titulares,
            "candidatos": candidatos,
            "error": _ERRORES_CARTERA.get(error, ""),
            "aviso": _AVISOS_CARTERA.get(aviso_cartera, ""),
        },
    )


# Los resultados de la cartera viajan por la URL como código, igual que _ERRORES.
_ERRORES_CARTERA = {
    cartera.AJENO: "Solo puedes agregarte o quitarte a ti de la cartera.",
    cartera.USUARIO: "Ese colaborador no está habilitado en la oficina del cliente.",
}
_AVISOS_CARTERA = {
    cartera.AGREGADO: "✅ Agregado a la cartera.",
    cartera.YA_ESTABA: "Ya estaba en la cartera.",
    cartera.QUITADO: "Quitado de la cartera.",
    cartera.NO_ESTABA: "No estaba en la cartera.",
}


def _tras_cartera(phone: str, resultado: str) -> RedirectResponse:
    if resultado == cartera.NO_EXISTE:
        # Mismo corte que el resto de la ficha: de otra oficina, «no existe».
        return RedirectResponse("/", status_code=303)
    if resultado in _ERRORES_CARTERA:
        return RedirectResponse(f"/clients/{phone}?error={resultado}", status_code=303)
    return RedirectResponse(f"/clients/{phone}?cartera={resultado}", status_code=303)


@router.post("/clients/{phone}/cartera")
def cartera_agregar(request: Request, phone: str, username: str = Form("")):
    """Agrega a la cartera: sin `username`, a uno mismo. Meter a otro es de admin, y la regla
    vive en app/cartera.py, no aquí: el formulario solo lo ofrece, no lo decide."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with get_session() as session:
        resultado = cartera.agregar(session, phone, username.strip() or user, por=user)
    return _tras_cartera(phone, resultado)


@router.post("/clients/{phone}/cartera/quitar")
def cartera_quitar(request: Request, phone: str, username: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    with get_session() as session:
        resultado = cartera.quitar(session, phone, username.strip() or user, por=user)
    return _tras_cartera(phone, resultado)


@router.post("/clients/{phone}/update")
def update_client(
    request: Request,
    phone: str,
    email: str = Form(""),
    origen: str = Form(""),
    idioma: str = Form(""),
    notas_internas: str = Form(""),
):
    """Ficha CRM (fase 1). El nombre se corrige en /clients (propaga a folios);
    aquí va el resto de la ficha."""
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        client = _cliente_visible(session, phone, user)
        if client is None:
            return RedirectResponse("/", status_code=303)
        nuevo_email = email.strip().lower()[:200]
        if not nuevo_email:
            client.email = None
        elif EMAIL_RE.fullmatch(nuevo_email):
            client.email = nuevo_email
        # email inválido: se conserva el anterior — la ficha no tiene canal de
        # error y guardar basura sería peor que ignorar el intento.
        origen_limpio = origen.strip()[:20]
        client.origen = origen_limpio if origen_limpio in _ORIGENES else None
        client.idioma = idioma.strip()[:10] or "es-MX"
        client.notas_internas = notas_internas.strip()[:2000] or None
        session.add(client)
        session.commit()
    # Mismo criterio que el alta: a este cliente se le cobra; quién editó su
    # ficha es dato de aclaración.
    log_admin(user, "cliente:ficha", phone)
    return RedirectResponse(f"/clients/{phone}", status_code=303)


@router.post("/clients/{phone}/consentimiento")
def set_consent(request: Request, phone: str, accion: str = Form(""), fuente: str = Form("")):
    """Opt-in / opt-out para mensajes iniciados por el negocio.

    Meta exige poder probar cuándo y cómo se dio el consentimiento; por eso el
    opt-in guarda fuente y fecha en vez de ser un simple booleano. Un opt-in
    nuevo limpia el opt-out anterior (consentimiento renovado); el opt-out
    nunca se limpia solo.
    """
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    if accion not in ("opt_in", "opt_out"):
        return RedirectResponse(f"/clients/{phone}", status_code=303)

    with get_session() as session:
        client = _cliente_visible(session, phone, user)
        if client is None:
            return RedirectResponse("/", status_code=303)
        if accion == "opt_in":
            client.opt_in_at = utcnow()
            client.opt_in_source = fuente.strip()[:120] or f"registrado por {user}"
            client.opt_out_at = None
        else:
            client.opt_out_at = utcnow()
        session.add(client)
        session.commit()
    log_admin(user, f"cliente:{accion}", phone)
    logger.info("Consentimiento %s de %s por %s", accion, mask_phone(phone), user)
    return RedirectResponse(f"/clients/{phone}", status_code=303)


@router.post("/clients/{phone}/notes")
def add_client_note(request: Request, phone: str, body: str = Form("")):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    cleaned = body.strip()
    with get_session() as session:
        if _cliente_visible(session, phone, user) is None:
            # return RedirectResponse("/inbox", status_code=303)  # Bandeja apagada
            return RedirectResponse("/", status_code=303)
        if cleaned:
            session.add(ClientNote(phone=phone, author=user, body=cleaned[:2000]))
            session.commit()
            logger.info("Nota de cliente agregada a %s por %s", mask_phone(phone), user)
    return RedirectResponse(f"/clients/{phone}", status_code=303)


@router.post("/clients/{phone}/notes/{note_id}/delete")
def delete_client_note(request: Request, phone: str, note_id: int):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    with get_session() as session:
        if _cliente_visible(session, phone, user) is None:
            return RedirectResponse("/", status_code=303)
        note = session.get(ClientNote, note_id)
        # Soft-delete: solo el autor o un admin pueden ocultar una nota.
        if note is not None and note.phone == phone and (note.author == user or is_admin(user)):
            note.habilitado = False
            session.add(note)
            session.commit()
    return RedirectResponse(f"/clients/{phone}", status_code=303)
