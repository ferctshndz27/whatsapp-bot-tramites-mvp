"""Precalentado del prefijo del catálogo (2026-09-23).

Lo que se rompe en el negocio si esto se pone rojo: el system prompt del
clasificador lleva el catálogo entero y Ollama lo evalúa en frío tras cada
arranque del bot y tras cada cambio del catálogo — más de 60 s con qwen3:4b en
las 2 vCPU del VPS, 22,8 s medidos con 1.7b. Con OLLAMA_TIMEOUT=12, el primer
cliente después de cada deploy escribe y se queda sin modelo. El precalentado
paga ese costo antes de que llegue nadie; estas pruebas clavan cuándo se paga
(arranque, cambio real del catálogo), cuándo no (mismo catálogo, sin Ollama,
lecturas) y que un fallo suyo nunca sale de su hilo.
"""

import threading
from types import SimpleNamespace

import httpx
import pytest

import app.jobs as jobs
import app.main as main
import app.nlu as nlu
from app.catalog import Catalog, catalog
from app.config import settings


@pytest.fixture
def ollama_encendido(monkeypatch):
    """Enciende la capa (URL falsa) y el interruptor que conftest apaga, y
    olvida cualquier prefijo calentado por otra prueba."""
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(nlu, "PRECALENTADO_ACTIVO", True)
    monkeypatch.setattr(nlu, "_prefijo_calentado", None)
    monkeypatch.setattr(nlu, "_precalentando", False)
    monkeypatch.setattr(nlu, "_precalentar_de_nuevo", False)


@pytest.fixture
def ollama_chat_espia(monkeypatch):
    """Sustituye el embudo: registra cada llamada y contesta un mensaje vacío."""
    llamadas: list[dict] = []

    def falso(messages, **kw):
        llamadas.append({"messages": messages, **kw})
        return {"role": "assistant", "content": "{"}

    monkeypatch.setattr(nlu, "ollama_chat", falso)
    return llamadas


def _primer_servicio():
    return catalog.categories[0].services[0]


# --- Qué manda -----------------------------------------------------------------


def test_precalentar_manda_el_mismo_prefijo_que_el_clasificador(ollama_encendido, ollama_chat_espia):
    """Si el system no es byte a byte el del clasificador, Ollama evalúa un
    prefijo que nadie va a reutilizar: se paga el minuto y el cliente lo
    vuelve a pagar. Y sin `num_ctx`: otro tamaño de contexto es otro runner."""
    assert nlu.precalentar_prefijo() is True

    assert len(ollama_chat_espia) == 1
    llamada = ollama_chat_espia[0]
    assert llamada["messages"][0] == {"role": "system", "content": nlu._catalog_system_prompt()}
    assert llamada["messages"][1]["role"] == "user"
    assert "num_ctx" not in llamada or llamada["num_ctx"] is None
    assert llamada["num_predict"] == 1, "un token de salida basta; lo caro es el prefijo"
    assert llamada["timeout"] >= 60, (
        "con el timeout del clasificador (12 s) el precalentado moriría antes de servir"
    )


def test_sin_ollama_no_precalienta(ollama_chat_espia, monkeypatch):
    monkeypatch.setattr(nlu, "PRECALENTADO_ACTIVO", True)
    monkeypatch.setattr(nlu, "_prefijo_calentado", None)
    assert settings.ollama_url == ""  # lo deja así conftest

    assert nlu.precalentar_prefijo() is False
    assert nlu.precalentar_en_segundo_plano("arranque") is False
    assert ollama_chat_espia == []


def test_el_mismo_catalogo_no_se_calienta_dos_veces_y_un_cambio_si(
    ollama_encendido, ollama_chat_espia
):
    """Cada calentamiento son las 2 vCPU ocupadas ~1 min: repetirlo por un
    guardado que no tocó el prompt (asesores, un requisito) es regalar ese
    minuto a los clientes que están escribiendo."""
    assert nlu.precalentar_prefijo() is True
    assert nlu.precalentar_prefijo() is False, "el mismo prefijo se volvió a mandar"
    assert len(ollama_chat_espia) == 1

    svc = _primer_servicio()
    assert catalog.update_service(svc.id, svc.name + " (renombrado)", svc.price_info, admin="qa") is None

    assert nlu.precalentar_prefijo() is True, "el catálogo cambió y el prefijo viejo se dio por bueno"
    assert len(ollama_chat_espia) == 2
    assert svc.name + " (renombrado)" in ollama_chat_espia[1]["messages"][0]["content"]


