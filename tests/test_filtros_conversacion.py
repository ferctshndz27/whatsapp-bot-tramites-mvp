"""Los filtros de conversación fusionados en la vista de Solicitudes.

REGLA: los chips «esperando respuesta», «caducadas» y «bot pausado» viven ahora
en la fila de filtros de «/», y tienen que listar EXACTAMENTE lo mismo que la
Bandeja — es el requisito que permite borrar la Bandeja después. Si un predicado
se duplica en vez de compartirse, esta prueba lo cacha: el chip diría un número
y la lista mostraría otro.
"""

import re
from datetime import timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app.config import settings
from app.main import app
from app.models import Client, Conversation, Direction, Message, utcnow

CLAVE = "clave-de-prueba"
# rounds=4: con el costo por defecto (12) cada login del test costaría ~0.3 s.
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()

ESPERANDO = "525500000001"  # último mensaje entrante, ventana viva
CADUCADA = "525500000002"  # último entrante hace 30 h: fuera de la ventana de 24 h
PAUSADA = "525500000003"  # conversación escalada: el bot está apagado

FILTROS = ["esperando", "caducadas", "pausadas"]


@pytest.fixture(autouse=True)
def _base_compartida(monkeypatch):
    """Engine con StaticPool para que el hilo del TestClient vea la misma base."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    yield


@pytest.fixture()
def http():
    from app.models import User

    ahora = utcnow()
    with Session(db.engine) as s:
        s.add(User(username="asesora", password_hash=HASH_CLAVE))

        # Espera respuesta: lo último lo dijo el cliente y la ventana sigue viva.
        s.add(Client(phone=ESPERANDO, name="Ana Ruiz"))
        s.add(Conversation(phone=ESPERANDO, state="menu_categoria"))
        s.add(Message(phone=ESPERANDO, direction=Direction.IN, body="sigo esperando"))

        # Caducada: el último entrante fue hace 30 h, así que ya no se le puede
        # escribir libre. Responder después no reabre la ventana.
        s.add(Client(phone=CADUCADA, name="Beto Lara"))
        s.add(Conversation(phone=CADUCADA, state="menu_categoria"))
        s.add(
            Message(
                phone=CADUCADA,
                direction=Direction.IN,
                body="hola",
                created_at=ahora - timedelta(hours=30),
            )
        )
        s.add(
            Message(
                phone=CADUCADA,
                direction=Direction.OUT,
                body="te confirmo",
                created_at=ahora - timedelta(hours=29),
            )
        )

        # Bot pausado: conversación escalada a un humano.
        s.add(Client(phone=PAUSADA, name="Carla Gil"))
        s.add(Conversation(phone=PAUSADA, state="escalado"))
        s.add(Message(phone=PAUSADA, direction=Direction.OUT, body="ya te atiendo"))
        s.commit()

    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.post(
            "/login", data={"username": "asesora", "password": CLAVE}, follow_redirects=False
        )
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        yield cliente


def _telefonos(html: str) -> set[str]:
    """Teléfonos de las tarjetas de conversación que trae la página."""
    return set(re.findall(r'href="/inbox/(\d+)', html))


@pytest.mark.parametrize("filtro", FILTROS)
def test_filtro_lista_lo_mismo_que_la_bandeja(http, filtro):
    solicitudes = http.get(f"/?conv={filtro}")
    bandeja = http.get(f"/inbox?filter={filtro}")
    assert solicitudes.status_code == 200
    assert _telefonos(solicitudes.text) == _telefonos(bandeja.text)


@pytest.mark.parametrize(
    "filtro,esperados",
    [
        ("esperando", {ESPERANDO}),  # en CADUCADA lo último lo dijimos nosotros
        # PAUSADA nunca escribió, así que tampoco tiene ventana abierta: la
        # Bandeja la cuenta como caducada y aquí tiene que pasar lo mismo.
        ("caducadas", {CADUCADA, PAUSADA}),
        ("pausadas", {PAUSADA}),
    ],
)
def test_cada_filtro_trae_las_conversaciones_correctas(http, filtro, esperados):
    assert _telefonos(http.get(f"/?conv={filtro}").text) == esperados


def test_los_conteos_de_los_chips_coinciden_con_la_bandeja(http):
    inicio = http.get("/").text
    bandeja = http.get("/inbox").text
    for etiqueta in ("esperando respuesta", "caducadas", "bot pausado"):
        patron = re.escape(etiqueta) + r" \((\d+)\)"
        assert re.search(patron, inicio), f"falta el chip «{etiqueta}» en /"
        assert re.search(patron, inicio).group(1) == re.search(patron, bandeja).group(1)


def test_sin_filtro_la_vista_sigue_listando_folios(http):
    html = http.get("/").text
    # El encabezado de la tabla de folios solo existe en el modo solicitudes.
    assert "<h2>Solicitudes</h2>" in html
    assert "convo-card" not in html


def test_un_filtro_desconocido_no_deja_la_vista_en_blanco(http):
    """?conv=basura equivale a «todas»: nunca una lista vacía sin chip activo."""
    html = http.get("/?conv=basura").text
    assert "<h2>Solicitudes</h2>" in html
    assert 'href="/" aria-current="true"' in html


def test_el_chip_todas_no_esta_repetido(http):
    assert len(re.findall(r">todas \(\d+\)<", http.get("/").text)) == 1


def _folios(html: str) -> set[str]:
    """Folios que trae la tabla de solicitudes."""
    return set(re.findall(r'href="/tickets/(\d+)', html))


@pytest.fixture()
def con_folios(http):
    """Cuatro folios en tres estados; dos de ellos sin asignar."""
    from app.models import Ticket, TicketStatus

    estados = [
        (TicketStatus.NUEVO, None),
        (TicketStatus.NUEVO, None),
        (TicketStatus.TOMADO, "asesora"),
        (TicketStatus.CERRADO, "asesora"),
    ]
    with Session(db.engine) as s:
        for estado, dueno in estados:
            s.add(
                Ticket(
                    phone=ESPERANDO,
                    client_name="Ana Ruiz",
                    category="Actas",
                    service_id="sb-acta",
                    service_name="Acta de nacimiento",
                    price_info="$120",
                    status=estado,
                    assigned_to=dueno,
                )
            )
        s.commit()
    return http


@pytest.mark.parametrize(
    "etiqueta,esperado",
    [("todas", 4), ("nuevo", 2), ("tomado", 1), ("en proceso", 0), ("cerrado", 1),
     ("sin asignar", 2)],
)
def test_cada_chip_de_folio_muestra_su_conteo(con_folios, etiqueta, esperado):
    html = con_folios.get("/").text
    encontrado = re.search(re.escape(etiqueta) + r" \((\d+)\)", html)
    assert encontrado, f"el chip «{etiqueta}» no trae conteo"
    assert int(encontrado.group(1)) == esperado


@pytest.mark.parametrize(
    "url,esperado",
    [("/", 4), ("/?status=nuevo", 2), ("/?status=tomado", 1), ("/?status=cerrado", 1),
     ("/?assigned=none", 2)],
)
def test_cada_chip_de_folio_filtra_lo_que_promete(con_folios, url, esperado):
    """El conteo del chip y las filas que abre tienen que coincidir: si no, el
    número miente sobre lo que la operadora va a ver."""
    assert len(_folios(con_folios.get(url).text)) == esperado


def test_los_conteos_no_cambian_al_activar_un_filtro(con_folios):
    """Los chips cuentan sobre el total, no sobre lo ya filtrado — si no, todos
    menos el activo dirían 0 y dejarían de servir para navegar."""
    sin_filtro = con_folios.get("/").text
    filtrada = con_folios.get("/?status=cerrado").text
    chips = re.compile(r">([a-zñáéíóú ]+) \((\d+)\)<")
    assert dict(chips.findall(sin_filtro)) == dict(chips.findall(filtrada))


def test_volver_desde_una_conversacion_regresa_al_filtro(http):
    """Entrar a un chat desde «/» y volver tiene que restaurar el filtro, no
    tirar a la operadora a la vista sin filtros."""
    html = http.get("/?conv=pausadas").text
    assert "volver=/%3Fconv%3Dpausadas" in html
    chat = http.get(f"/inbox/{PAUSADA}?volver=/%3Fconv%3Dpausadas")
    assert 'href="/?conv=pausadas"' in chat.text
