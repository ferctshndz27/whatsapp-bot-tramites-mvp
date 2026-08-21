from datetime import timedelta

import pytest
from sqlmodel import select

from app import conversation
from app.catalog import catalog, requisitos_del_ticket
from app.conversation import CAPTURE_TTL_HOURS, handle_incoming
from app.models import PROVIDER_TWILIO, Conversation, Ticket, TicketStatus, utcnow

PHONE = "5215500000000"

SERVICIO_CON_REQUISITOS = "sb-acta-divorcio"


def _con_requisitos(texto: str = "clave curp\nnumero de servicio") -> None:
    """Fija requisitos CONOCIDOS en un servicio real: la prueba no debe
    depender de lo que el catálogo de producción traiga documentado hoy."""
    svc = catalog.service_by_id(SERVICIO_CON_REQUISITOS)
    assert catalog.update_service(
        svc.id, svc.name, svc.price_info, requirements=texto, admin="prueba"
    ) is None


def _sin_requisitos(*service_ids: str) -> None:
    """Borra los requisitos documentados de servicios reales. El catálogo de
    producción (importado 2026-07-29) trae requisitos en casi todo; las pruebas
    que ejercitan el flujo SIN captura los limpian para no depender del dato."""
    for sid in service_ids:
        svc = catalog.service_by_id(sid)
        assert catalog.update_service(
            svc.id, svc.name, svc.price_info, requirements="", admin="prueba"
        ) is None


def _pedir_y_confirmar(session) -> list[str]:
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")
    return handle_incoming(session, PHONE, "sí")


def _ticket(session) -> Ticket:
    return session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()


def test_greeting_gets_open_question_not_service_list(session):
    replies = handle_incoming(session, PHONE, "Hola")

    assert "ayudarte" in replies[0].lower()
    assert "1." not in replies[0]  # regla: no listar servicios en la primera respuesta


def test_direct_service_request_skips_menu_and_creates_ticket(session):
    # Regla: si el cliente llega directo con su trámite, saludar y generar el ticket
    replies = handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    assert "Acta de divorcio" in replies[0]
    assert "confirmas" in replies[0].lower()
    assert "1." not in replies[0]  # sin listado

    replies = handle_incoming(session, PHONE, "sí")
    assert "folio" in replies[0].lower()

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.service_name == "Acta de divorcio"
    assert ticket.status == TicketStatus.NUEVO


def test_alta_sin_requisitos_conserva_el_flujo_de_siempre(session):
    # REGLA: la captura es data-gated. Sin requisitos documentados el cliente
    # queda en menu_categoria y el folio no guarda nada.
    _sin_requisitos(SERVICIO_CON_REQUISITOS)
    replies = _pedir_y_confirmar(session)

    assert "Su solicitud está siendo procesada, en breve le resolvemos" in replies[0]
    assert "necesito" not in replies[0].lower()
    assert session.get(Conversation, PHONE).state == "menu_categoria"
    assert _ticket(session).requirements_json is None


def test_agente_pide_los_requisitos_y_los_guarda_en_el_folio(session):
    _con_requisitos()

    alta = _pedir_y_confirmar(session)
    # Un solo mensaje: alta + primera pregunta (el pareo IN/OUT sigue 1:1).
    assert len(alta) == 1
    assert "folio" in alta[0].lower() and "clave curp" in alta[0]
    assert session.get(Conversation, PHONE).state == "pidiendo_requisitos"

    segunda = handle_incoming(session, PHONE, "PEGF900101HDFRRN09")
    assert "numero de servicio" in segunda[0]

    cierre = handle_incoming(session, PHONE, "0012345678")

    assert "gracias" in cierre[0].lower()
    assert session.get(Conversation, PHONE).state == "menu_categoria"
    session.expire_all()
    assert requisitos_del_ticket(_ticket(session).requirements_json) == [
        {"req": "clave curp", "valor": "PEGF900101HDFRRN09", "pedido": True},
        {"req": "numero de servicio", "valor": "0012345678", "pedido": True},
    ]


