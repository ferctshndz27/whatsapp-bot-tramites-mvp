"""En la confirmación de varios trámites, el cliente elige con números cuáles sí quiere.

Pedido del operador tras su prueba por WhatsApp (2026-10-10, «B1»): cuando el bot detecta varios
trámites, la confirmación solo admitía «sí» (todos) o «no» (ninguno). Un cliente que quería solo
uno tenía que decir «no» y empezar de nuevo. Ahora la lista va numerada y el cliente responde con
el número o los números de los que sí quiere («1», «1 y 3»); el bot deja solo esos y pide la
confirmación final.

WhatsApp no tiene casillas de selección múltiple (sus listas nativas eligen UNA opción), así que
la selección múltiple va por texto con números, la misma que ya se acepta en los menús.
"""

from app.conversation import handle_incoming
from app.models import Conversation, Ticket
from sqlmodel import select

PHONE = "5215533330000"
PEDIDO = "necesito acta de nacimiento y mi curp"


def _estado(session) -> str:
    session.expire_all()
    return session.get(Conversation, PHONE).state


def _numero_de(texto: str, nombre: str) -> str:
    return next(linea.split(".")[0].strip() for linea in texto.splitlines() if nombre in linea and linea[:1].isdigit())


def _pedir_dos(session) -> str:
    texto = "\n".join(handle_incoming(session, PHONE, PEDIDO))
    assert "Confirmas estos 2 trámites" in texto, f"la preparación cambió: {texto!r}"
    return texto


def test_la_confirmacion_de_varios_numera_las_opciones_y_explica_como_elegir(session):
    texto = _pedir_dos(session)

    assert _numero_de(texto, "Acta de nacimiento") in {"1", "2"}
    assert _numero_de(texto, "CURP") in {"1", "2"}
    assert "número" in texto, f"no le dice al cliente que puede elegir con números: {texto!r}"


def test_responder_un_numero_deja_solo_ese_tramite(session):
    texto = _pedir_dos(session)

    replies = handle_incoming(session, PHONE, _numero_de(texto, "Acta de nacimiento"))

    texto = "\n".join(replies)
    assert _estado(session) == "confirmar", texto
    assert "Acta de nacimiento" in texto
    assert "CURP" not in texto, f"no quitó lo que el cliente no eligió: {texto!r}"

    handle_incoming(session, PHONE, "sí")
    folios = session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()
    assert [t.service_name for t in folios] == ["Acta de nacimiento"]


def test_responder_todos_los_numeros_equivale_a_si(session):
    _pedir_dos(session)

    texto = "\n".join(handle_incoming(session, PHONE, "1 y 2"))

    assert _estado(session) == "confirmar"
    assert "Confirmas estos 2 trámites" in texto
    assert "Puedes confirmar" not in texto, f"tomó la selección como respuesta inválida: {texto!r}"


def test_un_numero_fuera_de_la_lista_no_elige_nada(session):
    _pedir_dos(session)

    texto = "\n".join(handle_incoming(session, PHONE, "7"))

    assert _estado(session) == "confirmar"
    assert "Confirmas estos 2 trámites" in texto, f"perdió la lista con un número inválido: {texto!r}"
    folios = session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()
    assert folios == []


def test_si_y_no_siguen_igual(session):
    _pedir_dos(session)
    handle_incoming(session, PHONE, "sí")
    folios = session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()
    assert len(folios) == 2

    otro = "5215533331111"
    handle_incoming(session, otro, PEDIDO)
    handle_incoming(session, otro, "no")
    assert session.exec(select(Ticket).where(Ticket.phone == otro)).all() == []


def test_un_solo_tramite_no_se_numera(session):
    """Con uno solo no hay nada que elegir: sigue el sí/no de siempre."""
    texto = "\n".join(handle_incoming(session, PHONE, "acta de nacimiento"))

    assert "¿Confirmas 'Acta de nacimiento'" in texto, texto
    assert "1." not in texto
