"""Autocobro F3: la liga con token y el alta de la tarjeta.

Qué se rompe en el negocio si esto se pone rojo:
- Una liga que no vence, se reutiliza o se adivina deja a un tercero registrar (o ver) un cobro ajeno.
- Un alta que no compara lo que se mostró con lo que se guardó deja un consentimiento sin valor.
- Un número de tarjeta que cruzara el servidor metería al bot en PCI completo.
Datos sintéticos; Stripe es doble (ninguna prueba sale a la red).
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine, func, select

from app import autocobro, db, stripe_client
from app import autocobro_terminos as terminos
from app.autocobro import EstadoLiga
from app.config import settings
from app.main import app
from app.models import Client, ConsentimientoCobro, LigaAutocobro, PaymentMethod, PlanCobro, utcnow

TEL = "5215500000001"


@pytest.fixture(autouse=True)
def _entorno(monkeypatch):
    # Una sola conexión compartida: el TestClient corre en otro hilo y `sqlite://` daría una base vacía.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(settings, "stripe_publishable_key", "pk_test_falsa")
    monkeypatch.setattr(stripe_client, "llave_secreta_configurada", lambda: True)
    autocobro.limpiar_limite()


@pytest.fixture()
def web():
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def _sembrar() -> None:
    """Oficina y cliente. Se siembran en la prueba, no en un fixture: el arranque de la app
    (lifespan de TestClient) corre init_db() y deja la base sin ellos."""
    with db.get_session() as s:
        if s.get(Client, TEL) is None:
            s.add(Client(phone=TEL, office_id=1))
        s.commit()


def _plan(**cambios) -> int:
    _sembrar()
    datos = dict(
        phone=TEL, concepto="Mensualidad de asesoría", monto_centavos=150000, monto_total_centavos=900000,
        periodicidad="mensual", dia_cobro=5, estado="borrador", creado_por="ana",
    )
    datos.update(cambios)
    with db.get_session() as s:
        plan = PlanCobro(**datos)
        s.add(plan)
        s.commit()
        return plan.id


def _liga(plan_id: int) -> str:
    with db.get_session() as s:
        _, token = autocobro.crear_liga(s, s.get(PlanCobro, plan_id), "ana")
        s.commit()
    return token


class StripeDoble:
    """Registra lo que se le pide y contesta como Stripe contestaría."""

    def __init__(self, monkeypatch):
        self.llamadas: list[tuple[str, dict]] = []
        self.si: dict = {}
        self.fallar = False
        monkeypatch.setattr(stripe_client, "crear_customer", self.crear_customer)
        monkeypatch.setattr(stripe_client, "crear_setup_intent", self.crear_setup_intent)
        monkeypatch.setattr(stripe_client, "obtener_setup_intent", self.obtener_setup_intent)

    def _r(self, objeto):
        if self.fallar:
            return stripe_client.RespuestaStripe(ok=False, error_codigo="api_error", indeterminado=True)
        return stripe_client.RespuestaStripe(ok=True, http_status=200, objeto=objeto)

    def crear_customer(self, clave, metadata, *, nombre=None, email=None):
        self.llamadas.append(("customer", {"clave": clave, "metadata": metadata, "nombre": nombre, "email": email}))
        return self._r({"id": "cus_123"})

    def crear_setup_intent(self, customer_id, metadata, clave_idempotencia=None):
        self.llamadas.append(("si", {"customer": customer_id, "metadata": metadata}))
        self.si = {"id": "seti_123", "customer": customer_id, "metadata": {k: str(v) for k, v in metadata.items()},
                   "status": "succeeded", "mandate": "mandate_1",
                   "payment_method": {"id": "pm_123", "card": {
                       "brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030, "funding": "credit"}}}
        return self._r({"id": "seti_123", "client_secret": "seti_123_secret_abc"})

    def obtener_setup_intent(self, si_id):
        self.llamadas.append(("get_si", {"id": si_id}))
        return self._r(self.si)


@pytest.fixture()
def stripe(monkeypatch):
    return StripeDoble(monkeypatch)


def _preparar(web, token, **datos):
    base = {"nombre": "Cliente Prueba", "email": "cliente@example.com", "acepto": "1"}
    base.update(datos)
    return web.post(f"/autocobro/{token}/preparar", data=base)


# ---------------------------------------------------------------------------
# Dominio: crear y validar la liga
# ---------------------------------------------------------------------------


def test_la_liga_guarda_el_hash_y_nunca_el_token():
    plan_id = _plan()
    token = _liga(plan_id)
    assert len(token) >= 43  # 256 bits en base64url
    with db.get_session() as s:
        liga = s.exec(select(LigaAutocobro)).one()
        assert liga.token_hash == autocobro.hash_token(token)
        assert token not in {str(v) for v in liga.model_dump().values()}
        assert liga.expira_at.replace(tzinfo=None) - utcnow().replace(tzinfo=None) > timedelta(hours=71)
        assert s.get(PlanCobro, plan_id).estado == "esperando_cliente"


def test_reenviar_invalida_la_liga_anterior():
    plan_id = _plan()
    vieja = _liga(plan_id)
    nueva = _liga(plan_id)
    with db.get_session() as s:
        assert autocobro.liga_por_token(s, vieja)[0] is EstadoLiga.INACTIVA
        assert autocobro.liga_por_token(s, nueva)[0] is EstadoLiga.OK


@pytest.mark.parametrize("cambio", [
    {"monto_centavos": 0}, {"monto_total_centavos": None}, {"monto_total_centavos": 100},
    {"dia_cobro": None}, {"dia_cobro": 29}, {"periodicidad": "anual"}, {"concepto": "  "}, {"estado": "cancelado"},
])
def test_un_plan_incompleto_no_puede_llevar_liga(cambio):
    plan_id = _plan(**cambio)
    with db.get_session() as s, pytest.raises(ValueError):
        autocobro.crear_liga(s, s.get(PlanCobro, plan_id), "ana")


def test_estados_de_la_liga():
    plan_id = _plan()
    token = _liga(plan_id)
    with db.get_session() as s:
        assert autocobro.liga_por_token(s, "no-existe")[0] is EstadoLiga.NO_EXISTE
        assert autocobro.liga_por_token(s, "")[0] is EstadoLiga.NO_EXISTE
        assert autocobro.liga_por_token(s, "x" * 500)[0] is EstadoLiga.NO_EXISTE

        liga = s.exec(select(LigaAutocobro)).one()
        liga.expira_at = utcnow() - timedelta(seconds=1)
        s.add(liga); s.commit()
        assert autocobro.liga_por_token(s, token)[0] is EstadoLiga.VENCIDA

        liga.expira_at = utcnow() + timedelta(hours=1)
        liga.intentos_abiertos = autocobro.MAX_APERTURAS + 1
        s.add(liga); s.commit()
        assert autocobro.liga_por_token(s, token)[0] is EstadoLiga.DEMASIADAS

        liga.intentos_abiertos = 0
        s.add(liga); s.get(PlanCobro, plan_id).estado = "cancelado"; s.commit()
        assert autocobro.liga_por_token(s, token)[0] is EstadoLiga.INACTIVA

        s.get(PlanCobro, plan_id).estado = "esperando_cliente"
        liga.usada_at = utcnow()
        s.add(liga); s.commit()
        assert autocobro.liga_por_token(s, token)[0] is EstadoLiga.USADA


def test_abrir_la_liga_cuenta_la_apertura_y_mirar_no():
    token = _liga(_plan())
    with db.get_session() as s:
        autocobro.liga_por_token(s, token)
        autocobro.abrir_liga(s, token)
        autocobro.abrir_liga(s, token)
        s.commit()
        assert s.exec(select(LigaAutocobro)).one().intentos_abiertos == 2


# ---------------------------------------------------------------------------
# La página
# ---------------------------------------------------------------------------


def test_la_liga_muestra_el_resumen_y_la_casilla_viene_vacia(web):
    token = _liga(_plan())
    r = web.get(f"/autocobro/{token}")
    assert r.status_code == 200
    for dato in ("Tlaxcala te cobrará", "Mensualidad de asesoría", "$1,500.00 MXN", "$9,000.00 MXN", "el día 5"):
        assert dato in r.text
    assert 'id="acepto"' in r.text and "checked" not in r.text
    assert "Autorizo a Tlaxcala" in r.text and TEL not in r.text
    assert 'data-pk="pk_test_falsa"' in r.text


def test_la_liga_manda_cabeceras_de_pagina_con_secreto_y_csp_solo_para_stripe(web):
    token = _liga(_plan())
    h = web.get(f"/autocobro/{token}").headers
    assert h["referrer-policy"] == "no-referrer" and h["cache-control"] == "no-store"
    csp = h["content-security-policy"]
    script = next(d for d in csp.split("; ") if d.startswith("script-src"))
    assert script == "script-src 'self' https://js.stripe.com"
    assert "style-src 'self';" in csp + ";"
    assert "'unsafe-eval'" not in csp and "frame-ancestors 'none'" in csp
    assert "noindex" in h["x-robots-tag"]


@pytest.mark.parametrize(("token", "status"), [("no-existe", 404), ("a" * 300, 404)])
def test_una_liga_inventada_da_404_sin_detalles(web, token, status):
    r = web.get(f"/autocobro/{token}")
    assert r.status_code == status and "plan" not in r.text.lower().replace("plantilla", "")


def test_una_liga_vencida_o_reemplazada_da_410(web):
    plan_id = _plan()
    vieja = _liga(plan_id)
    _liga(plan_id)
    assert web.get(f"/autocobro/{vieja}").status_code == 410


def test_la_liga_se_bloquea_tras_demasiadas_aperturas(web):
    token = _liga(_plan())
    codigos = [web.get(f"/autocobro/{token}").status_code for _ in range(autocobro.MAX_APERTURAS + 3)]
    assert codigos[0] == 200 and codigos[-1] == 429


def test_sin_llave_publica_la_liga_avisa_503(web, monkeypatch):
    token = _liga(_plan())
    monkeypatch.setattr(settings, "stripe_publishable_key", "")
    assert web.get(f"/autocobro/{token}").status_code == 503


# ---------------------------------------------------------------------------
# preparar: customer + SetupIntent
# ---------------------------------------------------------------------------


def test_preparar_crea_customer_y_setup_intent_off_session(web, stripe):
    plan_id = _plan()
    token = _liga(plan_id)
    r = _preparar(web, token)
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["client_secret"] == "seti_123_secret_abc"
    assert cuerpo["return_url"].endswith(f"/autocobro/{token}/listo")
    tipos = [t for t, _ in stripe.llamadas]
    assert tipos == ["customer", "si"]
    assert stripe.llamadas[0][1]["email"] == "cliente@example.com"
    meta = stripe.llamadas[1][1]["metadata"]
    assert meta["plan_id"] == plan_id and len(meta["mandato_hash"]) == 64
    with db.get_session() as s:
        assert s.get(PlanCobro, plan_id).stripe_customer_id == "cus_123"


def test_un_segundo_preparar_reusa_el_customer(web, stripe):
    token = _liga(_plan())
    _preparar(web, token)
    _preparar(web, token)
    assert [t for t, _ in stripe.llamadas] == ["customer", "si", "si"]


def test_nunca_se_le_pasa_a_stripe_un_dato_de_tarjeta(web, stripe):
    token = _liga(_plan())
    _preparar(web, token, **{"card[number]": "4242424242424242", "cvc": "123"})
    volcado = repr(stripe.llamadas)
    assert "4242424242424242" not in volcado and "123" not in volcado.replace("cus_123", "").replace("seti_123", "")


@pytest.mark.parametrize("datos", [
    {"acepto": ""}, {"acepto": "0"}, {"nombre": "A"}, {"nombre": "x" * 121}, {"email": "no-es-correo"}, {"email": ""},
])
def test_preparar_exige_casilla_nombre_y_correo(web, stripe, datos):
    token = _liga(_plan())
    r = _preparar(web, token, **datos)
    assert r.status_code == 400 and r.json()["error"]
    assert stripe.llamadas == []


def test_preparar_con_liga_invalida_no_toca_stripe(web, stripe):
    assert _preparar(web, "no-existe").status_code == 410
    assert stripe.llamadas == []


def test_preparar_con_stripe_caido_da_502_sin_filtrar_el_error(web, stripe):
    token = _liga(_plan())
    stripe.fallar = True
    r = _preparar(web, token)
    assert r.status_code == 502 and "api_error" not in r.text


def test_preparar_tiene_tope_por_ip(web, stripe):
    token = _liga(_plan())
    codigos = [_preparar(web, token).status_code for _ in range(25)]
    assert codigos[0] == 200 and codigos[-1] == 429


# ---------------------------------------------------------------------------
# listo: cierre del alta
# ---------------------------------------------------------------------------


def _hasta_listo(web, stripe, **cambios_si):
    plan_id = _plan()
    token = _liga(plan_id)
    assert _preparar(web, token).status_code == 200
    stripe.si.update(cambios_si)
    return plan_id, token


def test_el_alta_completa_guarda_consentimiento_metodo_y_activa_el_plan(web, stripe):
    plan_id, token = _hasta_listo(web, stripe)
    mostrado = autocobro.resumen(db.get_session().__enter__(), PlanCobro(
        phone=TEL, concepto="Mensualidad de asesoría", monto_centavos=150000, monto_total_centavos=900000,
        periodicidad="mensual", dia_cobro=5))
    r = web.get(f"/autocobro/{token}/listo?setup_intent=seti_123")
    assert r.status_code == 200 and "quedó registrada" in r.text
    with db.get_session() as s:
        c = s.exec(select(ConsentimientoCobro)).one()
        assert c.texto_mandato == mostrado.mandato  # lo que se leyó es lo que se guardó
        assert c.hash_texto == terminos.hash_texto(terminos.VIGENTE) and c.version_terminos == "1"
        assert (c.tope_cargo_centavos, c.monto_total_centavos, c.dia_cobro) == (150000, 900000, 5)
        assert c.stripe_setup_intent_id == "seti_123" and c.stripe_mandate_id == "mandate_1"
        assert c.ip_hash and len(c.ip_hash) == 64 and "testclient" not in c.ip_hash
        pm = s.exec(select(PaymentMethod)).one()
        assert (pm.stripe_payment_method_id, pm.marca, pm.ultimos4) == ("pm_123", "visa", "4242")
        assert pm.autorizado_por == "Cliente Prueba"
        plan = s.get(PlanCobro, plan_id)
        assert (plan.estado, plan.saldo_pendiente_centavos, plan.cargos_previstos) == ("activo", 900000, 6)
        assert plan.paymentmethod_id == pm.id and plan.consentimiento_id == c.id
        assert s.exec(select(LigaAutocobro)).one().usada_at is not None
        cli = s.get(Client, TEL)
        assert (cli.name, cli.email) == ("Cliente Prueba", "cliente@example.com")


def test_refrescar_listo_sigue_siendo_exito_y_no_duplica(web, stripe):
    _, token = _hasta_listo(web, stripe)
    web.get(f"/autocobro/{token}/listo?setup_intent=seti_123")
    r = web.get(f"/autocobro/{token}/listo?setup_intent=seti_123")
    assert r.status_code == 200 and "quedó registrada" in r.text
    with db.get_session() as s:
        assert s.exec(select(func.count()).select_from(ConsentimientoCobro)).one() == 1
        assert s.exec(select(func.count()).select_from(PaymentMethod)).one() == 1


def test_la_liga_usada_no_sirve_para_otro_alta(web, stripe):
    _, token = _hasta_listo(web, stripe)
    web.get(f"/autocobro/{token}/listo?setup_intent=seti_123")
    assert web.get(f"/autocobro/{token}").status_code == 410
    assert _preparar(web, token).status_code == 410
    assert web.get(f"/autocobro/{token}/listo?setup_intent=seti_otro").status_code == 410


def test_un_setup_intent_de_otra_liga_se_rechaza_y_no_escribe(web, stripe):
    _, token = _hasta_listo(web, stripe)
    stripe.si["metadata"]["liga_id"] = "9999"
    assert web.get(f"/autocobro/{token}/listo?setup_intent=seti_123").status_code == 409
    with db.get_session() as s:
        assert s.exec(select(ConsentimientoCobro)).all() == [] and s.exec(select(PaymentMethod)).all() == []
        assert s.exec(select(LigaAutocobro)).one().usada_at is None


def test_un_customer_ajeno_se_rechaza(web, stripe):
    _, token = _hasta_listo(web, stripe, customer="cus_otro")
    assert web.get(f"/autocobro/{token}/listo?setup_intent=seti_123").status_code == 409


def test_si_el_plan_cambia_despues_de_mostrar_el_mandato_no_se_registra(web, stripe):
    plan_id, token = _hasta_listo(web, stripe)
    with db.get_session() as s:
        plan = s.get(PlanCobro, plan_id)
        plan.monto_total_centavos = 5_000_000  # el asesor lo subió a media captura
        s.add(plan); s.commit()
    assert web.get(f"/autocobro/{token}/listo?setup_intent=seti_123").status_code == 409
    with db.get_session() as s:
        assert s.exec(select(ConsentimientoCobro)).all() == []


@pytest.mark.parametrize("estado", ["requires_payment_method", "canceled", "requires_action"])
def test_un_setup_intent_que_no_triunfo_no_registra_nada(web, stripe, estado):
    _, token = _hasta_listo(web, stripe, status=estado)
    r = web.get(f"/autocobro/{token}/listo?setup_intent=seti_123")
    assert r.status_code == 200 and "No se pudo registrar" in r.text and "No se te cobró" in r.text
    with db.get_session() as s:
        assert s.exec(select(ConsentimientoCobro)).all() == []
        assert s.exec(select(LigaAutocobro)).one().usada_at is None


def test_processing_pide_esperar_y_stripe_caido_da_502(web, stripe):
    _, token = _hasta_listo(web, stripe, status="processing")
    assert web.get(f"/autocobro/{token}/listo?setup_intent=seti_123").status_code == 202
    stripe.fallar = True
    assert web.get(f"/autocobro/{token}/listo?setup_intent=seti_123").status_code == 502


@pytest.mark.parametrize("si", ["", "pi_123", "seti_" + "x" * 200])
def test_listo_exige_un_setup_intent_con_forma(web, stripe, si):
    _, token = _hasta_listo(web, stripe)
    assert web.get(f"/autocobro/{token}/listo?setup_intent={si}").status_code == 404


def test_sin_tarjeta_en_el_setup_intent_no_se_registra(web, stripe):
    _, token = _hasta_listo(web, stripe, payment_method=None)
    assert web.get(f"/autocobro/{token}/listo?setup_intent=seti_123").status_code == 409


def test_el_formulario_no_pisa_la_ficha_de_un_cliente_existente(web, stripe):
    _sembrar()
    with db.get_session() as s:
        cli = s.get(Client, TEL)
        cli.name, cli.email = "Nombre en la ficha", "ficha@example.com"
        s.add(cli); s.commit()
    _, token = _hasta_listo(web, stripe)
    web.get(f"/autocobro/{token}/listo?setup_intent=seti_123")
    with db.get_session() as s:
        cli = s.get(Client, TEL)
        assert (cli.name, cli.email) == ("Nombre en la ficha", "ficha@example.com")


def test_el_mandato_guardado_nunca_trae_numero_de_tarjeta(web, stripe):
    _, token = _hasta_listo(web, stripe)
    web.get(f"/autocobro/{token}/listo?setup_intent=seti_123")
    with db.get_session() as s:
        c = s.exec(select(ConsentimientoCobro)).one()
        assert "terminación" not in c.texto_mandato and "tarjeta que registro en este formulario" in c.texto_mandato
