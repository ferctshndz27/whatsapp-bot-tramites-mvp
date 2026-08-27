"""Fase 3a — horario de atención, aviso fuera de horario y carga por asesor.

Lo que se protege aquí no es el formato de una cadena: es que el bot deje de
prometer «en breve» cuando no hay nadie, y que el panel pueda decir dónde está
parado el trabajo antes de que la fase 3b empiece a repartirlo.
"""

from datetime import datetime

import bcrypt
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine, select

import app.db as db
from app import horario
from app.activity import carga_abierta
from app.config import settings
from app.db import get_session
from app.models import Ticket, TicketStatus, User
from app.templating import BUSINESS_TZ

CLAVE = "clave-de-prueba"


def _t(anio, mes, dia, hora, minuto=0) -> datetime:
    return datetime(anio, mes, dia, hora, minuto, tzinfo=BUSINESS_TZ)


# --- El horario -------------------------------------------------------------


@pytest.mark.parametrize(
    "momento, esperado, porque",
    [
        (_t(2026, 8, 24, 10, 0), True, "lunes a media mañana"),
        (_t(2026, 8, 24, 8, 0), True, "el minuto exacto de apertura ya cuenta"),
        (_t(2026, 8, 24, 7, 59), False, "un minuto antes de abrir, no"),
        (_t(2026, 8, 24, 19, 50), False, "el cierre es exclusivo: a las 19:50 ya cerró"),
        (_t(2026, 8, 24, 2, 0), False, "madrugada de lunes"),
        (_t(2026, 8, 29, 16, 0), True, "sábado por la tarde: el negocio SÍ atiende"),
        (_t(2026, 8, 29, 18, 0), False, "sábado después de las 18:00"),
        (_t(2026, 8, 30, 11, 0), False, "domingo no se abre"),
        (_t(2026, 9, 16, 11, 0), False, "16 de septiembre es excepción aunque caiga entre semana"),
    ],
)
def test_abierto_respeta_semana_bordes_y_excepciones(momento, esperado, porque):
    assert horario.abierto(momento) is esperado, porque


def test_una_excepcion_manda_sobre_el_horario_semanal():
    """El 16-sep-2026 es miércoles: sin la excepción estaría abierto."""
    assert horario.abierto(_t(2026, 9, 15, 11, 0)) is True, "martes normal, abierto"
    assert horario.abierto(_t(2026, 9, 16, 11, 0)) is False, "la excepción no se aplicó"


def test_sin_archivo_de_horario_se_asume_abierto(monkeypatch):
    """Prometer atención que llega tarde es mucho menos grave que decirle a un
    cliente que estamos cerrados cuando sí hay quien lo atienda."""
    monkeypatch.setattr(horario, "HORARIO", {})
    assert horario.abierto(_t(2026, 8, 30, 3, 0)) is True
    assert horario.aviso(_t(2026, 8, 30, 3, 0)) is None


def test_la_proxima_apertura_salta_el_fin_de_semana_completo():
    # Sábado 19:00 (cerró a las 18:00) → el domingo no abre → lunes 08:00.
    apertura = horario.proxima_apertura(_t(2026, 8, 29, 19, 0))
    assert apertura == _t(2026, 8, 31, 8, 0)


def test_la_proxima_apertura_salta_un_festivo():
    # Martes 15-sep 19:00 → miércoles 16 es excepción → jueves 17 a las 09:00.
    assert horario.proxima_apertura(_t(2026, 9, 15, 20, 0)) == _t(2026, 9, 17, 8, 0)


def test_el_aviso_dice_cuando_y_no_solo_que_esta_cerrado():
    """Un «estamos cerrados» a secas deja al cliente sin saber a qué atenerse:
    la queja que esto resuelve es de expectativas, no de disponibilidad."""
    aviso = horario.aviso(_t(2026, 8, 29, 19, 0))  # sábado, ya cerró (18:00)
    assert aviso is not None
    assert "lunes" in aviso and "08:00" in aviso


def test_dentro_de_horario_no_se_agrega_nada():
    assert horario.aviso(_t(2026, 8, 24, 10, 0)) is None


def test_un_horario_corrupto_no_tumba_el_dia_entero(monkeypatch):
    """Un rango mal escrito se descarta con log; los demás del día siguen."""
    monkeypatch.setattr(
        horario,
        "HORARIO",
        {"semana": {"0": [["nueve", "19:50"], ["08:00", "19:50"]]}, "excepciones": {}},
    )
    assert horario.abierto(_t(2026, 8, 24, 10, 0)) is True


# --- El aviso pegado a la promesa del bot -----------------------------------


