"""Capa informativa (app/rag.py) y su cableado en conversation.

La regla que protegen estas pruebas: el agente NUNCA revela información
delicada del negocio ni de otros clientes. Dos mecanismos: minimización (al
modelo solo viaja el corpus público) y validación en código de la respuesta
(_respuesta_segura). Además: con RAG_ENABLED apagado el flujo es EXACTAMENTE
el de siempre — la capa nueva no puede cambiar nada si nadie la prendió.
"""

from types import SimpleNamespace

from sqlmodel import Session, select

import app.db as db
from app import rag
import app.nlu as nlu
from app.config import settings
from app.conversation import (
    INFO_REPLY,
    NOT_RECOGNIZED_MARKER,
    handle_incoming,
)
from app.models import Client, Conversation, NluEvent

PHONE = "5215512345678"


def _cliente_que_responde(texto: str):
    return SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                stop_reason="end_turn",
                content=[SimpleNamespace(type="text", text=texto)],
            )
        )
    )


def _rag_prendido(monkeypatch, respuesta_llm: str):
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(nlu, "_client", _cliente_que_responde(respuesta_llm))


# ---------------------------------------------------------------------------
# Detector de pregunta informativa
# ---------------------------------------------------------------------------


def test_detector_reconoce_preguntas_informativas():
    for pregunta in [
        "¿Atienden sábados?",
        "cuanto tarda el acta de nacimiento",
        "que necesito para el rfc",
        "cuál es el horario de atención",
        "¿en cuánto tiempo entregan?",
    ]:
        assert rag.es_pregunta_informativa(pregunta), pregunta


def test_detector_ignora_solicitudes_de_tramite():
    for pedido in ["quiero mi curp", "necesito acta de nacimiento", "1", "si", "hola"]:
        assert not rag.es_pregunta_informativa(pedido), pedido


# ---------------------------------------------------------------------------
# Corpus público (frontera de privacidad)
# ---------------------------------------------------------------------------


def test_corpus_sale_del_catalogo(monkeypatch):
    monkeypatch.setattr(settings, "info_docs_path", "")
    docs = rag._docs()
    from app.catalog import catalog

    assert len(docs) == sum(len(c.services) for c in catalog.categories)
    assert any(catalog.categories[0].services[0].name in d for d in docs)


def test_corpus_incluye_el_documento_curado(monkeypatch, tmp_path):
    ruta = tmp_path / "info.md"
    ruta.write_text(
        "Atendemos sábados de 9 a 13 h.\n\nEntregas en 3 a 5 días hábiles.",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "info_docs_path", str(ruta))

    docs = rag._docs()

    assert "Atendemos sábados de 9 a 13 h." in docs
    assert "Entregas en 3 a 5 días hábiles." in docs


def test_documento_curado_inexistente_no_revienta(monkeypatch):
    monkeypatch.setattr(settings, "info_docs_path", "/no/existe/info.md")
    assert rag._docs()  # el catálogo sigue; el archivo roto solo se loguea


# ---------------------------------------------------------------------------
# answer(): apagadores y felices
# ---------------------------------------------------------------------------


