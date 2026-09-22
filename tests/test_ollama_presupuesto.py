"""Cupo global, presupuesto por mensaje y pausa alrededor de Ollama
(auditoría 2026-09, IA-4 / EXT-3; IA-8 queda cubierto de paso).

Antes no había nada entre mensajes ni alrededor de Ollama: sin semáforo ni
cola, con OLLAMA_TIMEOUT hasta 60 s y un solo worker cuyo threadpool de 40
hilos comparten el webhook, el panel y /health, N números con preguntas raras
degradaban a todos los clientes y congelaban el panel. Una pregunta no
entendida con RAG + agente sumaba [12, 45, 45, 45, 45] = 192 s.

Todo pasa por el único embudo, `nlu.ollama_chat`, así que se prueba ahí. El
POST real se sustituye: lo que importa es SI se llama y con qué timeout.
"""

from __future__ import annotations

import threading
import time

import httpx
import pytest

from app import nlu
from app.config import settings

MENSAJES = [{"role": "user", "content": "hola"}]


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.prueba")
    monkeypatch.setattr(settings, "ollama_timeout", 12.0)
    nlu.reiniciar_turno()
    # Estado global limpio entre pruebas: la pausa y el contador son de módulo.
    monkeypatch.setattr(nlu, "_timeouts_seguidos", 0)
    monkeypatch.setattr(nlu, "_pausado_hasta", 0.0)
    yield
    # El ContextVar vive en el hilo de la suite: un deadline abierto aquí le
    # recortaría el timeout a la siguiente prueba que lo mida (pasó con test_nlu).
    nlu._DEADLINE.set(None)


class _Respuesta:
    def raise_for_status(self):
        pass

    def json(self):
        return {"message": {"role": "assistant", "content": "ok"}}


def _post_espia(registro: list):
    def _post(url, **kw):
        registro.append(kw)
        return _Respuesta()
    return _post


def test_sin_cupo_no_se_llama_y_la_capa_degrada_en_un_segundo(monkeypatch):
    """Con OLLAMA_CONCURRENCIA generaciones en curso, la siguiente no espera al
    servidor: cae a las capas baratas en ~1 s y marca el backend degradado."""
    llamadas: list = []
    monkeypatch.setattr(httpx, "post", _post_espia(llamadas))
    tomados = [nlu._CUPO_OLLAMA.acquire(timeout=1) for _ in range(nlu.OLLAMA_CONCURRENCIA)]
    assert all(tomados)
    try:
        inicio = time.monotonic()
        with pytest.raises(nlu.OllamaSinCupo):
            nlu.ollama_chat(MENSAJES)
        assert time.monotonic() - inicio < nlu.OLLAMA_ESPERA_CUPO_SEGUNDOS + 1.5
        assert llamadas == [], "sin cupo y aun así hizo el POST"
        assert nlu.backend_degradado() is True
    finally:
        for _ in tomados:
            nlu._CUPO_OLLAMA.release()


def test_el_cupo_se_libera_aunque_el_post_reviente(monkeypatch):
    def _explota(url, **kw):
        raise httpx.ConnectError("caído")
    monkeypatch.setattr(httpx, "post", _explota)
    for _ in range(nlu.OLLAMA_CONCURRENCIA + 2):
        with pytest.raises(httpx.ConnectError):
            nlu.ollama_chat(MENSAJES)
        nlu.reiniciar_turno()
    # Si no se liberara, la (CONCURRENCIA+1)-ésima habría sido OllamaSinCupo, no ConnectError.


def test_sin_presupuesto_no_se_llama(monkeypatch):
    llamadas: list = []
    monkeypatch.setattr(httpx, "post", _post_espia(llamadas))
    nlu.iniciar_presupuesto(0.5)
    time.sleep(0.6)
    with pytest.raises(nlu.OllamaSinPresupuesto):
        nlu.ollama_chat(MENSAJES)
    assert llamadas == [] and nlu.backend_degradado() is True


def test_el_timeout_de_cada_llamada_se_recorta_a_lo_que_queda_del_mensaje(monkeypatch):
    """IA-8 de paso: turnos × timeout del Estudio ya no pueden rebasar el
    presupuesto del mensaje, porque cada llamada recibe solo lo que queda."""
    llamadas: list = []
    monkeypatch.setattr(httpx, "post", _post_espia(llamadas))
    monkeypatch.setattr(settings, "ollama_timeout", 60.0)
    nlu.iniciar_presupuesto(5.0)

    nlu.ollama_chat(MENSAJES, timeout=45.0)  # lo que pediría la capa informativa

    (kw,) = llamadas
    assert kw["timeout"].read <= 5.0, f"la llamada salió con {kw['timeout'].read} s, más que el presupuesto"
    assert kw["timeout"].connect == 2.0


