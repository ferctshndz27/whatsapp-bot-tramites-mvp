"""Fase 2 — el medidor de mensajería: qué salió, qué de eso cobra Meta y qué no sabemos.

Esta fase existe por una fecha. Meta lo publica en su página de precios de
mensajes que no son plantilla (consultada el 2026-08-24):

    «Effective October 1, 2026, Meta will charge on a per-message basis for
    service messages»

…y ese mismo día empiezan a cobrarse también las plantillas de UTILIDAD
respondidas dentro de la ventana de 24 h abierta. Hoy casi todo el saliente de
este bot son mensajes de servicio gratis dentro de la ventana; el 1-oct pasan a
costar uno por uno. La tabla de Meta marca la gratuidad de hoy como
«Free until October 1, 2026».

Qué protege este archivo, regla por regla:

- **El precio no se deduce, se copia.** El veredicto de cobro llega en el
  webhook de estado (`pricing.category` / `pricing.type`) y se sella tal cual en
  la fila del `Message`. Por eso el 1-oct no hay que tocar código: Meta empezará
  a mandar `type='regular'` donde hoy manda `free_customer_service` y el conteo
  de cobrables sube solo.
- **El veredicto no se pisa.** Meta manda `pricing` en `sent` y en UNO de
  delivered/read, nunca en los tres. Si el status posterior lo sobreescribiera
  con NULL, el mensaje pasaría a contarse como «sin dato» y la factura estimada
  saldría baja. Es el caso crítico de la fase.
- **Un total que se lee completo sin estarlo es peor que no tenerlo.** Todo
  resumen trae `sin_dato` al lado: Twilio nunca reporta precio y un wamid que no
  empareja deja su mensaje mudo.
- **Sin tarifa NO es gratis.** Una categoría que el rate card no lista vale
  «desconocido», no 0.0, y el resumen se declara incompleto nombrándola.
- **La tarjeta se elige por fecha de entrega**, no «la última publicada»:
  corregir una tarifa de hoy no puede reescribir lo que costó el trimestre
  pasado.
- **Las tres columnas de pricing llegan al espejo MSSQL por UPDATE**, porque el
  status siempre es posterior al INSERT del mensaje.
- **La ventana de 24 h** (`app/ventana.py`) tiene una sola definición y sus
  bordes son estrictos.

Esqueleto prestado de tests/test_webhook.py (monkeypatch de `db.engine` para que
`get_session()` caiga en la base del test) y de tests/test_archive.py (DDL local
del espejo). El DDL se copia en vez de importarse: `tests/` no es paquete.
"""

from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlmodel import select

import app.archive as archive
import app.db as db
import app.medidor as medidor
import app.ventana as ventana
from app.config import settings
from app.models import Direction, Message
from app.webhook import _process_status

# El espejo MSSQL de estas pruebas es SQLite: archive.py le pasa datetimes
# crudos por text(), que en producción recibe pymssql sin aviso, y aquí caen en
# el adaptador por defecto de sqlite3 (obsoleto desde Python 3.12). Es del
# doble, no del código: se calla solo en los archivos que usan ese doble.
pytestmark = pytest.mark.filterwarnings(
    "ignore:The default datetime adapter is deprecated:DeprecationWarning"
)

TELEFONO = "5215500001234"

# Periodos de reporte fijos: nada aquí depende de qué día se corran las pruebas.
AGOSTO_2026 = (
    datetime(2026, 8, 1, tzinfo=timezone.utc),
    datetime(2026, 9, 1, tzinfo=timezone.utc),
)
OCTUBRE_2026 = (
    datetime(2026, 10, 1, tzinfo=timezone.utc),
    datetime(2026, 11, 1, tzinfo=timezone.utc),
)
DIA_DE_AGOSTO = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
DIA_DE_OCTUBRE = datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc)

