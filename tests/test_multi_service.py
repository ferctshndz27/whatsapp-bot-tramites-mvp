import json

import pytest
from sqlmodel import select

from app.conversation import handle_incoming
from app.models import Conversation, Ticket

PHONE = "5215511112222"


def _tickets(session):
    return session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()


def _go_to_service_menu(session):
    handle_incoming(session, PHONE, "menu")
    handle_incoming(session, PHONE, "1")  # Servicios básicos


def test_menu_multi_pick_creates_one_ticket_per_service(session):
    _go_to_service_menu(session)

    replies = handle_incoming(session, PHONE, "1 y 2")
    assert "2 trámites" in replies[0]
    assert "Acta de nacimiento" in replies[0]
    assert "Acta de matrimonio" in replies[0]

    replies = handle_incoming(session, PHONE, "sí")
    assert "2 solicitudes" in replies[0]
    assert replies[0].count("folio #") == 2
    assert {t.service_name for t in _tickets(session)} == {
        "Acta de nacimiento",
        "Acta de matrimonio",
    }


@pytest.mark.parametrize("pick", ["1,2", "1 2", "1y2", "1 , 2", "1 y 2 y 3"])
def test_menu_multi_pick_accepts_common_separators(session, pick):
    _go_to_service_menu(session)

    replies = handle_incoming(session, PHONE, pick)

    assert "Acta de nacimiento" in replies[0]
    assert "Acta de matrimonio" in replies[0]
    assert "sí o no" in replies[0]


def test_menu_multi_pick_reports_out_of_range_numbers(session):
    _go_to_service_menu(session)

    replies = handle_incoming(session, PHONE, "1 y 99")
    assert "No encontré la opción 99" in replies[0]
    assert "Acta de nacimiento" in replies[0]

    handle_incoming(session, PHONE, "sí")
    assert [t.service_name for t in _tickets(session)] == ["Acta de nacimiento"]


def test_free_text_with_two_services_confirms_both(session):
    replies = handle_incoming(
        session, PHONE, "Hola, necesito acta de nacimiento y acta de divorcio"
    )
    assert "Acta de nacimiento" in replies[0]
    assert "Acta de divorcio" in replies[0]

    replies = handle_incoming(session, PHONE, "si")
    assert replies[0].count("folio #") == 2
    assert len(_tickets(session)) == 2


def test_multi_confirm_no_cancels_everything(session):
    _go_to_service_menu(session)
    handle_incoming(session, PHONE, "1 y 2")

    replies = handle_incoming(session, PHONE, "no")

    assert "Sin problema" in replies[0]
    assert _tickets(session) == []


def test_multi_categories_are_handled_one_by_one(session):
    handle_incoming(session, PHONE, "menu")

    replies = handle_incoming(session, PHONE, "1 y 2")

    assert "una por una" in replies[0]
    assert "1." in replies[0]  # muestra el menú de la primera categoría


def test_single_number_selection_still_works(session):
    _go_to_service_menu(session)

    replies = handle_incoming(session, PHONE, "1")
    assert "Acta de nacimiento" in replies[0]
    assert "trámites" not in replies[0]  # confirmación individual, no en bloque

    handle_incoming(session, PHONE, "sí")
    assert [t.service_name for t in _tickets(session)] == ["Acta de nacimiento"]


def test_legacy_pending_service_id_context_still_confirms(session):
    """Conversaciones que quedaron a media confirmación con el formato viejo
    del contexto (pending_service_id) deben seguir funcionando tras el deploy."""
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")
    conv = session.get(Conversation, PHONE)
    ctx = json.loads(conv.context_json)
    conv.context_json = json.dumps({"pending_service_id": ctx["pending_service_ids"][0]})
    session.add(conv)
    session.commit()

    replies = handle_incoming(session, PHONE, "sí")

    assert "folio" in replies[0].lower()
    assert len(_tickets(session)) == 1