def test_el_bot_no_promete_en_breve_fuera_de_horario(monkeypatch):
    """`_escalate` promete «un asesor te contactará en breve». A las 3 de la
    mañana de un domingo eso es falso, y era la promesa más explícita del flujo.
    """
    from app import conversation

    monkeypatch.setattr(horario, "ahora", lambda: _t(2026, 8, 30, 3, 0))
    texto = conversation._con_horario("Un asesor te contactará en breve.")
    assert "fuera de horario" in texto
    assert "ya quedó registrada" in texto, "hay que decir que el folio SÍ se abrió"


def test_dentro_de_horario_el_mensaje_queda_intacto(monkeypatch):
    from app import conversation

    monkeypatch.setattr(horario, "ahora", lambda: _t(2026, 8, 24, 10, 0))
    assert conversation._con_horario("Un asesor te contactará en breve.") == (
        "Un asesor te contactará en breve."
    )


# --- La carga por asesor ----------------------------------------------------


@pytest.fixture()
def entorno(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    with get_session() as s:
        s.add(
            User(
                username="admin",
                password_hash=bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt()).decode(),
                is_admin=True,
            )
        )
        s.commit()
    yield engine


def _ticket(**kw) -> Ticket:
    base = dict(
        phone="5215500000000",
        category="c",
        service_id="s",
        service_name="S",
        price_info="$1",
        status=TicketStatus.NUEVO,
    )
    base.update(kw)
    return Ticket(**base)


def test_la_carga_cuenta_folios_vivos_por_asesor(entorno):
    with get_session() as s:
        s.add(_ticket(assigned_to="ana"))
        s.add(_ticket(assigned_to="ana", status=TicketStatus.TOMADO))
        s.add(_ticket(assigned_to="beto"))
        s.commit()

    with get_session() as s:
        carga = carga_abierta(s)
    assert carga.get("ana") == 2
    assert carga.get("beto") == 1


def test_un_folio_cerrado_deja_de_pesar(entorno):
    """El tope de la fase 3b limita trabajo VIVO. Contar cierres haría que un
    asesor productivo se viera saturado justo por serlo."""
    with get_session() as s:
        s.add(_ticket(assigned_to="ana", status=TicketStatus.CERRADO))
        s.add(_ticket(assigned_to="ana"))
        s.commit()

    with get_session() as s:
        assert carga_abierta(s).get("ana") == 1


def test_un_folio_oculto_no_pesa(entorno):
    with get_session() as s:
        s.add(_ticket(assigned_to="ana", habilitado=False))
        s.commit()

    with get_session() as s:
        assert carga_abierta(s).get("ana", 0) == 0


def test_el_trabajo_sin_dueno_se_reporta_bajo_la_llave_none(entorno):
    """Hoy «sin asignar» ES el pendiente. En la fase 3b pasará a ser el
    desbordamiento del tope, así que tiene que seguir siendo visible aparte."""
    with get_session() as s:
        s.add(_ticket())
        s.add(_ticket())
        s.add(_ticket(assigned_to="ana"))
        s.commit()

    with get_session() as s:
        carga = carga_abierta(s)
    assert carga.get(None) == 2
    assert carga.get("ana") == 1


def test_el_panel_muestra_la_carga_y_los_folios_sin_dueno(entorno):
    from fastapi.testclient import TestClient

    from app.main import app

    with get_session() as s:
        s.add(_ticket(assigned_to="admin"))
        s.add(_ticket())
        s.commit()

    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post(
        "/login", data={"username": "admin", "password": CLAVE}, follow_redirects=False
    )
    assert r.status_code == 303, "el login de la prueba falló"
    html = cliente.get("/activity").text
    assert "Abiertos" in html, "falta la columna de carga"
    # Los NÚMEROS, no las cadenas fijas de la plantilla: «Abiertos» es un <th>
    # que está siempre, y «sin asesor» aparece también en la rama que dice que
    # NO hay ninguno. Sin estos asserts el test pasaba con la carga en blanco.
    assert "Hay <strong>1</strong> folio" in html, "no reportó el folio sin dueño"
    assert "<td><strong>1</strong></td>" in html, "no reportó la carga del asesor"


def test_los_folios_de_un_deshabilitado_no_se_cuentan_como_cubiertos(entorno):
    """Un colaborador deshabilitado no puede entrar, así que sus folios vivos no
    los va a mover nadie. Contarlos como atendidos es el mismo bug que el espejo
    que se declaraba «al día»: trabajo huérfano presentado como cubierto."""
    from fastapi.testclient import TestClient

    from app.main import app

    with get_session() as s:
        s.add(User(username="beto", password_hash="x", habilitado=False))
        s.commit()
        s.add(_ticket(assigned_to="beto"))
        s.add(_ticket(assigned_to="beto"))
        s.commit()

    cliente = TestClient(app, base_url="https://testserver")
    cliente.post(
        "/login", data={"username": "admin", "password": CLAVE}, follow_redirects=False
    )
    html = cliente.get("/activity").text
    assert "deshabilitado" in html, "la fila del deshabilitado no está marcada"
    assert "Además hay <strong>2</strong> folio" in html, (
        "los folios de un deshabilitado se están contando como cubiertos"
    )