# Rate card sintético. Las pruebas NO se atan a las cifras reales de
# app/rate_card.json: ese archivo es dato editable —Meta puede moverlo hasta
# cuatro veces al año— y un test clavado en 0.0099 se rompería en el próximo
# ajuste de tarifas sin que nada esté mal. Lo que sí se prueba es la MECÁNICA:
# elegir tarjeta por fecha, y distinguir «sin tarifa» de «gratis».
RATE_CARD_DE_PRUEBA = {
    "moneda": "USD",
    "unidad": "por mensaje entregado",
    "tarjetas": [
        {
            "vigencia_desde": "2025-07-01",
            "pais": "MX",
            "confianza": "de_prueba",
            "fuente": "sintética",
            "tarifas": {"utility": 0.01, "marketing": 0.05},
        },
        {
            "vigencia_desde": "2026-10-01",
            "pais": "MX",
            "confianza": "de_prueba",
            "fuente": "sintética",
            # `service` presente pero en null: la forma exacta que hoy tiene el
            # rate card del 1-oct-2026, porque Meta no publica esa tarifa sino
            # hasta el 1-sep-2026 a más tardar.
            "tarifas": {"utility": 0.02, "marketing": 0.06, "service": None},
        },
    ],
}

# Espejo MSSQL: solo las dos tablas que toca `archive_wa_message`. `wa_clients`
# no es opcional — el archivado hace `_upsert_client()` antes de escribir el
# mensaje, así que sin ella el INSERT muere por «no such table» y la prueba de
# pricing nunca llegaría a ejecutarse.
DDL_ESPEJO = [
    """CREATE TABLE wa_clients (
        phone TEXT PRIMARY KEY, name TEXT,
        habilitado INTEGER DEFAULT 1, created_at TIMESTAMP,
        email TEXT, origen TEXT, idioma TEXT DEFAULT 'es-MX',
        opt_in_at TIMESTAMP, opt_in_source TEXT, opt_out_at TIMESTAMP,
        atributos_json TEXT, last_activity_at TIMESTAMP,
        notas_internas TEXT)""",
    """CREATE TABLE wa_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_id INTEGER UNIQUE,
        phone TEXT REFERENCES wa_clients(phone), direction TEXT, body TEXT,
        wa_message_id TEXT, wa_status TEXT, media_path TEXT, sent_by TEXT,
        habilitado INTEGER DEFAULT 1, provider TEXT DEFAULT 'meta',
        pricing_category TEXT, pricing_type TEXT, pricing_billable INTEGER,
        media_ref TEXT, media_mime TEXT, media_nombre TEXT, media_bytes INTEGER,
        media_vence_at TIMESTAMP, ticket_id INTEGER,
        created_at TIMESTAMP)""",
]


@pytest.fixture()
def sin_espejo(monkeypatch):
    """Apaga el espejo MSSQL para las pruebas que solo miran SQLite.

    Sin esto, cada `commit()` dispara los listeners `after_insert` y `_safe()`
    intenta abrir MSSQL: no rompe nada (degrada a warning por diseño) pero
    ensucia la salida y cuesta segundos. No es autouse a propósito: la fixture
    `espejo` necesita justo lo contrario y no deben pelearse.
    """
    monkeypatch.setattr(settings, "mssql_host", "")


