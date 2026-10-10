"""En «varios parecidos», el cliente puede elegir escribiendo el nombre, y nada elegido se pierde.

Prueba por WhatsApp del operador (2026-10-10): «quiero acta y curp» → el bot anotó la CURP y
preguntó cuál acta, con una lista numerada. El cliente contestó «acta de matrimonio» —el nombre, no
el número— y el bot confirmó SOLO la CURP: el acta se perdió sin avisar y se abrió un folio
incompleto. `_handle_sugerencia` solo entendía números; cualquier otra cosa, con trámites ya
anotados, los confirmaba y tiraba la elección (su propio `ponytail` lo admitía).

Decisión del operador («C»): el nombre vale igual que el número, y lo que el cliente pidió nunca se
pierde en silencio. Si estas pruebas se ponen rojas, el cliente vuelve a recibir un folio al que le
falta el trámite que eligió, y lo descubre cuando el asesor le escribe.
"""

import pytest

from app.conversation import handle_incoming
from app.models import Conversation

PHONE = "5215544440000"


def _estado(session) -> str:
    session.expire_all()
    return session.get(Conversation, PHONE).state


def _pedir_acta_y_curp(session) -> str:
    replies = handle_incoming(session, PHONE, "quiero acta y curp")
    texto = "\n".join(replies)
    assert "Voy anotando 'Descarga de CURP'" in texto, f"la preparación cambió: {texto!r}"
    assert "Acta de matrimonio" in texto
    assert _estado(session) == "eligiendo_sugerencia"
    return texto


@pytest.mark.parametrize("respuesta", ["acta de matrimonio", "Acta de Matrimonio", "la de matrimonio", "matrimonio"])
def test_elegir_por_nombre_confirma_lo_anotado_y_lo_elegido(session, respuesta):
    """El caso del operador y sus variantes: el nombre completo o la palabra que distingue la opción."""
    _pedir_acta_y_curp(session)

    replies = handle_incoming(session, PHONE, respuesta)

    texto = "\n".join(replies)
    assert _estado(session) == "confirmar", texto
    assert "Acta de matrimonio" in texto, f"se perdió el acta elegida: {texto!r}"
    assert "Descarga de CURP" in texto, f"se perdió lo ya anotado: {texto!r}"
    assert "Acta de nacimiento" not in texto


def test_elegir_por_numero_sigue_igual(session):
    texto = _pedir_acta_y_curp(session)
    numero = next(
        linea.split(".")[0] for linea in texto.splitlines() if linea.endswith("Acta de matrimonio")
    )

    replies = handle_incoming(session, PHONE, numero)

    texto = "\n".join(replies)
    assert "Acta de matrimonio" in texto and "Descarga de CURP" in texto, texto


def test_una_palabra_que_comparten_todas_las_opciones_no_elige(session):
    """«acta» está en las cuatro: no dice cuál. No elige, y tampoco tira el acta: se vuelve a preguntar."""
    _pedir_acta_y_curp(session)

    replies = handle_incoming(session, PHONE, "el acta")

    texto = "\n".join(replies)
    assert _estado(session) == "eligiendo_sugerencia", f"confirmó sin que el cliente eligiera: {texto!r}"
    assert "Confirmas" not in texto


def test_si_no_se_entiende_la_eleccion_se_vuelve_a_preguntar_sin_perder_nada(session):
    """Una respuesta que no elige nada no puede confirmar lo anotado tirando el resto: se le dice
    al cliente que no se entendió y se repiten las opciones."""
    _pedir_acta_y_curp(session)

    replies = handle_incoming(session, PHONE, "mmm no sé")

    texto = "\n".join(replies)
    assert _estado(session) == "eligiendo_sugerencia", f"salió de la elección: {texto!r}"
    assert "Acta de matrimonio" in texto, "no repitió las opciones"
    assert "Descarga de CURP" in texto, "no recordó lo ya anotado"


def test_a_la_segunda_respuesta_sin_eleccion_se_confirma_lo_anotado_avisando(session):
    """Para no atorar al cliente: a la segunda, se confirma lo anotado y se le DICE que el otro
    trámite quedó fuera, para que lo pida después si lo necesita."""
    _pedir_acta_y_curp(session)
    handle_incoming(session, PHONE, "mmm no sé")

    replies = handle_incoming(session, PHONE, "no sé cuál")

    texto = "\n".join(replies)
    assert _estado(session) == "confirmar", texto
    assert "Descarga de CURP" in texto
    assert "no identifiqué" in texto.lower(), f"tiró el acta sin avisar: {texto!r}"
