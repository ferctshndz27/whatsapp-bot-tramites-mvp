"""Admin de oficina — el tercer nivel entre colaborador y admin global.

`User.solo_su_oficina` con `is_admin=True`: administra su oficina y nada más.
Revierte lo que docs/SPEC-MULTI-OFICINA.md §5 había descartado, a pedido del
operador (2026-09-11). Lo que se asevera aquí, en orden de gravedad:

1. Que un admin de oficina NO vea ni opere nada de otra oficina — ni por las
   secciones que ya acotan, ni por las globales (que no acotan: se le cierran).
2. Que no pueda escalar: ni crear admins globales, ni tocar a uno, ni quitarse
   el techo, ni mover gente a otra oficina.
3. Que los admins que ya existen sigan siendo globales tras la migración.

Cada candado lleva su control positivo con el admin global o con la oficina
propia: un guardia que lo rechazara todo pasaría el candado en verde y dejaría
al admin de oficina sin poder trabajar, o a Tlaxcala sin su admin.
"""

import re
import sqlite3

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.auth import alcance, is_admin_global
from app.config import settings
from app.main import app
from app.models import Client, Cobro, Invitation, Office, Ticket, TicketStatus, User

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        for nombre, admin, solo, oficina in (
            ("jefa1", True, False, 1),    # admin global, vive en Tlaxcala
            ("jefe2", True, True, 2),     # admin DE OFICINA de Puebla
            ("global2", True, False, 2),  # admin global que vive en Puebla
            ("colab1", False, False, 1),
            ("colab2", False, False, 2),
        ):
            s.add(User(username=nombre, password_hash=HASH, is_admin=admin,
                       solo_su_oficina=solo, office_id=oficina))
        for oficina, ciudad in ((1, "Tlaxcala"), (2, "Puebla")):
            tel = f"52550000000{oficina}"
            s.add(Client(phone=tel, name=f"Cliente de {ciudad}", office_id=oficina))
            s.add(Ticket(phone=tel, client_name=f"folio de {ciudad}", category="Actas",
                         service_id="acta", service_name=f"folio de {ciudad}",
                         price_info="N/A", status=TicketStatus.NUEVO, office_id=oficina))
            s.add(Cobro(phone=tel, concepto=f"acta de {ciudad}", monto_centavos=15000,
                        moneda="mxn", estado="pendiente", office_id=oficina,
                        token_verificacion=f"tok-{oficina}"))
            s.add(Invitation(token=f"inv-{ciudad}", created_by="jefa1", office_id=oficina))
        s.commit()
    yield


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303, f"el login de {username} falló"
    return cliente


def _usuario(nombre: str) -> User:
    with Session(db.engine) as s:
        return s.exec(select(User).where(User.username == nombre)).one()


# --- 1. El alcance -----------------------------------------------------------

def test_el_admin_de_oficina_ve_solo_la_suya():
    assert alcance("jefe2") == (2, False)
    assert alcance("jefa1").ve_todas and alcance("global2").ve_todas
    assert not is_admin_global("jefe2")
    assert is_admin_global("jefa1") and is_admin_global("global2")


@pytest.mark.parametrize("ruta, de_puebla, de_tlaxcala", [
    ("/", "folio de Puebla", "folio de Tlaxcala"),
    ("/clients", "Cliente de Puebla", "Cliente de Tlaxcala"),
    ("/cobros", "acta de Puebla", "acta de Tlaxcala"),
    ("/export/cobros.csv", "acta de Puebla", "acta de Tlaxcala"),
    ("/invitations", "inv-Puebla", "inv-Tlaxcala"),
])
def test_las_secciones_acotadas_le_ensenan_solo_su_oficina(ruta, de_puebla, de_tlaxcala):
    """Si esto se pone rojo, el admin de Puebla lee folios, clientes, cobros o
    códigos de invitación de Tlaxcala: exactamente lo que el rol existe para
    impedir."""
    suyo = _sesion("jefe2").get(ruta).text
    assert de_puebla in suyo, f"{ruta}: no ve ni lo de su oficina"
    assert de_tlaxcala not in suyo, f"{ruta}: ve lo de otra oficina"
    todo = _sesion("jefa1").get(ruta).text
    assert de_puebla in todo and de_tlaxcala in todo, f"{ruta}: el global dejó de ver todas"


def test_los_usuarios_que_lista_son_solo_los_de_su_oficina():
    html = _sesion("jefe2").get("/users").text
    assert "colab2" in html and "colab1" not in html and "jefa1" not in html


# --- Lo global se le cierra ---------------------------------------------------

