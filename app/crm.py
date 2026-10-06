"""CRM Kanban del ERP (/crm): embudo comercial por cliente, bandeja y chat en una página.

Fase A de la sustitución del chat de la bandeja. La página lee y escribe por JSON (etapas,
tablero, hilo del chat, mover de etapa, alta de contacto), pero ENVIAR, REACTIVAR EL BOT,
BLOQUEAR y OCULTAR MENSAJES siguen siendo los endpoints de app/inbox.py, llamados por fetch
desde static/js/crm.js: la regla «quien responde se queda el chat» (inbox._pause_bot), la
ventana de 24 h y el sellado de first_response_at tienen UN solo dueño. La fase B ya está
hecha: GET /inbox/{phone} solo reenvía aquí (con el ?err= que traigan esos POST) e
inbox_chat.html se retiró; el chat del panel es este.

Miembros: el tablero trae también a los usuarios habilitados del alcance (User.estado, la
misma disponibilidad de «Mi día», y cuántos chats del tablero atiende cada uno por el folio
vigente del cliente), agrupados por oficina para el admin global. El JS los pinta como un
filtro más: quién atiende qué, sin salir del tablero.

Alcance por oficina: cada consulta pasa por auth.alcance(); un teléfono que llega por URL se
comprueba contra Client.office_id (clients._cliente_visible: de otra oficina responde como «no
existe»). Y por folio: un ADMIN (global o de oficina) ve todas las conversaciones de su
alcance y a los miembros de la oficina; un COLABORADOR ve las conversaciones donde tiene un
folio abierto —la tarjeta muestra el SUYO: una conversación puede traer folios de dueños
distintos desde la asignación por servicio— y las de los clientes de su cartera, sin tira de
miembros y sin alta de contactos (reglas del operador, 2026-09-14 y 2026-09-24). Las conversaciones con
sus compañeros siguen en el chat de equipo (/team).

El tablero es PERSONAL (decisión del operador, 2026-09-29): cada usuario, admins incluidos, crea,
renombra, recolorea, reordena y da de baja SUS columnas y acomoda ahí a los clientes que ve, sin
tocar el tablero de nadie. Las reglas —siembra desde el embudo global, dónde cae cada cliente,
baja por cadena de destinos— viven en app/tablero.py; aquí solo se valida la petición y se
contesta. En la API las columnas se siguen llamando «etapas» y `etapa_id`, y una columna de otro
responde igual que una que no existe.

El cajón lateral del chat (static/js/crm-panel.js) unifica lo que antes obligaba a salir a
/tickets/{id}: gestión del folio, etiquetas, notas, cobro, seguimiento y transferencia. Lee de
GET /crm/api/panel/{phone} —aparte del hilo a propósito: el chat se sondea cada 8 s y no tiene
por qué cargar cobros ni compañeros— y ESCRIBE por los mismos POST de app/dashboard.py,
app/my.py, app/cobranza_panel.py y app/clients.py, llamados por fetch. Ni un endpoint de
escritura nuevo: quien decide sigue siendo el dueño de cada regla.

«Mis respuestas» —las respuestas guardadas personales que antes vivían en /replies— es un diálogo
de esta página: la barra superior y la fila de respuestas rápidas del compositor lo abren, y lee
y escribe por /crm/api/respuestas. Aquí SÍ hay endpoints de escritura propios, a diferencia del
cajón: la vista que era dueña de los POST se retiró, y el diálogo necesita el motivo exacto de
un rechazo (tope, campo vacío) que un 303 no trae. Las reglas siguen en app/replies.py.

La API contesta 403 JSON a un anónimo (como el chat de equipo, app/team_chat.py) en vez de
redirigir a /login: quien la llama es el JS de la página, no un navegador que pueda seguir la
redirección con sentido.
"""

from __future__ import annotations

import logging
import re
import unicodedata
import zlib
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import select
from sse_starlette.sse import EventSourceResponse

from app import adjuntos, cartera, eventos, tablero, ventana
from app.audit import log_admin
from app.auth import alcance, is_admin, is_admin_global
from app.authlog import sesion_vigente
from app.clients import MAX_NOMBRE, _cliente_visible, folio_vigente, folios_escribibles
from app.db import get_session
from app.inbox import QUICK_REPLIES
from app.models import (
    PROVIDER_META,
    Client,
    ClientNote,
    Conversation,
    CrmColumna,
    Direction,
    FollowUp,
    Label,
    Message,
    Office,
    Ticket,
    TicketLabel,
    TicketStatus,
    User,
)
from app.replies import (
    MAX_REPLIES,
    MAX_TEXTO,
    MAX_TITULO,
    RespuestasAlTope,
    borrar_respuesta,
    crear_respuesta,
    personal_replies,
)
from app.templating import local_dt, templates
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_NOMBRE_ETAPA = 40
# Cada cuánto el stream de avisos manda señal de vida si no hay nada que contar (§3.3 de
# docs/SPEC-EVENTOS-CRM.md): por debajo del minuto que cortan los proxys por omisión.
LATIDO_SEGUNDOS = 25
# Tope para escribir en un navegador que dejó de leer: pasado eso se le corta la conexión y
# EventSource reconecta por su cuenta. Sin tope, esa tarea se queda en el servidor para siempre.
ENVIO_MAX_SEGUNDOS = 15
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
COLOR_ETAPA_DEFAULT = "#25D366"
# ponytail: el tablero carga hasta este número de clientes (los más recientes de la oficina).
# La mitad de este marcador ya se cobró: sus mensajes se agregan EN SQL (_agregar), no en
# Python como todavía hace build_conversation_items en app/inbox.py. Falta paginar, cuando una
# oficina pase de unos cientos de clientes y el tope empiece a esconder conversaciones vivas.
LIMITE_CONTACTOS = 500
# ponytail: el hilo del chat trae los últimos N mensajes; sin «cargar más» todavía.
MAX_MENSAJES_CHAT = 300
# Colores de avatar deterministas por teléfono: el mismo cliente se ve igual en todas las
# sesiones y no hay que guardar nada.
AVATAR_PALETTE = (
    "#1E88E5", "#8E24AA", "#F4511E", "#00897B", "#3949AB", "#D81B60", "#6D4C41", "#0097A7",
)


# ---------------------------------------------------------------------------
# Sesión y respuestas
# ---------------------------------------------------------------------------

def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _error(status: int, mensaje: str) -> JSONResponse:
    return JSONResponse({"error": mensaje}, status_code=status)


def _sin_sesion() -> JSONResponse:
    return _error(403, "Sesión requerida")


def _iso(dt: datetime | None) -> str | None:
    """ISO 8601 en UTC con `Z` y sin microsegundos, el formato que consume el JS."""
    return None if dt is None else ventana.as_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Etapas (las columnas del tablero personal, app/tablero.py)
# ---------------------------------------------------------------------------

def _etapa_json(etapa: CrmColumna, count: int = 0) -> dict:
    return {
        "id": etapa.id,
        "nombre": etapa.nombre,
        "color": etapa.color,
        "ord": etapa.ord,
        "count": count,
    }


def _validar_nombre_etapa(valor) -> tuple[str | None, JSONResponse | None]:
    if not isinstance(valor, str) or not valor.strip():
        return None, _error(400, "El nombre de la etapa es obligatorio")
    nombre = " ".join(valor.split())
    if len(nombre) > MAX_NOMBRE_ETAPA:
        return None, _error(400, f"El nombre debe tener entre 1 y {MAX_NOMBRE_ETAPA} caracteres")
    # Controles (ESC, NUL…) y sustitutos UTF-16 sueltos: los primeros llegaban crudos al log y
    # al espejo, los segundos no se pueden guardar y eran un 500. Los saltos de línea ya los
    # quitó el split() de arriba; acentos, eñes y emoji pasan.
    if any(unicodedata.category(ch) in ("Cc", "Cs") for ch in nombre):
        return None, _error(400, "El nombre tiene caracteres no válidos")
    return nombre, None


