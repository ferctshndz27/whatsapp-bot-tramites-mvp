"""Calendario de cobranza: reglas puras y la vista /mensualidades (solo lectura).

REGLAS: el estatus se deriva (vence hoy no es vencido; la frontera de «por vencer» es exacta);
un asesor ve solo su cartera, un admin su oficina, y un cliente compartido no duplica filas.
"""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app import mensualidades as dom
from app.config import settings
from app.main import app
from app.models import (
    ClienteMensualidad,
    Mensualidad,
    MensualidadAsesor,
    OFICINA_TLAXCALA_ID,
    User,
)

HOY = date(2026, 11, 18)
CLAVE = "clave-de-prueba"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


def _m(vence, pagado=False):
    return Mensualidad(
        cliente_id=1,
        periodo=vence.strftime("%Y-%m"),
        vence_el=vence,
        importe_centavos=100000,
        pagado_at=datetime(2026, 11, 1) if pagado else None,
    )


# ---------- reglas puras ----------


@pytest.mark.parametrize(
    "periodo,dia,esperado",
    [
        ("2026-02", 31, date(2026, 2, 28)),
        ("2028-02", 31, date(2028, 2, 29)),  # bisiesto
        ("2026-04", 31, date(2026, 4, 30)),
        ("2026-11", 15, date(2026, 11, 15)),
    ],
)
def test_vence_el_recorta_al_ultimo_dia_del_mes(periodo, dia, esperado):
    assert dom.vence_el(periodo, dia) == esperado


@pytest.mark.parametrize(
    "vence,esperado",
    [
        (date(2026, 11, 17), dom.VENCIDO),  # ayer
        (date(2026, 11, 18), dom.POR_VENCER),  # hoy NO es vencido
        (date(2026, 11, 23), dom.POR_VENCER),  # +5 = el aviso, dentro
        (date(2026, 11, 24), dom.PENDIENTE),  # +6, fuera
    ],
)
def test_estado_de_fronteras(vence, esperado):
    assert dom.estado_de(_m(vence), HOY, aviso=5) == esperado


def test_pagada_gana_aunque_este_vencida():
    assert dom.estado_de(_m(date(2026, 1, 1), pagado=True), HOY, aviso=5) == dom.PAGADO


def test_el_estatus_cambia_a_medianoche_de_mexico():
    mx = ZoneInfo("America/Mexico_City")
    antes = datetime(2026, 11, 17, 23, 59, tzinfo=mx)
    despues = datetime(2026, 11, 18, 0, 0, tzinfo=mx)
    m = _m(date(2026, 11, 17))
    assert dom.estado_de(m, dom.hoy_mx(antes), 5) == dom.POR_VENCER
    assert dom.estado_de(m, dom.hoy_mx(despues), 5) == dom.VENCIDO


def test_faltantes_y_cliente_completo():
    c = ClienteMensualidad(nombre="Ana")
    assert dom.faltantes(c) == ["whatsapp", "importe", "dia_pago", "primer_pago"]
    c.whatsapp, c.importe_centavos, c.dia_pago, c.primer_pago = "5255", 1, 5, HOY
    assert dom.faltantes(c) == []


def test_parse_mes_ilegible_cae_al_mes_de_hoy():
    assert dom.parse_mes("2026-12", HOY) == date(2026, 12, 1)
    for malo in ("", "basura", "2026-13", "0-0", "9999999-1"):
        assert dom.parse_mes(malo, HOY) == date(2026, 11, 1)


def test_cuadricula_empieza_en_lunes_y_rellena_huecos():
    semanas = dom.semanas_del_mes(date(2026, 11, 1))  # domingo
    assert semanas[0][:6] == [None] * 6 and semanas[0][6] == date(2026, 11, 1)
    assert all(len(s) == 7 for s in semanas)


def test_aviso_dias_tiene_barandal(monkeypatch):
    assert 1 <= settings.mensualidad_aviso_dias <= 15


# ---------- la vista ----------


