"""Captura de los datos de cobro del titular y generación de sus mensualidades.

REGLAS: solo un admin captura; todo o nada (un campo malo no escribe ninguno); la generación es
idempotente, una sola mensualidad por titular y periodo sin importar cuántas personas tenga, y
cambiar importe o día solo toca lo futuro sin pagar. Datos sintéticos: nada real.
"""

from datetime import date, datetime

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import mensualidades as dom
from app import mensualidades_captura as cap
from app.config import settings
from app.main import app
from app.models import (
    AdminAudit,
    ClienteMensualidad,
    CuentaCobranza,
    Mensualidad,
    MensualidadAsesor,
    OFICINA_TLAXCALA_ID,
    User,
)

HOY = date(2026, 11, 18)
CLAVE = "clave-de-prueba"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


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
        s.add(User(username="otra", password_hash=HASH_CLAVE, office_id=2))
        s.commit()
    return engine


def _cuenta(s, codigo="7", personas=1, asesores=("ana",), **kw):
    c = CuentaCobranza(codigo=codigo, office_id=kw.pop("office", OFICINA_TLAXCALA_ID), **kw)
    s.add(c)
    s.flush()
    for i in range(personas):
        s.add(ClienteMensualidad(nombre=f"Persona {codigo}-{i}", cuenta_id=c.id))
    for i, u in enumerate(asesores):
        s.add(
            MensualidadAsesor(
                cuenta_id=c.id, username=u, rol="responsable" if i == 0 else "colaborador"
            )
        )
    s.commit()
    s.refresh(c)  # el id sigue legible cuando la sesión del test ya se cerró
    return c


