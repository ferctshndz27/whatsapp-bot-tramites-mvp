"""CRM Kanban (/crm, app/crm.py): etapas globales, tablero acotado por oficina y chat JSON.

Misma base que tests/test_admin_oficina.py: dos oficinas, un admin global (jefa1), un admin de
oficina en Puebla (jefe2) y un colaborador por oficina. Aquí se afirma:

- init_db siembra las cuatro etapas del plan una sola vez (renombrar o dar de baja una no la
  resucita en el siguiente arranque).
- el tablero y el chat solo muestran clientes de la oficina del usuario; el admin global ve todo;
  un teléfono de otra oficina por URL responde como «no existe».
- un colaborador ve solo las conversaciones de las solicitudes que toma (folio vigente suyo);
  los admins ven todo su alcance, a los miembros de la oficina y son los únicos que dan de alta.
- «no leídos» = entrantes desde el último saliente; NULL en etapa_crm_id = primera etapa.
- mover de etapa escribe por el ORM (etapa_crm_id/at/por) y respeta el alcance.
- las etapas las administra SOLO el admin global; dar de baja una mueve a sus clientes a la
  primera restante y la última no se puede dar de baja.
- la API contesta 403 JSON a un anónimo, nunca 200.
- el panel del folio (el cajón del chat) trae folio, permisos, etiquetas, notas, cobro,
  seguimiento y compañeros con la MISMA visibilidad que el chat, y sus permisos son los de
  los endpoints —no los de ticket_detail.html, que esconde de más.
"""

from datetime import timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.config import settings
from app.main import app
import app.crm as crm
from app.models import (
    ETAPAS_CRM_DEFAULT,
    Client,
    ClientNote,
    Cobro,
    Conversation,
    CrmEtapa,
    Direction,
    FollowUp,
    Label,
    Message,
    Office,
    Ticket,
    TicketLabel,
    TicketStatus,
    User,
    utcnow,
)

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()

ANA = "525500000011"      # Tlaxcala, con conversación: 1 saliente y 2 entrantes después
BETO = "525500000012"     # Tlaxcala, sin mensajes ni folio
CARLA = "522200000021"    # Puebla


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()  # siembra Tlaxcala y las etapas del CRM
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    ahora = utcnow()
    with Session(engine) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        for nombre, admin, solo, oficina in (
            ("jefa1", True, False, 1),   # admin global
            ("jefe1", True, True, 1),    # admin DE OFICINA de Tlaxcala
            ("jefe2", True, True, 2),    # admin DE OFICINA de Puebla
            ("colab1", False, False, 1),
            ("colab2", False, False, 2),
        ):
            s.add(User(username=nombre, password_hash=HASH, is_admin=admin,
                       solo_su_oficina=solo, office_id=oficina))
        # Altas de hace días: el orden del tablero es por actividad (último mensaje o alta)
        # y Ana, que escribió hace 5 minutos, debe ir antes que Beto, que nunca escribió.
        s.add(Client(phone=ANA, name="Ana Lucía Ramírez", office_id=1,
                     created_at=ahora - timedelta(days=3)))
        s.add(Client(phone=BETO, name="Beto Nuñez", office_id=1,
                     created_at=ahora - timedelta(days=2)))
        s.add(Client(phone=CARLA, name="Carla Pérez", office_id=2))
        s.add(Conversation(phone=ANA, state="escalado", office_id=1))
        s.add(Ticket(phone=ANA, client_name="Ana Lucía Ramírez", category="Actas",
                     service_id="acta", service_name="Acta de nacimiento", price_info="$150",
                     status=TicketStatus.TOMADO, assigned_to="colab1", office_id=1))
        s.add(Message(phone=ANA, direction=Direction.IN, body="Hola, ¿cuánto cuesta un acta?",
                      created_at=ahora - timedelta(hours=3)))
        s.add(Message(phone=ANA, direction=Direction.OUT, body="Hola Ana, son $150.",
                      sent_by="colab1", wa_status="read", created_at=ahora - timedelta(hours=2)))
        s.add(Message(phone=ANA, direction=Direction.IN, body="¿Aceptan transferencia?",
                      created_at=ahora - timedelta(minutes=30)))
        s.add(Message(phone=ANA, direction=Direction.IN, body="Y ¿cuánto tarda?",
                      created_at=ahora - timedelta(minutes=5)))
        s.commit()
    yield


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303, f"el login de {username} falló"
    return cliente


