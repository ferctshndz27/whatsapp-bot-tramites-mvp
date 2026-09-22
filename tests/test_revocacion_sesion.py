"""La sesión del panel se revoca del lado del servidor (auditoría 2026-09, AUTH-1).

La cookie de Starlette es un dato FIRMADO que vive en el navegador: prueba que
alguna vez se inició sesión, no que el acceso siga en pie. Antes de este cambio
dar de baja a alguien solo le cerraba el login NUEVO; la cookie que ya traía le
conservaba hasta 12 h de acceso completo —CURP, RFC y NSS de clientes, enviar
WhatsApp como el negocio, generar cobros—, y a un admin, seguir creando admins.

Todas las pruebas inician sesión de verdad (POST /login). No es un detalle: el
guardia solo corre `if request.session.get("user")`, así que un cliente anónimo
lo haría pasar con el arreglo revertido — tests/ ya tuvo una prueba de
middleware verde por esa razón.

Cada regla se prueba por el único camino que solo ella puede producir:

- `habilitado` → se apaga en la base, con la websession todavía abierta.
- `ended_at`   → la cookie se copia ANTES del logout; el usuario sigue habilitado.
- reset / baja → por la ruta real de /users, con la cookie en manos de otro.
- no resucitar → a nivel de helper: con el guardia puesto, ese punto de
  `touch_session` ya no se alcanza por HTTP, y una prueba que no lo ejercita no
  lo protege.

Mutaciones aplicadas y vistas en rojo (se revierten; ver el documento de FTD):
quitar el guardia, quitarle cada una de sus dos condiciones, volver a poner
`ended_at = None`, quitar `revocar_sesiones` de cada ruta, dejar pasar el
request cuando la base no contesta, hacer el commit del hash ANTES de revocar,
y quitar la re-verificación del stream SSE.

Dos de estas pruebas nacieron de una refutación adversarial del primer arreglo
(2026-09-21), que reprodujo ambos huecos ejecutándolos: la revocación corría en
una segunda transacción y un fallo ahí dejaba la contraseña nueva guardada con
la cookie vieja viva; y el stream del CRM ya abierto seguía entregando avisos
después de la baja.
"""

from __future__ import annotations

import asyncio

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine, select

import app.authlog as authlog
import app.db as db
import app.users as users
from app import eventos
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import User, WebSession

JEFA = "jefa"    # admin global que sigue en la empresa
JEFE2 = "jefe2"  # admin global al que dan de baja: el PoC del informe
ANA = "ana"      # colaboradora

