"""Cartera de clientes (app/cartera.py): qué colaboradores consideran suyo a un cliente.

Decisiones del operador (2026-09-24/25): un cliente puede estar en varias carteras
(compartido); cada colaborador se agrega o se quita a sí mismo y un admin mueve a cualquier
colaborador de la oficina del cliente; la cartera da visibilidad y preferencia de asignación,
nunca permiso de escribirle al cliente (eso es del folio, tests/test_chat_varios_duenos.py).

Base: dos oficinas, un admin global (jefa), un admin de la oficina 2 (jefe2), dos colaboradoras
de la oficina 1 (ana, beto) y una de la 2 (carla). `init_db()` real, porque el índice de fila
viva `ux_clienteasesor_vivo` es raw SQL y `create_all()` no lo crea.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import cartera, system
from app.config import settings
from app.main import app
from app.models import AdminAudit, Client, ClienteAsesor, Office, User

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()

CLI1 = "525500000031"  # oficina 1
CLI2 = "522200000032"  # oficina 2


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        for nombre, admin, solo, oficina in (
            ("jefa", True, False, 1),
            ("jefe2", True, True, 2),
            ("ana", False, False, 1),
            ("beto", False, False, 1),
            ("carla", False, False, 2),
        ):
            s.add(User(username=nombre, password_hash=HASH, is_admin=admin,
                       solo_su_oficina=solo, office_id=oficina))
        s.add(Client(phone=CLI1, name="Cliente Uno", office_id=1))
        s.add(Client(phone=CLI2, name="Cliente Dos", office_id=2))
        s.commit()
    yield


def _duenos(phone: str = CLI1) -> set[str]:
    with Session(db.engine) as s:
        return cartera.duenos(s, phone)


def _agregar(phone: str, username: str, por: str) -> str:
    with Session(db.engine) as s:
        return cartera.agregar(s, phone, username, por)


def _quitar(phone: str, username: str, por: str) -> str:
    with Session(db.engine) as s:
        return cartera.quitar(s, phone, username, por)


def _filas(phone: str = CLI1) -> list[ClienteAsesor]:
    with Session(db.engine) as s:
        return list(s.exec(select(ClienteAsesor).where(ClienteAsesor.phone == phone)).all())


# --- Las reglas del módulo -------------------------------------------------


def test_un_colaborador_se_agrega_a_si_mismo_y_queda_en_la_cartera():
    assert _agregar(CLI1, "ana", por="ana") == "agregado"
    assert _duenos() == {"ana"}
    with Session(db.engine) as s:
        assert cartera.es_mio(s, CLI1, "ana") is True
        assert cartera.es_mio(s, CLI1, "beto") is False


def test_agregarse_dos_veces_no_duplica_la_fila():
    """Un doble clic no puede dejar a alguien contando doble en la cartera."""
    _agregar(CLI1, "ana", por="ana")
    assert _agregar(CLI1, "ana", por="ana") == "ya-estaba"
    assert len([f for f in _filas() if f.habilitado]) == 1


def test_un_cliente_puede_ser_compartido():
    _agregar(CLI1, "ana", por="ana")
    _agregar(CLI1, "beto", por="beto")
    assert _duenos() == {"ana", "beto"}


def test_un_colaborador_no_agrega_ni_quita_a_otro():
    """Cada quien registra SU cartera (decisión del 2026-09-24): meter a un compañero
    le cambiaría a quién le llegan los folios de ese cliente sin que él lo decidiera."""
    assert _agregar(CLI1, "beto", por="ana") == "ajeno"
    assert _duenos() == set(), "ana metió a beto en una cartera"
    _agregar(CLI1, "beto", por="beto")
    assert _quitar(CLI1, "beto", por="ana") == "ajeno"
    assert _duenos() == {"beto"}, "ana sacó a beto de su cartera"


def test_el_admin_mueve_a_cualquier_colaborador_de_la_oficina_del_cliente():
    assert _agregar(CLI1, "beto", por="jefa") == "agregado"
    assert _duenos() == {"beto"}
    assert _quitar(CLI1, "beto", por="jefa") == "quitado"
    assert _duenos() == set()


def test_nadie_de_otra_oficina_entra_a_la_cartera():
    """El folio de este cliente nace en su oficina: alguien de otra no podría ni abrirlo."""
    assert _agregar(CLI1, "carla", por="jefa") == "usuario"
    assert _duenos() == set()


def test_un_admin_de_otra_oficina_no_toca_la_cartera():
    """Para el admin de oficina el cliente de otra no existe (mismo corte que la ficha)."""
    assert _agregar(CLI1, "ana", por="jefe2") == "no_existe"
    assert _duenos() == set()
    # control positivo: en su oficina sí
    assert _agregar(CLI2, "carla", por="jefe2") == "agregado"
    assert _duenos(CLI2) == {"carla"}


def test_un_colaborador_no_se_agrega_a_un_cliente_de_otra_oficina():
    assert _agregar(CLI2, "ana", por="ana") == "no_existe"
    assert _duenos(CLI2) == set()


def test_quitarse_apaga_la_fila_y_volver_a_agregarse_la_reactiva():
    """Baja lógica, como CatalogAsesor: el espejo MSSQL se entera por after_update y la
    historia de quién tuvo al cliente no se pierde."""
    _agregar(CLI1, "ana", por="ana")
    (fila,) = _filas()
    assert _quitar(CLI1, "ana", por="ana") == "quitado"
    assert _duenos() == set()
    assert _quitar(CLI1, "ana", por="ana") == "no-estaba"
    assert _agregar(CLI1, "ana", por="ana") == "agregado"
    assert [(f.id, f.habilitado) for f in _filas()] == [(fila.id, True)], "creó otra fila en vez de reactivar"


def test_un_usuario_dado_de_baja_deja_de_contar_como_dueno():
    _agregar(CLI1, "ana", por="ana")
    with Session(db.engine) as s:
        u = s.exec(select(User).where(User.username == "ana")).one()
        u.habilitado = False
        s.add(u)
        s.commit()
    assert _duenos() == set()


def test_clientes_de_devuelve_los_telefonos_de_su_cartera():
    _agregar(CLI1, "ana", por="ana")
    _agregar(CLI2, "carla", por="carla")
    with Session(db.engine) as s:
        assert cartera.clientes_de(s, "ana") == {CLI1}
        assert cartera.clientes_de(s, "beto") == set()


def test_cada_cambio_queda_en_la_auditoria():
    _agregar(CLI1, "beto", por="jefa")
    _quitar(CLI1, "beto", por="jefa")
    _agregar(CLI1, "beto", por="ana")  # rechazado: no deja rastro
    with Session(db.engine) as s:
        filas = s.exec(
            select(AdminAudit).where(AdminAudit.action.like("cartera:%")).order_by(AdminAudit.id)
        ).all()
    assert [(f.admin, f.action) for f in filas] == [
        ("jefa", "cartera:agregar"), ("jefa", "cartera:quitar"),
    ]
    assert all(CLI1 in (f.target or "") and "beto" in (f.target or "") for f in filas)


def test_el_indice_impide_dos_filas_vivas_del_mismo_par():
    """El candado de la base, no solo el del código: dos altas concurrentes del mismo par
    dejarían a alguien contando doble en la asignación."""
    with Session(db.engine) as s:
        s.add(ClienteAsesor(phone=CLI1, username="ana"))
        s.commit()
        s.add(ClienteAsesor(phone=CLI1, username="ana"))
        with pytest.raises(IntegrityError):
            s.commit()
        s.rollback()
        # Una fila APAGADA del mismo par no choca: es la historia.
        s.add(ClienteAsesor(phone=CLI1, username="ana", habilitado=False))
        s.commit()


def test_la_cartera_se_espeja_a_mssql():
    """Los cinco lugares del espejo los vigila tests/test_mirror_consistency.py a partir de
    MIRROR_TABLES; esta línea es la que obliga a que la tabla esté ahí."""
    assert (ClienteAsesor, "cliente_asesores") in system.MIRROR_TABLES


# --- Por HTTP real ------------------------------------------------------------


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303, f"el login de {username} falló"
    return cliente


def test_la_ficha_muestra_la_cartera_y_el_boton_para_agregarse():
    html = _sesion("ana").get(f"/clients/{CLI1}").text
    assert "Cartera" in html
    assert f'action="/clients/{CLI1}/cartera"' in html
    assert "Agregar a mi cartera" in html


def test_por_http_el_colaborador_se_agrega_y_se_quita():
    ana = _sesion("ana")
    r = ana.post(f"/clients/{CLI1}/cartera", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/clients/{CLI1}?cartera=agregado"
    assert _duenos() == {"ana"}
    assert "Quitar de mi cartera" in ana.get(f"/clients/{CLI1}").text
    r = ana.post(f"/clients/{CLI1}/cartera/quitar", follow_redirects=False)
    assert r.headers["location"] == f"/clients/{CLI1}?cartera=quitado"
    assert _duenos() == set()


def test_por_http_un_colaborador_no_mete_a_otro_aunque_arme_el_post():
    r = _sesion("ana").post(f"/clients/{CLI1}/cartera", data={"username": "beto"},
                            follow_redirects=False)
    assert r.headers["location"] == f"/clients/{CLI1}?error=ajeno"
    assert _duenos() == set(), "un POST armado a mano metió a otro en la cartera"
    # control positivo: el admin sí
    _sesion("jefa").post(f"/clients/{CLI1}/cartera", data={"username": "beto"})
    assert _duenos() == {"beto"}


def test_por_http_un_cliente_de_otra_oficina_responde_como_no_existe():
    r = _sesion("carla").post(f"/clients/{CLI1}/cartera", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert _duenos() == set()


def test_el_error_llega_como_codigo_y_se_pinta_traducido():
    """Como /clients (auditoría 2026-09, INJ-5): texto libre en la URL servía para
    suplantar contenido dentro del panel."""
    html = _sesion("ana").get(f"/clients/{CLI1}?error=ajeno").text
    assert "Solo puedes agregarte o quitarte a ti" in html
    html = _sesion("ana").get(f"/clients/{CLI1}?error=zzinyectadozz").text
    assert "zzinyectadozz" not in html


def test_el_admin_ve_un_selector_con_los_colaboradores_de_la_oficina_del_cliente():
    html = _sesion("jefa").get(f"/clients/{CLI1}").text
    assert 'value="beto"' in html and 'value="ana"' in html
    assert 'value="carla"' not in html, "ofrece a alguien de otra oficina"
    assert 'value="beto"' not in _sesion("ana").get(f"/clients/{CLI1}").text, (
        "a una colaboradora se le ofrece meter a otro"
    )


def test_el_alta_manual_agrega_a_la_cartera_si_se_marca_la_casilla():
    ana = _sesion("ana")
    ana.post("/clients", data={"phone": "5510000041", "name": "Nuevo Uno", "cartera": "1"})
    assert _duenos("525510000041") == {"ana"}
    ana.post("/clients", data={"phone": "5510000042", "name": "Nuevo Dos"})
    assert _duenos("525510000042") == set(), "sin la casilla, igual lo metió"
    # Un cliente que ya existía en su oficina queda compartido.
    _sesion("beto").post("/clients", data={"phone": "5510000041", "name": "Nuevo Uno", "cartera": "1"})
    assert _duenos("525510000041") == {"ana", "beto"}


def test_la_casilla_del_alta_viene_marcada():
    html = _sesion("ana").get("/clients").text
    assert 'name="cartera"' in html and "checked" in html.split('name="cartera"')[1].split(">")[0]


def test_mis_clientes_filtra_el_directorio():
    _agregar(CLI1, "ana", por="ana")
    with Session(db.engine) as s:
        s.add(Client(phone="525500000033", name="Cliente Tres", office_id=1))
        s.commit()
    ana = _sesion("ana")
    todos = ana.get("/clients").text
    assert "Cliente Uno" in todos and "Cliente Tres" in todos
    mios = ana.get("/clients?mios=1").text
    assert "Cliente Uno" in mios and "Cliente Tres" not in mios
