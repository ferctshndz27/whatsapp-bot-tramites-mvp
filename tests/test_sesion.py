"""GET /sesion: lo que el panel de la extensión pregunta para decidir entre el acceso y la barra.

REGLA DE NEGOCIO: sin sesión el panel muestra el acceso; con sesión, la barra de módulos; al salir,
otra vez el acceso. Si /sesion mintiera un «sí», el panel pintaría la barra sobre un marco que solo
muestra el login.

REGLA DE SEGURIDAD: solo de lectura, solo devuelve el usuario a quien ya está dentro, y nunca queda
en caché.

REGLA DE NEGOCIO (V4): la barra solo ofrece lo que el usuario puede abrir: colaborador 9 módulos,
admin de oficina 13, admin global 17 — los mismos que le enseña el menú del ERP. Esconder un botón
es cortesía; la ruta de administración sigue negándose por su cuenta.
"""

import re

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app.config import settings
from app.main import app
from app.models import User

CLAVE = "clave-de-prueba"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def _base_compartida(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(User(username="asesora", password_hash=HASH_CLAVE))
        s.add(User(username="jefa", password_hash=HASH_CLAVE, is_admin=True, solo_su_oficina=True))
        s.add(User(username="jefe", password_hash=HASH_CLAVE, is_admin=True))
        s.commit()


def _cliente() -> TestClient:
    return TestClient(app, base_url="https://testserver")


def _entrar(cliente: TestClient, usuario: str = "asesora") -> None:
    r = cliente.post("/login", data={"username": usuario, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303, "el login de la prueba dejó de funcionar"


def test_sin_sesion_contesta_401_con_json_y_sin_nombre_alguno():
    r = _cliente().get("/sesion", follow_redirects=False)
    assert r.status_code == 401, "sin sesión /sesion debe negarse, no redirigir al login"
    assert r.json() == {"error": "sin sesión"}


def test_con_sesion_dice_quien_es_y_no_se_guarda_en_cache():
    with _cliente() as cliente:
        _entrar(cliente)
        r = cliente.get("/sesion", follow_redirects=False)
    assert r.status_code == 200 and r.json()["usuario"] == "asesora"
    assert r.headers["cache-control"] == "no-store", "un «sí» en caché sobrevive a la sesión vencida"


def test_al_salir_vuelve_a_negarse():
    """El botón «Salir» del panel llama a /logout y después vuelve a preguntar."""
    with _cliente() as cliente:
        _entrar(cliente)
        assert cliente.get("/sesion").status_code == 200
        cliente.get("/logout", follow_redirects=False)
        assert cliente.get("/sesion").status_code == 401, "la sesión siguió viva después de /logout"


def test_el_marco_no_cambia_la_respuesta():
    """Preguntar desde el panel (con o sin la señal de marco) da la misma respuesta."""
    r = _cliente().get("/sesion", headers={"Sec-Fetch-Dest": "iframe"}, follow_redirects=False)
    assert r.status_code == 401


# id del módulo (ext-cse/src/barra.js) → enlace que le corresponde en el menú del ERP.
ENLACE = {
    "dia": "/my", "cartera": "/my/cobros", "nuevocobro": "/cobros/nuevo", "solicitudes": "/", "whatsapp": "/crm", "clientes": "/clients",
    "equipo": "/team", "actividad": "/activity", "guia": "/kb", "catalogo": "/catalog",
    "invitaciones": "/invitations", "oficinas": "/oficinas", "usuarios": "/users", "cobros": "/cobros",
    "accesos": "/logins", "agente": "/unmatched", "estudio": "/agente", "sistema": "/system",
}


def _modulos(usuario: str) -> list[str]:
    with _cliente() as cliente:
        _entrar(cliente, usuario)
        return cliente.get("/sesion").json()["modulos"]


def _enlaces_del_menu(usuario: str) -> set[str]:
    with _cliente() as cliente:
        _entrar(cliente, usuario)
        html = cliente.get("/my").text
    menu = html.split('aria-label="Principal"', 1)[1].split("</nav>", 1)[0]
    return set(re.findall(r'<a [^>]*href="(/[^"]*)"', menu))


@pytest.mark.parametrize("usuario, cuantos", [("asesora", 10), ("jefa", 14), ("jefe", 18)])
def test_la_barra_ofrece_solo_lo_que_cada_rol_puede_abrir(usuario, cuantos):
    modulos = _modulos(usuario)
    assert len(modulos) == cuantos and len(set(modulos)) == cuantos, f"{usuario} recibió {modulos}"


def test_el_admin_de_oficina_no_recibe_lo_que_es_de_todas_las_oficinas():
    modulos = _modulos("jefa")
    for global_ in ("oficinas", "agente", "estudio", "sistema"):
        assert global_ not in modulos, f"el admin de oficina vería «{global_}», de todas las oficinas"
    assert "usuarios" in modulos


@pytest.mark.parametrize("usuario", ["asesora", "jefa", "jefe"])
def test_la_lista_coincide_con_el_menu_del_erp(usuario):
    """Si alguien agrega una entrada al menú y no a la lista (o al revés), la barra de la extensión y
    el ERP enseñarían cosas distintas al mismo usuario."""
    esperado = {ENLACE[m] for m in _modulos(usuario)}
    assert esperado == _enlaces_del_menu(usuario)


@pytest.mark.parametrize("ruta", ["/users", "/oficinas", "/system", "/unmatched", "/agente"])
@pytest.mark.parametrize("marco", [{}, {"Sec-Fetch-Dest": "iframe"}])
def test_la_ruta_de_administracion_sigue_negandose_al_colaborador(ruta, marco):
    """Que la barra no ofrezca el botón no es la guardia: la ruta contesta lo mismo con o sin marco."""
    with _cliente() as cliente:
        _entrar(cliente)
        r = cliente.get(ruta, headers=marco, follow_redirects=False)
    assert r.status_code != 200, f"{ruta} abrió para un colaborador"
