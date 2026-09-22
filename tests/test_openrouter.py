"""OpenRouter debajo del embudo `nlu.ollama_chat` (labs cerrado, 2026-09-22).

Lo que se fija aquí, y lo que se rompe en el negocio si se va a rojo:

- Con OPENROUTER_API_KEY la capa de modelo abierto existe aunque no haya
  OLLAMA_URL, y con las dos manda OpenRouter. Si no, el bot vuelve a entender
  solo lo literal: el ~19 % de mensajes que resolvía el modelo cae a «ninguna».
- La llamada sale en formato OpenAI con la llave en la cabecera, el esquema
  como `response_format` y las tools tal cual; sin la llave OpenRouter contesta
  401 y la capa muere en silencio.
- Lo que vuelve tiene la forma de Ollama (argumentos como dict) y el historial
  del agente viaja con `id`/`tool_call_id`: sin eso el nodo agéntico revienta
  en la segunda vuelta y el cliente no recibe la cotización.
- Un 200 con `error` cuenta como fallo y la cascada sigue.
- Las claves crudas de `clasificar_codigos` NUNCA salen a OpenRouter.
- Cupo, presupuesto y pausa mandan igual por el mismo embudo.

OpenRouter nunca se toca: `httpx.post` se sustituye por un espía.
"""

from __future__ import annotations

import json
import time

import httpx
import pytest
from sqlmodel import Session

from app import agent_settings, agent_tools, agente_studio, nlu, rag, synonym_miner
from app.catalog import catalog
from app.config import Settings, settings

URL = "https://openrouter.ai/api/v1/chat/completions"
LLAVE = "sk-or-prueba"
# No coincide con nada del catálogo: la capa literal no resuelve y el texto sí
# llega al modelo (misma frase que tests/test_pii_hacia_modelos.py).
MENSAJE = "oiga necesito arreglar lo de mi papel"


class _Respuesta:
    def __init__(self, cuerpo, status: int = 200):
        self._cuerpo, self.status_code = cuerpo, status

    def raise_for_status(self):
        if self.status_code >= 400:
            peticion = httpx.Request("POST", URL)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=peticion,
                response=httpx.Response(self.status_code, request=peticion),
            )

    def json(self):
        return self._cuerpo


def _openai(content=None, tool_calls=None) -> dict:
    """Un `chat.completion` de OpenRouter con un solo choice."""
    mensaje: dict = {"role": "assistant", "content": content}
    if tool_calls:
        mensaje["tool_calls"] = tool_calls
    return {"id": "gen-1", "choices": [{"index": 0, "message": mensaje, "finish_reason": "stop"}]}


def _nada() -> dict:
    return _openai(json.dumps({"service_id": None, "category_id": None}))


