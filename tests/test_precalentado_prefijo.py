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

import httpx
import pytest

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
