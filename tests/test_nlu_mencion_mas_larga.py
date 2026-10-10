"""La mención más larga gana en `deduce_all` (PR9 de «casos para análisis», D1 reabierta).

`deduce_all` decide si la frase nombra VARIOS trámites. Hasta ahora contaba cada servicio cuyo nombre
o sinónimo aparecía, aunque apareciera DENTRO del nombre de otro: «Acta de matrimonio» contaba
también «Acta de nacimiento» (su sinónimo «acta») y el bot le ofrecía al cliente dos trámites que no
pidió. Medido con el catálogo de producción: 24 nombres o sinónimos de 18 servicios se troceaban así.

La regla, la misma que `_exact_matches` ya aplica a un solo servicio, ahora por tramo: un servicio
cuenta solo si alguna de sus menciones no cae dentro de una mención MÁS LARGA de otro servicio. Dos
menciones del mismo tramo («vigencia de derechos», nombre o sinónimo de dos servicios) cuentan las
dos: eso sí es ambigüedad real y la resuelve el cliente.

Si estas pruebas se ponen rojas, el cliente que pide un trámite recibe una confirmación de dos o
tres, y el asesor recibe folios que nadie pidió.
"""

from sqlmodel import select

import app.nlu as nlu
from app.catalog import catalog
from app.conversation import handle_incoming
from app.models import Conversation, Ticket
from tests.datos.set_dorado_casos import CASO_2026_09_24

PHONE = "5215533330000"


def _ids(texto: str) -> list[str]:
    return [s.id for s in nlu.deduce_all(texto)]


def _terminos_vivos() -> list[tuple[str, str]]:
    return [
        (s.id, t)
        for c in catalog.categories
        for s in c.services
        for t in [s.name, *s.synonyms]
        if t.strip()
    ]


def test_ningun_nombre_ni_sinonimo_vivo_se_trocea_salvo_vigencia_de_derechos(catalogo_vivo):
    """Escribir el nombre exacto de un trámite nunca debe ofrecer varios. La única excepción es
    «vigencia de derechos», que en producción es nombre de un servicio y sinónimo de otro: ahí el
    texto sí es ambiguo y el cliente elige."""
    troceados = {(sid, t): _ids(t) for sid, t in _terminos_vivos() if len(_ids(t)) >= 2}

    assert {nlu._norm(t) for _, t in troceados} == {"vigencia de derechos"}, (
        f"nombres o sinónimos vivos que siguen troceándose: {troceados}"
    )
    for ids in troceados.values():
        assert sorted(ids) == ["con-vigencia-derechos", "sin-vigencia-derechos"]


def test_el_nombre_de_un_acta_ya_no_arrastra_el_acta_de_nacimiento(catalogo_vivo):
    assert _ids("Acta de matrimonio") == []
    assert nlu.deduce("Acta de matrimonio")[0].id == "sb-acta-matrimonio"


def test_dos_tramites_distintos_siguen_contando_dos(catalogo_vivo):
    """La regla solo quita lo que cae DENTRO de otra mención; dos pedidos separados siguen siendo dos."""
    assert _ids("quiero acta y curp") == ["sb-acta-nacimiento", "sb-curp"]


def test_el_caso_del_24_de_septiembre_sigue_nombrando_tres(catalogo_vivo):
    """Ese texto nombra tres servicios por separado; corregirlo es de la regla del folio (PR4),
    no de esta."""
    assert _ids(CASO_2026_09_24) == ["sb-curp", "con-semanas-cotizadas", "con-nss-pdf"]


def test_basta_una_mencion_suelta_para_que_el_servicio_cuente(catalogo_vivo):
    """«rfc» aparece dos veces: una suelta y otra dentro de «constancia con id cif y rfc». La suelta
    es un pedido propio de la constancia original y no se puede perder."""
    ids = _ids("mi rfc y la constancia con id cif y rfc")

    assert "sat-csf-original" in ids, ids
    assert "sat-cif-rfc" in ids, ids


def test_la_mencion_mas_larga_de_otro_es_estricta(catalogo_vivo):
    """Con el mismo largo no hay «más larga»: los dos servicios cuentan (ambigüedad real)."""
    assert sorted(_ids("vigencia de derechos")) == ["con-vigencia-derechos", "sin-vigencia-derechos"]


