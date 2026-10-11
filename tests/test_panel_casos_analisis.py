"""El panel de los casos para análisis (casos-analisis PR6, tareas 35–38).

Lo que se protege: el asesor ve a qué folio quedó ligado el mensaje del cliente; el equipo puede
decir si un caso para análisis era de verdad un caso (es el KPI de la regla); quien edita el
catálogo se entera si un sinónimo apaga una frase de la revisión; y el probador del Estudio muestra
lo que verían las reglas nuevas. Si una se pone roja: el asesor no sabe qué mensaje es de qué
folio, el KPI se queda sin datos, un sinónimo apaga la revisión sin que nadie lo note, o el
probador rechaza un caso real por largo.
"""

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.catalog import catalog
from app.config import settings
from app.crm import _mensaje_json
from app.main import app
from app.models import Direction, Message, Ticket, TicketActivity, TicketStatus, User
from app.synonym_miner import CAPA_COLORS

CLAVE = "clave-de-prueba-1234"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
PHONE = "5215544445555"
# El caso real del 2026-09-24 con claves sintéticas (las reales no salen de producción).
CASO_REAL = (
    "Escribo por el trabajador. Tiene dos registros: CURP anterior AAAA000101HDFBBB01 asociada al "
    "NSS 12 34567890 1, con 694 semanas cotizadas; CURP oficial vigente AAAA000101HDFBBB09 asociada "
    "al NSS 98 76543210 0, con 0 semanas cotizadas. Ya corrigió sus datos ante RENAPO. Por lo "
    "anterior pedimos la unificación de los NSS, la vinculación del NSS con la CURP vigente y la "
    "constancia de semanas cotizadas, porque hay una duplicidad que no le deja tramitar su pensión."
) * 2


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    for k, v in (("mssql_host", ""), ("anthropic_api_key", ""), ("geoip_enabled", False),
                 ("ollama_url", ""), ("rag_enabled", False)):
        monkeypatch.setattr(settings, k, v)
    with Session(db.engine) as s:
        s.add(User(username="admin", password_hash=HASH, is_admin=True))
        s.add(User(username="colab", password_hash=HASH, is_admin=False))
        s.commit()
    yield
    catalog._refresh()


def _sesion_de(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303, "el login de la prueba falló"
    return cliente


def _folio(service_id="analisis", **extra) -> int:
    with Session(db.engine) as s:
        t = Ticket(phone=PHONE, category="(análisis)", service_id=service_id,
                   service_name="Caso para análisis", price_info="N/A", escalado=True,
                   status=TicketStatus.NUEVO, assigned_to="colab", **extra)
        s.add(t)
        s.commit()
        return t.id


def _acciones(ticket_id: int) -> list[str]:
    with Session(db.engine) as s:
        return [a.action for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == ticket_id))]


# --- 35. La burbuja del cliente ligada a su folio ---------------------------------------------------


def test_el_mensaje_del_cliente_ligado_lleva_su_folio_y_el_suelto_no():
    folio = Ticket(id=63, phone=PHONE, category="c", service_id="x",
                   service_name="Unificación de NSS", price_info="$1")
    ligado = Message(id=1, phone=PHONE, direction=Direction.IN, body="ya tengo la CURP", ticket_id=63)
    suelto = Message(id=2, phone=PHONE, direction=Direction.IN, body="ok")

    assert _mensaje_json(ligado, "ana", {63: folio})["folio"] == {"id": 63, "service_name": "Unificación de NSS"}
    assert _mensaje_json(suelto, "ana", {63: folio})["folio"] is None


def test_el_js_pinta_el_folio_tambien_en_las_burbujas_del_cliente():
    """Mutación: con `m.direction === "out"` de vuelta, la burbuja ligada sale sin folio. El juez
    del CRM (criterio 9c) lo verifica en el navegador; esto fija la condición en la fuente."""
    from pathlib import Path

    js = Path("static/js/crm.js").read_text(encoding="utf-8")
    assert "if (m.folio) {" in js
    assert 'm.folio && m.direction === "out"' not in js


# --- 36. Veredicto del colaborador en el folio de análisis -----------------------------------------


def test_el_veredicto_se_registra_y_se_ve_en_el_folio():
    folio = _folio()
    c = _sesion_de("colab")

    r = c.post(f"/tickets/{folio}/veredicto", data={"valor": "caso"}, follow_redirects=False)

    assert r.status_code == 303 and r.headers["location"] == f"/tickets/{folio}"
    assert "veredicto:caso" in _acciones(folio)
    html = c.get(f"/tickets/{folio}").text
    assert "¿Era de verdad un caso para análisis?" in html and "Sí, era un caso" in html
    c.post(f"/tickets/{folio}/veredicto", data={"valor": "tramite-normal"}, follow_redirects=False)
    assert "Veredicto:</strong> <span class=\"badge asignada\">No, era un trámite normal" in c.get(f"/tickets/{folio}").text