def _validar_color(valor) -> tuple[str | None, JSONResponse | None]:
    if not isinstance(valor, str) or not HEX_RE.match(valor.strip()):
        return None, _error(400, "El color debe tener el formato #RRGGBB")
    return valor.strip().upper(), None


# ---------------------------------------------------------------------------
# Contactos: agregación por teléfono
# ---------------------------------------------------------------------------

def _normalizar(texto: str) -> str:
    """Sin acentos ni mayúsculas, para buscar «ramirez» y encontrar «Ramírez»."""
    descompuesto = unicodedata.normalize("NFKD", texto or "")
    return "".join(ch for ch in descompuesto if not unicodedata.combining(ch)).casefold()


def _iniciales(cliente: Client) -> str:
    palabras = (cliente.name or "").split()
    if len(palabras) >= 2:
        return (palabras[0][0] + palabras[1][0]).upper()
    if palabras:
        return palabras[0][:2].upper()
    return cliente.phone[-2:]


def _color_avatar(phone: str) -> str:
    return AVATAR_PALETTE[zlib.crc32(phone.encode("utf-8")) % len(AVATAR_PALETTE)]


def _ventana_json(last_in: datetime | None) -> dict:
    # Import diferido por la misma razón que en app/inbox.py: app.my importa app.inbox al
    # cargar. El umbral de urgencia es EL MISMO que marca «Mi día».
    from app.my import EXPIRING_HOURS

    abierta = ventana.abierta(last_in)
    horas = ventana.horas_restantes(last_in)
    return {
        "abierta": abierta,
        "texto": ventana.restante_texto(last_in),
        "urgente": abierta and horas <= EXPIRING_HOURS,
        "nunca": last_in is None,
    }


def _agregar(session, clientes: list[Client], viewer: str | None = None) -> dict[str, dict]:
    """Por teléfono: último mensaje, no leídos, último entrante, folio, etiquetas y conversación.

    «No leídos» no existe como columna (Message no lleva read_at): se deriva como los mensajes
    entrantes que llegaron DESPUÉS del último saliente, que es lo que el asesor todavía no ha
    contestado. El folio es el de `clients.folio_vigente`: con `viewer` (un colaborador), su
    folio abierto si lo tiene; si no —o para un admin, que pasa None—, el abierto más reciente
    o, sin abiertos, el más reciente.

    `apagado_por` y `bot_callado` miran TODOS los folios abiertos, no el de la tarjeta: el bot
    calla si cualquiera tiene el agente apagado (conversation.handle_incoming), y la cabecera
    tiene que decirlo aunque el folio apagado sea de otro.
    """
    agg = {
        c.phone: {
            "last": None, "unread": 0, "last_in": None,
            "ticket": None, "labels": [], "conv": None,
            "apagado_por": [], "bot_callado": False, "abiertos": [], "folios": [],
        }
        for c in clientes
    }
    telefonos = list(agg)
    if not telefonos:
        return agg

    # Los tres datos que salen de `message` se calculan EN SQL. Traerse el histórico de los
    # LIMITE_CONTACTOS teléfonos para derivarlos en Python costaba 961 ms y 100 000 filas
    # materializadas con un año de operación; el agregado sobre ix_message_phone cuesta ~48 ms
    # y trae 500 (medido, docs/SPEC-EVENTOS-CRM.md §0 y §6). Importa porque esto se sirve cada
    # 20 s a CADA panel abierto, así que era el consumidor de CPU dominante del proceso.
    #
    # El último mensaje se elige por `MAX(id)` y no por `created_at`: el ORDER BY que había
    # aquí ya desempataba por id, y el id es el único criterio que no depende de un reloj
    # (en la base real no hay un solo mensaje cuyo created_at rompa el orden de inserción).
    corte_saliente = func.coalesce(
        func.max(case((Message.direction == Direction.OUT, Message.id))), 0
    )
    cortes = (
        select(
            Message.phone.label("phone"),
            func.max(Message.id).label("ult_id"),
            corte_saliente.label("corte"),
            func.max(
                case((Message.direction == Direction.IN, Message.created_at))
            ).label("last_in"),
        )
        .where(Message.habilitado == True)  # noqa: E712
        .where(Message.phone.in_(telefonos))
        .group_by(Message.phone)
        .cte("cortes")
    )
    # Los no leídos son los entrantes posteriores al corte de CADA teléfono. El JOIN va contra
    # el CTE en vez de repetir el IN: con 500 contactos serían 1 000 parámetros enlazados y el
    # tope de variables de SQLite es 999 en las versiones que todavía trae alguna imagen vieja.
    nuevos = aliased(Message)
    filas = session.exec(
        select(cortes.c.phone, cortes.c.ult_id, cortes.c.last_in, func.count(nuevos.id))
        .select_from(cortes)
        .outerjoin(
            nuevos,
            and_(
                nuevos.phone == cortes.c.phone,
                nuevos.habilitado == True,  # noqa: E712
                nuevos.direction == Direction.IN,
                nuevos.id > cortes.c.corte,
            ),
        )
        .group_by(cortes.c.phone, cortes.c.ult_id, cortes.c.last_in)
    ).all()
    ultimos: dict[int, dict] = {}
    for phone, ult_id, last_in, no_leidos in filas:
        a = agg.get(phone)
        if a is None:
            continue
        a["unread"] = no_leidos
        a["last_in"] = last_in
        if ult_id is not None:
            ultimos[ult_id] = a
    if ultimos:
        # El cuerpo del último mensaje, por PK: una fila por contacto, no el histórico.
        for m in session.exec(select(Message).where(Message.id.in_(list(ultimos)))).all():
            ultimos[m.id]["last"] = m

    tickets = session.exec(
        select(Ticket)
        .where(Ticket.phone.in_(telefonos))
        .where(Ticket.habilitado == True)  # noqa: E712
        # `id` desempata igual que en clients.folio_vigente_de: con created_at
        # iguales, el orden físico de SQLite decidía la tarjeta.
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())  # type: ignore[union-attr]
    ).all()
    # Qué folio representa a cada conversación lo decide `clients.folio_vigente`,
    # la MISMA función que usa el guard de escritura de la bandeja: si la regla
    # viviera aquí y allá, la tarjeta y el permiso volverían a divergir (AUTHZ-2).
    por_telefono: dict[str, list[Ticket]] = {}
    for t in tickets:
        if t.phone in agg:
            por_telefono.setdefault(t.phone, []).append(t)
    for phone, folios in por_telefono.items():
        agg[phone]["ticket"] = folio_vigente(folios, viewer)
        apagados = [t for t in folios if t.status != TicketStatus.CERRADO and not t.agent_active]
        agg[phone]["bot_callado"] = bool(apagados)
        agg[phone]["apagado_por"] = sorted({t.assigned_to for t in apagados if t.assigned_to})
        # Todos los trámites en curso, del más viejo al más nuevo (el orden en que el cliente
        # los pidió): la cabecera del chat los lista todos, no solo el de la tarjeta.
        agg[phone]["abiertos"] = [
            t for t in reversed(folios) if t.status != TicketStatus.CERRADO
        ]
        agg[phone]["folios"] = folios

    ids_folios = [a["ticket"].id for a in agg.values() if a["ticket"] is not None]
    if ids_folios:
        filas = session.exec(
            select(TicketLabel.ticket_id, Label.nombre)
            .join(Label, Label.id == TicketLabel.label_id)
            .where(TicketLabel.ticket_id.in_(ids_folios))
            .where(TicketLabel.habilitado == True)  # noqa: E712
            .where(Label.habilitado == True)  # noqa: E712
            .order_by(Label.nombre)
        ).all()
        por_folio: dict[int, list[str]] = {}
        for ticket_id, nombre in filas:
            por_folio.setdefault(ticket_id, []).append(nombre)
        for a in agg.values():
            if a["ticket"] is not None:
                a["labels"] = por_folio.get(a["ticket"].id, [])

    for conv in session.exec(
        select(Conversation).where(Conversation.phone.in_(telefonos))
    ).all():
        if conv.phone in agg:
            agg[conv.phone]["conv"] = conv
    return agg


