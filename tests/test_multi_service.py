import json

import pytest
from sqlmodel import select

from app.catalog import catalog
from app.conversation import handle_incoming
from app.models import Conversation, Ticket

PHONE = "5215511112222"


def _tickets(session):
    return session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()


def _go_to_service_menu(session):
    handle_incoming(session, PHONE, "menu")
    handle_incoming(session, PHONE, "1")  # Servicios básicos



def _sin_requisitos(*service_ids: str) -> None:
    """Borra los requisitos documentados de servicios reales. El catálogo de
    producción (importado 2026-07-29) trae requisitos en casi todo; las pruebas
    que ejercitan el flujo SIN captura los limpian para no depender del dato."""
    for sid in service_ids:
        svc = catalog.service_by_id(sid)
        assert catalog.update_service(
            svc.id, svc.name, svc.price_info, requirements="", admin="prueba"
        ) is None


def test_menu_multi_pick_creates_one_ticket_per_service(session):
    _sin_requisitos("sb-acta-nacimiento", "sb-acta-matrimonio")
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
    _sin_requisitos("sb-acta-nacimiento", "sb-acta-divorcio")
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


def test_max_cola_deja_los_requisitos_sobrantes_como_no_preguntados(session):
    """Dos folios con 5 requisitos cada uno rebasan MAX_COLA=8: los dos últimos
    del segundo folio jamás se preguntan y deben quedar como pedido=False
    ("sin preguntar"), no como si el cliente los hubiera negado."""
    from app.catalog import catalog, requisitos_del_ticket

    for sid in ("sb-acta-nacimiento", "sb-acta-matrimonio"):
        svc = catalog.service_by_id(sid)
        catalog.update_service(
            svc.id, svc.name, svc.price_info,
            requirements="\n".join(f"dato {i}" for i in range(1, 6)), admin="prueba",
        )
    _go_to_service_menu(session)
    handle_incoming(session, PHONE, "1 y 2")
    handle_incoming(session, PHONE, "sí")

    for i in range(8):  # solo caben 8 preguntas en la cola
        handle_incoming(session, PHONE, f"respuesta {i + 1}")

    session.expire_all()
    segundo = _tickets(session)[1]
    estados = [
        (r["valor"] is not None, r["pedido"])
        for r in requisitos_del_ticket(segundo.requirements_json)
    ]
    assert estados == [
        (True, True), (True, True), (True, True),  # preguntados y contestados
        (False, False), (False, False),            # nunca se preguntaron
    ]


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


# ---------------------------------------------------------------------------
# Pedido de dos trámites cuando uno de los dos queda ambiguo
# ---------------------------------------------------------------------------
#
# Caso real (folio #33 en producción): el cliente escribió "quiero un acta y un
# rfc". El rfc se resuelve solo (sinónimo del catálogo), pero "un acta" no dice
# cuál de las cuatro — y el agente levantaba SOLO el rfc, tirando el acta sin
# avisar. Ahora pregunta y confirma los dos juntos.

AMBIGUO_Y_CLARO = "quiero un acta y un rfc"


def _hasta_pedir(session):
    handle_incoming(session, PHONE, "hola")
    handle_incoming(session, PHONE, "Fernando")


def test_tramite_ambiguo_junto_a_uno_claro_pregunta_y_levanta_ambos(session):
    _sin_requisitos("sb-acta-nacimiento", "sat-csf-original")
    _hasta_pedir(session)

    pregunta = handle_incoming(session, PHONE, AMBIGUO_Y_CLARO)[0]

    # No confirma todavía: avisa lo que ya entendió y pregunta por lo que falta.
    assert "Constancia de situación fiscal original" in pregunta
    assert "Acta de nacimiento" in pregunta and "Acta de divorcio" in pregunta
    assert "Responde con el número" in pregunta
    conv = session.get(Conversation, PHONE)
    assert conv.state == "eligiendo_sugerencia"

    confirmacion = handle_incoming(session, PHONE, "1")[0]
    assert "2 trámites" in confirmacion

    handle_incoming(session, PHONE, "sí")
    nombres = {t.service_name for t in _tickets(session)}
    assert nombres == {"Constancia de situación fiscal original", "Acta de nacimiento"}


def test_pedido_completo_no_dispara_la_pregunta(session):
    # Guarda anti-vacuidad: suggestions() matchea por palabra compartida, así
    # que "acta de nacimiento" arrastra a las otras tres actas. Si el recorte
    # de menciones fallara, ESTE pedido —que no tiene nada pendiente— se
    # volvería una pregunta y el agente daría lata en el caso más común.
    _sin_requisitos("sb-acta-nacimiento")
    _hasta_pedir(session)

    replies = handle_incoming(session, PHONE, "quiero mi acta de nacimiento")

    assert "¿Confirmas" in replies[0]
    assert session.get(Conversation, PHONE).state == "confirmar"


