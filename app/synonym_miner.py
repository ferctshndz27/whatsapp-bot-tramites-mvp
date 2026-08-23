"""Fase 0 + 1 del plan de mejora del agente.

Fase 0 — reporte de mensajes NO entendidos: se derivan de la tabla message
existente (el mensaje IN inmediatamente anterior a cada respuesta OUT que
lleva el aviso de "no te entendí"). Cero migración: ninguna tabla nueva.

Fase 1 — minería de sinónimos: con ANTHROPIC_API_KEY, Claude analiza esos
mensajes por lote y propone sinónimos para el catálogo; el admin aprueba
uno por uno (nada se agrega solo). Sin API key la vista sigue sirviendo:
lista los mensajes y permite agregar sinónimos a mano.
"""

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, Form, Request
from app.templating import templates
from rapidfuzz import fuzz
from sqlmodel import Session, select

from app import nlu
from app.audit import log_admin
from app.auth import is_admin
from app.catalog import catalog
from app.config import settings
from app.conversation import NOT_RECOGNIZED_MARKER
from app.db import get_session
from app.models import CAPAS_EXTRA, Direction, Message, NluEvent, SynonymProposal, utcnow
from app.system import _admin_or_redirect, _csv_response

logger = logging.getLogger(__name__)

router = APIRouter()

DAYS_BACK = 30
MAX_MESSAGES = 100

# Se busca por el fragmento estable del aviso, no por su inicio: así los
# mensajes ya enviados siguen contando aunque se retoque la cortesía inicial.
_HINT_MARKER = NOT_RECOGNIZED_MARKER

_MINE_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service_id": {"type": "string"},
                    "synonym": {"type": "string"},
                },
                "required": ["service_id", "synonym"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["proposals"],
    "additionalProperties": False,
}

_MINE_SYSTEM = """Eres analista de un bot de WhatsApp de trámites en México. Recibes \
mensajes reales de clientes que el bot NO supo relacionar con su catálogo. Propón \
sinónimos (palabras o frases cortas, en minúsculas, tal como las escribiría un cliente) \
para que el bot los entienda la próxima vez.

Catálogo (service_id: nombre):
{listing}

Reglas:
- Solo service_id que existan en el catálogo.
- Sinónimos de 1 a 5 palabras, sin signos, útiles para matching por texto.
- Omite mensajes que sean saludos, quejas o que no correspondan a ningún servicio.
- Máximo 20 propuestas, sin duplicados."""


def unmatched_messages(session: Session, days: int = DAYS_BACK, limit: int = MAX_MESSAGES):
    """Mensajes IN que precedieron a un 'No reconocí…' y que el motor actual
    sigue sin entender (los ya cubiertos por sinónimos nuevos se filtran)."""
    cutoff = utcnow() - timedelta(days=days)
    hints = session.exec(
        select(Message)
        .where(Message.direction == Direction.OUT)
        .where(Message.created_at >= cutoff)
        .where(Message.body.contains(_HINT_MARKER))  # type: ignore[attr-defined]
        .order_by(Message.created_at.desc())
        .limit(limit * 2)
    ).all()

    seen: set[str] = set()
    results = []
    for hint in hints:
        # ponytail: N+1 queries — con decenas de filas por mes sobra; pasar a un
        # self-join si el reporte llega a miles.
        incoming = session.exec(
            select(Message)
            .where(Message.phone == hint.phone)
            .where(Message.direction == Direction.IN)
            .where(Message.created_at <= hint.created_at)
            .order_by(Message.created_at.desc())
        ).first()
        if incoming is None:
            continue
        key = incoming.body.strip().lower()
        if not key or key in seen or incoming.body.startswith("["):
            continue
        # Si el motor de keywords/sinónimos de hoy ya lo entiende, no es pendiente.
        if nlu._deduce_keywords(incoming.body) != (None, None):
            continue
        seen.add(key)
        results.append(incoming)
        if len(results) >= limit:
            break
    return results


def mine_synonyms(texts: list[str]) -> list[dict]:
    """Pide al LLM propuestas de sinónimos y las valida contra el catálogo.

    Backend: el Ollama de labs si OLLAMA_URL está configurada (el negocio no
    paga API, y el texto de clientes se queda en infra propia); Claude solo
    como alternativa si hay ANTHROPIC_API_KEY."""
    if not texts:
        return []
    listing = "\n".join(
        f"- {s.id}: {s.name}" for c in catalog.categories for s in c.services
    )
    body = "\n".join(f"{i}. {t[:200]}" for i, t in enumerate(texts[:MAX_MESSAGES], start=1))
    system = _MINE_SYSTEM.format(listing=listing)

    if settings.ollama_url:
        message = nlu.ollama_chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": body},
            ],
            format=_MINE_SCHEMA,
            num_predict=1024,
        )
        data = json.loads(message["content"])
    else:
        response = nlu._get_client().messages.create(
            model=settings.nlu_model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": _MINE_SCHEMA}},
            messages=[{"role": "user", "content": body}],
        )
        if response.stop_reason == "refusal":
            return []
        data = json.loads(next(b.text for b in response.content if b.type == "text"))
    proposals, seen = [], set()
    for p in data.get("proposals", []):
        service = catalog.service_by_id(p.get("service_id", ""))
        synonym = p.get("synonym", "").strip().lower()
        key = (p.get("service_id"), synonym)
        if service is None or not synonym or key in seen:
            continue  # id alucinado o duplicado: fuera
        seen.add(key)
        proposals.append(
            {"service_id": service.id, "service_name": service.name, "synonym": synonym}
        )
    return proposals