def _tablero(username: str, **params) -> dict:
    r = _sesion(username).get("/crm/api/tablero", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _contacto(tablero: dict, phone: str) -> dict:
    return next(c for c in tablero["contactos"] if c["phone"] == phone)


def _etapas() -> list[CrmEtapa]:
    with Session(db.engine) as s:
        return s.exec(
            select(CrmEtapa).where(CrmEtapa.habilitado == True).order_by(CrmEtapa.ord)  # noqa: E712
        ).all()


# ---------------------------------------------------------------------------
# Siembra de etapas
# ---------------------------------------------------------------------------

def test_init_db_siembra_las_etapas_del_plan_una_sola_vez():
    etapas = _etapas()
    assert [(e.nombre, e.color) for e in etapas] == list(ETAPAS_CRM_DEFAULT)
    assert [e.ord for e in etapas] == [0, 1, 2, 3]
    db.init_db()  # el cron también arranca init_db: no duplica
    assert len(_etapas()) == 4


def test_una_etapa_renombrada_o_dada_de_baja_no_vuelve_al_arrancar():
    with Session(db.engine) as s:
        primera, ultima = _etapas()[0], _etapas()[-1]
        primera = s.get(CrmEtapa, primera.id)
        primera.nombre = "Prospecto"
        ultima = s.get(CrmEtapa, ultima.id)
        ultima.habilitado = False
        s.add(primera)
        s.add(ultima)
        s.commit()
    db.init_db()
    nombres = [e.nombre for e in _etapas()]
    assert nombres == ["Prospecto", "En Negociación", "Seguimiento"]


# ---------------------------------------------------------------------------
# Página y acceso
# ---------------------------------------------------------------------------

def test_la_pagina_abre_para_cualquier_usuario_con_sesion_y_no_para_un_anonimo():
    for quien in ("jefa1", "jefe2", "colab1"):
        r = _sesion(quien).get("/crm")
        assert r.status_code == 200, quien
        assert 'id="crm-datos"' in r.text and "crm.js" in r.text
    anonimo = TestClient(app, base_url="https://testserver")
    r = anonimo.get("/crm", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_el_menu_lateral_trae_la_entrada_del_crm():
    html = _sesion("colab1").get("/crm").text
    assert 'href="/crm" aria-current="page"' in html


def test_la_api_contesta_403_json_a_un_anonimo():
    anonimo = TestClient(app, base_url="https://testserver")
    for metodo, ruta in (
        ("GET", "/crm/api/tablero"),
        ("GET", f"/crm/api/chat/{ANA}"),
        ("GET", f"/crm/api/panel/{ANA}"),
        ("POST", "/crm/api/etapas"),
        ("PUT", f"/crm/api/contactos/{ANA}/etapa"),
        ("POST", "/crm/api/contactos"),
    ):
        r = anonimo.request(metodo, ruta, json={}, follow_redirects=False)
        assert r.status_code == 403, (metodo, ruta, r.status_code)
        assert r.json() == {"error": "Sesión requerida"}


def test_los_datos_de_arranque_dicen_quien_es_y_que_puede():
    import json
    import re

    def datos(quien):
        html = _sesion(quien).get("/crm").text
        return json.loads(re.search(r'id="crm-datos">(.*?)</script>', html, re.S).group(1))

    assert datos("jefa1")["admin"] and datos("jefa1")["admin_global"]
    assert datos("jefe2")["admin"] and not datos("jefe2")["admin_global"]
    assert not datos("colab1")["admin"]
    d = datos("colab1")
    assert d["usuario"] == "colab1" and d["ventana_horas"] == 24 and d["chat_inicial"] is None
    assert d["respuestas_rapidas"] and all({"title", "text"} <= set(q) for q in d["respuestas_rapidas"])
    html = _sesion("colab1").get(f"/crm?chat={ANA}").text
    assert f'"chat_inicial": "{ANA}"' in html
    html = _sesion("colab1").get("/crm?chat=<script>").text
    assert '"chat_inicial": null' in html


# ---------------------------------------------------------------------------
# Tablero: alcance, forma y conteos
# ---------------------------------------------------------------------------

def test_el_tablero_solo_trae_clientes_de_la_oficina_del_usuario():
    assert {c["phone"] for c in _tablero("jefe1")["contactos"]} == {ANA, BETO}
    assert {c["phone"] for c in _tablero("jefe2")["contactos"]} == {CARLA}
    assert {c["phone"] for c in _tablero("jefa1")["contactos"]} == {ANA, BETO, CARLA}


def test_un_colaborador_ve_solo_las_conversaciones_de_las_solicitudes_que_toma():
    # Ana tiene el folio tomado por colab1; Beto no tiene folio: colab1 solo ve a Ana.
    assert [c["phone"] for c in _tablero("colab1")["contactos"]] == [ANA]
    assert _tablero("colab2")["contactos"] == []
    assert _sesion("colab2").get(f"/crm/api/chat/{CARLA}").status_code == 404
    with Session(db.engine) as s:
        s.add(Ticket(phone=BETO, client_name="Beto Nuñez", category="CURP", service_id="curp",
                     service_name="CURP certificada", price_info="$80",
                     status=TicketStatus.TOMADO, assigned_to="colab1", office_id=1))
        s.commit()
    assert {c["phone"] for c in _tablero("colab1")["contactos"]} == {ANA, BETO}
    # Un folio nuevo de Ana que toma otro asesor se lleva la conversación: el vigente manda.
    with Session(db.engine) as s:
        s.add(Ticket(phone=ANA, client_name="Ana Lucía Ramírez", category="Actas",
                     service_id="acta-def", service_name="Acta de defunción", price_info="$150",
                     status=TicketStatus.TOMADO, assigned_to="jefe1", office_id=1,
                     created_at=utcnow() + timedelta(seconds=5)))
        s.commit()
    assert [c["phone"] for c in _tablero("colab1")["contactos"]] == [BETO]
    assert _sesion("colab1").get(f"/crm/api/chat/{ANA}").status_code == 404
    assert _sesion("colab1").put(f"/crm/api/contactos/{ANA}/etapa", json={"etapa_id": 2}).status_code == 404
    # Los admins siguen viendo a todos, y la tira de miembros es solo de ellos.
    assert {c["phone"] for c in _tablero("jefe1")["contactos"]} == {ANA, BETO}
    assert _tablero("colab1")["miembros"] == [] and _tablero("colab1")["oficinas"] == []


def test_forma_del_contacto_no_leidos_ventana_folio_y_etapa_por_defecto():
    t = _tablero("jefe1")
    ids = [e["id"] for e in t["etapas"]]
    ana = _contacto(t, ANA)
    assert ana["name"] == "Ana Lucía Ramírez" and ana["initials"] == "AL"
    assert ana["etapa_id"] == ids[0]  # NULL = primera etapa
    assert ana["unread"] == 2         # dos entrantes después del último saliente
    assert ana["last_message"]["body"] == "Y ¿cuánto tarda?"
    assert ana["last_message"]["direction"] == "in"
    assert ana["ventana"]["abierta"] is True and ana["ventana"]["nunca"] is False
    assert ana["ventana"]["texto"] != "cerrada"
    assert ana["ticket"]["status"] == "tomado" and ana["ticket"]["assigned_to"] == "colab1"
    assert ana["bot_pausado"] is True and ana["provider"] == "meta"
    assert ana["bloqueado"] is False and ana["labels"] == []
    beto = _contacto(t, BETO)
    assert beto["last_message"] is None and beto["unread"] == 0 and beto["ticket"] is None
    assert beto["ventana"] == {"abierta": False, "texto": "cerrada", "urgente": False, "nunca": True}
    assert beto["bot_pausado"] is False
    # Ana tiene actividad más reciente que Beto: va primero
    assert [c["phone"] for c in t["contactos"]] == [ANA, BETO]
    assert t["counts"] == {str(ids[0]): 2, str(ids[1]): 0, str(ids[2]): 0, str(ids[3]): 0}
    assert t["total"] == 2
    assert t["etapas"][0]["count"] == 2


def test_la_busqueda_ignora_acentos_y_formato_del_telefono():
    assert [c["phone"] for c in _tablero("jefe1", q="ramirez")["contactos"]] == [ANA]
    assert [c["phone"] for c in _tablero("jefe1", q="NUÑEZ")["contactos"]] == [BETO]
    assert [c["phone"] for c in _tablero("jefe1", q="+52 55 0000 0012")["contactos"]] == [BETO]
    assert [c["phone"] for c in _tablero("jefe1", q="0000001")["contactos"]] == [ANA, BETO]
    t = _tablero("jefe1", q="ramirez")
    assert t["total"] == 1 and sum(t["counts"].values()) == 1
    assert _tablero("jefe1", q="carla")["contactos"] == []  # es de Puebla
    # El colaborador busca solo entre lo suyo: Beto no tiene folio suyo.
    assert _tablero("colab1", q="NUÑEZ")["contactos"] == []


# ---------------------------------------------------------------------------
# Mover de etapa
# ---------------------------------------------------------------------------

def test_mover_de_etapa_escribe_por_el_orm_y_deja_quien_y_cuando():
    ids = [e.id for e in _etapas()]
    r = _sesion("colab1").put(f"/crm/api/contactos/{ANA}/etapa", json={"etapa_id": ids[2]})
    assert r.status_code == 200, r.text
    assert r.json()["etapa_id"] == ids[2] and r.json()["phone"] == ANA
    with Session(db.engine) as s:
        ana = s.get(Client, ANA)
        assert ana.etapa_crm_id == ids[2] and ana.etapa_crm_por == "colab1"
        assert ana.etapa_crm_at is not None
    assert _contacto(_tablero("colab1"), ANA)["etapa_id"] == ids[2]
    t = _tablero("jefe1")
    assert t["counts"][str(ids[0])] == 1 and t["counts"][str(ids[2])] == 1


def test_mover_valida_la_etapa_y_no_cruza_de_oficina():
    ids = [e.id for e in _etapas()]
    c = _sesion("colab1")
    assert c.put(f"/crm/api/contactos/{ANA}/etapa", json={"etapa_id": "2"}).status_code == 400
    assert c.put(f"/crm/api/contactos/{ANA}/etapa", json={"etapa_id": 9999}).status_code == 400
    assert c.put(f"/crm/api/contactos/{ANA}/etapa", json={}).status_code == 400
    r = c.put(f"/crm/api/contactos/{CARLA}/etapa", json={"etapa_id": ids[1]})
    assert r.status_code == 404 and r.json() == {"error": "No existe"}
    r = c.put("/crm/api/contactos/5299999999999/etapa", json={"etapa_id": ids[1]})
    assert r.status_code == 404
    with Session(db.engine) as s:
        assert s.get(Client, CARLA).etapa_crm_id is None
    # el admin de oficina de Puebla sí mueve a Carla, y el global también
    assert _sesion("jefe2").put(f"/crm/api/contactos/{CARLA}/etapa", json={"etapa_id": ids[1]}).status_code == 200
    assert _sesion("jefa1").put(f"/crm/api/contactos/{ANA}/etapa", json={"etapa_id": ids[3]}).status_code == 200


# ---------------------------------------------------------------------------
# Etapas: solo el admin global
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("quien", ["jefe2", "colab1"])
def test_solo_el_admin_global_administra_las_etapas(quien):
    c = _sesion(quien)
    primera = _etapas()[0].id
    assert c.post("/crm/api/etapas", json={"nombre": "Por cobrar"}).status_code == 403
    assert c.put(f"/crm/api/etapas/{primera}", json={"nombre": "X"}).status_code == 403
    assert c.delete(f"/crm/api/etapas/{primera}").status_code == 403
    assert [e.nombre for e in _etapas()] == [n for n, _ in ETAPAS_CRM_DEFAULT]


def test_el_admin_global_crea_renombra_y_da_de_baja_etapas():
    c = _sesion("jefa1")
    r = c.post("/crm/api/etapas", json={"nombre": "  Por   cobrar ", "color": "#ffb300"})
    assert r.status_code == 201, r.text
    nueva = r.json()
    assert nueva["nombre"] == "Por cobrar" and nueva["color"] == "#FFB300"
    assert nueva["ord"] == 4 and nueva["count"] == 0
    assert c.post("/crm/api/etapas", json={"nombre": "por COBRAR"}).status_code == 409
    assert c.post("/crm/api/etapas", json={"nombre": ""}).status_code == 400
    assert c.post("/crm/api/etapas", json={"nombre": "Ok", "color": "rojo"}).status_code == 400
    assert c.post("/crm/api/etapas", json={"nombre": "x" * 41}).status_code == 400

    r = c.put(f"/crm/api/etapas/{nueva['id']}", json={"nombre": "Cobrado", "ord": 1})
    assert r.status_code == 200 and r.json()["nombre"] == "Cobrado" and r.json()["ord"] == 1
    assert c.put(f"/crm/api/etapas/{nueva['id']}", json={"nombre": "seguimiento"}).status_code == 409
    assert c.put(f"/crm/api/etapas/{nueva['id']}", json={}).status_code == 400
    assert c.put(f"/crm/api/etapas/{nueva['id']}", json={"ord": -1}).status_code == 400
    assert c.put("/crm/api/etapas/9999", json={"nombre": "Nada"}).status_code == 404

    # Ana pasa a la etapa nueva; al darla de baja vuelve a la primera restante
    c.put(f"/crm/api/contactos/{ANA}/etapa", json={"etapa_id": nueva["id"]})
    r = c.delete(f"/crm/api/etapas/{nueva['id']}")
    assert r.status_code == 200
    primera = _etapas()[0].id
    assert r.json() == {"deleted": nueva["id"], "moved": 1, "target_id": primera}
    with Session(db.engine) as s:
        assert s.get(Client, ANA).etapa_crm_id == primera
        assert s.get(CrmEtapa, nueva["id"]).habilitado is False
    assert c.delete(f"/crm/api/etapas/{nueva['id']}").status_code == 404
    # volver a crearla con el mismo nombre la resucita al final, no la duplica
    r = c.post("/crm/api/etapas", json={"nombre": "cobrado"})
    assert r.status_code == 201 and r.json()["id"] == nueva["id"] and r.json()["ord"] == 4


def test_la_unica_etapa_no_se_puede_dar_de_baja():
    c = _sesion("jefa1")
    etapas = _etapas()
    for e in etapas[1:]:
        assert c.delete(f"/crm/api/etapas/{e.id}").status_code == 200
    r = c.delete(f"/crm/api/etapas/{etapas[0].id}")
    assert r.status_code == 400
    assert r.json() == {"error": "No se puede eliminar la única etapa"}
    assert len(_etapas()) == 1


# ---------------------------------------------------------------------------
# Alta de contacto desde el tablero
# ---------------------------------------------------------------------------

def test_el_alta_nace_en_la_oficina_de_quien_la_hace_con_telefono_canonico():
    ids = [e.id for e in _etapas()]
    alta = {"name": "  Dora Ibarra ", "phone": "+52 1 33 4444 5555", "etapa_id": ids[1]}
    # Un colaborador no da de alta: el contacto nacería sin folio y él no lo vería.
    assert _sesion("colab2").post("/crm/api/contactos", json=alta).status_code == 403
    r = _sesion("jefe2").post("/crm/api/contactos", json=alta)
    assert r.status_code == 201, r.text
    nuevo = r.json()
    assert nuevo["phone"] == "523344445555" and nuevo["name"] == "Dora Ibarra"
    assert nuevo["etapa_id"] == ids[1] and nuevo["initials"] == "DI"
    with Session(db.engine) as s:
        cliente = s.get(Client, "523344445555")
        assert cliente.office_id == 2 and cliente.origen == "manual"
        assert cliente.etapa_crm_por == "jefe2"
    assert "523344445555" in {c["phone"] for c in _tablero("jefe2")["contactos"]}
    assert "523344445555" not in {c["phone"] for c in _tablero("jefe1")["contactos"]}


def test_el_alta_rechaza_telefono_invalido_nombre_vacio_y_repetidos():
    c = _sesion("jefe1")
    assert c.post("/crm/api/contactos", json={"name": "X", "phone": "123"}).status_code == 400
    assert c.post("/crm/api/contactos", json={"name": "  ", "phone": "5512345678"}).status_code == 400
    r = c.post("/crm/api/contactos", json={"name": "Otra Ana", "phone": ANA})
    assert r.status_code == 409 and "Ya existe" in r.json()["error"]
    r = c.post("/crm/api/contactos", json={"name": "Otra Carla", "phone": CARLA})
    assert r.status_code == 409 and "otra oficina" in r.json()["error"]
    with Session(db.engine) as s:
        assert s.get(Client, ANA).name == "Ana Lucía Ramírez"


# ---------------------------------------------------------------------------
# Chat JSON
# ---------------------------------------------------------------------------

def test_el_chat_trae_el_hilo_completo_la_ventana_y_las_respuestas_rapidas():
    r = _sesion("colab1").get(f"/crm/api/chat/{ANA}")
    assert r.status_code == 200, r.text
    chat = r.json()
    assert [m["direction"] for m in chat["mensajes"]] == ["in", "out", "in", "in"]
    assert chat["mensajes"][1]["mio"] is True and chat["mensajes"][1]["wa_status"] == "read"
    assert chat["mensajes"][0]["mio"] is False
    assert chat["contacto"]["phone"] == ANA and chat["contacto"]["unread"] == 2
    assert chat["ventana"]["abierta"] is True
    assert chat["bot_pausado"] is True and chat["agente_activo"] is True
    assert chat["ticket"]["id"] and chat["provider"] == "meta" and chat["bloqueado"] is False
    assert chat["respuestas_rapidas"]
    # otro asesor: el mismo saliente no es «suyo»
    otro = _sesion("jefa1").get(f"/crm/api/chat/{ANA}").json()
    assert otro["mensajes"][1]["mio"] is False


def test_el_chat_de_otra_oficina_responde_como_no_existe():
    r = _sesion("colab1").get(f"/crm/api/chat/{CARLA}")
    assert r.status_code == 404 and r.json() == {"error": "No existe"}
    assert _sesion("jefe2").get(f"/crm/api/chat/{CARLA}").status_code == 200
    assert _sesion("jefa1").get(f"/crm/api/chat/{CARLA}").status_code == 200
    assert _sesion("colab1").get("/crm/api/chat/5299999999999").status_code == 404


def test_un_mensaje_oculto_no_sale_en_el_hilo_ni_cuenta_como_no_leido():
    with Session(db.engine) as s:
        m = s.exec(select(Message).where(Message.body == "Y ¿cuánto tarda?")).one()
        m.habilitado = False
        s.add(m)
        s.commit()
    chat = _sesion("colab1").get(f"/crm/api/chat/{ANA}").json()
    assert len(chat["mensajes"]) == 3 and chat["contacto"]["unread"] == 1


# ---------------------------------------------------------------------------
# Miembros de la oficina
# ---------------------------------------------------------------------------

def test_el_tablero_trae_a_los_miembros_de_la_oficina_con_su_estado_y_cuantos_atienden():
    with Session(db.engine) as s:
        oficina1 = s.get(Office, 1).nombre
        colab1 = s.exec(select(User).where(User.username == "colab1")).one()
        colab1.estado = "ocupado"
        s.add(colab1)
        # Un usuario dado de baja no es miembro de nada.
        s.add(User(username="baja1", password_hash=HASH, habilitado=False, office_id=1))
        s.commit()

    # La tira es de los admins: el colaborador no la recibe.
    assert _tablero("colab1")["miembros"] == []
    t = _tablero("jefe1")
    assert [m["username"] for m in t["miembros"]] == ["colab1", "jefa1", "jefe1"]
    assert [o["nombre"] for o in t["oficinas"]] == [oficina1]
    colab = next(m for m in t["miembros"] if m["username"] == "colab1")
    assert colab["estado"] == "ocupado" and colab["atiende"] == 1  # Ana: folio tomado por colab1
    assert colab["oficina"] == oficina1 and colab["admin"] is False
    assert colab["initials"] == "CO" and colab["avatar_color"].startswith("#")
    jefa = next(m for m in t["miembros"] if m["username"] == "jefa1")
    assert jefa["estado"] == "disponible" and jefa["atiende"] == 0 and jefa["admin"] is True

    # Puebla ve a los suyos; el admin global a todos, ordenados por oficina.
    assert [m["username"] for m in _tablero("jefe2")["miembros"]] == ["colab2", "jefe2"]
    todos = _tablero("jefa1")
    assert [(m["username"], m["oficina"]) for m in todos["miembros"]] == [
        ("colab1", oficina1), ("jefa1", oficina1), ("jefe1", oficina1),
        ("colab2", "Puebla"), ("jefe2", "Puebla"),
    ]
    assert [o["nombre"] for o in todos["oficinas"]] == [oficina1, "Puebla"]

    # El conteo describe lo que se ve: con la búsqueda sin Ana, colab1 no atiende a nadie.
    buscado = _tablero("jefe1", q="beto")
    assert next(m for m in buscado["miembros"] if m["username"] == "colab1")["atiende"] == 0


# ---------------------------------------------------------------------------
# Conectado al bot: lo que entra por el webhook aparece en el CRM de su oficina
# ---------------------------------------------------------------------------

NUEVO = "525500000099"
SECRETO_WEBHOOK = "secreto-webhook-crm"


def _sobre_meta(texto: str, phone: str, wamid: str, phone_number_id: str) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "5212200000001", "phone_number_id": phone_number_id},
            "messages": [{
                "from": phone, "id": wamid, "timestamp": "1700000000",
                "type": "text", "text": {"body": texto},
            }],
        }}]}],
    }