@pytest.fixture()
def espejo(monkeypatch):
    """Espejo MSSQL simulado con un SQLite en memoria (igual que test_archive)."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        for ddl in DDL_ESPEJO:
            conn.execute(text(ddl))
    monkeypatch.setattr(archive, "_engine", engine)
    monkeypatch.setattr(archive, "_ready", True)
    monkeypatch.setattr(settings, "mssql_host", "test")
    monkeypatch.setattr(settings, "mssql_password", "test")
    yield engine
    engine.dispose()


def _status_meta(wamid: str, estado: str, pricing: dict | None = None) -> dict:
    """Un status tal como viaja en `entry[].changes[].value.statuses[]` de Meta.

    `pricing` se OMITE del payload cuando no se pasa, que es como llega de
    verdad: Meta no manda la llave en vacío, simplemente no la manda.
    """
    status = {
        "id": wamid,
        "status": estado,
        "timestamp": "1755864000",
        "recipient_id": TELEFONO,
        "conversation": {"id": "conv.HBgL", "origin": {"type": "service"}},
    }
    if pricing is not None:
        status["pricing"] = pricing
    return status


def _guardar(session, wamid: str, *, cuando: datetime = DIA_DE_AGOSTO) -> None:
    """Un saliente ya enviado y con su wamid pegado, esperando veredicto."""
    session.add(
        Message(
            phone=TELEFONO,
            direction=Direction.OUT,
            body="Listo, tu folio quedó abierto. Un asesor te confirma el precio.",
            created_at=cuando,
            wa_message_id=wamid,
        )
    )
    session.commit()


def _mensaje(
    session,
    direccion: Direction,
    *,
    categoria: str | None = None,
    tipo: str | None = None,
    cuando: datetime = DIA_DE_AGOSTO,
) -> None:
    """Un mensaje ya con (o sin) veredicto de cobro, para alimentar al medidor."""
    session.add(
        Message(
            phone=TELEFONO,
            direction=direccion,
            body="…",
            created_at=cuando,
            pricing_category=categoria,
            pricing_type=tipo,
        )
    )
    session.commit()


# ---------------------------------------------------------------------------
# El veredicto de cobro: lo dicta Meta, no lo deduce el bot
# ---------------------------------------------------------------------------


def test_el_status_sella_el_veredicto_de_cobro_tal_como_lo_manda_meta(
    monkeypatch, session, sin_espejo
):
    """El bot no calcula lo que cuesta un mensaje: copia lo que Meta reportó.

    Categoría, tipo y `billable` se guardan sin interpretar. Es lo que hace que
    el cambio de precios del 1-oct-2026 no requiera tocar una línea de lógica.
    """
    monkeypatch.setattr(db, "engine", session.get_bind())
    _guardar(session, "wamid.SELLADO")

    # Lo que Meta manda HOY por un mensaje libre dentro de la ventana de 24 h.
    _process_status(
        _status_meta(
            "wamid.SELLADO",
            "sent",
            {
                "billable": False,
                "pricing_model": "PMP",
                "category": "service",
                "type": "free_customer_service",
            },
        )
    )

    session.expire_all()
    fila = session.exec(select(Message).where(Message.wa_message_id == "wamid.SELLADO")).one()
    assert fila.wa_status == "sent"
    assert fila.pricing_category == "service"
    assert fila.pricing_type == "free_customer_service"
    assert fila.pricing_billable is False


def test_un_status_posterior_sin_pricing_no_borra_el_veredicto_ya_guardado(
    monkeypatch, session, sin_espejo
):
    """EL CASO CRÍTICO DE LA FASE.

    Meta manda `pricing` en `sent` y en UNO de delivered/read — nunca en los
    tres. Por eso el veredicto se FUSIONA en vez de asignarse: si el status que
    llega después lo pisara con NULL, ese mensaje pasaría a contarse como «sin
    dato» y la factura estimada saldría baja justo en los mensajes que sí se
    cobraron.
    """
    monkeypatch.setattr(db, "engine", session.get_bind())
    _guardar(session, "wamid.PAREJA")

    _process_status(
        _status_meta(
            "wamid.PAREJA",
            "sent",
            {
                "billable": True,
                "pricing_model": "PMP",
                "category": "utility",
                "type": "regular",
            },
        )
    )
    # El segundo status llega SIN la llave `pricing`, como en producción.
    _process_status(_status_meta("wamid.PAREJA", "read"))

    session.expire_all()
    fila = session.exec(select(Message).where(Message.wa_message_id == "wamid.PAREJA")).one()
    # Este assert es el que prueba que sí se releyó la fila desde la base: si el
    # estado avanzó a `read`, lo de abajo no puede venir de la caché de la sesión.
    assert fila.wa_status == "read", "la fila no se releyó; los asserts de abajo no probarían nada"
    assert fila.pricing_category == "utility"
    assert fila.pricing_type == "regular"
    assert fila.pricing_billable is True

    # Y la consecuencia que importa: el medidor lo sigue viendo como cobrable.
    reporte = medidor.resumen(session, *AGOSTO_2026)
    assert reporte["cobrables"] == 1
    assert reporte["sin_dato"] == 0


def test_un_pricing_parcial_no_borra_los_campos_que_no_trae(monkeypatch, session, sin_espejo):
    """La fusión, campo por campo — no solo la guarda `if pricing:`.

    El test de arriba manda el segundo status SIN la llave `pricing`, así que
    nunca entra al cuerpo del `if` y no ejercita los `or row.pricing_*`. Este sí:
    manda un `pricing` PRESENTE pero incompleto, que es lo que deja una fila
    asimétrica (categoría sin tipo). Sin la fusión campo por campo, `category` y
    `type` se irían a NULL y el mensaje pasaría de «cobrable» a «sin dato».
    """
    monkeypatch.setattr(db, "engine", session.get_bind())
    _guardar(session, "wamid.PARCIAL")

    _process_status(
        _status_meta(
            "wamid.PARCIAL",
            "sent",
            {"billable": True, "pricing_model": "PMP", "category": "utility", "type": "regular"},
        )
    )
    # Segundo status CON pricing, pero sin categoría ni tipo.
    _process_status(
        _status_meta("wamid.PARCIAL", "delivered", {"billable": True, "pricing_model": "PMP"})
    )

    session.expire_all()
    fila = session.exec(select(Message).where(Message.wa_message_id == "wamid.PARCIAL")).one()
    assert fila.wa_status == "delivered", "la fila no se releyó"
    assert fila.pricing_category == "utility", "la fusión perdió la categoría"
    assert fila.pricing_type == "regular", "la fusión perdió el tipo"

    reporte = medidor.resumen(session, *AGOSTO_2026)
    assert reporte["cobrables"] == 1
    assert reporte["sin_dato"] == 0


def test_un_pricing_vacio_tampoco_borra_el_veredicto(monkeypatch, session, sin_espejo):
    """`pricing: {}` es falsy: ni siquiera entra al `if`. Se fija el
    comportamiento para que nadie lo cambie por un `if pricing is not None`."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _guardar(session, "wamid.VACIO")

    _process_status(
        _status_meta("wamid.VACIO", "sent", {"category": "marketing", "type": "regular"})
    )
    _process_status(_status_meta("wamid.VACIO", "read", {}))

    session.expire_all()
    fila = session.exec(select(Message).where(Message.wa_message_id == "wamid.VACIO")).one()
    assert fila.wa_status == "read", "la fila no se releyó"
    assert fila.pricing_category == "marketing"
    assert fila.pricing_type == "regular"


