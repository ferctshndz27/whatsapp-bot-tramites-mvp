"""El set dorado de «casos para análisis» contra el bot real (diseño v3.2 §8.1).

Cada caso entra por handle_incoming() con el catálogo de PRODUCCIÓN y las dos perillas
encendidas: el set describe lo que el bot debe hacer el día que el operador las
encienda. Que con las perillas apagadas nada cambie lo prueban las matrices de PR4 y
PR5, no este archivo.

Lo que el bot todavía no hace lleva `xfail` estricto con el PR que lo trae
(`PENDIENTES`). Cuando ese PR lo resuelve, el XPASS rompe la suite y obliga a quitar la
marca: el set no puede quedarse atrás del código sin que nadie lo note. La marca solo
cubre un AssertionError; un caso mal armado (un servicio que no existe, una clave que
falta) sigue rompiendo la suite, y la coherencia de los datos se revisa aparte, sin
marcas.

Las etiquetas las decide el operador; si al refrescar el volcado del catálogo cambia un
caso, lo que cambió es producción y se dice en el PR.
"""

import json
import re

import pytest

import app.db as db
import app.nlu as nlu
from app.catalog import catalog
from app.config import settings
from app.conversation import _status_reply, handle_incoming
from app.models import Conversation, Ticket
from tests.datos.set_dorado_casos import (
    ACTA_39,
    ACTA_40,
    CASOS,
    NEUTRO_39,
    NEUTRO_40,
    Caso,
    Hace,
)

PHONE = "5215511112222"

# Nacen en PR3 (tarea 18) y las leen PR4 y PR5; antes de eso, ponerlas no cambia nada.
PERILLAS = ("contexto_folio_enabled", "revision_enabled")

# Los estados que crean la regla del folio y la de la revisión (diseño §5.1 y §5.2).
ESTADOS_NUEVOS = {"aclarando_folio", "aclarando_caso"}

# Caso → el PR que lo resuelve. Al resolverlo, ese PR borra su línea.
PENDIENTES = {
    "real-24-09-sin-folio": "PR5: el caso real sin folio ofrece la revisión",
    "situacion-con-mi-pension": "PR5: una frase sin servicios ofrece la revisión",
    "me-revisas-mi-acta-40": "PR5: frase, un servicio y 40 palabras ofrecen la revisión",
    "me-orientas-licencia": "PR5: una frase sin servicios ofrece la revisión",
    "corregi-ante-renapo": "PR5: una frase sin servicios ofrece la revisión",
    "real-24-09-sin-frases": "PR5: D9, tres servicios en un texto largo",
    "dos-servicios-en-45-palabras": "PR5: D9, dos servicios en un texto largo",
    "se-solicita-apoyo-ante-renapo": "PR5: una frase sin servicios ofrece la revisión",
}


def _casos_con_marcas() -> list:
    params = []
    for caso in CASOS:
        marcas = ()
        if caso.id in PENDIENTES:
            marcas = pytest.mark.xfail(
                strict=True, raises=AssertionError, reason=PENDIENTES[caso.id]
            )
        params.append(pytest.param(caso, id=caso.id, marks=marcas))
    return params