def test_un_admin_puede_rescatar_el_folio_de_un_deshabilitado(entorno):
    """Sin esto los folios quedaban congelados PARA SIEMPRE: su dueño ya no
    puede entrar a soltarlos y `claim` solo toma folios sin dueño. La única
    salida era editar SQLite a mano."""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.models import TicketActivity

    with get_session() as s:
        s.add(User(username="beto", password_hash="x", habilitado=False))
        s.commit()
        s.add(_ticket(assigned_to="beto"))
        s.commit()

    cliente = TestClient(app, base_url="https://testserver")
    cliente.post(
        "/login", data={"username": "admin", "password": CLAVE}, follow_redirects=False
    )
    r = cliente.post("/tickets/1/release", follow_redirects=False)
    assert r.status_code == 303

    with get_session() as s:
        assert s.get(Ticket, 1).assigned_to is None, "el admin no pudo soltarlo"
        acciones = [a.action for a in s.exec(select(TicketActivity)).all()]
    assert any("soltado-por-admin:beto" in a for a in acciones), (
        "el histórico no dice quién perdió el folio ni quién lo soltó"
    )


# --- Que el aviso llegue DE VERDAD al cliente -------------------------------
#
# Las pruebas de arriba llaman a los helpers directo. Una mutación demostró que
# desconectar los CINCO puntos de emisión dejaba la suite entera idéntica: el
# feature se podía apagar sin que nadie se enterara. Estas pasan por
# handle_incoming(), que es el camino real.


@pytest.fixture()
def conversacion(monkeypatch):
    from sqlmodel import Session

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    with Session(engine) as s:
        yield s


def _fuera_de_horario(monkeypatch):
    monkeypatch.setattr(horario, "ahora", lambda: _t(2026, 8, 30, 3, 0))  # domingo 03:00


def _dentro_de_horario(monkeypatch):
    monkeypatch.setattr(horario, "ahora", lambda: _t(2026, 8, 24, 10, 0))  # lunes 10:00


def _pedir_asesor(session) -> str:
    from app.conversation import handle_incoming

    handle_incoming(session, "5215500000000", "hola")
    replies = handle_incoming(session, "5215500000000", "quiero hablar con un asesor")
    return " ".join(replies)


def test_el_cliente_que_escribe_de_madrugada_recibe_el_aviso(conversacion, monkeypatch):
    """Prueba de extremo a extremo: sin ella, desconectar los puntos de emisión
    no rompía una sola prueba de las 1318."""
    _fuera_de_horario(monkeypatch)
    texto = _pedir_asesor(conversacion)
    assert "fuera de horario" in texto, "el aviso no llegó por el camino real"


def test_fuera_de_horario_el_bot_deja_de_decir_en_breve(conversacion, monkeypatch):
    """La promesa se SUSTITUYE, no se acompaña: «en breve le resolvemos» seguido
    de «ahorita estamos fuera de horario» se contradice en la misma línea."""
    _fuera_de_horario(monkeypatch)
    texto = _pedir_asesor(conversacion)
    assert "en breve" not in texto, "sigue prometiendo «en breve» estando cerrado"


def test_dentro_de_horario_el_cliente_no_ve_nada_del_horario(conversacion, monkeypatch):
    """El mensaje normal no se ensucia: dentro de horario queda como siempre."""
    _dentro_de_horario(monkeypatch)
    texto = _pedir_asesor(conversacion)
    assert "fuera de horario" not in texto
    assert "en breve" in texto, "dentro de horario la promesa original debe seguir"


def test_la_lista_de_festivos_no_se_ha_quedado_corta():
    """Candado con fecha de caducidad, a propósito.

    `excepciones` es una lista CERRADA de fechas. El día que se acabe, el bot
    volverá a dar los festivos por abiertos y le prometerá a un cliente que lo
    atienden el 25 de diciembre — sin que nada avise. Esta prueba se pone roja
    con seis meses de anticipación para que dé tiempo de rellenarla.

    Si te encuentras esto en rojo: agrega el siguiente año de días de descanso
    obligatorio (LFT art. 74) en `app/horario.json`. Son 1-ene, primer lunes de
    febrero, tercer lunes de marzo, 1-may, 16-sep, tercer lunes de noviembre y
    25-dic; más el 1-dic cada seis años a partir de 2030 (transmisión del
    Ejecutivo).
    """
    from datetime import date, timedelta

    fechas = sorted(horario.HORARIO.get("excepciones") or {})
    assert fechas, "no hay ni un festivo cargado"
    ultimo = date.fromisoformat(fechas[-1])
    margen = date.today() + timedelta(days=182)
    assert ultimo >= margen, (
        f"los festivos se acaban el {ultimo} y eso ya está a menos de seis meses. "
        f"Agrega el año siguiente en app/horario.json (ver el docstring de esta prueba)."
    )
