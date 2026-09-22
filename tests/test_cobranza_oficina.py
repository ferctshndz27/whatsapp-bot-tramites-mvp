"""Los cobros tienen oficina y un evento de Stripe no cruza de una a otra
(auditoría 2026-09, AUTHZ-4 / EXT-1), y un payload grande no deja huérfano al
evento (EXT-7).

Lo que el informe demostró con PoC:

- `Cobro(...)` nacía sin `office_id` y caía al DEFAULT: con una segunda oficina,
  la oficina 1 veía nombre, teléfono, monto y referencia de recibo de clientes
  de la 2, y el asesor dueño no veía su propio cobro.
- `aplicar_evento` buscaba el cobro en TODAS las oficinas por `metadata.cobro_id`
  (secuencial) sin comparar con la oficina cuyo secreto verificó la firma, y el
  `token` de la metadata era opcional: un evento firmado con el secreto de la
  oficina B con `{"cobro_id": N}` marcaba PAGADO el cobro N de la A, sin
  discrepancia si el monto coincidía.
- Un payload > 32 KB se guardaba recortado, `json.loads` fallaba y el evento
  quedaba «huérfano» hasta el barrido de 2 h.

Los helpers copian la forma de tests/test_cobranza_aplicar.py.
"""

from __future__ import annotations

import json
from uuid import uuid4

from sqlmodel import Session, select

import app.cobranza as cobranza
import app.db as db
from app import stripe_webhook
from app.models import Alert, Client, Cobro, EventoPasarela, Ticket, TicketStatus

PHONE = "5215511112222"


def _cobro(s: Session, **kw) -> Cobro:
    base = dict(
        phone=PHONE, concepto="CURP (folio #42)", monto_centavos=125000, estado="pendiente",
        llave_cobro_vivo=f"{PHONE}:svc:{uuid4().hex[:6]}", token_verificacion=uuid4().hex, creado_por="ana",
    )
    base.update(kw)
    c = Cobro(**base)
    s.add(c)
    s.commit()
    s.refresh(c)
    return c


def _pago(s: Session, c: Cobro, *, con_token: bool = True) -> EventoPasarela:
    meta = {"cobro_id": str(c.id)}
    if con_token:
        meta["token"] = c.token_verificacion
    eid = f"evt_{uuid4().hex[:10]}"
    payload = {"id": eid, "type": "checkout.session.completed", "created": 200, "livemode": None,
               "data": {"object": {"object": "checkout.session", "id": f"cs_{uuid4().hex[:8]}",
                                   "payment_status": "paid", "amount_total": 125000,
                                   "currency": "mxn", "metadata": meta}}}
    ev = EventoPasarela(stripe_event_id=eid, tipo=payload["type"], stripe_created=200,
                        payload_json=json.dumps(payload))
    s.add(ev)
    s.commit()
    s.refresh(ev)
    return ev


def _alertas(s: Session, code: str) -> list[Alert]:
    return list(s.exec(select(Alert).where(Alert.code == code)).all())


# ---------------------------------------------------------------------------
# AUTHZ-4 — el cobro nace en la oficina del folio
# ---------------------------------------------------------------------------


def test_el_cobro_hereda_la_oficina_del_folio_y_sin_folio_la_de_la_ficha():
    with Session(db.engine) as s:
        s.add(Client(phone=PHONE, office_id=2))
        s.add(Ticket(id=42, phone=PHONE, category="c", service_id="s", service_name="S",
                     price_info="$1", status=TicketStatus.TOMADO, office_id=2))
        s.add(Client(phone="5215599990000", office_id=1))
        s.commit()

        con_folio = cobranza.crear_cobro(
            s, phone=PHONE, concepto="x", monto_centavos=100, creado_por="ana",
            llave_generacion="g1", token_verificacion="t1", ticket_id=42,
        )
        sin_folio = cobranza.crear_cobro(
            s, phone=PHONE, concepto="y", monto_centavos=100, creado_por="ana",
            llave_generacion="g2", token_verificacion="t2",
        )
        otra = cobranza.crear_cobro(
            s, phone="5215599990000", concepto="z", monto_centavos=100, creado_por="ana",
            llave_generacion="g3", token_verificacion="t3",
        )
        s.commit()
        assert s.get(Cobro, con_folio.id).office_id == 2, "el cobro no heredó la oficina del folio"
        assert s.get(Cobro, sin_folio.id).office_id == 2, "sin folio, no tomó la oficina de la ficha"
        assert s.get(Cobro, otra.id).office_id == 1


# ---------------------------------------------------------------------------
# EXT-1 — la oficina que firmó tiene que ser la del cobro; el token es obligatorio
# ---------------------------------------------------------------------------


def test_un_evento_firmado_por_otra_oficina_no_marca_pagado_el_cobro():
    """El PoC del informe: firma de la oficina B, metadata con el cobro N de la A.
    Queda `ignorado`, el cobro sigue pendiente y hay alerta propia. Con la
    oficina correcta (control positivo) sí se aplica."""
    with Session(db.engine) as s:
        c = _cobro(s, office_id=1)
        ev = _pago(s, c)
        resultado = cobranza.aplicar_evento(s, ev, office_id=2)
        s.commit()
        assert resultado == "ignorado" and ev.detalle == "oficina no coincide"
        assert s.get(Cobro, c.id).estado == "pendiente", "un evento de otra oficina marcó pagado el cobro"
        assert _alertas(s, "cobro:oficina-no-coincide")

        ev2 = _pago(s, c)
        assert cobranza.aplicar_evento(s, ev2, office_id=1) == "aplicado"
        s.commit()
        assert s.get(Cobro, c.id).estado == "pagado"


