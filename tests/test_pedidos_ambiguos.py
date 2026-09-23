"""Regresiones de la conversación del 2026-09-11 (teléfono …1152, folio #49).

Veinticuatro minutos de charla que terminaron en CERO folios. El registro de
nluevent dejó las tres causas por escrito:

    capa=ollama  otros-servicios           ← "que tramites hay?"
    capa=ollama  imss-alta-medica-basica   ← "alta medica"
    capa=ollama_error  timeout             ← (el día anterior, cuatro veces)

Cada prueba de este archivo clava una de ellas. Si alguna se pone roja, el bot
volvió a decidir por el cliente o a hacerlo esperar de más.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from sqlmodel import Session, select

import app.conversation as conversation
import app.db as db
import app.nlu as nlu
import app.rag as rag
from app.catalog import catalog
from app.config import settings
from app.models import Conversation, Ticket

TEL = "525512340000"


def _ollama_responde(monkeypatch, payload: dict) -> None:
    """Enciende la capa Ollama y hace que conteste exactamente `payload`."""
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"message": {"content": json.dumps(payload)}},
        ),
    )


def _servicio(nombre: str):
    return next(
        s for c in catalog.categories for s in c.services
        if s.name.lower() == nombre.lower()
    )


# --- Falla 1: preguntar por el catálogo no es pedir un trámite ---------------


def test_preguntar_que_tramites_hay_abre_el_menu_principal(session, monkeypatch):
    """Si "que tramites hay?" no abre el catálogo, el cliente recibe el submenú
    de una categoría que nunca pidió — el 2026-09-11 le ofrecieron buró de
    crédito y pensiones a alguien que preguntaba qué se podía tramitar."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    # El modelo insiste en su cajón de sastre; el atajo debe ganarle de calle.
    _ollama_responde(monkeypatch, {"service_id": None, "category_id": "otros-servicios"})

    respuestas = conversation.handle_incoming(session, TEL, "que tramites hay?")

    texto = "\n".join(respuestas)
    assert "Servicios de" not in texto, "contestó el submenú de una categoría"
    assert "¿Qué tipo de servicio necesitas?" in texto