def _contacto_json(cliente: Client, a: dict, etapa_id: int | None) -> dict:
    last = a["last"]
    ticket = a["ticket"]
    conv = a["conv"]
    return {
        "phone": cliente.phone,
        "name": cliente.name,
        "alias": cliente.alias,
        "initials": _iniciales(cliente),
        "avatar_color": _color_avatar(cliente.phone),
        "etapa_id": etapa_id,
        "unread": a["unread"],
        "pinned": False,
        "labels": a["labels"],
        "bloqueado": not cliente.habilitado,
        "last_message": None if last is None else {
            "direction": last.direction.value,
            "body": last.body,
            "media_path": last.media_path,
            "media_mime": last.media_mime,
            "wa_status": last.wa_status,
            "created_at": _iso(last.created_at),
            "sent_by": last.sent_by,
        },
        "last_seen": _iso(a["last_in"]),
        "created_at": _iso(cliente.created_at),
        "ticket": None if ticket is None else {
            "id": ticket.id,
            "status": ticket.status.value,
            "assigned_to": ticket.assigned_to,
            "service_name": ticket.service_name,
            "escalado": bool(ticket.escalado),
            "agent_active": bool(ticket.agent_active),
        },
        "ventana": _ventana_json(a["last_in"]),
        # Folio vigente cerrado: fuera del panel salvo en «Folios cerrados» o en la búsqueda.
        "cerrada": ticket is not None and ticket.status == TicketStatus.CERRADO,
        # Pausado también si algún folio abierto tiene el agente apagado: el bot calla por
        # eso aunque la conversación ya no esté escalada, y «bot activo» sería mentira.
        "bot_pausado": bool(conv and conv.state == "escalado") or a["bot_callado"],
        # Quién tiene el agente apagado en su folio (con varios dueños puede no ser quien mira).
        "apagado_por": a["apagado_por"],
        "provider": conv.provider if conv else PROVIDER_META,
    }


def _adjunto_json(m: Message) -> dict | None:
    """Lo que el CRM necesita para pintar el archivo; el archivo lo trae el visor."""
    if not m.media_ref:
        return None
    return {
        "url": f"/crm/api/adjunto/{m.id}",
        "mime": m.media_mime,
        "nombre": m.media_nombre,
        "bytes": m.media_bytes,
        "vence_at": _iso(m.media_vence_at) if m.media_vence_at else None,
        "vencido": adjuntos.vencido(m),
    }


def _mensaje_json(m: Message, user: str, folios: dict[int, Ticket] | None = None) -> dict:
    # El folio sobre el que contestó un colaborador, para la burbuja «ana · #46 · Acta de
    # nacimiento». `folios` trae los del teléfono ya cargados; un ticket_id que no esté ahí
    # (folio deshabilitado) se pinta sin folio, como los mensajes anteriores a la columna.
    folio = (folios or {}).get(m.ticket_id) if m.ticket_id is not None else None
    return {
        "id": m.id,
        "direction": m.direction.value,
        "body": m.body,
        "media_path": m.media_path,
        "adjunto": _adjunto_json(m),
        "wa_status": m.wa_status,
        "created_at": _iso(m.created_at),
        "sent_by": m.sent_by,
        "mio": m.direction == Direction.OUT and m.sent_by == user,
        "folio": None if folio is None else {"id": folio.id, "service_name": folio.service_name},
    }


def _folios_abiertos_json(abiertos: list[Ticket], escribibles: list[Ticket]) -> list[dict]:
    """Los trámites en curso de la conversación, para la cabecera del chat y el selector del
    compositor. `puedo_escribir`: si quien mira puede contestar sobre ese folio
    (`clients.folios_escribibles`, la misma lista que autoriza el envío)."""
    ids = {t.id for t in escribibles}
    return [
        {
            "id": t.id,
            "service_name": t.service_name,
            "status": t.status.value,
            "assigned_to": t.assigned_to,
            "puedo_escribir": t.id in ids,
        }
        for t in abiertos
    ]


def _coincide(cliente: Client, q: str) -> bool:
    aguja = _normalizar(q)
    if (aguja in _normalizar(cliente.name or "") or aguja in _normalizar(cliente.alias or "")
            or aguja in cliente.phone):
        return True
    digitos = re.sub(r"\D", "", q)
    return bool(digitos) and digitos in cliente.phone


def _atiende(a: dict, user: str) -> bool:
    """¿El folio con el que este usuario mira la conversación (el de la tarjeta) es suyo?"""
    return a["ticket"] is not None and a["ticket"].assigned_to == user


def _conversacion_visible(session, phone: str, user: str):
    """(cliente, agregado) si este usuario puede ver esa conversación; (None, None) si no.

    Dos cortes, y los dos responden «no existe» para no revelar que el número está en la
    base: el de oficina (clients._cliente_visible) y, para quien no es admin, el del folio:
    un colaborador ve las conversaciones donde tiene un folio (su folio es el de la tarjeta)
    o las de los clientes de su CARTERA (decisión del operador, 2026-09-24). Ver no es
    escribir: eso lo decide clients.conversacion_escribible, que no mira la cartera."""
    cliente = _cliente_visible(session, phone, user)
    if cliente is None:
        return None, None
    admin = is_admin(user)
    agg = _agregar(session, [cliente], None if admin else user)
    if (
        not admin
        and not _atiende(agg[cliente.phone], user)
        and not cartera.es_mio(session, cliente.phone, user)
    ):
        return None, None
    return cliente, agg