def test_los_codigos_del_primer_mensaje_no_se_vuelven_a_pedir(session):
    # Caso real (producción, 2026-08-08): el cliente pidió el trámite Y pegó sus
    # códigos en el MISMO mensaje, y el agente le pidió la CURP de todos modos.
    # Los tres códigos van al mismo folio: quien decide si son tres actas es el
    # asesor, no el agente (el agente nunca fija el precio final).
    _con_requisitos("clave curp")
    codigos = "CUSE234543KPLRT02 COUE237643KPLRT02 COSE234543KPLRT00"

    handle_incoming(session, PHONE, f"acta de divorcio {codigos}")
    alta = handle_incoming(session, PHONE, "sí")

    assert "folio" in alta[0].lower()
    assert "necesito" not in alta[0].lower()  # ya no pregunta lo que ya tiene
    assert "ya tomé los datos" in alta[0].lower()
    assert session.get(Conversation, PHONE).state == "menu_categoria"
    session.expire_all()
    assert requisitos_del_ticket(_ticket(session).requirements_json) == [
        {"req": "clave curp", "valor": codigos.replace(" ", ", "), "pedido": False}
    ]


def test_la_confirmacion_cotiza_por_la_cantidad_de_codigos(session):
    # Tres claves = tres unidades del mismo trámite. Se cotiza ANTES de crear
    # el folio, con el precio del catálogo y dicho como estimado: el agente no
    # fija el precio final (regla de negocio) — lo confirma el asesor.
    _con_requisitos("clave curp")
    svc = catalog.service_by_id(SERVICIO_CON_REQUISITOS)
    catalog.update_service(svc.id, svc.name, "$25.00", requirements="clave curp", admin="prueba")

    confirmacion = handle_incoming(
        session, PHONE,
        "acta de divorcio CUSE234543KPLRT02 COUE237643KPLRT02 COSE234543KPLRT00",
    )

    assert "3 datos" in confirmacion[0]
    assert "$75.00 MXN" in confirmacion[0]
    assert "asesor" in confirmacion[0]  # se dice que es estimado
    # Y sigue siendo UNA solicitud, no tres folios.
    handle_incoming(session, PHONE, "sí")
    session.expire_all()
    assert len(session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()) == 1


def test_ollama_se_consulta_una_sola_vez_por_peticion(session, monkeypatch):
    # La clasificación cuesta ~5s en CPU y va DENTRO del webhook. Se paga en el
    # turno que ya paga la deducción, y ni el "sí" ni un titubeo la repiten:
    # el veredicto queda guardado en el contexto de la conversación.
    _con_requisitos("clave curp")
    llamadas = []
    monkeypatch.setattr(
        conversation.nlu, "clasificar_codigos",
        lambda cods: (llamadas.append(list(cods)), {c: "CURP" for c in cods})[1],
    )

    handle_incoming(session, PHONE, "acta de divorcio CUSE234543KPLRT02 COUE237643KPLRT02")
    handle_incoming(session, PHONE, "que?")
    handle_incoming(session, PHONE, "eh?")
    handle_incoming(session, PHONE, "sí")

    assert llamadas == [["CUSE234543KPLRT02", "COUE237643KPLRT02"]]


def test_un_precio_no_numerico_no_se_multiplica(session):
    # 7 de 58 servicios del catálogo de producción dicen "A cotizar": ahí no
    # hay número que multiplicar y el agente no debe inventarlo.
    svc = catalog.service_by_id(SERVICIO_CON_REQUISITOS)
    catalog.update_service(
        svc.id, svc.name, "A cotizar", requirements="clave curp", admin="prueba"
    )

    confirmacion = handle_incoming(
        session, PHONE, "acta de divorcio CUSE234543KPLRT02 COUE237643KPLRT02"
    )

    assert "2 datos" in confirmacion[0]
    assert "El asesor te confirma el total" in confirmacion[0]
    assert "MXN" not in confirmacion[0]


def test_los_codigos_sobreviven_a_una_confirmacion_titubeante(session):
    # El folio no existe hasta que el cliente confirma: los códigos viajan en el
    # contexto de la conversación y un "¿qué?" en medio no debe borrarlos.
    _con_requisitos("clave curp")

    handle_incoming(session, PHONE, "acta de divorcio CUSE234543KPLRT02")
    handle_incoming(session, PHONE, "que?")
    handle_incoming(session, PHONE, "sí")

    session.expire_all()
    assert requisitos_del_ticket(_ticket(session).requirements_json) == [
        {"req": "clave curp", "valor": "CUSE234543KPLRT02", "pedido": False}
    ]


