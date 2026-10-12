"""Los archivos que el cliente manda ANTES de contar su caso quedan en el folio del caso (operador,
2026-10-12).

Uso real: un gestor manda por el mismo WhatsApp las fotos de un trabajador (INE, constancia…) y
DESPUÉS escribe el caso. El bot acusa cada foto («Recibí tu archivo 📎») y no tiene folio al que
ligarla; cuando el caso por fin se abre, solo ligaba lo que llegó desde el texto del caso en
adelante: las fotos de antes se quedaban sin folio y el asesor no las veía en la ficha.

Ahora, al abrir el caso se liga también la RACHA de archivos inmediatamente anterior al mensaje que
lo disparó: del mismo teléfono, sin folio, sin un texto del cliente de por medio y de los últimos
minutos. Se liga sin preguntar (un cuarto turno a un flujo que ya tiene tres); equivocarse cuesta
una etiqueta mal puesta en el chat, no ligar cuesta que la foto no aparezca en ningún folio.

Si una de estas pruebas se pone roja: las fotos que el cliente mandó antes de contar su caso no
llegan a la ficha, o se le liga al caso un archivo de otro cliente, de otro asunto (con un texto de
por medio) o de hace horas.
"""

import json
from datetime import timedelta

import pytest
from sqlmodel import select

import app.db as db
from app.config import settings
from app.conversation import (
    MAX_ARCHIVOS_PREVIOS,
    VENTANA_ARCHIVOS_PREVIOS,
    handle_incoming,
)
from app.models import (
    Conversation,
    Direction,
    Message,
    Ticket,
    TicketActivity,
    TicketStatus,
    utcnow,
)

PHONE = "5215533334444"
OTRO = "5215500001111"
SITUACION = "tengo una situación con mi pensión, qué se puede hacer"


@pytest.fixture
def s(session, monkeypatch, request):
    monkeypatch.setattr(db, "engine", session.get_bind())
    request.getfixturevalue("catalogo_vivo")
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", True)
    monkeypatch.setattr(settings, "contexto_min_palabras", 40)
    s_ = session
    s_.add(Conversation(phone=PHONE, state="menu_categoria", context_json=json.dumps({})))
    s_.commit()
    return s_


def _pdf(n: int) -> dict:
    return {"ref": f"media-{n}", "mime": "application/pdf", "nombre": f"doc-{n}.pdf", "caption": ""}


def _manda_archivo(s, n: int):
    return handle_incoming(s, PHONE, "", is_media=True, adjunto=_pdf(n))


def _abre_el_caso(s):
    """El texto del caso y la elección «Revisión»: el camino real de las tres vueltas."""
    handle_incoming(s, PHONE, SITUACION)
    handle_incoming(s, PHONE, "revision")


def _casos(s) -> list[Ticket]:
    s.expire_all()
    return [t for t in s.exec(select(Ticket).where(Ticket.phone == PHONE)).all() if t.service_id == "analisis"]


def _archivo(s, n: int, phone=PHONE) -> Message:
    s.expire_all()
    return s.exec(select(Message).where(Message.phone == phone, Message.media_ref == f"media-{n}")).one()


def _folio_de(s, n: int, phone=PHONE):
    return _archivo(s, n, phone).ticket_id


def _acciones(s, ticket_id: int) -> list[str]:
    return [a.action for a in s.exec(select(TicketActivity).where(TicketActivity.ticket_id == ticket_id))]


def test_los_archivos_de_antes_del_texto_quedan_en_el_folio_del_caso(s):
    """Lo que el operador pidió, de punta a punta: dos fotos, luego el caso. Antes quedaban sin
    folio. Si se pone roja, el asesor abre la ficha del caso y no ve lo que el cliente mandó."""
    _manda_archivo(s, 1)
    _manda_archivo(s, 2)

    _abre_el_caso(s)

    caso = _casos(s)[0]
    assert _folio_de(s, 1) == caso.id and _folio_de(s, 2) == caso.id
    assert "archivo-cliente:2" in _acciones(s, caso.id)


