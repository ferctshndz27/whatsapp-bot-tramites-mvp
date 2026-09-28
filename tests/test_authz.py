"""Autorización de las rutas admin del panel, ruta por ruta y por HTTP real.

REGLA de negocio que protege este archivo: el panel tiene DOS roles y la
frontera entre ellos es dura. Un colaborador puede atender solicitudes, pero
NO puede administrar el sistema: no se hace admin a sí mismo, no edita el
catálogo (que fija los precios que se le cotizan a un cliente real), no
aprueba sinónimos (REGLA DE ORO: nada auto-aprende — un sinónimo mal
aprobado mal-cotiza), no borra solicitudes, no bloquea clientes y no ve las
bitácoras ni las exportaciones completas.

Cada ruta se verifica en TRES tiempos, porque un redirect por sí solo no
prueba nada (una ruta inexistente también "no cambia nada"):
  1. el colaborador es rechazado con el redirect que corresponde — y ojo:
     cinco rutas NO mandan a "/" sino a /tickets/{id} o /inbox/{phone};
  2. el estado que esa ruta muta NO cambió (rol, habilitado, precio, status
     de la propuesta, conteos...) o el dato sensible no se filtró en el
     cuerpo de la respuesta;
  3. CONTROL POSITIVO: el MISMO request hecho por un admin SÍ surte efecto.
     Sin (3), un typo en el path haría pasar (1) y (2) por 404.

Se ejercita por la URL real (TestClient sobre app.main), no llamando a la
función del router: así el path forma parte del contrato probado.
"""

import inspect
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

import bcrypt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine, select

import app.db as db
import app.nlu as nlu
import app.synonym_miner as miner
import app.whatsapp_client as whatsapp_client
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import (
    Office,
    OfficeCredential,
    AdminAudit,
    CatalogCategory,
    CatalogService,
    Client,
    ClientNote,
    Cobro,
    Direction,
    Invitation,
    LoginEvent,
    Message,
    NluEvent,
    SynonymProposal,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
    utcnow,
)

ADMIN = "jefa"
COLAB = "colab"
VICTIMA = "victima"  # colaborador objetivo de las rutas de /users

PHONE = "5215500000000"
PHONE_NO_ENTENDIDO = "5215599998888"  # otro teléfono: no debe tener IN el del ticket

CLAVE = "clave-de-prueba-1234"
# rounds=4: el hash solo tiene que ser válido, no seguro — con el costo por
# defecto (12) cada login del test costaría ~0.3 s.
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()

# Marcadores sembrados en la base: datos sensibles que SOLO el admin puede ver.
TELEFONO_COLABORADOR = "5215544443333"  # dato personal listado en /users
TOKEN_INVITACION = "tok-authz"
IP_ACCESO = "203.0.113.77"  # rango de documentación (RFC 5737), nunca real
MARCA_ACTIVIDAD = "marca-secreta-actividad"
MARCA_BITACORA = "secreto-en-bitacora"
CLIENTE_SECRETO = "Cliente Secreto"
REFERENCIA_COBRO = "recibo-secreto-9931"  # solo sale en el CSV de conciliación
PRECIO_SEMBRADO = "$777"
TEXTO_NO_ENTENDIDO = "quiero rentar un zeppelin para el domingo"
SINONIMO_MINADO = "sinonimo minado por claude"


# ---- Dobles: nada de red en este archivo ----


def _sin_llm(*_args, **_kwargs):
    raise AssertionError("una prueba de autorización no debe hablar con Claude")


def _sin_whatsapp(*_args, **_kwargs):
    raise AssertionError("una prueba de autorización no debe enviar WhatsApp")


def _mineria_falsa(_texts):
    """Doble de synonym_miner.mine_synonyms: lo que se prueba aquí es el
    guard de la ruta, no la minería (que iría a la API de Claude)."""
    return [{"service_id": "curp", "synonym": SINONIMO_MINADO}]


# ---- Sondas de estado (cada una abre su propia sesión sobre el engine parchado) ----


