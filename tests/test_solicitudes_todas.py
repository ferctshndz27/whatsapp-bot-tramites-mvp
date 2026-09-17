"""Solicitudes: lo que nadie atiende va arriba y lo cerrado vive en su chip.

REGLA DEL OPERADOR (2026-09-16), en dos partes:

1. Un folio cerrado solo se lista en el chip «cerrado». En «todas», «sin
   asignar», «escalados» o con una etiqueta sola se revolvía con el trabajo
   vivo, y cerrar no apaga `escalado`: «escalados» lo arrastraba para siempre.
2. «todas» abre con las nuevas sin asignar. Ordenadas solo por fecha, una nueva
   que nadie tomó quedaba debajo de las ya tomadas, a veces en otra página, y
   es justo la que nadie está atendiendo.

Si esto se pone rojo, la asesora vuelve a buscar entre folios terminados lo que
le toca, o una solicitud sin dueño se queda esperando donde nadie la ve.
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
from app.dashboard import PER_PAGE
from app.main import app
from app.models import Label, Ticket, TicketLabel, TicketStatus, User, utcnow

CLAVE = "clave-de-prueba"
# rounds=4: con el costo por defecto (12) cada login del test costaría ~0.3 s.
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


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
    with Session(db.engine) as s:
        s.add(User(username="asesora", password_hash=HASH_CLAVE))
        s.commit()
    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.post(
            "/login", data={"username": "asesora", "password": CLAVE}, follow_redirects=False
        )
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        yield cliente


def _folio(s: Session, *, hace_horas: float, status=TicketStatus.NUEVO, dueno=None,
           escalado=False) -> int:
    t = Ticket(
        phone="525500000001",
        client_name="Ana Ruiz",
        category="Actas",
        service_id="sb-acta",
        service_name="Acta de nacimiento",
        price_info="$120",
        status=status,
        assigned_to=dueno,
        escalado=escalado,
        created_at=utcnow() - timedelta(hours=hace_horas),
    )
    s.add(t)
    s.flush()
    return t.id


def _filas(html: str) -> list[int]:
    """Folios de la tabla, en el orden en que se pintan (un data-href por fila)."""
    return [int(n) for n in re.findall(r'<tr data-href="/tickets/(\d+)', html)]


def _chip(html: str, etiqueta: str) -> int:
    encontrado = re.search(">" + re.escape(etiqueta) + r" \((\d+)\)<", html)
    assert encontrado, f"el chip «{etiqueta}» no trae conteo"
    return int(encontrado.group(1))


def _tarjeta(html: str, titulo: str) -> int:
    encontrado = re.search(re.escape(titulo) + r'</div>\s*<div class="value">(\d+)<', html)
    assert encontrado, f"falta la tarjeta «{titulo}»"
    return int(encontrado.group(1))


@pytest.fixture()
def cola(http):
    """Un folio por cada situación que produce el panel, por nombre → id.

    El cerrado es a propósito el más reciente, no tiene dueño (un admin puede
    terminar un folio que nadie tomó), está escalado y lleva etiqueta: cumple lo
    que piden «todas», «sin asignar», «escalados» y la etiqueta, así que si no
    sale en ellas es por estar cerrado y por nada más."""
    with Session(db.engine) as s:
        urgente = Label(nombre="urgente")
        s.add(urgente)
        s.flush()
        folios = {
            "cerrado": _folio(s, hace_horas=0.5, status=TicketStatus.CERRADO, escalado=True),
            "tomado": _folio(s, hace_horas=1, status=TicketStatus.TOMADO, dueno="asesora",
                             escalado=True),
            # El reparto automático deja la nueva con dueño y todavía en NUEVO.
            "nueva_repartida": _folio(s, hace_horas=2, dueno="asesora"),
            # users.mover_de_oficina le quita el dueño sin regresarla a NUEVO.
            "tomado_huerfano": _folio(s, hace_horas=3, status=TicketStatus.TOMADO),
            "nueva_reciente": _folio(s, hace_horas=4),
            "en_proceso": _folio(s, hace_horas=5, status=TicketStatus.EN_PROCESO,
                                 dueno="asesora"),
            "nueva_vieja": _folio(s, hace_horas=6),
        }
        for nombre in ("cerrado", "tomado"):
            s.add(TicketLabel(ticket_id=folios[nombre], label_id=urgente.id))
        s.commit()
    return folios


def _listados(http, cola, url: str) -> list[str]:
    por_id = {folio: nombre for nombre, folio in cola.items()}
    return [por_id[f] for f in _filas(http.get(url).text)]


@pytest.mark.parametrize(
    "url,esperados",
    [
        ("/", {"tomado", "nueva_repartida", "tomado_huerfano", "nueva_reciente",
               "en_proceso", "nueva_vieja"}),
        ("/?assigned=none", {"tomado_huerfano", "nueva_reciente", "nueva_vieja"}),
        ("/?status=escalados", {"tomado"}),
        ("/?etiqueta=urgente", {"tomado"}),
        ("/?status=cerrado", {"cerrado"}),
        ("/?status=cerrado&etiqueta=urgente", {"cerrado"}),
    ],
)
def test_un_folio_cerrado_solo_se_lista_en_el_chip_cerrado(http, cola, url, esperados):
    assert set(_listados(http, cola, url)) == esperados


def test_los_conteos_tampoco_cuentan_el_cerrado_fuera_de_su_chip(http, cola):
    """Un chip que cuenta un folio que su lista ya no muestra manda a buscar lo
    que no está. La tarjeta «Escaladas» es la misma cuenta que su chip; el total
    de arriba sí sigue siendo el histórico, cerradas incluidas."""
    html = http.get("/").text
    assert _chip(html, "todas") == 6
    assert _chip(html, "sin asignar") == 3
    assert _chip(html, "escalados") == 1
    assert _chip(html, "cerrado") == 1, "el chip cerrado dejó de contar el suyo"
    assert _tarjeta(html, "Escaladas") == 1
    assert _tarjeta(html, "Total de solicitudes") == 7


def test_todas_lista_primero_las_nuevas_sin_asignar(http, cola):
    """Cuentan las dos condiciones: la nueva que repartió el bot ya tiene dueño y
    la tomada que se quedó sin él no es nueva, así que ninguna de las dos sube.
    Dentro de cada grupo sigue mandando la fecha, la más reciente primero."""
    assert _listados(http, cola, "/") == [
        "nueva_reciente", "nueva_vieja",
        "tomado", "nueva_repartida", "tomado_huerfano", "en_proceso",
    ]


def test_la_nueva_sin_asignar_encabeza_la_pagina_1_aunque_sea_la_mas_vieja(http):
    """Ordenar después de paginar la dejaría en la página 2, detrás de una página
    entera de folios tomados: justo el caso que la regla vino a arreglar."""
    with Session(db.engine) as s:
        for hace_horas in range(1, PER_PAGE + 1):
            _folio(s, hace_horas=hace_horas, status=TicketStatus.TOMADO, dueno="asesora")
        vieja = _folio(s, hace_horas=100)
        s.commit()
    filas = _filas(http.get("/").text)
    assert len(filas) == PER_PAGE, "la página 1 no viene completa"
    assert filas[0] == vieja, "la nueva sin asignar quedó fuera del inicio de la página 1"