# Bitácora, accesos y reportes CSV salieron de aquí en la parte 2: ya acotan
# por oficina (ver la sección de abajo). Lo que queda es de TODAS las oficinas.
GLOBALES = [
    "/oficinas", "/oficinas/1", "/system", "/unmatched",
    "/export/no-entendidos.csv", "/catalog", "/agente",
]


@pytest.mark.parametrize("ruta", GLOBALES)
def test_lo_global_no_se_le_abre_al_admin_de_oficina(ruta):
    """Estas secciones son de TODAS las oficinas (llaves, /system, catálogo,
    sinónimos, Estudio): abrírselas al de oficina es enseñarle las demás por la
    puerta de atrás. El control positivo es el global, que sigue entrando."""
    r = _sesion("jefe2").get(ruta, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/", f"{ruta} se le abrió"
    assert _sesion("jefa1").get(ruta, follow_redirects=False).status_code == 200, (
        f"{ruta}: el admin global ya no entra"
    )


def test_no_da_de_alta_oficinas():
    _sesion("jefe2").post("/oficinas", data={"nombre": "CDMX", "slug": "cdmx", "ciudad": "CDMX"})
    with Session(db.engine) as s:
        assert s.exec(select(Office).where(Office.slug == "cdmx")).first() is None
    _sesion("jefa1").post("/oficinas", data={"nombre": "CDMX", "slug": "cdmx", "ciudad": "CDMX"})
    with Session(db.engine) as s:
        assert s.exec(select(Office).where(Office.slug == "cdmx")).first() is not None


def test_ver_a_alguien_de_otra_oficina_en_actividad_cae_a_la_propia():
    """?user= de otra oficina no abre su historial: cae a la vista propia sin
    avisar. Con alguien de su oficina sí abre, y el global abre a cualquiera."""
    ajeno = _sesion("jefe2").get("/activity?user=colab1").text
    assert "Histórico de jefe2" in ajeno and "Histórico de colab1" not in ajeno
    assert "Histórico de colab2" in _sesion("jefe2").get("/activity?user=colab2").text
    assert "Histórico de colab1" in _sesion("jefa1").get("/activity?user=colab1").text


def test_el_menu_no_le_ofrece_lo_global():
    menu = _sesion("jefe2").get("/users").text
    assert 'href="/users"' in menu and 'href="/cobros"' in menu
    for global_ in ('href="/oficinas"', 'href="/system"', 'href="/catalog"', 'href="/agente"'):
        assert global_ not in menu, f"el menú le ofrece {global_}"
    assert 'href="/oficinas"' in _sesion("jefa1").get("/users").text


# --- 2. No escala -------------------------------------------------------------

def test_lo_que_da_de_alta_nace_en_su_oficina_y_con_su_techo():
    jefe = _sesion("jefe2")
    jefe.post("/users", data={"username": "nueva", "password": "clave-larga-1",
                              "role": "admin_oficina", "office_id": 2})
    nueva = _usuario("nueva")
    assert (nueva.office_id, nueva.is_admin, nueva.solo_su_oficina) == (2, True, True)


@pytest.mark.parametrize("datos, motivo", [
    ({"role": "admin", "office_id": 2}, "creó un admin GLOBAL"),
    ({"role": "colaborador", "office_id": 1}, "dio de alta en otra oficina"),
])
def test_no_crea_admins_globales_ni_gente_de_otra_oficina(datos, motivo):
    _sesion("jefe2").post("/users", data={"username": "intruso", "password": "clave-larga-1",
                                          **datos})
    with Session(db.engine) as s:
        assert s.exec(select(User).where(User.username == "intruso")).first() is None, motivo
    # Control: el global sí puede las dos cosas.
    _sesion("jefa1").post("/users", data={"username": "intruso", "password": "clave-larga-1",
                                          **datos})
    assert _usuario("intruso").office_id == datos["office_id"]


def test_hacer_admin_desde_la_oficina_fabrica_un_admin_de_oficina():
    """Sin forzar el techo, «Hacer admin» desde abajo fabricaba un GLOBAL."""
    _sesion("jefe2").post("/users/colab2/role")
    assert (_usuario("colab2").is_admin, _usuario("colab2").solo_su_oficina) == (True, True)
    # El global conserva el comportamiento de siempre: su «Hacer admin» es global.
    _sesion("jefa1").post("/users/colab1/role")
    assert (_usuario("colab1").is_admin, _usuario("colab1").solo_su_oficina) == (True, False)


@pytest.mark.parametrize("objetivo", ["global2", "colab1"],
                         ids=["admin-global-de-su-oficina", "de-otra-oficina"])
@pytest.mark.parametrize("accion", ["toggle", "role", "password"])
def test_no_toca_a_un_admin_global_ni_a_gente_de_otra_oficina(objetivo, accion):
    """global2 vive en Puebla: si el de oficina pudiera deshabilitarlo o
    degradarlo, se quitaba el techo quitando a quien se lo puso."""
    antes = _usuario(objetivo)
    _sesion("jefe2").post(f"/users/{objetivo}/{accion}", data={"password": "otra-clave-9"})
    despues = _usuario(objetivo)
    assert (despues.habilitado, despues.is_admin, despues.password_hash) == (
        antes.habilitado, antes.is_admin, antes.password_hash), f"{accion} sobre {objetivo}"


@pytest.mark.parametrize("accion", ["toggle", "role", "password"])
def test_si_gestiona_a_la_gente_de_su_oficina(accion):
    antes = _usuario("colab2")
    _sesion("jefe2").post(f"/users/colab2/{accion}", data={"password": "otra-clave-9"})
    despues = _usuario("colab2")
    assert (despues.habilitado, despues.is_admin, despues.password_hash) != (
        antes.habilitado, antes.is_admin, antes.password_hash), f"{accion} ya no funciona"


def test_no_se_quita_el_techo_ni_mueve_gente_de_oficina():
    jefe = _sesion("jefe2")
    jefe.post("/users/jefe2/solo-oficina")
    assert _usuario("jefe2").solo_su_oficina is True, "se quitó el techo solo"
    jefe.post("/users/colab2/oficina", data={"office_id": 1})
    assert _usuario("colab2").office_id == 2, "movió a alguien fuera de su oficina"


def test_no_le_quita_el_techo_a_otro_admin_de_su_oficina():
    """La escalada de verdad: quitarse el techo a sí mismo ya lo frena la regla
    de «no a ti mismo», pero dos admins de la misma oficina podrían volverse
    globales el uno al otro."""
    with Session(db.engine) as s:
        s.add(User(username="jefe2b", password_hash=HASH, is_admin=True,
                   solo_su_oficina=True, office_id=2))
        s.commit()
    _sesion("jefe2").post("/users/jefe2b/solo-oficina")
    assert _usuario("jefe2b").solo_su_oficina is True, "volvió global a su par"


def test_el_global_pone_y_quita_el_techo():
    jefa = _sesion("jefa1")
    jefa.post("/users/jefe2/solo-oficina")
    assert _usuario("jefe2").solo_su_oficina is False
    assert alcance("jefe2").ve_todas, "sin techo tiene que ver todas"
    jefa.post("/users/jefe2/solo-oficina")
    assert _usuario("jefe2").solo_su_oficina is True
    # Un colaborador no tiene techo que poner: la bandera sola no lo hace admin.
    jefa.post("/users/colab2/solo-oficina")
    assert _usuario("colab2").solo_su_oficina is False


def test_el_global_no_se_limita_a_si_mismo():
    """El último global que se limitara dejaría el panel sin nadie que
    administre oficinas y llaves."""
    _sesion("jefa1").post("/users/jefa1/solo-oficina")
    assert _usuario("jefa1").solo_su_oficina is False


# --- Invitaciones y folios ------------------------------------------------------

def _invitaciones(oficina: int) -> list[Invitation]:
    with Session(db.engine) as s:
        return list(s.exec(select(Invitation).where(Invitation.office_id == oficina)).all())


def test_solo_invita_a_su_oficina():
    jefe = _sesion("jefe2")
    jefe.post("/invitations", data={"office_id": 1})
    assert len(_invitaciones(1)) == 1, "invitó gente a otra oficina"
    # Con una sola oficina a su alcance el formulario manda la suya oculta.
    html = jefe.get("/invitations").text
    assert 'name="office_id" value="2"' in html, "el formulario mandaría Tlaxcala por default"
    jefe.post("/invitations", data={"office_id": 2})
    assert len(_invitaciones(2)) == 2, "control positivo: a la suya sí"


def test_no_revoca_invitaciones_de_otra_oficina():
    ajena = _invitaciones(1)[0].id
    propia = _invitaciones(2)[0].id
    _sesion("jefe2").post(f"/invitations/{ajena}/toggle")
    _sesion("jefe2").post(f"/invitations/{propia}/toggle")
    with Session(db.engine) as s:
        assert s.get(Invitation, ajena).habilitado is True
        assert s.get(Invitation, propia).habilitado is False


def test_solo_elimina_folios_de_su_oficina():
    with Session(db.engine) as s:
        de_tlaxcala = s.exec(select(Ticket.id).where(Ticket.office_id == 1)).one()
        de_puebla = s.exec(select(Ticket.id).where(Ticket.office_id == 2)).one()
    _sesion("jefe2").post(f"/tickets/{de_tlaxcala}/delete")
    _sesion("jefe2").post(f"/tickets/{de_puebla}/delete")
    with Session(db.engine) as s:
        assert s.get(Ticket, de_tlaxcala).habilitado is True, "eliminó un folio ajeno"
        assert s.get(Ticket, de_puebla).habilitado is False, "control positivo"


# --- 3. La migración ------------------------------------------------------------

def test_los_admins_que_ya_existen_siguen_siendo_globales(tmp_path, monkeypatch):
    """Si esto se pone rojo, el deploy degrada a TODOS los admins actuales a
    admin de oficina y nadie puede volver a entrar a /oficinas ni a /system.
    La bandera es negativa justo para esto: DEFAULT 0, sin reescribir filas."""
    ruta = tmp_path / "desplegada.db"
    con = sqlite3.connect(ruta)
    con.execute(
        'CREATE TABLE "user" (id INTEGER NOT NULL PRIMARY KEY, username VARCHAR NOT NULL, '
        "password_hash VARCHAR NOT NULL, is_admin BOOLEAN NOT NULL DEFAULT 0, "
        "habilitado BOOLEAN NOT NULL DEFAULT 1, office_id INTEGER NOT NULL DEFAULT 1, "
        "estado VARCHAR NOT NULL DEFAULT 'disponible', phone VARCHAR)"
    )
    con.execute("INSERT INTO \"user\" (id, username, password_hash, is_admin) "
                "VALUES (1, 'admin', 'x', 1)")
    con.commit()
    con.close()
    engine = create_engine(f"sqlite:///{ruta}")
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    assert alcance("admin").ve_todas and is_admin_global("admin")


# --- Parte 2: actividad, bitácora, accesos y reportes, acotados ------------------

from app import activity  # noqa: E402
from app.audit import log_admin  # noqa: E402
from app.models import LoginEvent, SavedReply, TicketActivity  # noqa: E402


def _folio(oficina: int) -> int:
    with Session(db.engine) as s:
        return s.exec(select(Ticket.id).where(Ticket.office_id == oficina)).one()


def _cierres_de_cada_oficina() -> None:
    with Session(db.engine) as s:
        s.add(TicketActivity(ticket_id=_folio(1), username="colab1", action="cerrado"))
        s.add(TicketActivity(ticket_id=_folio(2), username="colab2", action="cerrado"))
        s.commit()


def test_el_tablero_de_equipo_del_admin_de_oficina_es_su_gente():
    """Parte 2: el admin de oficina ya tiene la vista de equipo, acotada. Si
    esto se pone rojo, o no la tiene, o ve a la gente de otra oficina."""
    html = _sesion("jefe2").get("/activity").text
    assert "colab2" in html, "no ve a su propia gente en el tablero"
    assert "colab1" not in html and "jefa1" not in html, "ve gente de otra oficina"
    todo = _sesion("jefa1").get("/activity").text
    assert "colab1" in todo and "colab2" in todo, "el global dejó de ver a todos"


def test_la_mensajeria_sigue_siendo_del_global():
    """`message` no lleva oficina: el costo de Meta no se puede partir todavía."""
    assert "Mensajería y costo" not in _sesion("jefe2").get("/activity").text
    assert "Mensajería y costo" in _sesion("jefa1").get("/activity").text


def test_los_numeros_del_tablero_son_de_los_folios_de_su_oficina():
    """Cada agregado cuenta la oficina del FOLIO. `None` sigue siendo todas:
    es lo que usa el reparto (asignacion) y lo que ve el global."""
    _cierres_de_cada_oficina()
    starts = activity._period_starts_utc()
    with Session(db.engine) as s:
        assert set(activity.dispatch_counts(s, starts, office_id=2)) == {"colab2"}
        assert set(activity.dispatch_counts(s, starts)) == {"colab1", "colab2"}
        assert activity.carga_abierta(s, 2).get(None) == 1, "sin dueño de otra oficina"
        assert activity.carga_abierta(s).get(None) == 2
        embudo = dict(activity.operacion_stats(s, 2)["embudo"])
        assert sum(embudo.values()) == 1, "el embudo cuenta folios de otra oficina"


def test_la_bitacora_del_admin_de_oficina_es_la_de_su_gente():
    for quien in ("colab1", "colab2", "jefa1"):
        log_admin(quien, "prueba:bitacora", f"hecho-por-{quien}")
    suyo = _sesion("jefe2").get("/audit").text
    assert "hecho-por-colab2" in suyo, "no ve lo de su oficina"
    assert "hecho-por-colab1" not in suyo, "ve acciones de otra oficina"
    assert "hecho-por-jefa1" not in suyo, "ve acciones del admin global"
    todo = _sesion("jefa1").get("/audit").text
    assert all(f"hecho-por-{q}" in todo for q in ("colab1", "colab2", "jefa1"))


def test_los_accesos_del_admin_de_oficina_son_los_de_su_gente():
    """Un intento con un usuario inexistente no es de ninguna oficina: la
    fuerza bruta contra el panel la ve el global, no el de oficina."""
    with Session(db.engine) as s:
        for quien, ok in (("colab1", True), ("colab2", True), ("fantasma", False)):
            s.add(LoginEvent(username=quien, success=ok))
        s.commit()
    for ruta in ("/logins", "/export/logins.csv"):
        suyo = _sesion("jefe2").get(ruta).text
        assert "colab2" in suyo, f"{ruta}: no ve los de su oficina"
        assert "colab1" not in suyo and "fantasma" not in suyo, f"{ruta}: ve ajenos"
        todo = _sesion("jefa1").get(ruta).text
        assert "colab1" in todo and "fantasma" in todo, f"{ruta}: el global dejó de ver todo"


def test_el_menu_le_ofrece_accesos():
    assert 'href="/logins"' in _sesion("jefe2").get("/users").text


@pytest.mark.parametrize("ruta, de_puebla, de_tlaxcala", [
    ("/export/tickets.csv", "folio de Puebla", "folio de Tlaxcala"),
    ("/export/activity.csv", "colab2", "colab1"),
])
def test_los_reportes_csv_son_de_su_oficina(ruta, de_puebla, de_tlaxcala):
    _cierres_de_cada_oficina()
    # Sin seguir redirecciones: el dashboard al que manda un 303 también dice
    # «folio de Puebla», y la prueba pasaría con la ruta cerrada.
    r = _sesion("jefe2").get(ruta, follow_redirects=False)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), (
        f"{ruta} no se le abre al admin de oficina"
    )
    suyo = r.text
    assert de_puebla in suyo and de_tlaxcala not in suyo, f"{ruta} no acota"
    todo = _sesion("jefa1").get(ruta).text
    assert de_puebla in todo and de_tlaxcala in todo, f"{ruta}: el global dejó de ver todo"