def test_solo_se_prellena_el_requisito_que_pide_ese_dato(session):
    # El código llena la CURP; el dato que no es una clave se sigue preguntando.
    _con_requisitos("clave curp\nnombre completo")

    handle_incoming(session, PHONE, "acta de divorcio PEGF900101HDFRRN09")
    alta = handle_incoming(session, PHONE, "sí")

    assert "nombre completo" in alta[0]
    assert "clave curp" not in alta[0]
    session.expire_all()
    assert [r["valor"] for r in requisitos_del_ticket(_ticket(session).requirements_json)] == [
        "PEGF900101HDFRRN09",
        None,
    ]


def test_omitir_deja_el_requisito_como_faltante(session):
    _con_requisitos("clave curp")

    _pedir_y_confirmar(session)
    handle_incoming(session, PHONE, "omitir")

    session.expire_all()
    assert requisitos_del_ticket(_ticket(session).requirements_json) == [
        {"req": "clave curp", "valor": None, "pedido": True}
    ]


def test_respuestas_ambiguas_no_secuestran_la_captura(session):
    # REGLA: en captura de texto libre no existe "no te entendí". Palabras que
    # en otro estado son atajos (folio → estatus, "ok" → despedida) aquí son
    # el dato del cliente, y una respuesta rara NO debe escalar.
    # Requisitos SIN clave oficial a propósito: de un "número de servicio" no
    # hay formato que validar, así que se guarda tal cual venga.
    _con_requisitos("numero de servicio\nreferencia del recibo")

    _pedir_y_confirmar(session)
    handle_incoming(session, PHONE, "mi folio de la CFE es 12345")
    handle_incoming(session, PHONE, "ok")

    session.expire_all()
    assert [r["valor"] for r in requisitos_del_ticket(_ticket(session).requirements_json)] == [
        "mi folio de la CFE es 12345",
        "ok",
    ]
    assert _ticket(session).escalado is False
    assert session.get(Conversation, PHONE).state == "menu_categoria"


def test_respuesta_sin_clave_se_repregunta_una_sola_vez(session):
    # REGLA: si se pidió una clave (CURP/RFC/NSS) y la respuesta no trae nada
    # con esa forma —el caso real: el cliente contestó "."— se repregunta UNA
    # vez. A la segunda se guarda tal cual: el agente no discute con el cliente.
    _con_requisitos("clave curp")

    _pedir_y_confirmar(session)
    reintento = handle_incoming(session, PHONE, ".")

    assert "no reconocí" in reintento[0].lower()
    assert session.get(Conversation, PHONE).state == "pidiendo_requisitos"
    session.expire_all()
    assert requisitos_del_ticket(_ticket(session).requirements_json)[0]["valor"] is None

    cierre = handle_incoming(session, PHONE, ".")

    assert "gracias" in cierre[0].lower()
    session.expire_all()
    assert requisitos_del_ticket(_ticket(session).requirements_json)[0]["valor"] == "."
    assert _ticket(session).escalado is False


def test_omitir_sigue_valiendo_pese_al_guardia_de_claves(session):
    # La salida de emergencia no se cierra: *omitir* deja el requisito como
    # faltante sin pasar por la repregunta.
    _con_requisitos("clave curp")

    _pedir_y_confirmar(session)
    cierre = handle_incoming(session, PHONE, "omitir")

    assert "gracias" in cierre[0].lower()
    session.expire_all()
    assert requisitos_del_ticket(_ticket(session).requirements_json) == [
        {"req": "clave curp", "valor": None, "pedido": True}
    ]


