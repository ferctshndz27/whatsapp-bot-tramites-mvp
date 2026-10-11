"""El buscador de /crm encuentra también el texto de los mensajes (casos-analisis PR8).

Lo que se protege: un asesor que recuerda «el de la duplicidad ante RENAPO» encuentra el chat sin
saber el nombre; y la búsqueda nunca le enseña a nadie un pedazo de una conversación que no puede
ver. Si una se pone roja: o el buscador deja de encontrar por texto, o un fragmento de otra oficina
(o de un cliente que no es tuyo) sale en tu tablero, o el texto que se buscó queda en los logs.
Base: la de tests/test_crm.py (Ana y Beto en Tlaxcala, Carla en Puebla; Ana es de colab1).
"""

import logging
from datetime import timedelta

from sqlmodel import Session, select

import app.db as db
from app import crm
from app.main import SinBusquedaEnLogs
from app.models import Client, Direction, Message, Ticket, TicketStatus, utcnow
from tests.test_crm import ANA, BETO, CARLA, _base, _sesion, _sse_entre_loops, _tablero  # noqa: F401

RENAPO = ("Buen día. El trabajador tiene dos CURP y una duplicidad de NSS; ya corrigió sus datos "
          "ante RENAPO y necesitamos la unificación para que pueda tramitar su pensión este mes.")


def _mensaje(phone: str, body: str, minutos: int = 1, **extra) -> int:
    with Session(db.engine) as s:
        m = Message(phone=phone, direction=Direction.IN, body=body,
                    created_at=utcnow() - timedelta(minutes=minutos), **extra)
        s.add(m)
        s.commit()
        return m.id


def _telefonos(t: dict) -> list[str]:
    return [c["phone"] for c in t["contactos"]]


def test_buscar_por_telefono_parcial_encuentra_el_chat_de_mi_cartera():
    """Lo de hoy no cambia: teléfono parcial y nombre sin acentos, sin fragmento de mensaje."""
    assert _telefonos(_tablero("colab1", q="0011")) == [ANA]
    t = _tablero("jefe1", q="ramirez")
    assert _telefonos(t) == [ANA] and t["contactos"][0]["en_mensajes"] is None


def test_buscar_una_palabra_del_cuerpo_encuentra_el_chat_y_muestra_el_fragmento():
    _mensaje(BETO, RENAPO)
    _mensaje(BETO, "otra vez lo de la UNIFICACION", minutos=30)

    t = _tablero("jefe1", q="unificacion")

    assert _telefonos(t) == [BETO]
    hallado = t["contactos"][0]["en_mensajes"]
    assert hallado["n"] == 2, "cuenta los dos mensajes, sin acentos ni mayúsculas"
    # El fragmento es del más reciente, en su texto original, ±60 caracteres alrededor.
    assert "unificación" in hallado["fragmento"]
    # Recorta por delante (hay más de 60 antes) y no por detrás (quedan 46 hasta el final).
    assert hallado["fragmento"].startswith("…") and hallado["fragmento"].endswith("este mes.")
    assert len(hallado["fragmento"]) <= len("unificación") + 2 * crm.FRAGMENTO_RADIO + 2
    # Con «%» o «_» en la búsqueda no hay comodines de SQL.
    assert _tablero("jefe1", q="un_ficac")["contactos"] == []
    assert _tablero("jefe1", q="%%%")["contactos"] == []


def test_no_encuentra_chats_fuera_de_mi_cartera_ni_de_mis_asignados():
    _mensaje(CARLA, RENAPO)   # Puebla
    _mensaje(BETO, RENAPO)    # Tlaxcala, sin folio de colab1 ni en su cartera
    _mensaje(ANA, "y lo de RENAPO, ¿cómo va?")

    assert _telefonos(_tablero("colab1", q="renapo")) == [ANA]
    assert sorted(_telefonos(_tablero("jefe1", q="renapo"))) == sorted([ANA, BETO])
    assert _telefonos(_tablero("colab2", q="renapo")) == []
    assert _telefonos(_tablero("jefe2", q="renapo")) == [CARLA]