def test_el_resumen_y_la_grafica_nunca_se_contradicen(monkeypatch, session, sin_espejo):
    """La plantilla afirma por escrito que la suma de la gráfica cuadra con las
    tarjetas de arriba. Una fila con categoría pero SIN tipo las hacía divergir:
    el resumen la contaba como «gratis» (subestimando la factura) y la serie
    diaria como «sin dato». El veredicto es el TIPO, y hay una sola definición.
    """
    monkeypatch.setattr(db, "engine", session.get_bind())
    ayer = AGOSTO_2026[0]
    session.add(
        Message(
            phone=TELEFONO,
            direction=Direction.OUT,
            body="categoría sin tipo",
            created_at=ayer,
            pricing_category="service",
        )
    )
    session.commit()

    reporte = medidor.resumen(session, *AGOSTO_2026)
    serie = medidor.serie_diaria(session, *AGOSTO_2026)

    assert reporte["sin_dato"] == sum(d["sin_dato"] for d in serie)
    assert reporte["salientes"] == sum(d["salientes"] for d in serie)
    assert reporte["cobrables"] == sum(d["cobrables"] for d in serie)
    assert reporte["gratis"] == 0, "una fila sin tipo no es gratis: es desconocida"


def test_un_status_de_un_wamid_desconocido_ni_revienta_ni_inventa_filas(
    monkeypatch, session, sin_espejo
):
    """Llegan estados de mensajes que no tienen fila local: el emparejamiento
    FIFO de wamid cruza ids entre canales y hay salientes que no dejaron fila.

    Eso se registra y se ignora. Nunca crea una fila fantasma —que el medidor
    contaría como saliente— ni le pega el veredicto ajeno a otro mensaje.
    """
    monkeypatch.setattr(db, "engine", session.get_bind())
    _guardar(session, "wamid.MIO")

    _process_status(
        _status_meta(
            "wamid.DE.NADIE",
            "delivered",
            {"billable": True, "pricing_model": "PMP", "category": "marketing",
             "type": "regular"},
        )
    )

    session.expire_all()
    filas = session.exec(select(Message)).all()
    assert len(filas) == 1, "un estado huérfano creó una fila que nadie envió"
    assert filas[0].wa_message_id == "wamid.MIO"
    assert filas[0].pricing_type is None, "el veredicto ajeno se pegó al mensaje equivocado"