def test_un_fallo_de_red_no_sale_del_precalentado(ollama_encendido, monkeypatch):
    """Nadie espera por esto: un Ollama caído al arrancar se anota en el log y
    el bot atiende con las capas baratas, como siempre."""
    def revienta(messages, **kw):
        raise httpx.ConnectError("sin ruta a ollama")

    monkeypatch.setattr(nlu, "ollama_chat", revienta)

    assert nlu.precalentar_prefijo() is False
    assert nlu._prefijo_calentado is None, "un fallo dejó el prefijo marcado como caliente"


# --- Cuándo se dispara -----------------------------------------------------------


def test_un_cambio_del_catalogo_avisa_y_una_lectura_no(monkeypatch):
    """El aviso vive en la recarga, no en los diez mutadores: así una edición
    desde la parrilla, el Estudio o una aprobación en /unmatched llegan por el
    mismo camino, y una lectura normal no cuesta nada."""
    avisos: list[str] = []
    monkeypatch.setattr(nlu, "precalentar_en_segundo_plano", lambda motivo: avisos.append(motivo))
    _ = catalog.categories  # primera lectura de esta prueba: catálogo recién sembrado
    avisos.clear()

    _ = catalog.categories
    _ = catalog.categories
    assert avisos == [], "una lectura sin cambios disparó el precalentado"

    svc = _primer_servicio()
    assert catalog.update_service(svc.id, svc.name + " bis", svc.price_info, admin="qa") is None
    assert avisos == ["catalogo"], "cambiar un nombre del catálogo no avisó"

    # Y tocar quién lo atiende no cambia el prompt: tampoco avisa.
    avisos.clear()
    _ = catalog.categories
    assert avisos == []


def test_la_primera_carga_no_avisa():
    """El arranque ya precalienta; si la primera lectura también avisara,
    cada arranque lanzaría dos calentamientos a la vez."""
    avisos: list[int] = []
    nuevo = Catalog()
    nuevo.al_cambiar(lambda: avisos.append(1))

    _ = nuevo.categories

    assert avisos == []


def test_un_suscrito_que_falla_no_rompe_la_lectura_del_catalogo():
    nuevo = Catalog()
    _ = nuevo.categories

    def revienta():
        raise RuntimeError("hilo sin memoria")

    nuevo.al_cambiar(revienta)
    svc = nuevo.categories[0].services[0]
    assert nuevo.update_service(svc.id, svc.name + " x", svc.price_info, admin="qa") is None
    assert nuevo.categories, "el aviso fallido tumbó la recarga del catálogo"


def test_el_arranque_lanza_el_precalentado(monkeypatch):
    """`on_startup()` termina lanzándolo en segundo plano: si desaparece la
    línea, el primer cliente tras cada deploy paga el prefijo frío."""
    motivos: list[str] = []
    for nombre in ("init_db",):
        monkeypatch.setattr(main, nombre, lambda: None)
    monkeypatch.setattr(main.agent_settings, "aplicar_overrides", lambda sesion: None)
    monkeypatch.setattr(main.dashboard, "seed_dashboard_user", lambda s: None)
    monkeypatch.setattr(main.archive, "init", lambda: None)
    monkeypatch.setattr(main.archive, "backfill", lambda: None)
    monkeypatch.setattr(main.activity, "backfill_closed_tickets", lambda: None)
    monkeypatch.setattr(main, "_aplicar_eventos_pendientes", lambda: None)
    monkeypatch.setattr(nlu, "precalentar_en_segundo_plano", lambda motivo: motivos.append(motivo))

    main.on_startup()

    assert motivos == ["arranque"]


# --- El hilo -----------------------------------------------------------------------


def test_dos_avisos_con_uno_en_vuelo_son_dos_corridas_no_tres(ollama_encendido, monkeypatch):
    """Dos ediciones seguidas de la parrilla no pueden ser dos hilos peleando
    por las 2 vCPU; pero tampoco puede perderse la segunda, o el prefijo en
    caché sería el de un catálogo que ya no existe."""
    puede_seguir = threading.Event()
    corridas: list[int] = []

    def lento():
        corridas.append(1)
        puede_seguir.wait(timeout=10)
        return True

    monkeypatch.setattr(nlu, "precalentar_prefijo", lento)

    assert nlu.precalentar_en_segundo_plano("catalogo") is True
    for _ in range(50):  # hasta que el hilo entre en el primer calentamiento
        if corridas:
            break
        threading.Event().wait(0.01)
    assert nlu.precalentar_en_segundo_plano("catalogo") is True
    assert nlu.precalentar_en_segundo_plano("catalogo") is True
    assert threading.active_count() >= 2
    puede_seguir.set()

    for _ in range(200):
        with nlu._precalentado_lock:
            if not nlu._precalentando:
                break
        threading.Event().wait(0.01)

    assert corridas == [1, 1], f"se esperaban dos corridas, hubo {len(corridas)}"