def test_answer_apagado_por_flag_devuelve_none(monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    assert rag.answer("¿atienden sábados?") is None


def test_answer_sin_api_key_devuelve_none(monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert rag.answer("¿atienden sábados?") is None


def test_answer_feliz_devuelve_el_texto(monkeypatch):
    _rag_prendido(monkeypatch, "Sí, atendemos sábados de 9 a 13 😊")
    assert rag.answer("¿atienden sábados?") == "Sí, atendemos sábados de 9 a 13 😊"


def test_el_payload_al_modelo_es_solo_corpus_publico_y_va_truncado(monkeypatch):
    # La minimización de datos es LA defensa principal: lo que no entra al
    # prompt no se puede filtrar. Este test la fija — si alguien agrega una
    # fuente (tickets, notas, mensajes de otros clientes), el assert revienta.
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "info_docs_path", "")
    monkeypatch.setattr(settings, "embeddings_url", "")
    capturado = {}

    def _create(**kw):
        capturado.update(kw)
        return SimpleNamespace(
            stop_reason="end_turn",
            content=[SimpleNamespace(type="text", text="Listo 😊")],
        )

    monkeypatch.setattr(nlu, "_client", SimpleNamespace(messages=SimpleNamespace(create=_create)))

    largo = "¿atienden sábados? " + "x" * 1000
    rag.answer(largo)

    # 1) El mensaje del cliente viaja truncado (tope de tokens y de abuso).
    enviado = capturado["messages"][0]["content"]
    assert len(enviado) == rag.MAX_INPUT_CHARS

    # 2) El system es EXACTAMENTE la plantilla + el corpus público, nada más.
    esperado = rag._SYSTEM_TEMPLATE.format(no_se=rag.NO_SE, corpus="\n".join(rag._docs()))
    assert capturado["system"][0]["text"] == esperado


def test_answer_no_se_devuelve_none(monkeypatch):
    _rag_prendido(monkeypatch, rag.NO_SE)
    assert rag.answer("¿atienden sábados?") is None


def test_answer_ante_excepcion_devuelve_none(monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    def _boom(**kw):
        raise RuntimeError("timeout simulado")

    monkeypatch.setattr(nlu, "_client", SimpleNamespace(messages=SimpleNamespace(create=_boom)))
    assert rag.answer("¿atienden sábados?") is None


def test_answer_refusal_devuelve_none(monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[])
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)
    assert rag.answer("¿atienden sábados?") is None


# ---------------------------------------------------------------------------
# _respuesta_segura: la validación EN CÓDIGO, caso por caso
# ---------------------------------------------------------------------------

def test_respuesta_informativa_limpia_pasa():
    assert rag._respuesta_segura("Entregamos en 3 a 5 días hábiles 😊") is not None
    # Cifras SIN contexto de dinero no son un monto: horarios y plazos pasan.
    assert rag._respuesta_segura("Atendemos sábados de 9 a 13 h") is not None
    # Estas tres son la redacción natural de las respuestas que esta capa
    # existe para dar. Un filtro de dinero que las bloquee la deja inservible:
    # NO volver a meter "son"/"vale" a _DINERO_RE.
    assert rag._respuesta_segura("Son 3 a 5 días hábiles 😊") is not None
    assert rag._respuesta_segura("Son 2 documentos: INE y CURP") is not None
    assert rag._respuesta_segura("Vale por 90 días naturales") is not None


def test_cualquier_monto_se_descarta():
    # Hallazgo HIGH de la revisión: validar "que la cifra exista en el corpus"
    # no bastaba — con 58 servicios en un blob, el precio de OTRO servicio
    # también existe (cotización cruzada), y un monto sin "$" ni se miraba.
    # Esta capa no cotiza: cualquier mención de dinero tumba la respuesta y
    # el precio lo da el flujo determinista, del servicio correcto.
    for texto in [
        "Cuesta $999, aprovecha",
        "La CURP cuesta $80 😊",  # aunque el precio fuera real, aquí no se cotiza
        "Cuesta 999 pesos, aprovecha",
        "El acta son 5000 pesos",
        "MXN 1500 en total",
        "Son 999 pesos la pieza",
        "El precio 250 incluye envío",
        "Son ochocientos pesos",
        "Cobran 800 por el trámite",
    ]:
        assert rag._respuesta_segura(texto) is None, texto


def test_curp_en_la_respuesta_se_descarta():
    # Un CURP con formato real jamás está en el corpus: si aparece, el modelo
    # lo inventó o alguien lo indujo — fuga en potencia, se tira.
    assert rag._respuesta_segura("Tu CURP es MAHJ850315HDFRRN09") is None


def test_telefono_en_la_respuesta_se_descarta():
    assert rag._respuesta_segura("Marca al 55 1234 5678 y listo") is None


def test_curp_en_minusculas_tambien_se_descarta():
    # Hallazgo de la revisión: sin IGNORECASE, el eco de una curp tecleada en
    # minúsculas por el propio cliente pasaba el filtro.
    assert rag._respuesta_segura("tu curp es mahj850315hdfrrn09") is None


def test_telefono_con_guiones_o_parentesis_tambien_se_descarta():
    assert rag._respuesta_segura("Marca al 55-1234-5678") is None
    assert rag._respuesta_segura("Llama al (55) 1234.5678 ya") is None


def test_respuesta_desmedida_se_descarta():
    larga = "palabra " * (rag.MAX_RESPONSE_WORDS + 1)
    assert rag._respuesta_segura(larga) is None


def test_respuesta_vacia_o_no_se_se_descarta():
    assert rag._respuesta_segura("") is None
    assert rag._respuesta_segura(f"Mmm, {rag.NO_SE}") is None


def test_el_corpus_no_lleva_precios(monkeypatch):
    # Frontera: el precio ni siquiera entra al contexto del modelo. Sin esto,
    # una inducción del cliente podría sacar la cifra del servicio equivocado.
    monkeypatch.setattr(settings, "info_docs_path", "")
    assert not any("$" in d for d in rag._docs())


# ---------------------------------------------------------------------------
# Recuperación: puerto y sus dos implementaciones
# ---------------------------------------------------------------------------


def test_sin_embeddings_url_se_usa_corpus_completo(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_url", "")
    assert isinstance(rag._retriever(), rag.CorpusCompleto)


def test_con_embeddings_url_se_usa_top_k(monkeypatch):
    monkeypatch.setattr(settings, "embeddings_url", "http://labs/api/embed")
    assert isinstance(rag._retriever(), rag.TopKVectorial)


def test_top_k_ordena_por_similitud(monkeypatch, tmp_path):
    ruta = tmp_path / "info.md"
    ruta.write_text("Atendemos sábados de 9 a 13 h.", encoding="utf-8")
    monkeypatch.setattr(settings, "info_docs_path", str(ruta))
    monkeypatch.setattr(settings, "rag_top_k", 1)

    def _embed_falso(texts):
        # Eje 1 = "habla de sábados", eje 2 = todo lo demás.
        return [[1.0, 0.0] if "sábados" in t else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(rag, "_embed", _embed_falso)

    contexto = rag.TopKVectorial().contexto("sábados")

    assert contexto == "Atendemos sábados de 9 a 13 h."


def test_embed_acepta_forma_ollama_y_openai(monkeypatch):
    capturado = {}

    def _post_falso(url, json=None, headers=None, timeout=None):
        capturado["headers"] = headers
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"embeddings": [[1.0, 2.0]]},
        )

    monkeypatch.setattr(settings, "embeddings_url", "http://labs/api/embed")
    monkeypatch.setattr(settings, "embeddings_token", "tok-labs")
    monkeypatch.setattr(rag.httpx, "post", _post_falso)
    assert rag._embed(["hola"]) == [[1.0, 2.0]]
    # El token de labs viaja como Bearer (nginx del laboratorio lo exige).
    assert capturado["headers"]["Authorization"] == "Bearer tok-labs"

    def _post_openai(url, json=None, headers=None, timeout=None):
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"data": [{"embedding": [3.0, 4.0]}]},
        )

    monkeypatch.setattr(rag.httpx, "post", _post_openai)
    assert rag._embed(["hola"]) == [[3.0, 4.0]]