def _campo(modelo, pk, nombre_campo) -> Callable[[], object]:
    """Sonda: valor de un campo de una fila por PK; None si la fila no existe."""

    def sonda():
        with get_session() as s:
            fila = s.get(modelo, pk)
            return getattr(fila, nombre_campo) if fila is not None else None

    return sonda


def _conteo(modelo, *filtros) -> Callable[[], int]:
    """Sonda: cuántas filas cumplen los filtros."""

    def sonda():
        with get_session() as s:
            consulta = select(modelo)
            for f in filtros:
                consulta = consulta.where(f)
            return len(s.exec(consulta).all())

    return sonda


def _sinonimos_de_curp() -> list[str]:
    with get_session() as s:
        return json.loads(s.get(CatalogService, "curp").synonyms_json)


def _clave_de_victima_es_nueva() -> bool:
    """True solo si la contraseña quedó reseteada al valor que manda el request."""
    with get_session() as s:
        fila = s.exec(select(User).where(User.username == VICTIMA)).one()
    return bcrypt.checkpw(b"contrasena-impuesta", fila.password_hash.encode())


# ---- La tabla de rutas ----


@dataclass(frozen=True)
class Ruta:
    nombre: str
    metodo: str
    path: str
    redirige_a: str  # Location que debe recibir el colaborador
    data: dict = field(default_factory=dict)
    sonda: Callable[[], object] | None = None  # estado que NO debe cambiar
    tras_admin: object = None  # valor de la sonda después del request del admin
    filtracion: str | None = None  # dato que solo el admin puede ver en el cuerpo