def test_menu_aborta_la_captura_sin_perder_lo_capturado(session):
    _con_requisitos("clave curp\nnumero de servicio")

    _pedir_y_confirmar(session)
    handle_incoming(session, PHONE, "PEGF900101HDFRRN09")
    handle_incoming(session, PHONE, "menu")  # escape explícito

    session.expire_all()
    assert session.get(Conversation, PHONE).state == "menu_categoria"
    assert requisitos_del_ticket(_ticket(session).requirements_json) == [
        {"req": "clave curp", "valor": "PEGF900101HDFRRN09", "pedido": True},
        # Se preguntó y el cliente escapó con *menu*: "sin respuesta".
        {"req": "numero de servicio", "valor": None, "pedido": True},
    ]


def test_palabras_de_escalado_dentro_del_dato_no_escalan(session):
    # BUG confirmado: "persona" por substring escalaba, borraba la cola y el
    # dato que el cliente SÍ escribió se perdía para siempre.
    _con_requisitos("nombre de la persona titular\ncorreo de contacto")

    _pedir_y_confirmar(session)
    handle_incoming(session, PHONE, "la persona titular es Maria Lopez")
    handle_incoming(session, PHONE, "personal@hotmail.com")

    session.expire_all()
    ticket = _ticket(session)
    assert ticket.escalado is False
    assert [r["valor"] for r in requisitos_del_ticket(ticket.requirements_json)] == [
        "la persona titular es Maria Lopez",
        "personal@hotmail.com",
    ]


def test_pedir_asesor_explicito_si_escala_durante_la_captura(session):
    _con_requisitos("clave curp")

    _pedir_y_confirmar(session)
    replies = handle_incoming(session, PHONE, "mejor quiero hablar con un asesor")

    session.expire_all()
    assert "asesor" in replies[0].lower()
    assert _ticket(session).escalado is True


def test_saludo_en_confirmar_no_escala(session):
    # Regresión detectada por la revisión: dos "hola" seguidos en confirmar
    # escalaban a humano y creaban el ticket "Escalado a asesor".
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")

    replies = handle_incoming(session, PHONE, "hola")

    assert "hola" in replies[0].lower()
    assert session.get(Conversation, PHONE).state == "menu_categoria"
    assert _ticket(session) is None  # nada se escaló ni se creó


def test_ok_en_confirmar_repregunta_si_o_no(session):
    # "ok" es un intento de confirmar, no una despedida: el handler re-pregunta.
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")

    replies = handle_incoming(session, PHONE, "ok")

    assert "confirmar con" in replies[0].lower()
    assert session.get(Conversation, PHONE).state == "confirmar"


def test_captura_caducada_atiende_el_mensaje_como_nuevo(session):
    _con_requisitos("clave curp")
    _pedir_y_confirmar(session)

    conv = session.get(Conversation, PHONE)
    conv.updated_at = utcnow() - timedelta(hours=CAPTURE_TTL_HOURS + 1)
    session.add(conv)
    session.commit()

    replies = handle_incoming(session, PHONE, "hola buen dia")

    # Días después, un saludo es un saludo — no "el dato que le pedí".
    assert "hola" in replies[0].lower()
    session.expire_all()
    requisito = requisitos_del_ticket(_ticket(session).requirements_json)[0]
    assert requisito["valor"] is None  # el folio no se ensució con el saludo


def test_no_lo_tengo_con_punto_cuenta_como_omision(session):
    _con_requisitos("clave curp")
    _pedir_y_confirmar(session)

    handle_incoming(session, PHONE, "No lo tengo.")

    session.expire_all()
    requisito = requisitos_del_ticket(_ticket(session).requirements_json)[0]
    assert requisito["valor"] is None
    assert requisito["pedido"] is True


def test_omision_con_cola_larga_no_se_traga_el_dato(session):
    # "despues de..." arranca como omisión pero la cola larga trae el dato.
    _con_requisitos("numero de servicio")
    _pedir_y_confirmar(session)

    handle_incoming(session, PHONE, "despues de buscarlo lo encontre: 0012345678")

    session.expire_all()
    requisito = requisitos_del_ticket(_ticket(session).requirements_json)[0]
    assert requisito["valor"] == "despues de buscarlo lo encontre: 0012345678"


