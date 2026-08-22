"""Estudio del Agente (/agente) — solo administradores.

El "workflow builder" del agente real: un canvas con las capas de la cascada
como nodos (literal → Ollama → Claude → fuzzy → RAG → tools → escalada), su
configuración editable en caliente (app.agent_settings) y los párrafos del
corpus informativo (tabla infodoc) como nodos conectados a la capa RAG.

Sin esto, encender una capa o corregir un párrafo del corpus exige ssh al
VPS + recrear el contenedor. Todas las mutaciones quedan en AdminAudit y las
tablas se espejan a MSSQL como el resto.

Guardrail de contenido AL GUARDAR un párrafo (la misma regla que la capa RAG
aplica a la SALIDA): sin montos, sin CURP, sin teléfonos. Un corpus que nace
limpio no puede filtrar lo que no tiene.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlmodel import Session, func, select

import app.db as db
from app import agent_settings, rag
from app.audit import log_admin
from app.auth import is_admin
from app.config import settings
from app.models import InfoDoc, NluEvent, utcnow
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_PARRAFO = 1200
METRICAS_DIAS = 14


def _admin_or_redirect(request: Request):
    user = request.session.get("user")
    if not user:
        return None, RedirectResponse("/login", status_code=303)
    if not is_admin(user):
        return None, RedirectResponse("/", status_code=303)
    return user, None


def _admin_or_403(request: Request) -> str | None:
    user = request.session.get("user")
    return user if user and is_admin(user) else None


def _motivo_rechazo(texto: str) -> str | None:
    """La regla dura del corpus, aplicada a la ENTRADA. Reusa los patrones de
    rag para que entrada y salida vigilen exactamente lo mismo."""
    if rag._DINERO_RE.search(texto):
        return "El corpus no lleva precios ni montos: cotizar es del flujo determinista."
    if rag._CURP_RE.search(texto):
        return "El corpus no lleva CURPs ni datos de clientes."
    if rag._TELEFONO_RE.search(rag._SEPARADORES_RE.sub("", texto)):
        return "El corpus no lleva teléfonos."
    return None


def _parrafos(session: Session) -> list[dict]:
    filas = session.exec(
        select(InfoDoc)
        .where(InfoDoc.habilitado == True)  # noqa: E712
        .order_by(InfoDoc.position, InfoDoc.id)
    ).all()
    return [{"id": f.id, "texto": f.texto, "position": f.position} for f in filas]


def _metricas_por_capa(session: Session) -> dict[str, int]:
    """Mensajes por capa (14 días) para pintar actividad en cada nodo."""
    from datetime import timedelta

    cutoff = utcnow() - timedelta(days=METRICAS_DIAS)
    filas = session.exec(
        select(NluEvent.capa, func.count())
        .where(NluEvent.created_at >= cutoff)
        .where(NluEvent.habilitado == True)  # noqa: E712
        .group_by(NluEvent.capa)
    ).all()
    return {capa: n for capa, n in filas}


def _herramientas(session: Session) -> list[dict]:
    """Uso por herramienta del nodo agéntico (14 días).

    Cuenta las filas capa "tool"/"tool_error" — una por LLAMADA — y NO se mezcla
    con `mensajes` del nodo, que sigue siendo "mensajes que el nodo respondió".
    Son números distintos: un mensaje puede gastar varias llamadas.

    Se listan todas las herramientas del catálogo de permisos, aunque nunca se
    hayan usado: un cero explícito es información, una fila ausente no."""
    from datetime import timedelta

    from app import agent_tools

    cutoff = utcnow() - timedelta(days=METRICAS_DIAS)
    filas = session.exec(
        select(NluEvent.service_id, NluEvent.capa, func.count())
        .where(NluEvent.created_at >= cutoff)
        .where(NluEvent.capa.in_(("tool", "tool_error")))
        .where(NluEvent.habilitado == True)  # noqa: E712
        .group_by(NluEvent.service_id, NluEvent.capa)
    ).all()
    conteo: dict[str, dict[str, int]] = {}
    for nombre, capa, n in filas:
        registro = conteo.setdefault(nombre or "?", {"ok": 0, "error": 0})
        registro["error" if capa == "tool_error" else "ok"] += n
    return [
        {
            "nombre": nombre,
            "permiso": permiso,
            # "cobrar" es DENY por construcción y NO se puede encender desde
            # aquí: mover dinero es del panel del asesor, nunca del modelo.
            "bloqueada": permiso != "allow",
            "usos": conteo.get(nombre, {}).get("ok", 0),
            "errores": conteo.get(nombre, {}).get("error", 0),
        }
        for nombre, permiso in agent_tools.PERMISOS.items()
    ]


def _estado(session: Session) -> dict:
    m = _metricas_por_capa(session)
    config = agent_settings.estado_actual()
    # Los nodos del pipeline REAL, en el orden real de la cascada. "activo"
    # sale de la config vigente; las capas sin flag siempre corren.
    literal = m.get("nombre", 0) + m.get("sinonimo", 0) + m.get("keyword", 0)
    nodos = [
        {"id": "literal", "nombre": "Match literal", "activo": True, "fijo": True,
         "detalle": "nombre / sinónimo / keyword — gratis e instantáneo", "mensajes": literal},
        {"id": "ollama", "nombre": f"Ollama · {settings.ollama_model}", "activo": bool(settings.ollama_url),
         "fijo": not settings.ollama_url, "detalle": "clasificador local en labs",
         "mensajes": m.get("ollama", 0)},
        # El nodo "Claude (respaldo)" ya no se dibuja: config.CLAUDE_HABILITADO
        # está en False, así que esa capa nunca puede encenderse y pintarla solo
        # ensuciaba el lienzo con un nodo apagado para siempre. Sus mensajes
        # históricos (capa "llm") siguen contando en las métricas del panel.
        {"id": "fuzzy", "nombre": f"Difuso · umbral {settings.fuzzy_threshold}", "activo": True,
         "fijo": True, "detalle": "tolera faltas de ortografía", "mensajes": m.get("fuzzy", 0)},
        {"id": "rag", "nombre": "Capa informativa (RAG)", "activo": settings.rag_enabled,
         "fijo": False, "detalle": "responde dudas con el corpus público",
         "mensajes": m.get("rag", 0), "descartes": m.get("rag_descartado", 0)},
        {"id": "tools", "nombre": "Nodo agéntico (tools)", "activo": settings.agent_tools_enabled,
         "fijo": False, "detalle": "cotiza y consulta folios con herramientas",
         "mensajes": m.get("tools", 0)},
        {"id": "escalada", "nombre": "Asesor humano", "activo": True, "fijo": True,
         "detalle": "el último recurso de siempre", "mensajes": m.get("ninguna", 0)},
    ]
    return {"nodos": nodos, "config": config, "parrafos": _parrafos(session),
            "herramientas": _herramientas(session)}


@router.get("/agente")
def estudio(request: Request):
    user, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with Session(db.engine) as session:
        estado = _estado(session)
    return templates.TemplateResponse(
        "agente.html", {"request": request, "user": user, "estado": estado}
    )


class ConfigIn(BaseModel):
    key: str
    value: str


@router.post("/agente/config")
def guardar_config(request: Request, data: ConfigIn):
    user = _admin_or_403(request)
    if not user:
        return JSONResponse({"error": "solo administradores"}, status_code=403)
    try:
        with Session(db.engine) as session:
            valor = agent_settings.guardar(session, data.key, data.value, user)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    log_admin(user, f"agente:config {data.key}={data.value}")
    return {"ok": True, "key": data.key, "valor": valor}


class ParrafoIn(BaseModel):
    texto: str


@router.post("/agente/corpus")
def crear_parrafo(request: Request, data: ParrafoIn):
    user = _admin_or_403(request)
    if not user:
        return JSONResponse({"error": "solo administradores"}, status_code=403)
    texto = data.texto.strip()
    if not texto or len(texto) > MAX_PARRAFO:
        return JSONResponse({"error": f"el párrafo va de 1 a {MAX_PARRAFO} caracteres"}, status_code=400)
    if (motivo := _motivo_rechazo(texto)) is not None:
        return JSONResponse({"error": motivo}, status_code=400)
    with Session(db.engine) as session:
        tope = session.exec(select(func.max(InfoDoc.position))).one() or 0
        fila = InfoDoc(texto=texto, position=tope + 1, updated_by=user)
        session.add(fila)
        session.commit()
        session.refresh(fila)
    log_admin(user, "agente:corpus-alta", str(fila.id))
    return {"ok": True, "id": fila.id, "position": fila.position}


class ParrafoEdit(BaseModel):
    texto: str | None = None
    position: int | None = None
    borrar: bool = False


@router.post("/agente/corpus/{doc_id}")
def editar_parrafo(request: Request, doc_id: int, data: ParrafoEdit):
    user = _admin_or_403(request)
    if not user:
        return JSONResponse({"error": "solo administradores"}, status_code=403)
    with Session(db.engine) as session:
        fila = session.get(InfoDoc, doc_id)
        if fila is None or not fila.habilitado:
            return JSONResponse({"error": "párrafo no encontrado"}, status_code=404)
        if data.borrar:
            # Soft delete: el espejo MSSQL es upsert-only y un DELETE local
            # dejaría una divergencia invisible en /system.
            fila.habilitado = False
            accion = "agente:corpus-baja"
        else:
            if data.texto is not None:
                texto = data.texto.strip()
                if not texto or len(texto) > MAX_PARRAFO:
                    return JSONResponse(
                        {"error": f"el párrafo va de 1 a {MAX_PARRAFO} caracteres"}, status_code=400
                    )
                if (motivo := _motivo_rechazo(texto)) is not None:
                    return JSONResponse({"error": motivo}, status_code=400)
                fila.texto = texto
            if data.position is not None:
                fila.position = max(0, int(data.position))
            accion = "agente:corpus-edicion"
        fila.updated_by = user
        fila.updated_at = utcnow()
        session.add(fila)
        session.commit()
    log_admin(user, accion, str(doc_id))
    return {"ok": True}


class PruebaIn(BaseModel):
    texto: str


@router.post("/agente/probar")
def probar(request: Request, data: PruebaIn):
    """El "Run" del estudio: corre el mensaje por la cascada REAL en seco.

    Sin efectos: nlu.deduce_explained y rag.answer_explained solo LEEN; aquí
    no se escribe nluevent ni se manda WhatsApp — es un banco de pruebas, no
    tráfico. El nodo agéntico no se ejecuta (haría commits y llamadas caras);
    se reporta si le tocaría el turno."""
    import time

    from app import nlu

    user = _admin_or_403(request)
    if not user:
        return JSONResponse({"error": "solo administradores"}, status_code=403)
    texto = data.texto.strip()
    if not texto or len(texto) > 500:
        return JSONResponse({"error": "el mensaje va de 1 a 500 caracteres"}, status_code=400)

    inicio = time.perf_counter()
    service, category, capa, confianza = nlu.deduce_explained(texto)
    resultado = {
        "capa": capa,
        "servicio": service.name if service else None,
        "categoria": category.name if category else None,
        "confianza": confianza,
        "es_vago": nlu.es_vago(texto),
        "respuesta_rag": None,
        "le_tocaria_tools": False,
    }
    if service is None and category is None:
        sugerencias = [s.name for s in nlu.suggestions(texto)]
        resultado["sugerencias"] = sugerencias
        if settings.rag_enabled and rag.es_pregunta_informativa(texto):
            respuesta, motivo = rag.answer_explained(texto)
            resultado["respuesta_rag"] = respuesta
            resultado["rag_descartada"] = motivo
            # Sin respuesta y sin guardrail = el modelo dijo NO_SE o la
            # llamada falló (timeout/red). Antes esto era indistinguible de
            # "la capa está apagada" y el admin no sabía qué revisar.
            resultado["rag_sin_respuesta"] = respuesta is None and motivo is None
        if resultado["respuesta_rag"] is None and not sugerencias:
            resultado["le_tocaria_tools"] = settings.agent_tools_enabled
    resultado["ms"] = round((time.perf_counter() - inicio) * 1000)
    log_admin(user, "agente:prueba", texto[:80])
    return resultado


# ---------------------------------------------------------------------------
# Match literal: sinónimos del catálogo
#
# La capa literal no tiene "configuración": lo que la alimenta son los datos
# del catálogo (nombre oficial + sinónimos). Editarlos desde aquí NO crea un
# segundo catálogo — todo pasa por app.catalog, que es quien audita el cambio
# y refresca su caché. Las keywords no son un campo: la capa keyword empata
# subcadenas del NOMBRE del servicio (catalog.search_by_keyword).
# ---------------------------------------------------------------------------


@router.get("/agente/sinonimos")
def listar_sinonimos(request: Request):
    """Servicios con sus sinónimos, para el panel del nodo literal. Se pide al
    hacer clic en el nodo (no viaja en cada carga de /agente)."""
    if not _admin_or_403(request):
        return JSONResponse({"error": "solo administradores"}, status_code=403)
    from app.catalog import catalog

    servicios = [
        {
            "id": s.id,
            "nombre": s.name,
            "categoria": c.name,
            "sinonimos": list(s.synonyms),
        }
        for c in catalog.categories
        for s in c.services
    ]
    return {"servicios": servicios}


class SinonimoIn(BaseModel):
    service_id: str
    texto: str
    quitar: bool = False


@router.post("/agente/sinonimos")
def editar_sinonimo(request: Request, data: SinonimoIn):
    user = _admin_or_403(request)
    if not user:
        return JSONResponse({"error": "solo administradores"}, status_code=403)
    from app.catalog import catalog

    texto = data.texto.strip()
    if not texto or len(texto) > 80:
        return JSONResponse({"error": "el sinónimo va de 1 a 80 caracteres"}, status_code=400)
    # catalog devuelve el error como texto (no lanza), igual que catalog_admin.
    error = (
        catalog.remove_synonym(data.service_id, texto, admin=user)
        if data.quitar
        else catalog.add_synonym(data.service_id, texto, admin=user)
    )
    if error:
        return JSONResponse({"error": error}, status_code=400)
    accion = "quitar" if data.quitar else "agregar"
    log_admin(user, f"agente:sinonimo-{accion}", f"{data.service_id}: {texto[:60]}")
    return {"ok": True}