# --- Desalojo por otro prompt (2026-09-23, segunda FTD) ---------------------------
# Confirmado en el log de Ollama del VPS: el minero de las 3:30 pasó con otro
# system y num_ctx 8192; con una sola ranura, el clasificador de las 13:19 tuvo
# que levantar otro runner y evaluar sus 1 491 tokens desde cero: 12 s, 500,
# fuzzy contestó. El precalentado solo vigilaba cambios del catálogo.


def _httpx_responde(monkeypatch, *, content: str = "{}", exc: Exception | None = None):
    """El servidor: contesta `content` o revienta con `exc` al recibir la petición."""
    def post(url, **kw):
        if exc is not None:
            raise exc
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"message": {"content": content}})
    monkeypatch.setattr("httpx.post", post)


@pytest.fixture
def desalojos(ollama_encendido, monkeypatch):
    """Registra cada recalentado pedido, sin abrir hilos."""
    motivos: list[str] = []
    _ = catalog.categories  # el aviso del catálogo recién sembrado sale ANTES del espía
    monkeypatch.setattr(nlu, "precalentar_en_segundo_plano", lambda motivo: motivos.append(motivo) or True)
    monkeypatch.setattr(nlu, "_pausado_hasta", 0.0)
    return motivos


_CLASIFICADOR = lambda: [  # noqa: E731 - se evalúa en cada test, con su catálogo
    {"role": "system", "content": nlu._catalog_system_prompt()},
    {"role": "user", "content": "quiero un acta"},
]
_MINERO = [{"role": "system", "content": "Eres un minero de sinónimos."},
           {"role": "user", "content": "mensajes…"}]


def test_otro_prompt_por_el_embudo_desaloja_el_prefijo(desalojos, monkeypatch):
    _httpx_responde(monkeypatch)
    antes = nlu._generacion_cache

    nlu.ollama_chat(_MINERO, num_ctx=8192)

    assert desalojos == ["otro_prompt"], "el minero pasó por la ranura y nadie recalentó"
    assert nlu._generacion_cache == antes + 1


def test_el_clasificador_no_se_desaloja_a_si_mismo(desalojos, monkeypatch):
    _httpx_responde(monkeypatch)

    nlu.ollama_chat(_CLASIFICADOR(), format=nlu._SCHEMA)

    assert desalojos == [], "cada clasificación lanzaría un recalentado: 2 vCPU ocupadas por nada"


def test_el_mismo_system_con_otro_num_ctx_es_otro_runner(desalojos, monkeypatch):
    _httpx_responde(monkeypatch)

    nlu.ollama_chat(_CLASIFICADOR(), num_ctx=8192)

    assert desalojos == ["otro_prompt"]


def test_un_timeout_del_clasificador_dispara_el_recalentado(desalojos, monkeypatch):
    """Sin esto, cada cliente paga 12 s y aborta, Ollama tira lo evaluado, y el
    siguiente vuelve a empezar: el prefijo nunca termina de calentarse solo."""
    _httpx_responde(monkeypatch, exc=httpx.ReadTimeout("lento"))

    with pytest.raises(httpx.ReadTimeout):
        nlu.ollama_chat(_CLASIFICADOR(), format=nlu._SCHEMA)

    assert desalojos == ["timeout"]


def test_un_servidor_caido_o_en_pausa_no_pide_recalentar(desalojos, monkeypatch):
    _httpx_responde(monkeypatch, exc=httpx.ConnectError("sin ruta"))
    with pytest.raises(httpx.ConnectError):
        nlu.ollama_chat(_CLASIFICADOR())
    with pytest.raises(httpx.ConnectError):
        nlu.ollama_chat(_MINERO, num_ctx=8192)

    monkeypatch.setattr(nlu, "_pausado_hasta", float("inf"))
    with pytest.raises(nlu.OllamaEnPausa):
        nlu.ollama_chat(_MINERO, num_ctx=8192)

    assert desalojos == [], "recalentar contra un servidor caído solo suma otro fallo"


