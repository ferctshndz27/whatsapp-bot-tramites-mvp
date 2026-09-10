import io
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from sqlmodel import select

import app.db as db
from app.media_store import save_upload
from app.models import ChatMember, ChatRoom, MessageReminder, TeamMessage
from app.team_chat import (
    active_reminders, team_chat, team_delete_message, team_remind, team_send, team_send_media,
)


def _request(user: str) -> SimpleNamespace:
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path="/team"), headers={})


def _upload(filename: str, content_type: str, data: bytes) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers={"content-type": content_type},
    )


def test_send_text_message(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    
    # Init general room
    team_chat(_request("ana"))
    room = session.query(ChatRoom).first()

    team_send(_request("ana"), room_id=room.id, body="hola equipo")

    msg = session.query(TeamMessage).one()
    assert msg.author == "ana"
    assert msg.body == "hola equipo"
    assert msg.room_id == room.id


def test_empty_message_ignored(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    team_chat(_request("ana"))
    room = session.query(ChatRoom).first()

    team_send(_request("ana"), room_id=room.id, body="   ")

    assert session.query(TeamMessage).count() == 0


def test_send_media_message(monkeypatch, session, tmp_path):
    monkeypatch.setattr(db, "engine", session.get_bind())
    from app.config import settings

    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    team_chat(_request("bob"))
    room = session.query(ChatRoom).first()

    team_send_media(
        _request("bob"), room_id=room.id, file=_upload("nota.pdf", "application/pdf", b"%PDF-1.4 test"), caption=""
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
    team_chat(_request("ana"))
    room = session.query(ChatRoom).first()

    team_send(_request("ana"), room_id=room.id, body="mensaje a borrar")
    msg = session.query(TeamMessage).one()

    team_delete_message(_request("ana"), msg.id)

    session.refresh(msg)
    assert msg.habilitado is False


def test_cannot_delete_other_users_message(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    team_chat(_request("ana"))
    room = session.query(ChatRoom).first()

    team_send(_request("ana"), room_id=room.id, body="mensaje de ana")
    msg = session.query(TeamMessage).one()

    team_delete_message(_request("bob"), msg.id)

    session.refresh(msg)
    assert msg.habilitado is True


# --- Recordatorios: devuelven el texto del mensaje, así que son una lectura ---

def _dm_de_ana_y_carla(session) -> TeamMessage:
    room = ChatRoom(room_type="direct")
    session.add(room)
    session.commit()
    session.add_all([ChatMember(room_id=room.id, username="ana"),
                     ChatMember(room_id=room.id, username="carla")])
    msg = TeamMessage(room_id=room.id, author="carla", body="mi CURP es secreta")
    session.add(msg)
    session.commit()
    return msg


def _recordatorios_de(session, quien: str) -> list[MessageReminder]:
    session.expire_all()
    return session.exec(select(MessageReminder).where(MessageReminder.username == quien)).all()


def test_no_se_pone_recordatorio_sobre_un_mensaje_de_sala_ajena(monkeypatch, session):
    """El recordatorio te devuelve el texto: ponerlo sobre el id de un DM ajeno
    era leer ese DM. Bob no está en la sala de ana y carla."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    msg = _dm_de_ana_y_carla(session)

    resp = team_remind(_request("bob"), msg.id, remind_in_minutes=5)

    assert resp.status_code == 404
    assert _recordatorios_de(session, "bob") == []


def test_un_id_que_no_existe_responde_igual_que_uno_ajeno(monkeypatch, session):
    """Mismo 404 en los dos casos: si difirieran, se podría sondear qué ids existen."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    assert team_remind(_request("bob"), 999, remind_in_minutes=5).status_code == 404


@pytest.mark.parametrize("minutos", [0, -60, 7 * 24 * 60 + 1])
def test_minutos_fuera_de_rango_no_crean_recordatorio(monkeypatch, session, minutos):
    """Cero o negativo vence al instante — era la vía rápida de la fuga."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    msg = _dm_de_ana_y_carla(session)

    assert team_remind(_request("ana"), msg.id, remind_in_minutes=minutos).status_code == 422
    assert _recordatorios_de(session, "ana") == []


def test_un_miembro_si_pone_su_recordatorio_y_le_llega(monkeypatch, session):
    """Control positivo: un candado que lo rechazara todo pasaría en verde los
    de arriba y nadie podría usar la función."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    msg = _dm_de_ana_y_carla(session)

    assert team_remind(_request("ana"), msg.id, remind_in_minutes=1).status_code == 200
    (rem,) = _recordatorios_de(session, "ana")
    rem.remind_at = datetime.utcnow() - timedelta(minutes=1)
    session.add(rem)
    session.commit()

    entregados = json.loads(active_reminders(_request("ana")).body)
    assert [r["body"] for r in entregados] == ["mi CURP es secreta"]


@pytest.mark.parametrize("caso", ["nunca_fue_miembro", "lo_sacaron", "mensaje_oculto"])
def test_al_entregar_se_vuelve_a_comprobar_el_acceso(monkeypatch, session, caso):
    """Un recordatorio que ya está en la base —creado antes del candado, o de
    una sala de la que lo sacaron, o sobre un mensaje que su autor ocultó— no
    entrega el texto."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    msg = _dm_de_ana_y_carla(session)
    quien = "bob" if caso == "nunca_fue_miembro" else "ana"
    if caso == "lo_sacaron":
        miembro = session.exec(select(ChatMember).where(ChatMember.username == "ana")).one()
        miembro.habilitado = False
        session.add(miembro)
    if caso == "mensaje_oculto":
        msg.habilitado = False
        session.add(msg)
    session.add(MessageReminder(message_id=msg.id, username=quien,
                                remind_at=datetime.utcnow() - timedelta(minutes=1)))
    session.commit()

    assert json.loads(active_reminders(_request(quien)).body) == []