@pytest.mark.parametrize("como", ["eliminado", "cerrado"])
def test_folio_muerto_corta_la_captura_sin_ensuciarlo(session, como):
    _con_requisitos("clave curp\nnumero de servicio")
    _pedir_y_confirmar(session)

    ticket = _ticket(session)
    if como == "eliminado":
        ticket.habilitado = False  # lo que hace /tickets/{id}/delete
    else:
        ticket.status = TicketStatus.CERRADO
    session.add(ticket)
    session.commit()

    handle_incoming(session, PHONE, "PEGF900101HDFRRN09")

    session.expire_all()
    # La respuesta a un folio muerto no se escribe en él y la captura cierra.
    assert all(
        r["valor"] is None
        for r in requisitos_del_ticket(_ticket(session).requirements_json)
    )
    assert session.get(Conversation, PHONE).state == "menu_categoria"


def test_agente_apagado_calla_tambien_durante_la_captura(session):
    _con_requisitos("clave curp")
    _pedir_y_confirmar(session)

    ticket = _ticket(session)
    ticket.agent_active = False  # /tickets/{id}/agent-toggle
    session.add(ticket)
    session.commit()

    assert handle_incoming(session, PHONE, "PEGF900101HDFRRN09") == []


def test_requisito_larguisimo_no_revienta_el_mensaje(session):
    # Meta rechaza cuerpos >4096: la pregunta trunca el requisito.
    _con_requisitos("x" * 4000)

    alta = _pedir_y_confirmar(session)

    assert len(alta[0]) < 1000


def test_services_keyword_shows_catalog_listing(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "¿qué servicios tienen?")

    assert "1." in replies[0]  # única situación donde sí se lista

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_identity_question_gets_fixed_reply(session):
    replies = handle_incoming(session, PHONE, "¿quién eres?")

    assert "agente" in replies[0].lower()


def test_annoyed_client_is_escalated_to_human(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "estoy muy molesto con su servicio")

    assert "asesor" in replies[0].lower()
    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None and ticket.escalado is True

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"


def test_escalation_keyword_from_any_state(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "quiero hablar con un asesor")
    assert "asesor" in replies[0].lower()

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.escalado is True

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    # tras escalar (ticket sin asignar), el bot nunca deja sin respuesta
    replies = handle_incoming(session, PHONE, "sigo esperando")
    assert replies != []
    assert "asesor" in replies[0].lower()


def test_two_failed_attempts_auto_escalates(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "xyzqq zzz")  # intento 1
    assert "asesor" in replies[0].lower()  # ofrece el contacto humano

    replies = handle_incoming(session, PHONE, "wwwk qqq")  # intento 2 -> escala
    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket is not None
    assert ticket.escalado is True


def test_media_message_is_not_processed(session):
    replies = handle_incoming(session, PHONE, "", is_media=True)
    assert "documentos" in replies[0].lower()

    conv = session.get(Conversation, PHONE)
    assert conv.state == "inicio"  # no cambia de estado por un adjunto


def test_menu_keyword_shows_listing_from_any_state(session):
    handle_incoming(session, PHONE, "Hola")

    replies = handle_incoming(session, PHONE, "menu")
    assert "1." in replies[0]

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_numeric_flow_still_works_after_listing(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "servicios")  # muestra categorías

    replies = handle_incoming(session, PHONE, "1")  # Servicios básicos
    assert "Acta de nacimiento" in replies[0]

    replies = handle_incoming(session, PHONE, "0")  # volver
    assert "1." in replies[0]
    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_greeting_after_closed_ticket_is_cordial_short(session):
    _sin_requisitos("sb-acta-nacimiento")
    handle_incoming(session, PHONE, "necesito acta de nacimiento")
    handle_incoming(session, PHONE, "sí")  # folio creado, estado vuelve a menu_categoria

    replies = handle_incoming(session, PHONE, "hola")

    assert "no logré" not in replies[0].lower()
    assert "ayud" in replies[0].lower()
    assert "1." not in replies[0]  # el saludo no lista servicios


def test_escalated_user_resumes_bot_with_menu_if_ticket_unclaimed(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    replies = handle_incoming(session, PHONE, "menu")
    assert replies != []
    assert "1." in replies[0]

    conv = session.get(Conversation, PHONE)
    assert conv.state == "menu_categoria"


def test_escalated_user_keeps_getting_bot_replies_after_being_claimed(session):
    # Regla: tomar el ticket NO apaga el bot por sí solo — solo lo hace el
    # colaborador de forma explícita (agent_active=False, ver test siguiente).
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.assigned_to = "ana"
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "cualquier cosa")
    assert replies != []
    assert "asesor" in replies[0].lower()