def test_los_archivos_de_antes_y_de_despues_del_texto_se_ligan_todos_una_sola_vez(s):
    """Dos de antes, uno en medio de la pregunta del caso: tres en total, sin repetir ninguno ni
    perder el de en medio. Si se pone roja, la racha de antes se suma dos veces a lo de siempre."""
    _manda_archivo(s, 1)
    _manda_archivo(s, 2)
    handle_incoming(s, PHONE, SITUACION)
    _manda_archivo(s, 3)
    handle_incoming(s, PHONE, "revision")

    caso = _casos(s)[0]
    assert [_folio_de(s, n) for n in (1, 2, 3)] == [caso.id] * 3
    assert "archivo-cliente:3" in _acciones(s, caso.id)


def test_un_texto_del_cliente_entre_los_archivos_corta_la_racha(s):
    """Lo que está del otro lado de un texto del cliente es de otra conversación. Si se pone roja,
    se liga al caso una foto de un asunto que el cliente ya dejó atrás."""
    _manda_archivo(s, 1)
    handle_incoming(s, PHONE, "gracias, es todo por ahora")
    _manda_archivo(s, 2)

    _abre_el_caso(s)

    caso = _casos(s)[0]
    assert _folio_de(s, 2) == caso.id
    assert _folio_de(s, 1) is None, "ligó un archivo de antes de otra conversación"


def test_un_archivo_de_hace_mas_de_la_ventana_no_se_liga_ni_deja_pasar_a_los_de_antes(s):
    """La racha son los últimos minutos, no el historial. Si se pone roja, el caso de hoy se queda
    con la INE de hace tres días."""
    _manda_archivo(s, 1)
    _manda_archivo(s, 2)
    _manda_archivo(s, 3)
    viejo = _archivo(s, 2)
    viejo.created_at = utcnow() - VENTANA_ARCHIVOS_PREVIOS - timedelta(minutes=5)
    s.add(viejo)
    s.commit()

    _abre_el_caso(s)

    caso = _casos(s)[0]
    assert _folio_de(s, 3) == caso.id
    assert _folio_de(s, 2) is None and _folio_de(s, 1) is None


def test_la_ventana_se_mide_desde_el_texto_del_caso_no_desde_el_momento_de_abrirlo(s):
    """El cliente puede contestar «Revisión» horas después de contar su caso (la pregunta vive 6 h).
    Las fotos que mandó junto con el texto siguen siendo de esa racha. Si se pone roja, un cliente
    que tarda en elegir pierde las fotos que mandó con su caso."""
    _manda_archivo(s, 1)
    handle_incoming(s, PHONE, SITUACION)
    hace_dos_horas = utcnow() - timedelta(hours=2)
    mensajes = s.exec(select(Message).where(Message.phone == PHONE, Message.direction == Direction.IN)).all()
    for m in mensajes:  # el archivo y el texto, con su orden y su cercanía de siempre
        m.created_at = hace_dos_horas + timedelta(seconds=m.id)
        s.add(m)
    conv = s.get(Conversation, PHONE)
    conv.updated_at = utcnow() - timedelta(minutes=1)  # la pregunta sigue viva
    s.add(conv)
    s.commit()

    handle_incoming(s, PHONE, "revision")

    caso = _casos(s)[0]
    assert _folio_de(s, 1) == caso.id


def test_un_archivo_que_ya_tiene_folio_no_se_toca_y_corta_la_racha(s):
    """Un archivo ya ligado a otro folio es de otra solicitud: no se le quita ni se sigue
    retrocediendo más allá de él. Si se pone roja, un caso nuevo le roba el archivo a un folio."""
    cerrado = Ticket(phone=PHONE, category="c", service_id="sb-curp", service_name="CURP",
                     price_info="N/A", status=TicketStatus.CERRADO)
    s.add(cerrado)
    s.commit()
    _manda_archivo(s, 1)
    _manda_archivo(s, 2)
    ligado = _archivo(s, 2)
    ligado.ticket_id = cerrado.id
    s.add(ligado)
    s.commit()
    _manda_archivo(s, 3)

    _abre_el_caso(s)

    caso = _casos(s)[0]
    assert _folio_de(s, 3) == caso.id
    assert _folio_de(s, 2) == cerrado.id, "le quitó el archivo a otro folio"
    assert _folio_de(s, 1) is None, "retrocedió más allá de un archivo que ya tenía folio"