@pytest.fixture()
def base(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    monkeypatch.setattr(dom, "hoy_mx", lambda ahora=None: HOY)
    with Session(engine) as s:
        s.add(User(username="ana", password_hash=HASH_CLAVE))
        s.add(User(username="beto", password_hash=HASH_CLAVE))
        s.add(User(username="root", password_hash=HASH_CLAVE, is_admin=True))
        s.commit()
    return engine


def _cliente(s, nombre, asesores=(), office=OFICINA_TLAXCALA_ID, completo=True, **kw):
    c = ClienteMensualidad(
        nombre=nombre,
        office_id=office,
        whatsapp="5255000000" if completo else None,
        importe_centavos=150000 if completo else None,
        dia_pago=10 if completo else None,
        primer_pago=date(2026, 1, 10) if completo else None,
        **kw,
    )
    s.add(c)
    s.flush()
    for i, u in enumerate(asesores):
        s.add(
            MensualidadAsesor(
                cliente_id=c.id, username=u, rol="responsable" if i == 0 else "colaborador"
            )
        )
    return c


def _mens(s, c, vence, pagado=False):
    s.add(
        Mensualidad(
            cliente_id=c.id,
            periodo=vence.strftime("%Y-%m"),
            vence_el=vence,
            importe_centavos=150000,
            pagado_at=datetime(2026, 11, 1) if pagado else None,
        )
    )


def _entrar(usuario):
    http = TestClient(app, base_url="https://testserver")
    r = http.post("/login", data={"username": usuario, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return http


def test_sin_sesion_manda_a_login(base):
    r = TestClient(app, base_url="https://testserver").get("/mensualidades", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_asesor_ve_solo_su_cartera(base):
    with Session(base) as s:
        a = _cliente(s, "Cliente De Ana", ["ana"])
        b = _cliente(s, "Cliente De Beto", ["beto"])
        _mens(s, a, date(2026, 11, 20))
        _mens(s, b, date(2026, 11, 21))
        s.commit()
    html = _entrar("ana").get("/mensualidades").text
    assert "Cliente De Ana" in html and "Cliente De Beto" not in html


def test_cliente_compartido_no_duplica_filas(base):
    with Session(base) as s:
        c = _cliente(s, "Compartido", ["ana", "beto"])
        _mens(s, c, date(2026, 11, 20))
        s.commit()
    html = _entrar("beto").get("/mensualidades").text
    assert html.count("Compartido") == 1


def test_admin_ve_la_oficina_y_no_otra(base):
    with Session(base) as s:
        a = _cliente(s, "De Tlaxcala", [])
        _mens(s, a, date(2026, 11, 20))
        s.commit()
    assert "De Tlaxcala" in _entrar("root").get("/mensualidades").text


def test_estatus_vencido_cuenta_y_suma_aunque_sea_de_otro_mes(base):
    with Session(base) as s:
        # Un periodo por cliente y mes (índice único): cada fila, su cliente.
        _mens(s, _cliente(s, "Moroso viejo", ["ana"]), date(2026, 9, 10))  # fuera del mes que se mira
        _mens(s, _cliente(s, "Moroso", ["ana"]), date(2026, 11, 10))  # vencida este mes
        _mens(s, _cliente(s, "Por vencer", ["ana"]), date(2026, 11, 20))
        _mens(s, _cliente(s, "Pendiente", ["ana"]), date(2026, 11, 29))
        s.commit()
    html = _entrar("ana").get("/mensualidades").text
    assert "$3,000.00 MXN" in html  # dos vencidas × $1,500
    assert 'class="badge vencido"' in html and 'class="badge por_vencer"' in html
    assert 'class="badge pendiente"' in html


def test_filtro_de_estatus_y_dia(base):
    with Session(base) as s:
        _mens(s, _cliente(s, "Filtro uno", ["ana"]), date(2026, 11, 10))
        _mens(s, _cliente(s, "Filtro dos", ["ana"]), date(2026, 11, 29))
        s.commit()
    http = _entrar("ana")
    solo_venc = http.get("/mensualidades?estatus=vencido").text
    assert "10/11/2026" in solo_venc and "29/11/2026" not in solo_venc
    solo_dia = http.get("/mensualidades?dia=29").text
    assert "29/11/2026" in solo_dia and "10/11/2026" not in solo_dia


def test_parametros_basura_no_tumban_la_pagina(base):
    http = _entrar("ana")
    for q in ("mes=zzz", "mes=2026-99", "estatus=x", "dia=-4", "dia=99", "q=" + "a" * 500):
        assert http.get(f"/mensualidades?{q}").status_code == 200, q


def test_busqueda_no_interpreta_html(base):
    with Session(base) as s:
        _cliente(s, "<script>alert(1)</script>", ["ana"])
        s.commit()
    html = _entrar("ana").get("/mensualidades?q=%3Cscript%3E").text
    assert "<script>alert" not in html


def test_por_completar_lista_lo_que_falta(base):
    with Session(base) as s:
        _cliente(s, "Incompleto", ["ana"], completo=False)
        _cliente(s, "Completo", ["ana"])
        s.commit()
    html = _entrar("ana").get("/mensualidades").text
    assert "Incompleto" in html and "Primer pago" in html
    seccion = html.split("<h2>Por completar</h2>")[1]
    assert "<strong>Completo</strong>" not in seccion


def test_cliente_dado_de_baja_no_aparece(base):
    with Session(base) as s:
        c = _cliente(s, "De Baja", ["ana"], habilitado=False)
        _mens(s, c, date(2026, 11, 20))
        s.commit()
    assert "De Baja" not in _entrar("ana").get("/mensualidades").text


def test_cada_estatus_emitido_tiene_su_css():
    css = Path("static/css/mensualidades.css").read_text()
    for est in dom.ESTATUS:
        assert f".badge.{est}" in css, est


def test_el_menu_enlaza_el_modulo(base):
    assert 'href="/mensualidades"' in _entrar("ana").get("/mensualidades").text