# ---------------------------------------------------------------------------
# El resumen: cobrable, gratis y —sobre todo— «no sabemos»
# ---------------------------------------------------------------------------


def test_el_resumen_separa_lo_cobrado_de_lo_gratis_y_de_lo_que_no_sabemos(session, sin_espejo):
    """Tres poblaciones que no se pueden confundir.

    `regular` es lo único que Meta cobra; `free_customer_service` es gratis; y un
    saliente sin veredicto (Twilio no reporta precio, o el wamid nunca emparejó)
    no es ninguna de las dos: es un hueco, y va contado aparte.

    Los ENTRANTES nunca cuentan: Meta cobra la ENTREGA, y lo que entra no se
    entrega, se recibe. El entrante de esta prueba trae veredicto `regular` a
    propósito — si el filtro de dirección se cayera, inflaría la factura.
    """
    _mensaje(session, Direction.OUT, categoria="utility", tipo="regular")
    _mensaje(session, Direction.OUT, categoria="service", tipo="free_customer_service")
    _mensaje(session, Direction.OUT)
    _mensaje(session, Direction.IN, categoria="utility", tipo="regular")

    reporte = medidor.resumen(session, *AGOSTO_2026)

    assert reporte["salientes"] == 3, "un entrante se coló al conteo de salientes"
    assert reporte["con_veredicto"] == 2
    assert reporte["cobrables"] == 1
    assert reporte["gratis"] == 1
    assert reporte["sin_dato"] == 1
    assert reporte["por_categoria"]["utility"]["cobrables"] == 1
    assert reporte["por_categoria"]["service"]["total"] == 1
    assert reporte["por_categoria"]["service"]["cobrables"] == 0