def test_un_mensaje_que_entra_por_el_webhook_aparece_en_el_tablero_y_el_chat_de_su_oficina(monkeypatch):
    """La misma ruta HTTP que usa Meta: POST /webhook firmado → conversation → Message.

    El número de Puebla recibe el mensaje, así que el cliente nace en Puebla: lo ven
    colab2 y jefe2 en su tablero y su chat, colab1 no. Y el bot contesta, así que el
    último mensaje es un saliente sin asesor (el JS lo pinta como «Bot»)."""
    import hashlib
    import hmac
    import json

    import app.webhook as webhook
    import app.whatsapp_client as whatsapp_client

    monkeypatch.setattr(settings, "app_secret", SECRETO_WEBHOOK)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(webhook, "resolver_oficina_meta",
                        lambda pnid: 2 if pnid == "PNID-PUEBLA" else None)
    enviados: list[str] = []
    monkeypatch.setattr(whatsapp_client, "send_text",
                        lambda phone, body: enviados.append("text") or "wamid.OUT1")
    monkeypatch.setattr(whatsapp_client, "send_list",
                        lambda phone, body, button, rows: enviados.append("list") or "wamid.OUT2")
    monkeypatch.setattr(whatsapp_client, "send_buttons",
                        lambda phone, body, buttons, content_sid=None: enviados.append("buttons") or "wamid.OUT3")

    texto = "Hola, quiero mi acta de nacimiento"
    cuerpo = json.dumps(_sobre_meta(texto, NUEVO, "wamid.IN-CRM-1", "PNID-PUEBLA")).encode()
    firma = "sha256=" + hmac.new(SECRETO_WEBHOOK.encode(), cuerpo, hashlib.sha256).hexdigest()
    r = TestClient(app).post(
        "/webhook", content=cuerpo,
        headers={"content-type": "application/json", "x-hub-signature-256": firma},
    )
    assert r.status_code == 200, r.text
    assert enviados, "el bot debía contestar"

    puebla = _tablero("jefe2")
    nuevo = _contacto(puebla, NUEVO)
    assert nuevo["last_message"]["direction"] == "out" and nuevo["last_message"]["sent_by"] is None
    assert nuevo["ventana"]["abierta"] is True and nuevo["unread"] == 0  # el bot ya contestó
    assert nuevo["bot_pausado"] is False and nuevo["provider"] == "meta"
    assert NUEVO not in {c["phone"] for c in _tablero("colab1")["contactos"]}
    # Todavía no hay folio tomado: el colaborador de Puebla no lo ve; su admin sí.
    assert NUEVO not in {c["phone"] for c in _tablero("colab2")["contactos"]}
    assert _sesion("colab2").get(f"/crm/api/chat/{NUEVO}").status_code == 404

    chat = _sesion("jefe2").get(f"/crm/api/chat/{NUEVO}").json()
    assert chat["mensajes"][0]["direction"] == "in" and chat["mensajes"][0]["body"] == texto
    assert chat["mensajes"][-1]["direction"] == "out" and chat["mensajes"][-1]["sent_by"] is None
    assert _sesion("colab1").get(f"/crm/api/chat/{NUEVO}").status_code == 404

    # Un segundo mensaje del cliente vuelve a contar como no leído hasta que alguien conteste.
    cuerpo = json.dumps(_sobre_meta("¿Cuánto cuesta?", NUEVO, "wamid.IN-CRM-2", "PNID-PUEBLA")).encode()
    firma = "sha256=" + hmac.new(SECRETO_WEBHOOK.encode(), cuerpo, hashlib.sha256).hexdigest()
    assert TestClient(app).post(
        "/webhook", content=cuerpo,
        headers={"content-type": "application/json", "x-hub-signature-256": firma},
    ).status_code == 200
    with Session(db.engine) as s:
        n_in = len(s.exec(select(Message).where(Message.phone == NUEVO, Message.direction == Direction.IN)).all())
    assert n_in == 2


