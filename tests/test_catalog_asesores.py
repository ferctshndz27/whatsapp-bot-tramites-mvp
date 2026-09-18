"""/catalog de todos los colaboradores, y quién atiende qué producto (CatalogAsesor).

Regla del operador (2026-09-18): el catálogo sigue siendo UNO para todas las
oficinas (SPEC-MULTI-OFICINA §11.3), pero ya no es solo del admin global — lo
editan todos, y ahí declara cada colaborador qué productos atiende para que la
asignación le mande esos folios (las reglas de reparto están en
tests/test_fase3b_asignacion.py). Lo que se asevera aquí:

- que un colaborador entra y el menú se lo ofrece;
- que inscribirse y darse de baja reusa la MISMA fila (soft delete, como
  TicketLabel), no duplica y queda en el historial con su nombre;
- que nadie puede inscribir a otro: la ruta toma el usuario de la sesión;
- que cada oficina ve a SU gente — en la columna y en el historial —, aunque el
  catálogo sea de todas. El admin global ve a todos (control positivo);
- que el índice único parcial impide dos inscripciones vivas iguales.
"""

import re

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.catalog import catalog
from app.config import settings
from app.main import app
from app.models import CatalogAsesor, CatalogAudit, Office, User

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
ACTA = "sb-acta-nacimiento"


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()  # crea también el índice ux_catalogasesor_vivo
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        for nombre, admin, oficina in (
            ("jefa1", True, 1),     # admin global
            ("colab1", False, 1),   # Tlaxcala
            ("colab2", False, 2),   # Puebla
        ):
            s.add(User(username=nombre, password_hash=HASH, is_admin=admin, office_id=oficina))
        s.commit()
    catalog.categories  # importa la semilla del catálogo
    yield


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303, f"el login de {username} falló"
    return cliente


def _inscripciones(service_id: str = ACTA) -> list[tuple[str, bool]]:
    with Session(db.engine) as s:
        filas = s.exec(select(CatalogAsesor).where(CatalogAsesor.service_id == service_id)
                       .order_by(CatalogAsesor.id)).all()
        return [(f.username, f.habilitado) for f in filas]


def _atender(cliente: TestClient, quiere: bool, service_id: str = ACTA, **extra):
    r = cliente.post(f"/catalog/services/{service_id}/atencion",
                     data={"atiende": "si" if quiere else "no", **extra},
                     follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/catalog", r.text[:200]


def _quien_atiende_en_pagina(html: str, service_name: str) -> list[str]:
    bloque = re.search(
        rf'<ul class="asesores" aria-label="Quién atiende {re.escape(service_name)}">(.*?)</ul>',
        html, re.S,
    )
    return re.findall(r">([^<]+)</li>", bloque.group(1)) if bloque else []


def test_un_colaborador_entra_y_el_menu_se_lo_ofrece():
    cliente = _sesion("colab1")
    r = cliente.get("/catalog")
    assert r.status_code == 200
    assert 'href="/catalog"' in r.text, "el menú no le ofrece el catálogo"
    assert "Lo atiendo" in r.text


def test_inscribirse_y_darse_de_baja_reusa_la_misma_fila_y_queda_en_el_historial():
    colab = _sesion("colab1")
    _atender(colab, True)
    _atender(colab, True)  # doble envío: ya estaba, no escribe nada
    assert _inscripciones() == [("colab1", True)]

    _atender(colab, False)
    assert _inscripciones() == [("colab1", False)], "darse de baja no apagó la fila"

    _atender(colab, True)
    assert _inscripciones() == [("colab1", True)], "volver a inscribirse duplicó la fila"

    with Session(db.engine) as s:
        historial = s.exec(select(CatalogAudit).where(CatalogAudit.field == "asesores")
                           .order_by(CatalogAudit.id)).all()
    # Tres cambios reales (el doble envío no cuenta), con su nombre y SOLO su cambio:
    # la lista completa mezclaría gente de todas las oficinas.
    assert [(h.admin, h.old_value, h.new_value) for h in historial] == [
        ("colab1", "no lo atiende", "lo atiende"),
        ("colab1", "lo atiende", "no lo atiende"),
        ("colab1", "no lo atiende", "lo atiende"),
    ]


def test_nadie_puede_inscribir_a_otro():
    """Si la ruta aceptara un nombre, cualquiera podría llenarle la bandeja a un
    compañero inscribiéndolo en productos ajenos."""
    _atender(_sesion("colab1"), True, username="colab2")
    assert _inscripciones() == [("colab1", True)]


def test_cada_oficina_ve_a_su_gente_aunque_el_catalogo_sea_de_todas():
    _atender(_sesion("colab1"), True)
    _atender(_sesion("colab2"), True)
    nombre = catalog.service_by_id(ACTA).name

    de_puebla = _sesion("colab2").get("/catalog").text
    assert _quien_atiende_en_pagina(de_puebla, nombre) == ["colab2"]
    # Tampoco por el historial: ahí también estaba colab1 inscribiéndose.
    assert "colab1" not in de_puebla, "Puebla ve a la gente de Tlaxcala"

    global_ = _sesion("jefa1").get("/catalog").text
    assert _quien_atiende_en_pagina(global_, nombre) == ["colab1", "colab2"]


def test_quien_esta_dado_de_baja_deja_de_aparecer():
    _atender(_sesion("colab1"), True)
    with Session(db.engine) as s:
        u = s.exec(select(User).where(User.username == "colab1")).one()
        u.habilitado = False
        s.add(u)
        s.commit()
    assert catalog.asesores_por_servicio().get(ACTA, []) == []


def test_el_indice_impide_dos_inscripciones_vivas_iguales():
    with Session(db.engine) as s:
        s.add(CatalogAsesor(service_id=ACTA, username="colab1"))
        s.add(CatalogAsesor(service_id=ACTA, username="colab1"))
        with pytest.raises(IntegrityError):
            s.commit()