def _tablero(session, vista, user: str, q: str = "", cerradas: bool = False) -> dict:
    # is_admin ANTES de cargar el tablero: abre su propia sesión, y la primera carga siembra.
    solo_mios = not is_admin(user)
    mio = tablero.cargar(session, user)
    ids = mio.ids

    consulta = vista.filtrar(select(Client), Client.office_id)
    if solo_mios:
        # Un colaborador ve las conversaciones de las solicitudes que toma —clientes con
        # algún folio suyo— y las de su cartera. La decisión final la toma su folio
        # (abajo); acotar ya en SQL evita que sus clientes viejos queden fuera del tope de
        # LIMITE_CONTACTOS.
        consulta = consulta.where(
            or_(
                Client.phone.in_(
                    select(Ticket.phone)
                    .where(Ticket.assigned_to == user)
                    .where(Ticket.habilitado == True)  # noqa: E712
                ),
                Client.phone.in_(cartera.telefonos_de(user)),
            )
        )
    # Una conversación sale del panel cuando TODOS sus folios están cerrados —su folio
    # vigente es uno cerrado— y vuelve sola cuando se abre otro (regla del operador,
    # 2026-09-16: vale para todos, admins incluidos). Los clientes SIN folio se quedan:
    # son leads y altas manuales, y ocultarlos haría desaparecer un contacto recién dado
    # de alta. Va en SQL, antes del tope de LIMITE_CONTACTOS, para que el histórico
    # cerrado no empuje fuera a las conversaciones abiertas. Ocultar no es prohibir:
    # /crm/api/chat/{phone} no cambia, y los enlaces «Abrir chat» siguen abriendo.
    folios = (
        select(Ticket.id)
        .where(Ticket.phone == Client.phone)
        .where(Ticket.habilitado == True)  # noqa: E712
    )
    tiene_folio = folios.exists()
    tiene_abierto = folios.where(Ticket.status != TicketStatus.CERRADO).exists()
    if cerradas:
        consulta = consulta.where(tiene_folio, ~tiene_abierto)
    elif not q:
        # La búsqueda sí alcanza a las cerradas: es como se encuentra una por nombre.
        consulta = consulta.where(or_(~tiene_folio, tiene_abierto))
    digitos = re.sub(r"\D", "", q)
    if digitos:
        # Con dígitos se busca teléfono en SQL, antes del tope de LIMITE_CONTACTOS: un cliente
        # viejo se encuentra por número aunque no esté entre los recientes. El nombre se
        # filtra abajo, sin acentos, sobre lo cargado.
        canonico = normalize_phone(q)
        condiciones = [Client.phone.like(f"%{digitos}%")]
        if canonico:
            condiciones.append(Client.phone == canonico)
        consulta = consulta.where(or_(*condiciones))
    clientes = session.exec(
        consulta.order_by(Client.created_at.desc()).limit(LIMITE_CONTACTOS)  # type: ignore[union-attr]
    ).all()
    if q and not digitos:
        clientes = [c for c in clientes if _coincide(c, q)]

    agg = _agregar(session, clientes, user if solo_mios else None)
    if solo_mios:
        mi_cartera = cartera.clientes_de(session, user)
        clientes = [c for c in clientes if _atiende(agg[c.phone], user) or c.phone in mi_cartera]
    contactos = [_contacto_json(c, agg[c.phone], mio.columna_de(c)) for c in clientes]
    contactos.sort(
        key=lambda x: (x["last_message"] or {}).get("created_at") or x["created_at"] or "",
        reverse=True,
    )
    counts = {str(i): 0 for i in ids}
    for x in contactos:
        if x["etapa_id"] is not None:
            counts[str(x["etapa_id"])] += 1
    # Solo los admins ven a los miembros de la oficina (y sus conteos).
    miembros, oficinas = ([], []) if solo_mios else _miembros(session, vista, contactos)
    return {
        "etapas": [_etapa_json(e, counts[str(e.id)]) for e in mio.activas],
        "contactos": contactos,
        "counts": counts,
        "total": sum(counts.values()),
        "miembros": miembros,
        "oficinas": oficinas,
    }


# ---------------------------------------------------------------------------
# Miembros de la oficina
# ---------------------------------------------------------------------------

def _miembros(session, vista, contactos: list[dict]) -> tuple[list[dict], list[dict]]:
    """Los usuarios habilitados del alcance y las oficinas del alcance.

    De cada miembro: su disponibilidad (`User.estado`, la que fija «Mi día») y cuántos
    chats DEL TABLERO atiende, contando los contactos cuyo folio vigente le está asignado;
    el conteo sigue a la búsqueda porque describe lo que se ve. El admin global recibe a
    los de todas las oficinas, ordenados por oficina, y la lista de oficinas para agrupar;
    un admin de oficina o un colaborador, solo a los de la suya.
    """
    atiende: dict[str, int] = {}
    for c in contactos:
        asesor = (c.get("ticket") or {}).get("assigned_to")
        if asesor:
            atiende[asesor] = atiende.get(asesor, 0) + 1
    oficinas = {
        o.id: o.nombre
        for o in session.exec(
            vista.filtrar(select(Office).where(Office.habilitado == True), Office.id)  # noqa: E712
            .order_by(Office.id)
        ).all()
    }
    usuarios = session.exec(
        vista.filtrar(select(User).where(User.habilitado == True), User.office_id)  # noqa: E712
        .order_by(User.office_id, User.username)
    ).all()
    miembros = [
        {
            "username": u.username,
            "initials": u.username[:2].upper(),
            "avatar_color": _color_avatar(u.username),
            "office_id": u.office_id,
            "oficina": oficinas.get(u.office_id, f"Oficina {u.office_id}"),
            "admin": bool(u.is_admin),
            "estado": u.estado or "disponible",
            "atiende": atiende.get(u.username, 0),
        }
        for u in usuarios
    ]
    return miembros, [{"id": i, "nombre": n} for i, n in oficinas.items()]


def _respuestas_rapidas(session, user: str) -> list[dict]:
    """Las globales (app/quick_replies.json) y las personales del usuario, en ese orden."""
    globales = [{"title": q["title"], "text": q["text"]} for q in QUICK_REPLIES]
    mias = [{"title": f"★ {r.title}", "text": r.body} for r in personal_replies(session, user)]
    return globales + mias


def _mis_respuestas_json(session, user: str) -> dict:
    """Lo que pinta el diálogo «Mis respuestas» y, de paso, la fila del compositor: así una
    alta o una baja repinta las dos con una sola respuesta del servidor."""
    return {
        "mias": [{"id": r.id, "title": r.title, "text": r.body} for r in personal_replies(session, user)],
        "max": MAX_REPLIES,
        "respuestas_rapidas": _respuestas_rapidas(session, user),
    }


def _datos_bootstrap(session, user: str, telefono: str | None, abrir_respuestas: bool = False) -> dict:
    # Import diferido: app.my importa app.inbox al cargar (ver _ventana_json).
    from app.my import EXPIRING_HOURS

    return {
        "usuario": user,
        "admin": is_admin(user),
        "admin_global": is_admin_global(user),
        "chat_inicial": telefono,
        "ventana_horas": ventana.WINDOW_HOURS,
        "urgente_horas": EXPIRING_HOURS,
        "respuestas_rapidas": _respuestas_rapidas(session, user),
        "abrir_respuestas": abrir_respuestas,
        # Los largos de app/replies.py: la plantilla los pone de maxlength en el diálogo.
        "respuestas_limites": {"max": MAX_REPLIES, "titulo": MAX_TITULO, "texto": MAX_TEXTO},
    }


# ---------------------------------------------------------------------------
# Página
# ---------------------------------------------------------------------------

@router.get("/crm")
def crm_page(request: Request, chat: str = "", respuestas: str = ""):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    # `chat` abre esa conversación al cargar. Solo dígitos: el JS lo mete en una URL de la
    # API y el alcance lo comprueba el endpoint del chat, no esta página.
    telefono = chat if re.fullmatch(r"\d{8,15}", chat or "") else None
    # `respuestas=1` abre «Mis respuestas» al cargar: es a donde reenvía la ruta vieja /replies.
    with get_session() as session:
        datos = _datos_bootstrap(session, user, telefono, abrir_respuestas=respuestas == "1")
    return templates.TemplateResponse(
        request, "crm.html", {"request": request, "user": user, "datos": datos}
    )


# ---------------------------------------------------------------------------
# API: tablero y etapas
# ---------------------------------------------------------------------------

@router.get("/crm/api/tablero")
def crm_tablero(request: Request, q: str = "", cerradas: bool = False):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    vista = alcance(user)
    with get_session() as session:
        return JSONResponse(_tablero(session, vista, user, q.strip(), cerradas))


@router.get("/crm/api/etapas")
def crm_etapas(request: Request):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    vista = alcance(user)
    with get_session() as session:
        return JSONResponse(_tablero(session, vista, user)["etapas"])


# Son de quien las tiene, admins incluidos (como «Mis respuestas»): no hay alcance por oficina
# que aplicar ni un admin que las administre por otro. Cada endpoint trabaja sobre el tablero
# del usuario de la sesión, y un id de otro tablero contesta como uno que no existe. Sin
# AdminAudit, igual que las respuestas guardadas: acomodar el tablero propio no es un acto de
# administración, y las filas —con su baja lógica— ya son el registro.

