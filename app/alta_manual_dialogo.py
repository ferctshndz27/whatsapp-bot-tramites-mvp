"""Diálogo del alta manual (ext-cse, B6): del número del cliente al folio creado.

Arranca donde termina el barrido (B5): el agente ya preguntó el número y el colaborador contesta.
Cada paso es un estado de `conversation.py` y todos comparten el contexto del lote
(`colaborador`, `mensajes`, `total`, `pregunta`), al que se le suman los datos que se van juntando:

    alta_manual_numero    ──número──▶ alta_manual_confirma ─Sí─▶ alta_manual_nombre (solo si es nuevo)
                                              ▲  Corregir ─┘              │
    alta_manual_servicio  ◀────────────────────────────────────────────────┘
          │ (lista de 8 por página, o el nombre tecleado)
    alta_manual_asignado  (colaboradores elegibles, «Yo» primero)
          │
    alta_manual_resumen   ─Sí─▶ crear_folio_manual ─▶ inicio

Qué NO hace, a propósito:
- No escribe nada hasta el «Sí» del resumen: todo lo anterior vive en `conversation.context_json`,
  así que cancelar o dejar caducar no deja ni un cliente ni un folio a medias.
- No decide reglas del folio: las valida `folio_manual.crear_folio_manual` y aquí solo se traduce su
  rechazo a una frase y al paso al que se vuelve.
- No pasa por la cascada NLU: un colaborador que dicta números y nombres de trámite no está pidiendo
  un trámite para sí.

Los mensajes del lote se MUDAN al chat del cliente al crear el folio (`Message.phone` pasa del
colaborador al cliente): así el material aparece en `/crm` donde el asesor lo busca. Siguen siendo
filas entrantes; el folio y la bitácora (`alta-manual`) dicen quién los reenvió.

ponytail: la lista de trámites es plana por orden de catálogo, 8 por página. Con un catálogo de cientos,
el paso natural es elegir categoría primero; hoy el colaborador también puede teclear el nombre.
"""

import re

from sqlmodel import Session, select

from app import asignacion
from app.auth import alcance
from app.catalog import catalog
from app.clients import MAX_NOMBRE
from app.conversation import (
    ALTA_ASIGNADO,
    ALTA_CONSENT,
    ALTA_CONFIRMA,
    ALTA_NOMBRE,
    ALTA_NUMERO,
    ALTA_RESUMEN,
    ALTA_SERVICIO,
    CONFIRM_BUTTONS,
    ESTADOS_ALTA_DIALOGO,
    _get_context,
    _set_state,
)
from app.folio_manual import FolioManualError, crear_folio_manual
from app.invitacion import consentido
from app.models import Client, Conversation, Message, User, utcnow
from app.utils import normalize_phone

POR_PAGINA = 8  # + «Más» + «Anteriores» = 10 filas, el tope de Meta
ID_MAS, ID_ANTERIORES = "9", "10"
CORREGIR_BOTONES = [("si", "✅ Sí, es ese"), ("corregir", "✏️ Corregir")]
SI = {"si", "sí", "s", "correcto", "ok", "confirmo", "es ese", "sí, es ese"}
CORREGIR = {"corregir", "no", "otro", "incorrecto", "✏️ corregir"}
_SOLO_NUMERO = re.compile(r"^[\d\s+()\-.]+$")
_QUE_PASO = {
    "otra_oficina": "Ese cliente es de otra oficina y no puedes darle de alta un folio. Descarté lo que me enviaste.",
    "cliente_inhabilitado": "Ese cliente está dado de baja. Descarté lo que me enviaste.",
    "tope": "Ese cliente ya tiene el máximo de folios abiertos. Descarté lo que me enviaste.",
    "creador": "Tu usuario ya no está habilitado. Descarté lo que me enviaste.",
    "telefono": "Ese número no sirve. Descarté lo que me enviaste.",
}


def _norm(texto: str) -> str:
    return texto.strip().lower().strip("¡!.,; ")


