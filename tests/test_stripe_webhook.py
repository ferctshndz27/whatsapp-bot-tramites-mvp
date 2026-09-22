"""Webhook de Stripe: firma fail-closed, INSERT-es-la-detección y el contrato
de códigos de respuesta (200 vs 403 vs 503). Diseño §9.15-19."""

import hashlib
import hmac
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

import app.db as db
import app.stripe_webhook as sw
from app.config import settings
from app.models import Cobro, EventoPasarela, Office

SECRETO = "whsec_sintetico"


def _firma(raw: bytes, secreto: str = SECRETO, t: int | None = None) -> str:
    marca = t if t is not None else int(time.time())
    mac = hmac.new(secreto.encode(), f"{marca}.".encode() + raw, hashlib.sha256).hexdigest()
    return f"t={marca},v1={mac}"


@pytest.fixture()
def cliente(monkeypatch):
    # StaticPool: TestClient atiende en otro hilo y una sqlite en memoria sin
    # pool compartido le daría una base VACÍA a esa conexión (patrón authz).
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(settings, "stripe_webhook_secret", SECRETO)
    app = FastAPI()
    app.include_router(sw.router)
    return TestClient(app)


def _evento_crudo(evt_id: str = "evt_1", tipo: str = "payment_intent.succeeded") -> bytes:
    return json.dumps({
        "id": evt_id, "type": tipo, "created": 100, "livemode": False,
        "api_version": "2026-01-01",
        "data": {"object": {"object": "payment_intent", "id": "pi_1",
                            "amount_received": 125000, "currency": "mxn"}},
    }).encode()


def test_15_sin_secreto_rechaza_todo():
    """Fail-closed (espejo de test_missing_app_secret_rejects_everything):
    sin STRIPE_WEBHOOK_SECRET ninguna firma es válida."""
    raw = b"{}"
    assert sw._verificar_firma(raw, _firma(raw), "") is False


def test_16_tolerancia_y_rotacion_de_secreto(cliente):
    """t=now-400 → 403 (replay de cuerpo viejo). Cabecera con DOS v1 y solo
    la segunda válida → aceptada (Stripe manda ambas durante la rotación)."""
    raw = _evento_crudo()
    vieja = cliente.post("/webhooks/stripe", content=raw,
                         headers={"stripe-signature": _firma(raw, t=int(time.time()) - 400)})
    assert vieja.status_code == 403

    marca = int(time.time())
    buena = hmac.new(SECRETO.encode(), f"{marca}.".encode() + raw, hashlib.sha256).hexdigest()
    header = f"t={marca},v1={'0' * 64},v1={buena}"
    r = cliente.post("/webhooks/stripe", content=raw, headers={"stripe-signature": header})
    assert r.status_code == 200


def test_17_mismo_evento_dos_veces_una_fila_y_un_aplicar(cliente, monkeypatch):
    """El IntegrityError ES la detección del replay: 200 ambas veces, una
    sola fila, y aplicar_evento invocado UNA vez."""
    llamadas = {"n": 0}
    original = sw.cobranza.aplicar_evento

    def _contado(session, evento, **kw):  # office_id/payload: EXT-1 y EXT-7 (auditoría 2026-09)
        llamadas["n"] += 1
        return original(session, evento, **kw)

    monkeypatch.setattr(sw.cobranza, "aplicar_evento", _contado)
    raw = _evento_crudo("evt_repetido")
    for _ in range(2):
        r = cliente.post("/webhooks/stripe", content=raw,
                         headers={"stripe-signature": _firma(raw)})
        assert r.status_code == 200

    with Session(db.engine) as s:
        filas = s.exec(select(EventoPasarela)
                       .where(EventoPasarela.stripe_event_id == "evt_repetido")).all()
    assert len(filas) == 1
    assert llamadas["n"] == 1


def test_18_database_locked_es_503_no_200(cliente, monkeypatch):
    """PREVIENE el evento perdido: 'no pude escribir' NUNCA se confunde con
    'duplicado'. Con 503 Stripe reintenta; con 200 el evento muere aquí."""
    def _revienta():
        raise OperationalError("INSERT", {}, Exception("database is locked"))

    monkeypatch.setattr(sw, "get_session", _revienta)
    raw = _evento_crudo("evt_lock")
    r = cliente.post("/webhooks/stripe", content=raw,
                     headers={"stripe-signature": _firma(raw)})
    assert r.status_code == 503


def test_19_fallo_al_aplicar_devuelve_200_y_deja_cola(cliente, monkeypatch):
    """El payload ya es nuestro: aplicar puede fallar (200 igual) y la fila
    queda con procesado_at NULL e intentos_proceso=1 para el barrido."""
    def _explota(session, evento):
        raise RuntimeError("aplicador roto")

    monkeypatch.setattr(sw.cobranza, "aplicar_evento", _explota)
    raw = _evento_crudo("evt_cola")
    r = cliente.post("/webhooks/stripe", content=raw,
                     headers={"stripe-signature": _firma(raw)})
    assert r.status_code == 200

    with Session(db.engine) as s:
        fila = s.exec(select(EventoPasarela)
                      .where(EventoPasarela.stripe_event_id == "evt_cola")).one()
        assert fila.procesado_at is None
        assert fila.intentos_proceso == 1