def test_los_archivos_de_otro_telefono_no_se_ligan(s):
    """Si se pone roja, el archivo de un cliente aparece en el caso de otro."""
    s.add(Message(phone=OTRO, direction=Direction.IN, body="[archivo adjunto]", media_ref="media-90",
                  media_mime="application/pdf", media_nombre="ajeno.pdf"))
    s.commit()
    _manda_archivo(s, 1)

    _abre_el_caso(s)

    caso = _casos(s)[0]
    assert _folio_de(s, 1) == caso.id
    assert _folio_de(s, 90, phone=OTRO) is None


def test_un_archivo_ocultado_desde_el_panel_no_se_liga_ni_corta_la_racha(s):
    """Un archivo que el equipo ocultó ya no existe para el chat. Si se pone roja, el caso recibe
    un archivo que el asesor retiró, o la racha se corta por un mensaje invisible."""
    _manda_archivo(s, 1)
    _manda_archivo(s, 2)
    oculto = _archivo(s, 2)
    oculto.habilitado = False
    s.add(oculto)
    s.commit()
    _manda_archivo(s, 3)

    _abre_el_caso(s)

    caso = _casos(s)[0]
    assert _folio_de(s, 3) == caso.id and _folio_de(s, 1) == caso.id
    assert _folio_de(s, 2) is None


def test_una_racha_larguisima_se_corta_en_el_tope_y_se_queda_con_los_mas_cercanos(s):
    """Sin tope de casos tampoco hay tope de archivos, pero la racha que se liga sin preguntar sí
    lo tiene: se quedan los más cercanos al texto del caso. Si se pone roja, un solo caso puede
    arrastrar cientos de archivos."""
    exceso = 5
    ids = []
    hace_diez_minutos = utcnow() - timedelta(minutes=10)  # dentro de la ventana, fuera del rate limit
    for n in range(1, MAX_ARCHIVOS_PREVIOS + exceso + 1):
        s.add(Message(phone=PHONE, direction=Direction.IN, body="[archivo adjunto]", media_ref=f"media-{n}",
                      media_mime="application/pdf", media_nombre=f"doc-{n}.pdf",
                      created_at=hace_diez_minutos + timedelta(seconds=n)))
        ids.append(n)
    s.commit()

    _abre_el_caso(s)

    caso = _casos(s)[0]
    ligados = [n for n in ids if _folio_de(s, n) == caso.id]
    assert len(ligados) == MAX_ARCHIVOS_PREVIOS
    assert ligados == ids[exceso:], "se quedó con los más viejos en vez de los más cercanos al caso"


def test_si_el_cliente_pide_un_tramite_los_archivos_de_antes_no_se_ligan_a_ningun_folio(s):
    """El cambio es solo del caso para análisis: elegir «un trámite» deja los archivos como hoy.
    Si se pone roja, el cambio se salió de su alcance y toca el alta de trámites."""
    _manda_archivo(s, 1)

    handle_incoming(s, PHONE, SITUACION)
    handle_incoming(s, PHONE, "tramite")

    assert not _casos(s)
    assert _folio_de(s, 1) is None


def test_la_perilla_de_la_revision_apagada_no_cambia_nada(s, monkeypatch):
    """Sin la revisión no hay caso para análisis: el archivo de antes queda como siempre."""
    monkeypatch.setattr(settings, "revision_enabled", False)
    _manda_archivo(s, 1)

    handle_incoming(s, PHONE, SITUACION)

    assert not _casos(s)
    assert _folio_de(s, 1) is None


def test_el_caso_sin_archivos_previos_se_abre_como_siempre(s):
    """Control: sin archivos de antes no hay nada que ligar y nada se rompe."""
    _abre_el_caso(s)

    caso = _casos(s)[0]
    assert not any(a.startswith("archivo-cliente") for a in _acciones(s, caso.id))