RUTAS: list[Ruta] = [
    # --- Colaboradores: la escalada de privilegios vive aquí ---
    Ruta(
        "GET /users", "GET", "/users", "/",
        filtracion=TELEFONO_COLABORADOR,
    ),
    Ruta(
        "POST /users", "POST", "/users", "/",
        data={"username": "intruso", "password": "contrasena-larga-123", "role": "admin"},
        sonda=_conteo(User, User.username == "intruso"), tras_admin=1,
    ),
    Ruta(
        "POST /users/{u}/password", "POST", f"/users/{VICTIMA}/password", "/",
        data={"password": "contrasena-impuesta"},
        sonda=_clave_de_victima_es_nueva, tras_admin=True,
    ),
    Ruta(  # la ruta de escalada a admin
        "POST /users/{u}/role", "POST", f"/users/{VICTIMA}/role", "/",
        sonda=_conteo(User, User.username == VICTIMA, User.is_admin == True),  # noqa: E712
        tras_admin=1,
    ),
    Ruta(
        "POST /users/{u}/toggle", "POST", f"/users/{VICTIMA}/toggle", "/",
        sonda=_conteo(User, User.username == VICTIMA, User.habilitado == True),  # noqa: E712
        tras_admin=0,
    ),
    Ruta(
        # Mover a alguien de oficina le cambia lo que ve y suelta sus folios:
        # un colaborador que pudiera hacerlo se mudaría solo a donde quisiera.
        "POST /users/{u}/oficina", "POST", f"/users/{VICTIMA}/oficina", "/",
        data={"office_id": 2},
        sonda=_campo(User, 3, "office_id"), tras_admin=2,  # 3 = VICTIMA en _sembrar
    ),
    # --- Catálogo: SALIÓ de esta lista el 2026-09-18 (regla del operador) ---
    # Lo editan todos los colaboradores y ahí declara cada quien qué productos
    # atiende. Lo que sigue protegido: el anónimo no entra (lo cubre la
    # enumeración de rutas al final de este archivo) y nadie inscribe a otro (la
    # ruta toma el usuario de la sesión) — ver tests/test_catalog_asesores.py.
    # --- Agente WhatsApp: la REGLA DE ORO (nada auto-aprende) se defiende aquí ---
    Ruta(
        "GET /unmatched", "GET", "/unmatched", "/",
        filtracion=TEXTO_NO_ENTENDIDO,
    ),
    Ruta(
        "POST /unmatched/mine", "POST", "/unmatched/mine", "/",
        sonda=_conteo(SynonymProposal, SynonymProposal.synonym == SINONIMO_MINADO), tras_admin=1,
    ),
    Ruta(  # aprobar una propuesta la mete al catálogo que cotiza el bot
        "POST /unmatched/proposals/{id}/approve", "POST", "/unmatched/proposals/1/approve", "/",
        sonda=_campo(SynonymProposal, 1, "status"), tras_admin="aprobada",
    ),
    Ruta(
        "POST /unmatched/proposals/{id}/reject", "POST", "/unmatched/proposals/2/reject", "/",
        sonda=_campo(SynonymProposal, 2, "status"), tras_admin="rechazada",
    ),
    Ruta(
        "POST /unmatched/synonyms", "POST", "/unmatched/synonyms", "/",
        data={"service_id": "curp", "synonym": "acta de curp"},
        sonda=_sinonimos_de_curp, tras_admin=["acta de curp"],
    ),
    Ruta(  # ignorar = soft delete del grupo; un colaborador no debe poder
        "POST /unmatched/clusters/ignore", "POST", "/unmatched/clusters/ignore", "/",
        data={"synonym": TEXTO_NO_ENTENDIDO},
        sonda=_campo(NluEvent, 1, "habilitado"), tras_admin=False,
    ),
    Ruta(
        "GET /export/no-entendidos.csv", "GET", "/export/no-entendidos.csv", "/",
        filtracion=TEXTO_NO_ENTENDIDO,
    ),
    # --- Estudio del Agente ---
    Ruta("GET /agente", "GET", "/agente", "/"),
    # --- Sistema, bitácoras y exportaciones ---
    Ruta("GET /system", "GET", "/system", "/"),
    Ruta(
        "POST /system/sync", "POST", "/system/sync", "/",
        sonda=_conteo(AdminAudit, AdminAudit.action == "espejo:sync"), tras_admin=1,
    ),
    Ruta("GET /audit", "GET", "/audit", "/", filtracion=MARCA_BITACORA),
    Ruta("GET /logins", "GET", "/logins", "/", filtracion=IP_ACCESO),
    Ruta("GET /export/logins.csv", "GET", "/export/logins.csv", "/", filtracion=IP_ACCESO),
    Ruta("GET /export/tickets.csv", "GET", "/export/tickets.csv", "/", filtracion=CLIENTE_SECRETO),
    # --- Cobranza: lo que se ve aquí es dinero de clientes ---
    Ruta("GET /cobros", "GET", "/cobros", "/", filtracion=CLIENTE_SECRETO),
    Ruta("GET /cobros/payouts", "GET", "/cobros/payouts", "/", filtracion=CLIENTE_SECRETO),
    Ruta("GET /export/cobros.csv", "GET", "/export/cobros.csv", "/", filtracion=CLIENTE_SECRETO),
    Ruta(
        "GET /export/conciliacion.csv", "GET", "/export/conciliacion.csv", "/",
        filtracion=REFERENCIA_COBRO,
    ),
    Ruta(
        "GET /export/activity.csv", "GET", "/export/activity.csv", "/",
        filtracion=MARCA_ACTIVIDAD,
    ),
    # --- Invitaciones: quien las emite decide quién entra al panel ---
    Ruta("GET /invitations", "GET", "/invitations", "/", filtracion=TOKEN_INVITACION),
    Ruta(
        "POST /invitations", "POST", "/invitations", "/",
        sonda=_conteo(Invitation), tras_admin=2,
    ),
    Ruta(
        "POST /invitations/{id}/toggle", "POST", "/invitations/1/toggle", "/",
        sonda=_campo(Invitation, 1, "habilitado"), tras_admin=False,
    ),
    # --- Las cinco que NO redirigen a "/" ---
    Ruta(
        "POST /tickets/{id}/delete", "POST", "/tickets/1/delete", "/tickets/1",
        sonda=_campo(Ticket, 1, "habilitado"), tras_admin=False,
    ),
    Ruta(
        "POST /inbox/{phone}/block", "POST", f"/inbox/{PHONE}/block", f"/inbox/{PHONE}",
        sonda=_campo(Client, PHONE, "habilitado"), tras_admin=False,
    ),
    # Éxito y rechazo redirigen al MISMO lugar: aquí el redirect no prueba
    # nada y la sonda es el único testigo real.
    Ruta(
        "POST /tickets/{id}/finish", "POST", "/tickets/1/finish", "/tickets/1",
        sonda=_campo(Ticket, 1, "status"), tras_admin=TicketStatus.CERRADO,
    ),
    Ruta(
        "POST /clients/{phone}/notes/{id}/delete", "POST",
        f"/clients/{PHONE}/notes/1/delete", f"/clients/{PHONE}",
        sonda=_campo(ClientNote, 1, "habilitado"), tras_admin=False,
    ),
    # --- Oficinas: quien entra aquí lee la pista de las llaves de TODAS las
    # oficinas y puede rotar la de Stripe (consecuencia asumida en §5 de
    # docs/SPEC-MULTI-OFICINA.md: en este alcance solo existe `is_admin`).
    Ruta(
        "GET /oficinas", "GET", "/oficinas", "/",
    ),
    Ruta(
        "POST /oficinas", "POST", "/oficinas", "/",
        data={"nombre": "Sucursal Intrusa", "slug": "intrusa", "ciudad": "X"},
        sonda=_conteo(Office, Office.slug == "intrusa"), tras_admin=1,
    ),
    Ruta(
        "GET /oficinas/{id}", "GET", "/oficinas/1", "/",
    ),
    Ruta(
        # Editar mueve la URL del webhook de Stripe de esa oficina: un
        # colaborador que pudiera hacerlo dejaría sus cobros en el limbo.
        "POST /oficinas/{id}", "POST", "/oficinas/2", "/",
        data={"nombre": "Secuestrada", "slug": "secuestrada", "ciudad": "X"},
        sonda=_campo(Office, 2, "slug"), tras_admin="secuestrada",
    ),
    Ruta(
        # La baja de una oficina corta su entrada de mensajes y sus pagos.
        "POST /oficinas/{id}/baja", "POST", "/oficinas/2/baja", "/",
        sonda=_campo(Office, 2, "habilitado"), tras_admin=False,
    ),
    Ruta(
        "POST /oficinas/{id}/llaves", "POST", "/oficinas/1/llaves", "/",
        data={"clave": "STRIPE_SECRET_KEY", "valor": "sk_test_intruso_9999"},
        sonda=_conteo(OfficeCredential, OfficeCredential.habilitado == True),  # noqa: E712
        tras_admin=1,
    ),
    Ruta(
        "POST /oficinas/{id}/llaves/{clave}/baja", "POST",
        "/oficinas/1/llaves/STRIPE_SECRET_KEY/baja", "/",
    ),
    Ruta(
        # A la oficina 2, no a la 1: VICTIMA ya nace en la 1 y mover ahí no
        # cambiaría nada — la sonda sería ciega al guard que dice probar.
        "POST /oficinas/{id}/usuarios", "POST", "/oficinas/2/usuarios", "/",
        data={"username": VICTIMA},
        sonda=_campo(User, 3, "office_id"), tras_admin=2,  # 3 = VICTIMA en _sembrar
    ),
]


