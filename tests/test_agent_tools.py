"""Nodo agéntico (app/agent_tools.py): permisos, bucle y validación.

Las reglas que protegen estas pruebas: "cobrar" JAMÁS se expone al modelo,
el teléfono lo inyecta el servidor (ninguna herramienta lo acepta como
argumento), toda cifra de la respuesta final debe venir textual de un
tool_result, y cualquier falla devuelve None — el flujo determinista de
siempre responde. Con AGENT_TOOLS_ENABLED apagado el flujo es EXACTAMENTE
el de antes.
"""

from types import SimpleNamespace

from sqlmodel import Session, select

import app.db as db
import app.nlu as nlu
from app import agent_tools
from app.catalog import catalog
from app.config import settings
from app.conversation import NOT_RECOGNIZED_MARKER, handle_incoming
from app.models import Client, Conversation, NluEvent

PHONE = "5215512345678"


def _servicio():
    # Uno con dígitos en el precio: la regla de dinero exige que la cifra de
    # una cotización venga textual del tool_result, y "A cotizar" no trae.
    for c in catalog.categories:
        for s in c.services:
            if any(ch.isdigit() for ch in s.price_info):
                return s
    return catalog.categories[0].services[0]


def _respuesta_final(texto: str):
    return SimpleNamespace(
        stop_reason="end_turn",
        content=[SimpleNamespace(type="text", text=texto)],
    )


def _respuesta_tool_use(name: str, args: dict, tool_id: str = "tu_1"):
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args)],
    )


class _ClienteGuionado:
    """Cliente Anthropic falso: entrega respuestas en orden y guarda cada
    llamada para inspeccionar tools/messages enviados."""

    def __init__(self, respuestas):
        self._respuestas = list(respuestas)
        self.llamadas: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.llamadas.append(kwargs)
        return self._respuestas.pop(0)