def test_sin_presupuesto_abierto_manda_el_timeout_propio(monkeypatch):
    """El minero nocturno no abre presupuesto: ahí manda su timeout largo."""
    llamadas: list = []
    monkeypatch.setattr(httpx, "post", _post_espia(llamadas))
    nlu._DEADLINE.set(None)
    nlu.ollama_chat(MENSAJES, timeout=300.0)
    assert llamadas[0]["timeout"].read == 300.0


def test_tres_timeouts_seguidos_pausan_a_ollama_para_todos_y_la_pausa_caduca(monkeypatch):
    """La pausa es ENTRE mensajes: sin ella cada mensaje descubría por su cuenta
    que el servidor no contesta, pagando el timeout completo."""
    llamadas: list = []

    def _timeout(url, **kw):
        llamadas.append(kw)
        raise httpx.ReadTimeout("lento")

    monkeypatch.setattr(httpx, "post", _timeout)
    for _ in range(nlu.OLLAMA_TIMEOUTS_PARA_PAUSAR):
        with pytest.raises(httpx.ReadTimeout):
            nlu.ollama_chat(MENSAJES)
        nlu.reiniciar_turno()
    assert len(llamadas) == nlu.OLLAMA_TIMEOUTS_PARA_PAUSAR

    with pytest.raises(nlu.OllamaEnPausa):
        nlu.ollama_chat(MENSAJES)
    assert len(llamadas) == nlu.OLLAMA_TIMEOUTS_PARA_PAUSAR, "en pausa y aun así hizo el POST"

    reloj = time.monotonic()
    monkeypatch.setattr(nlu.time, "monotonic", lambda: reloj + nlu.OLLAMA_PAUSA_SEGUNDOS + 1)
    nlu.reiniciar_turno()
    with pytest.raises(httpx.ReadTimeout):
        nlu.ollama_chat(MENSAJES)
    assert len(llamadas) == nlu.OLLAMA_TIMEOUTS_PARA_PAUSAR + 1, "pasada la pausa no volvió a intentar"


def test_un_acierto_reinicia_la_cuenta_de_timeouts(monkeypatch):
    estados = iter(["timeout", "timeout", "ok", "timeout", "timeout", "ok"])

    def _post(url, **kw):
        if next(estados) == "ok":
            return _Respuesta()
        raise httpx.ReadTimeout("lento")

    monkeypatch.setattr(httpx, "post", _post)
    for _ in range(6):
        try:
            nlu.ollama_chat(MENSAJES)
        except httpx.ReadTimeout:
            pass
        nlu.reiniciar_turno()
    assert not nlu._en_pausa(), "dos timeouts con un acierto en medio no deben pausar"


def test_reiniciar_turno_abre_el_presupuesto_del_mensaje():
    nlu._DEADLINE.set(None)
    nlu.reiniciar_turno()
    restante = nlu.presupuesto_restante()
    assert restante is not None and nlu.PRESUPUESTO_IA_SEGUNDOS - 1 < restante <= nlu.PRESUPUESTO_IA_SEGUNDOS


def test_el_motivo_del_fallo_dice_por_que_no_se_llamo(monkeypatch):
    """El panel del Estudio muestra el motivo por evento: «sin_cupo», «sin_presupuesto»
    y «en_pausa» son accionables; un nombre de clase no."""
    nlu.iniciar_presupuesto(0.0)
    with pytest.raises(nlu.OllamaSinPresupuesto):
        nlu.ollama_chat(MENSAJES)
    assert nlu._FALLO_OLLAMA.get() == "sin_presupuesto"


def test_el_cupo_es_global_entre_hilos(monkeypatch):
    """Ocho hilos a la vez, un POST que tarda: nunca hay más de
    OLLAMA_CONCURRENCIA dentro, y los demás salen por OllamaSinCupo."""
    dentro = {"ahora": 0, "maximo": 0}
    candado = threading.Lock()

    def _lento(url, **kw):
        with candado:
            dentro["ahora"] += 1
            dentro["maximo"] = max(dentro["maximo"], dentro["ahora"])
        time.sleep(1.6)
        with candado:
            dentro["ahora"] -= 1
        return _Respuesta()

    monkeypatch.setattr(httpx, "post", _lento)
    resultados: list[str] = []
    barrera = threading.Barrier(8, timeout=10)

    def uno():
        barrera.wait()
        nlu.reiniciar_turno()
        try:
            nlu.ollama_chat(MENSAJES)
            resultados.append("ok")
        except nlu.OllamaSinCupo:
            resultados.append("sin_cupo")

    hilos = [threading.Thread(target=uno) for _ in range(8)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=20)
    assert dentro["maximo"] <= nlu.OLLAMA_CONCURRENCIA, f"hubo {dentro['maximo']} generaciones a la vez"
    assert resultados.count("ok") == nlu.OLLAMA_CONCURRENCIA and resultados.count("sin_cupo") == 8 - nlu.OLLAMA_CONCURRENCIA