def test_los_llamadores_internos_no_pasan_oficina_y_siguen_igual():
    """El barrido y el panel aplican eventos que nosotros escribimos; no traen
    firma de ninguna oficina y no deben empezar a rechazarse."""
    with Session(db.engine) as s:
        c = _cobro(s, office_id=2)
        ev = _pago(s, c)
        assert cobranza.aplicar_evento(s, ev) == "aplicado"


def test_metadata_con_cobro_id_pero_sin_token_no_resuelve_al_cobro():
    """`cobro_id` es secuencial y lo pone quien arma la metadata; el token es la
    prueba de que la escribimos nosotros. Antes, sin token, se resolvía igual."""
    with Session(db.engine) as s:
        c = _cobro(s)
        ev = _pago(s, c, con_token=False)
        resultado = cobranza.aplicar_evento(s, ev, office_id=1)
        s.commit()
        assert resultado == "ignorado" and ev.detalle == "metadata sin token", (resultado, ev.detalle)
        assert s.get(Cobro, c.id).estado == "pendiente"
        cobro, rechazo = cobranza.resolver_cobro(s, json.loads(ev.payload_json))
        assert cobro is None and rechazo == "metadata sin token"


def test_por_payment_intent_sigue_resolviendo_sin_metadata():
    """Los ids de Stripe no son adivinables: un evento sobre el pi del cobro
    resuelve aunque la metadata venga vacía (la respuesta de Stripe a nuestro
    POST no siempre la trae en el mismo lugar)."""
    with Session(db.engine) as s:
        c = _cobro(s, stripe_payment_intent_id="pi_conocido_123")
        cobro, rechazo = cobranza.resolver_cobro(s, {"data": {"object": {
            "object": "payment_intent", "id": "pi_conocido_123", "metadata": {}}}})
        assert cobro is not None and cobro.id == c.id and rechazo is None


# ---------------------------------------------------------------------------
# EXT-7 — un payload grande se guarda parseable
# ---------------------------------------------------------------------------


def _payload_grande(relleno_kb: int, en_object: bool) -> tuple[bytes, dict]:
    payload = {"id": "evt_grande", "type": "checkout.session.completed", "created": 1, "livemode": False,
               "data": {"object": {"object": "checkout.session", "id": "cs_g", "payment_status": "paid",
                                   "amount_total": 100, "currency": "mxn", "metadata": {"cobro_id": "1", "token": "t"}},
                        "previous_attributes": {}},
               "request": {"id": "req_x"}}
    relleno = "x" * (relleno_kb * 1024)
    if en_object:
        payload["data"]["object"]["descripcion_larga"] = relleno
    else:
        payload["data"]["previous_attributes"]["basura"] = relleno
    return json.dumps(payload, indent=2).encode(), payload


def test_un_payload_grande_se_guarda_parseable_y_con_su_objeto():
    """Escalera: crudo si cabe; compacto; lo esencial (id, tipo y data.object).
    Cualquiera de los tres deja un JSON que `aplicar_evento` puede leer."""
    chico, payload = _payload_grande(1, en_object=False)
    assert stripe_webhook._payload_para_guardar(chico, payload) == chico.decode()

    grande_fuera, payload = _payload_grande(40, en_object=False)  # el relleno está fuera de data.object
    guardado = stripe_webhook._payload_para_guardar(grande_fuera, payload)
    assert len(guardado) <= cobranza.PAYLOAD_MAX
    leido = json.loads(guardado)
    assert leido["id"] == "evt_grande" and leido["data"]["object"]["metadata"]["token"] == "t"
    assert "basura" not in guardado, "lo esencial no debía arrastrar previous_attributes"


def test_un_payload_imposible_de_acotar_cae_al_recorte_pero_el_aplicador_recibe_el_parseado():
    """Si ni lo esencial cabe (el objeto mismo es enorme), se recorta como antes;
    por eso `_procesar` le pasa a `aplicar_evento` el payload ya parseado, y la
    aplicación inmediata no depende de lo guardado."""
    enorme, payload = _payload_grande(64, en_object=True)
    guardado = stripe_webhook._payload_para_guardar(enorme, payload)
    assert len(guardado) == cobranza.PAYLOAD_MAX
    with Session(db.engine) as s:
        c = _cobro(s)
        payload["data"]["object"]["metadata"] = {"cobro_id": str(c.id), "token": c.token_verificacion}
        ev = EventoPasarela(stripe_event_id="evt_enorme", tipo="checkout.session.completed",
                            stripe_created=1, payload_json=guardado)
        s.add(ev)
        s.commit()
        assert cobranza.aplicar_evento(s, ev, office_id=1, payload=payload) == "aplicado"
        s.commit()
        assert s.get(Cobro, c.id).estado == "pagado"
