import io
from types import SimpleNamespace

from fastapi import UploadFile

import app.db as db
from app.media_store import save_upload
from app.models import TeamMessage
from app.team_chat import team_chat, team_delete_message, team_send, team_send_media


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path="/team"))


def _upload(filename: str, content_type: str, data: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers={"content-type": content_type},
    )


def test_send_text_message(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    team_send(_request("ana"), body="hola equipo")

    msg = session.query(TeamMessage).one()
    assert msg.author == "ana"
    assert msg.body == "hola equipo"


def test_empty_message_ignored(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())

    team_send(_request("ana"), body="   ")

    assert session.query(TeamMessage).count() == 0


def test_send_media_message(monkeypatch, session, tmp_path):
    monkeypatch.setattr(db, "engine", session.get_bind())
    from app.config import settings

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))

    team_send_media(
        _request("bob"), file=_upload("nota.pdf", "application/pdf", b"%PDF-1.4 test"), caption=""
    )

    msg = session.query(TeamMessage).one()
    assert msg.author == "bob"
    assert msg.body == "[archivo: nota.pdf]"
    assert msg.media_path.startswith("/media/") and msg.media_path.endswith(".pdf")


def test_media_type_rejected(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))

    web, disk, err = save_upload(_upload("x.exe", "application/x-msdownload", b"MZ"))
    assert (web, disk) == (None, None)
    assert err == "tipo"


def test_media_too_big_rejected(monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))

    big = b"x" * (5 * 1024 * 1024 + 1)
    web, disk, err = save_upload(_upload("foto.jpg", "image/jpeg", big))
    assert err == "peso"


def test_own_message_can_be_soft_deleted(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    team_send(_request("ana"), body="mensaje a borrar")
    msg = session.query(TeamMessage).one()

    team_delete_message(_request("ana"), msg.id)

    session.refresh(msg)
    assert msg.habilitado is False


def test_cannot_delete_other_users_message(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    team_send(_request("ana"), body="mensaje de ana")
    msg = session.query(TeamMessage).one()

    team_delete_message(_request("bob"), msg.id)

    session.refresh(msg)
    assert msg.habilitado is True


def test_soft_deleted_message_hidden_from_chat(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    team_send(_request("ana"), body="visible")
    team_send(_request("ana"), body="se va a ocultar")
    hidden = session.query(TeamMessage).filter(TeamMessage.body == "se va a ocultar").one()

    team_delete_message(_request("ana"), hidden.id)

    response = team_chat(_request("ana"))
    bodies = [m.body for m in response.context["messages"]]
    assert "visible" in bodies
    assert "se va a ocultar" not in bodies