def _avanzar(session: Session, conv: Conversation, estado: str, ctx: dict, **nuevos) -> None:
    _set_state(session, conv, estado, {**ctx, **nuevos})


def _paginar(items: list, pagina: int) -> tuple[list, bool, bool, int]:
    """(los de esta página, ¿hay más?, ¿hay anteriores?, página corregida)."""
    ultima = max(0, (len(items) - 1) // POR_PAGINA)
    pagina = min(max(pagina, 0), ultima)
    ini = pagina * POR_PAGINA
    return items[ini : ini + POR_PAGINA], pagina < ultima, pagina > 0, pagina


def _servicios() -> list:
    return [s for c in catalog.categories for s in c.services]


def _filas(visibles: list[str], hay_mas: bool, hay_ant: bool, etiqueta_mas: str) -> list[tuple[str, str]]:
    filas = [(str(i), t) for i, t in enumerate(visibles, start=1)]
    if hay_mas:
        filas.append((ID_MAS, etiqueta_mas))
    if hay_ant:
        filas.append((ID_ANTERIORES, "⬅️ Anteriores"))
    return filas


def _menu(titulo: str, filas: list[tuple[str, str]]) -> str:
    lineas = [f"{i}. {t}" for i, t in filas]
    return titulo + "\n" + "\n".join(lineas) + "\nResponde con el número."


# --- Pasos ------------------------------------------------------------------------------------


def _pide_numero(session: Session, conv: Conversation, ctx: dict, aviso: str) -> list[str]:
    _avanzar(session, conv, ALTA_NUMERO, ctx, pregunta="enviada")
    return [aviso]


def _paso_numero(session, conv, ctx, colaborador, text, entrante_id) -> list[str] | None:
    # Algo que no parece un número es MÁS material del lote (None: el lote lo toma y reinicia el reloj).
    if not text.strip() or not _SOLO_NUMERO.match(text.strip()):
        return None
    canonico = normalize_phone(text)
    if not canonico:
        return _pide_numero(session, conv, ctx, "Ese número no es válido. Escríbelo con lada, 10 dígitos.")
    propio = session.exec(select(User).where(User.username == colaborador)).first()
    if propio is not None and normalize_phone(propio.phone) == canonico:
        return _pide_numero(session, conv, ctx, "Ese es tu número. Escribe el del cliente.")
    cliente = session.get(Client, canonico)
    if cliente is not None:
        if not cliente.habilitado:
            return _pide_numero(session, conv, ctx, "Ese cliente está dado de baja. Escribe otro número o «cancelar».")
        if not alcance(colaborador).alcanza(cliente.office_id):
            return _pide_numero(session, conv, ctx, "Ese cliente es de otra oficina. Escribe otro número o «cancelar».")
    _avanzar(session, conv, ALTA_CONFIRMA, ctx, cliente=canonico, nombre=(cliente.name if cliente else None))
    quien = f" ({cliente.name})" if cliente and cliente.name else " (cliente nuevo)" if cliente is None else ""
    return [f"El número es {canonico}{quien}. ¿Es correcto?"]


def _paso_confirma(session, conv, ctx, text) -> list[str]:
    n = _norm(text)
    if n in CORREGIR:
        return _pide_numero(session, conv, ctx, "Escribe el número correcto del cliente.")
    if n not in SI:
        return [f"El número es {ctx['cliente']}. ¿Es correcto? Responde Sí o Corregir."]
    if ctx.get("nombre"):
        return _lista_servicios(session, conv, ctx, 0)
    _avanzar(session, conv, ALTA_NOMBRE, ctx)
    return ["Es un cliente nuevo. ¿Cómo se llama?"]


def _paso_nombre(session, conv, ctx, text) -> list[str]:
    nombre = " ".join(text.split())[:MAX_NOMBRE]
    if len(nombre) < 2 or _SOLO_NUMERO.match(nombre):
        return ["Escribe el nombre del cliente (por ejemplo, Juan Pérez)."]
    ctx = {**ctx, "nombre": nombre}
    return _lista_servicios(session, conv, ctx, 0)


def _lista_servicios(session, conv, ctx, pagina) -> list[str]:
    visibles, mas, ant, pagina = _paginar(_servicios(), pagina)
    _avanzar(session, conv, ALTA_SERVICIO, ctx, pagina=pagina)
    filas = _filas([s.name for s in visibles], mas, ant, "➡️ Más trámites")
    return [_menu("¿Qué trámite es?", filas)]


def _paso_servicio(session, conv, ctx, text) -> list[str]:
    n = _norm(text)
    pagina = int(ctx.get("pagina", 0))
    visibles, mas, ant, pagina = _paginar(_servicios(), pagina)
    if n == ID_MAS and mas:
        return _lista_servicios(session, conv, ctx, pagina + 1)
    if n == ID_ANTERIORES and ant:
        return _lista_servicios(session, conv, ctx, pagina - 1)
    elegido = None
    if n.isdigit() and 1 <= int(n) <= len(visibles):
        elegido = visibles[int(n) - 1]
    else:
        exactos = [s for s in _servicios() if s.name.strip().lower() == n]
        encontrados = exactos or catalog.search_by_keyword(text)
        if len(encontrados) == 1:
            elegido = encontrados[0]
        elif len(encontrados) > 1:
            nombres = ", ".join(s.name for s in encontrados[:5])
            return [f"Hay varios que coinciden: {nombres}. Escribe el nombre completo o elige de la lista."]
    if elegido is None:
        return _lista_servicios(session, conv, ctx, pagina)
    return _lista_asignados(session, conv, {**ctx, "servicio": elegido.id}, 0)


def _oficina_del_folio(session: Session, ctx: dict, colaborador: str) -> int:
    cliente = session.get(Client, ctx["cliente"])
    return cliente.office_id if cliente else alcance(colaborador).office_id


def _elegibles(session: Session, ctx: dict, colaborador: str) -> list[str]:
    """«Yo» primero; luego el resto de la oficina del folio por nombre de usuario."""
    usuarios = asignacion._elegibles(session, _oficina_del_folio(session, ctx, colaborador))
    return ([colaborador] if colaborador in usuarios else []) + [u for u in usuarios if u != colaborador]


def _lista_asignados(session, conv, ctx, pagina) -> list[str]:
    elegibles = _elegibles(session, ctx, ctx["colaborador"])
    if not elegibles:
        _set_state(session, conv, "inicio")
        return ["No hay a quién asignarle el folio en esa oficina. Descarté lo que me enviaste."]
    visibles, mas, ant, pagina = _paginar(elegibles, pagina)
    _avanzar(session, conv, ALTA_ASIGNADO, ctx, pagina_asignado=pagina)
    etiquetas = [f"Yo ({u})" if u == ctx["colaborador"] else u for u in visibles]
    return [_menu("¿A quién se lo asigno?", _filas(etiquetas, mas, ant, "➡️ Más personas"))]


def _paso_asignado(session, conv, ctx, text) -> list[str]:
    n = _norm(text)
    pagina = int(ctx.get("pagina_asignado", 0))
    elegibles = _elegibles(session, ctx, ctx["colaborador"])
    visibles, mas, ant, pagina = _paginar(elegibles, pagina)
    if n == ID_MAS and mas:
        return _lista_asignados(session, conv, ctx, pagina + 1)
    if n == ID_ANTERIORES and ant:
        return _lista_asignados(session, conv, ctx, pagina - 1)
    elegido = None
    if n.isdigit() and 1 <= int(n) <= len(visibles):
        elegido = visibles[int(n) - 1]
    elif n in {"yo", "a mi", "a mí"} and ctx["colaborador"] in elegibles:
        elegido = ctx["colaborador"]
    elif text.strip().lower() in {u.lower() for u in elegibles}:
        elegido = next(u for u in elegibles if u.lower() == text.strip().lower())
    if elegido is None:
        return _lista_asignados(session, conv, ctx, pagina)
    _avanzar(session, conv, ALTA_RESUMEN, ctx, asignado=elegido)
    servicio = catalog.service_by_id(ctx["servicio"])
    quien = "tú" if elegido == ctx["colaborador"] else elegido
    nuevo = "" if ctx.get("nombre") and session.get(Client, ctx["cliente"]) else " (cliente nuevo)"
    return [
        f"Resumen:\n• Cliente: {ctx['nombre']} · {ctx['cliente']}{nuevo}\n"
        f"• Trámite: {servicio.name if servicio else ctx['servicio']}\n• Asignado a: {quien}\n"
        f"• Material: {ctx.get('total', 0)} mensaje(s)\n¿Lo creo?"
    ]


def _paso_resumen(session, conv, ctx, text) -> list[str]:
    n = _norm(text)
    if n in CORREGIR:
        return _lista_servicios(session, conv, ctx, 0)
    if n not in SI:
        return ["¿Lo creo? Responde Sí, No (para cambiar el trámite) o Cancelar."]
    cliente = session.get(Client, ctx["cliente"])
    if cliente is not None and consentido(cliente):
        return _crear_folio(session, conv, ctx)
    return _pide_consent(session, conv, ctx)


NO_CONSENT = {"no"}


def _paso_consent(session, conv, ctx, text) -> list[str]:
    n = _norm(text)
    if n not in SI | NO_CONSENT:
        return ["Responde Sí (para invitarlo) o No (creo el folio sin invitación)."]
    if n in NO_CONSENT:
        return _crear_folio(session, conv, ctx)
    # La fila del cliente nace al crear el folio: para registrar el opt-in ANTES de
    # esa creación hay que asegurarla aquí (con el nombre ya captado, si es nuevo).
    cliente = session.get(Client, ctx["cliente"])
    if cliente is None:
        cliente = Client(phone=ctx["cliente"], name=(ctx.get("nombre") or "")[:MAX_NOMBRE] or None)
        session.add(cliente)
        session.flush()
    if not consentido(cliente):
        cliente.opt_in_at = utcnow()
        cliente.opt_in_source = (
            "el cliente aceptó ser contactado desde el número A; lo captó " + ctx["colaborador"]
        )
        session.add(cliente)
        session.commit()
    return _crear_folio(session, conv, ctx)


def _pide_consent(session, conv, ctx) -> list[str]:
    _set_state(session, conv, ALTA_CONSENT, ctx)
    return [
        "El cliente habló contigo, no conmigo: no tengo ventana para escribirle desde mi número. "
        "¿Aceptó que lo contactemos desde ese número? Responde Sí (para invitarlo) o No "
        "(creo el folio sin invitación), o Cancelar para descartar todo."
    ]


def _crear_folio(session, conv, ctx) -> list[str]:
    """El «Sí» del resumen: crea el folio, muda el material y contesta. Aquí y solo aquí se escribe."""
    ids = [i for i in ctx.get("mensajes", []) if isinstance(i, int)]
    # Solo los mensajes que de verdad son de este colaborador: el id viaja en un JSON y no se confía.
    propio = session.exec(select(User).where(User.username == ctx["colaborador"])).first()
    mensajes = session.exec(
        select(Message).where(Message.id.in_(ids)).where(Message.phone == normalize_phone(propio.phone if propio else None))  # type: ignore[attr-defined]
    ).all() if ids else []
    try:
        ticket = crear_folio_manual(
            session, ctx["colaborador"], ctx["cliente"], ctx.get("nombre"), ctx["servicio"],
            ctx["asignado"], mensajes=mensajes, provider=conv.provider,
        )
    except FolioManualError as exc:
        return _rechazo(session, conv, ctx, exc)
    # Mudanza al chat del cliente (ORM: el espejo MSSQL escucha el update). `sent_by` dice quién se
    # lo mandó a A: son palabras del cliente, pero las reenvió el colaborador, así que no abren la
    # ventana del cliente (`ventana.ABRE_VENTANA`) ni el colaborador puede ocultarlas en la bandeja.
    for m in mensajes:
        m.phone = ticket.phone
        m.sent_by = ctx["colaborador"]
        session.add(m)
    session.commit()
    _set_state(session, conv, "inicio")
    quien = "tú" if ticket.assigned_to == ctx["colaborador"] else ticket.assigned_to
    return [f"Listo ✅ Creé el folio #{ticket.id} ({ticket.service_name}) de {ticket.client_name}, asignado a {quien}."]


def _rechazo(session, conv, ctx, exc: FolioManualError) -> list[str]:
    if exc.codigo == "repetido":
        return [f"Ese cliente ya tiene el folio #{exc.folio_id} de ese trámite."] + _lista_servicios(session, conv, ctx, 0)
    if exc.codigo == "nombre":
        _avanzar(session, conv, ALTA_NOMBRE, ctx)
        return ["Falta el nombre del cliente. ¿Cómo se llama?"]
    if exc.codigo == "servicio":
        return _lista_servicios(session, conv, ctx, 0)
    if exc.codigo == "asignado":
        return _lista_asignados(session, conv, ctx, 0)
    _set_state(session, conv, "inicio")
    return [_QUE_PASO.get(exc.codigo, "No pude crear el folio. Descarté lo que me enviaste.")]


def atender(session: Session, conv: Conversation, colaborador: str, text: str, entrante_id: int | None) -> list[str] | None:
    """Respuesta del paso en curso, o None si el mensaje es material del lote (solo en `alta_manual_numero`)."""
    ctx = _get_context(conv)
    ctx.setdefault("colaborador", colaborador)
    if conv.state == ALTA_NUMERO:
        return _paso_numero(session, conv, ctx, colaborador, text, entrante_id)
    if conv.state == ALTA_CONFIRMA:
        return _paso_confirma(session, conv, ctx, text)
    if conv.state == ALTA_NOMBRE:
        return _paso_nombre(session, conv, ctx, text)
    if conv.state == ALTA_SERVICIO:
        return _paso_servicio(session, conv, ctx, text)
    if conv.state == ALTA_ASIGNADO:
        return _paso_asignado(session, conv, ctx, text)
    if conv.state == ALTA_RESUMEN:
        return _paso_resumen(session, conv, ctx, text)
    if conv.state == ALTA_CONSENT:
        return _paso_consent(session, conv, ctx, text)
    return None


def presentar(session: Session, conv: Conversation, ultimo: str) -> dict | None:
    """Botones o lista nativa para la última respuesta del diálogo; None = texto plano."""
    if conv.state not in ESTADOS_ALTA_DIALOGO:
        return None
    ctx = _get_context(conv)
    if conv.state == ALTA_CONFIRMA and ultimo.endswith("¿Es correcto?"):
        return {"text": ultimo, "buttons": list(CORREGIR_BOTONES)}
    if conv.state == ALTA_RESUMEN and ultimo.endswith("¿Lo creo?"):
        return {"text": ultimo, "buttons": list(CONFIRM_BUTTONS)}
    if conv.state == ALTA_SERVICIO and ultimo.startswith("¿Qué trámite es?"):
        visibles, mas, ant, _ = _paginar(_servicios(), int(ctx.get("pagina", 0)))
        filas = _filas([s.name for s in visibles], mas, ant, "➡️ Más trámites")
        return {"text": ultimo, "list": filas, "button": "Ver opciones"} if filas else None
    if conv.state == ALTA_ASIGNADO and ultimo.startswith("¿A quién se lo asigno?"):
        elegibles = _elegibles(session, ctx, ctx.get("colaborador", ""))
        visibles, mas, ant, _ = _paginar(elegibles, int(ctx.get("pagina_asignado", 0)))
        etiquetas = [f"Yo ({u})" if u == ctx.get("colaborador") else u for u in visibles]
        filas = _filas(etiquetas, mas, ant, "➡️ Más personas")
        return {"text": ultimo, "list": filas, "button": "Ver opciones"} if filas else None
    return None
