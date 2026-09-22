import time
from types import SimpleNamespace

import bcrypt
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

import app.authlog as authlog
import app.dashboard as dashboard
import app.db as db
from app.config import settings
from app.models import IpGeoCache, LoginEvent, User, WebSession

UA_WIN_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
UA_ANDROID_FIREFOX = "Mozilla/5.0 (Android 14; Mobile; rv:127.0) Gecko/127.0 Firefox/127.0"
UA_IPHONE_SAFARI = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)


def _request(user=None, sid=None, host="192.168.1.50", ua=UA_WIN_CHROME, headers=None):
    sess = {}
    if user:
        sess["user"] = user
    if sid:
        sess["sid"] = sid
    return SimpleNamespace(
        session=sess,
        url=SimpleNamespace(path="/logins"),
        query_params={},
        headers={**({"user-agent": ua} if ua else {}), **(headers or {})},
        client=SimpleNamespace(host=host) if host else None,
    )


def _make_user(session, username="maria", password="clave-1234", habilitado=True, is_admin=False):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    session.add(User(username=username, password_hash=hashed, habilitado=habilitado, is_admin=is_admin))
    session.commit()


@pytest.fixture(autouse=True)
def _clean_state():
    dashboard._login_attempts.clear()
    authlog._last_touch.clear()
    authlog._resolving.clear()
    yield
    dashboard._login_attempts.clear()
    authlog._last_touch.clear()
    authlog._resolving.clear()


# ---- IP y dispositivo --------------------------------------------------------


def test_client_ip_takes_last_forwarded_hop():
    # El último salto lo agrega nuestro nginx; los anteriores son spoofeables.
    req = _request(headers={"x-forwarded-for": "1.2.3.4, 187.190.1.1"})
    assert authlog.client_ip(req) == "187.190.1.1"


def test_client_ip_falls_back_to_socket_peer():
    assert authlog.client_ip(_request(host="10.1.2.3")) == "10.1.2.3"
    assert authlog.client_ip(_request(host=None)) is None


def test_parse_device_variants():
    assert authlog.parse_device(UA_WIN_CHROME) == "Chrome · Windows"
    assert authlog.parse_device(UA_ANDROID_FIREFOX) == "Firefox · Android"
    assert authlog.parse_device(UA_IPHONE_SAFARI) == "Safari · iPhone"
    assert authlog.parse_device(None) is None
    assert authlog.parse_device("curl/8.0") == "otro"


# ---- Registro de logins ------------------------------------------------------


def test_login_success_records_event_and_session(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session)
    req = _request()

    resp = dashboard.login_submit(req, username="maria", password="clave-1234")

    assert resp.status_code == 303
    assert req.session["user"] == "maria"
    assert req.session["sid"]
    event = session.exec(select(LoginEvent)).one()
    assert event.success is True and event.reason is None
    assert event.ip == "192.168.1.50"
    assert event.geo == authlog.LOCAL_NET_LABEL  # IP privada: sin lookup externo
    assert event.device == "Chrome · Windows"
    ws = session.exec(select(WebSession)).one()
    assert ws.username == "maria" and ws.token == req.session["sid"]
    assert ws.ended_at is None


@pytest.mark.parametrize(
    "username,password,expected_reason",
    [
        ("nadie", "loquesea", "usuario no existe"),
        ("maria", "incorrecta", "contraseña incorrecta"),
        ("baja", "clave-1234", "usuario deshabilitado"),
    ],
)
def test_login_failure_reasons(monkeypatch, session, username, password, expected_reason):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "maria")
    _make_user(session, "baja", habilitado=False)
    req = _request()

    dashboard.login_submit(req, username=username, password=password)

    event = session.exec(select(LoginEvent)).one()
    assert event.success is False
    assert event.reason == expected_reason
    assert "user" not in req.session and "sid" not in req.session


def test_login_lockout_records_reason(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session)
    # La llave del lockout es (usuario, IP) desde AUTH-2; la IP es la del fake de arriba.
    dashboard._login_attempts[dashboard._llaves("maria", "192.168.1.50")[0]] = [time.time()] * dashboard.LOGIN_MAX_ATTEMPTS
    req = _request()

    dashboard.login_submit(req, username="maria", password="clave-1234")

    event = session.exec(select(LoginEvent)).one()
    assert event.success is False and event.reason == "bloqueado temporalmente"
    assert "user" not in req.session


