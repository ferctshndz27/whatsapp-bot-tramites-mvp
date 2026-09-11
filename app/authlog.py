"""Bitácora de accesos al panel: quién entra, desde dónde y qué falló.

Registra cada intento de login (éxito o fallo con su motivo) en loginevent
y mantiene una sesión por dispositivo (websession) para mostrar en /logins
quién está conectado ahora, con IP, navegador y ubicación. La geolocalización
se resuelve best-effort contra ipwho.is en un hilo aparte con cache por IP
(ipgeocache) — el login nunca espera a la red. LoginEvent y WebSession se
espejan a MSSQL vía app.archive; el cache de geo no (es derivable).
"""

import ipaddress
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Request
from app.templating import templates
from sqlmodel import select
from starlette.concurrency import run_in_threadpool

from app.auth import is_admin
from app.config import settings
from app.db import get_session
from app.models import IpGeoCache, LoginEvent, WebSession, utcnow

logger = logging.getLogger(__name__)

router = APIRouter()

# Ventana para considerar una sesión "conectada ahora"
ACTIVE_WINDOW = timedelta(minutes=5)
# last_seen se escribe a lo más una vez por minuto por sesión
TOUCH_INTERVAL_SECONDS = 60

GEO_URL = "https://ipwho.is/{ip}?lang=es"
GEO_TIMEOUT_SECONDS = 5
GEO_TTL_OK = timedelta(days=30)
GEO_TTL_FAIL = timedelta(days=1)
LOCAL_NET_LABEL = "Red local"

USERNAME_MAX_LEN = 64
UA_MAX_LEN = 300

REASON_NO_USER = "usuario no existe"
REASON_BAD_PASSWORD = "contraseña incorrecta"
REASON_DISABLED = "usuario deshabilitado"
REASON_LOCKED = "bloqueado temporalmente"

