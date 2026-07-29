"""Concurrencia e idempotencia bajo carga real (hilos de verdad, no simulados).

Contexto honesto de producción: `receive_webhook` es `async def` y llama
`_process_message` de forma síncrona, así que con un solo worker de uvicorn
(el CMD del Dockerfile no pasa --workers) el procesamiento de mensajes queda
SERIALIZADO en el event loop. La concurrencia real hoy viene de (a) las rutas
síncronas del panel, que Starlette corre en su threadpool y tocan las mismas
tablas y el mismo singleton `catalog`, (b) el job nocturno de app/jobs.py y
(c) cualquier futuro `--workers > 1`. Estas pruebas son deliberadamente más
estrictas que el runtime actual: documentan INVARIANTES, no fallas en curso.

Por qué NO se usa el fixture autouse `_isolated_db` del conftest:
  1. `create_engine("sqlite://")` en memoria usa SingletonThreadPool → cada
     hilo vería una base VACÍA distinta y la prueba pasaría por la razón
     equivocada.
  2. El conftest nunca llama `init_db()`, así que el índice único
     `ux_message_wa_message_id` — la ÚNICA red real de producción contra
     webhooks duplicados concurrentes — no existe en las pruebas.
Por eso todo aquí corre sobre el fixture local `engine_produccion`: archivo
temporal + los PRAGMA reales (WAL, busy_timeout) + `init_db()` completo.
"""

import hashlib
import hmac
import json
import threading
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine, select

import app.catalog as catalog_mod
import app.db as db
from app import nlu, webhook, whatsapp_client
from app.catalog import catalog
from app.config import settings
from app.conversation import (
    MAX_FAILED_ATTEMPTS,
    RATE_LIMIT_MAX_MESSAGES,
    RATE_LIMIT_WINDOW_SECONDS,
    handle_incoming,
)
from app.models import (
    Client,
    ClientNote,
    Conversation,
    Direction,
    Feedback,
    Message,
    Ticket,
    TicketStatus,
    utcnow,
)
from app.webhook import _tag_sent_message

PHONE = "5215500000000"

# Estados válidos de la máquina conversacional (app/conversation.py).
ESTADOS_CONOCIDOS = {
    "inicio",
    "esperando_nombre",
    "menu_categoria",
    "menu_servicio",
    "confirmar",
    "eligiendo_sugerencia",
    "pidiendo_requisitos",
    "escalado",
}

# Timeout de las barreras: generoso a propósito. No es sincronización (eso lo
# hace la barrera), es solo el seguro que evita colgar la suite si un hilo
# muere antes de llegar al punto de encuentro.
BARRERA_TIMEOUT = 20.0


# --------------------------------------------------------------------------
# Fixtures y helpers locales (no se toca conftest.py)
# --------------------------------------------------------------------------


class RedProhibida(BaseException):
    """Se dispara si algo de este archivo intenta usar el cliente de Anthropic.

    NO hereda de `Exception` a propósito: `nlu.deduce_explained` envuelve la
    capa LLM en un `except Exception` que cae a keywords, así que una excepción
    normal se tragaría en silencio y la fuga de red se vería exactamente igual
    que un fallback legítimo. Con BaseException el escape es imposible de
    disimular.
    """


class _ClienteAnthropicProhibido:
    """Doble que revienta ante CUALQUIER acceso de atributo (`.messages`, …)."""

    def __getattr__(self, nombre: str):
        raise RedProhibida(f"una prueba intentó usar el cliente de Anthropic (.{nombre})")


@pytest.fixture(autouse=True)
def sin_dependencias_externas(monkeypatch):
    """Apaga el espejo MSSQL y el LLM: sin esto `archive.enabled()` es True
    (el .env local trae MSSQL_HOST y MSSQL_SA_PASSWORD) y CADA insert dispara
    un after_insert que intenta conectar a MSSQL desde N hilos, con globals
    `_engine`/`_ready` que tampoco son thread-safe."""
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    # Cortafuegos de red. `nlu._get_client()` cachea en el global `nlu._client`
    # y solo construye `anthropic.Anthropic(...)` cuando ese global está en
    # None; dejando ahí un doble que explota, ninguna prueba de este archivo
    # puede instanciar el cliente real ni abrir un socket — ni aunque alguien
    # reactive la API key. La guardia se auto-verifica en
    # test_ninguna_prueba_de_este_archivo_puede_salir_a_la_red.
    monkeypatch.setattr(nlu, "_client", _ClienteAnthropicProhibido())