def test_lo_que_no_sabemos_siempre_cuadra_con_lo_que_salio(session, sin_espejo):
    """Invariante del reporte: `sin_dato` == `salientes` − `con_veredicto`.

    Es el denominador honesto. Sin él, «no cobrado» y «no sabemos» se confunden
    y el total se lee completo sin estarlo — la misma falla que tenía /system
    cuando llamaba «al día» a un espejo inflado.
    """
    vacio = medidor.resumen(session, *AGOSTO_2026)
    assert vacio["salientes"] == 0
    assert vacio["sin_dato"] == vacio["salientes"] - vacio["con_veredicto"] == 0

    for _ in range(3):
        _mensaje(session, Direction.OUT, categoria="marketing", tipo="regular")
    for _ in range(2):
        _mensaje(session, Direction.OUT, categoria="service", tipo="free_customer_service")
    for _ in range(4):
        _mensaje(session, Direction.OUT)
    # Un saliente FUERA del periodo: el reporte es de un mes, no de la historia.
    _mensaje(session, Direction.OUT, categoria="marketing", tipo="regular",
             cuando=DIA_DE_OCTUBRE)
    _mensaje(session, Direction.IN)

    reporte = medidor.resumen(session, *AGOSTO_2026)

    assert reporte["salientes"] == 9
    assert reporte["sin_dato"] == reporte["salientes"] - reporte["con_veredicto"] == 4
    assert reporte["cobrables"] + reporte["gratis"] + reporte["sin_dato"] == reporte["salientes"]


# ---------------------------------------------------------------------------
# El dinero: estimado, fechado y capaz de decir «no sé»
# ---------------------------------------------------------------------------


def test_una_categoria_sin_tarifa_vale_desconocido_y_nunca_cero(monkeypatch, session, sin_espejo):
    """Sumar 0 por una categoría sin tarifa haría que el total pareciera barato
    justo cuando aparece una categoría nueva — el momento en que más importa que
    no mienta. `tarifa()` devuelve None (no 0.0) y el resumen se declara
    incompleto nombrando la categoría.
    """
    monkeypatch.setattr(medidor, "RATE_CARD", RATE_CARD_DE_PRUEBA)

    # Categoría que el rate card no lista siquiera.
    assert medidor.tarifa("referral_conversion", date(2026, 8, 10)) is None
    # Categoría listada pero con valor nulo: es la forma de `service` para el
    # rate card del 1-oct-2026 mientras Meta no publique la cifra.
    assert medidor.tarifa("service", date(2026, 10, 15)) is None
    # Y la que sí existe devuelve un número, no None.
    assert medidor.tarifa("utility", date(2026, 8, 10)) == 0.01

    _mensaje(session, Direction.OUT, categoria="utility", tipo="regular")
    _mensaje(session, Direction.OUT, categoria="referral_conversion", tipo="regular")

    reporte = medidor.resumen(session, *AGOSTO_2026)

    assert reporte["cobrables"] == 2
    assert reporte["costo_incompleto"] is True
    assert reporte["categorias_sin_tarifa"] == ["referral_conversion"]
    assert reporte["por_categoria"]["referral_conversion"]["sin_tarifa"] == 1
    assert reporte["por_categoria"]["referral_conversion"]["costo_usd"] == 0.0
    assert reporte["costo_usd"] == pytest.approx(0.01)


def test_la_tarjeta_se_elige_por_fecha_de_entrega_no_por_la_ultima_publicada(monkeypatch):
    """Meta cobra con el rate card vigente el día de la ENTREGA.

    Publicar la tarjeta del 1-oct-2026 no puede reescribir lo que costó agosto:
    una entrega de 2026-08-10 sigue usando la tarjeta con vigencia 2025-07-01
    aunque en el archivo ya viva otra con 2026-10-01.
    """
    monkeypatch.setattr(medidor, "RATE_CARD", RATE_CARD_DE_PRUEBA)

    vieja = medidor.tarjeta_vigente(date(2026, 8, 10))
    assert vieja["vigencia_desde"] == "2025-07-01"
    assert medidor.tarifa("utility", date(2026, 8, 10)) == 0.01

    nueva = medidor.tarjeta_vigente(date(2026, 10, 15))
    assert nueva["vigencia_desde"] == "2026-10-01"
    assert medidor.tarifa("utility", date(2026, 10, 15)) == 0.02

    # El día exacto en que entra en vigor ya cobra con la nueva.
    assert medidor.tarjeta_vigente(date(2026, 10, 1))["vigencia_desde"] == "2026-10-01"
    # Antes de la primera tarjeta no hay tarifa que inventar.
    assert medidor.tarjeta_vigente(date(2025, 1, 1)) is None
    assert medidor.tarifa("utility", date(2025, 1, 1)) is None


