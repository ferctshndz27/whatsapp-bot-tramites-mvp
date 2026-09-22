"""El login y el registro del panel, endurecidos (auditoría 2026-09, AUTH-2/3/4/6/7, INJ-4).

Lo que el informe demostró con PoC y aquí se fija:

- AUTH-2: el lockout era por usuario y sin candado. Cinco claves malas a
  «jefa» desde cualquier parte la dejaban fuera cinco minutos (DoS de cuenta);
  30 intentos concurrentes pasaban 28 por bcrypt (límite nominal 5); 28
  intentos desde una misma IP contra usuarios distintos no se frenaban.
- AUTH-7: las llaves nunca se purgaban ni se truncaban; 200 POST anónimos con
  usernames de 500 KB retenían 100 MB.
- AUTH-3: bcrypt solo corría si el usuario existía y estaba habilitado
  (219 ms contra 5 ms): el tiempo decía qué nombres existen.
- AUTH-4: la invitación se marcaba usada DESPUÉS de crear el usuario y sin
  candado: cinco registros concurrentes con el mismo código, tres cuentas.
- AUTH-6: el token vivo de la invitación iba a `AdminAudit.target`, que lee
  cualquier admin de oficina y se espeja a MSSQL.
- INJ-4: el username entraba tal cual a las líneas de log (CRLF = renglones
  inventados en la bitácora).

Las de concurrencia usan hilos reales con una barrera, como test_concurrency.py:
un `sleep` no sincroniza nada.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import bcrypt
import pytest
from sqlalchemy import event
from sqlmodel import create_engine, select

import app.dashboard as dashboard
import app.db as db
from app import authlog
from app.config import settings
from app.db import get_session
from app.models import AdminAudit, Invitation, LoginEvent, User, utcnow

CLAVE = "clave-de-prueba-1234"
IP_A, IP_B = "203.0.113.10", "203.0.113.20"  # rango de documentación, nunca real


def _request(host: str = IP_A, user: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        session={"user": user} if user else {},
        url=SimpleNamespace(path="/login"),
        query_params={},
        headers={"user-agent": "pytest"},
        client=SimpleNamespace(host=host),
        base_url="https://testserver/",
    )


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch, tmp_path):
    """Engine de ARCHIVO con el listener de PRAGMA real (WAL, busy_timeout), como
    `engine_produccion` en test_concurrency.py: las pruebas de carrera escriben
    la bitácora desde 30 hilos a la vez, y una base en memoria con StaticPool es
    UNA conexión compartida —dos hilos se pisan y sus resultados se pierden—."""
    engine = create_engine(f"sqlite:///{tmp_path}/login.db", connect_args={"check_same_thread": False})
    event.listen(engine, "connect", db._set_sqlite_pragmas)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    monkeypatch.setattr(settings, "registration_code", "")
    dashboard._login_attempts.clear()
    hashed = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
    with get_session() as s:
        s.add(User(username="jefa", password_hash=hashed, is_admin=True))
        s.add(User(username="ana", password_hash=hashed))
        s.add(User(username="baja", password_hash=hashed, habilitado=False))
        s.commit()
    yield
    dashboard._login_attempts.clear()
    engine.dispose()


def _login(username: str, password: str, ip: str = IP_A):
    return dashboard.login_submit(_request(ip), username=username, password=password)


def _entro(respuesta) -> bool:
    return respuesta.status_code == 303


# ---------------------------------------------------------------------------
# AUTH-2: tres llaves
# ---------------------------------------------------------------------------


def test_cinco_fallos_desde_una_ip_no_dejan_fuera_a_la_duena_desde_otra():
    """El DoS de cuenta del informe: antes, quien supiera el nombre de la jefa la
    bloqueaba. Ahora el par (usuario, IP) se bloquea; la jefa, desde SU IP, entra."""
    for _ in range(dashboard.LOGIN_MAX_ATTEMPTS):
        assert not _entro(_login("jefa", "mala", ip=IP_B))
    assert not _entro(_login("jefa", CLAVE, ip=IP_B)), "el atacante siguió probando desde su IP"
    with get_session() as s:
        ultimo = s.exec(select(LoginEvent).order_by(LoginEvent.id.desc())).first()
    assert ultimo.reason == authlog.REASON_LOCKED

    assert _entro(_login("jefa", CLAVE, ip=IP_A)), "bloquearon a la jefa desde otra IP"


def test_una_ip_que_prueba_muchos_usuarios_se_frena(monkeypatch):
    """Spraying: 28 intentos desde la misma IP contra nombres distintos pasaban."""
    monkeypatch.setattr(dashboard, "LOGIN_MAX_POR_IP", 8)
    for i in range(8):
        _login(f"usuario{i}", "mala", ip=IP_B)
    assert not _entro(_login("ana", CLAVE, ip=IP_B)), "la IP del spraying siguió entrando"
    assert _entro(_login("ana", CLAVE, ip=IP_A)), "el bloqueo de una IP alcanzó a otra"


def test_los_logins_correctos_no_cuentan_como_fallos():
    """La primera versión dejaba la reserva en la llave de la IP y veinte logins
    CORRECTOS desde una misma IP cerraban el panel (lo cazó la suite). Un
    acierto no es un fallo en ninguna de las tres llaves."""
    for _ in range(dashboard.LOGIN_MAX_POR_IP + 5):
        assert _entro(_login("ana", CLAVE)), "los aciertos acumulados bloquearon la IP"
    assert not dashboard._login_attempts, f"quedaron reservas tras aciertos: {dashboard._login_attempts}"


def test_el_respaldo_por_usuario_frena_un_ataque_repartido(monkeypatch):
    monkeypatch.setattr(dashboard, "LOGIN_MAX_POR_USUARIO", 6)
    for i in range(6):
        _login("jefa", "mala", ip=f"198.51.100.{i}")  # una IP distinta cada vez
    assert not _entro(_login("jefa", CLAVE, ip="198.51.100.99")), "seis IPs distintas no toparon el respaldo"


def test_treinta_intentos_concurrentes_solo_pasan_el_cupo_por_bcrypt(monkeypatch):
    """La carrera del informe: chequeo y registro eran dos pasos. Con la reserva
    atómica, de 30 hilos exactamente LOGIN_MAX_ATTEMPTS llegan a bcrypt."""
    evaluados: list[str] = []
    real = bcrypt.checkpw

    def _espia(clave, hash_):
        evaluados.append("x")
        return real(clave, hash_)

    monkeypatch.setattr(dashboard.bcrypt, "checkpw", _espia)
    barrera = threading.Barrier(30, timeout=20)
    resultados: list[int] = []
    errores: list[BaseException] = []

    def intento():
        barrera.wait()
        try:
            resultados.append(_login("jefa", "mala").status_code)
        except BaseException as exc:  # noqa: BLE001 — un hilo que muere en silencio es un falso verde
            errores.append(exc)

    hilos = [threading.Thread(target=intento) for _ in range(30)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)
    assert not errores, errores
    assert len(resultados) == 30
    assert len(evaluados) == dashboard.LOGIN_MAX_ATTEMPTS, (
        f"{len(evaluados)} intentos llegaron a bcrypt; el cupo es {dashboard.LOGIN_MAX_ATTEMPTS}"
    )


def test_la_reserva_es_atomica_a_nivel_de_helper():
    """Sin bcrypt ni base: 50 hilos piden permiso a la vez para el mismo par y
    exactamente el cupo lo obtiene."""
    barrera = threading.Barrier(50, timeout=20)
    permisos: list[float | None] = []
    candado = threading.Lock()

    def pedir():
        barrera.wait()
        r = dashboard._intento_permitido("jefa", IP_A)
        with candado:
            permisos.append(r)

    hilos = [threading.Thread(target=pedir) for _ in range(50)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)
    assert sum(1 for p in permisos if p is not None) == dashboard.LOGIN_MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# AUTH-7: memoria acotada
# ---------------------------------------------------------------------------


def test_la_llave_se_trunca_y_las_viejas_se_purgan(monkeypatch):
    """200 usernames de 500 KB retenían 100 MB. Ahora la llave se corta a
    _LLAVE_MAX y, pasado el umbral, un fallo nuevo barre las llaves caducadas."""
    # El reloj se congela ANTES de la primera llave: una registrada con la hora real
    # nunca «caduca» frente a un reloj falso que va un año atrás.
    reloj = [1_000_000.0]
    monkeypatch.setattr(dashboard.time, "time", lambda: reloj[0])
    enorme = "u" * 500_000
    dashboard._record_failed_login(enorme, IP_A)
    assert all(len(v) <= dashboard._LLAVE_MAX + len("|") + len(IP_A) for _, v in dashboard._login_attempts), (
        "una llave conserva el username completo"
    )

    for i in range(dashboard._PURGA_A_PARTIR_DE + 10):
        dashboard._record_failed_login(f"viejo{i}", f"10.0.{i // 250}.{i % 250}")
    assert len(dashboard._login_attempts) > dashboard._PURGA_A_PARTIR_DE
    reloj[0] += dashboard.LOGIN_WINDOW_SECONDS + 1  # todo lo anterior caducó
    dashboard._record_failed_login("nuevo", IP_A)
    assert len(dashboard._login_attempts) == 3, (
        f"la purga no barrió las llaves caducadas: quedan {len(dashboard._login_attempts)}"
    )


# ---------------------------------------------------------------------------
# AUTH-3: tiempo constante
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("username", ["nadie", "baja"])
def test_bcrypt_corre_aunque_el_usuario_no_exista_o_este_de_baja(monkeypatch, username):
    """Antes, con usuario inexistente o deshabilitado no se evaluaba nada y la
    respuesta tardaba 40 veces menos: el tiempo enumeraba usuarios."""
    llamadas: list[bytes] = []
    real = bcrypt.checkpw
    monkeypatch.setattr(dashboard.bcrypt, "checkpw", lambda c, h: llamadas.append(h) or real(c, h))

    r = _login(username, "loquesea")

    assert not _entro(r)
    assert llamadas == [dashboard._HASH_SENUELO], "no se evaluó la clave contra el hash señuelo"
    with get_session() as s:
        evento = s.exec(select(LoginEvent)).one()
    esperado = authlog.REASON_NO_USER if username == "nadie" else authlog.REASON_DISABLED
    assert evento.reason == esperado, "el motivo real dejó de llegar a la bitácora"


# ---------------------------------------------------------------------------
# AUTH-4: la invitación se consume una vez
# ---------------------------------------------------------------------------


def test_cinco_registros_concurrentes_con_el_mismo_codigo_crean_una_sola_cuenta():
    with get_session() as s:
        s.add(Invitation(token="unica-2026", created_by="jefa"))
        s.commit()
    barrera = threading.Barrier(5, timeout=20)
    codigos: list[int] = []

    def registrar(i: int):
        barrera.wait()
        r = dashboard.register_submit(
            _request(host=f"198.51.100.{i}"), username=f"colado{i}", password=CLAVE,
            code="unica-2026", phone=f"33 1234 567{i}",
        )
        codigos.append(r.status_code)

    hilos = [threading.Thread(target=registrar, args=(i,)) for i in range(5)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=30)

    with get_session() as s:
        creados = s.exec(select(User).where(User.username.like("colado%"))).all()
        inv = s.exec(select(Invitation).where(Invitation.token == "unica-2026")).one()
    assert len(creados) == 1, f"una invitación creó {len(creados)} cuentas: {[u.username for u in creados]}"
    assert inv.used_by == creados[0].username
    assert codigos.count(303) == 1 and codigos.count(200) == 4, codigos


def test_una_invitacion_valida_sigue_registrando_y_consumiendose():
    """Control positivo del UPDATE condicionado: el camino normal no se rompió."""
    with get_session() as s:
        s.add(Invitation(token="normal-2026", created_by="jefa", office_id=1))
        s.commit()
    r = dashboard.register_submit(_request(), username="nueva", password=CLAVE, code="normal-2026", phone="3312345678")
    assert _entro(r)
    with get_session() as s:
        inv = s.exec(select(Invitation).where(Invitation.token == "normal-2026")).one()
        assert inv.used_at is not None and inv.used_by == "nueva" and inv.phone == "5213312345678"
        assert s.exec(select(User).where(User.username == "nueva")).one().office_id == 1


# ---------------------------------------------------------------------------
# AUTH-6: el token no va a la bitácora
# ---------------------------------------------------------------------------


def test_cancelar_una_invitacion_no_escribe_el_token_en_la_bitacora():
    """Un admin de oficina 1 leyó en /audit un código vivo de la oficina 2 y se
    registró allá. La bitácora lleva el id; el token, nunca."""
    with get_session() as s:
        inv = Invitation(token="secreto-vivo-2026", created_by="jefa")
        s.add(inv)
        s.commit()
        s.refresh(inv)
    dashboard.invitations_toggle(_request(user="jefa"), inv.id)
    with get_session() as s:
        filas = s.exec(select(AdminAudit)).all()
    assert filas, "el toggle no dejó rastro en la bitácora"
    assert all("secreto-vivo-2026" not in (f.target or "") for f in filas), "el token vivo quedó en AdminAudit"
    assert any(f.target == f"#{inv.id}" for f in filas)


# ---------------------------------------------------------------------------
# INJ-4: el username no inventa renglones en el log
# ---------------------------------------------------------------------------


def test_un_username_con_saltos_de_linea_no_parte_la_linea_de_log(caplog):
    with caplog.at_level(logging.WARNING, logger="app.dashboard"):
        _login("jefa\r\nWARNING inventado", "mala")
    lineas = [r.getMessage() for r in caplog.records if "Login fallido" in r.getMessage()]
    assert lineas, "no se registró el fallo"
    assert all("\n" not in l and "\r" not in l for l in lineas), lineas
    assert "\\r\\n" in lineas[0], "el CRLF no quedó escapado (repr)"