def test_escalated_user_stays_silent_only_after_agent_explicitly_paused(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.assigned_to = "ana"
    ticket.agent_active = False
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "menu")
    assert replies == []


def test_status_query_lists_open_tickets_with_folio(session):
    _sin_requisitos(SERVICIO_CON_REQUISITOS)
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")

    replies = handle_incoming(session, PHONE, "cual es el estatus de mi tramite")

    assert "Acta de divorcio" in replies[0]
    assert "folio #" in replies[0]


def test_status_query_understands_casual_phrasings(session):
    """Frases reales de clientes (captura 2026-07-21): "como va?" y
    "mi solicitud como va?" deben responder estatus, no escalar por error."""
    _sin_requisitos(SERVICIO_CON_REQUISITOS)
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")

    for phrasing in ["como va?", "mi solicitud como va?", "¿Cómo va mi trámite?",
                     "que pasó con mi acta", "ya quedó?"]:
        replies = handle_incoming(session, PHONE, phrasing)
        assert "Acta de divorcio" in replies[0], phrasing
        assert "folio #" in replies[0], phrasing

    conv = session.get(Conversation, PHONE)
    assert conv.state != "escalado"  # preguntar estatus jamás escala


def test_ambiguous_keyword_shows_matching_services(session):
    """Captura 2026-07-21: "quiero un acta" debe listar las actas disponibles,
    no rendirse con "no logré identificar el trámite"."""
    handle_incoming(session, PHONE, "hola")

    replies = handle_incoming(session, PHONE, "quiero un acta")
    assert "parecidos" in replies[0]
    for name in ["Acta de nacimiento", "Acta de matrimonio",
                 "Acta de defunción", "Acta de divorcio"]:
        assert name in replies[0]

    replies = handle_incoming(session, PHONE, "2")
    assert "Acta de matrimonio" in replies[0]
    assert "confirmas" in replies[0].lower()

    handle_incoming(session, PHONE, "sí")
    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert ticket.service_name == "Acta de matrimonio"


def test_ambiguous_keyword_works_as_first_message(session):
    replies = handle_incoming(session, PHONE, "quiero un acta")

    assert "parecidos" in replies[0]


def test_suggestion_menu_accepts_multi_pick(session):
    handle_incoming(session, PHONE, "quiero un acta")

    replies = handle_incoming(session, PHONE, "1 y 4")

    assert "2 trámites" in replies[0]
    assert "Acta de nacimiento" in replies[0]
    assert "Acta de divorcio" in replies[0]


def test_escalated_client_asking_for_service_gets_attended(session):
    """Captura 2026-07-21 (2): estando escalado, "quiero un acta" recibía dos
    veces el 'espera' enlatado — debe ofrecer el mini-menú y registrar."""
    handle_incoming(session, PHONE, "quiero hablar con un asesor")
    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"

    replies = handle_incoming(session, PHONE, "hola quiero una acta")
    assert "parecidos" in replies[0]
    assert "Acta de nacimiento" in replies[0]

    handle_incoming(session, PHONE, "1")
    replies = handle_incoming(session, PHONE, "sí")
    assert "folio" in replies[0].lower()


def test_escalated_client_with_direct_service_goes_to_confirm(session):
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    replies = handle_incoming(session, PHONE, "necesito un acta de divorcio")

    assert "Acta de divorcio" in replies[0]
    assert "confirmas" in replies[0].lower()


def test_escalated_client_with_vague_message_still_gets_wait_reply(session):
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    replies = handle_incoming(session, PHONE, "gracias, aquí espero")

    assert "asesor te contactará" in replies[0].lower()


def test_asking_for_info_gets_warm_reply_not_error(session):
    """Captura 2026-07-22: "hola, quiero informes" recibía "No logré
    identificar el trámite" — debe responder con calidez y sin sumar
    intentos fallidos (pedir informes es interés, no un error)."""
    handle_incoming(session, PHONE, "hola")

    replies = handle_incoming(session, PHONE, "hola, quiero informes")

    assert "con gusto" in replies[0].lower()
    conv = session.get(Conversation, PHONE)
    assert conv.failed_attempts == 0
    assert conv.state != "escalado"