@pytest.fixture
def sesion_viva(session, monkeypatch, request):
    """La sesión del bot con el catálogo de producción en la MISMA base y las perillas
    encendidas. Sin unir los engines, el catálogo quedaría en la base de
    `_isolated_db` y los folios en la de `session`."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    request.getfixturevalue("catalogo_vivo")
    for perilla in PERILLAS:
        monkeypatch.setattr(settings, perilla, True, raising=False)
    return session


def _prepara(session, caso: Caso) -> list[int]:
    """Abre los folios del caso y deja la conversación en su estado; devuelve los ids."""
    folios = []
    for folio in caso.folios:
        servicio = catalog.service_by_id(folio.servicio)
        if servicio is None:
            # LookupError y no assert: la marca xfail de un caso pendiente cubre los
            # AssertionError, y un caso mal armado no debe pasar por «aún no lo hace».
            raise LookupError(f"{caso.id}: {folio.servicio} no está en el catálogo")
        ticket = Ticket(
            phone=PHONE,
            category=servicio.category_name,
            service_id=servicio.id,
            service_name=servicio.name,
            price_info=servicio.price_info,
            escalado=folio.escalado,
            agent_active=folio.agente_activo,
        )
        session.add(ticket)
        session.commit()
        folios.append(ticket.id)
    contexto = {"pending_service_ids": list(caso.pendientes)} if caso.pendientes else {}
    session.add(Conversation(phone=PHONE, state=caso.estado, context_json=json.dumps(contexto)))
    session.commit()
    return folios


@pytest.mark.parametrize("caso", _casos_con_marcas())
def test_el_bot_hace_lo_que_el_set_dorado_espera(sesion_viva, caso):
    """Si un caso que pasaba se rompe, el bot cambió lo que le hace a un cliente real:
    pregunta por un folio a quien solo contestaba, trocea información, o calla cuando
    debía responder. Cada falla dice el caso y de dónde sale su etiqueta."""
    folios = _prepara(sesion_viva, caso)

    replies = handle_incoming(sesion_viva, PHONE, caso.texto)

    sesion_viva.expire_all()
    conv = sesion_viva.get(Conversation, PHONE)
    contexto = json.loads(conv.context_json or "{}")
    accion = caso.accion
    donde = f"{caso.id} ({caso.nota})"
    if accion.hace is Hace.PREGUNTA_FOLIO:
        assert conv.state == "aclarando_folio", f"{donde}: no preguntó por el folio ({conv.state})"
        assert sorted(contexto.get("folios", [])) == sorted(folios), (
            f"{donde}: la pregunta no ofrece los folios abiertos"
        )
    elif accion.hace is Hace.REVISION:
        assert conv.state == "aclarando_caso", f"{donde}: no ofreció la revisión ({conv.state})"
        assert contexto.get("servicios") == list(accion.servicios), (
            f"{donde}: la lista no trae los servicios nombrados, en orden"
        )
    elif accion.hace is Hace.CAMINO_DE_HOY:
        assert conv.state not in ESTADOS_NUEVOS, f"{donde}: una regla nueva lo secuestró"
        tomados = contexto.get("pending_service_ids") or contexto.get("resueltos_ids") or []
        assert tomados == list(accion.servicios), f"{donde}: tomó {tomados}"
    elif accion.hace is Hace.ESTATUS:
        assert replies == [_status_reply(sesion_viva, PHONE)], f"{donde}: no dio el estatus"
        assert conv.state == caso.estado, f"{donde}: el estatus cambió el estado"
    elif accion.hace is Hace.SILENCIO:
        assert replies == [], f"{donde}: contestó con el agente apagado"
        assert conv.state == caso.estado, f"{donde}: el silencio cambió el estado"
    else:
        pytest.fail(f"{donde}: la acción {accion.hace} no tiene verificación")
    if accion.estado is not None:
        assert conv.state == accion.estado, f"{donde}: quedó en {conv.state}"


def test_los_casos_y_sus_marcas_no_se_desfasan():
    """Una marca de un caso renombrado nunca se retiraría y su PR no sabría que ya lo
    resolvió; un id repetido haría que pytest corriera dos casos con el mismo nombre."""
    ids = [caso.id for caso in CASOS]
    assert len(ids) == len(set(ids)), "hay ids de caso repetidos"
    assert set(PENDIENTES) <= set(ids), f"marcas sin caso: {set(PENDIENTES) - set(ids)}"
    sin_pr = [razon for razon in PENDIENTES.values() if not re.match(r"PR[459]: ", razon)]
    assert not sin_pr, f"cada marca empieza con el PR que la retira: {sin_pr}"


def test_los_servicios_del_set_existen_en_produccion(catalogo_vivo):
    """En un caso marcado xfail, un id mal escrito fallaría por la razón equivocada y la
    marca lo escondería hasta que su PR lo encontrara roto por otra causa."""
    vivos = {s["id"] for c in catalogo_vivo["categories"] for s in c["services"]}
    for caso in CASOS:
        usados = {f.servicio for f in caso.folios} | set(caso.pendientes)
        usados |= set(caso.accion.servicios)
        assert usados <= vivos, f"{caso.id}: no están en producción: {usados - vivos}"
        assert (caso.estado == "confirmar") == bool(caso.pendientes), (
            f"{caso.id}: `confirmar` sin nada que confirmar, o pendientes fuera de él"
        )


@pytest.mark.parametrize(
    ("texto", "palabras"), [(NEUTRO_40, 40), (NEUTRO_39, 39), (ACTA_40, 40), (ACTA_39, 39)]
)
def test_los_pares_de_d12_quedan_a_una_palabra_del_umbral(texto, palabras):
    """Los pares miden el salto de `contexto_min_palabras` (40): si una edición los
    aleja del umbral, dejan de medirlo y nadie lo nota."""
    assert len(nlu._norm(texto).split()) == palabras