def test_acta_de_matrimonio_confirma_solo_ese_tramite(session, catalogo_vivo):
    """Por el camino real: el cliente escribe el nombre del trámite y la confirmación que recibe
    es de ese trámite, no de dos."""
    replies = handle_incoming(session, PHONE, "Necesito el acta de matrimonio, soy Juan Pérez")

    texto = "\n".join(replies)
    assert "Acta de matrimonio" in texto, texto
    assert "Acta de nacimiento" not in texto, f"se troceó en dos trámites: {texto!r}"
    assert session.get(Conversation, PHONE).state == "confirmar", texto

    handle_incoming(session, PHONE, "si")
    session.expire_all()
    pedidos = {t.service_id for t in session.exec(select(Ticket).where(Ticket.phone == PHONE)).all()}
    pendientes = session.get(Conversation, PHONE)
    assert pedidos <= {"sb-acta-matrimonio"}, f"folios de trámites que no pidió: {pedidos}"
    assert "sb-acta-nacimiento" not in pendientes.context_json, "quedó pendiente un acta que no pidió"


# --- Empate en el mismo tramo: se pregunta cuál (A1, operador 2026-10-10) -----------------------
#
# «vigencia de derechos» es nombre de un servicio y sinónimo de otro. La regla de arriba deja los
# dos porque el texto de verdad no dice cuál; lo que estaba mal era lo siguiente: el bot pedía
# confirmar LOS DOS, y el cliente que quería uno solo no tenía cómo decirlo. Ahora pregunta cuál.


def _estado(session) -> str:
    session.expire_all()
    return session.get(Conversation, PHONE).state


def test_el_empate_se_detecta_solo_en_el_mismo_tramo(catalogo_vivo):
    vigencia = "vigencia de derechos"
    assert sorted(s.id for s in nlu.empate_por_tramo(vigencia, nlu.deduce_all(vigencia))) == [
        "con-vigencia-derechos",
        "sin-vigencia-derechos",
    ]
    pedido = "quiero acta y curp"
    assert nlu.empate_por_tramo(pedido, nlu.deduce_all(pedido)) == [], "dos pedidos distintos no son un empate"


def test_un_empate_pregunta_cual_en_vez_de_confirmar_los_dos(session, catalogo_vivo):
    replies = handle_incoming(session, PHONE, "vigencia de derechos")

    texto = "\n".join(replies)
    assert "¿Cuál necesitas?" in texto, f"confirmó los dos en vez de preguntar: {texto!r}"
    assert "Confirmas estos 2" not in texto
    assert "Sindo de vigencia de derechos" in texto and "Vigencia de derechos" in texto
    assert _estado(session) == "eligiendo_sugerencia"


def test_al_elegir_del_empate_se_confirma_solo_lo_elegido(session, catalogo_vivo):
    replies = handle_incoming(session, PHONE, "vigencia de derechos")
    opciones = [linea for linea in replies[-1].splitlines() if linea[:2] in ("1.", "2.")]
    elegido = opciones[0].split(". ", 1)[1]

    replies = handle_incoming(session, PHONE, "1")

    texto = "\n".join(replies)
    assert _estado(session) == "confirmar"
    assert f"¿Confirmas '{elegido}'" in texto, texto
    assert "estos 2" not in texto


def test_un_empate_junto_a_otro_tramite_anota_el_claro_y_pregunta_el_otro(session, catalogo_vivo):
    """«vigencia de derechos y mi curp»: la CURP es clara y no se pierde; la vigencia se pregunta."""
    replies = handle_incoming(session, PHONE, "necesito vigencia de derechos y mi curp")

    texto = "\n".join(replies)
    assert "Voy anotando 'Descarga de CURP'" in texto, texto
    assert "¿Cuál necesitas?" in texto
    assert _estado(session) == "eligiendo_sugerencia"

    replies = handle_incoming(session, PHONE, "2")

    texto = "\n".join(replies)
    assert "Confirmas estos 2 trámites" in texto, texto
    assert "Descarga de CURP" in texto
