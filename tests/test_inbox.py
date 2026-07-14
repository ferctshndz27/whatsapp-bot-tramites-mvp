from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db as db
from app import whatsapp_client
from app.inbox import build_conversation_items, inbox_bot_on, inbox_send
from app.models import Conversation, Direction, Message


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user})


def _msg(phone: str, direction: Direction, body: str, hours_ago: float = 0) -> Message:
    return Message(
        phone=phone,
        direction=direction,
        body=body,
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


def test_items_flag_waiting_and_expired_window(session):
    session.add(_msg("111", Direction.IN, "hola", hours_ago=30))
    session.add(_msg("222", Direction.IN, "buenas", hours_ago=1))
    session.add(_msg("222", Direction.OUT, "hola, ¿en qué ayudo?", hours_ago=0.5))
    session.add(Conversation(phone="111", state="escalado"))
    session.add(Conversation(phone="222", state="inicio"))
    session.commit()

    items = {i["phone"]: i for i in build_conversation_items(session)}

    assert items["111"]["waiting"] is True
    assert items["111"]["window_open"] is False
    assert items["111"]["bot_paused"] is True
    assert items["222"]["waiting"] is False
    assert items["222"]["window_open"] is True
    assert items["222"]["bot_paused"] is False


def test_send_logs_message_and_pauses_bot(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(whatsapp_client, "send_text", lambda phone, body: "wamid.SENT1")
    session.add(_msg("333", Direction.IN, "hola", hours_ago=1))
    session.add(Conversation(phone="333", state="inicio"))
    session.commit()

    inbox_send(_request("ana"), "333", body="¿En qué puedo ayudarte?")

    sent = session.query(Message).filter(
        Message.phone == "333", Message.direction == Direction.OUT
    ).one()
    assert sent.wa_message_id == "wamid.SENT1"
    assert sent.sent_by == "ana"
    conv = session.get(Conversation, "333")
    session.refresh(conv)
    assert conv.state == "escalado"


def test_send_blocked_when_window_expired(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    called = []
    monkeypatch.setattr(whatsapp_client, "send_text", lambda *a: called.append(a))
    session.add(_msg("444", Direction.IN, "hola", hours_ago=30))
    session.commit()

    response = inbox_send(_request("ana"), "444", body="tarde")

    assert called == []
    assert "err=window" in response.headers["location"]
    out = session.query(Message).filter(
        Message.phone == "444", Message.direction == Direction.OUT
    ).all()
    assert out == []


def test_bot_reactivation(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(Conversation(phone="555", state="escalado"))
    session.commit()

    inbox_bot_on(_request("ana"), "555")

    conv = session.get(Conversation, "555")
    session.refresh(conv)
    assert conv.state == "inicio"