@router.post("/crm/api/etapas")
def crm_etapa_crear(request: Request, payload: dict = Body(default={})):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    nombre, error = _validar_nombre_etapa(payload.get("nombre"))
    if error:
        return error
    color, error = _validar_color(payload.get("color", COLOR_ETAPA_DEFAULT))
    if error:
        return error

    with get_session() as session:
        try:
            etapa = tablero.crear(session, user, nombre, color)
        except (tablero.ColumnasAlTope, tablero.NombreRepetido) as e:
            return _error(409, str(e))
        # Nace vacía: ninguna ubicación ni cadena de bajas puede apuntar a una columna nueva.
        return JSONResponse(_etapa_json(etapa, 0), status_code=201)


@router.put("/crm/api/etapas")
def crm_etapas_ordenar(request: Request, payload: dict = Body(default={})):
    """Reordena el tablero: `{"orden": [ids de izquierda a derecha]}`, cada columna propia una
    vez. Es PUT sobre la colección y no una ruta hermana de /etapas/{etapa_id}, que se
    tragaría «orden» como un id y contestaría 422. Reordenar no mueve a ningún cliente."""
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    vista = alcance(user)
    with get_session() as session:
        try:
            tablero.reordenar(session, user, payload.get("orden"))
        except ValueError as e:
            return _error(400, str(e))
        return JSONResponse(_tablero(session, vista, user)["etapas"])


@router.put("/crm/api/etapas/{etapa_id}")
def crm_etapa_editar(request: Request, etapa_id: int, payload: dict = Body(default={})):
    user = _current_user(request)
    if not user:
        return _sin_sesion()

    # Sin `ord` (revisión de seguridad, 2026-09-29): era un entero libre que ahora puede mandar
    # cualquier colaborador, y en el espejo es un INT. Una columna con ord > 2^31, dada de baja
    # y ya imposible de editar, cortaba cada backfill de MSSQL antes de reparar mensajes y chat
    # de equipo. El orden lo cambia PUT /crm/api/etapas, que numera 0..n-1.
    if "ord" in payload:
        return _error(400, "El orden se cambia reordenando el tablero, no etapa por etapa")
    cambios: dict = {}
    if "nombre" in payload:
        nombre, error = _validar_nombre_etapa(payload.get("nombre"))
        if error:
            return error
        cambios["nombre"] = nombre
    if "color" in payload:
        color, error = _validar_color(payload.get("color"))
        if error:
            return error
        cambios["color"] = color
    if not cambios:
        return _error(400, "Debes enviar al menos un campo: nombre o color")

    vista = alcance(user)
    with get_session() as session:
        etapa = tablero.columna_propia(session, user, etapa_id)
        if etapa is None:
            return _error(404, "La etapa no existe")
        try:
            etapa = tablero.editar(session, user, etapa, cambios)
        except tablero.NombreRepetido as e:
            return _error(409, str(e))
        counts = _tablero(session, vista, user)["counts"]
        return JSONResponse(_etapa_json(etapa, counts.get(str(etapa.id), 0)))


@router.delete("/crm/api/etapas/{etapa_id}")
def crm_etapa_borrar(request: Request, etapa_id: int):
    user = _current_user(request)
    if not user:
        return _sin_sesion()

    vista = alcance(user)
    with get_session() as session:
        etapa = tablero.columna_propia(session, user, etapa_id)
        if etapa is None:
            return _error(404, "La etapa no existe")
        # Cuántos se van, contados en el tablero abierto (sin búsqueda): el número que anuncia el
        # «¿Eliminar…?». Es informativo y se queda corto a propósito: no cuenta conversaciones
        # con todos sus folios cerrados ni las que deja fuera LIMITE_CONTACTOS, que también se
        # van al destino. Contarlas exactas costaría rehacer el alcance sin tope, para un número
        # que el navegador no usa.
        movidos = _tablero(session, vista, user)["counts"].get(str(etapa_id), 0)
        nombre = etapa.nombre
        try:
            destino = tablero.dar_de_baja(session, user, etapa)
        except ValueError as e:
            return _error(400, str(e))
        logger.info(
            "Etapa «%s» de %s dada de baja: su tablero abierto mostraba ahí %s clientes; van a «%s»",
            nombre, user, movidos, destino.nombre,
        )
        return JSONResponse({"deleted": etapa_id, "moved": movidos, "target_id": destino.id})


# ---------------------------------------------------------------------------
# API: contactos y chat
# ---------------------------------------------------------------------------

@router.put("/crm/api/contactos/{phone}/etapa")
def crm_mover_contacto(request: Request, phone: str, payload: dict = Body(default={})):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    etapa_id = payload.get("etapa_id")
    if not isinstance(etapa_id, int) or isinstance(etapa_id, bool):
        return _error(400, "La etapa debe ser un entero")

    with get_session() as session:
        cliente, _ = _conversacion_visible(session, phone, user)
        if cliente is None:
            return _error(404, "No existe")
        mio = tablero.cargar(session, user)
        # Solo una columna viva de SU tablero: la de otro contesta igual que una inventada.
        if etapa_id not in mio.ids:
            return _error(400, "La etapa no existe")
        if mio.columna_de(cliente) != etapa_id:
            # Solo en el tablero de quien lo mueve: ni Client.etapa_crm_id ni el de nadie más.
            tablero.mover(session, user, cliente.phone, etapa_id)
            logger.info("Cliente %s movido a la etapa %s por %s", mask_phone(phone), etapa_id, user)
        # La tarjeta que devuelve es la que ve quien la movió: su folio, no el de otro.
        agg = _agregar(session, [cliente], None if is_admin(user) else user)
        return JSONResponse(_contacto_json(cliente, agg[cliente.phone], etapa_id))


@router.post("/crm/api/contactos")
def crm_alta_contacto(request: Request, payload: dict = Body(default={})):
    """Alta manual desde una columna del tablero. Mismas reglas que POST /clients: teléfono
    canónico, nombre obligatorio, nace en la oficina de quien lo da de alta. A diferencia del
    directorio, un teléfono que ya existe NO se renombra: es un 409, la columna ya lo tiene."""
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    if not is_admin(user):
        # Un colaborador solo ve lo que atiende: un contacto sin folio nacería invisible
        # para él. Dar de alta es tarea del admin (o del bot, cuando el cliente escribe).
        return _error(403, "Solo un admin da de alta contactos desde el tablero")
    canonico = normalize_phone(str(payload.get("phone") or ""))
    if not canonico:
        return _error(400, "Teléfono inválido: usa 10 dígitos, o 52 + 10 dígitos.")
    nombre = str(payload.get("name") or "").strip()[:MAX_NOMBRE]
    if not nombre:
        return _error(400, "El nombre es obligatorio.")
    etapa_id = payload.get("etapa_id")
    if etapa_id is not None and (not isinstance(etapa_id, int) or isinstance(etapa_id, bool)):
        return _error(400, "La etapa debe ser un entero")

    vista = alcance(user)
    with get_session() as session:
        existente = session.get(Client, canonico)
        if existente is not None:
            if not vista.alcanza(existente.office_id):
                # Igual que POST /clients: el alta no puede fingir que lo creó.
                return _error(409, "Ese teléfono ya es cliente de otra oficina.")
            return _error(409, "Ya existe un cliente con ese teléfono.")
        if etapa_id is not None and etapa_id not in tablero.cargar(session, user).ids:
            return _error(400, "La etapa no existe")
        # Sin etapa_crm_*: el embudo global ya no se escribe. La columna donde se dio de alta
        # es de quien lo dio de alta; los demás lo ven en la copia de su primera etapa. Cliente
        # y columna van en el MISMO commit: si algo falla no queda un contacto sin su columna,
        # ni un reintento que conteste «ya existe».
        cliente = Client(phone=canonico, name=nombre, office_id=vista.office_id, origen="manual")
        session.add(cliente)
        if etapa_id is not None:
            tablero.ubicar(session, user, canonico, etapa_id)
        session.commit()
        session.refresh(cliente)
        log_admin(user, "cliente:alta", canonico)
        agg = _agregar(session, [cliente])
        return JSONResponse(
            _contacto_json(cliente, agg[cliente.phone], tablero.cargar(session, user).columna_de(cliente)),
            status_code=201,
        )