# ---------------------------------------------------------------------------
# El 1 de octubre de 2026
# ---------------------------------------------------------------------------


def test_el_mensaje_de_servicio_de_octubre_2026_se_cuenta_cobrable_sin_tocar_codigo(
    monkeypatch, session, sin_espejo
):
    """La razón de ser de la fase.

    Hoy casi todo el saliente de este bot son mensajes de servicio —libres
    dentro de la ventana de 24 h— y Meta los reporta `free_customer_service`.
    Desde el 1-oct-2026 los cobra por mensaje y empieza a reportarlos
    `regular`. Como el veredicto se copia en vez de deducirse, el conteo de
    cobrables sube solo: misma categoría, mismo código, distinto veredicto.

    Y como Meta todavía no publica la tarifa de `service`, el costo se declara
    incompleto en vez de contar esos mensajes como 0.
    """
    monkeypatch.setattr(medidor, "RATE_CARD", RATE_CARD_DE_PRUEBA)

    # Antes: mismo tipo de mensaje, gratis.
    _mensaje(session, Direction.OUT, categoria="service", tipo="free_customer_service")
    # Después: lo que Meta va a mandar a partir del 1-oct-2026.
    _mensaje(session, Direction.OUT, categoria="service", tipo="regular",
             cuando=DIA_DE_OCTUBRE)
    _mensaje(session, Direction.OUT, categoria="service", tipo="regular",
             cuando=DIA_DE_OCTUBRE)

    agosto = medidor.resumen(session, *AGOSTO_2026)
    assert agosto["cobrables"] == 0
    assert agosto["gratis"] == 1

    octubre = medidor.resumen(session, *OCTUBRE_2026)
    assert octubre["cobrables"] == 2, "el mensaje de servicio de octubre no se contó como cobrable"
    assert octubre["gratis"] == 0
    assert octubre["sin_dato"] == 0
    assert octubre["por_categoria"]["service"]["cobrables"] == 2
    # El reporte de octubre cotiza con la tarjeta de octubre…
    assert octubre["rate_card"]["vigencia_desde"] == "2026-10-01"
    # …y esa tarjeta todavía no trae precio de `service`: se dice, no se asume 0.
    assert octubre["costo_incompleto"] is True
    assert octubre["categorias_sin_tarifa"] == ["service"]


# ---------------------------------------------------------------------------
# El espejo MSSQL
# ---------------------------------------------------------------------------


def test_el_veredicto_de_cobro_llega_al_espejo_por_update_no_solo_por_insert(espejo):
    """El precio SIEMPRE llega tarde: el status de Meta es posterior al INSERT
    del mensaje, así que en el espejo entra por UPDATE o no entra nunca.

    Si `sync_wa_message` no copiara las tres columnas de pricing, el espejo
    —de donde sale la contabilidad histórica— jamás vería una cifra, aunque
    SQLite la tuviera.
    """
    def _fila(**kw) -> Message:
        base = dict(
            id=42,
            phone=TELEFONO,
            direction=Direction.OUT,
            body="Listo, tu folio quedó abierto.",
            created_at=DIA_DE_OCTUBRE,
        )
        base.update(kw)
        return Message(**base)

    def _pricing_espejado():
        with espejo.connect() as conn:
            return conn.execute(
                text(
                    "SELECT pricing_category, pricing_type, pricing_billable"
                    " FROM wa_messages WHERE source_id = 42"
                )
            ).first()

    archive.archive_wa_message(_fila())
    assert tuple(_pricing_espejado()) == (None, None, None), (
        "al insertarse el mensaje todavía no se sabe qué cobró Meta"
    )

    # Llega el status: la fila local se ACTUALIZA y el listener after_update
    # dispara sync_wa_message con el veredicto ya sellado.
    archive.sync_wa_message(
        _fila(
            wa_message_id="wamid.ESPEJO",
            wa_status="sent",
            pricing_category="service",
            pricing_type="regular",
            pricing_billable=True,
        )
    )

    assert tuple(_pricing_espejado()) == ("service", "regular", 1), (
        "el UPDATE del espejo no copió el veredicto de cobro"
    )