def _entrar(usuario):
    http = TestClient(app, base_url="https://testserver")
    r = http.post("/login", data={"username": usuario, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return http


def _datos(**kw):
    base = {"whatsapp": "", "importe": "", "dia_pago": "", "primer_pago": "", "responsable": ""}
    return {**base, **kw}


def _guardar(usuario, cuenta_id, **kw):
    return _entrar(usuario).post(
        f"/mensualidades/cuentas/{cuenta_id}/datos", data=_datos(**kw), follow_redirects=False
    )


def _mensualidades(engine, cuenta_id):
    with Session(engine) as s:
        return s.exec(
            select(Mensualidad)
            .where(Mensualidad.cuenta_id == cuenta_id)
            .where(Mensualidad.habilitado == True)  # noqa: E712
            .order_by(Mensualidad.periodo)
        ).all()


# ---------- reglas puras ----------


@pytest.mark.parametrize(
    "texto,centavos",
    [("1250.50", 125050), ("$1,250", 125000), (" 99 ", 9900), ("0.01", 1)],
)
def test_importe_se_lee_en_pesos_y_se_guarda_en_centavos(texto, centavos):
    assert cap.parse_importe(texto) == centavos


@pytest.mark.parametrize("malo", ["", "abc", "0", "-5", "10.555", "NaN", "Infinity", "1e400", "99999999"])
def test_importe_malo_se_rechaza(malo):
    with pytest.raises(cap.DatoInvalido):
        cap.parse_importe(malo)


@pytest.mark.parametrize("malo", ["0", "32", "x", "", "1.5"])
def test_dia_fuera_de_rango_se_rechaza(malo):
    with pytest.raises(cap.DatoInvalido):
        cap.parse_dia(malo)


def test_el_primer_mes_se_salta_si_su_vencimiento_cae_antes_del_primer_pago():
    # Primer pago el 20, día de pago 10: el 10 de ese mes ya pasó para este titular.
    ps = dom.periodos_a_generar(date(2026, 11, 20), 10, HOY)
    assert ps[0] == ("2026-12", date(2026, 12, 10))


def test_la_ventana_llega_a_doce_meses_despues_de_hoy_y_recorta_mes_corto():
    ps = dict(dom.periodos_a_generar(date(2026, 11, 1), 31, HOY))
    assert min(ps) == "2026-11" and max(ps) == "2027-11"
    assert ps["2027-02"] == date(2027, 2, 28)


# ---------- la generación ----------


def test_una_mensualidad_por_titular_y_periodo_sin_importar_las_personas(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, personas=3, importe_centavos=50000, dia_pago=10, primer_pago=date(2026, 11, 10))
        n = cap.generar(s, c, HOY)
        s.commit()
    periodos = [m.periodo for m in _mensualidades(base, c.id)]
    assert n == len(periodos) == 13 and len(set(periodos)) == 13


def test_generar_dos_veces_no_duplica(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, importe_centavos=50000, dia_pago=10, primer_pago=date(2026, 11, 10))
        cap.generar(s, c, HOY)
        s.commit()
        assert cap.generar(s, c, HOY) == 0
        s.commit()
    assert len(_mensualidades(base, c.id)) == 13


@pytest.mark.parametrize("falta", ["importe_centavos", "dia_pago", "primer_pago"])
def test_sin_un_dato_de_cobro_no_se_genera_nada(base, falta):
    datos = {"importe_centavos": 50000, "dia_pago": 10, "primer_pago": date(2026, 11, 10)}
    datos[falta] = None
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, **datos)
        assert cap.generar(s, c, HOY) == 0


def test_un_titular_sin_personas_vivas_no_genera(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, personas=0, importe_centavos=50000, dia_pago=10, primer_pago=date(2026, 11, 10))
        assert cap.generar(s, c, HOY) == 0
        s.add(ClienteMensualidad(nombre="De baja", cuenta_id=c.id, baja_at=datetime(2026, 11, 1)))
        s.flush()
        assert cap.generar(s, c, HOY) == 0, "una persona dada de baja no cuenta como viva"


def test_cambiar_el_importe_toca_solo_lo_futuro_sin_pagar(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, importe_centavos=50000, dia_pago=10, primer_pago=date(2026, 9, 10))
        cap.generar(s, c, HOY)
        s.commit()
        for m in s.exec(select(Mensualidad).where(Mensualidad.periodo == "2026-12")).all():
            m.pagado_at = datetime(2026, 11, 2)  # pagada por adelantado
            s.add(m)
        s.commit()
        c.importe_centavos = 80000
        cap.generar(s, c, HOY)
        s.commit()
    por = {m.periodo: m for m in _mensualidades(base, c.id)}
    assert por["2026-09"].importe_centavos == 50000, "lo vencido conserva su importe"
    assert por["2026-12"].importe_centavos == 50000, "lo pagado conserva su importe"
    assert por["2027-01"].importe_centavos == 80000, "lo futuro sin pagar toma el nuevo"


def test_mover_el_primer_pago_adelante_apaga_lo_futuro_que_ya_no_toca(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, importe_centavos=50000, dia_pago=10, primer_pago=date(2026, 12, 10))
        cap.generar(s, c, HOY)
        s.commit()
        c.primer_pago = date(2027, 2, 10)
        cap.generar(s, c, HOY)
        s.commit()
    assert min(m.periodo for m in _mensualidades(base, c.id)) == "2027-02"


# ---------- la ruta ----------


def test_un_asesor_con_la_cuenta_en_su_cartera_no_puede_capturar(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, asesores=("ana",))
    r = _guardar("ana", c.id, importe="900", dia_pago="5", primer_pago="2026-11-05", responsable="beto")
    assert r.status_code == 303 and "err=permiso" in r.headers["location"]
    with Session(base, expire_on_commit=False) as s:
        cc = s.get(CuentaCobranza, c.id)
        assert (cc.importe_centavos, cc.dia_pago, cc.primer_pago) == (None, None, None)
        assert s.exec(select(MensualidadAsesor).where(MensualidadAsesor.username == "beto")).first() is None
    assert _mensualidades(base, c.id) == []


def test_una_cuenta_ajena_o_inexistente_contesta_lo_mismo_404(base):
    with Session(base, expire_on_commit=False) as s:
        ajena = _cuenta(s, codigo="8", asesores=("beto",))
    assert _guardar("ana", ajena.id, importe="900").status_code == 404
    assert _guardar("ana", 99999, importe="900").status_code == 404


def test_un_admin_de_otra_oficina_no_toca_la_cuenta(base):
    with Session(base, expire_on_commit=False) as s:
        s.add(User(username="admin2", password_hash=HASH_CLAVE, is_admin=True, solo_su_oficina=True, office_id=2))
        c = _cuenta(s)
        s.commit()
    assert _guardar("admin2", c.id, importe="900").status_code == 404


def test_el_admin_captura_genera_y_deja_responsable(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, asesores=())
    r = _guardar(
        "root", c.id, whatsapp="55 1234 5678", importe="1,250.50", dia_pago="10",
        primer_pago="2026-11-10", responsable="beto",
    )
    assert r.status_code == 303 and "ok=13" in r.headers["location"]
    with Session(base, expire_on_commit=False) as s:
        cc = s.get(CuentaCobranza, c.id)
        assert (cc.importe_centavos, cc.dia_pago, cc.primer_pago) == (125050, 10, date(2026, 11, 10))
        assert cc.whatsapp == "525512345678"
        resp = s.exec(select(MensualidadAsesor).where(MensualidadAsesor.rol == "responsable")).all()
        assert [(r.cuenta_id, r.username) for r in resp] == [(c.id, "beto")]
    assert len(_mensualidades(base, c.id)) == 13
    assert all(m.importe_centavos == 125050 for m in _mensualidades(base, c.id))


def test_un_campo_malo_no_escribe_ninguno(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, asesores=())
    r = _guardar("root", c.id, importe="900", dia_pago="45", primer_pago="2026-11-10")
    assert "err=dia" in r.headers["location"]
    with Session(base, expire_on_commit=False) as s:
        cc = s.get(CuentaCobranza, c.id)
        assert (cc.importe_centavos, cc.dia_pago, cc.primer_pago) == (None, None, None)
    assert _mensualidades(base, c.id) == []


def test_un_responsable_de_otra_oficina_o_inexistente_se_rechaza(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, asesores=())
    for quien in ("otra", "fantasma"):
        r = _guardar("root", c.id, importe="900", responsable=quien)
        assert "err=responsable" in r.headers["location"]
    with Session(base, expire_on_commit=False) as s:
        assert s.get(CuentaCobranza, c.id).importe_centavos is None
        assert s.exec(select(MensualidadAsesor).where(MensualidadAsesor.username == "otra")).first() is None


def test_cambiar_de_responsable_deja_uno_solo_vivo_y_conserva_el_historial(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, asesores=("ana", "beto"))  # ana responsable, beto colaborador
    _guardar("root", c.id, responsable="beto")
    with Session(base, expire_on_commit=False) as s:
        filas = s.exec(select(MensualidadAsesor).where(MensualidadAsesor.cuenta_id == c.id)).all()
        vivos = {f.username: f.rol for f in filas if f.habilitado}
        assert vivos == {"beto": "responsable"}, "beto sube, ana sale y no hay dos responsables"
        assert any(f.username == "ana" and not f.habilitado for f in filas), "ana queda en historial"


def test_la_captura_no_toca_a_otro_titular(base):
    with Session(base, expire_on_commit=False) as s:
        a = _cuenta(s, codigo="1", asesores=())
        b = _cuenta(s, codigo="2", asesores=(), importe_centavos=1, dia_pago=1, primer_pago=date(2026, 11, 1))
    _guardar("root", a.id, importe="900", dia_pago="5", primer_pago="2026-11-05")
    with Session(base, expire_on_commit=False) as s:
        assert s.get(CuentaCobranza, b.id).importe_centavos == 1
    assert _mensualidades(base, b.id) == []


def test_la_bitacora_dice_que_campos_cambiaron_pero_no_sus_valores(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, codigo="42", asesores=())
    _guardar("root", c.id, whatsapp="5512345678", importe="777.77")
    with Session(base, expire_on_commit=False) as s:
        a = s.exec(select(AdminAudit).where(AdminAudit.action == "mensualidades-datos")).one()
    assert a.admin == "root" and "42" in a.target and "importe" in a.target
    assert "777" not in a.target and "5512" not in a.target


def test_el_mensaje_de_error_sale_del_codigo_no_de_la_url(base):
    html = _entrar("root").get("/mensualidades?err=Llama+al+555-0000").text
    assert "Llama" not in html
    assert cap.MENSAJES["dia"] in _entrar("root").get("/mensualidades?err=dia").text


def test_el_formulario_solo_lo_ve_el_admin(base):
    with Session(base, expire_on_commit=False) as s:
        c = _cuenta(s, asesores=("ana",))
    assert f"/mensualidades/cuentas/{c.id}/datos" in _entrar("root").get("/mensualidades").text
    assert "/datos" not in _entrar("ana").get("/mensualidades").text
