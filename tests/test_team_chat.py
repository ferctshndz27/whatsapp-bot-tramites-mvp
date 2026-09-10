import io
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import bcrypt
import pathlib
import re
from contextlib import contextmanager

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.archive as archive
import app.db as db
from app.config import settings
from app.main import app
from app.media_store import save_upload
from app.models import ChatMember, ChatRoom, MessageReaction, MessageReminder, TeamMessage, User
from app.team_chat import (
    active_reminders, create_room, team_chat, team_delete_message, team_remind,
    team_room_messages, team_send, team_send_media,
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


def _colaboradores(session, *nombres):
    for n in nombres:
        session.add(User(username=n, password_hash="x", office_id=1))
    session.commit()


def _pedir_dm(de: str, con: str):
    return create_room(_request(de), room_type="group", name="", members=[con])


def _sala_de_la_url(respuesta) -> int:
    ubicacion = respuesta.headers["location"]
    assert ubicacion.startswith("/team?room="), f"no lleva a una sala: {ubicacion}"
    return int(ubicacion.split("=", 1)[1])


def test_pedir_un_dm_que_ya_existe_lleva_a_esa_conversacion(monkeypatch, session):
    """Si ana vuelve a pedir el DM con beto, o beto se lo pide a ella, no nace
    otra sala: los mensajes quedarían partidos en dos conversaciones. Y lo deja
    DENTRO de la que ya existe; mandarlo a la general lo obligaba a buscarla."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _colaboradores(session, "ana", "beto")

    primera = _sala_de_la_url(_pedir_dm("ana", "beto"))
    otra_vez = _sala_de_la_url(_pedir_dm("ana", "beto"))
    al_reves = _sala_de_la_url(_pedir_dm("beto", "ana"))

    session.expire_all()
    directos = session.exec(select(ChatRoom).where(ChatRoom.room_type == "direct")).all()
    assert [r.id for r in directos] == [primera], "se duplicó el DM"
    assert otra_vez == primera and al_reves == primera, "no lo llevó al DM que ya tenía"


def test_crear_un_grupo_lleva_a_la_sala_nueva(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _colaboradores(session, "ana", "beto", "carla")

    sala = _sala_de_la_url(
        create_room(_request("ana"), room_type="group", name="Actas", members=["beto", "carla"])
    )

    session.expire_all()
    assert session.get(ChatRoom, sala).name == "Actas"


def test_el_dm_se_llama_como_la_otra_persona(monkeypatch, session):
    """Cada quien ve el DM con el nombre del otro, como en WhatsApp; con un
    "Chat Privado" genérico no se distinguen dos DMs en la lista."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _colaboradores(session, "ana", "beto")
    sala = _sala_de_la_url(_pedir_dm("ana", "beto"))

    assert team_room_messages(_request("ana"), sala).context["room_display_name"] == "beto"
    assert team_room_messages(_request("beto"), sala).context["room_display_name"] == "ana"
    lista = {r["id"]: r["display_name"] for r in team_chat(_request("ana")).context["rooms"]}
    assert lista[sala] == "beto"


def test_la_url_abre_la_sala_pedida_solo_si_es_suya(monkeypatch, session):
    """`?room=` es lo que conserva el DM abierto tras la auto-recarga. Con el id
    de una sala ajena la página no la abre: cae a la general."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _colaboradores(session, "ana", "beto", "carla")
    sala = _sala_de_la_url(_pedir_dm("ana", "beto"))

    assert team_chat(_request("ana"), room=sala).context["sala_inicial"] == sala
    ajena = team_chat(_request("carla"), room=sala).context
    assert ajena["sala_inicial"] == ajena["general_room_id"]
    sin_sala = team_chat(_request("ana")).context
    assert sin_sala["sala_inicial"] == sin_sala["general_room_id"]


def test_se_puede_abrir_un_dm_con_uno_mismo(monkeypatch, session):
    """El DM consigo mismo (notas propias, como el "Tú" de WhatsApp) es suyo y
    solo suyo. Antes, pedirlo casaba con CUALQUIER DM de ana —ella es miembro
    de todos— y la mandaba a su conversación con beto."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _colaboradores(session, "ana", "beto")
    con_beto = _sala_de_la_url(_pedir_dm("ana", "beto"))

    propio = _sala_de_la_url(_pedir_dm("ana", "ana"))
    assert propio != con_beto, "el DM propio la mandó a su DM con beto"
    assert _sala_de_la_url(_pedir_dm("ana", "ana")) == propio, "se duplicó el DM propio"
    assert _sala_de_la_url(_pedir_dm("ana", "beto")) == con_beto

    session.expire_all()
    integrantes = session.exec(select(ChatMember.username).where(ChatMember.room_id == propio)).all()
    assert integrantes == ["ana"]
    assert team_room_messages(_request("ana"), propio).context["room_display_name"] == "ana (tú)"


def test_el_formulario_ofrece_el_dm_con_uno_mismo(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _colaboradores(session, "ana", "beto")

    html = team_chat(_request("ana")).body.decode()

    assert '<option value="ana">ana (tú)</option>' in html


@pytest.mark.parametrize("apagado", ["sala", "membresia_propia", "membresia_del_otro"])
def test_pedir_un_dm_desactivado_lo_renueva(monkeypatch, session, apagado):
    """Un DM desactivado no deja a ana sin poder escribirle a beto: pedirlo otra
    vez reactiva la misma sala, con su historial, para los dos."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _colaboradores(session, "ana", "beto")
    sala = _sala_de_la_url(_pedir_dm("ana", "beto"))
    if apagado == "sala":
        session.get(ChatRoom, sala).habilitado = False
    else:
        quien = "ana" if apagado == "membresia_propia" else "beto"
        session.exec(select(ChatMember).where(
            ChatMember.room_id == sala, ChatMember.username == quien)).one().habilitado = False
    session.commit()

    assert _sala_de_la_url(_pedir_dm("ana", "beto")) == sala, "abrió otra sala en vez de renovar"

    session.expire_all()
    assert session.get(ChatRoom, sala).habilitado
    assert all(m.habilitado for m in session.exec(select(ChatMember).where(ChatMember.room_id == sala)))
    for u in ("ana", "beto"):
        assert sala in {r["id"] for r in team_chat(_request(u)).context["rooms"]}, f"{u} no ve el DM"


# --- Candados por la URL real -----------------------------------------------
# Que nadie lea ni toque una conversación ajena cambiando el id de la URL
# (/team?room=3, /team/rooms/3/messages, ...). Va por TestClient con login real
# y no llamando a las funciones: así pasa también por la validación de FastAPI,
# que es la que frena el texto inyectado en `room`.

CLAVE = "clave-de-prueba"
SECRETO = "el acta de doña Rosa lleva la CURP al reverso"


@pytest.fixture()
def dm_de_ana_y_beto(_isolated_db, monkeypatch, tmp_path):
    """DM de ana y beto con un mensaje; carla es de la misma oficina pero ajena.

    StaticPool: TestClient atiende en otro hilo y el pool por defecto le daría
    una base :memory: distinta y vacía (ver tests/test_authz.py)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    monkeypatch.setattr(settings, "media_dir", str(tmp_path))
    hash_clave = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
    with Session(engine) as s:
        for u in ("ana", "beto", "carla"):
            s.add(User(username=u, password_hash=hash_clave))
        s.commit()

    ana = _entrar("ana")
    sala = _sala_de_la_url(ana.post("/team/rooms", data={"room_type": "group", "name": "", "members": ["beto"]},
                                    follow_redirects=False))
    ana.post(f"/team/rooms/{sala}/send", data={"body": SECRETO}, headers={"hx-request": "true"})
    with Session(engine) as s:
        mensaje = s.exec(select(TeamMessage).where(TeamMessage.room_id == sala)).one()
    return SimpleNamespace(engine=engine, sala=sala, mensaje=mensaje.id, media=tmp_path)


def _entrar(usuario: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": usuario, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", "el login de la prueba falló"
    return cliente


def _contar(engine, modelo, *condiciones) -> int:
    with Session(engine) as s:
        return len(s.exec(select(modelo).where(*condiciones)).all())


def test_nadie_ajeno_lee_un_dm_cambiando_el_id_de_la_url(dm_de_ana_y_beto):
    """carla trabaja en la misma oficina y conoce el id: aun así no ve el DM.
    Si esto se pone rojo, cualquier compañero lee las conversaciones privadas
    de los demás —con los datos de clientes que se comparten ahí."""
    dm = dm_de_ana_y_beto
    carla = _entrar("carla")

    pagina = carla.get(f"/team?room={dm.sala}")
    hilo = carla.get(f"/team/rooms/{dm.sala}/messages")

    assert pagina.status_code == 200 and SECRETO not in pagina.text
    assert f"TEAM_SALA_INICIAL = {dm.sala};" not in pagina.text, "la página abrió la sala ajena"
    assert SECRETO not in hilo.text and "No tienes acceso" in hilo.text
    # Control positivo: la misma ruta SÍ le enseña el mensaje a beto. Sin esto,
    # una ruta mal escrita (404) pasaría por candado.
    assert SECRETO in _entrar("beto").get(f"/team/rooms/{dm.sala}/messages").text


@pytest.mark.parametrize("inyectado", [
    "1 OR 1=1", "1; DROP TABLE chatroom", "1' OR '1'='1", "</script><script>alert(1)</script>",
])
def test_texto_inyectado_en_room_se_rechaza_antes_de_tocar_la_base(dm_de_ana_y_beto, inyectado):
    dm = dm_de_ana_y_beto
    carla = _entrar("carla")

    r = carla.get("/team", params={"room": inyectado})

    assert r.status_code == 422, "`room` no se validó como entero"
    assert SECRETO not in r.text
    # La base sigue intacta: beto todavía lee su DM.
    assert SECRETO in _entrar("beto").get(f"/team/rooms/{dm.sala}/messages").text


@pytest.mark.parametrize("sala", ["-1", "0", "999999"])
def test_un_id_que_no_es_suyo_cae_en_la_sala_general(dm_de_ana_y_beto, sala):
    pagina = _entrar("carla").get(f"/team?room={sala}")

    assert pagina.status_code == 200
    assert f"TEAM_SALA_INICIAL = {sala};" not in pagina.text


def test_sin_sesion_no_se_ve_nada(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto
    anonimo = TestClient(app, base_url="https://testserver")

    pagina = anonimo.get(f"/team?room={dm.sala}", follow_redirects=False)
    hilo = anonimo.get(f"/team/rooms/{dm.sala}/messages", follow_redirects=False)

    assert pagina.status_code == 303 and pagina.headers["location"] == "/login"
    assert hilo.status_code == 403 and SECRETO not in hilo.text


def test_nadie_ajeno_escribe_reacciona_ni_silencia_en_un_dm(dm_de_ana_y_beto):
    """Leer no es la única fuga: escribir en un DM ajeno lo usa de buzón, y la
    respuesta de cada acción es el hilo pintado — si el candado faltara ahí,
    reaccionar a un mensaje devolvería la conversación entera."""
    dm = dm_de_ana_y_beto
    carla = _entrar("carla")
    hx = {"hx-request": "true"}

    envio = carla.post(f"/team/rooms/{dm.sala}/send", data={"body": "me colé"}, headers=hx)
    reaccion = carla.post(f"/team/messages/{dm.mensaje}/react", data={"reaction": "👍"}, headers=hx)
    recordatorio = carla.post(f"/team/messages/{dm.mensaje}/remind", data={"remind_in_minutes": "5"})
    silencio = carla.post(f"/team/rooms/{dm.sala}/mute", data={"mute": "true"}, headers=hx)

    for respuesta in (envio, reaccion, recordatorio, silencio):
        assert SECRETO not in respuesta.text
    assert reaccion.status_code == 404 and recordatorio.status_code == 404
    assert _contar(dm.engine, TeamMessage, TeamMessage.room_id == dm.sala) == 1, "carla escribió en el DM"
    assert _contar(dm.engine, MessageReaction) == 0
    assert _contar(dm.engine, MessageReminder) == 0
    assert _contar(dm.engine, ChatMember, ChatMember.room_id == dm.sala, ChatMember.username == "carla") == 0
    assert _contar(dm.engine, ChatMember, ChatMember.room_id == dm.sala, ChatMember.is_muted == True) == 0  # noqa: E712

    # Control positivo: beto sí reacciona.
    assert _entrar("beto").post(f"/team/messages/{dm.mensaje}/react", data={"reaction": "👍"},
                                headers=hx).status_code == 200
    assert _contar(dm.engine, MessageReaction) == 1


def test_a_quien_sacaron_de_la_sala_ya_no_la_lee_ni_escribe(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto
    with Session(dm.engine) as s:
        s.exec(select(ChatMember).where(ChatMember.room_id == dm.sala,
                                        ChatMember.username == "beto")).one().habilitado = False
        s.commit()
    beto = _entrar("beto")
    hx = {"hx-request": "true"}

    assert SECRETO not in beto.get(f"/team/rooms/{dm.sala}/messages").text
    beto.post(f"/team/rooms/{dm.sala}/send", data={"body": "sigo aquí"}, headers=hx)
    reaccion = beto.post(f"/team/messages/{dm.mensaje}/react", data={"reaction": "👍"}, headers=hx)

    assert _contar(dm.engine, TeamMessage, TeamMessage.room_id == dm.sala) == 1
    assert reaccion.status_code == 404 and _contar(dm.engine, MessageReaction) == 0


def test_una_sala_desactivada_no_acepta_mensajes(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto
    with Session(dm.engine) as s:
        s.get(ChatRoom, dm.sala).habilitado = False
        s.commit()

    _entrar("ana").post(f"/team/rooms/{dm.sala}/send", data={"body": "¿hola?"}, headers={"hx-request": "true"})

    assert _contar(dm.engine, TeamMessage, TeamMessage.room_id == dm.sala) == 1


def test_reaccionar_a_algo_que_no_existe_o_no_se_ofrece_no_truena(dm_de_ana_y_beto):
    """Un id inexistente respondía 500; una reacción de texto libre guardaba
    cualquier cadena, de cualquier largo, pintada luego a toda la sala."""
    dm = dm_de_ana_y_beto
    ana = _entrar("ana")
    hx = {"hx-request": "true"}

    fantasma = ana.post("/team/messages/999999/react", data={"reaction": "👍"}, headers=hx)
    libre = ana.post(f"/team/messages/{dm.mensaje}/react", data={"reaction": "<b>x</b>" * 50}, headers=hx)

    assert fantasma.status_code == 404 and libre.status_code == 404
    assert _contar(dm.engine, MessageReaction) == 0


def test_un_ajeno_no_deja_archivos_en_disco(dm_de_ana_y_beto):
    """La membresía se comprobaba DESPUÉS de guardar: el mensaje no nacía, pero
    el archivo de carla sí quedaba en el disco del servidor."""
    dm = dm_de_ana_y_beto
    archivo = {"file": ("nota.pdf", b"%PDF-1.4 prueba", "application/pdf")}

    _entrar("carla").post(f"/team/rooms/{dm.sala}/send-media", files=archivo)
    assert list(dm.media.iterdir()) == [], "quedó un archivo de alguien ajeno a la sala"

    _entrar("beto").post(f"/team/rooms/{dm.sala}/send-media", files=archivo)
    assert len(list(dm.media.iterdir())) == 1, "control positivo: el miembro sí sube"


# --- El refresco del chat no se ve ------------------------------------------

def test_el_chat_no_recarga_la_pagina_ni_repinta_la_caja_de_texto():
    """El chat se refrescaba recargando la página cada 20 s y repintando el
    panel entero cada 5 s: parpadeo, el scroll brincaba al fondo a media
    lectura y el mensaje a medio escribir se borraba. El sondeo ahora solo
    cambia el hilo, y solo si cambió."""
    plantilla = pathlib.Path("templates/team_chat.html").read_text()
    js = pathlib.Path("static/js/team.js").read_text()
    sondeo = js.split("function actualizarHilo(")[1].split("\nfunction ")[0]

    assert 'name="auto-recarga"' not in plantilla and 'http-equiv="refresh"' not in plantilla
    assert "location.reload" not in js
    assert "else actualizarHilo(html, false)" in js, "el sondeo ya no pasa por actualizarHilo"
    assert "!== ultimoHilo" in sondeo, "el sondeo repinta aunque nada haya cambiado"
    assert "chat-input" not in sondeo and "chatDiv" not in sondeo, "el sondeo toca la caja de texto"


def test_la_plantilla_del_chat_no_lleva_estilos_ni_eventos_en_linea():
    """Regla del operador (skill frontend-senior): el estilo vive en
    static/css/team.css y el comportamiento en static/js/team.js; la plantilla
    solo marca y le pasa datos al JS."""
    plantilla = pathlib.Path("templates/team_chat.html").read_text()
    js = pathlib.Path("static/js/team.js").read_text()

    assert "style=" not in plantilla
    assert "onclick=" not in plantilla and " on" + "change=" not in plantilla
    # Lo que hacían esos onclick sigue conectado desde el JS.
    for gancho in ('data-accion="abrir-nueva-sala"', 'data-accion="cerrar-nueva-sala"'):
        assert gancho in plantilla and gancho.split("=")[1] in js
    assert "closest('.room-item')" in js, "el clic en una sala ya no abre nada"


# --- Eliminar chat: solo para quien lo elimina, como WhatsApp (soft delete) ---

def _membresia(engine, sala: int, usuario: str) -> ChatMember:
    with Session(engine) as s:
        return s.exec(select(ChatMember).where(
            ChatMember.room_id == sala, ChatMember.username == usuario)).one()


def test_eliminar_un_chat_lo_quita_solo_para_quien_lo_elimina(dm_de_ana_y_beto):
    """Si esto se pone rojo, borrar un chat le arrancaría la conversación a la
    otra persona — o, al revés, el chat «eliminado» seguiría en la lista."""
    dm = dm_de_ana_y_beto
    ana, beto = _entrar("ana"), _entrar("beto")

    r = ana.post(f"/team/rooms/{dm.sala}/delete", follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == "/team"
    assert f'data-room-id="{dm.sala}"' not in ana.get("/team").text, "ana sigue viendo el chat"
    assert f'data-room-id="{dm.sala}"' in beto.get("/team").text, "a beto también se le borró"
    assert SECRETO in beto.get(f"/team/rooms/{dm.sala}/messages").text
    # Soft delete: nada se borró de la base.
    assert _membresia(dm.engine, dm.sala, "ana").borrado_at is not None
    assert _contar(dm.engine, TeamMessage, TeamMessage.room_id == dm.sala,
                   TeamMessage.habilitado == True) == 1  # noqa: E712
    with Session(dm.engine) as s:
        assert s.get(ChatRoom, dm.sala).habilitado


def test_el_chat_eliminado_reaparece_solo_con_lo_nuevo(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto
    ana, beto = _entrar("ana"), _entrar("beto")
    ana.post(f"/team/rooms/{dm.sala}/delete")

    beto.post(f"/team/rooms/{dm.sala}/send", data={"body": "¿ya viste lo nuevo?"}, headers={"hx-request": "true"})

    assert f'data-room-id="{dm.sala}"' in ana.get("/team").text, "no reapareció con el mensaje nuevo"
    hilo = ana.get(f"/team/rooms/{dm.sala}/messages").text
    assert "¿ya viste lo nuevo?" in hilo
    assert SECRETO not in hilo, "reapareció con el historial que ana eliminó"


def test_volver_a_abrir_el_dm_eliminado_lo_muestra_vacio(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto
    ana = _entrar("ana")
    ana.post(f"/team/rooms/{dm.sala}/delete")

    r = ana.post("/team/rooms", data={"room_type": "group", "name": "", "members": ["beto"]}, follow_redirects=False)

    assert _sala_de_la_url(r) == dm.sala, "abrió otro DM en vez del mismo"
    assert f"TEAM_SALA_INICIAL = {dm.sala};" in ana.get(f"/team?room={dm.sala}").text
    assert SECRETO not in ana.get(f"/team/rooms/{dm.sala}/messages").text


def test_lo_anterior_al_borrado_ya_no_se_toca_ni_se_recuerda(dm_de_ana_y_beto):
    """Reaccionar, poner un recordatorio o recibir uno devuelven el texto del
    mensaje: tras eliminar el chat, lo de antes no puede volver por ahí."""
    dm = dm_de_ana_y_beto
    ana = _entrar("ana")
    with Session(dm.engine) as s:
        s.add(MessageReminder(message_id=dm.mensaje, username="ana",
                              remind_at=datetime.utcnow() - timedelta(minutes=1)))
        s.commit()
    ana.post(f"/team/rooms/{dm.sala}/delete")

    hx = {"hx-request": "true"}
    assert ana.post(f"/team/messages/{dm.mensaje}/react", data={"reaction": "👍"}, headers=hx).status_code == 404
    assert ana.post(f"/team/messages/{dm.mensaje}/remind", data={"remind_in_minutes": "5"}).status_code == 404
    assert ana.get("/team/reminders/active").json() == []


def test_la_oficina_general_no_se_elimina(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto
    ana = _entrar("ana")
    ana.get("/team")
    with Session(dm.engine) as s:
        general = s.exec(select(ChatRoom).where(ChatRoom.room_type == "general")).one().id

    ana.post(f"/team/rooms/{general}/delete")

    assert _membresia(dm.engine, general, "ana").borrado_at is None
    assert "form-eliminar-chat" not in ana.get(f"/team/rooms/{general}/messages").text
    assert "form-eliminar-chat" in ana.get(f"/team/rooms/{dm.sala}/messages").text


def test_un_ajeno_no_elimina_el_chat_de_otros(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto

    _entrar("carla").post(f"/team/rooms/{dm.sala}/delete")

    for u in ("ana", "beto"):
        assert _membresia(dm.engine, dm.sala, u).borrado_at is None
    assert _contar(dm.engine, ChatMember, ChatMember.room_id == dm.sala, ChatMember.username == "carla") == 0


# --- Compartir imágenes, capturas y documentos, como WhatsApp -----------------

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


def _adjuntar(cliente, sala, nombre, datos, tipo, pie=""):
    return cliente.post(f"/team/rooms/{sala}/send-media", files={"file": (nombre, datos, tipo)},
                        data={"caption": pie}, headers={"hx-request": "true"})


def test_una_captura_se_comparte_y_se_ve_en_el_hilo(dm_de_ana_y_beto):
    dm = dm_de_ana_y_beto

    r = _adjuntar(_entrar("ana"), dm.sala, "captura-20260910-120000.png", PNG, "image/png")

    assert r.status_code == 200
    assert '<img src="/media/' in r.text
    assert "[archivo:" not in r.text, "el nombre de relleno se pinta como si fuera texto"
    with Session(dm.engine) as s:
        assert s.exec(select(TeamMessage).where(TeamMessage.media_path != None)).one().media_path.endswith(".png")  # noqa: E711


def test_el_pie_de_foto_acompana_a_la_imagen(dm_de_ana_y_beto):
    r = _adjuntar(_entrar("ana"), dm_de_ana_y_beto.sala, "image.png", PNG, "image/png", pie="así sale el error")

    assert "así sale el error" in r.text and '<img src="/media/' in r.text


def test_un_documento_se_muestra_con_su_nombre(dm_de_ana_y_beto):
    r = _adjuntar(_entrar("ana"), dm_de_ana_y_beto.sala, "requisitos.pdf", b"%PDF-1.4 prueba", "application/pdf")

    assert 'class="doc-adjunto"' in r.text and "requisitos.pdf" in r.text
    assert "[archivo: requisitos.pdf]" not in r.text


@pytest.mark.parametrize("nombre, datos, tipo, motivo", [
    ("notas.txt", b"hola", "text/plain", "tipo de archivo"),
    ("enorme.png", b"0" * (5 * 1024 * 1024 + 1), "image/png", "demasiado grande"),
])
def test_un_adjunto_rechazado_dice_por_que_y_no_queda_en_disco(dm_de_ana_y_beto, nombre, datos, tipo, motivo):
    dm = dm_de_ana_y_beto

    r = _adjuntar(_entrar("ana"), dm.sala, nombre, datos, tipo)

    assert r.status_code == 422 and motivo in r.text
    assert list(dm.media.iterdir()) == []
    assert _contar(dm.engine, TeamMessage, TeamMessage.room_id == dm.sala) == 1


def test_los_archivos_del_chat_solo_los_abren_sus_miembros(dm_de_ana_y_beto):
    """El documento compartido en un DM es parte de la conversación: si carla
    lo abre con el enlace, se filtró aunque no pueda leer los mensajes."""
    dm = dm_de_ana_y_beto
    ana = _entrar("ana")
    _adjuntar(ana, dm.sala, "acta.pdf", b"%PDF-1.4 acta de cliente", "application/pdf")
    with Session(dm.engine) as s:
        ruta = s.exec(select(TeamMessage.media_path).where(TeamMessage.media_path != None)).one()  # noqa: E711

    assert _entrar("beto").get(ruta).content == b"%PDF-1.4 acta de cliente"
    ajena = _entrar("carla").get(ruta, follow_redirects=False)
    assert ajena.status_code == 303 and ajena.headers["location"] == "/", "carla abrió el archivo del DM"
    anonima = TestClient(app, base_url="https://testserver").get(ruta, follow_redirects=False)
    assert anonima.headers["location"] == "/login"
    # Quien eliminó el chat deja de ver también sus archivos, como en WhatsApp.
    ana.post(f"/team/rooms/{dm.sala}/delete")
    assert ana.get(ruta, follow_redirects=False).headers["location"] == "/"


def test_un_adjunto_de_la_bandeja_sigue_igual(dm_de_ana_y_beto):
    """Control: la regla nueva es solo para archivos del chat de equipo. Un
    adjunto de cliente (bandeja) conserva su regla de siempre: basta sesión."""
    nombre = "a" * 32 + ".pdf"
    (dm_de_ana_y_beto.media / nombre).write_bytes(b"%PDF-1.4 cliente")

    assert _entrar("carla").get(f"/media/{nombre}").content == b"%PDF-1.4 cliente"


def test_el_compositor_acepta_clip_pegar_y_arrastrar_sin_codigo_en_linea():
    parcial = pathlib.Path("templates/partials/team_messages.html").read_text()
    js = pathlib.Path("static/js/team.js").read_text()

    assert "style=" not in parcial and "onclick=" not in parcial
    assert 'type="file"' in parcial and "data-adjunto-action" in parcial
    assert "addEventListener('paste'" in js, "pegar una captura ya no adjunta nada"
    assert "addEventListener('drop'" in js, "arrastrar un archivo ya no adjunta nada"
    assert "form.dataset.adjuntoAction" in js, "enviar ya no manda el adjunto"


def test_el_espejo_mssql_guarda_cuando_se_elimino_el_chat(monkeypatch):
    """No hay MSSQL local y `_safe()` se traga cualquier error del espejo, así
    que un INSERT con una columna de más o una columna que MSSQL no tiene falla
    en silencio para siempre. Aquí se captura el SQL que el espejo manda —sin
    conectarse— y se revisa que `borrado_at` viaje completa."""
    enviados = []

    class _Conexion:
        def execute(self, sql, params):
            enviados.append((str(sql), params))

    class _Motor:
        @contextmanager
        def begin(self):
            yield _Conexion()

    monkeypatch.setattr(archive, "_get_engine", lambda: _Motor())
    miembro = ChatMember(id=7, room_id=3, username="ana", borrado_at=datetime(2026, 9, 10, 12, 0))

    archive.archive_chat_member(miembro)
    archive.sync_chat_member(miembro)

    (insert, p_insert), (update, p_update) = enviados
    columnas = [c.strip() for c in re.search(r"\(([^)]*)\)\s*VALUES", insert).group(1).split(",")]
    valores = re.search(r"VALUES\s*\(([^)]*)\)", insert).group(1).split(",")
    assert len(columnas) == len(valores), "el INSERT del espejo no cuadra columnas con valores"
    assert "borrado_at" in columnas and p_insert["bor"] == miembro.borrado_at
    assert "borrado_at = :bor" in update and p_update["bor"] == miembro.borrado_at
    # La columna tiene que existir en MSSQL: chat_members ya está en producción,
    # así que solo la crea el ALTER de las columnas tardías.
    assert "borrado_at" in archive._COLUMNS_ADDED_AFTER_LAUNCH.get("chat_members", {})
