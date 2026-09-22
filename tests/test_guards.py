from sqlmodel import select

from app.conversation import RATE_LIMIT_MAX_MESSAGES, handle_incoming
from app.dashboard import LOGIN_MAX_ATTEMPTS, _is_login_locked, _login_attempts, _record_failed_login
from app.models import Direction, Message

PHONE = "5215500009999"


def test_rate_limit_drops_messages_over_threshold(session):
    for _ in range(RATE_LIMIT_MAX_MESSAGES):
        session.add(Message(phone=PHONE, direction=Direction.IN, body="x"))
    session.commit()

    replies = handle_incoming(session, PHONE, "Hola")
    assert replies == []


def test_rate_limit_allows_messages_under_threshold(session):
    replies = handle_incoming(session, PHONE, "Hola")
    assert replies != []


def test_con_rate_limit_no_se_derivan_datos_del_cliente(session, monkeypatch):
    """EXT-4 (auditoría 2026-09): `_extract_client_data` corría ANTES del
    chequeo y 60 entrantes en un minuto dejaban 123 ClientNote. El entrante sí
    se registra (es la base del contador y de la idempotencia); lo demás, no."""
    from app import conversation

    derivados: list[str] = []
    monkeypatch.setattr(conversation, "_extract_client_data", lambda s, p, t: derivados.append(t))
    for _ in range(RATE_LIMIT_MAX_MESSAGES):
        session.add(Message(phone=PHONE, direction=Direction.IN, body="x"))
    session.commit()
    antes = len(session.exec(select(Message).where(Message.phone == PHONE)).all())

    assert handle_incoming(session, PHONE, "me llamo Ana, mi correo es ana@x.mx") == []

    assert derivados == [], "con el rate limit activo se siguieron derivando datos del cliente"
    assert len(session.exec(select(Message).where(Message.phone == PHONE)).all()) == antes + 1, (
        "el entrante dejó de registrarse: el contador y la idempotencia dependen de él"
    )


def test_login_lockout_after_max_attempts():
    """La API del lockout cambió con AUTH-2 (auditoría 2026-09): la llave es
    (usuario, IP). Lo que se afirma es lo mismo: al quinto fallo, bloqueado."""
    username, ip = "test-lockout-user", "203.0.113.9"
    _login_attempts.clear()

    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert _is_login_locked(username, ip) is False
        _record_failed_login(username, ip)

    assert _is_login_locked(username, ip) is True
    _login_attempts.clear()