@pytest.fixture
def openrouter(monkeypatch):
    """OpenRouter configurado, Ollama no, presupuesto y pausa en cero, y un
    espía en httpx.post que guarda cada llamada y contesta lo programado en
    `respuestas` (en orden; la última se repite)."""
    monkeypatch.setattr(settings, "openrouter_api_key", LLAVE)
    monkeypatch.setattr(settings, "openrouter_model", "openai/gpt-4.1-nano")
    # Un .env de desarrollador con otra base o política no debe mover estas pruebas.
    monkeypatch.setattr(settings, "openrouter_base_url", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(settings, "openrouter_permitir_retencion", False)
    monkeypatch.setattr(settings, "ollama_url", "")
    monkeypatch.setattr(settings, "ollama_timeout", 12.0)
    monkeypatch.setattr(nlu, "_timeouts_seguidos", 0)
    monkeypatch.setattr(nlu, "_pausado_hasta", 0.0)
    token = nlu._DEADLINE.set(None)
    llamadas: list[dict] = []
    respuestas: list = [_nada()]

    def _post(url, **kw):
        llamadas.append({"url": url, **kw})
        cuerpo = respuestas.pop(0) if len(respuestas) > 1 else respuestas[0]
        return cuerpo if isinstance(cuerpo, _Respuesta) else _Respuesta(cuerpo)

    monkeypatch.setattr(httpx, "post", _post)
    _post.llamadas = llamadas
    _post.respuestas = respuestas
    yield _post
    nlu._DEADLINE.reset(token)


def _servicio_real():
    return catalog.categories[0].services[0]


# ---------------------------------------------------------------------------
# Configuración: quién sirve la capa
# ---------------------------------------------------------------------------


def test_modelo_abierto_dice_quien_sirve_la_capa_y_openrouter_manda(monkeypatch):
    casos = [("", "", ""), ("", "http://ollama.invalid", "ollama"),
             (LLAVE, "", "openrouter"), (LLAVE, "http://ollama.invalid", "openrouter")]
    for llave, url, esperado in casos:
        monkeypatch.setattr(settings, "openrouter_api_key", llave)
        monkeypatch.setattr(settings, "ollama_url", url)
        assert settings.modelo_abierto == esperado, (llave, url)


def test_las_tres_variables_se_leen_del_entorno_con_sus_defaults(monkeypatch):
    for clave in ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OPENROUTER_BASE_URL"):
        monkeypatch.delenv(clave, raising=False)
    limpio = Settings()
    assert limpio.openrouter_api_key == ""
    assert limpio.openrouter_model == "openai/gpt-4.1-nano"
    assert limpio.openrouter_base_url == "https://openrouter.ai/api/v1"

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setenv("OPENROUTER_MODEL", "qwen/qwen3-30b-a3b-instruct-2507")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://proxy.example/v1/")
    con_valores = Settings()
    assert con_valores.openrouter_api_key == "k"
    assert con_valores.openrouter_model == "qwen/qwen3-30b-a3b-instruct-2507"
    assert con_valores.openrouter_base_url == "https://proxy.example/v1", "la diagonal final duplicaría //chat"


# ---------------------------------------------------------------------------
# El clasificador sale a OpenRouter en formato OpenAI
# ---------------------------------------------------------------------------


def test_el_clasificador_sale_a_openrouter_con_la_llave_y_el_esquema(openrouter):
    nlu.deduce_explained(MENSAJE)

    assert len(openrouter.llamadas) == 1, "el texto no llegó a OpenRouter: la prueba no mide nada"
    llamada = openrouter.llamadas[0]
    assert llamada["url"] == URL
    assert llamada["headers"]["Authorization"] == f"Bearer {LLAVE}"
    assert "X-Title" in llamada["headers"]
    assert not any("5215" in str(v) or "papel" in str(v) for v in llamada["headers"].values()), \
        "una cabecera lleva algo del cliente"

    cuerpo = llamada["json"]
    assert cuerpo["model"] == "openai/gpt-4.1-nano"
    assert cuerpo["temperature"] == 0 and cuerpo["stream"] is False
    assert cuerpo["max_tokens"] == 128, "num_predict del clasificador no viajó como max_tokens"
    assert cuerpo["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "respuesta", "strict": True, "schema": nlu._SCHEMA},
    }
    assert cuerpo["provider"] == {"require_parameters": True, "data_collection": "deny"}, \
        "sin data_collection=deny OpenRouter enruta por defecto a proveedores que guardan y entrenan"
    assert "think" not in cuerpo and "options" not in cuerpo, "claves de Ollama en un payload OpenAI"
    assert [m["role"] for m in cuerpo["messages"]] == ["system", "user"]
    assert cuerpo["messages"][1]["content"] == MENSAJE

    tiempo = llamada["timeout"]
    assert tiempo.read == 12.0 and tiempo.connect == 2.0


def test_relajar_la_politica_de_datos_es_una_decision_escrita_en_el_env(openrouter, monkeypatch):
    """Solo OPENROUTER_PERMITIR_RETENCION=1 cambia `deny` por `allow`: no un
    toggle en la cuenta de OpenRouter que nadie versiona ni prueba."""
    monkeypatch.setattr(settings, "openrouter_permitir_retencion", True)
    nlu.ollama_chat([{"role": "user", "content": "hola"}])
    assert openrouter.llamadas[-1]["json"]["provider"]["data_collection"] == "allow"

    monkeypatch.delenv("OPENROUTER_PERMITIR_RETENCION", raising=False)
    assert Settings().openrouter_permitir_retencion is False, "el default tiene que ser deny"
    monkeypatch.setenv("OPENROUTER_PERMITIR_RETENCION", "1")
    assert Settings().openrouter_permitir_retencion is True


def test_la_llave_no_aparece_en_los_logs_cuando_openrouter_falla(openrouter, caplog):
    """Candado de regresión (mismo espíritu que tests/test_pii_en_errores.py):
    la capa registra la excepción con %s; si alguien mañana registra
    `exc.response.text` o la petición, la llave del despliegue acaba en el
    log del contenedor."""
    import logging

    for cuerpo in (_Respuesta({"error": "x"}, status=401),
                   {"error": {"message": "Provider returned error", "code": 502}}):
        openrouter.respuestas[:] = [cuerpo]
        with caplog.at_level(logging.WARNING, logger=nlu.logger.name):
            nlu.deduce_explained(MENSAJE)
        assert "falló" in caplog.text, "no se registró el fallo: la prueba no mide nada"
        assert LLAVE not in caplog.text
        caplog.clear()


def test_una_cadena_de_razonamiento_fugada_no_llega_al_cliente_por_el_agente(openrouter, monkeypatch, session):
    """Con OpenRouter el modelo lo elige un admin desde el panel; si uno de
    razonamiento cuela `<think>` en el content, el cliente no lo ve."""
    monkeypatch.setattr(settings, "agent_tools_enabled", True)
    openrouter.respuestas[:] = [_openai(
        "<think>el cliente pregunta por sus folios, no tiene</think>Por ahora no tienes folios abiertos."
    )]

    respuesta = agent_tools.responder(session, "5215512345678", "como van mis folios")

    assert respuesta == "Por ahora no tienes folios abiertos."


def test_lo_que_openrouter_elige_vuelve_como_capa_ollama(openrouter):
    servicio = _servicio_real()
    openrouter.respuestas[:] = [_openai(json.dumps({"service_id": servicio.id, "category_id": None}))]

    resultado, categoria, capa, _ = nlu.deduce_explained(MENSAJE)

    assert resultado is not None and resultado.id == servicio.id
    assert capa == "ollama", "la capa cambió de nombre: las métricas históricas se parten"


def test_sin_llave_el_embudo_sigue_hablando_con_ollama(openrouter, monkeypatch):
    """Control positivo del despacho: el camino de Ollama no se tocó."""
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid")
    openrouter.respuestas[:] = [{"message": {"role": "assistant", "content": "{}"}}]

    mensaje = nlu.ollama_chat([{"role": "user", "content": "hola"}], format=nlu._SCHEMA)

    assert mensaje == {"role": "assistant", "content": "{}"}
    llamada = openrouter.llamadas[0]
    assert llamada["url"] == "http://ollama.invalid/api/chat"
    assert llamada["json"]["think"] is False and llamada["json"]["format"] == nlu._SCHEMA
    assert "Authorization" not in llamada["headers"]


def test_con_ollama_y_openrouter_a_la_vez_gana_openrouter(openrouter, monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid")

    nlu.ollama_chat([{"role": "user", "content": "hola"}])

    assert openrouter.llamadas[0]["url"] == URL


# ---------------------------------------------------------------------------
# Traducción de ida y vuelta: lo que consumen las capas es la forma de Ollama
# ---------------------------------------------------------------------------


def test_la_respuesta_vuelve_en_la_forma_de_ollama_con_argumentos_como_dict(openrouter):
    openrouter.respuestas[:] = [_openai(None, [{
        "id": "call_abc", "type": "function",
        "function": {"name": "cotizar_servicio", "arguments": "{\"service_id\": \"acta\"}"},
    }])]

    mensaje = nlu.ollama_chat([{"role": "user", "content": "hola"}], tools=agent_tools._tools_ollama())

    assert mensaje["role"] == "assistant" and mensaje["content"] == ""
    [llamada] = mensaje["tool_calls"]
    assert llamada["function"] == {"name": "cotizar_servicio", "arguments": {"service_id": "acta"}}
    assert llamada["id"] == "call_abc", "sin el id no hay cómo devolver tool_call_id en la vuelta"


def test_argumentos_ilegibles_o_vacios_quedan_en_dict_vacio(openrouter):
    openrouter.respuestas[:] = [_openai(None, [
        {"id": "c1", "type": "function", "function": {"name": "consultar_folios", "arguments": ""}},
        {"id": "c2", "type": "function", "function": {"name": "cotizar_servicio", "arguments": "{no es json"}},
    ])]

    mensaje = nlu.ollama_chat([{"role": "user", "content": "hola"}], tools=[])

    assert [c["function"]["arguments"] for c in mensaje["tool_calls"]] == [{}, {}]


def test_el_historial_del_agente_viaja_con_id_y_tool_call_id(openrouter):
    """La segunda vuelta del bucle manda lo que la primera devolvió. OpenAI
    exige `id`+`type` en la llamada, argumentos como JSON en texto y el
    `tool_call_id` en cada resultado; Ollama nada de eso."""
    historial = [
        {"role": "system", "content": "sistema"},
        {"role": "user", "content": "cuanto cuesta"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_abc", "function": {"name": "cotizar_servicio", "arguments": {"service_id": "acta"}}},
            {"function": {"name": "consultar_folios", "arguments": {}}},  # sin id: vino de Ollama
        ]},
        {"role": "tool", "tool_name": "cotizar_servicio", "content": "Acta: $150"},
        {"role": "tool", "tool_name": "consultar_folios", "content": "sin folios"},
    ]

    nlu.ollama_chat(historial, tools=[])

    enviados = openrouter.llamadas[0]["json"]["messages"]
    assert [m["role"] for m in enviados] == ["system", "user", "assistant", "tool", "tool"]
    asistente = enviados[2]
    assert asistente["tool_calls"][0]["id"] == "call_abc"
    assert asistente["tool_calls"][0]["type"] == "function"
    assert json.loads(asistente["tool_calls"][0]["function"]["arguments"]) == {"service_id": "acta"}
    sintetico = asistente["tool_calls"][1]["id"]
    assert sintetico, "una llamada sin id no puede casar con su resultado"
    assert enviados[3] == {"role": "tool", "tool_call_id": "call_abc", "content": "Acta: $150"}
    assert enviados[4] == {"role": "tool", "tool_call_id": sintetico, "content": "sin folios"}
    assert all("tool_name" not in m for m in enviados)


def test_el_nodo_agentico_completa_dos_vueltas_por_openrouter(openrouter, monkeypatch, session):
    """De punta a punta por `agent_tools.responder`: la primera vuelta pide una
    herramienta, la segunda contesta. Si la traducción del historial falla, el
    cliente se queda sin respuesta del agente."""
    monkeypatch.setattr(settings, "agent_tools_enabled", True)
    openrouter.respuestas[:] = [
        _openai(None, [{"id": "call_1", "type": "function",
                        "function": {"name": "consultar_folios", "arguments": "{}"}}]),
        _openai("Por ahora no tienes folios abiertos; un asesor te ayuda a abrir uno."),
    ]

    respuesta = agent_tools.responder(session, "5215512345678", "como van mis folios")

    assert len(openrouter.llamadas) == 2, "el bucle no dio la segunda vuelta"
    assert respuesta == "Por ahora no tienes folios abiertos; un asesor te ayuda a abrir uno."
    assert openrouter.llamadas[0]["json"]["max_tokens"] == 400, "el num_predict del agente no viajó"
    segunda = openrouter.llamadas[1]["json"]["messages"]
    assert segunda[-1]["role"] == "tool" and segunda[-1]["tool_call_id"] == "call_1"
    assert segunda[-2]["tool_calls"][0]["type"] == "function"
    assert openrouter.llamadas[1]["json"]["tools"], "la segunda vuelta salió sin herramientas"


# ---------------------------------------------------------------------------
# Fallos: cuentan como fallo del modelo y la cascada sigue
# ---------------------------------------------------------------------------


def test_un_200_con_error_del_proveedor_es_fallo_y_la_cascada_sigue(openrouter):
    openrouter.respuestas[:] = [{"error": {"message": "Provider returned error", "code": 502}}]

    # El mensaje del proveedor tiene que llegar al log: «sin choices» no dice
    # qué revisar.
    with pytest.raises(nlu.OpenRouterError, match="Provider returned error"):
        nlu.ollama_chat([{"role": "user", "content": "hola"}])
    assert nlu._clasificar_fallo(nlu.OpenRouterError("x")) == "openrouter"

    servicio, categoria, capa, _ = nlu.deduce_explained(MENSAJE)
    assert servicio is None and capa != "ollama", "un error del proveedor no puede salir como veredicto"


def test_una_respuesta_sin_choices_tambien_es_fallo(openrouter):
    openrouter.respuestas[:] = [{"id": "gen-1", "choices": []}]

    with pytest.raises(nlu.OpenRouterError):
        nlu.ollama_chat([{"role": "user", "content": "hola"}])


def test_un_401_o_402_se_clasifica_por_codigo_para_el_panel(openrouter):
    for codigo in (401, 402, 429):
        openrouter.respuestas[:] = [_Respuesta({"error": "x"}, status=codigo)]
        with pytest.raises(httpx.HTTPStatusError) as excinfo:
            nlu.ollama_chat([{"role": "user", "content": "hola"}])
        assert nlu._clasificar_fallo(excinfo.value) == f"http_{codigo}"


# ---------------------------------------------------------------------------
# Claves crudas: nunca a un tercero
# ---------------------------------------------------------------------------


CASI_CURP = "GOMC800101HDFRRN0"  # 17 caracteres: ningún patrón de codigos.py la reconoce


def test_las_claves_crudas_no_salen_a_openrouter(openrouter, monkeypatch):
    assert nlu.clasificar_codigos([CASI_CURP]) == {}
    assert openrouter.llamadas == [], "una clave cruda viajó a OpenRouter"

    # Con las dos cosas configuradas manda OpenRouter, así que tampoco.
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid")
    assert nlu.clasificar_codigos([CASI_CURP]) == {}
    assert openrouter.llamadas == []


def test_con_ollama_propio_las_claves_si_se_tipifican(openrouter, monkeypatch):
    """Control positivo: la puerta es por proveedor, no está rota."""
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid")
    openrouter.respuestas[:] = [{"message": {"content": json.dumps(
        {"codigos": [{"codigo": CASI_CURP, "tipo": "CURP"}]}
    )}}]

    assert nlu.clasificar_codigos([CASI_CURP]) == {CASI_CURP: "CURP"}
    assert openrouter.llamadas[0]["url"].endswith("/api/chat")


# ---------------------------------------------------------------------------
# Cupo, presupuesto y pausa: el mismo embudo
# ---------------------------------------------------------------------------


def test_en_pausa_no_se_llama_a_openrouter(openrouter, monkeypatch):
    monkeypatch.setattr(nlu, "_pausado_hasta", time.monotonic() + 100)

    with pytest.raises(nlu.OllamaEnPausa):
        nlu.ollama_chat([{"role": "user", "content": "hola"}])
    assert openrouter.llamadas == []


def test_sin_presupuesto_no_se_llama_a_openrouter(openrouter):
    nlu.iniciar_presupuesto(0.2)

    with pytest.raises(nlu.OllamaSinPresupuesto):
        nlu.ollama_chat([{"role": "user", "content": "hola"}])
    assert openrouter.llamadas == []


def test_los_timeouts_de_openrouter_cuentan_para_la_pausa(openrouter, monkeypatch):
    def _lento(url, **kw):
        raise httpx.ReadTimeout("lento")

    monkeypatch.setattr(httpx, "post", _lento)
    for _ in range(nlu.OLLAMA_TIMEOUTS_PARA_PAUSAR):
        with pytest.raises(httpx.ReadTimeout):
            nlu.ollama_chat([{"role": "user", "content": "hola"}])

    assert nlu._en_pausa(), "tres timeouts seguidos de OpenRouter no pausaron la capa"


def test_el_cupo_se_libera_aunque_openrouter_reviente(openrouter, monkeypatch):
    def _explota(url, **kw):
        raise httpx.ConnectError("caído")

    monkeypatch.setattr(httpx, "post", _explota)
    for _ in range(nlu.OLLAMA_CONCURRENCIA + 1):
        with pytest.raises(httpx.ConnectError):
            nlu.ollama_chat([{"role": "user", "content": "hola"}])
    assert nlu._CUPO_OLLAMA._value == nlu.OLLAMA_CONCURRENCIA


# ---------------------------------------------------------------------------
# Esquemas y panel
# ---------------------------------------------------------------------------


def test_strict_solo_para_los_esquemas_que_cierran_sus_propiedades():
    assert nlu._formato_openai(nlu._SCHEMA)["json_schema"]["strict"] is True
    assert nlu._formato_openai(synonym_miner._MINE_SCHEMA)["json_schema"]["strict"] is True
    assert nlu._formato_openai(rag._FORMATO_RESPUESTA)["json_schema"]["strict"] is False, \
        "OpenAI rechaza con 400 un esquema estricto que no cierra propiedades"


def test_la_capa_informativa_sale_a_openrouter(openrouter, monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", True)
    assert isinstance(rag._retriever(), rag.TopKLexical)
    openrouter.respuestas[:] = [_openai(json.dumps({"respuesta": rag.NO_SE}))]

    rag.answer_explained("a que hora abren")

    assert openrouter.llamadas and openrouter.llamadas[-1]["url"] == URL
    assert nlu.etiqueta_modelo_abierto() == ("OpenRouter", "openai/gpt-4.1-nano")


def test_el_minero_de_sinonimos_sale_a_openrouter_con_su_esquema(openrouter):
    """Con solo OPENROUTER_API_KEY el minero sigue proponiendo (y nada aprende
    solo: lo que vuelve son propuestas pendientes de aprobación)."""
    servicio = _servicio_real()
    openrouter.respuestas[:] = [_openai(json.dumps(
        {"proposals": [{"service_id": servicio.id, "synonym": "cosa del seguro"}]}
    ))]

    propuestas = synonym_miner.mine_synonyms(["quiero la cosa esa del seguro"])

    assert openrouter.llamadas, "el minero no llamó al modelo: la puerta sigue mirando OLLAMA_URL"
    cuerpo = openrouter.llamadas[-1]["json"]
    assert openrouter.llamadas[-1]["url"] == URL
    assert cuerpo["response_format"]["json_schema"]["schema"] == synonym_miner._MINE_SCHEMA
    assert cuerpo["max_tokens"] == 1024
    assert [(p["service_id"], p["synonym"]) for p in propuestas] == [(servicio.id, "cosa del seguro")]


def test_una_caida_de_openrouter_manda_a_revisar_openrouter_y_no_ollama(openrouter):
    """El aviso del Estudio le dice al admin QUÉ revisar: en una caída real de
    OpenRouter, mandarlo al «servidor de Ollama» cuesta tiempo de reacción."""
    import app.db as db
    from app.models import NluEvent

    with Session(db.engine) as s:
        s.add(NluEvent(phone="5215500000000", text="hola", capa="ollama_error", service_id="http_402"))
        s.commit()
        nodo = next(n for n in agente_studio._estado(s)["nodos"] if n["id"] == "ollama")

    assert any("1 falla de conexión" in a and "OpenRouter" in a for a in nodo["avisos"]), nodo["avisos"]
    assert not any("servidor de Ollama" in a for a in nodo["avisos"])


def test_un_content_en_partes_vuelve_como_texto(openrouter):
    """Algunos proveedores contestan `content` como lista de partes; las capas
    hacen json.loads sobre el content y con una lista tronarían (y degradarían
    en silencio a la capa de abajo)."""
    openrouter.respuestas[:] = [_openai([{"type": "text", "text": "{\"service_id\": "},
                                         "basura", {"type": "text", "text": "null, \"category_id\": null}"}])]

    mensaje = nlu.ollama_chat([{"role": "user", "content": "hola"}], format=nlu._SCHEMA)

    assert mensaje["content"] == "{\"service_id\": null, \"category_id\": null}"
    assert json.loads(mensaje["content"])["service_id"] is None


def test_dos_turnos_de_llamadas_seguidos_no_cruzan_sus_resultados(openrouter):
    """Cola FIFO sobre todo el historial: si algún día el bucle manda dos
    turnos de llamadas antes de los resultados, cada resultado sigue tomando
    el id de la llamada más antigua sin contestar."""
    historial = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "A", "function": {"name": "cotizar_servicio", "arguments": {}}},
            {"id": "B", "function": {"name": "consultar_folios", "arguments": {}}},
        ]},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "C", "function": {"name": "cotizar_servicio", "arguments": {}}},
        ]},
        {"role": "tool", "content": "rA"}, {"role": "tool", "content": "rB"}, {"role": "tool", "content": "rC"},
    ]

    nlu.ollama_chat(historial, tools=[])

    enviados = openrouter.llamadas[0]["json"]["messages"]
    assert [(m["tool_call_id"], m["content"]) for m in enviados if m["role"] == "tool"] == \
        [("A", "rA"), ("B", "rB"), ("C", "rC")]