def test_logout_closes_web_session(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    token = authlog.open_session(_request(user="maria"), "maria")

    dashboard.logout(_request(user="maria", sid=token))

    ws = session.exec(select(WebSession)).one()
    assert ws.ended_at is not None


# ---- Actividad de sesión (usuarios conectados) --------------------------------


def test_touch_creates_session_for_legacy_cookie(monkeypatch, session):
    """Sesiones iniciadas antes de este módulo (cookie sin sid) se registran solas."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    req = _request(user="maria")

    assert authlog.should_touch(req) is True
    authlog.touch_session(req)

    assert req.session["sid"]
    ws = session.exec(select(WebSession)).one()
    assert ws.username == "maria"


def test_touch_updates_last_seen_and_throttles(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    token = authlog.open_session(_request(user="maria"), "maria")
    ws = session.exec(select(WebSession)).one()
    old_seen = ws.last_seen_at
    time.sleep(0.01)
    req = _request(user="maria", sid=token)

    authlog.touch_session(req)

    session.expire_all()
    ws = session.exec(select(WebSession)).one()
    assert ws.last_seen_at > old_seen
    # recién tocada: el throttle evita otra escritura inmediata
    assert authlog.should_touch(req) is False


def test_active_sessions_excludes_idle_and_ended(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    from app.models import utcnow

    now = utcnow()
    session.add(WebSession(token="a", username="activa", last_seen_at=now))
    session.add(
        WebSession(token="b", username="dormida",
                   last_seen_at=now - authlog.ACTIVE_WINDOW * 3)
    )
    session.add(WebSession(token="c", username="salio", last_seen_at=now, ended_at=now))
    session.commit()

    active = authlog.active_sessions(now)

    assert [a["s"].username for a in active] == ["activa"]


# ---- Geolocalización ---------------------------------------------------------


def test_geo_hint_uses_fresh_cache_without_lookup(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(IpGeoCache(ip="8.8.8.8", geo="Mountain View, California, Estados Unidos"))
    session.commit()
    monkeypatch.setattr(authlog, "_start_resolve", lambda ip: pytest.fail("no debía resolver"))

    assert authlog.geo_hint("8.8.8.8") == "Mountain View, California, Estados Unidos"


def test_geo_hint_schedules_resolution_on_cache_miss(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "geoip_enabled", True)
    scheduled = []
    monkeypatch.setattr(authlog, "_start_resolve", scheduled.append)

    assert authlog.geo_hint("8.8.4.4") is None
    assert scheduled == ["8.8.4.4"]


def test_resolve_and_apply_backfills_rows_and_cache(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(LoginEvent(username="maria", success=True, ip="8.8.4.4"))
    session.add(WebSession(token="t1", username="maria", ip="8.8.4.4"))
    session.commit()
    monkeypatch.setattr(authlog, "_fetch_geo", lambda ip: "Zapopan, Jalisco, México")

    authlog._resolve_and_apply("8.8.4.4")

    session.expire_all()
    assert session.exec(select(LoginEvent)).one().geo == "Zapopan, Jalisco, México"
    assert session.exec(select(WebSession)).one().geo == "Zapopan, Jalisco, México"
    assert session.get(IpGeoCache, "8.8.4.4").geo == "Zapopan, Jalisco, México"


# ---- Vistas admin ------------------------------------------------------------


def test_logins_view_redirects_non_admin(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "colega")

    resp = authlog.logins_view(_request(user="colega"))

    assert resp.status_code == 303 and resp.headers["location"] == "/"


def test_logins_view_renders_for_admin(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)
    session.add(LoginEvent(username="maria", success=False, reason="contraseña incorrecta",
                           ip="192.168.1.9", device="Chrome · Windows"))
    session.add(WebSession(token="t1", username="maria", device="Chrome · Windows"))
    session.commit()

    resp = authlog.logins_view(_request(user="jefa"), result="fail")

    assert resp.status_code == 200
    body = resp.body.decode()
    assert "contraseña incorrecta" in body
    assert "maria" in body
    assert resp.context["stats"]["hoy_fail"] == 1
    assert resp.context["stats"]["activos"] == 1


def test_export_logins_csv(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _make_user(session, "jefa", is_admin=True)
    session.add(LoginEvent(username="maria", success=True, ip="192.168.1.9"))
    session.commit()

    resp = authlog.export_logins(_request(user="jefa"))

    assert resp.media_type.startswith("text/csv")
    assert "maria" in resp.body.decode("utf-8")


# ---- Integración: orden de middlewares ----------------------------------------


def test_middleware_runs_inside_session_middleware(tmp_path, monkeypatch):
    """Reproduce el orden de registro de app.main: el middleware de actividad
    debe poder leer request.session y el sid debe persistir en la cookie
    (una segunda petición no duplica la websession)."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from starlette.middleware.sessions import SessionMiddleware

    engine = create_engine(
        f"sqlite:///{tmp_path / 'middleware.db'}", connect_args={"check_same_thread": False}
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    # Desde AUTH-1 (auditoría 2026-09) el mismo middleware es también el guardia
    # de sesión, y la sesión de un usuario que no existe en la base se vacía. Sin
    # esta fila el guardia sacaría a «ana» en el segundo request, el handler la
    # volvería a poner y nacería una segunda websession: la prueba mediría el
    # guardia, no que el sid persista. Lo que afirma no cambió; su siembra sí.
    with Session(engine) as s:
        s.add(User(username="ana", password_hash="x"))
        s.commit()

    api = FastAPI()
    api.middleware("http")(authlog.session_activity_middleware)
    api.add_middleware(SessionMiddleware, secret_key="test")

    @api.get("/whoami")
    def whoami(request: Request):
        request.session["user"] = "ana"
        return {"ok": True}

    client = TestClient(api)
    assert client.get("/whoami").status_code == 200
    with Session(engine) as s:
        rows = s.exec(select(WebSession)).all()
        assert len(rows) == 1 and rows[0].username == "ana"

    assert client.get("/whoami").status_code == 200
    with Session(engine) as s:
        assert len(s.exec(select(WebSession)).all()) == 1