def persist_proposals(session: Session, proposals: list[dict], origin: str) -> int:
    """Guarda propuestas nuevas como pendientes; se saltan las duplicadas y
    las que el catálogo ya cubre. Devuelve cuántas se crearon. Nada se
    aprueba solo: el admin decide en el panel."""
    existing = {
        (p.service_id, p.synonym.lower())
        for p in session.exec(select(SynonymProposal)).all()
    }
    created = 0
    for p in proposals:
        service = catalog.service_by_id(p.get("service_id", ""))
        synonym = p.get("synonym", "").strip().lower()
        if service is None or not synonym:
            continue
        key = (service.id, synonym)
        if key in existing or synonym in (s.lower() for s in service.synonyms):
            continue
        session.add(SynonymProposal(service_id=service.id, synonym=synonym, origin=origin))
        existing.add(key)
        created += 1
    return created


CLUSTER_DAYS = 14
CLUSTER_THRESHOLD = 80  # token_set_ratio: tolera orden y palabras extra
CLUSTER_TOP = 10


def _unmatched_events(session: Session, days: int) -> list[NluEvent]:
    """Eventos "no entendidos" vigentes de la ventana: respeta el soft delete
    de los grupos ignorados (habilitado=False). Fuente única de los grupos del
    panel y de lo que marca `ignore_cluster`."""
    from datetime import timezone

    cutoff = (utcnow() - timedelta(days=days)).astimezone(timezone.utc).replace(tzinfo=None)
    # WHERE en SQL para no escanear la tabla completa (nluevent es append-only);
    # +1 día de margen y el corte fino por fecha lo hace el filtro de abajo.
    where_cutoff = utcnow() - timedelta(days=days + 1)
    events = session.exec(
        select(NluEvent)
        .where(NluEvent.capa == "ninguna")
        .where(NluEvent.habilitado == True)  # noqa: E712
        .where(NluEvent.created_at >= where_cutoff)
    ).all()

    vigentes: list[NluEvent] = []
    for e in events:
        created_at = e.created_at
        if created_at.tzinfo is not None:
            created_at = created_at.astimezone(timezone.utc).replace(tzinfo=None)
        if created_at >= cutoff:
            vigentes.append(e)
    return vigentes


def cluster_unmatched(days: int = CLUSTER_DAYS) -> list[dict]:
    """Agrupa los textos que el agente no entendió (nluevent capa='ninguna')
    por similitud difusa y los ordena por frecuencia: "17 clientes pidieron
    'constancia de semanas cotizadas'" en vez de una lista plana."""
    with get_session() as session:
        events = _unmatched_events(session, days)

    counts: dict[str, int] = {}
    for e in events:
        text = nlu._norm(e.text)
        if len(text) < 4:
            continue
        counts[text] = counts.get(text, 0) + 1

    clusters: list[dict] = []
    for text, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        for c in clusters:
            if fuzz.token_set_ratio(text, c["rep"]) >= CLUSTER_THRESHOLD:
                c["count"] += n
                if len(c["examples"]) < 3:
                    c["examples"].append(text)
                break
        else:
            clusters.append({"rep": text, "count": n, "examples": []})
    clusters.sort(key=lambda c: -c["count"])
    return clusters[:CLUSTER_TOP]


def ignore_cluster(rep: str, days: int = CLUSTER_DAYS) -> int:
    """Ignora un grupo de no-entendidos: soft delete (habilitado=False) de los
    nluevent parecidos al representante, con la MISMA regla difusa que los
    agrupó. Nada se borra de la base; simplemente dejan de verse en el panel y
    de contar en las métricas. Devuelve cuántos se marcaron."""
    target = nlu._norm(rep)
    with get_session() as session:
        hits = [
            e
            for e in _unmatched_events(session, days)
            if fuzz.token_set_ratio(nlu._norm(e.text), target) >= CLUSTER_THRESHOLD
        ]
        for event in hits:
            event.habilitado = False
            session.add(event)
        session.commit()
    return len(hits)