@pytest.fixture()
def engine_produccion(tmp_path, monkeypatch):
    """Engine igual al de producción: archivo real (no :memory:), listener de
    PRAGMA real e `init_db()` completo — que es quien crea el índice único
    `ux_message_wa_message_id`."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/tramites.db", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", db._set_sqlite_pragmas)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    yield engine
    # El catálogo es un singleton de módulo compartido con TODA la suite: si
    # una prueba lo dejó corrupto, aquí se reconstruye antes de soltar el engine.
    catalog._refresh()
    engine.dispose()


@pytest.fixture()
def catalogo_tibio(engine_produccion):
    """Fuerza la siembra del catálogo ANTES de arrancar hilos, para que las
    pruebas de concurrencia no midan además la carrera de la importación."""
    assert len(catalog.categories) == 9  # semilla del repo (app/catalog.json)
    return catalog


@pytest.fixture()
def whatsapp_falso(monkeypatch):
    """Stubea el envío a Meta: sin esto `whatsapp_client._send` haría un POST
    real con reintentos. Devuelve la lista (compartida) de envíos."""
    enviados: list[tuple[str, str]] = []
    candado = threading.Lock()

    def _fabricar(kind: str):
        def _enviar(to: str, *args, **kwargs) -> str:
            with candado:
                enviados.append((kind, to))
                return f"wamid.O{len(enviados)}"

        return _enviar

    monkeypatch.setattr(whatsapp_client, "send_text", _fabricar("text"))
    monkeypatch.setattr(whatsapp_client, "send_buttons", _fabricar("buttons"))
    monkeypatch.setattr(whatsapp_client, "send_list", _fabricar("list"))
    return enviados


APP_SECRET = "test-secret-concurrencia"


@pytest.fixture()
def app_webhook(monkeypatch):
    """App mínima con solo el router del webhook (sin middlewares del panel)."""
    monkeypatch.setattr(settings, "app_secret", APP_SECRET)
    aplicacion = FastAPI()
    aplicacion.include_router(webhook.router)
    return aplicacion


def _firmar(raw: bytes) -> dict[str, str]:
    firma = hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return {"x-hub-signature-256": "sha256=" + firma, "content-type": "application/json"}


def _payload_texto(phone: str, wamid: str, texto: str) -> bytes:
    return json.dumps(
        {
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": wamid,
                                        "from": phone,
                                        "type": "text",
                                        "text": {"body": texto},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ]
        }
    ).encode()


def _esperar(barrera: threading.Barrier) -> None:
    """Punto de encuentro. Si el otro hilo murió antes de llegar, se sigue de
    largo en vez de colgar la suite (la aserción del test dirá qué pasó)."""
    try:
        barrera.wait(timeout=BARRERA_TIMEOUT)
    except threading.BrokenBarrierError:
        pass


def en_paralelo(fn, n: int):
    """Lanza n hilos que se sincronizan en una barrera antes de ejecutar fn(i).
    Cero sleeps: el solape se fuerza con la barrera. Devuelve
    ({indice: resultado}, [(indice, excepcion)])."""
    barrera = threading.Barrier(n)
    resultados: dict[int, object] = {}
    errores: list[tuple[int, BaseException]] = []
    candado = threading.Lock()

    def _worker(i: int) -> None:
        _esperar(barrera)
        try:
            valor = fn(i)
        except BaseException as exc:  # noqa: BLE001 — se reporta, no se traga
            with candado:
                errores.append((i, exc))
        else:
            with candado:
                resultados[i] = valor

    hilos = [threading.Thread(target=_worker, args=(i,), daemon=True) for i in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=BARRERA_TIMEOUT + 15)
    vivos = [h.name for h in hilos if h.is_alive()]
    assert not vivos, f"hilos colgados: {vivos}"
    return resultados, errores


class SesionConBarrera(Session):
    """Session que se detiene en una barrera la PRIMERA vez que recibe un
    add() de `tipo`. Es la forma limpia de volver determinista la ventana
    leer→escribir sin tocar app/ y sin dejar abierta una transacción de
    escritura de SQLite (add() no hace flush, así que nadie tiene el lock)."""

    def __init__(self, bind, tipo, barrera: threading.Barrier):
        super().__init__(bind)
        self._tipo_vigilado = tipo
        self._barrera = barrera
        self._ya_espero = False

    def add(self, instance, *args, **kwargs):
        if not self._ya_espero and isinstance(instance, self._tipo_vigilado):
            self._ya_espero = True
            _esperar(self._barrera)
        return super().add(instance, *args, **kwargs)


def _sembrar_cliente(engine, phone: str = PHONE, state: str = "inicio") -> None:
    """Alta previa de Client + Conversation: aísla la carrera bajo prueba de
    la carrera (aparte) del get-or-create."""
    with Session(engine) as s:
        s.add(Client(phone=phone))
        s.add(Conversation(phone=phone, state=state))
        s.commit()


def _filas(engine, modelo, *filtros) -> list:
    with Session(engine) as s:
        consulta = select(modelo)
        for f in filtros:
            consulta = consulta.where(f)
        return list(s.exec(consulta).all())


def _entrantes(engine, phone: str | None = None) -> list[Message]:
    filtros = [Message.direction == Direction.IN]
    if phone is not None:
        filtros.append(Message.phone == phone)
    return _filas(engine, Message, *filtros)


def _salientes(engine, phone: str | None = None) -> list[Message]:
    filtros = [Message.direction == Direction.OUT]
    if phone is not None:
        filtros.append(Message.phone == phone)
    return _filas(engine, Message, *filtros)


# --------------------------------------------------------------------------
# Idempotencia del webhook (reintentos de Meta)
# --------------------------------------------------------------------------


def test_webhook_duplicado_en_paralelo_deja_una_sola_fila_in(engine_produccion, catalogo_tibio):
    # REGLA: dos reintentos simultáneos de Meta con el mismo wa_message_id son
    # UN solo mensaje. `_already_processed` solo cubre el caso secuencial; el
    # índice único ux_message_wa_message_id es la red real cuando hay solape.
    _sembrar_cliente(engine_produccion)

    def _procesar(_i):
        with Session(engine_produccion) as s:
            return handle_incoming(
                s, PHONE, "necesito acta de nacimiento", wa_message_id="wamid.DUP"
            )

    resultados, errores = en_paralelo(_procesar, 2)

    # Invariante (no el mecanismo: a veces gana _already_processed y a veces el
    # índice único, según el scheduler).
    con_wamid = _filas(
        engine_produccion,
        Message,
        Message.wa_message_id == "wamid.DUP",
        Message.direction == Direction.IN,
    )
    assert len(con_wamid) == 1
    assert sum(1 for r in resultados.values() if r) == 1, "el cliente recibiría 2 respuestas"
    # Lo único que puede escapar es el choque del índice, que receive_webhook
    # ya traga sin devolver 500.
    assert all(isinstance(exc, IntegrityError) for _, exc in errores), errores


def test_confirmacion_duplicada_con_el_mismo_wamid_no_duplica_el_ticket(
    engine_produccion, catalogo_tibio
):
    # REGLA: la idempotencia protege el efecto de negocio, no solo la bitácora.
    # Un reintento del "sí" no puede abrir dos folios del mismo trámite.
    _sembrar_cliente(engine_produccion)
    with Session(engine_produccion) as s:
        handle_incoming(s, PHONE, "necesito acta de nacimiento", wa_message_id="wamid.SETUP")
        assert s.get(Conversation, PHONE).state == "confirmar"

    def _confirmar(_i):
        with Session(engine_produccion) as s:
            return handle_incoming(s, PHONE, "si", wa_message_id="wamid.SI")

    en_paralelo(_confirmar, 2)

    tickets = _filas(engine_produccion, Ticket)
    assert len(tickets) == 1
    assert tickets[0].service_name == "Acta de nacimiento"


def test_reintento_secuencial_de_meta_no_duplica_salidas_ni_avanza_el_estado(
    engine_produccion, catalogo_tibio
):
    # REGLA: el reintento secuencial (camino feliz de Meta) es un no-op TOTAL:
    # ni segunda respuesta, ni filas OUT extra, ni avance de la máquina.
    _sembrar_cliente(engine_produccion)
    with Session(engine_produccion) as s:
        primeras = handle_incoming(
            s, PHONE, "necesito acta de nacimiento", wa_message_id="wamid.R1"
        )
        assert primeras != []
        conv = s.get(Conversation, PHONE)
        estado_antes, contexto_antes = conv.state, conv.context_json
        salidas_antes = len(_salientes(engine_produccion, PHONE))

        segundas = handle_incoming(
            s, PHONE, "necesito acta de nacimiento", wa_message_id="wamid.R1"
        )

        assert segundas == []
        s.expire_all()
        conv = s.get(Conversation, PHONE)
        assert (conv.state, conv.context_json) == (estado_antes, contexto_antes)

    entrantes = [m for m in _entrantes(engine_produccion, PHONE) if m.wa_message_id == "wamid.R1"]
    assert len(entrantes) == 1
    assert len(_salientes(engine_produccion, PHONE)) == salidas_antes


def test_dos_post_identicos_a_la_ruta_webhook_responden_200_y_entregan_una_sola_vez(
    engine_produccion, catalogo_tibio, app_webhook, whatsapp_falso
):
    # REGLA end-to-end: el webhook nunca devuelve 500 ante duplicados
    # concurrentes y el cliente recibe el mensaje UNA sola vez (no dos
    # WhatsApp iguales por un reintento de Meta).
    _sembrar_cliente(engine_produccion)
    cuerpo = _payload_texto(PHONE, "wamid.HTTP", "necesito acta de nacimiento")
    cabeceras = _firmar(cuerpo)

    def _postear(_i):
        with TestClient(app_webhook) as cliente:
            return cliente.post("/webhook", content=cuerpo, headers=cabeceras).status_code

    resultados, errores = en_paralelo(_postear, 2)

    assert errores == []
    assert sorted(resultados.values()) == [200, 200]
    assert len(whatsapp_falso) == 1, f"el cliente recibió {len(whatsapp_falso)} mensajes"
    assert len(_entrantes(engine_produccion, PHONE)) == 1


# --------------------------------------------------------------------------
# Alta concurrente de cliente nuevo (read-then-write sin upsert)
# --------------------------------------------------------------------------


def test_la_carrera_de_alta_no_desincroniza_respuesta_bitacora_y_estado(
    engine_produccion, catalogo_tibio
):
    # REGLA: bajo la carrera del alta de un cliente nuevo, las tres cosas que
    # ve el negocio tienen que contar la MISMA historia: lo que se le contestó
    # al cliente, lo que quedó en la bitácora y el estado guardado. Un hilo que
    # muere a media conversación no puede dejar una respuesta sin registrar, un
    # registro sin respuesta, ni una Conversation en un estado que no
    # corresponde al último mensaje enviado — si el bot pide confirmar y el
    # estado no quedó en "confirmar", el "sí" del cliente se lee mal y se abre
    # el folio equivocado (o ninguno).
    #
    # Lo que esta prueba NO assertea, a propósito: que exista UN solo Client y
    # UNA sola Conversation. `Client.phone` y `Conversation.phone` son PRIMARY
    # KEY en app/models.py, así que esa unicidad la garantiza el esquema de
    # SQLite y ninguna mutación de la lógica de app/ podría romperla. El efecto
    # observable de esta carrera —que el mensaje del hilo perdedor se pierde—
    # lo mide la prueba hermana
    # test_primer_mensaje_de_un_cliente_nuevo_no_se_pierde_por_la_carrera_de_alta.
    barrera = threading.Barrier(2)

    def _procesar(i):
        with SesionConBarrera(engine_produccion, Client, barrera) as s:
            return handle_incoming(
                s, PHONE, "necesito acta de nacimiento", wa_message_id=f"wamid.N{i}"
            )

    resultados, errores = en_paralelo(_procesar, 2)

    # Hoy gana un hilo y el otro choca contra la PK; el día que se arregle el
    # get-or-create ganarán los dos. Todo lo de abajo vale en ambos mundos a
    # propósito: la prueba documenta coherencia, no castiga el arreglo.
    assert all(isinstance(exc, IntegrityError) for _, exc in errores), errores
    contestados = {i: r for i, r in resultados.items() if r}
    assert contestados, "ningún hilo llegó a atender al cliente"

    # 1) Bitácora de ENTRADA == mensajes que de verdad se atendieron.
    entrantes = _entrantes(engine_produccion, PHONE)
    assert {m.wa_message_id for m in entrantes} == {f"wamid.N{i}" for i in contestados}

    # 2) Bitácora de SALIDA == respuestas devueltas, ni una de más ni una de
    #    menos (una salida fantasma sería un WhatsApp que el cliente nunca
    #    recibió; una faltante, evidencia perdida para el asesor). Se compara
    #    como multiconjunto: el orden entre hilos no es determinista, el
    #    contenido sí.
    salientes = _salientes(engine_produccion, PHONE)
    assert sorted(m.body for m in salientes) == sorted(
        texto for respuestas in contestados.values() for texto in respuestas
    )
    assert all("Acta de nacimiento" in m.body for m in salientes), [m.body for m in salientes]

    # 3) El estado guardado corresponde a esa respuesta: se pidió confirmar UN
    #    servicio, así que el "sí" siguiente debe encontrar ese folio pendiente.
    conv = _filas(engine_produccion, Conversation)[0]
    assert conv.state == "confirmar"
    assert json.loads(conv.context_json)["pending_service_ids"] == ["sb-acta-nacimiento"]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: get-or-create sin upsert — un hilo revienta con IntegrityError "
    "y su mensaje nunca se registra ni se contesta",
)
def test_primer_mensaje_de_un_cliente_nuevo_no_se_pierde_por_la_carrera_de_alta(
    engine_produccion, catalogo_tibio
):
    # REGLA: ningún mensaje entrante puede perderse. Si dos mensajes DISTINTOS
    # de un cliente NUEVO llegan a la vez, ambos deben quedar en la bitácora.
    barrera = threading.Barrier(2)

    def _procesar(i):
        with SesionConBarrera(engine_produccion, Client, barrera) as s:
            return handle_incoming(
                s, PHONE, f"necesito acta de nacimiento {i}", wa_message_id=f"wamid.P{i}"
            )

    en_paralelo(_procesar, 2)

    assert len(_entrantes(engine_produccion, PHONE)) == 2


# --------------------------------------------------------------------------
# Estado conversacional bajo solape
# --------------------------------------------------------------------------


def test_dos_mensajes_distintos_del_mismo_telefono_no_corrompen_la_conversacion(
    engine_produccion, catalogo_tibio
):
    # REGLA: el estado conversacional queda siempre consistente — state dentro
    # del conjunto conocido y context_json siempre JSON válido — pase lo que
    # pase con el orden de escritura.
    _sembrar_cliente(engine_produccion)
    textos = ["necesito acta de nacimiento", "necesito acta de matrimonio"]

    def _procesar(i):
        with Session(engine_produccion) as s:
            return handle_incoming(s, PHONE, textos[i], wa_message_id=f"wamid.C{i}")

    resultados, errores = en_paralelo(_procesar, 2)

    assert errores == []
    assert all(r != [] for r in resultados.values())
    convs = _filas(engine_produccion, Conversation)
    assert len(convs) == 1
    assert convs[0].state in ESTADOS_CONOCIDOS
    contexto = json.loads(convs[0].context_json)  # revienta si quedó corrupto
    assert isinstance(contexto, dict)
    assert len(_entrantes(engine_produccion, PHONE)) == 2
    # Ninguno de los dos mensajes confirma nada: no debe abrirse ningún folio.
    assert _filas(engine_produccion, Ticket) == []


def test_telefonos_distintos_en_paralelo_quedan_totalmente_aislados(
    engine_produccion, catalogo_tibio
):
    # REGLA: no hay fuga de estado entre clientes — cada teléfono tiene su
    # Conversation y sus mensajes; jamás se cruza una fila.
    telefonos = [f"52155000001{i:02d}" for i in range(6)]

    def _procesar(i):
        with Session(engine_produccion) as s:
            return handle_incoming(
                s, telefonos[i], "necesito acta de nacimiento", wa_message_id=f"wamid.A{i}"
            )

    resultados, errores = en_paralelo(_procesar, len(telefonos))

    assert errores == []
    assert len(resultados) == len(telefonos)
    assert all(r != [] for r in resultados.values())
    assert len(_filas(engine_produccion, Conversation)) == len(telefonos)
    for i, phone in enumerate(telefonos):
        propios = _entrantes(engine_produccion, phone)
        assert [m.wa_message_id for m in propios] == [f"wamid.A{i}"]


# --------------------------------------------------------------------------
# Efectos de negocio duplicados (read-then-write sin candado)
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG: doble confirmación concurrente crea 2 tickets — _handle_confirmar "
    "lee el estado 'confirmar' y crea el folio sin protección",
)
def test_confirmacion_duplicada_concurrente_no_debe_crear_dos_tickets(
    engine_produccion, catalogo_tibio
):
    # REGLA: un doble toque del botón "✅ Sí, confirmar" (dos wamids reales,
    # la idempotencia no aplica) no debe terminar con dos folios idénticos: el
    # asesor trabajaría dos veces el mismo trámite.
    _sembrar_cliente(engine_produccion)
    with Session(engine_produccion) as s:
        handle_incoming(s, PHONE, "necesito acta de nacimiento", wa_message_id="wamid.SETUP2")
        assert s.get(Conversation, PHONE).state == "confirmar"

    barrera = threading.Barrier(2)

    def _confirmar(i):
        with SesionConBarrera(engine_produccion, Ticket, barrera) as s:
            return handle_incoming(s, PHONE, "si", wa_message_id=f"wamid.S{i}")

    en_paralelo(_confirmar, 2)

    assert len(_filas(engine_produccion, Ticket)) == 1


@pytest.mark.xfail(
    strict=True,
    reason="BUG: _escalate es read-then-write sin candado — dos escalaciones "
    "concurrentes abren dos tickets '(escalado)' del mismo cliente",
)
def test_dos_peticiones_de_asesor_concurrentes_no_deben_duplicar_el_escalado(
    engine_produccion, catalogo_tibio
):
    # REGLA: _escalate consulta _latest_open_ticket y solo crea el ticket
    # "(escalado)" si no hay ninguno abierto. Dos mensajes de escalación no
    # deben abrir dos tickets del mismo cliente.
    _sembrar_cliente(engine_produccion)
    textos = ["quiero hablar con un asesor", "necesito una persona real"]
    barrera = threading.Barrier(2)

    def _escalar(i):
        with SesionConBarrera(engine_produccion, Ticket, barrera) as s:
            return handle_incoming(s, PHONE, textos[i], wa_message_id=f"wamid.E{i}")

    en_paralelo(_escalar, 2)

    tickets = _filas(engine_produccion, Ticket)
    assert len(tickets) == 1
    assert tickets[0].service_id == "escalado"


@pytest.mark.xfail(
    strict=True,
    reason="BUG: _record_feedback no protege la unicidad por ticket — dos toques "
    "concurrentes del 👍 dejan dos filas Feedback del mismo folio",
)
def test_feedback_duplicado_concurrente_deja_una_sola_opinion_por_ticket(
    engine_produccion, catalogo_tibio
):
    # REGLA: el docstring del modelo Feedback dice "Una sola opinión por
    # ticket"; dos filas falsean la métrica de satisfacción del panel.
    _sembrar_cliente(engine_produccion, state="menu_categoria")
    with Session(engine_produccion) as s:
        ticket = Ticket(
            phone=PHONE,
            category="Servicios básicos",
            service_id="sb-acta-nacimiento",
            service_name="Acta de nacimiento",
            price_info="A cotizar",
            status=TicketStatus.CERRADO,
        )
        s.add(ticket)
        s.commit()
        s.refresh(ticket)
        ticket_id = ticket.id

    barrera = threading.Barrier(2)

    def _opinar(i):
        with SesionConBarrera(engine_produccion, Feedback, barrera) as s:
            return handle_incoming(s, PHONE, f"fb:{ticket_id}:up", wa_message_id=f"wamid.F{i}")

    en_paralelo(_opinar, 2)

    assert len(_filas(engine_produccion, Feedback)) == 1


@pytest.mark.xfail(
    strict=True,
    reason="BUG: la dedup de _extract_client_data es read-then-write (ClientNote."
    "body.contains) — la misma CURP concurrente deja dos notas idénticas",
)
def test_misma_curp_en_mensajes_concurrentes_no_duplica_la_nota_del_cliente(
    engine_produccion, catalogo_tibio
):
    # REGLA: la ficha del cliente no debe llenarse de ruido — el mismo dato
    # detectado dos veces se anota una sola vez.
    _sembrar_cliente(engine_produccion, state="menu_categoria")
    barrera = threading.Barrier(2)

    def _mandar_curp(i):
        with SesionConBarrera(engine_produccion, ClientNote, barrera) as s:
            return handle_incoming(
                s, PHONE, "mi curp es MAHJ850315HDFRRN09", wa_message_id=f"wamid.K{i}"
            )

    en_paralelo(_mandar_curp, 2)

    notas = _filas(engine_produccion, ClientNote)
    assert len(notas) == 1, [n.body for n in notas]


# --------------------------------------------------------------------------
# Silencio del agente (regla de producto load-bearing)
# --------------------------------------------------------------------------


def test_agente_apagado_no_deja_escapar_ninguna_respuesta_bajo_concurrencia(
    engine_produccion, catalogo_tibio
):
    # REGLA: con ticket escalado y agent_active=False el bot guarda silencio
    # TOTAL. Ninguna carrera puede colar un mensaje al cliente mientras el
    # asesor está atendiendo.
    _sembrar_cliente(engine_produccion)
    with Session(engine_produccion) as s:
        handle_incoming(s, PHONE, "quiero hablar con un asesor", wa_message_id="wamid.ESC")
        ticket = s.exec(select(Ticket).where(Ticket.phone == PHONE)).one()
        ticket.assigned_to = "ana"
        ticket.agent_active = False
        s.add(ticket)
        s.commit()
        assert s.get(Conversation, PHONE).state == "escalado"

    salidas_antes = len(_salientes(engine_produccion, PHONE))
    textos = ["menu", "estatus", "necesito acta de nacimiento", "hola"]

    def _mandar(i):
        with Session(engine_produccion) as s:
            return handle_incoming(s, PHONE, textos[i], wa_message_id=f"wamid.M{i}")

    resultados, errores = en_paralelo(_mandar, len(textos))

    assert errores == []
    assert list(resultados.values()) == [[], [], [], []]
    assert len(_salientes(engine_produccion, PHONE)) == salidas_antes


# --------------------------------------------------------------------------
# Frontera del rate limit
# --------------------------------------------------------------------------


def _sembrar_entrantes(engine, phone: str, cuantos: int, antiguedad_segundos: int = 0) -> None:
    creado = utcnow() - timedelta(seconds=antiguedad_segundos)
    with Session(engine) as s:
        for _ in range(cuantos):
            s.add(Message(phone=phone, direction=Direction.IN, body="x", created_at=creado))
        s.commit()


def test_rate_limit_frontera_exacta_veinte_pasa_veintiuno_se_bloquea(
    engine_produccion, catalogo_tibio
):
    # REGLA: _is_rate_limited usa `len(recent) > RATE_LIMIT_MAX_MESSAGES` y el
    # mensaje actual YA está logueado al evaluarlo → el 20 de la ventana pasa
    # y el 21 se bloquea. Bajarlo por accidente silenciaría clientes legítimos.
    #
    # El 19 va como LITERAL a propósito: sembrar con `RATE_LIMIT_MAX_MESSAGES - 1`
    # movía la siembra junto con la constante, así que cambiar el límite pasaba
    # en verde. El 20 es una decisión de producto, no un detalle interno: se
    # ancla, no se deriva.
    _sembrar_cliente(engine_produccion)
    _sembrar_entrantes(engine_produccion, PHONE, 19)

    with Session(engine_produccion) as s:
        veinte = handle_incoming(s, PHONE, "necesito acta de nacimiento", wa_message_id="wamid.20")
        veintiuno = handle_incoming(s, PHONE, "necesito acta de matrimonio", wa_message_id="wamid.21")
        veintidos = handle_incoming(s, PHONE, "necesito acta de divorcio", wa_message_id="wamid.22")

    assert veinte != []
    assert veintiuno == []
    assert veintidos == []


def test_mensaje_bloqueado_por_rate_limit_queda_registrado_en_la_bitacora(
    engine_produccion, catalogo_tibio
):
    # REGLA: aunque el bot no conteste, el mensaje del cliente NUNCA se pierde
    # — _log_message corre ANTES del chequeo de rate limit. Sin esto se
    # perdería la evidencia de lo que el cliente escribió.
    _sembrar_cliente(engine_produccion)
    _sembrar_entrantes(engine_produccion, PHONE, RATE_LIMIT_MAX_MESSAGES - 1)
    bloqueado = "necesito acta de matrimonio"

    with Session(engine_produccion) as s:
        handle_incoming(s, PHONE, "necesito acta de nacimiento", wa_message_id="wamid.b20")
        assert handle_incoming(s, PHONE, bloqueado, wa_message_id="wamid.b21") == []
        assert handle_incoming(s, PHONE, "necesito acta de divorcio", wa_message_id="wamid.b22") == []

    entrantes = _entrantes(engine_produccion, PHONE)
    assert len(entrantes) == RATE_LIMIT_MAX_MESSAGES + 2
    cuerpos = [m.body for m in entrantes]
    assert bloqueado in cuerpos
    assert "necesito acta de divorcio" in cuerpos
    # El único mensaje contestado fue el 20: una sola respuesta en la bitácora.
    assert len(_salientes(engine_produccion, PHONE)) == 1


def test_rate_limit_solo_cuenta_entrantes_del_mismo_telefono_dentro_de_la_ventana(
    engine_produccion, catalogo_tibio
):
    # REGLA: el contador filtra por direction=IN, por phone y por created_at
    # dentro de la ventana. Un falso positivo aquí deja mudo a un cliente que
    # no hizo nada malo.
    exceso = RATE_LIMIT_MAX_MESSAGES * 2

    # (a) Muchas SALIDAS del mismo teléfono no cuentan.
    phone_a = "5215500002001"
    _sembrar_cliente(engine_produccion, phone_a)
    with Session(engine_produccion) as s:
        for _ in range(exceso):
            s.add(Message(phone=phone_a, direction=Direction.OUT, body="x"))
        s.commit()
        assert handle_incoming(s, phone_a, "necesito acta de nacimiento") != []

    # (b) Muchas entradas de OTRO teléfono no cuentan.
    phone_b = "5215500002002"
    _sembrar_cliente(engine_produccion, phone_b)
    _sembrar_entrantes(engine_produccion, "5215500002999", exceso)
    with Session(engine_produccion) as s:
        assert handle_incoming(s, phone_b, "necesito acta de nacimiento") != []

    # (c) Entradas propias pero FUERA de la ventana de 60 s no cuentan.
    phone_c = "5215500002003"
    _sembrar_cliente(engine_produccion, phone_c)
    _sembrar_entrantes(
        engine_produccion, phone_c, exceso, antiguedad_segundos=RATE_LIMIT_WINDOW_SECONDS * 2
    )
    with Session(engine_produccion) as s:
        assert handle_incoming(s, phone_c, "necesito acta de nacimiento") != []


# --------------------------------------------------------------------------
# SQLite bajo carga (WAL + busy_timeout)
# --------------------------------------------------------------------------


def test_el_engine_aplica_wal_y_busy_timeout(engine_produccion):
    # REGLA: la configuración que sostiene la prueba de carga (journal_mode=WAL
    # y busy_timeout > 0) es parte del contrato de db._set_sqlite_pragmas, no
    # un accidente del driver.
    with engine_produccion.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() > 0


def test_escrituras_concurrentes_no_lanzan_database_is_locked(engine_produccion, catalogo_tibio):
    # REGLA: SQLite en WAL con busy_timeout no debe filtrar
    # OperationalError("database is locked") hacia el request path — un lock
    # perdido es un cliente sin respuesta.
    telefonos = [f"52155000030{i:02d}" for i in range(12)]
    guion = [
        "hola",
        "necesito acta de nacimiento",
        "si",
        "menu",
        "quiero hablar con un asesor",
        "gracias",
    ]

    def _conversar(i):
        phone = telefonos[i]
        with Session(engine_produccion) as s:
            for j, texto in enumerate(guion):
                handle_incoming(s, phone, texto, wa_message_id=f"wamid.L{i}.{j}")
        return phone

    resultados, errores = en_paralelo(_conversar, len(telefonos))

    assert errores == [], [repr(e) for _, e in errores]
    assert len(resultados) == len(telefonos)
    entrantes = _entrantes(engine_produccion)
    esperados = len(telefonos) * len(guion)
    assert len(entrantes) == esperados
    assert len({m.wa_message_id for m in entrantes}) == esperados


# --------------------------------------------------------------------------
# El "# ponytail:" de _tag_sent_message
# --------------------------------------------------------------------------


def test_tag_sent_message_respeta_el_orden_fifo_documentado(engine_produccion):
    # CARACTERIZACIÓN del "# ponytail:", no un contrato deseado: con varias
    # filas OUT sin taggear, el wamid va a la MÁS VIEJA. Documenta el heurístico
    # frágil para que un cambio accidental del order_by se note.
    #
    # OJO: si alguien ARREGLA _tag_sent_message de verdad (casar el wamid contra
    # el mensaje realmente enviado en vez de "la fila OUT más vieja"), esta
    # prueba va a fallar y NO es una regresión: BÓRRALA junto con el fix.
    with Session(engine_produccion) as s:
        s.add(Message(phone=PHONE, direction=Direction.OUT, body="A"))
        s.commit()
        s.add(Message(phone=PHONE, direction=Direction.OUT, body="B"))
        s.commit()

    _tag_sent_message(PHONE, "wamid.1")
    _tag_sent_message(PHONE, "wamid.2")

    salidas = sorted(_salientes(engine_produccion, PHONE), key=lambda m: m.id)
    assert [(m.body, m.wa_message_id) for m in salidas] == [("A", "wamid.1"), ("B", "wamid.2")]


@pytest.mark.xfail(
    strict=True,
    reason="BUG: la consulta de _tag_sent_message no filtra por Message.direction "
    "— un wamid de SALIDA aterriza en la fila de ENTRADA del cliente",
)
def test_tag_sent_message_nunca_debe_taggear_una_fila_entrante(engine_produccion):
    # REGLA: un wamid nuestro jamás debe escribirse sobre un mensaje del
    # cliente; si pasa, _process_status marcaría el mensaje entrante como
    # "delivered/read" propio y la bandeja mostraría datos falsos.
    with Session(engine_produccion) as s:
        s.add(Message(phone=PHONE, direction=Direction.IN, body="hola", wa_message_id=None))
        s.commit()
        s.add(Message(phone=PHONE, direction=Direction.OUT, body="respuesta"))
        s.commit()

    _tag_sent_message(PHONE, "wamid.OUT1")

    entrante = _entrantes(engine_produccion, PHONE)[0]
    saliente = _salientes(engine_produccion, PHONE)[0]
    assert entrante.wa_message_id is None
    assert saliente.wa_message_id == "wamid.OUT1"


@pytest.mark.xfail(
    strict=True,
    reason="BUG: lost update en _tag_sent_message — dos taggeos concurrentes "
    "seleccionan la misma fila más vieja y un wamid se pierde para siempre",
)
def test_dos_envios_concurrentes_al_mismo_telefono_no_deben_perder_un_wamid(
    engine_produccion, monkeypatch
):
    # REGLA: cada mensaje enviado debe quedar con su propio wamid; si dos
    # taggeos concurrentes eligen la misma fila, esa otra fila OUT nunca podrá
    # recibir su estado de entrega (_process_status no la encuentra).
    with Session(engine_produccion) as s:
        s.add(Message(phone=PHONE, direction=Direction.OUT, body="A"))
        s.commit()
        s.add(Message(phone=PHONE, direction=Direction.OUT, body="B"))
        s.commit()

    barrera = threading.Barrier(2)
    monkeypatch.setattr(
        webhook, "get_session", lambda: SesionConBarrera(db.engine, Message, barrera)
    )

    def _taggear(i):
        _tag_sent_message(PHONE, f"wamid.T{i}")

    _, errores = en_paralelo(_taggear, 2)

    assert errores == [], [repr(e) for _, e in errores]
    salidas = _salientes(engine_produccion, PHONE)
    asignados = [m.wa_message_id for m in salidas]
    assert None not in asignados, "una fila OUT se quedó sin wamid"
    assert sorted(asignados) == ["wamid.T0", "wamid.T1"]


# --------------------------------------------------------------------------
# El singleton `catalog` (el bug de mayor impacto de producto)
# --------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="BUG: Catalog._refresh no es thread-safe — reconstruye la lista "
    "compartida self._categories sin candado y queda con ids duplicados",
)
def test_catalogo_no_se_corrompe_con_refrescos_concurrentes(engine_produccion, catalogo_tibio):
    # REGLA: category_by_index / service_by_index son POSICIONALES. Una lista
    # corrupta hace que el número que tecleó el cliente apunte a otra
    # categoría → el bot confirmaría el trámite equivocado, que es lo peor que
    # puede pasar según la regla de oro del producto.
    barrera = threading.Barrier(2)
    local = threading.local()
    CategoryReal = catalog_mod.Category

    def _category_con_barrera(*args, **kwargs):
        # Solo la PRIMERA categoría que construye cada hilo se detiene: eso
        # basta para que ambos hilos ya hayan hecho `self._categories = []`.
        if not getattr(local, "espero", False):
            local.espero = True
            _esperar(barrera)
        return CategoryReal(*args, **kwargs)

    catalog_mod.Category = _category_con_barrera
    try:
        resultados, errores = en_paralelo(lambda _i: len(catalog.categories), 2)
    finally:
        catalog_mod.Category = CategoryReal

    assert errores == []
    ids = [c.id for c in catalog._categories]
    assert len(ids) == len(set(ids)), f"categorías duplicadas en el singleton: {len(ids)}"
    assert set(resultados.values()) == {9}


@pytest.mark.xfail(
    strict=True,
    reason="BUG: la siembra inicial del catálogo es read-then-write sin candado "
    "— en un arranque limpio varios hilos importan el JSON y chocan contra la PK",
)
def test_siembra_inicial_del_catalogo_concurrente_no_debe_reventar(engine_produccion):
    # REGLA: en un arranque limpio (volumen /data nuevo) las tablas del
    # catálogo están vacías; los primeros requests concurrentes no deben morir
    # por importar la semilla dos veces. Es el primer minuto de vida de un
    # despliegue.
    barrera = threading.Barrier(4)
    importar_real = catalog_mod._import_into

    def _import_con_barrera(session, data):
        # Todos los hilos ya pasaron el "¿está vacío?" antes de que alguno
        # escriba: así la carrera es determinista en vez de depender del
        # scheduler.
        _esperar(barrera)
        return importar_real(session, data)

    catalog_mod._import_into = _import_con_barrera
    try:
        resultados, errores = en_paralelo(lambda _i: len(catalog.categories), 4)
    finally:
        catalog_mod._import_into = importar_real

    assert errores == [], [repr(e) for _, e in errores]
    assert set(resultados.values()) == {9}
    # La base sí queda consistente aunque los requests mueran.
    assert len(_filas(engine_produccion, catalog_mod.CatalogCategory)) == 9


# --------------------------------------------------------------------------
# Guardia de configuración de las pruebas
# --------------------------------------------------------------------------


def test_el_indice_unico_de_idempotencia_existe_en_el_engine_de_produccion(engine_produccion):
    # REGLA: init_db() crea ux_message_wa_message_id. Es la única red real
    # contra webhooks duplicados concurrentes; si desapareciera, la
    # idempotencia dependería solo del chequeo secuencial.
    # row[1] = nombre, row[2] = bandera de unicidad. Mirar solo el nombre dejaba
    # pasar una degradación a CREATE INDEX (sigue llamándose igual), justo la
    # propiedad que esta guardia existe para proteger.
    with engine_produccion.connect() as conn:
        indices = {
            row[1]: row[2] for row in conn.exec_driver_sql("PRAGMA index_list(message)")
        }
    assert "ux_message_wa_message_id" in indices
    assert indices["ux_message_wa_message_id"] == 1, "el índice existe pero dejó de ser UNIQUE"


def test_los_limites_de_producto_son_los_acordados():
    # Ancla de valores, no de coherencia interna. Estas dos cifras las decidió
    # el negocio; derivarlas de la propia constante en cada assert dejaba que
    # un cambio accidental se colara en verde. Si el negocio las cambia a
    # propósito, hay que actualizar ESTA línea a mano — ese es el punto.
    assert (RATE_LIMIT_MAX_MESSAGES, RATE_LIMIT_WINDOW_SECONDS, MAX_FAILED_ATTEMPTS) == (20, 60, 2)


def test_ninguna_prueba_de_este_archivo_puede_salir_a_la_red(catalogo_tibio, monkeypatch):
    # REGLA de la suite: este archivo corre N hilos por prueba. Si la capa LLM
    # se activara, cada corrida haría N llamadas HTTP reales a Anthropic y las
    # pruebas dejarían de ser deterministas (y de ser gratis).
    #
    # (a) Camino normal: la deducción se resuelve con el catálogo local, capa
    #     literal por nombre. Mide comportamiento de nlu, no el valor que el
    #     propio fixture acaba de escribir.
    assert nlu.deduce_explained("necesito acta de nacimiento")[2] == "nombre"

    # (b) La guardia MUERDE: con una API key viva la cascada intenta usar el
    #     cliente y el doble revienta con una BaseException — justo lo que el
    #     `except Exception` de deduce_explained NO puede tragarse. Sin este
    #     detalle, una fuga de red se vería idéntica a un fallback legítimo a
    #     keywords y nadie se enteraría.
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-de-mentiras")
    with pytest.raises(RedProhibida):
        nlu.deduce_explained("necesito acta de nacimiento")
