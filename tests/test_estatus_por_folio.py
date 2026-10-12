"""El cliente pregunta por su caso y el agente le contesta por folio (operador, 2026-10-12).

Un gestor manda por el mismo chat los casos de varios trabajadores y recibe un folio por cada uno
(`CASO_RECIBIDO`). Cuando pregunta «¿ya revisaron mi caso?» el agente debe saber DE CUÁL hablan: si
el cliente nombra un folio se le contesta ese; si tiene varios abiertos y no nombra ninguno, se le
pregunta cuál. Antes, «¿ya revisaron mi caso?», «mi caso» y «#74» recibían «Un asesor te contactará
muy pronto» sin folio ni estado, «folio 74» volcaba la lista e ignoraba el número, y «estado» —que
el propio bot le dice al cliente que escriba— no se entendía.

Si una de estas pruebas se pone roja, el bot le contesta a un cliente real una de estas cosas: no
entiende la pregunta por su caso, le da el estatus de un folio que no preguntó, le confirma o le
niega el folio de OTRO cliente, le roba la respuesta a una pregunta que tenía pendiente, o toma un
número suelto por un folio sin haberle preguntado.
"""

import json
import re

import pytest
from sqlmodel import select

import app.db as db
from app.config import settings
from app.conversation import (
    ESCALATED_WAIT_REPLY,
    ESTATUS_FILA_TODOS,
    ESTATUS_FOLIO,
    ESTATUS_ID_TODOS,
    ESTATUS_PREGUNTA_INICIO,
    FOLIO_NO_ENCONTRADO,
    FOLIO_PREGUNTA_UNO,
    FOLIOS_TOPE,
    NO_TICKETS_REPLY,
    _asks_status,
    handle_incoming,
    presentation,
)
from app.models import Conversation, Ticket, TicketStatus
from app.whatsapp_client import LIST_DESC_MAX, MAX_LIST_ROWS

PHONE = "5215533334444"
OTRO = "5215599998888"
NUEVO_SIN_DUENO = "recibido, en espera de asignación"
LISTA = "Estatus de tus solicitudes"
CASO_LARGO = (
    "mi caso es el que le comento: ya tengo lo de la CURP, también revisé mis semanas cotizadas y "
    "el NSS en PDF porque me dijeron en la ventanilla que los necesitaba, le mando la información "
    "para que la revise y me diga si falta algo más de mi parte para continuar con el trámite que "
    "tenemos abierto desde hace semanas"
)
DOS_LARGO = (
    "Buenos días, quiero sacar mi acta de nacimiento y también la descarga de mi CURP porque me "
    "las piden para entrar a mi empleo nuevo la próxima semana; si me pueden decir cuánto cuesta "
    "cada una y cuándo me las entregan, se los agradezco mucho."
)


@pytest.fixture
def s(session, monkeypatch, request):
    monkeypatch.setattr(db, "engine", session.get_bind())
    request.getfixturevalue("catalogo_vivo")
    monkeypatch.setattr(settings, "contexto_folio_enabled", True)
    monkeypatch.setattr(settings, "revision_enabled", True)
    monkeypatch.setattr(settings, "contexto_min_palabras", 40)
    return session


def _conv(s, estado="escalado", phone=PHONE):
    s.add(Conversation(phone=phone, state=estado, context_json=json.dumps({})))
    s.commit()


def _estado(s, phone=PHONE):
    s.expire_all()
    return s.get(Conversation, phone).state


def _caso(s, phone=PHONE, **extra) -> Ticket:
    t = Ticket(phone=phone, category="(análisis)", service_id="analisis", service_name="Caso para análisis",
               price_info="N/A", status=extra.pop("status", TicketStatus.NUEVO), escalado=True, **extra)
    s.add(t)
    s.commit()
    return t


def _dice(s, texto, phone=PHONE) -> str:
    return "\n".join(handle_incoming(s, phone, texto))


# --- la palabra que el propio bot le pide escribir ------------------------------------------------


@pytest.mark.parametrize("aviso", [FOLIOS_TOPE, FOLIO_NO_ENCONTRADO])
def test_la_palabra_que_el_bot_le_pide_escribir_al_cliente_la_entiende(s, aviso):
    """Los avisos del tope y del folio inexistente dicen «Escribe *palabra* para ver cómo van». El
    del tope decía *estado*, que NO se entendía: caía en el menú de «Estado de cuenta» del catálogo.
    Si se pone roja, el bot manda al cliente a una palabra que él mismo no reconoce."""
    palabra = re.search(r"\*(\w+)\*", aviso).group(1)
    _conv(s, "menu_categoria")
    caso = _caso(s)

    assert _asks_status(palabra), f"el aviso pide escribir «{palabra}» y el bot no lo entiende"
    assert f"folio #{caso.id}" in _dice(s, palabra)


