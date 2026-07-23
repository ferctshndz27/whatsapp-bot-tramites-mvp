"""Fase 0 + 1 del plan de mejora del agente.

Fase 0 — reporte de mensajes NO entendidos: se derivan de la tabla message
existente (el mensaje IN inmediatamente anterior a cada respuesta OUT que
empieza con "No reconocí…"). Cero migración: ninguna tabla nueva.

Fase 1 — minería de sinónimos: con ANTHROPIC_API_KEY, Claude analiza esos
mensajes por lote y propone sinónimos para el catálogo; el admin aprueba
uno por uno (nada se agrega solo). Sin API key la vista sigue sirviendo:
lista los mensajes y permite agregar sinónimos a mano.
"""

import json
import logging
from datetime import timedelta

from fastapi import APIRouter, Form, Request
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app import nlu
from app.audit import log_admin
from app.auth import is_admin
from app.catalog import catalog
from app.config import settings
from app.conversation import NOT_RECOGNIZED_HINT
from app.db import get_session
from app.models import Direction, Message, utcnow
from app.system import _admin_or_redirect, _csv_response

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["is_admin"] = is_admin

DAYS_BACK = 30
MAX_MESSAGES = 100

_HINT_PREFIX = NOT_RECOGNIZED_HINT.split("\n")[0]  # "No reconocí esa opción."

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
        .where(Message.body.like(f"{_HINT_PREFIX}%"))  # type: ignore[attr-defined]
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
    """Pide a Claude propuestas de sinónimos y las valida contra el catálogo."""
    if not texts:
        return []
    listing = "\n".join(
        f"- {s.id}: {s.name}" for c in catalog.categories for s in c.services
    )
    body = "\n".join(f"{i}. {t[:200]}" for i, t in enumerate(texts[:MAX_MESSAGES], start=1))

    response = nlu._get_client().messages.create(
        model=settings.nlu_model,
        max_tokens=2048,
        system=[
            {
                "type": "text",
                "text": _MINE_SYSTEM.format(listing=listing),
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


def _render(request: Request, admin: str, proposals=None, error=None, ok=None):
    with get_session() as session:
        pending = unmatched_messages(session)
    services = [(s.id, s.name) for c in catalog.categories for s in c.services]
    return templates.TemplateResponse(
        "unmatched.html",
        {
            "request": request,
            "user": admin,
            "pending": pending,
            "services": services,
            "proposals": proposals or [],
            "mining_available": bool(settings.anthropic_api_key),
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
    log_admin(admin, "catalogo:mineria", f"{len(texts)} mensajes → {len(proposals)} propuestas")
    if not proposals:
        return _render(request, admin, ok="Claude no encontró sinónimos nuevos que proponer.")
    return _render(request, admin, proposals=proposals)


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