CLAVE = "clave-de-prueba-1234"
CLAVE_NUEVA = "otra-clave-distinta-5678"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch):
    """Base propia con StaticPool: el TestClient atiende en otro hilo, y el
    `sqlite://` por defecto le daría a ese hilo una base VACÍA."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")  # apaga archive.enabled()
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with get_session() as s:
        s.add(User(username=JEFA, password_hash=HASH_CLAVE, is_admin=True))
        s.add(User(username=JEFE2, password_hash=HASH_CLAVE, is_admin=True))
        s.add(User(username=ANA, password_hash=HASH_CLAVE, is_admin=False))
        s.commit()
    yield


def _entrar(username: str, clave: str = CLAVE) -> TestClient:
    """Cliente con cookie real. https por el flag Secure de la cookie."""
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post(
        "/login", data={"username": username, "password": clave}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/", "el login de la prueba falló"
    return cliente


def _cookie_de(cliente: TestClient) -> str:
    valor = cliente.cookies.get("session")
    assert valor, "el login no dejó cookie de sesión"
    return valor


def _con_cookie_ajena(cookie: str, path: str):
    """El request de quien COPIÓ la cookie: otro cliente, sin historial propio."""
    with TestClient(app, base_url="https://testserver") as ladron:
        return ladron.get(path, headers={"cookie": f"session={cookie}"}, follow_redirects=False)


def _fuera(respuesta) -> bool:
    return respuesta.status_code == 303 and respuesta.headers["location"].startswith("/login")


def _apagar(username: str) -> None:
    with get_session() as s:
        fila = s.exec(select(User).where(User.username == username)).one()
        fila.habilitado = False
        s.add(fila)
        s.commit()


def _websessions(username: str) -> list[WebSession]:
    with get_session() as s:
        return list(s.exec(select(WebSession).where(WebSession.username == username)).all())


# ---------------------------------------------------------------------------
# El guardia
# ---------------------------------------------------------------------------


def test_quien_sigue_habilitado_sigue_entrando():
    """Control positivo. Sin él, un guardia que sacara a TODO el mundo pasaría
    el resto del archivo: cada prueba de abajo solo afirma que alguien quedó fuera."""
    ana = _entrar(ANA)
    assert ana.get("/my", follow_redirects=False).status_code == 200
    assert ana.get("/my", follow_redirects=False).status_code == 200, "el segundo request lo sacó"
    assert _entrar(JEFA).get("/users", follow_redirects=False).status_code == 200


def test_una_colaboradora_dada_de_baja_pierde_el_acceso_en_su_siguiente_request():
    """Se apaga en la base, con su websession todavía ABIERTA: lo único que puede
    sacarla es que el guardia mire `habilitado`. Si se pone rojo, despedir a
    alguien vuelve a dejarle 12 h de acceso a los datos personales de los clientes."""
    ana = _entrar(ANA)
    assert ana.get("/my", follow_redirects=False).status_code == 200
    _apagar(ANA)
    assert all(ws.ended_at is None for ws in _websessions(ANA)), (
        "la websession se cerró sola: la prueba ya no aísla la regla de `habilitado`"
    )
    assert _fuera(ana.get("/my", follow_redirects=False)), "dada de baja y sigue viendo /my"


def test_un_admin_dado_de_baja_ya_no_crea_administradores():
    """El PoC del informe, por la ruta real. POST /users NO pasa por
    `dashboard._current_user`: lee `request.session` directo, como otras veinte
    rutas. Es la razón de que el guardia viva en el middleware — si alguien lo
    muda a `_current_user`, esta prueba es la que se pone roja."""
    jefe2 = _entrar(JEFE2)
    assert jefe2.get("/users", follow_redirects=False).status_code == 200
    _apagar(JEFE2)
    r = jefe2.post(
        "/users",
        data={"username": "puerta", "password": CLAVE, "role": "admin"},
        follow_redirects=False,
    )
    assert _fuera(r), "el admin dado de baja todavía llegó al handler de alta"
    with get_session() as s:
        assert s.exec(select(User).where(User.username == "puerta")).first() is None, (
            "un admin dado de baja creó otro administrador"
        )


def test_la_cookie_copiada_antes_del_logout_ya_no_revive_la_sesion():
    """Ana sigue habilitada; lo único que cambió es que cerró sesión. Antes,
    `touch_session` volvía a poner `ended_at = None` y la copia servía 12 h más:
    cerrar sesión en una máquina compartida no cerraba nada."""
    ana = _entrar(ANA)
    robada = _cookie_de(ana)
    assert _con_cookie_ajena(robada, "/my").status_code == 200, "la copia no servía ni antes del logout"
    ana.get("/logout", follow_redirects=False)
    assert _fuera(_con_cookie_ajena(robada, "/my")), "la cookie copiada revivió la sesión cerrada"
    assert all(ws.ended_at is not None for ws in _websessions(ANA)), (
        "el request con la cookie copiada reabrió la websession"
    )


def test_una_base_restaurada_no_deja_fuera_a_quien_sigue_habilitado():
    """Websession AUSENTE no es websession cerrada: tras restaurar un respaldo la
    fila no está y el panel entero quedaría fuera si el guardia la exigiera.
    Se recrea, como antes."""
    ana = _entrar(ANA)
    with get_session() as s:
        for ws in s.exec(select(WebSession).where(WebSession.username == ANA)).all():
            authlog._last_touch.pop(ws.token, None)  # que el siguiente request sí registre
            s.delete(ws)
        s.commit()
    assert ana.get("/my", follow_redirects=False).status_code == 200, "la restauración la dejó fuera"
    assert len(_websessions(ANA)) == 1, "la websession no se recreó"


def test_si_la_base_no_contesta_el_request_se_niega_y_la_sesion_sobrevive(monkeypatch):
    """Falla cerrado, sin sacar a todo el mundo: un «database is locked» pasajero
    niega ESE request (503) y el siguiente vuelve a entrar con la misma cookie."""
    ana = _entrar(ANA)

    def _base_caida(_user, _sid):
        raise RuntimeError("database is locked")

    with monkeypatch.context() as m:
        m.setattr(authlog, "sesion_vigente", _base_caida)
        assert ana.get("/my", follow_redirects=False).status_code == 503, (
            "no se pudo verificar la sesión y el request pasó igual"
        )
    assert ana.get("/my", follow_redirects=False).status_code == 200, "el fallo pasajero cerró la sesión"


def test_el_guardia_no_corre_para_anonimos_ni_para_estaticos(monkeypatch):
    """El webhook de WhatsApp —el camino con presión real, en un solo worker—
    no trae sesión y no debe pagar ni una lectura; y una página del panel pide
    además su CSS y su JS, que no deciden acceso."""
    llamadas: list[str] = []
    real = authlog.sesion_vigente

    def _espia(user, sid):
        llamadas.append(user)
        return real(user, sid)

    monkeypatch.setattr(authlog, "sesion_vigente", _espia)
    with TestClient(app, base_url="https://testserver") as anonimo:
        anonimo.get("/login")
        anonimo.get("/health")
    assert llamadas == [], "el guardia leyó la base para un request sin sesión"

    ana = _entrar(ANA)
    llamadas.clear()
    ana.get("/static/css/activity.css")
    assert llamadas == [], "el guardia corrió para un estático"
    ana.get("/my", follow_redirects=False)
    assert llamadas == [ANA], "el espía no ve al guardia: la prueba de arriba no probaría nada"


# ---------------------------------------------------------------------------
# Lo que cierra sesiones
# ---------------------------------------------------------------------------


def test_resetear_la_contrasena_saca_a_quien_ya_estaba_adentro():
    """Una contraseña se resetea porque se filtró; quien la tiene ya está adentro.
    Ana sigue habilitada y nadie cerró sesión: solo el reset puede sacarla."""
    ana = _entrar(ANA)
    robada = _cookie_de(ana)
    jefa = _entrar(JEFA)
    r = jefa.post(f"/users/{ANA}/password", data={"password": CLAVE_NUEVA}, follow_redirects=False)
    assert r.status_code == 200
    assert _fuera(_con_cookie_ajena(robada, "/my")), "la sesión previa sobrevivió al cambio de contraseña"
    assert _entrar(ANA, CLAVE_NUEVA).get("/my", follow_redirects=False).status_code == 200, (
        "con la contraseña nueva tampoco entra: se revocó de más"
    )


def test_quien_se_cambia_la_contrasena_a_si_mismo_conserva_su_sesion():
    """La otra mitad de la regla: sus DEMÁS dispositivos salen, el que usó no.
    Sin esto el admin se sacaría a sí mismo a media operación."""
    otro_dispositivo = _cookie_de(_entrar(JEFA))
    jefa = _entrar(JEFA)
    r = jefa.post(f"/users/{JEFA}/password", data={"password": CLAVE_NUEVA}, follow_redirects=False)
    assert r.status_code == 200
    assert jefa.get("/users", follow_redirects=False).status_code == 200, "se sacó a sí misma"
    assert _fuera(_con_cookie_ajena(otro_dispositivo, "/users")), "su otro dispositivo siguió adentro"


def _hash_de(username: str) -> str:
    with get_session() as s:
        return s.exec(select(User.password_hash).where(User.username == username)).one()


def test_si_la_revocacion_falla_la_contrasena_tampoco_cambia(monkeypatch):
    """O las dos cosas o ninguna. El primer arreglo hacía el commit del hash y
    DESPUÉS abría otra transacción para cerrar las websessions; un «database is
    locked» pasajero en ese segundo paso dejaba la contraseña nueva guardada y la
    cookie robada viva, con un 500 y sin rastro en la bitácora — el hueco AUTH-1c
    reabierto por un fallo transitorio (hallazgo de la refutación). El estado que
    esta prueba prohíbe es «hash cambiado + sesión vieja abierta»; con una sola
    transacción, si la revocación falla el hash se queda como estaba."""
    ana = _entrar(ANA)
    robada = _cookie_de(ana)
    hash_previo = _hash_de(ANA)

    def _base_caida(*_args, **_kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(users, "revocar_sesiones", _base_caida)
    jefa = _entrar(JEFA)
    with pytest.raises(RuntimeError):  # TestClient re-lanza el error del handler
        jefa.post(f"/users/{ANA}/password", data={"password": CLAVE_NUEVA}, follow_redirects=False)

    assert _hash_de(ANA) == hash_previo, (
        "la revocación falló y la contraseña cambió igual: quien tenga la cookie vieja se queda adentro"
    )
    assert _entrar(ANA).get("/my", follow_redirects=False).status_code == 200, (
        "el fallo dejó a Ana sin poder entrar con su contraseña de siempre"
    )
    assert _con_cookie_ajena(robada, "/my").status_code == 200, (
        "nada cambió en la base y sin embargo la sesión se cerró: el estado no es consistente"
    )


def test_rehabilitar_a_alguien_no_le_devuelve_su_sesion_vieja():
    """Baja y alta seguidas, sin que Ana haga un solo request en medio: al final
    está habilitada, así que `habilitado` no la saca. Solo el cierre de sus
    websessions en la baja. Vuelve a entrar con su contraseña, no con la cookie
    que traía cuando la dieron de baja."""
    vieja = _cookie_de(_entrar(ANA))
    jefa = _entrar(JEFA)
    assert jefa.post(f"/users/{ANA}/toggle", follow_redirects=False).status_code == 200
    assert all(ws.ended_at is not None for ws in _websessions(ANA)), "la baja no cerró sus websessions"
    assert jefa.post(f"/users/{ANA}/toggle", follow_redirects=False).status_code == 200
    assert _fuera(_con_cookie_ajena(vieja, "/my")), "la rehabilitación le devolvió la sesión vieja"
    assert _entrar(ANA).get("/my", follow_redirects=False).status_code == 200


# ---------------------------------------------------------------------------
# El stream del CRM: un request que no termina nunca
# ---------------------------------------------------------------------------


def _stream_del_crm(cliente: TestClient, disparar, segundos: float = 5.0) -> tuple[list[str], bool]:
    """Abre /crm/api/eventos por ASGI, corre `disparar()` y devuelve (eventos que
    llegaron, si el stream terminó solo). Misma técnica que tests/test_crm.py:
    TestClient espera a que la respuesta TERMINE y esta no termina nunca."""
    cookie = "; ".join(f"{k}={v}" for k, v in cliente.cookies.items())

    async def principal():
        # sse-starlette 2.4.1 guarda su evento de apagado en una variable de clase y lo
        # reutiliza; cada `asyncio.run()` es un loop nuevo y el segundo stream reventaría con
        # «bound to a different event loop». tests/test_crm.py lo reinicia en una fixture por
        # prueba; aquí una prueba abre DOS streams, así que va por llamada. Y la primera
        # versión de este helper pasaba justo por ese reventón: la tarea moría al instante,
        # `done()` era verdadero y «se cerró solo» salía verde sin re-verificación alguna.
        from sse_starlette.sse import AppStatus

        AppStatus.should_exit_event = None
        AppStatus.should_exit = False
        eventos.capturar_loop()  # lo hace el lifespan; aquí la app se invoca sin él
        scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1", "method": "GET", "scheme": "https",
            "path": "/crm/api/eventos", "raw_path": b"/crm/api/eventos", "query_string": b"",
            "root_path": "", "headers": [(b"host", b"testserver"), (b"cookie", cookie.encode())],
            "client": ("127.0.0.1", 50000), "server": ("testserver", 443),
        }
        trozos: list[bytes] = []
        hay_cabecera, hay_trozo = asyncio.Event(), asyncio.Event()

        async def receive():
            await asyncio.Event().wait()  # el cliente no manda nada ni se va

        async def send(mensaje):
            if mensaje["type"] == "http.response.start":
                assert mensaje["status"] == 200, mensaje
                hay_cabecera.set()
            elif mensaje["type"] == "http.response.body":
                trozos.append(mensaje.get("body", b""))
                hay_trozo.set()

        tarea = asyncio.ensure_future(app(scope, receive, send))
        try:
            await asyncio.wait_for(hay_cabecera.wait(), segundos)
            disparar()
            # Lo primero que ocurra: un aviso, o que la respuesta termine sola. Al
            # cerrar el stream EventSourceResponse no manda ningún trozo más, así
            # que esperar solo un trozo colgaría justo en el caso que se prueba.
            un_trozo = asyncio.ensure_future(hay_trozo.wait())
            hecho, _ = await asyncio.wait({un_trozo, tarea}, timeout=segundos,
                                          return_when=asyncio.FIRST_COMPLETED)
            un_trozo.cancel()
            assert hecho, f"ni aviso ni cierre en {segundos} s"
            # El trozo final llega un tick antes de que la respuesta acabe de recogerse.
            try:
                await asyncio.wait_for(asyncio.shield(tarea), 1.0)
            except asyncio.TimeoutError:
                pass  # sigue abierto: eso es lo que se reporta
            if tarea.done():
                tarea.result()  # una tarea que MURIÓ no es un stream que se cerró solo
            crudo = b"".join(trozos).decode()
            return (
                [l.removeprefix("event: ") for l in crudo.splitlines() if l.startswith("event: ")],
                tarea.done(),
            )
        finally:
            tarea.cancel()
            await asyncio.sleep(0)  # deja correr el `finally` del generador

    return asyncio.run(principal())


def test_una_pestana_del_crm_ya_abierta_deja_de_recibir_avisos_tras_la_baja():
    """El guardia corre una vez por request y este request no termina: a quien
    dieron de baja con el CRM abierto le seguían llegando avisos (hallazgo de la
    refutación). El aviso es mudo —solo «hay actividad»—, pero es actividad de
    los clientes del negocio contada a alguien que ya no trabaja ahí. Primero el
    control positivo con Ana habilitada: sin él, un generador que se cerrara
    siempre pasaría la segunda mitad."""
    ana = _entrar(ANA)
    assert _stream_del_crm(ana, eventos.publicar) == (["mensajes"], False), (
        "con Ana habilitada el aviso no llegó o el stream se cerró: el control positivo falló"
    )

    def dar_de_baja_y_publicar():
        _apagar(ANA)
        eventos.publicar()

    llegaron, termino = _stream_del_crm(ana, dar_de_baja_y_publicar)
    assert llegaron == [], "la pestaña de alguien dado de baja siguió recibiendo avisos"
    assert termino, "el stream no se cerró: el EventSource nunca va a reintentar y a rebotar en el guardia"
    assert eventos.conectados() == 0, "el generador no se dio de baja al cerrarse"


# ---------------------------------------------------------------------------
# touch_session
# ---------------------------------------------------------------------------


class _RequestFalso:
    """Lo mínimo que `touch_session` lee de un request."""

    def __init__(self, user: str, sid: str):
        self.session = {"user": user, "sid": sid}
        self.headers: dict[str, str] = {}
        self.client = None


def test_touch_session_no_resucita_una_websession_cerrada():
    """A nivel de helper a propósito: con el guardia puesto este punto ya no se
    alcanza por HTTP, así que una prueba por HTTP pasaría igual con la línea
    vieja (`ended_at = None`) de vuelta. Cubre la carrera con un logout desde
    otra pestaña entre el guardia y el touch."""
    ana = _entrar(ANA)
    sid = _websessions(ANA)[0].token
    ana.get("/logout", follow_redirects=False)
    cerrada = _websessions(ANA)[0].ended_at
    assert cerrada is not None

    authlog.touch_session(_RequestFalso(ANA, sid))

    assert _websessions(ANA)[0].ended_at == cerrada, "touch_session reabrió una sesión cerrada"