def test_estado_a_secas_sigue_siendo_el_estado_de_cuenta_del_catalogo(s):
    """Por eso el aviso pide *estatus* y no se «arregló» enseñándole al bot que «estado» es el
    estatus: «Estado de cuenta» es un servicio. Si se pone roja, un cliente que busca ese servicio
    recibe la lista de sus folios."""
    _conv(s, "menu_categoria")
    _caso(s)

    assert LISTA not in _dice(s, "estado")


# --- «mi caso»: el seguimiento se entiende y se contesta con el folio -----------------------------


@pytest.mark.parametrize("frase", [
    "¿ya revisaron mi caso?", "quiero saber de mi caso", "mi caso", "¿ya lo vio el asesor?",
    "¿hay novedades de mi caso?", "oiga, ¿y mi folio?",
])
def test_el_seguimiento_de_su_caso_recibe_el_estatus_con_el_folio(s, frase):
    """Antes recibían «Un asesor te contactará muy pronto», sin folio ni estado. Si se pone roja, el
    cliente que pregunta por su caso no sabe el folio ni en qué va."""
    _conv(s)
    caso = _caso(s)

    respuesta = _dice(s, frase)

    assert f"folio #{caso.id}" in respuesta and NUEVO_SIN_DUENO in respuesta, respuesta
    assert ESCALATED_WAIT_REPLY.split("\n")[0] not in respuesta


def test_un_texto_largo_que_dice_mi_caso_es_informacion_y_no_una_pregunta_por_el_estatus(s):
    """«mi caso es el que le comento…» más 60 palabras es el cliente sumando información: la regla
    del folio le pregunta si es para su folio, no le contesta el estatus. Si se pone roja, un
    cliente que escribe su caso completo recibe una lista de estatus y se pierde lo que mandó."""
    _conv(s)
    caso = _caso(s)

    respuesta = _dice(s, CASO_LARGO)

    assert FOLIO_PREGUNTA_UNO.split("{")[0] in respuesta and f"#{caso.id}" in respuesta
    assert LISTA not in respuesta


def test_sin_folios_abiertos_pedir_que_revisen_mi_caso_no_es_un_estatus(s):
    """El cliente SIN folio que dice «quiero que revisen mi caso» está pidiendo el caso, no
    preguntando por uno: contestarle «aún no tienes ninguna solicitud» lo manda al menú en vez de
    ofrecerle la revisión. Si se pone roja, se rompe la puerta de entrada de los casos."""
    _conv(s, "menu_categoria")

    respuesta = _dice(s, "quiero que revisen mi caso")

    assert NO_TICKETS_REPLY not in respuesta