@pytest.mark.parametrize(
    "texto",
    ["que tramites hay?", "quiero un tramite", "cuales tramites manejan"],
)
def test_las_preguntas_por_el_catalogo_no_cuentan_como_intento_fallido(
    session, monkeypatch, texto
):
    """Tres preguntas por el catálogo escalaban a un asesor: el bot no entendía
    "trámite", sumaba intento fallido y al tercero levantaba un folio
    "Escalado a asesor". Así nacieron los folios #47 y #48."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _ollama_responde(monkeypatch, {"service_id": None, "category_id": None})

    conversation.handle_incoming(session, TEL, texto)

    session.expire_all()
    conv = session.get(Conversation, TEL)
    assert conv.failed_attempts == 0, f"{texto!r} cuenta como intento fallido"
    escalados = session.exec(
        select(Ticket).where(Ticket.phone == TEL, Ticket.escalado == True)  # noqa: E712
    ).all()
    assert not escalados, "una pregunta por el catálogo levantó un folio escalado"


# --- Falla 2: un nombre incompleto lo desambigua el cliente ------------------


def test_un_nombre_incompleto_no_lo_resuelve_el_modelo(monkeypatch):
    """"alta medica" cabe en cinco trámites. Si el modelo elige uno, el cliente
    confirma —y paga— un trámite que no pidió."""
    basica = _servicio("Alta médica básica")
    _ollama_responde(monkeypatch, {"service_id": basica.id, "category_id": None})

    service, category, capa, _ = nlu.deduce_explained("alta medica")

    assert service is None, "el modelo eligió por el cliente"
    assert capa != "ollama"
    # Y la cascada barata ofrece las candidatas, que es la respuesta honesta.
    assert len(nlu.suggestions("alta medica")) >= 2


def test_un_empate_por_sinonimo_si_lo_decide_el_modelo():
    """La frontera del veto. Un empate por sinónimo compartido ("el papel ese")
    es ambigüedad de descripción y la resuelve el LLM leyendo contexto — por
    eso corre antes que el difuso. Confundir ambos casos apagaría la capa."""
    acta = _servicio("Acta de nacimiento")

    assert nlu.familia_ambigua("alta medica", _servicio("Alta médica básica"))
    assert not nlu.familia_ambigua("necesito el papel ese", acta)
    assert not nlu.familia_ambigua("acta de nacimiento", acta)
    # El nombre completo tampoco se veta: ya no le falta nada.
    assert not nlu.familia_ambigua(
        "alta medica basica", _servicio("Alta médica básica")
    )


def test_un_nombre_a_medias_no_lo_resuelve_el_modelo_ni_eligiendo_fuera_de_la_familia(monkeypatch):
    """Medido el 2026-09-23 con qwen3:4b en el VPS: «necesito una constancia»
    —diez constancias lo completan— y el modelo eligió 'Semanas cotizadas',
    que ni siquiera lleva la palabra. El veto solo miraba si la elección
    estaba DENTRO de la familia; una elección de fuera pasaba entera y el
    cliente confirmaba un trámite que no pidió, con un estimado que no era.
    La ambigüedad la define el mensaje, no lo que el modelo contestó."""
    semanas = _servicio("Semanas cotizadas")
    assert nlu.familia_ambigua("necesito una constancia", semanas), (
        "el nombre a medias no se vetó porque el modelo eligió fuera de la familia"
    )

    _ollama_responde(monkeypatch, {"service_id": semanas.id, "category_id": None})
    service, category, capa, _ = nlu.deduce_explained("necesito una constancia")

    assert service is None, "el modelo cotizó semanas cotizadas por una constancia sin apellido"
    assert capa != "ollama"
    assert len(nlu.suggestions("necesito una constancia")) >= 2


def test_alta_medica_pregunta_cual_en_vez_de_anotar_una(session, monkeypatch):
    """El flujo real. El bot llegó a decir "Voy anotando 'Alta médica básica'"
    y a ofrecer las otras cuatro como si fueran un SEGUNDO trámite; el cliente
    eligió una y acabó con dos folios en la confirmación."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    basica = _servicio("Alta médica básica")
    _ollama_responde(monkeypatch, {"service_id": basica.id, "category_id": None})

    respuestas = conversation.handle_incoming(session, TEL, "alta medica")

    texto = "\n".join(respuestas)
    assert "Voy anotando" not in texto, "anotó un trámite que el cliente no eligió"
    assert "También me pediste otro trámite" not in texto, "inventó un segundo pedido"
    assert "¿Cuál necesitas?" in texto


def test_un_servicio_deducido_no_arrastra_a_sus_hermanas(monkeypatch):
    """El mecanismo exacto del doble folio: cuando el servicio lo dedujo un
    modelo, el recorte de menciones no quita nada del texto y los "parecidos"
    que sobran son la misma familia, no otro pedido."""
    basica = _servicio("Alta médica básica")

    assert conversation._pendientes_por_aclarar("alta medica", [basica]) == []


def test_un_pedido_doble_de_verdad_si_pregunta_por_el_que_falta():
    """La contraparte: "quiero mi curp y un acta" sí deja algo sin identificar,
    y callarlo fue el bug que _pendientes_por_aclarar vino a arreglar."""
    curp = _servicio("Descarga de CURP")

    pendientes = conversation._pendientes_por_aclarar("quiero descarga de curp y un acta", [curp])

    assert pendientes, "se perdió en silencio el segundo trámite del mensaje"
    assert all("acta" in s.name.lower() for s in pendientes)


# --- Falla 3: el cliente puede corregir el alcance ---------------------------