def _nodo_prendido(monkeypatch, cliente):
    monkeypatch.setattr(settings, "agent_tools_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(nlu, "_client", cliente)


# ---------------------------------------------------------------------------
# Candados de permisos
# ---------------------------------------------------------------------------


def test_cobrar_jamas_se_expone_al_modelo():
    assert agent_tools.PERMISOS["cobrar"] == "deny"
    nombres = [t["name"] for t in agent_tools.tools_expuestos()]
    assert "cobrar" not in nombres
    assert set(nombres) == {"cotizar_servicio", "consultar_folios"}


def test_consultar_folios_no_acepta_telefono_como_argumento():
    # El phone lo pone el servidor: el esquema no tiene propiedades y rechaza
    # cualquier argumento extra que el modelo intente colar.
    schema = next(
        t["input_schema"] for t in agent_tools.tools_expuestos()
        if t["name"] == "consultar_folios"
    )
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False


def test_herramienta_en_deny_ejecutada_es_error(session):
    contenido, es_error = agent_tools._ejecutar(session, PHONE, "quiero cobrar", "cobrar", {})
    assert es_error


# ---------------------------------------------------------------------------
# Apagadores
# ---------------------------------------------------------------------------


def test_flag_apagado_devuelve_none_sin_llamar_al_modelo(monkeypatch, session):
    cliente = _ClienteGuionado([])
    monkeypatch.setattr(settings, "agent_tools_enabled", False)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(nlu, "_client", cliente)

    assert agent_tools.responder(session, PHONE, "cotiza algo") is None
    assert cliente.llamadas == []


def test_sin_api_key_devuelve_none(monkeypatch, session):
    monkeypatch.setattr(settings, "agent_tools_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    assert agent_tools.responder(session, PHONE, "cotiza algo") is None


def test_excepcion_del_modelo_devuelve_none(monkeypatch, session):
    def _revienta(**kwargs):
        raise RuntimeError("red caída")

    _nodo_prendido(
        monkeypatch, SimpleNamespace(messages=SimpleNamespace(create=_revienta))
    )

    assert agent_tools.responder(session, PHONE, "cotiza algo") is None


# ---------------------------------------------------------------------------
# Bucle de herramientas
# ---------------------------------------------------------------------------


def test_cotiza_con_el_precio_del_catalogo(monkeypatch, session):
    servicio = _servicio()
    final = f"'{servicio.name}' cuesta {servicio.price_info} 😊 ¿Te lo registro?"
    cliente = _ClienteGuionado(
        [
            _respuesta_tool_use("cotizar_servicio", {"service_id": servicio.id}),
            _respuesta_final(final),
        ]
    )
    _nodo_prendido(monkeypatch, cliente)

    assert agent_tools.responder(session, PHONE, "¿cuánto cuesta eso?") == final
    # Ambas llamadas exponen SOLO las herramientas permitidas.
    for llamada in cliente.llamadas:
        assert [t["name"] for t in llamada["tools"]] == ["cotizar_servicio", "consultar_folios"]
    # El segundo turno lleva el tool_result con el price_info textual.
    resultados = cliente.llamadas[1]["messages"][-1]["content"]
    assert resultados[0]["is_error"] is False
    assert servicio.price_info in resultados[0]["content"]


def test_cifra_que_no_salio_de_una_herramienta_tumba_la_respuesta(monkeypatch, session):
    servicio = _servicio()
    cliente = _ClienteGuionado(
        [
            _respuesta_tool_use("cotizar_servicio", {"service_id": servicio.id}),
            _respuesta_final("Cuesta 99999 pesos, aprovecha"),
        ]
    )
    _nodo_prendido(monkeypatch, cliente)

    assert agent_tools.responder(session, PHONE, "¿cuánto cuesta?") is None


def test_precio_inventado_en_palabras_tumba_la_respuesta(monkeypatch, session):
    # Sin dígitos no hay nada que cotejar, pero "cuesta … pesos" ES cotizar:
    # sin cifra textual de cotizar_servicio, la respuesta se tumba entera.
    cliente = _ClienteGuionado(
        [_respuesta_final("El acta cuesta ochocientos pesos, aprovecha hoy")]
    )
    _nodo_prendido(monkeypatch, cliente)

    assert agent_tools.responder(session, PHONE, "¿cuánto cuesta el acta?") is None


def test_cifra_subcadena_de_un_tool_result_no_pasa(monkeypatch, session):
    # "$1,500" en el tool_result NO valida un "500" suelto: el cotejo es por
    # número completo (1500), no por subcadena del blob.
    assert agent_tools._respuesta_valida(
        "Cuesta 500, te registro 1 pieza",
        ["«Trámite» — precio de lista: $1,500. Requisitos: INE."],
        ["«Trámite» — precio de lista: $1,500. Requisitos: INE."],
    ) is None
    # El reformateo fiel del mismo número sí pasa.
    assert (
        agent_tools._respuesta_valida(
            "Cuesta 1500 😊",
            ["«Trámite» — precio de lista: $1,500. Requisitos: INE."],
            ["«Trámite» — precio de lista: $1,500. Requisitos: INE."],
        )
        == "Cuesta 1500 😊"
    )


def test_service_id_alucinado_es_tool_result_de_error(monkeypatch, session):
    cliente = _ClienteGuionado(
        [
            _respuesta_tool_use("cotizar_servicio", {"service_id": "no-existe"}),
            _respuesta_final(agent_tools.rag.NO_SE),
        ]
    )
    _nodo_prendido(monkeypatch, cliente)

    assert agent_tools.responder(session, PHONE, "cotiza el mega trámite") is None
    resultados = cliente.llamadas[1]["messages"][-1]["content"]
    assert resultados[0]["is_error"] is True


def test_consultar_folios_usa_el_telefono_del_servidor(monkeypatch, session):
    session.add(Client(phone=PHONE))
    session.commit()
    cliente = _ClienteGuionado(
        [
            # El modelo intenta colar otro número: el esquema no tiene campos
            # y el ejecutor lo ignora — consulta el phone de la conversación.
            _respuesta_tool_use("consultar_folios", {"phone": "5215599999999"}),
            _respuesta_final("Aún no tienes solicitudes registradas 😊"),
        ]
    )
    _nodo_prendido(monkeypatch, cliente)

    respuesta = agent_tools.responder(session, PHONE, "¿cómo van mis cosas?")
    assert respuesta == "Aún no tienes solicitudes registradas 😊"
    resultados = cliente.llamadas[1]["messages"][-1]["content"]
    assert "Aún no tienes ninguna solicitud" in resultados[0]["content"]


def test_bucle_se_corta_en_max_turnos(monkeypatch, session):
    servicio = _servicio()
    tool_use = _respuesta_tool_use("cotizar_servicio", {"service_id": servicio.id})
    cliente = _ClienteGuionado([tool_use] * agent_tools.MAX_TURNOS)
    _nodo_prendido(monkeypatch, cliente)

    assert agent_tools.responder(session, PHONE, "cotiza") is None
    assert len(cliente.llamadas) == agent_tools.MAX_TURNOS


# ---------------------------------------------------------------------------
# Bucle sobre Ollama (sin API de Anthropic — el caso de producción)
# ---------------------------------------------------------------------------


class _OllamaGuionado:
    def __init__(self, mensajes):
        self._mensajes = list(mensajes)
        self.llamadas: list[dict] = []

    def __call__(self, messages, **kwargs):
        self.llamadas.append({"messages": list(messages), **kwargs})
        return self._mensajes.pop(0)


def _nodo_ollama(monkeypatch, fake):
    monkeypatch.setattr(settings, "agent_tools_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://labs.fake")
    monkeypatch.setattr(nlu, "ollama_chat", fake)


def test_ollama_cotiza_con_el_precio_del_catalogo(monkeypatch, session):
    servicio = _servicio()
    final = f"'{servicio.name}' cuesta {servicio.price_info} 😊"
    fake = _OllamaGuionado(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "cotizar_servicio", "arguments": {"service_id": servicio.id}}}
                ],
            },
            {"role": "assistant", "content": final},
        ]
    )
    _nodo_ollama(monkeypatch, fake)

    assert agent_tools.responder(session, PHONE, "¿cuánto cuesta eso?") == final
    # Herramientas en formato Ollama (function calling estilo OpenAI), solo
    # las permitidas.
    nombres = [t["function"]["name"] for t in fake.llamadas[0]["tools"]]
    assert nombres == ["cotizar_servicio", "consultar_folios"]
    # El segundo turno lleva el tool_result como mensaje role=tool.
    ultimo = fake.llamadas[1]["messages"][-1]
    assert ultimo["role"] == "tool" and servicio.price_info in ultimo["content"]