# Qué contesta el visor cuando no puede servir el archivo. Un código por caso
# para que el CRM diga lo que pasó en vez de un «error» genérico.
_ADJUNTO_ERRORES = {
    "sin-archivo": (404, "Este mensaje no tiene un archivo que abrir"),
    "vencido": (410, "El archivo ya no está en WhatsApp: pídele al cliente que lo reenvíe"),
    "no-disponible": (502, "WhatsApp no entregó el archivo; intenta de nuevo en un momento"),
    "tipo": (415, "El archivo no es del tipo que dice ser; no se abre"),
}
# Se ven en el navegador; lo demás (Word, Excel) se descarga.
_ADJUNTO_EN_LINEA = {"application/pdf", "image/jpeg", "image/png"}


@router.get("/crm/api/adjunto/{message_id}")
def crm_adjunto(request: Request, message_id: int):
    """El archivo de un mensaje, traído del proveedor (app/adjuntos.py).

    Mismo permiso que el chat (`_conversacion_visible`): quien no ve la
    conversación recibe el mismo 404 que si el mensaje no existiera. El VPS no
    guarda el archivo: lo pide a Meta o a Twilio en cada apertura, y lo pide
    FUERA de la sesión de SQLite para no retener la base durante la descarga.
    """
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    with get_session() as session:
        m = session.get(Message, message_id)
        if m is None or not m.habilitado:
            return _error(404, "No existe")
        cliente, _ = _conversacion_visible(session, m.phone, user)
        if cliente is None:
            return _error(404, "No existe")
        session.expunge(m)
    resultado = adjuntos.traer(m)
    if isinstance(resultado, str):
        status, texto = _ADJUNTO_ERRORES[resultado]
        return _error(status, texto)
    data, mime = resultado
    nombre = m.media_nombre or f"archivo-{m.id}"
    disposicion = "inline" if mime in _ADJUNTO_EN_LINEA else "attachment"
    return Response(
        content=data,
        media_type=mime,
        headers={
            # filename* (RFC 5987): el nombre lo puso el cliente y puede traer
            # acentos o comillas; así no rompe la cabecera.
            "Content-Disposition": f"{disposicion}; filename*=UTF-8''{quote(nombre)}",
            "X-Content-Type-Options": "nosniff",
            # Privado (nunca en un caché compartido) y de una hora: el chat se
            # repinta cada 8 s y cada imagen visible volvería a pedirse al
            # proveedor. Quien ya lo vio lo tiene igual en su navegador.
            "Cache-Control": "private, max-age=3600",
        },
    )


@router.get("/crm/api/chat/{phone}")
def crm_chat(request: Request, phone: str):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    with get_session() as session:
        cliente, agg = _conversacion_visible(session, phone, user)
        if cliente is None:
            return _error(404, "No existe")
        mensajes = session.exec(
            select(Message)
            .where(Message.phone == phone)
            .where(Message.habilitado == True)  # noqa: E712
            .order_by(Message.created_at.desc(), Message.id.desc())  # type: ignore[union-attr]
            .limit(MAX_MENSAJES_CHAT)
        ).all()
        mensajes.reverse()
        a = agg[cliente.phone]
        contacto = _contacto_json(cliente, a, tablero.cargar(session, user).columna_de(cliente))
        # Los trámites en curso van solo en el chat, no en la tarjeta: `puedo_escribir` cuesta
        # una consulta por folio provisional ajeno (asignacion._por_producto), y el tablero se
        # sirve cada 20 s con hasta LIMITE_CONTACTOS contactos.
        contacto["folios_abiertos"] = _folios_abiertos_json(
            a["abiertos"], folios_escribibles(session, cliente.phone, user)
        )
        por_id = {t.id: t for t in a["folios"]}
        return JSONResponse({
            "contacto": contacto,
            "mensajes": [_mensaje_json(m, user, por_id) for m in mensajes],
            "ventana": contacto["ventana"],
            "bot_pausado": contacto["bot_pausado"],
            # Si el bot habla de verdad: ningún folio abierto con el agente apagado. Antes era
            # el agente del folio de la tarjeta, y con varios dueños el bot podía estar callado
            # por el folio de otro mientras la cabecera decía «el agente sigue activo».
            "agente_activo": not agg[cliente.phone]["bot_callado"],
            "provider": contacto["provider"],
            "ticket": contacto["ticket"],
            "bloqueado": contacto["bloqueado"],
            "respuestas_rapidas": _respuestas_rapidas(session, user),
        })


# ---------------------------------------------------------------------------
# API: avisos en vivo (SSE)
# ---------------------------------------------------------------------------

@router.get("/crm/api/eventos")
async def crm_eventos(request: Request):
    """Stream que avisa de mensajes nuevos para que el panel no dependa solo del reloj.

    `async def` es OBLIGATORIO. Los demás endpoints del CRM son `def` y FastAPI los corre en el
    threadpool; un stream síncrono se quedaría con un hilo de ese pool PARA SIEMPRE, y con diez
    pestañas abiertas se come el que el webhook necesita para atender a Meta. No se ve en
    desarrollo y en producción aparece como «el bot dejó de contestar».

    El aviso es mudo a propósito (app/eventos.py): quién ve qué lo siguen decidiendo los
    endpoints que el navegador vuelve a pedir, no este stream.

    SSE y no WebSocket: el tráfico es de ida, las escrituras ya tienen sus POST con su sesión y
    su alcance, y un socket obligaría a inventar un protocolo y a re-autorizar cada acción
    dentro de él. Esto es un GET normal que pasa por SessionMiddleware, por la CSP
    (`default-src 'self'` ya cubre EventSource) y por las cabeceras de seguridad sin tocar nada.
    """
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    sid = request.session.get("sid")
    cola = eventos.suscribir()

    async def flujo():
        try:
            while True:
                aviso = await cola.get()
                # Una ráfaga de mensajes es UN aviso; y si el cierre viene en medio de la
                # ráfaga, gana él — tirarlo al drenar volvería a colgar el apagado.
                while aviso is not eventos.CIERRE and not cola.empty():
                    aviso = cola.get_nowait()
                if aviso is eventos.CIERRE:
                    # El proceso se apaga: terminar el generador cierra la conexión y deja que
                    # uvicorn pare en el acto, en vez de esperar a un stream que no acaba nunca.
                    return
                # El guardia de sesión (authlog) corre una vez por request, y este request no
                # termina nunca: a quien dieron de baja con la pestaña abierta le seguían
                # llegando avisos (refutación de AUTH-1, 2026-09-21). Se re-verifica antes de
                # cada aviso —un SELECT por índice único, y solo cuando hay algo que contar—
                # y si ya no está vigente se cierra el stream; el EventSource del navegador
                # reintenta solo y ese GET nuevo ya rebota en el guardia. Si la base no
                # contesta, también se cierra: falla cerrado, igual que el guardia.
                try:
                    vigente = sesion_vigente(user, sid)
                except Exception:  # noqa: BLE001 — cerrar el stream ES el manejo
                    vigente = False
                if not vigente:
                    return
                yield {"event": "mensajes", "data": "1"}
        finally:
            eventos.desuscribir(cola)

    # EventSourceResponse en vez de un StreamingResponse a mano: pone `Cache-Control: no-store`
    # y `X-Accel-Buffering: no` (sin lo segundo, nginx retiene los eventos hasta llenar su
    # búfer), manda el latido solo, detecta que el cliente se fue y —lo que de verdad importa—
    # CIERRA el stream cuando el proceso recibe la señal de apagado. Medido en este repo: sin eso
    # el contenedor con un panel abierto no paraba con SIGTERM y moría por SIGKILL a los 10 s.
    return EventSourceResponse(
        flujo(),
        ping=LATIDO_SEGUNDOS,
        # Un navegador que deja de leer no puede colgar su tarea del servidor para siempre.
        send_timeout=ENVIO_MAX_SEGUNDOS,
    )