def test_firma_invalida_no_escribe_nada(cliente):
    raw = _evento_crudo("evt_intruso")
    r = cliente.post("/webhooks/stripe", content=raw,
                     headers={"stripe-signature": "t=1,v1=basura"})
    assert r.status_code == 403
    with Session(db.engine) as s:
        assert s.exec(select(EventoPasarela)).first() is None


def test_evento_valido_aplica_al_cobro(cliente):
    """Camino feliz end-to-end del webhook: el evento resuelve por metadata y
    el cobro pasa a pagado."""
    with Session(db.engine) as s:
        cobro = Cobro(phone="5215511112222", concepto="CURP", monto_centavos=125000,
                      estado="pendiente", token_verificacion="tok-e2e",
                      llave_cobro_vivo="5215511112222:svc:curp", creado_por="ana")
        s.add(cobro)
        s.commit()
        s.refresh(cobro)
        cobro_id = cobro.id

    raw = json.dumps({
        "id": "evt_e2e", "type": "checkout.session.completed", "created": 200,
        "data": {"object": {
            "object": "checkout.session", "id": "cs_e2e", "payment_status": "paid",
            "amount_total": 125000, "currency": "mxn", "payment_intent": "pi_e2e",
            "metadata": {"cobro_id": str(cobro_id), "token": "tok-e2e"},
        }},
    }).encode()
    r = cliente.post("/webhooks/stripe", content=raw,
                     headers={"stripe-signature": _firma(raw)})
    assert r.status_code == 200

    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.estado == "pagado"
        assert fila.llave_cobro_vivo is None
        evento = s.exec(select(EventoPasarela)
                        .where(EventoPasarela.stripe_event_id == "evt_e2e")).one()
        assert evento.resultado == "aplicado"
        assert evento.cobro_id == cobro_id


def test_la_ruta_esta_montada_en_main():
    import app.main as main

    assert any(getattr(r, "path", "") == "/webhooks/stripe" for r in main.app.routes)


# --- Endpoint por oficina — docs/SPEC-MULTI-OFICINA.md §4.3 (PR 5) ---------

SECRETO_PUEBLA = "whsec_de_puebla"


@pytest.fixture()
def dos_oficinas(cliente, monkeypatch):
    """Tlaxcala heredando del .env y Puebla con su propio secreto en la tabla.

    Es la forma que tendrá producción cuando exista la segunda cuenta de Stripe:
    una oficina con fila y otra sin ella.
    """
    from cryptography.fernet import Fernet

    from app import credenciales
    db.init_db()
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())
    with Session(db.engine) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        s.commit()
    credenciales.guardar("STRIPE_WEBHOOK_SECRET", office_id=2,
                         valor=SECRETO_PUEBLA, updated_by="qa")
    return cliente


def _post(cliente, ruta: str, raw: bytes, secreto: str):
    return cliente.post(ruta, content=raw, headers={"stripe-signature": _firma(raw, secreto)})


def test_cada_oficina_verifica_con_su_propio_secreto(dos_oficinas):
    raw = _evento_crudo("evt_puebla")
    assert _post(dos_oficinas, "/webhooks/stripe/puebla", raw, SECRETO_PUEBLA).status_code == 200
    with Session(db.engine) as s:
        assert s.exec(select(EventoPasarela)).one().stripe_event_id == "evt_puebla"


def test_el_secreto_de_una_oficina_no_sirve_para_otra(dos_oficinas):
    """LO QUE ESTE ENDPOINT EXISTE PARA IMPEDIR: aceptar como propio un evento de
    dinero firmado por la cuenta de Stripe de otra oficina."""
    raw = _evento_crudo("evt_cruzado")
    assert _post(dos_oficinas, "/webhooks/stripe/tlaxcala", raw, SECRETO_PUEBLA).status_code == 403
    assert _post(dos_oficinas, "/webhooks/stripe/puebla", raw, SECRETO).status_code == 403
    with Session(db.engine) as s:
        assert s.exec(select(EventoPasarela)).all() == [], "escribió con una firma ajena"


def test_un_slug_desconocido_da_403_y_no_404(dos_oficinas):
    """403 y no 404: este endpoint es público, y distinguir «esa oficina no
    existe» de «tu firma no cuadra» le enumera las oficinas a cualquiera."""
    raw = _evento_crudo("evt_x")
    r = _post(dos_oficinas, "/webhooks/stripe/no-existe", raw, SECRETO)
    assert r.status_code == 403


def test_una_oficina_deshabilitada_deja_de_recibir_cobros(dos_oficinas):
    with Session(db.engine) as s:
        oficina = s.get(Office, 2)
        oficina.habilitado = False
        s.add(oficina)
        s.commit()
    raw = _evento_crudo("evt_baja")
    assert _post(dos_oficinas, "/webhooks/stripe/puebla", raw, SECRETO_PUEBLA).status_code == 403


def test_la_ruta_legada_sigue_siendo_la_de_tlaxcala(dos_oficinas):
    """Es el endpoint configurado HOY en el panel de Stripe de producción:
    moverlo sería coordinar un cambio allá con el deploy."""
    raw = _evento_crudo("evt_legado")
    assert _post(dos_oficinas, "/webhooks/stripe", raw, SECRETO).status_code == 200
    # Y sigue verificando con el secreto de Tlaxcala, no con cualquiera.
    assert _post(dos_oficinas, "/webhooks/stripe", _evento_crudo("evt_2"),
                 SECRETO_PUEBLA).status_code == 403