# Colores por capa de deducción para la dona del panel (el color nunca es el
# único indicador: leyenda y tooltips llevan nombre y número).
METRICS_DAYS = 14
CAPA_COLORS = {
    "llm": "#8b5cf6", "ollama": "#84cc16", "nombre": "#2563eb", "sinonimo": "#10b981",
    "keyword": "#22d3ee", "fuzzy": "#f59e0b", "multi": "#ec4899",
    "categoria": "#14b8a6", "sugerencia": "#64748b", "ninguna": "#f43f5e",
    "rag": "#a3e635", "rag_descartado": "#fb923c", "tools": "#38bdf8",
    # Rojo oscuro, distinto del rosa de "ninguna": no entender es normal, que
    # el servidor de Ollama no conteste no lo es.
    "ollama_error": "#991b1b",
}


def nlu_metrics(days: int = METRICS_DAYS) -> dict:
    """KPIs y gráficas de la bitácora nluevent (últimos N días, horario de
    negocio). Reusa los builders SVG del panel de Actividad."""
    # ponytail: imports locales — reuso puntual de app.activity sin mover los
    # builders a un módulo aparte hasta que un tercer panel los necesite.
    from datetime import datetime, timezone

    from app.activity import BUSINESS_TZ, _as_naive_utc, build_donut, build_line_chart
    from app.models import NluEvent

    today = datetime.now(BUSINESS_TZ).date()
    start = today - timedelta(days=days - 1)
    # WHERE en SQL (no traer toda la tabla append-only): +1 día de margen y el
    # bucketing por día de negocio de abajo hace el corte fino.
    where_cutoff = utcnow() - timedelta(days=days + 1)
    with get_session() as session:
        events = session.exec(
            select(NluEvent)
            .where(NluEvent.created_at >= where_cutoff)
            # Los grupos ignorados a mano (soft delete) no cuentan en los KPIs.
            .where(NluEvent.habilitado == True)  # noqa: E712
        ).all()

    day_buckets = {
        start + timedelta(days=i): {"entendidos": 0, "no entendidos": 0}
        for i in range((today - start).days + 1)
    }
    capa_totals: dict[str, int] = {}
    total = entendidos = tickets = 0
    for e in events:
        local_day = (
            _as_naive_utc(e.created_at)
            .replace(tzinfo=timezone.utc)
            .astimezone(BUSINESS_TZ)
            .date()
        )
        bucket = day_buckets.get(local_day)
        if bucket is None:
            continue
        capa_totals[e.capa] = capa_totals.get(e.capa, 0) + 1
        if e.capa in CAPAS_EXTRA:
            # Fila EXTRA del mismo mensaje (rag, rag_descartado, tools — ver
            # conversation._try_informative): se mide en la dona de capas, pero
            # contarla como mensaje inflaría el denominador y taparía el %
            # real de no entendidos.
            continue
        total += 1
        ok = e.capa != "ninguna"
        entendidos += ok
        tickets += e.termino_en_ticket
        bucket["entendidos" if ok else "no entendidos"] += 1

    chart = [
        {"date": d, "total": sum(v.values()), "by_user": v} for d, v in day_buckets.items()
    ]
    line_colors = {"entendidos": "#10b981", "no entendidos": "#f43f5e"}
    capa_colors = {c: CAPA_COLORS.get(c, "#64748b") for c in sorted(capa_totals)}
    return {
        "days": days,
        "total": total,
        "entendidos_pct": round(entendidos / total * 100) if total else 0,
        "ticket_pct": round(tickets / total * 100) if total else 0,
        "line": build_line_chart(chart, line_colors),
        "line_colors": line_colors,
        "donut": build_donut(
            [{"date": today, "total": total, "by_user": capa_totals}], capa_colors
        ),
    }


def _render(request: Request, admin: str, proposals=None, error=None, ok=None):
    from app.alerts import active_alerts, health_level

    with get_session() as session:
        pending = unmatched_messages(session)
        db_proposals = session.exec(
            select(SynonymProposal)
            .where(SynonymProposal.status == "pendiente")
            .order_by(SynonymProposal.created_at.desc())
        ).all()
        # Salud del agente (Fase 4b): banner + lista, solo en este panel admin.
        alerts = active_alerts(session)
    health = health_level(alerts)
    names = {s.id: s.name for c in catalog.categories for s in c.services}
    services = [(s.id, s.name) for c in catalog.categories for s in c.services]
    return templates.TemplateResponse(
        "unmatched.html",
        {
            "request": request,
            "user": admin,
            "pending": pending,
            "services": services,
            "proposals": proposals or [],
            "db_proposals": [
                {
                    "id": p.id,
                    "service_name": names.get(p.service_id, p.service_id),
                    "synonym": p.synonym,
                    "origin": p.origin,
                    "created_at": p.created_at,
                }
                for p in db_proposals
            ],
            "clusters": cluster_unmatched(),
            "metrics": nlu_metrics(),
            "alerts": alerts,
            "health": health,
            "mining_available": bool(settings.ollama_url or settings.anthropic_api_key),
            "error": error,
            "ok": ok,
        },
    )