# ---------------------------------------------------------------------------
# API: mis respuestas (el diálogo que sustituye a /replies)
# ---------------------------------------------------------------------------

# Son de quien las guarda y de nadie más, admins incluidos: no hay alcance por oficina que
# aplicar, cada endpoint filtra por el usuario de la sesión.

@router.get("/crm/api/respuestas")
def crm_respuestas(request: Request):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    with get_session() as session:
        return JSONResponse(_mis_respuestas_json(session, user))


@router.post("/crm/api/respuestas")
def crm_respuesta_crear(request: Request, payload: dict = Body(default={})):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    with get_session() as session:
        try:
            respuesta = crear_respuesta(session, user, payload.get("title"), payload.get("text"))
        except RespuestasAlTope as e:
            return _error(409, str(e))
        except ValueError as e:
            return _error(400, str(e))
        datos = _mis_respuestas_json(session, user)
        datos["creada"] = respuesta.id
        return JSONResponse(datos, status_code=201)


@router.delete("/crm/api/respuestas/{reply_id}")
def crm_respuesta_borrar(request: Request, reply_id: int):
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    with get_session() as session:
        if not borrar_respuesta(session, user, reply_id):
            return _error(404, "Esa respuesta ya no existe")
        return JSONResponse(_mis_respuestas_json(session, user))


# ---------------------------------------------------------------------------
# API: el panel del folio (el cajón lateral del chat)
# ---------------------------------------------------------------------------

# Cuántos cobros y notas trae el cajón; el resto se ve en la solicitud completa.
MAX_COBROS_PANEL = 8
MAX_NOTAS_CLIENTE = 20

# Espejo del bloque de estado de templates/_cobro.html. Vive aquí porque el
# cajón lo pinta con JS y el JSON tiene que llegar ya traducido; que los textos
# sigan siendo los mismos lo amarra tests/test_crm.py.
BADGE_COBRO = {
    "pagado": ("cerrado", "✓ Pagado"),
    "pendiente": ("tomado", "⏳ Pendiente de pago"),
    "cancelacion_solicitada": ("nuevo", "⚠ Cancelación sin confirmar"),
    "reembolsado": ("asignada", "↩ Reembolsado"),
    "borrador": ("asignada", "✎ Borrador"),
}
# Estados en los que el cobro todavía se cancela o se le pregunta a Stripe.
COBRO_VIVO = ("borrador", "pendiente", "cancelacion_solicitada")


def _ticket_panel_json(ticket: Ticket) -> dict:
    """El folio con lo que el cajón muestra de más que la tarjeta del tablero."""
    return {
        "id": ticket.id,
        "status": ticket.status.value,
        "assigned_to": ticket.assigned_to,
        "service_name": ticket.service_name,
        "category": ticket.category,
        "price_info": ticket.price_info,
        "escalado": bool(ticket.escalado),
        "agent_active": bool(ticket.agent_active),
        "habilitado": bool(ticket.habilitado),
        "created_at": _iso(ticket.created_at),
        # Una sola cadena con todas las notas, como la guarda _append_note.
        "notes": ticket.notes or "",
    }


def _permisos_folio(ticket: Ticket | None, user: str, companeros: list, stripe: bool) -> dict:
    """Lo que ESTE usuario puede hacerle al folio vigente.

    Copia las condiciones de los endpoints, no las de ticket_detail.html: la
    plantilla es más estricta que el backend en cuatro casos (terminar y soltar
    también los acepta un admin; etiquetar y agendar seguimiento, cualquiera de
    la oficina). El cajón ofrece lo que el servidor de verdad permite y explica
    el resto, en vez de dejar un botón que no hace nada.
    """
    from app import cobranza

    admin = is_admin(user)
    atiende = ticket is not None and ticket.assigned_to == user
    abierto = ticket is not None and ticket.status != TicketStatus.CERRADO
    cobrar = ticket is not None and cobranza.puede_cobrar(user, ticket, None)
    return {
        "es_admin": admin,
        "atiende": atiende,
        # dashboard.py:1167 — el estado SOLO lo cambia quien atiende, ni el admin.
        "estado": atiende,
        "terminar": bool((atiende or admin) and abierto),
        "soltar": bool((atiende or admin) and ticket is not None and ticket.assigned_to),
        "agente": bool(atiende and ticket is not None and ticket.escalado),
        "tomar": bool(ticket is not None and ticket.assigned_to is None and abierto),
        "notas": atiende,
        "etiquetas": ticket is not None,
        "seguimiento": ticket is not None,
        "transferir": bool(atiende and companeros),
        "cobrar": bool(cobrar),
        "generar_cobro": bool(cobrar and stripe and abierto),
    }


def _cobro_json(cobro, puede: bool) -> dict:
    from app.dinero import formato_monto

    clase, texto = BADGE_COBRO.get(
        cobro.estado, ("caducada", f"✕ {cobro.estado.capitalize()}")
    )
    avisos = []
    if cobro.confirmado_por_asesor and cobro.estado != "pagado":
        avisos.append({
            "clase": "esperando",
            "texto": f"Confirmado por {cobro.confirmado_por} · Stripe aún no",
        })
    if cobro.discrepancia:
        avisos.append({"clase": "nuevo", "texto": "⚠ Revisar: discrepancia"})
    if cobro.disputado:
        avisos.append({"clase": "nuevo", "texto": "⚠ En disputa"})
    if cobro.livemode is False:
        avisos.append({"clase": "asignada", "texto": "modo prueba"})

    # `metodo_usado` es el instrumento vivo, no un hecho consumado: solo se
    # conjuga en pasado cuando el dinero ya entró (misma regla que _cobro.html).
    pie = []
    if cobro.metodo_usado:
        verbo = "Pagó con" if cobro.estado in ("pagado", "reembolsado") else "Por"
        msi = f" a {cobro.msi_meses_usado} MSI" if cobro.msi_meses_usado else ""
        pie.append(f"{verbo} {cobro.metodo_usado}{msi}")
    pie.append(f"Generado por {cobro.creado_por} el {local_dt(cobro.created_at)}")
    if cobro.enviado_at:
        pie.append(f"enviado {local_dt(cobro.enviado_at)}")
    if cobro.voucher_expira_at:
        pie.append(f"la ficha vence el {local_dt(cobro.voucher_expira_at, '%d/%m/%Y')}")
    if cobro.referencia_recibo:
        pie.append(f"recibo {cobro.referencia_recibo}")

    vivo = cobro.estado in COBRO_VIVO
    return {
        "id": cobro.id,
        "estado": cobro.estado,
        "badge_clase": clase,
        "badge_texto": texto,
        "monto": formato_monto(cobro.monto_centavos, cobro.moneda),
        "concepto": cobro.concepto,
        "motivo": cobro.estado_motivo or "",
        "avisos": avisos,
        "pie": " · ".join(pie),
        "url": cobro.voucher_url or cobro.url_pago,
        "url_texto": "Ver ficha de pago" if cobro.voucher_url else "Abrir link de pago",
        "puede_enviar": bool(puede and cobro.estado in ("pendiente", "borrador")),
        "puede_cancelar": bool(puede and vivo),
        "puede_reconciliar": bool(puede and vivo),
        "puede_confirmar": bool(
            puede
            and cobro.estado in ("pendiente", "cancelacion_solicitada")
            and not cobro.confirmado_por_asesor
        ),
        "puede_avisar": bool(
            puede and cobro.estado == "pagado" and cobro.aviso_pago_enviado_at is None
        ),
    }