def test_ollama_argumentos_como_json_en_texto(monkeypatch, session):
    # Algunas versiones mandan arguments como string JSON: se parsea igual.
    servicio = _servicio()
    final = f"Sale en {servicio.price_info}"
    fake = _OllamaGuionado(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cotizar_servicio",
                            "arguments": f'{{"service_id": "{servicio.id}"}}',
                        }
                    }
                ],
            },
            {"role": "assistant", "content": final},
        ]
    )
    _nodo_ollama(monkeypatch, fake)

    assert agent_tools.responder(session, PHONE, "precio?") == final


def test_ollama_cifra_inventada_se_tumba_igual(monkeypatch, session):
    servicio = _servicio()
    fake = _OllamaGuionado(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": "cotizar_servicio", "arguments": {"service_id": servicio.id}}}
                ],
            },
            {"role": "assistant", "content": "Cuesta 99999 pesos"},
        ]
    )
    _nodo_ollama(monkeypatch, fake)

    assert agent_tools.responder(session, PHONE, "precio?") is None


def test_sin_ollama_ni_anthropic_devuelve_none(monkeypatch, session):
    monkeypatch.setattr(settings, "agent_tools_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "")

    assert agent_tools.responder(session, PHONE, "cotiza algo") is None


# ---------------------------------------------------------------------------
# Cableado en conversation (_not_understood)
# ---------------------------------------------------------------------------


def _sembrar() -> None:
    with Session(db.engine) as s:
        s.add(Client(phone=PHONE))
        s.add(Conversation(phone=PHONE, state="menu_categoria"))
        s.commit()


def _conversar(texto: str) -> list:
    with Session(db.engine) as s:
        return handle_incoming(s, PHONE, texto)


def _capas() -> list[str]:
    with Session(db.engine) as s:
        return [e.capa for e in s.exec(select(NluEvent).order_by(NluEvent.id)).all()]


def test_respuesta_del_nodo_llega_al_cliente_y_deja_bitacora(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")  # cascada sin LLM
    monkeypatch.setattr(
        agent_tools, "responder", lambda session, phone, text: "Va así tu asunto 😊"
    )
    _sembrar()

    respuestas = _conversar("zzkw pedido rarisimo qqte")

    assert respuestas == ["Va así tu asunto 😊"]
    capas = _capas()
    assert "ninguna" in capas and "tools" in capas


def test_nodo_sin_respuesta_deja_el_flujo_de_siempre(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(agent_tools, "responder", lambda session, phone, text: None)
    _sembrar()

    respuestas = _conversar("zzkw pedido rarisimo qqte")

    assert len(respuestas) == 1 and NOT_RECOGNIZED_MARKER in respuestas[0]
    assert "tools" not in _capas()


def test_ollama_del_nodo_recibe_contexto_amplio_y_timeout_holgado(monkeypatch, session):
    # Mismo mal que la capa RAG: catálogo + esquemas de tools truncados o
    # cortados a los 12s del clasificador. Candado del cableado.
    from app import rag

    fake = _OllamaGuionado([{"role": "assistant", "content": "NO_SE"}])
    _nodo_ollama(monkeypatch, fake)
    monkeypatch.setattr(settings, "ollama_timeout", 12.0)

    agent_tools.responder(session, "5215550000000", "¿me ayudas con un pedido compuesto?")

    assert fake.llamadas[0]["num_ctx"] == rag.OLLAMA_NUM_CTX
    assert fake.llamadas[0]["timeout"] == rag.OLLAMA_TIMEOUT_MIN


# ---------------------------------------------------------------------------
# Telemetría por herramienta (observabilidad del nodo agéntico)
# ---------------------------------------------------------------------------


def _eventos_de_tool(session):
    from sqlmodel import select

    from app.models import NluEvent

    return [
        (e.capa, e.service_id)
        for e in session.exec(select(NluEvent).where(NluEvent.capa.in_(("tool", "tool_error")))).all()
    ]


def test_cada_llamada_de_herramienta_deja_su_rastro(monkeypatch, session):
    # Sin esto, de un nodo agéntico en producción solo quedaba "atendió N
    # mensajes": qué herramienta usó era invisible cuando algo salía raro.
    servicio = _servicio()
    fake = _OllamaGuionado([
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "cotizar_servicio",
                                      "arguments": {"service_id": servicio.id}}}]},
        {"role": "assistant", "content": f"'{servicio.name}' cuesta {servicio.price_info} 😊"},
    ])
    _nodo_ollama(monkeypatch, fake)

    agent_tools.responder(session, PHONE, "cuánto cuesta")

    assert ("tool", "cotizar_servicio") in _eventos_de_tool(session)