def test_el_estudio_nombra_a_openrouter_y_ofrece_su_perilla(openrouter, session):
    estado = agente_studio._estado(session)
    nodo = next(n for n in estado["nodos"] if n["id"] == "ollama")
    assert nodo["nombre"] == "OpenRouter · openai/gpt-4.1-nano"
    assert nodo["activo"] is True and nodo["fijo"] is False
    assert nodo["campos"] == ["openrouter_model", "ollama_timeout"]


def test_sin_modelo_el_estudio_lo_dice_y_con_ollama_ofrece_la_suya(monkeypatch, session):
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "")
    nodo = next(n for n in agente_studio._estado(session)["nodos"] if n["id"] == "ollama")
    assert nodo["nombre"] == "Modelo abierto" and nodo["fijo"] is True
    assert nlu.etiqueta_modelo_abierto() == ("", "")

    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid")
    monkeypatch.setattr(settings, "ollama_model", "qwen3:4b")
    nodo = next(n for n in agente_studio._estado(session)["nodos"] if n["id"] == "ollama")
    assert nodo["nombre"] == "Ollama · qwen3:4b"
    assert nodo["campos"] == ["ollama_model", "ollama_timeout"]


def test_la_perilla_del_modelo_acepta_ids_de_openrouter_y_rechaza_basura():
    perilla = agent_settings.CONFIG_EDITABLE["openrouter_model"]
    assert agent_settings._convertir(perilla, " qwen/qwen3-30b-a3b-instruct-2507 ") == "qwen/qwen3-30b-a3b-instruct-2507"
    with pytest.raises(ValueError):
        agent_settings._convertir(perilla, "con espacios")
    with pytest.raises(ValueError):
        agent_settings._convertir(perilla, "a" * 81)