def test_dos_tramites_ambos_claros_siguen_por_la_via_multi(session):
    # Ambas partes son literales: deduce_all las agarra y nunca se llega a
    # preguntar. Fija que el camino viejo no cambió.
    _sin_requisitos("sb-acta-nacimiento", "sat-csf-original")
    _hasta_pedir(session)

    replies = handle_incoming(session, PHONE, "quiero mi acta de nacimiento y un rfc")

    assert "2 trámites" in replies[0]
    assert session.get(Conversation, PHONE).state == "confirmar"


def test_si_no_elige_no_se_pierde_lo_que_ya_se_habia_entendido(session):
    # Piso duro: antes de este flujo el cliente ya tendría el folio del rfc.
    # Responder cualquier cosa NO puede dejarlo con menos que eso.
    _sin_requisitos("sat-csf-original")
    _hasta_pedir(session)
    handle_incoming(session, PHONE, AMBIGUO_Y_CLARO)

    confirmacion = handle_incoming(session, PHONE, "mmm no sé")[0]

    assert "Constancia de situación fiscal original" in confirmacion
    handle_incoming(session, PHONE, "sí")
    assert [t.service_name for t in _tickets(session)] == [
        "Constancia de situación fiscal original"
    ]


def test_los_codigos_del_mensaje_original_sobreviven_a_la_pregunta(session):
    # El mensaje que trae la CURP es el original; el turno de la elección solo
    # trae "1". Sin arrastrar los códigos en el contexto, el dato se perdería
    # y el agente se lo volvería a pedir al cliente dos mensajes después.
    _sin_requisitos("sat-csf-original")
    _hasta_pedir(session)
    handle_incoming(session, PHONE, AMBIGUO_Y_CLARO + " mi curp es GOMC800101HDFXXX01")

    contexto = json.loads(session.get(Conversation, PHONE).context_json)
    guardados = json.dumps(contexto.get("codigos", {}))
    assert "GOMC800101HDFXXX01" in guardados

    handle_incoming(session, PHONE, "1")
    contexto = json.loads(session.get(Conversation, PHONE).context_json)
    assert "GOMC800101HDFXXX01" in json.dumps(contexto.get("codigos", {}))


def test_muletillas_no_disparan_la_pregunta(session):
    # Los nombres del catálogo contienen palabras funcionales ("Amparo PARA
    # liberar pensión"), así que sin filtrarlas cualquier frase con "para"
    # —de lo más común— hacía que el agente preguntara por trámites de
    # pensión que el cliente jamás pidió. El test negativo anterior usaba una
    # frase sin muletillas y no lo cazaba.
    _sin_requisitos("sb-acta-nacimiento", "sb-curp")
    _hasta_pedir(session)

    for frase in (
        "necesito mi acta de nacimiento para tramite escolar",
        "quiero mi curp para el banco por favor",
    ):
        replies = handle_incoming(session, PHONE, frase)
        assert "¿Confirmas" in replies[0], f"{frase!r} disparó la pregunta de más"
        assert session.get(Conversation, PHONE).state == "confirmar"
        handle_incoming(session, PHONE, "no")


def test_la_eleccion_del_cliente_nunca_se_recorta_por_el_tope(session):
    # Si los ya resueltos llenan MULTI_MAX_SERVICES, lo que se recorta debe
    # ser un resuelto viejo, jamás la respuesta que el cliente acaba de dar.
    from app.conversation import _handle_sugerencia, _set_state
    from app.models import Client

    _hasta_pedir(session)
    conv = session.get(Conversation, PHONE)
    llenos = ["sb-acta-nacimiento", "sb-acta-matrimonio", "sb-acta-defuncion",
              "sb-acta-divorcio", "sb-curp"]
    _set_state(session, conv, "eligiendo_sugerencia", {
        "suggestion_ids": ["sb-recibo-cfe", "sat-csf-original"],
        "resueltos_ids": llenos,
    })
    client = session.get(Client, PHONE)

    _handle_sugerencia(session, client, conv, "1 y 2", "1 y 2")

    elegidos = json.loads(session.get(Conversation, PHONE).context_json)["pending_service_ids"]
    assert "sb-recibo-cfe" in elegidos and "sat-csf-original" in elegidos