def test_actividad_le_da_los_reportes_al_admin_de_oficina():
    """No entra a /system, que es de donde se enlazaban: sin esto, las rutas
    acotadas existirían pero nadie de oficina las encontraría."""
    html = _sesion("jefe2").get("/activity").text
    for ruta in ("/export/tickets.csv", "/export/activity.csv", "/audit"):
        assert f'href="{ruta}"' in html, f"Actividad no enlaza {ruta}"


def test_una_respuesta_guardada_nace_en_la_oficina_de_quien_la_guarda():
    _sesion("colab2").post("/replies", data={"title": "saludo", "body": "Hola"})
    with Session(db.engine) as s:
        assert s.exec(select(SavedReply.office_id)).one() == 2


def _total_de(html: str, quien: str) -> int:
    """El TOTAL de cierres en la fila de `quien` del tablero de equipo."""
    fila = re.search(rf'<th scope="row">{quien}.*?</tr>', html, re.S).group(0)
    return int(re.findall(r"<td>(\d+)</td>", fila)[3])  # día, semana, mes, TOTAL


def test_la_pagina_de_actividad_pinta_los_numeros_de_su_oficina():
    """Las funciones acotan (prueba de arriba), pero la VISTA tiene que
    pasarles la oficina. colab2 cerró también un folio de Tlaxcala (de antes de
    cambiarse de oficina): en el tablero de Puebla cuenta solo el de Puebla."""
    _cierres_de_cada_oficina()
    with Session(db.engine) as s:
        s.add(TicketActivity(ticket_id=_folio(1), username="colab2", action="cerrado"))
        s.commit()

    suyo = _sesion("jefe2").get("/activity").text
    plano = " ".join(suyo.split())  # la plantilla parte la frase en dos renglones
    assert "Hay <strong>1</strong> folio vivo" in plano, "cuenta sin dueño de otra oficina"
    assert 'aria-label="nuevo: 1 folios"' in suyo, "el embudo cuenta folios de otra oficina"
    assert _total_de(suyo, "colab2") == 1, "el tablero cuenta cierres en folios de otra oficina"

    todo = _sesion("jefa1").get("/activity").text
    assert "Hay <strong>2</strong> folios vivos" in " ".join(todo.split())
    assert _total_de(todo, "colab2") == 2
