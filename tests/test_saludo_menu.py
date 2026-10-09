"""El saludo responde con el menú principal, y las frases que el bot no reconocía
(2026-09-24) ya cuentan como saludo o como pedido de humano (FTD-SALUDO-MENU §A-§C).

Qué se rompe en el negocio si estas pruebas fallan: un cliente que dice «hola» vuelve
a ver «¿En qué te ayudo?» sin saber qué puede pedir (hay que adivinar el número); un
«buenos tardes» vuelve a recibir «¿confirmas con sí o no?»; y un «trasferir agente»
en mitad de un trámite sigue perdiendo al cliente en el «No».
"""

import pytest
from sqlmodel import select

from app.conversation import (
    ESCALATION_KEYWORDS,
    GREETING_PHRASES,
    handle_incoming,
)
from app.models import Conversation, Ticket
from app.nlu import _norm

PHONE = "5215511110001"


def _ticket(session) -> Ticket | None:
    return session.exec(select(Ticket).where(Ticket.phone == PHONE)).first()


def test_un_saludo_responde_con_saludo_y_menu_de_categorias(session):
    """«Hola» debe llegar con el menú principal en el mismo mensaje: el cliente ve
    qué puede pedir sin tener que escribir «menu». Si se rompe, el saludo vuelve a
    ser una pregunta abierta y el cliente se queda sin opciones."""
    replies = handle_incoming(session, PHONE, "Hola")

    assert len(replies) == 1, "el saludo y el menú deben ir en un solo mensaje"
    texto = replies[0]
    assert "ayudarte" in texto.lower(), "falta el saludo"
    assert "esponde con el número" in texto, "falta el marcador del menú"
    assert session.get(Conversation, PHONE).state == "menu_categoria"


def test_el_saludo_con_nombre_tambien_trae_el_menu(session):
    """Un cliente conocido recibe el saludo personal, y el menú debajo."""
    from app.models import Client

    session.add(Client(phone=PHONE, name="Juan Pérez"))
    session.commit()

    replies = handle_incoming(session, PHONE, "buenas")

    assert len(replies) == 1
    assert "Juan Pérez" in replies[0]
    assert "esponde con el número" in replies[0]


def test_un_saludo_a_medio_requisito_sigue_siendo_la_respuesta_al_dato(session):
    """Dentro de «pidiendo_requisitos» un «hola» es la respuesta al dato, no un saludo:
    no debe cambiar de estado ni listar el menú (no regresión de §A)."""
    from tests.test_conversation import _con_requisitos

    _con_requisitos("clave curp")
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")
    handle_incoming(session, PHONE, "sí")
    assert session.get(Conversation, PHONE).state == "pidiendo_requisitos"

    replies = handle_incoming(session, PHONE, "hola")

    assert session.get(Conversation, PHONE).state == "pidiendo_requisitos"
    assert "esponde con el número" not in replies[0]


def test_el_saludo_no_deja_rastro_en_nluevent(session):
    """El saludo es trato social, no un trámite: no debe contar como «no entendido»
    en la bitácora (no regresión de §A; el filtro de `_log_nlu` no cambia)."""
    from app.models import NluEvent

    handle_incoming(session, PHONE, "Hola")

    assert session.exec(select(NluEvent).where(NluEvent.phone == PHONE)).all() == []


@pytest.mark.parametrize(
    "frase",
    ["buenos tardes", "buenos noches", "buenas dias", "buenas dia"],
)
def test_buenos_tardes_es_un_saludo(session, frase):
    """Las variantes con concordancia equivocada que el cliente teclea son saludos
    (D14, caso real 18:05:08: «Buenos tardes» recibió «¿confirmas con sí o no?»)."""
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")

    replies = handle_incoming(session, PHONE, frase)

    assert "esponde con el número" in replies[0], f"«{frase}» no se trató como saludo"
    assert session.get(Conversation, PHONE).state == "menu_categoria"
    assert _ticket(session) is None, "un saludo no abre folio"


def test_trasferir_agente_en_confirmar_escala_en_vez_de_pedir_si_o_no(session):
    """Caso real 17:50:58: «Trasferir agente» (con la errata) en `confirmar` recibió
    el re-prompt de sí o no, y el cliente se fue al «No». Debe pasar a un humano."""
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")
    assert session.get(Conversation, PHONE).state == "confirmar"

    replies = handle_incoming(session, PHONE, "Trasferir agente")

    session.expire_all()
    assert "asesor" in replies[0].lower(), "la respuesta debe ser el aviso de escalado"
    assert "sí" not in replies[0].lower(), "no debe repetir la pregunta de sí o no"
    assert session.get(Conversation, PHONE).state == "escalado"
    ticket = _ticket(session)
    assert ticket is not None and ticket.escalado is True, "el folio debe quedar escalado"


@pytest.mark.parametrize(
    "frase",
    [
        "transferir agente",
        "trasferir agente",
        "transfiereme",
        "pasame con",
        "pasarme con",
        "comunicame con",
        "atencion humana",
        "operador",
    ],
)
def test_cada_frase_nueva_de_escalado_pide_humano(session, frase):
    """Cada frase de la lista nueva es una petición de humano (no regresión de la
    lista vieja). Se prueba por frase para que una quitada de la lista falle sola."""
    assert _norm(frase) in ESCALATION_KEYWORDS, f"«{frase}» no está en la lista"


@pytest.mark.parametrize(
    "texto",
    [
        "¿el trámite se hace con un agente aduanal?",
        "tengo un agente de seguros que me pidió el acta",
        "mi agente de viajes me dio el número",
    ],
)
def test_agente_suelto_no_escala(session, texto):
    """«agente» solo no es una petición de humano: un cliente habla de su agente
    aduanal o de seguros (opción A de la pregunta 5, 2026-10-09). Si esto falla, el
    bot escala por cualquier mención de la palabra."""
    handle_incoming(session, PHONE, "Necesito un acta de divorcio")
    handle_incoming(session, PHONE, texto)

    session.expire_all()
    ticket = _ticket(session)
    assert ticket is None or ticket.escalado is False, f"«{texto}» escaló a un humano"


def test_las_palabras_de_escalado_no_son_nombres_del_catalogo(catalogo_vivo):
    """Ninguna frase nueva de escalado o saludo debe ser nombre o sinónimo de un
    servicio vivo: si lo fuera, el cliente que pregunta por ese trámite escalaría."""
    from app.catalog import catalog

    terminos = []
    for categoria in catalog.categories:
        for servicio in categoria.services:
            terminos.append(_norm(servicio.name))
            terminos += [_norm(s) for s in servicio.synonyms]
    nuevas = [
        "transferir agente", "trasferir agente", "transfiereme", "pasame con",
        "pasarme con", "comunicame con", "atencion humana", "operador",
        "buenos tardes", "buenos noches", "buenas dias", "buenas dia",
    ]
    choques = [(f, t) for f in nuevas for t in terminos if f in t]
    assert not choques, f"frases que colisionan con el catálogo vivo: {choques}"