def test_info_request_with_keyword_still_shows_suggestions(session):
    handle_incoming(session, PHONE, "hola")

    replies = handle_incoming(session, PHONE, "quiero informes de un acta")

    assert "parecidos" in replies[0]  # la desambiguación gana al saludo genérico
    assert "Acta de nacimiento" in replies[0]


def test_gibberish_still_gets_not_recognized_hint(session):
    handle_incoming(session, PHONE, "hola")

    replies = handle_incoming(session, PHONE, "xyzk qwrt")

    assert "ayudarte" in replies[0].lower()
    assert "*servicios*" in replies[0]


def test_starting_a_new_procedure_is_not_mistaken_for_status(session):
    replies = handle_incoming(session, PHONE, "quiero iniciar mi tramite")

    assert "no tienes ninguna solicitud" not in replies[0].lower()


def test_status_query_with_no_tickets_redirects_to_services(session):
    replies = handle_incoming(session, PHONE, "cual es el estatus de mi solicitud")

    assert "no tienes ninguna solicitud" in replies[0].lower()


def test_status_query_works_while_escalated_and_agent_active(session):
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    replies = handle_incoming(session, PHONE, "estatus")

    assert "Acta de divorcio" in replies[0]


def test_status_query_blocked_when_agent_explicitly_paused(session):
    handle_incoming(session, PHONE, "Necesito un acta de divorcio, soy Juan Pérez")
    handle_incoming(session, PHONE, "si")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.assigned_to = "ana"
    ticket.agent_active = False
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "estatus")
    assert replies == []

    conv = session.get(Conversation, PHONE)
    assert conv.state == "escalado"


def test_el_ticket_hereda_el_canal_de_la_conversacion(session):
    # REGLA: el ticket registra POR DÓNDE llegó la solicitud. Los envíos ya
    # resuelven canal por Conversation.provider, pero el ticket es lo que leen
    # reportes y cobranza: dejarlo en el default 'meta' miente para todo
    # cliente atendido por Twilio.
    handle_incoming(session, PHONE, "Necesito un acta de divorcio", provider=PROVIDER_TWILIO)
    handle_incoming(session, PHONE, "sí", provider=PROVIDER_TWILIO)

    ticket = _ticket(session)
    assert ticket is not None
    assert ticket.provider == PROVIDER_TWILIO


def test_el_ticket_de_escalado_tambien_hereda_el_canal(session):
    # REGLA: mismo criterio para el SEGUNDO sitio que crea tickets
    # (_escalate). Si solo se corrigiera el de confirmación, el cliente de
    # Twilio que pide un humano seguiría dejando un ticket 'meta'.
    handle_incoming(session, PHONE, "quiero hablar con un humano", provider=PROVIDER_TWILIO)

    ticket = _ticket(session)
    assert ticket is not None
    assert ticket.provider == PROVIDER_TWILIO


def test_escalado_sin_ticket_abierto_recrea_la_solicitud(session):
    # El caso de producción: la conversación quedó en "escalado", el ticket de
    # la escalada se cerró, y los mensajes del cliente recibían el acuse sin
    # registrar NADA — el bot prometía asesor y ningún asesor se enteraba.
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")
    ticket = session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    ticket.status = TicketStatus.CERRADO
    session.add(ticket)
    session.commit()

    replies = handle_incoming(session, PHONE, "sigo esperando, ¿me ayudan?")

    assert "asesor" in replies[0].lower()
    abiertos = session.exec(
        select(Ticket).where(Ticket.phone == PHONE, Ticket.status != TicketStatus.CERRADO)
    ).all()
    assert len(abiertos) == 1 and abiertos[0].escalado is True


def test_escalado_con_ticket_abierto_no_duplica_solicitudes(session):
    handle_incoming(session, PHONE, "Hola")
    handle_incoming(session, PHONE, "quiero hablar con un asesor")

    handle_incoming(session, PHONE, "¿ya me van a atender?")

    tickets = session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()
    assert len(tickets) == 1