def test_el_cliente_puede_quitar_un_tramite_de_la_confirmacion(session, monkeypatch):
    """"no solo alta medica basica" no es una respuesta inválida: es el cliente
    quitando el trámite que no pidió. Tratarlo como error costó 24 minutos y
    terminó en un "no" que borró todo."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    basica = _servicio("Alta médica básica")
    otra = _servicio("Alta médica semiestable")
    conversation.handle_incoming(session, TEL, "hola")
    conv = session.get(Conversation, TEL)
    conversation._set_state(
        session, conv, "confirmar",
        {"pending_service_ids": [otra.id, basica.id]},
    )

    respuestas = conversation.handle_incoming(session, TEL, "no solo alta medica basica")

    texto = "\n".join(respuestas)
    assert "¿Puedes confirmar con" not in texto, "repitió la pregunta en vez de corregir"
    assert basica.name in texto
    assert otra.name not in texto, "dejó el trámite que el cliente quitó"
    session.expire_all()
    conv = session.get(Conversation, TEL)
    assert json.loads(conv.context_json)["pending_service_ids"] == [basica.id]


def test_el_cliente_puede_cambiar_el_tramite_que_eligio_por_error(session, monkeypatch):
    """Se equivocó de número en el mini-menú y lo corrige nombrando el que sí
    quiere. Repetirle "responde sí o no" lo obliga a cancelar y empezar de
    cero: eso fue lo que pasó a las 23:48 del 2026-09-11."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    basica = _servicio("Alta médica básica")
    elegido_por_error = _servicio("Alta médica semiestable")
    conversation.handle_incoming(session, TEL, "hola")
    conv = session.get(Conversation, TEL)
    conversation._set_state(
        session, conv, "confirmar", {"pending_service_ids": [elegido_por_error.id]},
    )

    respuestas = conversation.handle_incoming(session, TEL, "no solo alta medica basica")

    texto = "\n".join(respuestas)
    assert "¿Puedes confirmar con" not in texto, "dejó al cliente atorado en el sí/no"
    assert basica.name in texto
    session.expire_all()
    conv = session.get(Conversation, TEL)
    assert json.loads(conv.context_json)["pending_service_ids"] == [basica.id]


def test_rechazar_el_tramite_pendiente_no_lo_reconfirma(session, monkeypatch):
    """La frontera de la corrección: "no quiero el acta" nombra el MISMO
    trámite que está pendiente. Eso es un rechazo, no una corrección, y no
    puede volver a preguntarse como si el cliente lo hubiera pedido."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    acta = _servicio("Acta de nacimiento")
    conversation.handle_incoming(session, TEL, "hola")
    conv = session.get(Conversation, TEL)
    conversation._set_state(
        session, conv, "confirmar", {"pending_service_ids": [acta.id]},
    )

    respuestas = conversation.handle_incoming(session, TEL, "no quiero acta de nacimiento")

    assert "Va, lo dejamos así" not in "\n".join(respuestas)


def test_confirmar_todo_lo_pedido_sigue_siendo_un_si(session, monkeypatch):
    """La corrección no puede tragarse el camino normal: nombrar los DOS
    trámites no reduce nada, y un "sí" sigue levantando los dos folios."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    basica = _servicio("Alta médica básica")
    otra = _servicio("Alta médica semiestable")
    conversation.handle_incoming(session, TEL, "hola")
    conv = session.get(Conversation, TEL)
    conversation._set_state(
        session, conv, "confirmar",
        {"pending_service_ids": [otra.id, basica.id]},
    )

    conversation.handle_incoming(session, TEL, "si")

    session.expire_all()
    folios = session.exec(select(Ticket).where(Ticket.phone == TEL)).all()
    assert {t.service_id for t in folios} == {basica.id, otra.id}


# --- Falla 4: un backend caído no se reintenta tres veces --------------------


def test_un_timeout_de_ollama_apaga_las_capas_de_prompt_largo(monkeypatch):
    """El clasificador corta a los 12 s, pero la capa informativa tiene piso de
    45 s contra el MISMO servidor. El 2026-09-10 eso dejó a un cliente
    esperando 70 s para acabar escalado a un asesor."""
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "rag_enabled", True)
    nlu.reiniciar_turno()

    def _se_agota(*args, **kwargs):
        raise httpx.TimeoutException("agotado")

    monkeypatch.setattr("httpx.post", _se_agota)
    with pytest.raises(httpx.TimeoutException):
        nlu.ollama_chat([{"role": "user", "content": "hola"}])

    assert nlu.backend_degradado(), "nadie se enteró de que el servidor no responde"

    llamadas = []
    monkeypatch.setattr(
        rag, "answer_explained",
        lambda *a, **kw: (llamadas.append(1), ("respuesta", None))[1],
    )
    monkeypatch.setattr(rag, "es_pregunta_informativa", lambda _t: True)

    assert conversation._try_informative(None, TEL, "cuanto tarda el acta?") is None
    assert not llamadas, "reintentó con la capa informativa y su timeout de 45 s"


def test_cada_mensaje_arranca_sin_heredar_la_degradacion_del_anterior():
    """El cron procesa muchos mensajes en el mismo contexto: si la bandera no
    se reinicia, un timeout de hace dos mensajes apaga las capas de este."""
    nlu.reiniciar_turno()
    nlu._BACKEND_CAIDO.set(True)

    nlu.reiniciar_turno()

    assert not nlu.backend_degradado()