# ---------------------------------------------------------------------------
# La ventana de servicio de 24 h
# ---------------------------------------------------------------------------


def test_sin_entrante_no_hay_ventana_y_a_las_24_horas_exactas_ya_cerro():
    """La ventana la abre el CLIENTE, y el borde es estricto, no «casi».

    A un cliente dado de alta a mano nunca se le abrió ventana: no se le puede
    escribir libre. Y a las 24 h en punto ya cerró — dejar pasar el borde
    significa mandar un mensaje que Meta rechaza.
    """
    ahora = datetime.now(timezone.utc)

    assert ventana.abierta(None) is False
    assert ventana.abierta(ahora - timedelta(hours=24)) is False
    assert ventana.abierta(ahora - timedelta(hours=23, minutes=59)) is True


def test_las_horas_restantes_nunca_son_negativas_y_el_texto_dice_cerrada():
    """Una ventana vencida hace mucho no «debe» horas: vale 0 y se lee
    «cerrada». El asesor tiene que enterarse ANTES de escribir un mensaje largo
    que ya no va a salir.
    """
    ahora = datetime.now(timezone.utc)

    assert ventana.horas_restantes(None) == 0.0
    assert ventana.horas_restantes(ahora - timedelta(hours=48)) == 0.0
    assert ventana.horas_restantes(ahora - timedelta(hours=24)) == 0.0
    assert ventana.restante_texto(None) == "cerrada"
    assert ventana.restante_texto(ahora - timedelta(hours=48)) == "cerrada"

    # Formato: las cuatro salidas posibles. La de «solo h» es la única rama sin
    # minutos y la que se cae si alguien «simplifica» el divmod.
    assert ventana.restante_texto(ahora - timedelta(hours=20, minutes=47, seconds=30)) == "3 h 12 min"
    assert ventana.restante_texto(ahora - timedelta(hours=23, minutes=18, seconds=30)) == "41 min"
    assert ventana.restante_texto(ahora - timedelta(hours=17, seconds=-1)) == "7 h"


def test_la_ventana_se_calcula_sobre_el_ultimo_entrante_aunque_venga_sin_zona(
    session, sin_espejo
):
    """SQLite devuelve los datetime SIN tzinfo, y `ultimo_entrante` entrega uno
    de esos directo a `abierta()`. Sin `as_utc` la comparación contra un now()
    consciente de zona revienta con TypeError en el request path.

    Y la ventana la abre solo lo que ENTRA: un teléfono al que únicamente le
    hemos escrito nosotros no tiene ventana abierta.
    """
    ahora = datetime.now(timezone.utc)
    session.add(
        Message(phone=TELEFONO, direction=Direction.IN, body="quiero mi CURP",
                created_at=ahora - timedelta(hours=2))
    )
    session.add(
        Message(phone="5215500009999", direction=Direction.OUT, body="hola",
                created_at=ahora - timedelta(minutes=5))
    )
    session.commit()
    session.expire_all()

    ultimo = ventana.ultimo_entrante(session, TELEFONO)
    assert ultimo is not None
    assert ultimo.tzinfo is None, "SQLite ya devuelve zona: as_utc dejó de ser necesario"
    assert ventana.abierta(ultimo) is True
    assert 21.9 < ventana.horas_restantes(ultimo) <= 22.0
    assert ventana.abierta_para(session, TELEFONO) is True

    assert ventana.ultimo_entrante(session, "5215500009999") is None
    assert ventana.abierta_para(session, "5215500009999") is False, (
        "un saliente no abre ventana; la abre el cliente"
    )
