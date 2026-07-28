"""Alta manual de clientes desde el panel.

REGLA DE NEGOCIO: hasta ahora un cliente solo nacía cuando escribía por
WhatsApp. Con el alta manual el asesor puede registrarlo tras cerrar el trato
por teléfono y programarle cobros sin esperar un mensaje.

El caso peligroso es el ALTA DE ALGUIEN QUE YA EXISTE: tiene que actualizar el
nombre y NADA MÁS. Si de paso reiniciara la conversación, un cliente a media
solicitud perdería su avance; si de paso lo re-habilitara, el alta sería una
puerta trasera para desbloquear a alguien que un admin bloqueó a propósito.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.config import settings
from app.main import app
from app.models import AdminAudit, Client, Conversation, User

CRUDO = "5215500000000"
CANONICO = "525500000000"

CLAVE = "clave-de-prueba"
# rounds=4: con el costo por defecto (12) cada login del test costaría ~0.3 s.
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def _base_compartida(monkeypatch):
    """Engine con StaticPool, obligatorio para las pruebas por HTTP.

    Depende de _isolated_db (autouse del conftest) para correr DESPUÉS y pisar
    su engine: TestClient atiende la app en el hilo del portal de anyio, y el
    SingletonThreadPool por defecto le daría a ese hilo una base :memory:
    distinta y VACÍA (mismo motivo documentado en tests/test_authz.py).
    """
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")  # apaga archive.enabled()
    monkeypatch.setattr(settings, "geoip_enabled", False)  # sin red en pruebas
    yield


@pytest.fixture()
def sesion_panel():
    """Cliente HTTP con la cookie de sesión real, obtenida por POST /login."""
    with Session(db.engine) as s:
        s.add(User(username="asesora", password_hash=HASH_CLAVE))
        s.commit()
    with TestClient(app) as http:
        respuesta = http.post(
            "/login",
            data={"username": "asesora", "password": CLAVE},
            follow_redirects=False,
        )
        assert respuesta.status_code == 303, "el login de la prueba dejó de funcionar"
        yield http


def _alta(http, phone: str, name: str):
    return http.post("/clients", data={"phone": phone, "name": name}, follow_redirects=False)


# ---------------------------------------------------------------------------
# Autorización
# ---------------------------------------------------------------------------


def test_sin_sesion_no_se_puede_dar_de_alta_ni_listar():
    # REGLA: el directorio de clientes es interno. Sin sesión, a /login.
    with TestClient(app) as http:
        assert http.get("/clients", follow_redirects=False).headers["location"] == "/login"
        assert _alta(http, CANONICO, "Intruso").headers["location"] == "/login"

    with Session(db.engine) as s:
        assert s.exec(select(Client)).all() == [], "un anónimo creó un cliente"


# ---------------------------------------------------------------------------
# Alta y upsert
# ---------------------------------------------------------------------------


def test_alta_de_numero_nuevo_crea_un_cliente(sesion_panel):
    # REGLA: el alta es el camino para registrar a alguien que nunca escribió.
    _alta(sesion_panel, "55 0000 0000", "Ana Ruiz")

    with Session(db.engine) as s:
        clientes = s.exec(select(Client)).all()
    assert len(clientes) == 1
    assert clientes[0].phone == CANONICO, "el teléfono no se guardó en formato canónico"
    assert clientes[0].name == "Ana Ruiz"


@pytest.mark.parametrize(
    "formato", [CRUDO, CANONICO, "whatsapp:+5215500000000", "+52 155 0000 0000", "5500000000"]
)
def test_alta_en_cualquier_formato_actualiza_al_mismo_cliente(sesion_panel, formato):
    # REGLA: LA prueba de esta fase. Si un formato distinto creara una fila
    # nueva, el cliente quedaría partido en dos y cobranza le abriría dos
    # calendarios sobre la misma tarjeta.
    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Nombre viejo"))
        s.commit()

    _alta(sesion_panel, formato, "Ana Ruiz")

    with Session(db.engine) as s:
        clientes = s.exec(select(Client)).all()
    assert len(clientes) == 1, f"el formato {formato!r} duplicó el cliente"
    assert clientes[0].name == "Ana Ruiz", "no se actualizó el nombre"


def test_dar_de_alta_a_un_cliente_bloqueado_no_lo_desbloquea(sesion_panel):
    # REGLA: `habilitado=False` lo pone un admin a propósito (cliente abusivo,
    # moroso). El alta NO puede ser la puerta trasera para revertirlo.
    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Bloqueado", habilitado=False))
        s.commit()

    _alta(sesion_panel, CANONICO, "Nombre nuevo")

    with Session(db.engine) as s:
        cliente = s.get(Client, CANONICO)
    assert cliente.habilitado is False, "el alta re-habilitó a un cliente bloqueado"
    assert cliente.name == "Nombre nuevo", "el nombre sí debía actualizarse"


def test_el_alta_no_toca_la_conversacion_en_curso(sesion_panel):
    # REGLA: registrar a alguien que va a media solicitud no puede reiniciar su
    # flujo — perdería el avance y el bot le volvería a preguntar todo.
    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Ana"))
        s.add(
            Conversation(
                phone=CANONICO,
                state="menu_servicio",
                context_json='{"categoria": "actas"}',
                failed_attempts=2,
            )
        )
        s.commit()

    _alta(sesion_panel, CRUDO, "Ana Ruiz")

    with Session(db.engine) as s:
        conv = s.get(Conversation, CANONICO)
    assert conv.state == "menu_servicio"
    assert conv.context_json == '{"categoria": "actas"}'
    assert conv.failed_attempts == 2


def test_el_alta_no_reescribe_la_fecha_de_alta_original(sesion_panel):
    # REGLA: `created_at` es cuándo llegó el cliente al negocio. Editarle el
    # nombre no puede hacerlo parecer nuevo en los reportes de captación.
    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Ana"))
        s.commit()
        original = s.get(Client, CANONICO).created_at

    _alta(sesion_panel, CANONICO, "Ana Ruiz")

    with Session(db.engine) as s:
        assert s.get(Client, CANONICO).created_at == original


# ---------------------------------------------------------------------------
# Validación y auditoría
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basura", ["   ", "no soy teléfono", "123"])
def test_un_telefono_invalido_no_crea_nada(sesion_panel, basura):
    # REGLA: `phone` es la LLAVE PRIMARIA. Una basura ahí es un cliente
    # imposible de encontrar y, en cobranza, un cobro que nadie concilia.
    _alta(sesion_panel, basura, "Ana Ruiz")

    with Session(db.engine) as s:
        assert s.exec(select(Client)).all() == []


def test_sin_nombre_no_se_da_de_alta(sesion_panel):
    # REGLA: el alta existe justamente para poner nombre. Un cliente sin nombre
    # no aporta nada sobre el que crea el webhook solo.
    _alta(sesion_panel, CANONICO, "   ")

    with Session(db.engine) as s:
        assert s.exec(select(Client)).all() == []


def test_el_alta_y_el_cambio_de_nombre_quedan_auditados(sesion_panel):
    # REGLA: a este cliente se le van a cobrar tarjetas. Quién le puso ese
    # nombre y cuándo es lo primero que se pregunta en una aclaración.
    _alta(sesion_panel, CANONICO, "Ana Ruiz")
    _alta(sesion_panel, CANONICO, "Ana Ruiz Pérez")

    with Session(db.engine) as s:
        acciones = [a.action for a in s.exec(select(AdminAudit)).all()]
    assert "cliente:alta" in acciones
    assert "cliente:nombre" in acciones


def test_reenviar_el_mismo_nombre_no_genera_ruido_en_la_bitacora(sesion_panel):
    # REGLA: un doble clic no puede ensuciar la auditoría con cambios que no
    # ocurrieron; la bitácora pierde valor si se llena de no-eventos.
    _alta(sesion_panel, CANONICO, "Ana Ruiz")
    _alta(sesion_panel, CANONICO, "Ana Ruiz")

    with Session(db.engine) as s:
        acciones = [a.action for a in s.exec(select(AdminAudit)).all()]
    assert acciones.count("cliente:nombre") == 0
    assert acciones.count("cliente:alta") == 1


# ---------------------------------------------------------------------------
# Directorio
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("termino", ["+52 155 0000 0000", CRUDO, "5500000000", "Ana"])
def test_buscar_por_telefono_en_cualquier_formato_encuentra_al_cliente(sesion_panel, termino):
    # REGLA: si buscar "+52 155 0000 0000" no encuentra al cliente guardado como
    # "525500000000", el asesor concluye que no existe y lo da de alta otra vez.
    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Ana Ruiz"))
        s.commit()

    assert "Ana Ruiz" in sesion_panel.get(f"/clients?q={termino}").text, (
        f"buscar {termino!r} no encontró al cliente"
    )


def test_el_directorio_no_lista_clientes_deshabilitados(sesion_panel):
    # REGLA: `habilitado` es el soft delete del panel y todas las vistas lo
    # respetan. El directorio no puede ser la excepción que revive a un
    # bloqueado.
    with Session(db.engine) as s:
        s.add(Client(phone=CANONICO, name="Bloqueado", habilitado=False))
        s.add(Client(phone="525511112222", name="Visible"))
        s.commit()

    html = sesion_panel.get("/clients").text
    assert "Visible" in html
    assert "Bloqueado" not in html