def test_el_anonimo_no_da_veredicto():
    folio = _folio()
    r = TestClient(app, base_url="https://testserver").post(
        f"/tickets/{folio}/veredicto", data={"valor": "caso"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert not any(a.startswith("veredicto") for a in _acciones(folio))


def test_un_valor_ajeno_o_un_folio_normal_no_registran_nada():
    caso, normal = _folio(), _folio("sb-curp")
    c = _sesion_de("colab")
    c.post(f"/tickets/{caso}/veredicto", data={"valor": "lo-que-sea"}, follow_redirects=False)
    c.post(f"/tickets/{normal}/veredicto", data={"valor": "caso"}, follow_redirects=False)
    assert not any(a.startswith("veredicto") for a in _acciones(caso) + _acciones(normal))
    assert "¿Era de verdad un caso" not in c.get(f"/tickets/{normal}").text


# --- 37. /catalog avisa si un sinónimo choca con FRASES_REVISION ------------------------------------


def _editar_sinonimos(c: TestClient, service_id: str, sinonimos: str):
    s = catalog.service_by_id(service_id)
    return c.post(f"/catalog/services/{service_id}/update", data={
        "name": s.name, "price_info": s.price_info, "synonyms": sinonimos,
        "requirements": s.requirements,
    }, follow_redirects=False)


def test_un_sinonimo_que_choca_se_guarda_y_avisa():
    c = _sesion_de("admin")
    sid = catalog.categories[0].services[0].id

    r = _editar_sinonimos(c, sid, "Controversia, papeleo")

    assert r.status_code == 303 and r.headers["location"] == "/catalog?choque=Controversia"
    assert "Controversia" in catalog.service_by_id(sid).synonyms, "el aviso no debe bloquear"
    html = c.get(r.headers["location"]).text
    assert "«Controversia»" in html and "se le ofrezca la revisión" in html


def test_un_sinonimo_normal_no_avisa():
    c = _sesion_de("admin")
    sid = catalog.categories[0].services[0].id
    r = _editar_sinonimos(c, sid, "papeleo")
    assert r.headers["location"] == "/catalog"
    assert "se le ofrezca la revisión" not in c.get("/catalog").text


# --- 38. El probador del Estudio -------------------------------------------------------------------


def test_el_probador_muestra_las_tres_capas_del_caso_real():
    c = _sesion_de("admin")
    assert 500 < len(CASO_REAL) <= 2000

    r = c.post("/agente/probar", json={"texto": CASO_REAL})

    assert r.status_code == 200, r.text
    caso = r.json()["caso"]
    assert len(caso["servicios_nombrados"]) >= 2
    assert caso["informativo"] and caso["informativo_via"] == "largo"
    assert caso["pide_revision"] and {"duplicidad", "ante renapo"} <= set(caso["frases"])
    assert caso["contexto_folio_enabled"] is False and caso["revision_enabled"] is False


def test_el_probador_reporta_la_via_y_las_colisiones(monkeypatch):
    c = _sesion_de("admin")
    sid = catalog.categories[0].services[0].id

    caso = c.post("/agente/probar", json={"texto": "tengo una situación con mi pensión, qué se puede hacer"}).json()["caso"]
    assert caso["informativo_via"] == "revision" and caso["pide_revision"]

    _editar_sinonimos(c, sid, "controversia")
    caso = c.post("/agente/probar", json={"texto": "tengo una controversia y una situacion"}).json()["caso"]
    # «controversia» ya es sinónimo: se ignora como frase y cuenta como UN servicio nombrado, así
    # que el texto (corto, un servicio) deja de pedir revisión. Es justo lo que avisa /catalog.
    assert caso["colisiones"] == ["controversia"] and "situacion" in caso["frases"]
    assert [s["id"] for s in caso["servicios_nombrados"]] == [sid]
    assert not caso["pide_revision"]


def test_el_probador_rechaza_mas_de_2000():
    c = _sesion_de("admin")
    assert c.post("/agente/probar", json={"texto": "a" * 2001}).status_code == 400


def test_las_capas_nuevas_tienen_color_en_la_dona():
    assert {"folio", "analisis"} <= set(CAPA_COLORS)
