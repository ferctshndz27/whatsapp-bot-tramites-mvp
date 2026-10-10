"""La rama «escalado» de la conversación vive en el dict de la FSM (PR1 de «casos para análisis»).

Refactor puro: la rama `elif conv.state == "escalado"` de `handle_incoming` pasó a
`_handle_escalado`, registrada en `_HANDLERS`. Las pruebas de caracterización de este archivo se
escribieron y se vieron en verde sobre `main` ANTES de mover nada, y fijan lo que recibe hoy el
cliente que espera a un asesor.

Lo que protegen no es estético: `escalado` NO pasa por el preludio de `_dispatch` (menú por
palabra, identidad, catálogo, saludo, despedida) ni por la molestia o el pedido de humano de
`handle_incoming`. Si el refactor lo metiera ahí, el cliente escalado que escribe "hola" recibiría
el menú de categorías en vez de la espera, y un "pésimo servicio" le abriría otra escalada.
"""

import pytest
from sqlmodel import select

from app import conversation
from app.catalog import catalog
from app.conversation import ESCALATED_WAIT_REPLY, handle_incoming
from app.models import Conversation, Ticket

PHONE = "5215511110000"


def _espera() -> str:
    """La espera tal como sale hoy. Se arma dentro de la prueba, no al importar: el aviso de
    horario depende de `horario.ahora`, que el fixture autouse fija solo durante la prueba."""
    return conversation._con_horario(ESCALATED_WAIT_REPLY)


def _escalar(session) -> None:
    """Escalada por el camino real: el cliente pide un asesor y queda su folio abierto."""
    handle_incoming(session, PHONE, "quiero hablar con un asesor")
    assert session.get(Conversation, PHONE).state == "escalado", "la preparación no escaló"


def _folios(session) -> set[int]:
    return {t.id for t in session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()}


@pytest.mark.parametrize(
    "texto",
    [
        "hola",  # saludo: el preludio de _dispatch respondería saludo + menú
        "gracias",  # despedida: el preludio respondería FAREWELL_REPLY
        "¿quién eres?",  # identidad: el preludio respondería IDENTITY_REPLY
        "lista de servicios",  # catálogo: el preludio respondería el menú de categorías
        "pésimo servicio",  # molestia: handle_incoming abriría otra escalada con disculpa
        "quiero hablar con un asesor",  # pedido de humano: otra escalada
    ],
)
def test_en_escalado_lo_que_no_pide_un_tramite_recibe_la_espera(session, texto):
    """El cliente ya está con un humano: lo social, la identidad, el catálogo, la molestia y
    volver a pedir asesor reciben la espera, sin salir de `escalado` ni abrir otro folio. Si esto
    se rompe, el que espera a un asesor recibe el menú del bot como si nadie lo atendiera, o el
    panel ve una escalada nueva por cada mensaje de seguimiento."""
    _escalar(session)
    folios_antes = _folios(session)

    replies = handle_incoming(session, PHONE, texto)

    assert replies == [_espera()], f"{texto!r} en escalado ya no recibe la espera: {replies!r}"
    session.expire_all()
    assert session.get(Conversation, PHONE).state == "escalado", f"{texto!r} sacó de escalado"
    assert _folios(session) == folios_antes, f"{texto!r} abrió otro folio"


def test_en_escalado_menu_retoma_el_bot_con_su_propio_aviso(session):
    """«menu» saca al cliente de la espera con «Claro, continuemos aquí.» antes del menú. El
    preludio de `_dispatch` contesta el menú a secas: si el escalado pasara por él, el cliente no
    sabría que dejó la fila del asesor."""
    _escalar(session)

    replies = handle_incoming(session, PHONE, "menu")

    assert replies == ["Claro, continuemos aquí. " + catalog.format_category_menu()]
    session.expire_all()
    assert session.get(Conversation, PHONE).state == "menu_categoria"


def test_en_escalado_preguntar_el_estatus_no_saca_de_escalado(session):
    """«¿Cómo va mi trámite?» contesta el estatus y deja al cliente esperando a su asesor. El
    folio de la escalada SÍ aparece, como «Revisión con asesor» (operador, 2026-10-09: excluirlo le
    daba al cliente el estatus de folios viejos; ver tests/test_estatus_ticket_escalado.py)."""
    _escalar(session)

    replies = handle_incoming(session, PHONE, "¿cómo va mi trámite?")

    assert len(replies) == 1 and "• Revisión con asesor (folio #" in replies[0], replies
    session.expire_all()
    assert session.get(Conversation, PHONE).state == "escalado"


def test_un_estado_desconocido_sigue_cayendo_a_inicio(session):
    """Un estado que la FSM no conoce (una fila vieja, un estado retirado) se atiende como
    `inicio`: el cliente recibe la pregunta de qué trámite necesita, no silencio ni un 500 en el
    webhook. Mover el dict no puede cambiar ese default."""
    handle_incoming(session, PHONE, "hola")
    conv = session.get(Conversation, PHONE)
    conv.state = "estado_que_ya_no_existe"
    session.add(conv)
    session.commit()

    replies = handle_incoming(session, PHONE, "tengo una duda")

    assert replies == ["¡Hola! 😊 " + conversation.ASK_GOAL_REPLY]
    session.expire_all()
    assert session.get(Conversation, PHONE).state == "menu_categoria"


def test_escalado_se_resuelve_por_el_dict_de_handlers(session, monkeypatch):
    """`escalado` es una entrada de la FSM, no una rama aparte de `handle_incoming`. Lo que se
    rompe si no: PR4 y PR5 meten la Regla del folio y la de la revisión DENTRO de
    `_handle_escalado`; una copia de la rama en `handle_incoming` las saltaría justo para el
    cliente escalado, el que más escribe sin pedir trámite. La entrada llega con el texto crudo y
    el normalizado de siempre (`strip().lower()`)."""
    assert conversation._HANDLERS.get("escalado") is conversation._handle_escalado, (
        "«escalado» no está registrado en el dict de la FSM"
    )
    _escalar(session)
    llamadas = []

    def espia(session_, client, conv, text, normalized):
        llamadas.append((client.phone, conv.state, text, normalized))
        return ["respuesta del espía"]

    monkeypatch.setitem(conversation._HANDLERS, "escalado", espia)

    replies = handle_incoming(session, PHONE, "  Hola, ¿SIGO esperando?  ")

    assert replies == ["respuesta del espía"], "handle_incoming no resolvió escalado por el dict"
    assert llamadas == [
        (PHONE, "escalado", "  Hola, ¿SIGO esperando?  ", "hola, ¿sigo esperando?")
    ]