def test_el_alcance_se_aplica_antes_de_leer_un_solo_cuerpo(monkeypatch):
    """Ni siquiera se produce el fragmento de un chat ajeno: _fragmento solo ve los suyos."""
    _mensaje(CARLA, RENAPO)
    _mensaje(BETO, RENAPO)
    _mensaje(ANA, "y lo de RENAPO, ¿cómo va?")
    vistos = []
    original = crm._fragmento
    monkeypatch.setattr(crm, "_fragmento", lambda body, aguja: vistos.append(body) or original(body, aguja))

    _tablero("colab1", q="renapo")

    assert vistos == ["y lo de RENAPO, ¿cómo va?"]


def test_un_mensaje_borrado_no_se_busca():
    _mensaje(BETO, RENAPO, habilitado=False)
    assert _tablero("jefa1", q="renapo")["contactos"] == []


def test_todos_solo_para_quien_ve_todas():
    """Sin selector «mios|todos» en /crm: el alcance es el de cada quien. El admin global ve las
    dos oficinas; el de oficina, la suya; el colaborador, sus folios y su cartera."""
    for phone in (ANA, BETO, CARLA):
        _mensaje(phone, RENAPO)
    assert sorted(_telefonos(_tablero("jefa1", q="renapo"))) == sorted([ANA, BETO, CARLA])
    assert sorted(_telefonos(_tablero("jefe1", q="renapo"))) == sorted([ANA, BETO])
    assert _telefonos(_tablero("colab1", q="renapo")) == [ANA]


def test_la_busqueda_no_deja_q_en_los_logs(caplog):
    _mensaje(BETO, RENAPO)
    with caplog.at_level(logging.DEBUG):
        _tablero("jefe1", q="duplicidad")
    # El cliente HTTP de la PRUEBA (httpx) registra su propia URL; en producción no existe.
    de_la_app = [r.getMessage() for r in caplog.records if not r.name.startswith("httpx")]
    assert de_la_app and not any("duplicidad" in m for m in de_la_app)
    # El log de acceso de uvicorn (TestClient no lo emite) se tacha con el filtro de app.main.
    r = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                          ("1.2.3.4:5", "GET", "/crm/api/tablero?q=duplicidad%20renapo&cerradas=1", "1.1", 200), None)
    assert any(isinstance(f, SinBusquedaEnLogs) for f in logging.getLogger("uvicorn.access").filters)
    SinBusquedaEnLogs().filter(r)
    assert r.getMessage() == '1.2.3.4:5 - "GET /crm/api/tablero?q=…&cerradas=1 HTTP/1.1" 200'


def test_menos_de_tres_letras_no_busca_en_mensajes():
    _mensaje(BETO, "ok")
    assert _tablero("jefe1", q="ok")["contactos"] == []


def test_un_cliente_viejo_fuera_del_tope_se_encuentra_por_su_mensaje(monkeypatch):
    monkeypatch.setattr(crm, "LIMITE_CONTACTOS", 1)
    _mensaje(BETO, RENAPO)
    with Session(db.engine) as s:
        beto = s.exec(select(Client).where(Client.phone == BETO)).one()
        beto.created_at = utcnow() - timedelta(days=400)
        s.add(beto)
        s.add(Client(phone="525500000099", name="Reciente", office_id=1))
        s.commit()
    assert _telefonos(_tablero("jefa1", q="renapo")) == [BETO]


def test_busca_tambien_en_folios_cerrados_y_respeta_cerradas():
    _mensaje(BETO, RENAPO)
    with Session(db.engine) as s:
        s.add(Ticket(phone=BETO, category="c", service_id="x", service_name="NSS", price_info="$1",
                     status=TicketStatus.CERRADO, office_id=1))
        s.commit()
    assert BETO in _telefonos(_tablero("jefe1", q="renapo"))
    assert _telefonos(_tablero("jefe1", q="renapo", cerradas=1)) == [BETO]


def test_fragmento_conserva_posiciones_con_emojis_y_ligaduras():
    assert crm._fragmento("👍👍 ﬁrma la unificación 👍", "unificacion") == "👍👍 ﬁrma la unificación 👍"
    assert crm._fragmento("nada", "unificacion") is None
    # «ﬁ» normaliza a dos letras: sin el mapa de posiciones, 80 ligaduras correrían el recorte 80
    # caracteres y la palabra buscada quedaría fuera del fragmento.
    largo = "ﬁ" * 80 + " la unificación " + "x" * 80
    fragmento = crm._fragmento(largo, "unificacion")
    assert "unificación" in fragmento and fragmento.startswith("…ﬁ") and fragmento.endswith("x…")