# ---- Infraestructura local ----


def _sembrar() -> None:
    """Estado mínimo para que cada ruta de la tabla tenga algo real que tocar."""
    with get_session() as s:
        s.add(User(username=ADMIN, password_hash=HASH_CLAVE, is_admin=True))
        s.add(User(username=COLAB, password_hash=HASH_CLAVE, is_admin=False))
        s.add(
            User(
                username=VICTIMA, password_hash=HASH_CLAVE, is_admin=False,
                habilitado=True, phone=TELEFONO_COLABORADOR,
            )
        )
        # Una segunda oficina para que «mover un colaborador» tenga a dónde:
        # con una sola, la sonda mediría un cambio que no existe.
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        s.add(Client(phone=PHONE, name=CLIENTE_SECRETO, habilitado=True))
        # assigned_to=None a propósito: así el admin pasa el guard de
        # /finish por ser admin, no por ser el asignado.
        s.add(
            Ticket(
                id=1, phone=PHONE, client_name=CLIENTE_SECRETO, category="Servicios básicos",
                service_id="curp", service_name="CURP certificada",
                price_info=PRECIO_SEMBRADO, status=TicketStatus.NUEVO, assigned_to=None,
            )
        )
        s.add(TicketActivity(ticket_id=1, username=ADMIN, action=MARCA_ACTIVIDAD))
        # Cobro ya liquidado y conciliado: alimenta /cobros, /cobros/payouts y
        # los dos CSV. llave_cobro_vivo=None a propósito — un cobro vivo haría
        # que /finish exigiera confirmación y cambiaría lo que prueban las
        # filas de arriba.
        s.add(
            Cobro(
                id=1, ticket_id=1, phone=PHONE, client_name=CLIENTE_SECRETO,
                service_id="curp", concepto="CURP certificada (folio #1)",
                monto_centavos=80000, monto_pagado_centavos=80000,
                comision_centavos=3000, neto_centavos=77000, moneda_liquidacion="mxn",
                estado="pagado", llave_cobro_vivo=None,
                token_verificacion="tok-authz", creado_por=ADMIN,
                stripe_charge_id="ch_semilla", payout_id="po_semilla",
                referencia_recibo=REFERENCIA_COBRO, metodo_usado="card",
                pagado_at=utcnow(),
            )
        )
        # Autor "tercero": el admin borra la nota por ser admin, no por autoría.
        s.add(ClientNote(id=1, phone=PHONE, author="tercero", body="ya pagó anticipo"))
        s.add(Invitation(id=1, token=TOKEN_INVITACION, created_by=ADMIN, used_at=None))
        s.add(AdminAudit(admin=ADMIN, action="usuario:password", target=MARCA_BITACORA))
        s.add(LoginEvent(username="fantasma", success=False, reason="x", ip=IP_ACCESO))
        s.add(SynonymProposal(id=1, service_id="curp", synonym="clave unica"))
        s.add(SynonymProposal(id=2, service_id="curp", synonym="mi curp"))
        # Grupo "no entendido" que el admin puede ignorar (soft delete).
        s.add(NluEvent(id=1, phone=PHONE_NO_ENTENDIDO, text=TEXTO_NO_ENTENDIDO, capa="ninguna"))
        # Catálogo propio: sembrarlo evita que app.catalog importe catalog.json
        # y ata la prueba a datos controlados.
        s.add(CatalogCategory(id="basicos", name="Servicios básicos", position=0))
        s.add(
            CatalogService(
                id="curp", category_id="basicos", name="CURP certificada",
                price_info=PRECIO_SEMBRADO, synonyms_json="[]", position=0,
            )
        )
        # Par IN + "No reconocí…" que alimenta /unmatched y su CSV. Va en OTRO
        # teléfono: un IN reciente en el del ticket abriría la ventana de 24 h
        # y /finish intentaría avisarle al cliente por WhatsApp.
        ayer = utcnow() - timedelta(days=1)
        s.add(
            Message(
                phone=PHONE_NO_ENTENDIDO, direction=Direction.IN,
                body=TEXTO_NO_ENTENDIDO, created_at=ayer,
            )
        )
        s.add(
            Message(
                phone=PHONE_NO_ENTENDIDO, direction=Direction.OUT,
                body=miner._HINT_MARKER + " Elige del menú.",
                created_at=ayer + timedelta(seconds=1),
            )
        )
        s.commit()


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch):
    """Engine compartido entre hilos + semilla + todo lo que salga a la red apagado.

    Depende de _isolated_db (autouse del conftest) para correr DESPUÉS y pisar
    su engine. StaticPool es obligatorio: TestClient atiende la app en el hilo
    del portal de anyio y el SingletonThreadPool por defecto le daría a ese
    hilo una base :memory: distinta y VACÍA.
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")  # apaga archive.enabled()
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    # Sin esto, POST /oficinas/{id}/llaves rechaza a TODOS —también al admin— y
    # el control positivo no podría distinguir «lo paró el guard» de «faltaba
    # la llave maestra».
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())
    monkeypatch.setattr(nlu, "_get_client", _sin_llm)
    monkeypatch.setattr(whatsapp_client, "send_text", _sin_whatsapp)
    monkeypatch.setattr(whatsapp_client, "send_buttons", _sin_whatsapp)
    monkeypatch.setattr(miner, "mine_synonyms", _mineria_falsa)
    _sembrar()
    yield


def _sesion_de(username: str) -> TestClient:
    """Cliente con la cookie de sesión real, obtenida por POST /login.

    base_url https: la cookie de sesión lleva el flag Secure cuando
    SECURE_COOKIES no es 0 y sobre http el navegador (y httpx) no la mandaría.
    """
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post(
        "/login", data={"username": username, "password": CLAVE}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/", "el login de la prueba falló"
    return cliente


@pytest.fixture()
def cliente_anonimo():
    """Sin login: es el punto de la prueba. base_url https por el flag Secure."""
    with TestClient(app, base_url="https://testserver") as cliente:
        yield cliente


def _pedir(cliente: TestClient, ruta: Ruta):
    if ruta.metodo == "GET":
        return cliente.get(ruta.path, follow_redirects=False)
    return cliente.post(ruta.path, data=ruta.data, follow_redirects=False)


# ---- Las pruebas ----


@pytest.mark.parametrize("ruta", RUTAS, ids=lambda r: r.nombre)
def test_colaborador_es_rechazado_y_no_cambia_nada(ruta: Ruta):
    """(a) rechazo con el redirect correcto y (b) el efecto NO ocurrió."""
    antes = ruta.sonda() if ruta.sonda else None
    if ruta.sonda is not None:
        # Sin esto la sonda podría ser tautológica (medir algo que el admin
        # tampoco cambia) y la prueba pasaría con el guard quitado.
        assert antes != ruta.tras_admin, f"sonda inútil en {ruta.nombre}"

    respuesta = _pedir(_sesion_de(COLAB), ruta)

    assert respuesta.status_code == 303, f"{ruta.nombre} no rechazó al colaborador"
    assert respuesta.headers["location"] == ruta.redirige_a
    if ruta.sonda is not None:
        assert ruta.sonda() == antes, f"{ruta.nombre} mutó el estado sin ser admin"
    if ruta.filtracion is not None:
        assert ruta.filtracion not in respuesta.text


@pytest.mark.parametrize("ruta", RUTAS, ids=lambda r: r.nombre)
def test_control_positivo_el_admin_si_surte_efecto(ruta: Ruta):
    """(c) sin esto, un typo en el path haría pasar la prueba de arriba por 404."""
    respuesta = _pedir(_sesion_de(ADMIN), ruta)

    assert respuesta.status_code != 404, f"{ruta.nombre} no existe: el path está mal"
    assert respuesta.headers.get("location") not in ("/", "/login"), (
        f"{ruta.nombre} rechazó a un admin"
    )
    if ruta.sonda is not None:
        assert ruta.sonda() == ruta.tras_admin, f"{ruta.nombre} no surtió efecto siendo admin"
    else:
        assert respuesta.status_code == 200
    if ruta.filtracion is not None:
        assert ruta.filtracion in respuesta.text


def test_activity_no_cierra_la_puerta_pero_filtra_los_datos():
    """Caso especial, NO es un agujero: /activity responde 200 a un
    colaborador a propósito — ahí is_admin FILTRA datos en vez de cerrar la
    puerta. Lo que no debe pasar es que ?user= le sirva el historial ajeno.
    """
    with get_session() as s:
        s.add(TicketActivity(ticket_id=1, username=ADMIN, action="cerrado-por-la-jefa"))
        s.add(TicketActivity(ticket_id=1, username=COLAB, action="cerrado-por-el-colab"))
        s.commit()

    como_colab = _sesion_de(COLAB).get(f"/activity?user={ADMIN}", follow_redirects=False)
    como_admin = _sesion_de(ADMIN).get(f"/activity?user={COLAB}", follow_redirects=False)

    assert como_colab.status_code == 200  # la puerta está abierta a propósito
    assert "cerrado-por-el-colab" in como_colab.text  # ve lo suyo
    assert "cerrado-por-la-jefa" not in como_colab.text  # ignoró el ?user= ajeno
    assert "cerrado-por-el-colab" in como_admin.text  # el admin sí puede mirar a otro


def _plantilla_de(metodo: str, path: str) -> str | None:
    """Plantilla del router ('/users/{username}/role') que atiende un path
    concreto ('/users/victima/role'), o None si ninguna lo atiende."""
    for route in app.routes:
        regex = getattr(route, "path_regex", None)
        if regex is None or metodo not in getattr(route, "methods", set()):
            continue
        if regex.match(path):
            return route.path
    return None


def test_la_tabla_cubre_toda_ruta_con_guard_de_admin():
    """Guardia de cobertura: si mañana se agrega una ruta que llama a
    is_admin/_admin_or_redirect y nadie le escribe su fila, esta prueba lo
    canta. Sin esto la tabla se queda congelada en las rutas de hoy.
    """
    # Rutas con guard propio deliberadamente fuera de la tabla: ahí is_admin
    # AMPLÍA lo que se puede hacer en vez de cerrar la puerta, así que no son
    # admin-only y no les toca fila.
    #   - GET /activity: filtra datos (tiene su prueba arriba).
    #   - POST /tickets/{id}/release: cualquiera suelta SU folio; un admin puede
    #     además soltar el de otro, que es la única salida para los folios de un
    #     colaborador deshabilitado (tiene su prueba en test_fase3_horario.py).
    #   - GET /clients/{phone}: la ficha es de cualquiera de la oficina; al admin
    #     solo le agrega el selector para meter a otro en la cartera (la regla
    #     vive en app/cartera.py y tiene su prueba en tests/test_cartera.py).
    #   - POST /inbox/{phone}/bot-on: quien puede escribir reactiva el agente de
    #     SUS folios; un admin, el de todos (tests/test_chat_varios_duenos.py).
    fuera_de_tabla = {
        ("GET", "/activity"),
        ("POST", "/tickets/{ticket_id}/release"),
        ("GET", "/clients/{phone}"),
        ("POST", "/inbox/{phone}/bot-on"),
    }
    # API JSON del CRM: sí cierra la puerta, pero contesta 403 JSON en vez de redirigir
    # (la llama fetch) y no encaja en el molde de Ruta. Su rechazo al colaborador y su
    # control positivo con el admin viven en tests/test_crm.py
    # (test_el_alta_nace_en_la_oficina_de_quien_la_hace_con_telefono_canonico).
    con_prueba_propia = {("POST", "/crm/api/contactos")}

    cubiertas = {(r.metodo, _plantilla_de(r.metodo, r.path)) for r in RUTAS}
    assert None not in {p for _, p in cubiertas}, "hay filas cuyo path no existe en el router"

    con_guard = set()
    for route in app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(route, "methods"):
            continue
        try:
            fuente = inspect.getsource(endpoint)
        except (OSError, TypeError):  # pragma: no cover — endpoints sin fuente
            continue
        if "is_admin(" not in fuente and "_admin_or_redirect(" not in fuente:
            continue
        con_guard |= {(m, route.path) for m in route.methods & {"GET", "POST"}}

    assert con_guard, "la detección de guards dejó de encontrar rutas"
    faltantes = sorted(
        f"{m} {p}" for m, p in con_guard - cubiertas - fuera_de_tabla - con_prueba_propia
    )
    assert faltantes == [], f"rutas admin sin fila en RUTAS: {faltantes}"


# ---------------------------------------------------------------------------
# El anónimo: la puerta de la calle
# ---------------------------------------------------------------------------
# Todo lo de arriba compara colaborador contra admin — dos personas que YA
# entraron. Falta la puerta de afuera, y es el riesgo que nadie vigila: una
# ruta nueva a la que se le olvide `_current_user(request)` nace PÚBLICA, y
# ninguna de las pruebas anteriores lo notaría porque ambas llegan con sesión.
# Se enumera el router en vez de mantener otra tabla a mano: una ruta que se
# agregue mañana entra sola a esta prueba.

# Públicas POR DISEÑO, cada una con su razón. La lista es corta a propósito:
# crecerla es una decisión de seguridad, no un trámite para poner algo en verde.
PUBLICAS: dict[tuple[str, str], str] = {
    ("GET", "/health"): "lo consulta deploy/pull-deploy.sh para decidir el rollback",
    ("GET", "/login"): "la puerta",
    ("POST", "/login"): "la puerta",
    ("GET", "/register"): "alta con código de invitación; sin él no registra",
    ("POST", "/register"): "alta con código de invitación; sin él no registra",
    # Webhooks: su autenticación es la FIRMA (HMAC), no la cookie. Sin firma
    # válida contestan 403 — verificado abajo, no asumido.
    ("GET", "/webhook"): "verificación de Meta (hub.challenge)",
    ("POST", "/webhook"): "entrante de Meta, firma HMAC",
    ("POST", "/webhook/twilio"): "entrante de Twilio, firma propia",
    ("POST", "/webhook/twilio/status"): "acuses de Twilio, firma propia",
    ("POST", "/webhooks/stripe"): "eventos de Stripe, firma propia (ruta legada)",
    ("POST", "/webhooks/stripe/{slug}"): "eventos de Stripe por oficina, firma propia",
    # El cliente que acaba de pagar vuelve del checkout de Stripe SIN sesión del
    # panel: si estas pidieran login, vería la pantalla de acceso al asesor.
    ("GET", "/cobros/gracias"): "retorno del checkout de Stripe",
    ("GET", "/cobros/cancelado"): "retorno del checkout de Stripe",
    # Adjunto servido por nombre aleatorio para que el cliente lo abra desde
    # WhatsApp; el secreto es la URL. Su gemela /media/{name} SÍ pide sesión.
    ("GET", "/media/publico/{name}"): "adjunto de cliente, URL no adivinable",
}


def _rutas_del_router() -> list[tuple[str, str]]:
    return sorted(
        (metodo, ruta.path)
        for ruta in app.routes
        if hasattr(ruta, "methods") and hasattr(ruta, "path")
        for metodo in ruta.methods & {"GET", "POST"}
    )


@pytest.mark.parametrize(("metodo", "path"), _rutas_del_router())
def test_ninguna_ruta_del_panel_le_contesta_a_un_anonimo(cliente_anonimo, metodo, path):
    """REGLA: sin sesión, ninguna ruta del panel devuelve 200.

    Lo que se afirma es la ausencia de CONTENIDO, no un código concreto: 303 a
    /login, 403 y 422 son todos rechazos válidos. El 422 aparece porque FastAPI
    valida el `Form(...)` antes de que el handler llegue a su guard — la
    petición muere igual, pero conviene saber que en esas rutas el orden es
    ese y no al revés.
    """
    respuesta = cliente_anonimo.request(
        metodo, re.sub(r"\{[^}]+\}", "1", path), follow_redirects=False
    )
    razon = PUBLICAS.get((metodo, path))
    if razon:
        assert respuesta.status_code != 500, f"{metodo} {path} es pública ({razon}) y revienta"
        return

    assert respuesta.status_code != 200, (
        f"{metodo} {path} le contestó 200 a un anónimo. Si es pública a propósito, "
        "agrégala a PUBLICAS con su razón; si no, le falta el guard de sesión."
    )


def test_la_lista_de_rutas_publicas_no_tiene_entradas_muertas():
    """Una entrada que ya no corresponde a ninguna ruta es un permiso que
    sobrevive a la ruta que lo justificaba: el día que alguien reuse ese path,
    nace público sin que nadie lo decida."""
    fantasmas = sorted(f"{m} {p}" for m, p in PUBLICAS if (m, p) not in _rutas_del_router())
    assert fantasmas == [], f"PUBLICAS nombra rutas que ya no existen: {fantasmas}"


@pytest.mark.parametrize(("metodo", "path"), [
    (m, p) for (m, p) in PUBLICAS if "/webhook" in p
])
def test_los_webhooks_publicos_rechazan_sin_firma(cliente_anonimo, metodo, path):
    """Los webhooks están en PUBLICAS porque su autenticación es la firma, no la
    cookie. Eso solo es aceptable si la firma DE VERDAD cierra: un webhook que
    aceptara sin firma sería una ruta pública de escritura."""
    respuesta = cliente_anonimo.request(
        metodo, re.sub(r"\{[^}]+\}", "1", path), follow_redirects=False
    )
    assert respuesta.status_code == 403, (
        f"{metodo} {path} contestó {respuesta.status_code} sin firma; debe ser 403"
    )