# El orden importa: Edge/Opera incluyen "Chrome", Chrome incluye "Safari".
_BROWSERS = [
    ("Edg/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("Firefox/", "Firefox"),
    ("Chrome/", "Chrome"),
    ("Safari/", "Safari"),
]
# Android incluye "Linux", por eso va antes.
_SYSTEMS = [
    ("Windows", "Windows"),
    ("Android", "Android"),
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Mac OS X", "macOS"),
    ("Linux", "Linux"),
]

# ponytail: estado en memoria por proceso (como el lockout de dashboard) —
# throttle de last_seen y dedupe de lookups de geo en vuelo. Pasar a
# DB/Redis si algún día se corre con más de un worker.
_last_touch: dict[str, float] = {}
_resolving: set[str] = set()
_resolving_lock = threading.Lock()


def _as_utc(dt: datetime) -> datetime:
    # SQLite devuelve datetimes naive; en la app siempre son UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def client_ip(request) -> str | None:
    """IP real del cliente detrás de nginx.

    Se toma el ÚLTIMO salto de X-Forwarded-For: es el que agregó nuestro
    proxy; los anteriores los puede inventar el cliente (spoofing)."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip() or None
    client = getattr(request, "client", None)
    return client.host if client else None


def parse_device(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    browser = next((name for token, name in _BROWSERS if token in user_agent), None)
    system = next((name for token, name in _SYSTEMS if token in user_agent), None)
    if browser and system:
        return f"{browser} · {system}"
    return browser or system or "otro"


# ---- Geolocalización best-effort -------------------------------------------


def _is_public_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_global
    except ValueError:
        return False


def _cached_geo(ip: str) -> tuple[str | None, bool]:
    """(geo, vigente). vigente=False → hay que (re)resolver."""
    with get_session() as session:
        row = session.get(IpGeoCache, ip)
    if row is None:
        return None, False
    ttl = GEO_TTL_OK if row.geo else GEO_TTL_FAIL
    return row.geo, utcnow() - _as_utc(row.resolved_at) < ttl


def geo_hint(ip: str | None) -> str | None:
    """Geo inmediata si se conoce; si no, dispara la resolución en segundo
    plano y devuelve lo que haya (las filas se completan al terminar)."""
    if not ip:
        return None
    if not _is_public_ip(ip):
        return LOCAL_NET_LABEL
    geo, fresh = _cached_geo(ip)
    if not fresh and settings.geoip_enabled:
        _start_resolve(ip)
    return geo


def _start_resolve(ip: str) -> None:
    with _resolving_lock:
        if ip in _resolving:
            return
        _resolving.add(ip)
    threading.Thread(target=_resolve_and_apply, args=(ip,), daemon=True).start()


def _fetch_geo(ip: str) -> str | None:
    resp = httpx.get(GEO_URL.format(ip=ip), timeout=GEO_TIMEOUT_SECONDS)
    data = resp.json()
    if not data.get("success"):
        return None
    parts = [data.get(k) for k in ("city", "region", "country")]
    return ", ".join(p for p in parts if p) or None


def _resolve_and_apply(ip: str) -> None:
    try:
        geo = _fetch_geo(ip)
        with get_session() as session:
            row = session.get(IpGeoCache, ip) or IpGeoCache(ip=ip)
            row.geo = geo or row.geo  # un lookup fallido no borra el valor previo
            row.resolved_at = utcnow()
            session.add(row)
            session.commit()
        if geo:
            _apply_geo(ip, geo)
    except Exception:
        logger.warning("No se pudo geolocalizar la IP %s", ip, exc_info=True)
    finally:
        with _resolving_lock:
            _resolving.discard(ip)


def _apply_geo(ip: str, geo: str) -> None:
    """Completa la geo en filas ya escritas con esa IP. Vía ORM a propósito:
    así el listener de app.archive también actualiza el espejo MSSQL."""
    with get_session() as session:
        for model in (LoginEvent, WebSession):
            for row in session.exec(
                select(model).where(model.ip == ip, model.geo.is_(None))
            ).all():
                row.geo = geo
                session.add(row)
        session.commit()


# ---- Registro de intentos y sesiones ----------------------------------------


def record_login(request, username: str, success: bool, reason: str | None = None) -> None:
    ip = client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:UA_MAX_LEN] or None
    with get_session() as session:
        session.add(
            LoginEvent(
                username=(username or "")[:USERNAME_MAX_LEN],
                success=success,
                reason=reason,
                ip=ip,
                geo=geo_hint(ip),
                device=parse_device(ua),
                user_agent=ua,
            )
        )
        session.commit()


def open_session(request, username: str) -> str:
    """Crea la websession del dispositivo y devuelve el token para la cookie."""
    ip = client_ip(request)
    ua = (request.headers.get("user-agent") or "")[:UA_MAX_LEN] or None
    token = secrets.token_urlsafe(16)
    with get_session() as session:
        session.add(
            WebSession(
                token=token,
                username=username,
                ip=ip,
                geo=geo_hint(ip),
                device=parse_device(ua),
                user_agent=ua,
            )
        )
        session.commit()
    return token


def close_session(request) -> None:
    token = request.session.get("sid")
    if not token:
        return
    with get_session() as session:
        ws = session.exec(select(WebSession).where(WebSession.token == token)).first()
        if ws is not None and ws.ended_at is None:
            ws.ended_at = utcnow()
            session.add(ws)
            session.commit()
    _last_touch.pop(token, None)


def should_touch(request) -> bool:
    """Chequeo barato (sin I/O) para decidir si toca escribir last_seen."""
    user = request.session.get("user")
    if not user:
        return False
    sid = request.session.get("sid")
    if not sid:
        return True  # sesión iniciada antes de este módulo: hay que registrarla
    return time.monotonic() - _last_touch.get(sid, 0.0) >= TOUCH_INTERVAL_SECONDS


def touch_session(request) -> None:
    user = request.session.get("user")
    if not user:
        return
    sid = request.session.get("sid")
    if not sid:
        request.session["sid"] = open_session(request, user)
        _last_touch[request.session["sid"]] = time.monotonic()
        return
    with get_session() as session:
        ws = session.exec(select(WebSession).where(WebSession.token == sid)).first()
        if ws is None:
            # La fila no existe (p.ej. base restaurada de un respaldo): se recrea.
            ua = (request.headers.get("user-agent") or "")[:UA_MAX_LEN] or None
            ip = client_ip(request)
            ws = WebSession(
                token=sid, username=user, ip=ip, geo=geo_hint(ip),
                device=parse_device(ua), user_agent=ua,
            )
        ws.last_seen_at = utcnow()
        ws.ended_at = None  # si la cookie sigue viva, la sesión está activa
        session.add(ws)
        session.commit()
    _last_touch[sid] = time.monotonic()


async def session_activity_middleware(request: Request, call_next):
    """Mantiene last_seen de la websession en cada request autenticado.

    Debe quedar POR DENTRO de SessionMiddleware (necesita request.session);
    en main.py se registra ANTES porque en Starlette el último middleware
    agregado es el más externo."""
    response = await call_next(request)
    if "session" in request.scope and should_touch(request):
        try:
            await run_in_threadpool(touch_session, request)
        except Exception:
            logger.warning("No se pudo actualizar la actividad de sesión", exc_info=True)
    return response


# ---- Vistas de administración ------------------------------------------------


def active_sessions(now: datetime | None = None) -> list[dict]:
    """Sesiones con actividad dentro de ACTIVE_WINDOW, con minutos de inactividad."""
    now = now or utcnow()
    with get_session() as session:
        rows = session.exec(
            select(WebSession)
            .where(WebSession.ended_at.is_(None))
            .order_by(WebSession.last_seen_at.desc())
        ).all()
    active = []
    for ws in rows:
        idle = now - _as_utc(ws.last_seen_at)
        if idle <= ACTIVE_WINDOW:
            active.append({"s": ws, "mins": max(0, int(idle.total_seconds() // 60))})
    return active


@router.get("/logins")
def logins_view(request: Request, result: str | None = None):
    from app.system import _admin_or_redirect

    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect

    now = utcnow()
    with get_session() as session:
        # ponytail: stats calculadas en Python sobre los últimos 1000 intentos —
        # a este volumen sobra; pasar a agregados SQL si crece a decenas de miles.
        events = session.exec(
            select(LoginEvent).order_by(LoginEvent.created_at.desc()).limit(1000)
        ).all()
    active = active_sessions(now)

    today = now.date()
    stats = {
        "activos": len({a["s"].username for a in active}),
        "hoy_ok": sum(1 for e in events if e.success and _as_utc(e.created_at).date() == today),
        "hoy_fail": sum(
            1 for e in events if not e.success and _as_utc(e.created_at).date() == today
        ),
        "fail_7d": sum(
            1 for e in events
            if not e.success and now - _as_utc(e.created_at) <= timedelta(days=7)
        ),
    }

    shown = events
    if result == "ok":
        shown = [e for e in shown if e.success]
    elif result == "fail":
        shown = [e for e in shown if not e.success]

    return templates.TemplateResponse(
        request, "logins.html",
        {
            "request": request,
            "user": admin,
            "stats": stats,
            "active": active,
            "entries": shown[:200],
            "result": result,
            "window_min": int(ACTIVE_WINDOW.total_seconds() // 60),
        },
    )


@router.get("/export/logins.csv")
def export_logins(request: Request):
    from app.system import _admin_or_redirect, _csv_response

    admin, redirect = _admin_or_redirect(request)
    if redirect:
        return redirect
    with get_session() as session:
        events = session.exec(select(LoginEvent).order_by(LoginEvent.created_at)).all()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_response(
        f"accesos-{stamp}.csv",
        ["id", "fecha_utc", "usuario", "resultado", "motivo", "ip", "ubicacion",
         "dispositivo", "user_agent"],
        (
            [e.id, e.created_at.isoformat(sep=" ", timespec="seconds"), e.username,
             "exito" if e.success else "fallo", e.reason or "", e.ip or "",
             e.geo or "", e.device or "", e.user_agent or ""]
            for e in events
        ),
    )
