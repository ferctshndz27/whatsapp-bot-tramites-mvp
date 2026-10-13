"""«Ninguno más» y «Cancelar» en las preguntas donde el agente puede equivocarse.

Pedido del operador (2026-10-13): si el agente detecta un trámite de más (falso positivo) el
cliente debe poder decir que no necesita otro, y si no entendió bien o el cliente se arrepiente,
debe poder cancelar. Antes la lista de «varios parecidos» solo dejaba elegir uno, y la confirmación
solo tenía Sí/No (el «No» manda al menú, no cierra). Si estas pruebas se ponen rojas, el cliente
vuelve a quedar atrapado eligiendo un trámite que no pidió.
"""

import json
import re

import pytest

from app import db
from app.catalog import catalog
from app.config import settings
from app.conversation import CONFIRM_BUTTONS, handle_incoming, presentation
from app.models import Conversation, Ticket
from sqlmodel import func, select

PHONE = "5215540000010"


@pytest.fixture
def s(session, monkeypatch):
    monkeypatch.setattr(db, "engine", session.get_bind())
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", True)
    session.add(Conversation(phone=PHONE, state="menu_categoria", context_json=json.dumps({})))
    session.commit()
    return session


def _di(s, texto):
    replies = handle_incoming(s, PHONE, texto)
    s.expire_all()
    return "\n".join(replies), s.get(Conversation, PHONE).state


def _folios(s):
    return s.exec(select(func.count()).select_from(Ticket).where(Ticket.phone == PHONE)).one()


def _renglon(texto, que):
    m = re.search(rf"^(\d+)\. .*{que}.*$", texto, re.MULTILINE | re.IGNORECASE)
    assert m, f"no hay renglón «{que}» en: {texto!r}"
    return m.group(1)


# ---- confirmar -------------------------------------------------------------------------------


def test_la_confirmacion_trae_el_boton_cancelar():
    ids = [i for i, _ in CONFIRM_BUTTONS]
    assert ids == ["si", "no", "cancelar"]
    assert len(CONFIRM_BUTTONS) <= 3 and all(len(t) <= 20 for _, t in CONFIRM_BUTTONS)


def test_confirmar_uno_sale_con_tres_botones(s):
    out = presentation(s, PHONE, handle_incoming(s, PHONE, "quiero mi acta de nacimiento"))[-1]
    assert [i for i, _ in out["buttons"]] == ["si", "no", "cancelar"]


@pytest.mark.parametrize("respuesta", ["cancelar", "Cancelar", "cancela", "3", "ya no, cancelar", "me arrepentí"])
def test_cancelar_en_la_confirmacion_no_abre_folio(s, respuesta):
    handle_incoming(s, PHONE, "quiero mi acta de nacimiento")
    texto, estado = _di(s, respuesta)
    assert _folios(s) == 0
    assert estado == "menu_categoria", (estado, texto)
    assert "cancel" in texto.lower(), texto


def test_cancelar_en_la_confirmacion_de_varios(s):
    handle_incoming(s, PHONE, "quiero acta de nacimiento y curp y nss")
    texto, estado = _di(s, "cancelar")
    assert _folios(s) == 0 and estado == "menu_categoria", (estado, texto)


def test_la_confirmacion_de_varios_avisa_que_se_puede_cancelar(s):
    texto, _ = _di(s, "quiero acta de nacimiento y curp y nss")
    assert "cancelar" in texto.lower(), texto


def test_no_sigue_llevando_al_menu_y_si_sigue_abriendo_el_folio(s):
    """Controles: Sí y No no cambian."""
    handle_incoming(s, PHONE, "quiero mi acta de nacimiento")
    texto, estado = _di(s, "no")
    assert estado == "menu_categoria" and _folios(s) == 0 and "Sin problema" in texto
    handle_incoming(s, PHONE, "quiero mi acta de nacimiento")
    _di(s, "sí")
    assert _folios(s) == 1


# ---- «varios parecidos» ----------------------------------------------------------------------


def _acta_y_curp(s):
    texto, estado = _di(s, "quiero acta y curp")
    assert estado == "eligiendo_sugerencia" and "Voy anotando 'Descarga de CURP'" in texto, texto
    return texto