def test_la_herramienta_que_falla_se_registra_como_error(session):
    agent_tools._ejecutar(session, PHONE, "cotiza algo", "cotizar_servicio", {"service_id": "no-existe"})

    assert ("tool_error", "cotizar_servicio") in _eventos_de_tool(session)


def test_las_filas_por_llamada_no_inflan_los_denominadores():
    # Una llamada de herramienta NO es un mensaje del cliente: si entrara al
    # denominador, el porcentaje de "no entendidos" se diluiría solo.
    from app.models import CAPAS_EXTRA

    assert {"tool", "tool_error"} <= CAPAS_EXTRA


def test_la_telemetria_no_puede_tumbar_la_respuesta(monkeypatch, session):
    # Best-effort de verdad: si la bitácora revienta, el cliente igual recibe
    # su respuesta.
    def _boom(*a, **kw):
        raise RuntimeError("base caída")

    monkeypatch.setattr(session, "add", _boom)
    contenido, es_error = agent_tools._ejecutar(session, PHONE, "hola", "cobrar", {})
    assert es_error and "no disponible" in contenido


# ---------------------------------------------------------------------------
# Perillas del panel: apagan lo PERMITIDO, jamás encienden lo prohibido
# ---------------------------------------------------------------------------


def test_ninguna_clave_del_panel_puede_encender_cobrar():
    # EL candado de esta feature. PERMISOS es el piso de seguridad (código
    # revisado); las perillas de /agente viven encima y solo pueden APAGAR.
    # Si alguien agrega mañana una clave que toque "cobrar", esto se pone rojo.
    from app.agent_settings import CONFIG_EDITABLE

    assert "cobrar" not in agent_tools.FLAG_POR_HERRAMIENTA
    assert not any("cobrar" in clave for clave in CONFIG_EDITABLE)
    # Y aunque un flag existiera con ese nombre, el permiso manda:
    assert agent_tools._encendida("cobrar") is False


def test_apagar_una_herramienta_la_saca_de_lo_que_ve_el_modelo(monkeypatch):
    monkeypatch.setattr(settings, "tool_cotizar_enabled", False)

    nombres = [t["name"] for t in agent_tools.tools_activos()]

    assert nombres == ["consultar_folios"]
    # El piso de permisos NO cambia: apagar es operativo, no de seguridad.
    assert {t["name"] for t in agent_tools.tools_expuestos()} == {
        "cotizar_servicio", "consultar_folios"
    }


def test_una_herramienta_apagada_tampoco_se_ejecuta(monkeypatch, session):
    # Defensa en profundidad: que no aparezca en el prompt no basta — un modelo
    # puede nombrarla igual (respuesta vieja, inyección). Se rechaza al ejecutar.
    monkeypatch.setattr(settings, "tool_cotizar_enabled", False)
    servicio = _servicio()

    contenido, es_error = agent_tools._ejecutar(
        session, PHONE, "cotiza", "cotizar_servicio", {"service_id": servicio.id}
    )

    assert es_error and "no disponible" in contenido


def test_los_topes_se_leen_en_caliente(monkeypatch, session):
    # Bajar el tope de palabras tumba una respuesta que antes pasaba, sin
    # reiniciar nada.
    larga = " ".join(["palabra"] * 50)
    assert agent_tools._respuesta_valida(larga, [], []) == larga

    monkeypatch.setattr(settings, "agent_max_words", 30)
    assert agent_tools._respuesta_valida(larga, [], []) is None


def test_los_defaults_de_config_no_se_separan_de_las_constantes():
    # Fuente única: los defaults del .env son espejo de las constantes del
    # módulo. Si una cambia y la otra no, el comportamiento por defecto
    # dependería de dónde se lea — este test lo impide.
    from app.config import Settings

    limpio = Settings()
    assert limpio.agent_max_turnos == agent_tools.MAX_TURNOS
    assert limpio.agent_max_words == agent_tools.MAX_RESPONSE_WORDS
