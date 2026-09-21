"""Las claves del cliente no salen hacia un modelo, la bitácora NLU ni el log.

Auditoría 2026-09 (IA-5/IA-6/IA-7, INFRA-4/EXT-5). El trámite se deduce de
«quiero mi acta de nacimiento», no de la CURP que viene al lado: todo lo que
viaja a Ollama (clasificador, capa informativa, nodo agéntico, minería), lo que
se guarda en `NluEvent` —que además se espeja a MSSQL— y lo que cae en una línea
de log va enmascarado con `codigos.enmascarar` o `utils.mask_phone`.

Ollama nunca se toca: todas las llamadas pasan por `nlu.ollama_chat`, el embudo
único, y aquí se sustituye por un espía que guarda lo que habría viajado.
"""

import json
import logging
import re
from pathlib import Path

import pytest
from sqlmodel import select

from app import agent_tools, conversation, nlu, rag, synonym_miner
from app.config import settings
from app.models import NluEvent

CURP = "GOMC800101HDFRRN09"
RFC = "GOMC800101AB1"
TELEFONO = "55 1234 5678"
# Nada aquí coincide con el catálogo: así la capa literal no resuelve y el
# texto sí llega a la capa del modelo.
# Tampoco dice «curp» ni «rfc» con letras: en el catálogo semilla son sinónimos.
MENSAJE = f"oiga necesito arreglar lo de mi papel, mis claves son {CURP} y {RFC}, tel {TELEFONO}"


@pytest.fixture
def espia(monkeypatch):
    """Sustituye el embudo de Ollama; devuelve la lista de `messages` enviados."""
    enviados: list[list[dict]] = []
    respuesta = {"content": json.dumps({"service_id": None, "category_id": None})}

    def _falso(messages, **_kwargs):
        enviados.append(messages)
        return dict(respuesta)

    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(nlu, "ollama_chat", _falso)
    _falso.respuesta = respuesta
    _falso.enviados = enviados
    return _falso


def _todo_lo_enviado(espia) -> str:
    return "\n".join(str(m.get("content")) for lote in espia.enviados for m in lote)


def _sin_claves(texto: str) -> None:
    assert CURP not in texto and CURP.lower() not in texto, texto
    assert RFC not in texto, texto
    assert "1234 5678" not in texto and "12345678" not in texto, texto


def test_el_clasificador_recibe_el_texto_sin_claves(espia):
    nlu.deduce_explained(MENSAJE)

    assert espia.enviados, "el texto no llegó a la capa Ollama: la prueba no mide nada"
    enviado = _todo_lo_enviado(espia)
    _sin_claves(enviado)
    # Lo que sí sirve para deducir el trámite sigue ahí.
    assert "[CURP]" in enviado and "papel" in enviado


def test_una_curp_partida_por_el_tope_no_viaja_a_medias(espia):
    """Enmascarar DESPUÉS de cortar dejaría pasar media CURP: ya no tiene forma
    de CURP y ningún patrón la ve."""
    relleno = "x" * (nlu.MAX_INPUT_CHARS - 10)
    nlu.deduce_explained(f"{relleno} {CURP}")

    assert espia.enviados
    assert CURP[:8] not in _todo_lo_enviado(espia)


def test_la_capa_informativa_pregunta_sin_claves(espia, monkeypatch):
    monkeypatch.setattr(settings, "rag_enabled", True)
    espia.respuesta["content"] = json.dumps({"respuesta": rag.NO_SE})

    rag.answer_explained(MENSAJE)

    assert espia.enviados
    usuario = [m["content"] for lote in espia.enviados for m in lote if m["role"] == "user"]
    _sin_claves("\n".join(usuario))


def test_el_nodo_agentico_recibe_el_texto_sin_claves(monkeypatch, session):
    monkeypatch.setattr(settings, "agent_tools_enabled", True)
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.invalid")
    vistos: list[str] = []
    monkeypatch.setattr(
        agent_tools, "_loop_ollama", lambda _s, _p, texto: vistos.append(texto)
    )

    agent_tools.responder(session, "5215512345678", MENSAJE)

    assert vistos
    _sin_claves(vistos[0])


def test_la_mineria_manda_los_mensajes_sin_claves():
    cuerpo = synonym_miner._cuerpo_acotado([MENSAJE, "quiero la cosa esa del seguro"])

    _sin_claves(cuerpo)
    assert "cosa esa del seguro" in cuerpo


def test_la_bitacora_nlu_guarda_el_texto_enmascarado(session):
    conversation._log_nlu(session, "5215512345678", MENSAJE, "ninguna")
    session.commit()

    evento = session.exec(select(NluEvent)).one()
    _sin_claves(evento.text)
    assert "[CURP]" in evento.text


def test_el_uso_de_una_herramienta_se_registra_enmascarado(session):
    agent_tools._registrar_uso(session, "5215512345678", MENSAJE, "consultar_folios", False)
    session.commit()

    evento = session.exec(select(NluEvent)).one()
    _sin_claves(evento.text)


def test_el_guardrail_pii_no_escribe_en_el_log_lo_que_acaba_de_bloquear(espia, monkeypatch, caplog):
    monkeypatch.setattr(settings, "rag_enabled", True)
    # Sexo «X»: el guardia de fugas de rag (laxo a propósito) la atrapa, pero
    # los patrones exactos de codigos no — `enmascarar` no la taparía. Por eso
    # con motivo `pii` no se registra NADA del texto, solo el largo.
    casi_curp = "GOMC800101XDFRRN09"
    espia.respuesta["content"] = json.dumps({"respuesta": f"Su CURP es {casi_curp}."})

    with caplog.at_level(logging.INFO, logger=rag.logger.name):
        respuesta, motivo = rag.answer_explained("cual es mi curp")

    assert respuesta is None and motivo == "pii"
    assert "pii" in caplog.text, "el guardrail no dejó rastro: la prueba no mide nada"
    assert casi_curp not in caplog.text


# Una llamada a logger que pasa `phone` (o `algo.phone`) como argumento suelto.
# Envuelto en mask_phone(...) el paréntesis previo lo excluye.
_PHONE_CRUDO_RE = re.compile(r"logger\.\w+\([^)]*?[,\s](?:\w+\.)?phone\s*[,)]", re.DOTALL)


def test_ningun_log_de_la_app_lleva_el_telefono_crudo():
    """INFRA-4: el webhook enmascaraba y el panel no. Se revisa el código, no
    una lista de rutas: la línea que alguien agregue mañana también cae aquí."""
    culpables = []
    for ruta in sorted(Path("app").glob("*.py")):
        fuente = ruta.read_text(encoding="utf-8")
        for m in _PHONE_CRUDO_RE.finditer(fuente):
            linea = fuente.count("\n", 0, m.start()) + 1
            culpables.append(f"{ruta}:{linea}")
    assert not culpables, f"teléfono sin mask_phone en: {culpables}"
