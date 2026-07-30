"""Cliente HTTP de Stripe: idempotencia, reintentos y las tres respuestas
distintas (sin credenciales / rechazado / no sé)."""

import httpx
import pytest

import app.stripe_client as sc
from app.config import settings


@pytest.fixture(autouse=True)
def _sin_esperas(monkeypatch):
    monkeypatch.setattr(sc.time, "sleep", lambda s: None)


@pytest.fixture()
def con_credenciales(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_sintetica")


class _Respuesta:
    def __init__(self, status=200, cuerpo=None, texto=""):
        self.status_code = status
        self._cuerpo = cuerpo
        self.text = texto or (str(cuerpo) if cuerpo else "")
        self.headers = {"Request-Id": "req_sintetico"}

    def json(self):
        if self._cuerpo is None:
            raise ValueError("sin json")
        return self._cuerpo


@pytest.mark.parametrize(
    "llave, es_real",
    [
        ("sk_test_abc", False),
        ("sk_live_abc", True),
        ("rk_test_abc", False),
        # ESTE es el que rompía: una llave RESTRINGIDA de modo real. Con
        # startswith("sk_live") se clasificaba como prueba, y el aplicador
        # habría descartado los eventos de pagos REALES con "modo no
        # coincide". Encontrado con una cuenta viva, no en el laboratorio.
        ("rk_live_abc", True),
        ("", False),
    ],
)
def test_modo_real_reconoce_las_llaves_restringidas(monkeypatch, llave, es_real):
    monkeypatch.setattr(settings, "stripe_secret_key", llave)
    assert sc.modo_real() is es_real


def test_sin_credenciales_es_noop_sin_red(monkeypatch):
    """Sin STRIPE_SECRET_KEY no debe salir NI UNA petición de red."""
    monkeypatch.setattr(settings, "stripe_secret_key", "")

    def _explota(*a, **kw):
        raise AssertionError("hizo red sin credenciales")

    monkeypatch.setattr(sc.httpx, "request", _explota)
    r = sc.obtener_payment_intent("pi_x")
    assert r.ok is False
    assert r.error_codigo == "sin_credenciales"
    assert r.indeterminado is False


def test_idempotency_key_identica_en_ambas_llamadas(con_credenciales, monkeypatch):
    """PRUEBA 13 del diseño: reintentar el POST usa la MISMA cabecera —
    con la misma clave Stripe devuelve la sesión original, no una segunda."""
    claves_vistas = []

    def _doble(metodo, url, **kw):
        claves_vistas.append(kw["headers"].get("Idempotency-Key"))
        return _Respuesta(200, {"id": "cs_test_123"})

    monkeypatch.setattr(sc.httpx, "request", _doble)
    for _ in range(2):
        sc.crear_checkout_session(
            clave_idempotencia="cobro:1:1", monto_centavos=125000, moneda="mxn",
            concepto="CURP", metadata={"cobro_id": 1}, metodos=["card"],
            success_url="https://x/ok", cancel_url="https://x/no",
        )
    assert claves_vistas == ["cobro:1:1", "cobro:1:1"]


def test_409_reintenta_y_no_es_rechazo(con_credenciales, monkeypatch):
    """PRUEBA 14: idempotency_key_in_use = la original sigue en vuelo. Se
    reintenta con backoff y si al final responde 200, la respuesta es ok."""
    intentos = {"n": 0}

    def _doble(metodo, url, **kw):
        intentos["n"] += 1
        if intentos["n"] < 3:
            return _Respuesta(409, {"error": {"code": "idempotency_key_in_use"}})
        return _Respuesta(200, {"id": "cs_test_123"})

    monkeypatch.setattr(sc.httpx, "request", _doble)
    r = sc.obtener_session("cs_x")  # cualquier operación pasa por _request
    assert r.ok is True
    assert intentos["n"] == 3


def test_409_agotado_es_indeterminado(con_credenciales, monkeypatch):
    monkeypatch.setattr(
        sc.httpx, "request",
        lambda *a, **kw: _Respuesta(409, {"error": {"code": "idempotency_key_in_use"}}),
    )
    r = sc.obtener_session("cs_x")
    assert r.ok is False
    assert r.indeterminado is True  # NUNCA marcar el cobro como fallido con esto


def test_error_de_red_es_indeterminado(con_credenciales, monkeypatch):
    def _sin_red(*a, **kw):
        raise httpx.ConnectError("sin red")

    monkeypatch.setattr(sc.httpx, "request", _sin_red)
    r = sc.obtener_payment_intent("pi_x")
    assert r.ok is False
    assert r.error_codigo == "error_red"
    assert r.indeterminado is True


def test_402_es_rechazo_definitivo_sin_reintentos(con_credenciales, monkeypatch):
    intentos = {"n": 0}

    def _doble(metodo, url, **kw):
        intentos["n"] += 1
        return _Respuesta(402, {"error": {"code": "card_declined", "message": "nope"}})

    monkeypatch.setattr(sc.httpx, "request", _doble)
    r = sc.obtener_session("cs_x")
    assert r.ok is False
    assert r.indeterminado is False  # rechazo real, no "no sé"
    assert r.error_codigo == "card_declined"
    assert intentos["n"] == 1


def _grabadora(monkeypatch):
    """Devuelve {path: data} de cada POST, con respuestas por endpoint."""
    llamadas: dict[str, dict] = {}
    orden: list[str] = []

    def _doble(metodo, url, **kw):
        path = url.replace(sc.API_BASE, "")
        orden.append(path)
        llamadas[path] = dict(kw.get("data") or {}, _clave=kw["headers"].get("Idempotency-Key"))
        ident = "cus_sintetico" if path == "/v1/customers" else "cs_test_123"
        return _Respuesta(200, {"id": ident})

    monkeypatch.setattr(sc.httpx, "request", _doble)
    return llamadas, orden


def test_checkout_manda_metadata_dos_veces_y_mapea_spei(con_credenciales, monkeypatch):
    """La metadata va a nivel sesión Y en payment_intent_data (la única
    búsqueda por metadata que existe es sobre PaymentIntents), y 'spei' se
    traduce a customer_balance con transferencia mx."""
    llamadas, _ = _grabadora(monkeypatch)
    sc.crear_checkout_session(
        clave_idempotencia="cobro:7:1", monto_centavos=125000, moneda="mxn",
        concepto="Acta", metadata={"cobro_id": 7, "token": "tok"},
        metodos=["card", "oxxo", "spei"], msi=True, oxxo_dias=3,
        success_url="https://x/ok", cancel_url="https://x/no",
    )
    capturado = llamadas["/v1/checkout/sessions"]
    assert capturado["line_items[0][price_data][unit_amount]"] == "125000"
    assert capturado["metadata[cobro_id]"] == "7"
    assert capturado["payment_intent_data[metadata][cobro_id]"] == "7"
    assert capturado["payment_intent_data[metadata][token]"] == "tok"
    assert capturado["payment_method_types[0]"] == "card"
    assert capturado["payment_method_types[1]"] == "oxxo"
    assert capturado["payment_method_types[2]"] == "customer_balance"
    assert capturado["payment_method_options[customer_balance][bank_transfer][type]"] == "mx_bank_transfer"
    assert capturado["payment_method_options[oxxo][expires_after_days]"] == "3"
    assert capturado["payment_method_options[card][installments][enabled]"] == "true"


def test_spei_crea_customer_antes_y_nunca_manda_customer_creation(con_credenciales, monkeypatch):
    """400 real de Stripe: customer_balance exige un Customer YA creado, y
    `customer` es excluyente con `customer_creation`. La clave derivada es lo
    que hace que el reintento del barrido reuse el mismo Customer."""
    llamadas, orden = _grabadora(monkeypatch)
    sc.crear_checkout_session(
        clave_idempotencia="cobro:7:1", monto_centavos=125000, moneda="mxn",
        concepto="Acta", metadata={"cobro_id": 7}, metodos=["card", "spei"],
        success_url="https://x/ok", cancel_url="https://x/no",
    )
    assert orden == ["/v1/customers", "/v1/checkout/sessions"]
    assert llamadas["/v1/customers"]["_clave"] == "cobro:7:1:cus"
    assert llamadas["/v1/customers"]["metadata[cobro_id]"] == "7"
    sesion = llamadas["/v1/checkout/sessions"]
    assert sesion["customer"] == "cus_sintetico"
    assert "customer_creation" not in sesion


def test_sin_spei_no_se_crea_customer(con_credenciales, monkeypatch):
    llamadas, orden = _grabadora(monkeypatch)
    sc.crear_checkout_session(
        clave_idempotencia="cobro:8:1", monto_centavos=1000, moneda="mxn",
        concepto="Acta", metadata={"cobro_id": 8}, metodos=["card", "oxxo"],
        success_url="https://x/ok", cancel_url="https://x/no",
    )
    assert orden == ["/v1/checkout/sessions"]
    assert "customer" not in llamadas["/v1/checkout/sessions"]


def test_si_falla_el_customer_no_se_crea_la_sesion(con_credenciales, monkeypatch):
    """Sin Customer no hay sesión posible: se propaga el error tal cual, con
    su `indeterminado` intacto para que el barrido decida — jamás un POST de
    sesión que Stripe rechazaría igual."""
    orden = []

    def _doble(metodo, url, **kw):
        orden.append(url)
        return _Respuesta(500, {"error": {"code": "api_error"}})

    monkeypatch.setattr(sc.httpx, "request", _doble)
    r = sc.crear_checkout_session(
        clave_idempotencia="cobro:9:1", monto_centavos=1000, moneda="mxn",
        concepto="Acta", metadata={"cobro_id": 9}, metodos=["spei"],
        success_url="https://x/ok", cancel_url="https://x/no",
    )
    assert r.ok is False
    assert r.indeterminado is True
    assert all(u.endswith("/v1/customers") for u in orden)