def _cobro_panel(session, ticket: Ticket, user: str) -> dict:
    """Lo mismo que dashboard._contexto_cobranza, ya traducido a JSON."""
    import secrets

    from app import cobranza
    from app.credenciales import credencial
    from app.dinero import OXXO_MAX_CENTAVOS, formato_monto, monto_editable, parse_monto
    from app.models import OFICINA_TLAXCALA_ID, Cobro

    cobros = session.exec(
        select(Cobro)
        .where(Cobro.ticket_id == ticket.id)
        .order_by(Cobro.created_at.desc())  # type: ignore[union-attr]
        .limit(MAX_COBROS_PANEL)
    ).all()
    # Cobros vivos del MISMO teléfono en otros folios: con OXXO el folio se
    # cierra y el bot abre otro, y ese instrumento sigue siendo cobrable.
    otros = [
        c for c in session.exec(
            select(Cobro)
            .where(Cobro.phone == ticket.phone)
            .where(Cobro.llave_cobro_vivo.is_not(None))  # type: ignore[union-attr]
        ).all()
        if c.ticket_id != ticket.id
    ]
    puede = cobranza.puede_cobrar(user, ticket, None)
    # La línea base del monto es lo que el bot le COTIZÓ al cliente, no el
    # catálogo vivo: ese es el número que el cliente leyó.
    cotizado = None
    if ticket.price_info:
        try:
            cotizado = parse_monto(ticket.price_info)
        except ValueError:
            cotizado = None  # "A cotizar" y cualquier texto ambiguo
    return {
        # ponytail: office_id fijo a Tlaxcala, igual que _contexto_cobranza.
        "stripe_configurado": bool(
            credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID, session=session)
        ),
        # Único por CONSULTA: dos envíos del mismo formulario (doble clic)
        # chocan contra ux_cobro_generacion en vez de crear dos cobros. El cajón
        # vuelve a pedir el panel después de cada acción, así que siempre tiene
        # uno fresco.
        "token_form": secrets.token_hex(16),
        "monto_sugerido": monto_editable(cotizado) if cotizado else "",
        "monto_cotizado": formato_monto(cotizado) if cotizado else "",
        "oxxo_max": formato_monto(OXXO_MAX_CENTAVOS),
        "concepto_sugerido": f"{ticket.service_name} (folio #{ticket.id})",
        "cobros": [_cobro_json(c, puede) for c in cobros],
        "otros_folios": [
            {
                "id": c.id,
                "ticket_id": c.ticket_id,
                "monto": formato_monto(c.monto_centavos, c.moneda),
                "concepto": c.concepto,
            }
            for c in otros
        ],
    }


@router.get("/crm/api/panel/{phone}")
def crm_panel(request: Request, phone: str):
    """Todo lo que el cajón lateral necesita del folio vigente de esa conversación.

    Aparte del hilo a propósito: el chat se sondea cada 8 s y no tiene por qué
    arrastrar cobros, compañeros y catálogo de etiquetas. Este se pide al abrir
    la conversación y después de cada acción. Misma regla de visibilidad que el
    chat: fuera de la oficina —o, para un colaborador, fuera de sus folios—
    contesta «no existe».
    """
    user = _current_user(request)
    if not user:
        return _sin_sesion()
    with get_session() as session:
        cliente, agg = _conversacion_visible(session, phone, user)
        if cliente is None:
            return _error(404, "No existe")
        ticket = agg[cliente.phone]["ticket"]

        etiquetas: list[dict] = []
        catalogo: list[str] = []
        seguimientos: list[dict] = []
        companeros: list[dict] = []
        cobro: dict | None = None
        if ticket is not None:
            etiquetas = [
                {"id": l.id, "nombre": l.nombre, "color": l.color}
                for l in session.exec(
                    select(Label)
                    .join(TicketLabel, TicketLabel.label_id == Label.id)
                    .where(TicketLabel.ticket_id == ticket.id)
                    .where(TicketLabel.habilitado == True)  # noqa: E712
                    .where(Label.habilitado == True)  # noqa: E712
                    .order_by(Label.nombre)
                ).all()
            ]
            # Para el datalist de «+ etiqueta», como en ticket_detail.
            catalogo = [
                l.nombre
                for l in session.exec(
                    select(Label)
                    .where(Label.habilitado == True)  # noqa: E712
                    .order_by(Label.nombre)
                ).all()
            ]
            seguimientos = [
                {
                    "id": f.id,
                    "fecha": local_dt(f.due_at, "%d %b"),
                    "username": f.username,
                    "note": f.note or "",
                }
                for f in session.exec(
                    select(FollowUp)
                    .where(FollowUp.ticket_id == ticket.id)
                    .where(FollowUp.done_at.is_(None))  # type: ignore[union-attr]
                    .where(FollowUp.habilitado == True)  # noqa: E712
                    .order_by(FollowUp.due_at)
                ).all()
            ]
            # A quién se puede transferir: gente de la oficina DEL FOLIO (no la
            # de quien mira: un admin ve varias). Mismo criterio que reassign.
            companeros = [
                {"username": u.username, "estado": u.estado or "disponible"}
                for u in session.exec(
                    select(User)
                    .where(User.habilitado == True, User.office_id == ticket.office_id)  # noqa: E712
                    .order_by(User.username)
                ).all()
                if u.username != user
            ]
            cobro = _cobro_panel(session, ticket, user)

        # La nota del cliente vive en la ficha y sobrevive a todos los folios:
        # es lo ÚNICO que se puede hacer en una conversación que todavía no
        # tiene solicitud, que en el CRM son muchas.
        notas_cliente = [
            {
                "id": n.id,
                "autor": n.author,
                "texto": n.body,
                "fecha": local_dt(n.created_at),
            }
            for n in session.exec(
                select(ClientNote)
                .where(ClientNote.phone == cliente.phone)
                .where(ClientNote.habilitado == True)  # noqa: E712
                .order_by(ClientNote.created_at.desc())  # type: ignore[union-attr]
                .limit(MAX_NOTAS_CLIENTE)
            ).all()
        ]

        permisos = _permisos_folio(
            ticket, user, companeros, bool(cobro and cobro["stripe_configurado"])
        )
        notas_folio = len(
            [l for l in ((ticket.notes if ticket else "") or "").splitlines() if l.strip()]
        )
        return JSONResponse({
            "phone": cliente.phone,
            "nombre": cliente.name,
            "ticket": None if ticket is None else _ticket_panel_json(ticket),
            "permisos": permisos,
            "etiquetas": etiquetas,
            "catalogo_etiquetas": catalogo,
            "notas_cliente": notas_cliente,
            "seguimientos": seguimientos,
            "companeros": companeros,
            "cobro": cobro,
            # Lo que el riel pinta en cada píldora sin abrir el cajón.
            "contadores": {
                "etiquetas": len(etiquetas),
                "notas": notas_folio + len(notas_cliente),
                "cobro": len(cobro["cobros"]) if cobro else 0,
                "seguimiento": len(seguimientos),
            },
        })