# ---------------------------------------------------------------------------
# Fase B: la ruta vieja del chat reenvía al CRM
# ---------------------------------------------------------------------------

def test_la_ruta_vieja_del_chat_reenvia_al_crm_y_conserva_el_error_del_envio():
    c = _sesion("colab1")
    r = c.get(f"/inbox/{ANA}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/crm?chat={ANA}"
    # volver, folio y todo se descartan; err viaja porque el compositor del CRM lo muestra
    r = c.get(f"/inbox/{ANA}?err=window&volver=//evil.com&folio=1&todo=1", follow_redirects=False)
    assert r.headers["location"] == f"/crm?chat={ANA}&err=window"
    r = c.get(f"/inbox/{ANA}?err=<script>", follow_redirects=False)
    assert r.headers["location"] == f"/crm?chat={ANA}"
    assert c.get("/inbox/no-es-telefono", follow_redirects=False).headers["location"] == "/crm"
    anonimo = TestClient(app, base_url="https://testserver")
    assert anonimo.get(f"/inbox/{ANA}", follow_redirects=False).headers["location"] == "/login"
    # El envío desde el CRM sigue el mismo camino: el ?err= llega hasta la página del CRM.
    r = c.post(f"/inbox/{BETO}/send", data={"body": "hola"}, follow_redirects=True)
    assert r.status_code == 200 and str(r.url).endswith(f"/crm?chat={BETO}&err=window")


# ---------------------------------------------------------------------------
# Panel del folio: el cajón lateral del chat
# ---------------------------------------------------------------------------

def _panel(username: str, phone: str = ANA, status: int = 200) -> dict:
    r = _sesion(username).get(f"/crm/api/panel/{phone}")
    assert r.status_code == status, r.text
    return r.json()


def _folio() -> Ticket:
    with Session(db.engine) as s:
        return s.exec(select(Ticket).where(Ticket.phone == ANA)).first()


def test_el_panel_trae_folio_permisos_contadores_y_companeros_de_la_oficina_del_folio():
    d = _panel("colab1")
    assert d["nombre"] == "Ana Lucía Ramírez"
    assert d["ticket"]["service_name"] == "Acta de nacimiento"
    assert d["ticket"]["status"] == "tomado" and d["ticket"]["assigned_to"] == "colab1"
    permisos = d["permisos"]
    assert permisos["atiende"] and permisos["estado"] and permisos["notas"]
    assert permisos["terminar"] and permisos["soltar"] and permisos["transferir"]
    assert not permisos["es_admin"] and not permisos["tomar"] and not permisos["agente"]
    # Compañeros: gente de la oficina DEL FOLIO (Tlaxcala), sin uno mismo.
    assert [c["username"] for c in d["companeros"]] == ["jefa1", "jefe1"]
    assert d["contadores"] == {"etiquetas": 0, "notas": 0, "cobro": 0, "seguimiento": 0}


def test_el_panel_respeta_la_misma_visibilidad_que_el_chat():
    _panel("jefe2", ANA, status=404)   # admin de otra oficina
    _panel("colab2", ANA, status=404)  # colaborador de otra oficina
    # Colaborador sin folio vigente suyo: tampoco, aunque el cliente sea de su oficina.
    _panel("colab1", BETO, status=404)
    _panel("colab1", CARLA, status=404)


def test_el_admin_que_no_atiende_termina_y_suelta_pero_no_toca_estado_ni_notas():
    """ticket_detail.html esconde los cuatro bloques; el backend sí distingue."""
    permisos = _panel("jefa1")["permisos"]
    assert permisos["es_admin"] and not permisos["atiende"]
    assert not permisos["estado"] and not permisos["notas"] and not permisos["transferir"]
    assert permisos["terminar"] and permisos["soltar"] and permisos["cobrar"]
    # Etiquetar y agendar seguimiento son de equipo: cualquiera de la oficina.
    assert permisos["etiquetas"] and permisos["seguimiento"]


def test_una_conversacion_sin_folio_solo_deja_la_nota_del_cliente():
    with Session(db.engine) as s:
        s.add(ClientNote(phone=BETO, author="jefa1", body="Prefiere llamadas después de las 6."))
        s.commit()
    d = _panel("jefa1", BETO)
    assert d["ticket"] is None and d["cobro"] is None
    assert d["etiquetas"] == [] and d["seguimientos"] == [] and d["companeros"] == []
    apagados = ("estado", "notas", "terminar", "soltar", "tomar", "etiquetas",
                "seguimiento", "transferir", "cobrar", "generar_cobro")
    assert not any(d["permisos"][k] for k in apagados)
    assert [n["texto"] for n in d["notas_cliente"]] == ["Prefiere llamadas después de las 6."]
    assert d["notas_cliente"][0]["autor"] == "jefa1"
    assert d["contadores"]["notas"] == 1


def test_un_folio_sin_dueno_se_puede_tomar_desde_el_cajon():
    with Session(db.engine) as s:
        folio = s.exec(select(Ticket).where(Ticket.phone == ANA)).first()
        folio.assigned_to = None
        folio.status = TicketStatus.NUEVO
        s.add(folio)
        s.commit()
    permisos = _panel("jefa1")["permisos"]
    assert permisos["tomar"] and not permisos["atiende"]
    # Sin dueño no hay nada que soltar ni a quién transferir.
    assert not permisos["soltar"] and not permisos["transferir"]


def test_las_etiquetas_y_los_recordatorios_del_panel_son_los_del_folio_vigente():
    folio = _folio()
    with Session(db.engine) as s:
        urgente = Label(nombre="urgente", color="#f43f5e")
        retirada = Label(nombre="retirada", color="#123456")
        s.add(urgente)
        s.add(retirada)
        s.commit()
        s.refresh(urgente)
        s.refresh(retirada)
        s.add(TicketLabel(ticket_id=folio.id, label_id=urgente.id))
        s.add(TicketLabel(ticket_id=folio.id, label_id=retirada.id, habilitado=False))
        s.add(FollowUp(ticket_id=folio.id, username="colab1", note="pedir el acta del papá",
                       due_at=utcnow() + timedelta(days=1)))
        s.add(FollowUp(ticket_id=folio.id, username="colab1", note="ya cerrado",
                       due_at=utcnow(), done_at=utcnow()))
        s.commit()
    d = _panel("colab1")
    assert [e["nombre"] for e in d["etiquetas"]] == ["urgente"]
    assert d["etiquetas"][0]["color"] == "#f43f5e"
    # El datalist ofrece TODAS las del sistema, puestas o no.
    assert sorted(d["catalogo_etiquetas"]) == ["retirada", "urgente"]
    assert [f["note"] for f in d["seguimientos"]] == ["pedir el acta del papá"]
    assert d["contadores"]["etiquetas"] == 1 and d["contadores"]["seguimiento"] == 1


def test_el_resumen_del_cobro_usa_los_mismos_textos_que_la_vista_del_folio():
    import pathlib

    folio = _folio()
    with Session(db.engine) as s:
        s.add(Cobro(ticket_id=folio.id, phone=ANA, concepto=f"Acta de nacimiento (folio #{folio.id})",
                    monto_centavos=15000, estado="pendiente", creado_por="colab1",
                    token_verificacion="tok-de-prueba", url_pago="https://pay.example/abc"))
        s.commit()
    cobro = _panel("colab1")["cobro"]
    c = cobro["cobros"][0]
    assert (c["badge_clase"], c["badge_texto"]) == ("tomado", "⏳ Pendiente de pago")
    assert c["monto"] == "$150.00 MXN" and c["url"] == "https://pay.example/abc"
    assert c["url_texto"] == "Abrir link de pago"
    assert c["puede_enviar"] and c["puede_cancelar"] and c["puede_reconciliar"]
    assert c["puede_confirmar"] and not c["puede_avisar"]
    assert "Generado por colab1" in c["pie"]
    # El monto se prellena con lo que el bot le COTIZÓ al cliente.
    assert cobro["monto_sugerido"] == "150.00" and cobro["monto_cotizado"] == "$150.00 MXN"
    assert cobro["concepto_sugerido"] == f"Acta de nacimiento (folio #{folio.id})"
    # El mapeo de estados vive en dos lados a propósito; que no se separen.
    plantilla = pathlib.Path("templates/_cobro.html").read_text(encoding="utf-8")
    for clase, texto in crm.BADGE_COBRO.values():
        assert f'<span class="badge {clase}">{texto}</span>' in plantilla, texto


def test_el_token_del_formulario_de_cobro_es_nuevo_en_cada_consulta():
    """Es lo único que evita el cobro duplicado por doble clic."""
    cliente = _sesion("colab1")
    uno = cliente.get(f"/crm/api/panel/{ANA}").json()["cobro"]["token_form"]
    dos = cliente.get(f"/crm/api/panel/{ANA}").json()["cobro"]["token_form"]
    assert uno and dos and uno != dos
