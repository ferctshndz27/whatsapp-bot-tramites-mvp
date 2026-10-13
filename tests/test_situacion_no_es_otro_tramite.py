"""«Tengo una situación con mi CURP» describe un caso: no es un segundo trámite.

Prueba del operador por Twilio (2026-10-13): «tengo una situación con mi curp qué se puede hacer».
El bot anotó 'Descarga de CURP' y preguntó «también me pediste otro trámite» con cuatro constancias
de situación fiscal, porque «situacion» es palabra de esas constancias. «situacion» y «que se puede
hacer» son frases con que el cliente cuenta un caso (`nlu.FRASES_REVISION`), no el nombre de nada.
Si estas pruebas se ponen rojas, el cliente que cuenta su problema recibe una lista de trámites
que no pidió.
"""

import pytest

from app.conversation import handle_incoming
from app.models import Conversation

PHONE = "5215544440000"


def _estado(session) -> str:
    session.expire_all()
    return session.get(Conversation, PHONE).state


@pytest.mark.parametrize(
    "texto",
    [
        "tengo una situación con mi curp qué se puede hacer",  # el caso real
        "tengo una situacion con mi curp",
        "mi curp tiene una situación, qué puedo hacer",
    ],
)
def test_describir_un_caso_con_un_tramite_no_pregunta_por_otro(session, texto):
    replies = handle_incoming(session, PHONE, texto)

    salida = "\n".join(replies)
    assert "También me pediste otro trámite" not in salida, salida
    assert "situación fiscal" not in salida.lower(), salida
    assert "Descarga de CURP" in salida, salida
    assert _estado(session) != "eligiendo_sugerencia", salida


def test_un_segundo_tramite_de_verdad_sigue_preguntando(session):
    """Control: «acta y curp» es un pedido doble con un acta sin definir; eso sí se pregunta."""
    replies = handle_incoming(session, PHONE, "quiero acta y curp")

    salida = "\n".join(replies)
    assert "Voy anotando 'Descarga de CURP'" in salida, salida
    assert "Acta de matrimonio" in salida, salida
    assert _estado(session) == "eligiendo_sugerencia"


def test_situacion_fiscal_pedida_por_nombre_sigue_siendo_tramite(session):
    """Control: «situación» dentro del nombre de un trámite no se descarta como frase de caso."""
    replies = handle_incoming(session, PHONE, "quiero mi curp y una constancia de situación fiscal")

    salida = "\n".join(replies)
    assert "Descarga de CURP" in salida, salida
    assert "ituación fiscal" in salida, salida