def test_con_una_pregunta_pendiente_mi_caso_no_se_la_roba(s):
    """D16: lo que el cliente contesta a una pregunta pendiente no se interpreta como otra cosa.
    Con la pregunta «¿es para tu folio?» en vuelo, «mi caso» es lo que escribió el cliente, no un
    estatus. Si se pone roja, el estatus le roba la respuesta a la pregunta del folio."""
    _conv(s, "menu_categoria")
    caso = _caso(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    assert _estado(s) == "aclarando_folio"

    respuesta = _dice(s, "mi caso")

    assert _estado(s) == "aclarando_folio"
    assert LISTA not in respuesta and f"#{caso.id}" in respuesta


# --- nombrar el folio -----------------------------------------------------------------------------


@pytest.mark.parametrize("plantilla", ["¿cómo va el folio #{n}?", "#{n}", "folio {n}", "caso {n}", "el caso #{n}"])
def test_nombrar_un_folio_contesta_ese_folio_y_no_los_demas(s, plantilla):
    """Con dos casos abiertos, nombrar uno contesta ESE: ni la lista de todos ni el otro. Si se pone
    roja, «folio 3» vuelca la lista e ignora el número (como hacía antes)."""
    _conv(s)
    viejo = _caso(s)
    nuevo = _caso(s, status=TicketStatus.TOMADO, assigned_to="ana")

    respuesta = _dice(s, plantilla.format(n=viejo.id))

    assert respuesta.startswith(ESTATUS_FOLIO.format(folio=viejo.id, servicio="Caso para análisis", estado=NUEVO_SIN_DUENO))
    assert f"#{nuevo.id}" not in respuesta and "en atención" not in respuesta
    assert _estado(s) == "escalado", "contestar un folio no mueve la conversación"


def test_un_folio_ya_cerrado_del_propio_cliente_tambien_se_contesta(s):
    """El cliente puede preguntar por un caso que ya cerró el asesor: «completado». Si se pone roja,
    «¿y el #12?» responde «no lo encuentro» de un folio que sí es suyo."""
    _conv(s)
    cerrado = _caso(s, status=TicketStatus.CERRADO)
    _caso(s)

    respuesta = _dice(s, f"folio {cerrado.id}")

    assert respuesta.startswith(ESTATUS_FOLIO.format(folio=cerrado.id, servicio="Caso para análisis", estado="completado ✅"))


def test_un_folio_borrado_desde_el_panel_se_contesta_como_inexistente(s):
    """Un folio deshabilitado (borrado suave del panel) ya no existe para el cliente. Si se pone roja,
    el cliente ve el estatus de un folio que el asesor retiró."""
    _conv(s)
    _caso(s)
    borrado = _caso(s, habilitado=False)

    assert _dice(s, f"folio {borrado.id}") == FOLIO_NO_ENCONTRADO.format(folio=borrado.id)


def test_el_folio_de_otro_cliente_y_uno_inexistente_reciben_la_misma_respuesta(s):
    """Privacidad: el bot no confirma ni niega que un folio exista si no es del que pregunta, ni
    deja ver su servicio ni su estado. Si se pone roja, teclear números enumera los folios de otros
    clientes."""
    _conv(s)
    _caso(s)
    ajeno = _caso(s, phone=OTRO, status=TicketStatus.TOMADO, assigned_to="ana")

    del_ajeno = _dice(s, f"folio {ajeno.id}")
    inexistente = _dice(s, "folio 99999")

    assert del_ajeno == FOLIO_NO_ENCONTRADO.format(folio=ajeno.id)
    assert inexistente == FOLIO_NO_ENCONTRADO.format(folio=99999)
    assert "Caso para análisis" not in del_ajeno and "en atención" not in del_ajeno


# --- varios folios: se pregunta cuál -------------------------------------------------------------


def test_con_varios_folios_abiertos_pregunta_de_cual_y_ofrece_la_lista(s):
    """Decisión del operador: si el cliente pregunta por su caso, el agente le pregunta el folio.
    La pregunta lista los folios abiertos, el más nuevo primero, y la lista nativa lleva el folio
    como id para que el toque llegue como «#74». Si se pone roja, el cliente con tres casos recibe
    un volcado o la espera, y no sabe cuál folio es cuál."""
    _conv(s)
    folios = [_caso(s) for _ in range(3)]

    replies = handle_incoming(s, PHONE, "¿cómo va mi caso?")
    salida = presentation(s, PHONE, replies)

    assert replies[0].startswith(ESTATUS_PREGUNTA_INICIO)
    assert all(f"#{t.id}" in replies[0] for t in folios)
    assert isinstance(salida[-1], dict) and "list" in salida[-1], f"la pregunta salió sin lista: {salida[-1]!r}"
    filas = salida[-1]["list"]
    assert [i for i, _ in filas] == [f"#{t.id}" for t in reversed(folios)] + [ESTATUS_ID_TODOS]
    assert filas[-1][1] == ESTATUS_FILA_TODOS
    assert len(filas) <= MAX_LIST_ROWS and all(len(titulo) <= LIST_DESC_MAX for _, titulo in filas)
    assert _estado(s) == "escalado", "preguntar el folio no mueve la conversación"


def test_con_mas_folios_que_filas_la_lista_cabe_y_conserva_todos(s):
    """Sin tope de casos, un gestor puede tener veinte abiertos: la lista de Meta admite 10 filas.
    Entran los 9 más nuevos y «Todos»; los demás se piden tecleando el número. Si se pone roja,
    Meta rechaza la lista y el cliente recibe solo el texto."""
    _conv(s)
    folios = [_caso(s) for _ in range(12)]

    salida = presentation(s, PHONE, handle_incoming(s, PHONE, "¿cómo va mi caso?"))

    filas = salida[-1]["list"]
    assert len(filas) == MAX_LIST_ROWS == 10
    assert filas[0][0] == f"#{folios[-1].id}" and filas[-1][0] == ESTATUS_ID_TODOS


@pytest.mark.parametrize("respuesta", ["{n}", "#{n}", "folio {n}"])
def test_contestar_la_pregunta_con_el_numero_da_el_estatus_de_ese_folio(s, respuesta):
    """El cliente contesta «74» a «¿de cuál folio?»: es lo natural. Si se pone roja, la respuesta
    que el propio bot pidió cae en la espera genérica."""
    _conv(s)
    viejo = _caso(s)
    _caso(s, status=TicketStatus.TOMADO, assigned_to="ana")
    handle_incoming(s, PHONE, "¿cómo va mi caso?")

    resp = _dice(s, respuesta.format(n=viejo.id))

    assert resp.startswith(ESTATUS_FOLIO.format(folio=viejo.id, servicio="Caso para análisis", estado=NUEVO_SIN_DUENO))


@pytest.mark.parametrize("frase", ["estatus", "¿cómo van mis solicitudes?", "mis casos", "seguimiento"])
def test_una_pregunta_general_por_el_estatus_sigue_recibiendo_la_lista(s, frase):
    """Preguntar por TODO («estatus», «mis casos») no se contesta preguntando cuál: se contesta con
    la lista de siempre. La pregunta del folio es para quien pregunta por UN caso. Si se pone roja,
    el cliente que quería el resumen tiene que dar un paso de más."""
    _conv(s)
    _caso(s)
    _caso(s)

    resp = _dice(s, frase)

    assert resp.startswith(LISTA) and ESTATUS_PREGUNTA_INICIO not in resp


def test_contestar_todos_da_el_estatus_de_todos_los_folios(s):
    _conv(s)
    a, b = _caso(s), _caso(s)
    handle_incoming(s, PHONE, "¿cómo va mi caso?")

    resp = _dice(s, "Todos")

    assert LISTA in resp and f"#{a.id}" in resp and f"#{b.id}" in resp


def test_un_numero_suelto_sin_pregunta_previa_no_es_un_folio(s):
    """El número a secas solo es un folio si el bot acaba de preguntar cuál. Si se pone roja, un
    «3» (la cantidad de copias, un día) se toma por el folio #3 y se le contesta el de otro caso."""
    _conv(s)
    caso = _caso(s)
    _caso(s)

    resp = _dice(s, str(caso.id))

    assert resp != "" and f"Folio #{caso.id}" not in resp and LISTA not in resp


def test_el_numero_deja_de_ser_respuesta_cuando_la_pregunta_ya_se_contesto(s):
    """Pregunta → «74» (contestada) → «74» otra vez: la segunda ya no tiene pregunta pendiente.
    Si se pone roja, la pregunta nunca caduca y cualquier número futuro es un folio."""
    _conv(s)
    caso = _caso(s)
    _caso(s)
    handle_incoming(s, PHONE, "¿cómo va mi caso?")
    handle_incoming(s, PHONE, str(caso.id))

    segunda = _dice(s, str(caso.id))

    assert f"Folio #{caso.id}" not in segunda


def test_con_un_solo_folio_abierto_no_se_pregunta_cual(s):
    """Con uno solo no hay a qué desambiguar: se contesta directo. Si se pone roja, el cliente con
    un caso tiene que escribir un número para recibir lo único que podía recibir."""
    _conv(s)
    caso = _caso(s)

    resp = _dice(s, "¿cómo va mi caso?")

    assert ESTATUS_PREGUNTA_INICIO not in resp and f"folio #{caso.id}" in resp


def test_en_un_estado_que_espera_otra_respuesta_no_se_pregunta_cual(s):
    """Con la pregunta «¿es para tu folio?» pendiente, «¿cómo va mi caso?» se contesta como siempre
    (la lista), sin una segunda pregunta encima. Si se pone roja, dos preguntas pendientes a la vez."""
    _conv(s, "menu_categoria")
    _caso(s)
    _caso(s)
    handle_incoming(s, PHONE, DOS_LARGO)
    assert _estado(s) == "aclarando_folio"

    resp = _dice(s, "¿cómo va mi caso?")

    assert resp.startswith(LISTA) and ESTATUS_PREGUNTA_INICIO not in resp
    assert _estado(s) == "aclarando_folio"


def test_con_la_perilla_apagada_se_conserva_la_conducta_de_hoy(s, monkeypatch):
    """Rollback: apagar `contexto_folio_enabled` devuelve el estatus de hoy —la lista, sin
    pregunta; «mi caso» a la espera genérica; el número ignorado—. Si se pone roja, apagar la
    perilla no apaga lo nuevo."""
    monkeypatch.setattr(settings, "contexto_folio_enabled", False)
    _conv(s)
    viejo = _caso(s)
    _caso(s)

    assert _dice(s, "¿cómo va mi caso?").startswith(LISTA)
    assert ESCALATED_WAIT_REPLY.split("\n")[0] in _dice(s, "¿ya revisaron mi caso?")
    assert _dice(s, f"folio {viejo.id}").startswith(LISTA)
