"""Modo embebido: las vistas del ERP dentro del panel lateral de la extensión (ext-cse).

REGLA DE NEGOCIO: el colaborador ve cada módulo en el panel sin el menú ni la barra superior del ERP
(ahí los pone la barra de la extensión). Y la web normal no cambia: sin la señal, la página sale
exactamente como siempre.

REGLA DE SEGURIDAD: esto es solo presentación. La señal no concede nada: una ruta de admin contesta
igual con o sin ella.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app.config import settings
from app.main import app
from app.models import User
from app.utils import es_embebido

CLAVE = "clave-de-prueba"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
MARCO = {"Sec-Fetch-Dest": "iframe"}
MENU = '<aside class="sidebar">'
BARRA = '<div class="topbar">'


@pytest.fixture(autouse=True)
def _base_compartida(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    yield


def _entrar(usuario: str, **campos) -> TestClient:
    with Session(db.engine) as s:
        s.add(User(username=usuario, password_hash=HASH_CLAVE, **campos))
        s.commit()
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": usuario, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303, "el login de la prueba dejó de funcionar"
    return cliente


@pytest.fixture()
def http():
    with _entrar("asesora") as cliente:
        yield cliente


class _Peticion:
    def __init__(self, cabeceras=None, consulta=None):
        self.headers = cabeceras or {}
        self.query_params = consulta or {}


@pytest.mark.parametrize(
    "cabeceras, consulta, esperado",
    [
        ({"sec-fetch-dest": "iframe"}, {}, True),
        ({}, {"embed": "1"}, True),
        ({"sec-fetch-dest": "document"}, {}, False),
        ({"sec-fetch-dest": "empty"}, {}, False),
        ({}, {}, False),
        ({}, {"embed": "0"}, False),
        ({}, {"embed": "true"}, False),
    ],
)
def test_solo_un_marco_o_embed_1_activan_el_modo(cabeceras, consulta, esperado):
    assert es_embebido(_Peticion(cabeceras, consulta)) is esperado


def test_sin_la_senal_la_vista_conserva_menu_y_barra(http):
    html = http.get("/my").text
    assert MENU in html and BARRA in html, "la web normal perdió su menú o su barra superior"
    assert "embed.css" not in html


def test_en_un_marco_la_vista_no_trae_menu_ni_barra(http):
    r = http.get("/my", headers=MARCO)
    assert r.status_code == 200
    assert MENU not in r.text and BARRA not in r.text, "el panel de la extensión mostró el menú del ERP"
    assert "embed.css" in r.text and 'class="embebido"' in r.text


def test_embed_1_en_la_url_tambien_lo_activa(http):
    html = http.get("/my?embed=1").text
    assert MENU not in html and BARRA not in html


def test_el_redirect_de_un_marco_sigue_siendo_embebido(http):
    """Los POST del panel contestan 303; el marco los sigue con la misma señal y no vuelve al ERP entero."""
    r = http.get("/inbox/525500000000", headers=MARCO, follow_redirects=True)
    assert MENU not in r.text and BARRA not in r.text


def test_la_senal_no_concede_nada_a_un_colaborador():
    """Un colaborador que pide una ruta de admin recibe lo mismo con y sin la señal."""
    with _entrar("colaboradora") as cliente:
        sin = cliente.get("/users", follow_redirects=False)
        con = cliente.get("/users", headers=MARCO, follow_redirects=False)
        con_url = cliente.get("/users?embed=1", follow_redirects=False)
    assert (sin.status_code, sin.headers.get("location")) == (con.status_code, con.headers.get("location"))
    assert sin.status_code in (302, 303, 403), "la ruta de admin dejó de estar protegida"
    assert con_url.status_code == sin.status_code


def test_la_pagina_de_acceso_tambien_sale_sin_menu_en_un_marco():
    r = TestClient(app, base_url="https://testserver").get("/login", headers=MARCO)
    assert r.status_code == 200 and MENU not in r.text


def test_en_un_marco_mi_dia_solo_deja_los_clientes_esperando(http):
    """El panel de la extensión no debe cargar seguimientos ni solicitudes activas; la web sí."""
    normal = http.get("/my").text
    marco = http.get("/my", headers=MARCO).text
    for titulo in ("Seguimientos", "Mis solicitudes activas"):
        assert titulo in normal, f"la web normal perdió «{titulo}»"
        assert titulo not in marco, f"el panel de la extensión muestra «{titulo}»"


def test_en_un_marco_cada_celda_de_la_cartera_dice_a_que_columna_pertenece(http):
    """En ~340 px la tabla de Mi cartera no cabe: Monto y Estado quedaban fuera de vista.

    El panel la pinta como una tarjeta por cobro, y para eso cada celda lleva el rótulo de su
    columna (`data-label`). Si una celda lo pierde, en la tarjeta quedaría un dato sin nombre.
    """
    import re

    from app.models import Cobro

    with Session(db.engine) as s:
        s.add(Cobro(phone="525500000001", concepto="Acta de nacimiento", monto_centavos=15000, creado_por="asesora",
                    token_verificacion="tok-marco"))
        s.commit()
    html = http.get("/my/cobros?todos=1", headers=MARCO).text
    cabecera = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
    columnas = re.findall(r"<th>(.*?)</th>", cabecera)
    fila = re.search(r"<tbody>.*?<tr[^>]*>(.*?)</tr>", html, re.S).group(1)
    rotulos = re.findall(r'<td data-label="([^"]*)"', fila)
    assert len(columnas) == 9 and columnas[4] == "Monto" and columnas[5] == "Estado"
    assert rotulos == columnas, "alguna celda no dice de qué columna es (o no coincide con su encabezado)"


def _solicitud(**campos):
    from app.models import Ticket

    with Session(db.engine) as s:
        s.add(Ticket(phone="525500000001", client_name="Ana Prueba", category="Actas", service_id="acta_nacimiento", service_name="Acta de nacimiento",
                     price_info="$150", **campos))
        s.commit()


def test_en_un_marco_cada_celda_de_solicitudes_dice_a_que_columna_pertenece(http):
    """Con 7 columnas la tabla de Solicitudes no cabe en ~340 px; el panel la pinta como tarjetas.

    Una celda sin su rótulo (`data-label`) sería, en la tarjeta, un dato sin nombre.
    """
    import re

    _solicitud()
    html = http.get("/", headers=MARCO).text
    columnas = re.findall(r"<th>(.*?)</th>", re.search(r"<thead>(.*?)</thead>", html, re.S).group(1))
    fila = re.search(r"<tbody>.*?<tr[^>]*>(.*?)</tr>", html, re.S).group(1)
    rotulos = re.findall(r'<td data-label="([^"]*)"', fila)
    assert len(columnas) == 7 and columnas[0] == "Cliente" and columnas[4] == "Estado"
    assert rotulos == columnas, "alguna celda de Solicitudes no dice de qué columna es"


def test_en_un_marco_solicitudes_omite_el_saludo_y_las_cifras_pero_la_web_las_conserva(http):
    """Las cuatro cifras repiten las cuentas de los filtros y empujaban las solicitudes fuera de vista."""
    _solicitud()
    normal = http.get("/").text
    marco = http.get("/", headers=MARCO).text
    for texto in ("Total de solicitudes", "Hola, asesora"):
        assert texto in normal, f"la web normal perdió «{texto}»"
        assert texto not in marco, f"el panel de la extensión muestra «{texto}»"
    assert "todas (" in marco, "el panel perdió los filtros, que ahora llevan las cuentas"


def test_un_colaborador_recibe_lo_mismo_de_solicitudes_con_o_sin_la_senal_de_marco(http):
    """La señal es presentación: no cambia qué folios ve quien pregunta (R5)."""
    import re

    _solicitud(assigned_to="otra-persona")
    ids = lambda h: sorted(re.findall(r"/tickets/(\d+)\?volver", h))
    assert ids(http.get("/").text) == ids(http.get("/", headers=MARCO).text)


def test_en_un_marco_clientes_omite_titulo_y_explicacion_pero_la_web_los_conserva(http):
    """La explicación del alta ocupaba la mitad del panel y dejaba el directorio fuera de vista."""
    normal = http.get("/clients").text
    marco = http.get("/clients", headers=MARCO).text
    for texto in ("<h1>Clientes</h1>", "no se reinicia su conversación"):
        assert texto in normal, f"la web normal perdió «{texto}»"
        assert texto not in marco, f"el panel de la extensión muestra «{texto}»"
    assert 'id="alta-phone"' in marco and 'id="q"' in marco, "el panel perdió el alta o el buscador"


def test_en_un_marco_cada_celda_del_historial_de_la_ficha_dice_a_que_columna_pertenece(http):
    """El historial de la ficha tiene 5 columnas y en ~340 px se pinta como tarjetas con rótulo."""
    import re

    from app.models import Client

    with Session(db.engine) as s:
        s.add(Client(phone="525500000001", name="Ana Prueba"))
        s.commit()
    _solicitud()
    html = http.get("/clients/525500000001", headers=MARCO).text
    cabecera = re.search(r"<thead>(.*?)</thead>", html, re.S).group(1)
    columnas = re.findall(r"<th>(.*?)</th>", cabecera)
    fila = re.search(r"<tbody>.*?<tr[^>]*>(.*?)</tr>", html, re.S).group(1)
    assert columnas == ["Folio", "Servicio", "Estado", "Fecha", "Atendió"]
    assert re.findall(r'<td data-label="([^"]*)"', fila) == columnas


def _celdas_con_rotulo(html: str, tabla_ini: str, columnas: list[str]):
    """Devuelve los rótulos de la primera fila de la tabla que empieza en `tabla_ini`."""
    import re

    bloque = html[html.index(tabla_ini):]
    fila = re.search(r"<tbody>.*?<tr[^>]*>(.*?)</tr>", bloque, re.S).group(1)
    return re.findall(r'<td[^>]*data-label="([^"]*)"', fila)


def test_en_un_marco_la_guia_de_tramites_pinta_tarjetas_con_rotulo(http):
    import re

    html = http.get("/kb", headers=MARCO).text
    assert 'class="table-wrap tabla-tarjetas"' in html
    fila = re.search(r"<tbody>.*?<tr[^>]*>(.*?)</tr>", html, re.S).group(1)
    assert re.findall(r'data-label="([^"]*)"', fila) == ["Servicio", "Precio", "Requisitos"]


def test_en_un_marco_el_catalogo_pinta_cada_servicio_como_tarjeta_y_sus_campos_siguen_ligados_a_su_formulario():
    import re

    with _entrar("jefa", is_admin=True) as cliente:
        html = cliente.get("/catalog", headers=MARCO).text
    assert html.count("tabla-tarjetas") >= 2
    fila = re.search(r'<tr class="svc-row[^>]*>(.*?)</tr>', html, re.S).group(1)
    assert re.findall(r'data-label="([^"]*)"', fila) == ["Servicio", "Precio", "Sinónimos", "Lo atienden", "Estado", "Acciones"]
    # Si un input perdiera su `form=`, «Guardar» dejaría de enviarlo: la tarjeta no puede romper eso.
    assert len(re.findall(r'<input[^>]* form="f-[^"]+"', fila)) >= 3