def test_la_lista_ofrece_ninguno_mas_y_cancelar_al_final(s):
    texto = _acta_y_curp(s)
    actas = [m for m in re.findall(r"^(\d+)\. ", texto, re.MULTILINE)]
    ninguno, cancelar = _renglon(texto, "ninguno"), _renglon(texto, "cancelar")
    assert int(ninguno) == len(actas) - 1 and int(cancelar) == len(actas), (actas, texto)


@pytest.mark.parametrize("respuesta", ["NINGUNO", "ninguno", "no necesito más", "ya no requiero más", "nada más"])
def test_ninguno_confirma_solo_lo_anotado(s, respuesta):
    _acta_y_curp(s)
    texto, estado = _di(s, respuesta)
    assert estado == "confirmar", (estado, texto)
    assert "Descarga de CURP" in texto and "Acta" not in texto, texto


def test_ninguno_por_numero(s):
    n = _renglon(_acta_y_curp(s), "ninguno")
    texto, estado = _di(s, n)
    assert estado == "confirmar" and "Descarga de CURP" in texto and "Acta" not in texto, texto
    _di(s, "sí")
    assert _folios(s) == 1


@pytest.mark.parametrize("como", ["palabra", "numero"])
def test_cancelar_en_la_lista_no_abre_nada_ni_siquiera_lo_anotado(s, como):
    n = _renglon(_acta_y_curp(s), "cancelar")
    texto, estado = _di(s, "cancelar" if como == "palabra" else n)
    assert _folios(s) == 0 and estado == "menu_categoria", (estado, texto)
    assert "cancel" in texto.lower(), texto


def test_elegir_un_acta_sigue_funcionando(s):
    """Control: las opciones nuevas no mueven los números de las reales."""
    _acta_y_curp(s)
    texto, estado = _di(s, "1")
    assert estado == "confirmar" and "Descarga de CURP" in texto and "Acta" in texto, texto


def test_empate_sin_nada_anotado_tambien_ofrece_ninguno_y_cancelar(s):
    texto, estado = _di(s, "vigencia de derechos")
    assert estado == "eligiendo_sugerencia", (estado, texto)
    n = _renglon(texto, "ninguno")
    _renglon(texto, "cancelar")
    texto2, estado2 = _di(s, n)
    assert estado2 == "menu_categoria" and _folios(s) == 0, (estado2, texto2)


def test_el_reintento_repite_las_opciones(s):
    _acta_y_curp(s)
    texto, estado = _di(s, "no sé, lo que sea")
    assert estado == "eligiendo_sugerencia", (estado, texto)
    _renglon(texto, "ninguno")
    _renglon(texto, "cancelar")


# ---- plantilla de Twilio ---------------------------------------------------------------------


def test_la_plantilla_de_confirmar_lleva_los_tres_botones(monkeypatch):
    from app import twilio_client

    definicion = twilio_client.definicion_plantilla("tramites_confirmar", list(CONFIRM_BUTTONS))
    acciones = definicion["types"]["twilio/quick-reply"]["actions"]
    assert [a["id"] for a in acciones] == ["si", "no", "cancelar"]

    visto = {}
    monkeypatch.setattr(
        twilio_client, "credencial",
        lambda clave, **_: visto.setdefault("clave", clave) and "HXconfirmar" if clave == "TWILIO_CONTENT_SID_CONFIRMAR" else "",
    )
    assert twilio_client._sid_de_plantilla(("si", "no", "cancelar")) == "HXconfirmar"


# ---- no confundir con un trámite que se llama «cancelar …» -----------------------------------


@pytest.mark.parametrize("texto", ["baja para cancelar retiro", "cancelar retiro por desempleo"])
def test_pedir_el_tramite_de_cancelar_retiro_no_cancela_la_solicitud(s, texto):
    """Hay un servicio «Baja para cancelar retiro por desempleo»: pedirlo no es cancelar."""
    from app.conversation import _quiere_cancelar

    assert not _quiere_cancelar(texto)