def test_el_precalentado_no_se_reprograma_a_si_mismo_en_bucle(desalojos, monkeypatch):
    """Su propia llamada es la del clasificador; si cortara por timeout y eso
    contara como desalojo, el hilo se repetiría sin fin contra un Ollama lento."""
    _httpx_responde(monkeypatch, exc=httpx.ReadTimeout("prefijo eterno"))

    assert nlu.precalentar_prefijo() is False

    assert desalojos == []


def test_tras_un_desalojo_el_mismo_catalogo_se_vuelve_a_calentar(ollama_encendido, ollama_chat_espia, monkeypatch):
    monkeypatch.setattr(nlu, "precalentar_en_segundo_plano", lambda motivo: True)
    assert nlu.precalentar_prefijo() is True
    assert nlu.precalentar_prefijo() is False

    assert nlu.prefijo_desalojado("otro_prompt") is True

    assert nlu.precalentar_prefijo() is True, "mismo hash, pero la caché ya no está: había que volver"
    assert len(ollama_chat_espia) == 2


def _sin_catalogo_en_el_hilo(monkeypatch):
    """El hilo del precalentado lee el catálogo, y el motor en memoria de la
    suite es de un solo hilo (SingletonThreadPool: otro hilo ve una base
    vacía). Aquí se prueba el hilo, no el catálogo: prefijo y firma fijos."""
    monkeypatch.setattr(nlu, "_catalog_system_prompt", lambda: "system fijo")
    monkeypatch.setattr(nlu, "_firma_prefijo", lambda: "firma fija")


def test_un_desalojo_durante_un_precalentado_en_vuelo_no_se_pierde(ollama_encendido, monkeypatch):
    """El minero termina mientras el hilo aún calienta el prefijo de antes: el
    hilo guarda «caliente» un prefijo que el minero acaba de tirar. La
    generación de caché hace que la repetición sí llame al servidor."""
    puede_seguir = threading.Event()
    llamadas: list[int] = []

    def lento(messages, **kw):
        llamadas.append(1)
        puede_seguir.wait(timeout=10)
        return {"content": "{"}

    monkeypatch.setattr(nlu, "ollama_chat", lento)
    _sin_catalogo_en_el_hilo(monkeypatch)

    assert nlu.precalentar_en_segundo_plano("arranque") is True
    for _ in range(100):
        if llamadas:
            break
        threading.Event().wait(0.01)
    assert nlu.prefijo_desalojado("otro_prompt") is True  # con el hilo dentro de la llamada
    puede_seguir.set()
    assert nlu.esperar_precalentado(5.0) is True

    assert llamadas == [1, 1], "el desalojo llegó durante la llamada y la repetición se saltó por hash"


def test_esperar_precalentado_devuelve_false_si_sigue_en_vuelo(ollama_encendido, monkeypatch):
    puede_seguir = threading.Event()
    monkeypatch.setattr(nlu, "ollama_chat", lambda messages, **kw: (puede_seguir.wait(timeout=10), {"content": "{"})[1])
    _sin_catalogo_en_el_hilo(monkeypatch)
    assert nlu.esperar_precalentado(0.1) is True, "sin hilo no hay nada que esperar"

    nlu.precalentar_en_segundo_plano("arranque")
    assert nlu.esperar_precalentado(0.1) is False
    puede_seguir.set()
    assert nlu.esperar_precalentado(5.0) is True


def test_el_job_nocturno_espera_al_recalentado_antes_de_salir(monkeypatch):
    """El cron corre `python -m app.jobs` en OTRO proceso: el hilo demonio que
    dispara el minero moriría al salir el job, y el prefijo se quedaría frío
    hasta el primer cliente de la mañana (13:19 UTC del 2026-09-23)."""
    esperas: list[float] = []
    monkeypatch.setattr(jobs, "nightly", lambda: {"propuestas": 0, "motivo": "sin mensajes pendientes",
                                                   "cobranza": {}, "alertas": {}})
    monkeypatch.setattr(nlu, "esperar_precalentado", lambda timeout: esperas.append(timeout) or True)

    assert jobs.main() == 0

    assert len(esperas) == 1 and esperas[0] >= nlu.PRECALENTADO_TIMEOUT_SEGUNDOS