def test_coseno_casos_borde():
    assert rag._coseno([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert abs(rag._coseno([1.0, 2.0], [1.0, 2.0]) - 1.0) < 1e-9
    assert rag._coseno([0.0, 0.0], [1.0, 2.0]) == 0.0


def test_fallo_de_embeddings_no_revienta_answer(monkeypatch):
    _rag_prendido(monkeypatch, "da igual")
    monkeypatch.setattr(settings, "embeddings_url", "http://labs/api/embed")

    def _post_roto(*args, **kwargs):
        raise RuntimeError("labs caído")

    monkeypatch.setattr(rag.httpx, "post", _post_roto)
    assert rag.answer("¿atienden sábados?") is None


# ---------------------------------------------------------------------------
# Cableado en conversation (flujo completo con base en memoria del conftest)
# ---------------------------------------------------------------------------


def _sembrar(state: str = "menu_categoria") -> None:
    with Session(db.engine) as s:
        s.add(Client(phone=PHONE))
        s.add(Conversation(phone=PHONE, state=state))
        s.commit()


def _conversar(texto: str) -> list:
    with Session(db.engine) as s:
        return handle_incoming(s, PHONE, texto)


def _capas_registradas() -> list[str]:
    with Session(db.engine) as s:
        return [e.capa for e in s.exec(select(NluEvent).order_by(NluEvent.id)).all()]


def test_flujo_con_rag_apagado_es_el_de_siempre(monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    _sembrar()

    respuestas = _conversar("¿atienden los sábados?")

    assert len(respuestas) == 1 and NOT_RECOGNIZED_MARKER in respuestas[0]
    assert "rag" not in _capas_registradas()


def test_pregunta_libre_con_rag_prendido_se_responde(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")  # cascada sin LLM
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(rag, "answer_explained", lambda text: ("Sí, sábados de 9 a 13 😊", None))
    _sembrar()

    respuestas = _conversar("¿atienden los sábados?")

    assert respuestas == ["Sí, sábados de 9 a 13 😊"]
    # Bitácora doble a propósito: "ninguna" (la cascada no dedujo trámite,
    # el minero de sinónimos sigue viendo lo mismo) + "rag" (la capa nueva).
    capas = _capas_registradas()
    assert "ninguna" in capas and "rag" in capas


def test_pregunta_con_palabra_del_catalogo_prefiere_la_respuesta_al_menu(monkeypatch):
    # "¿cuánto tarda el acta?" pregunta por tiempos; el mini-menú de actas
    # era la malinterpretación clásica. Con respuesta con evidencia, gana la
    # respuesta; sin ella, el menú de siempre (prueba siguiente).
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(
        rag, "answer_explained", lambda text: ("Entregamos en 3 a 5 días hábiles 😊", None)
    )
    _sembrar()

    respuestas = _conversar("¿cuánto tarda el acta?")

    assert respuestas == ["Entregamos en 3 a 5 días hábiles 😊"]


def test_sin_respuesta_con_evidencia_el_menu_de_sugerencias_sigue_vivo(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(
        rag, "answer_explained", lambda text: (None, None)
    )  # NO_SE / falla / apagada
    _sembrar()

    respuestas = _conversar("¿cuánto tarda el acta?")

    assert len(respuestas) == 1
    assert "parecidos" in respuestas[0] or "Confirmas" in respuestas[0]


def test_metricas_excluyen_la_fila_extra_rag():
    # Hallazgo de la revisión: la fila "rag" es EXTRA del mismo mensaje; si
    # contara en el denominador, diluiría el % de no entendidos y podría
    # silenciar la alerta (12/50=24% < umbral 25 vs 12/40=30% real).
    from app import alerts, synonym_miner

    with Session(db.engine) as s:
        s.add(NluEvent(phone=PHONE, text="¿atienden sábados?", capa="ninguna"))
        s.add(NluEvent(phone=PHONE, text="¿atienden sábados?", capa="rag"))
        s.add(NluEvent(phone=PHONE, text="quiero mi curp", capa="nombre"))
        s.commit()
        total, pct = alerts._no_entendidos_stats(s)

    assert (total, pct) == (2, 50.0)  # la fila "rag" no cuenta como mensaje

    metricas = synonym_miner.nlu_metrics()
    assert metricas["total"] == 2
    assert metricas["entendidos_pct"] == 50


def test_informes_genericos_siguen_en_su_embudo_calido(monkeypatch):
    # "¿cuánto cuesta?" sin trámite no tiene respuesta única en el corpus:
    # el embudo de informes gana y la capa rag NI SE INTENTA.
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(rag, "answer_explained", lambda text: ("no debería llamarse", None))
    _sembrar()

    respuestas = _conversar("¿cuánto cuesta?")

    assert respuestas == [INFO_REPLY]