@router.get("/unmatched")
def unmatched_view(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    return _render(request, admin)


@router.post("/unmatched/mine")
def unmatched_mine(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        texts = [m.body for m in unmatched_messages(session)]
    try:
        proposals = mine_synonyms(texts)
    except Exception as exc:
        logger.warning("Minería de sinónimos falló: %s", exc)
        return _render(request, admin, error="No se pudo consultar a Claude. Intenta de nuevo.")
    with get_session() as session:
        created = persist_proposals(session, proposals, origin="claude")
        session.commit()
    log_admin(admin, "catalogo:mineria", f"{len(texts)} mensajes → {created} propuestas nuevas")
    if created == 0:
        return _render(request, admin, ok="Claude no encontró sinónimos nuevos que proponer.")
    return _render(
        request, admin,
        ok=f"{created} propuesta{'s' if created != 1 else ''} nueva{'s' if created != 1 else ''} "
           "pendiente de tu aprobación (abajo).",
    )


@router.post("/unmatched/proposals/{proposal_id}/approve")
def proposal_approve(request: Request, proposal_id: int):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        prop = session.get(SynonymProposal, proposal_id)
        if prop is None or prop.status != "pendiente":
            return _render(request, admin, error="Propuesta no encontrada o ya decidida.")
        error = catalog.add_synonym(prop.service_id, prop.synonym, admin=admin)
        # Si el catálogo la rechaza (p.ej. ya existe), se descarta para
        # sacarla de la cola en vez de dejarla atorada.
        prop.status = "aprobada" if error is None else "rechazada"
        prop.decided_by = admin
        prop.decided_at = utcnow()
        session.add(prop)
        session.commit()
        synonym, status = prop.synonym, prop.status
    log_admin(admin, "catalogo:propuesta", f"{status}:{synonym}")
    if error:
        return _render(request, admin, ok=f"Propuesta descartada: {error}")
    return _render(request, admin, ok=f"Sinónimo '{synonym}' agregado al catálogo.")


@router.post("/unmatched/proposals/{proposal_id}/reject")
def proposal_reject(request: Request, proposal_id: int):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        prop = session.get(SynonymProposal, proposal_id)
        if prop is None or prop.status != "pendiente":
            return _render(request, admin, error="Propuesta no encontrada o ya decidida.")
        prop.status = "rechazada"
        prop.decided_by = admin
        prop.decided_at = utcnow()
        session.add(prop)
        session.commit()
        synonym = prop.synonym
    log_admin(admin, "catalogo:propuesta", f"rechazada:{synonym}")
    return _render(request, admin, ok=f"Propuesta '{synonym}' descartada.")


@router.post("/unmatched/clusters/ignore")
def cluster_ignore(request: Request, synonym: str = Form(...)):
    """"Ignorar": el texto no era un trámite (saludo, cortesía, ruido). Soft
    delete — las filas se conservan en la base, pero salen del panel y de las
    estadísticas. Sirve desde los grupos (un clic) y desde el alta a mano
    (escribiendo la frase), por eso comparte el campo `synonym`."""
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    texto = synonym.strip()
    marcados = ignore_cluster(texto) if texto else 0
    if marcados == 0:
        return _render(request, admin, error=f"No hay mensajes pendientes parecidos a '{texto}'.")
    log_admin(admin, "agente:ignorar", f"{texto[:120]} ({marcados})")
    return _render(
        request, admin,
        ok=f"'{texto}' ignorado: {marcados} mensaje{'s' if marcados != 1 else ''} "
           "fuera del panel y de las estadísticas (nada se borró de la base).",
    )


@router.post("/unmatched/synonyms")
def unmatched_add_synonym(
    request: Request, service_id: str = Form(...), synonym: str = Form(...)
):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    error = catalog.add_synonym(service_id, synonym, admin=admin)
    if error:
        return _render(request, admin, error=error)
    log_admin(admin, "catalogo:sinonimo", f"{service_id}:{synonym.strip()}")
    logger.info("Sinónimo '%s' agregado a %s por %s", synonym.strip(), service_id, admin)
    return _render(request, admin, ok=f"Sinónimo '{synonym.strip()}' agregado.")


@router.get("/export/no-entendidos.csv")
def export_unmatched(request: Request):
    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        pending = unmatched_messages(session)
    stamp = utcnow().strftime("%Y%m%d")
    return _csv_response(
        f"no-entendidos-{stamp}.csv",
        ["fecha", "telefono", "mensaje"],
        (
            [m.created_at.isoformat(sep=" ", timespec="seconds"), m.phone, m.body]
            for m in pending
        ),
    )
