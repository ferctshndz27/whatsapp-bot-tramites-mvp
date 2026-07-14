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


def test_login_lockout_after_max_attempts():
    username = "test-lockout-user"
    _login_attempts.pop(username, None)

    for _ in range(LOGIN_MAX_ATTEMPTS):
        assert _is_login_locked(username) is False
        _record_failed_login(username)

    assert _is_login_locked(username) is True
