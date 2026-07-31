"""Resolución de dos factores y máquina de estados del aplicador de eventos.

Cada prueba nombra el bug del diseño (§9.20-31) que previene.
"""

import json
from uuid import uuid4

from sqlmodel import Session, select

import app.cobranza as cobranza
import app.db as db
from app.config import settings
from app.models import Alert, Cobro, EventoPasarela


def _cobro(s: Session, **kw) -> Cobro:
    base = dict(
        phone="5215511112222", concepto="CURP (folio #42)", monto_centavos=125000,
        estado="pendiente", llave_cobro_vivo=f"5215511112222:svc:{uuid4().hex[:6]}",
        token_verificacion=uuid4().hex, creado_por="ana",
    )
    base.update(kw)
    c = Cobro(**base)
    s.add(c)
    s.commit()
    s.refresh(c)
    return c


def _evento(s: Session, tipo: str, obj: dict, created: int = 100,
            livemode: bool | None = None, event_id: str | None = None) -> EventoPasarela:
    eid = event_id or f"evt_{uuid4().hex[:10]}"
    payload = {"id": eid, "type": tipo, "created": created, "livemode": livemode,
               "data": {"object": obj}}
    ev = EventoPasarela(stripe_event_id=eid, tipo=tipo, livemode=livemode,
                        stripe_created=created, payload_json=json.dumps(payload))
    s.add(ev)
    s.commit()
    s.refresh(ev)
    return ev


def _meta(c: Cobro) -> dict:
    return {"cobro_id": str(c.id), "token": c.token_verificacion}


def _alertas(s: Session, code: str) -> list[Alert]:
    return list(s.exec(select(Alert).where(Alert.code == code)).all())


def test_20_evento_sin_ids_no_engancha_la_fila_equivocada():
    """PREVIENE el `== None` -> IS NULL: dos cobros sin payment_intent en la
    base y un evento sin payment_intent deben dar huérfano, no la fila vieja."""
    with Session(db.engine) as s:
        _cobro(s)
        _cobro(s, phone="5215533334444")
        ev = _evento(s, "payment_intent.succeeded",
                     {"object": "charge", "payment_intent": None})
        resultado = cobranza.aplicar_evento(s, ev)
        s.commit()
        assert resultado == "huerfano"
        assert ev.cobro_id is None


def test_21_token_reciclado_no_toca_al_cobro_nuevo():
    """PREVIENE el id reciclado tras restaurar respaldo: cobro_id coincide
    pero el token no -> no aplica y hay Alert."""
    with Session(db.engine) as s:
        c = _cobro(s, token_verificacion="yyy")
        ev = _evento(s, "payment_intent.succeeded", {
            "object": "payment_intent", "id": "pi_1",
            "metadata": {"cobro_id": str(c.id), "token": "xxx"},
        })
        resultado = cobranza.aplicar_evento(s, ev)
        s.commit()
        assert resultado == "ignorado"
        assert ev.detalle == "metadata no coincide"
        assert s.get(Cobro, c.id).estado == "pendiente"
        assert _alertas(s, "cobro:metadata-no-coincide")


def test_22_completed_unpaid_no_es_pago_pero_absorbe():
    """PREVIENE el bug clásico de OXXO (dar por cobrado el voucher) Y perder
    el payment_intent por la monotonía: el estado no sube, los datos sí."""
    with Session(db.engine) as s:
        c = _cobro(s)
        ev = _evento(s, "checkout.session.completed", {
            "object": "checkout.session", "id": "cs_1", "payment_status": "unpaid",
            "payment_intent": "pi_voucher", "metadata": _meta(c),
            "payment_method_types": ["oxxo"],
        })
        cobranza.aplicar_evento(s, ev)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.estado == "pendiente"
        assert fila.stripe_payment_intent_id == "pi_voucher"
        assert fila.detalle_pendiente_at is not None
        assert fila.metodo_usado == "oxxo"


def test_23_expired_con_voucher_vivo_se_ignora():
    """La sesión expira a 24 h pero el voucher vive 72: expirar la sesión NO
    mata el instrumento y el cobro sigue pendiente."""
    with Session(db.engine) as s:
        c = _cobro(s, stripe_payment_intent_id="pi_voucher")
        ev = _evento(s, "checkout.session.expired", {
            "object": "checkout.session", "id": "cs_1", "metadata": _meta(c),
        })
        resultado = cobranza.aplicar_evento(s, ev)
        s.commit()
        assert resultado == "ignorado"
        fila = s.get(Cobro, c.id)
        assert fila.estado == "pendiente"
        assert fila.llave_cobro_vivo is not None  # el candado sigue puesto


def test_24_pago_tardio_gana_sobre_vencido_y_cancelado():
    """El dinero real manda: un succeeded sobre rango 3 pasa a pagado con
    discrepancia y Alert, jamás se rechaza."""
    for estado_terminal in ("vencido", "cancelado"):
        with Session(db.engine) as s:
            c = _cobro(s, estado=estado_terminal, llave_cobro_vivo=None,
                       ultimo_evento_created=50)
            ev = _evento(s, "checkout.session.async_payment_succeeded", {
                "object": "checkout.session", "id": f"cs_{estado_terminal}",
                "payment_status": "paid", "amount_total": 125000, "currency": "mxn",
                "metadata": _meta(c),
            }, created=200)
            cobranza.aplicar_evento(s, ev)
            s.commit()
            fila = s.get(Cobro, c.id)
            assert fila.estado == "pagado"
            assert fila.discrepancia is True
            assert fila.estado_motivo == "pago tardío aceptado"
            assert _alertas(s, "cobro:pago-tardio")


def test_25_expired_posterior_no_desmarca_el_pago():
    """Monotonía: expired (created=100) llegando DESPUÉS de succeeded
    (created=90) no baja el rango."""
    with Session(db.engine) as s:
        c = _cobro(s)
        pago = _evento(s, "payment_intent.succeeded", {
            "object": "payment_intent", "id": "pi_1", "amount_received": 125000,
            "currency": "mxn", "metadata": _meta(c),
        }, created=90)
        cobranza.aplicar_evento(s, pago)
        vencido = _evento(s, "checkout.session.expired", {
            "object": "checkout.session", "id": "cs_1", "metadata": _meta(c),
        }, created=100)
        cobranza.aplicar_evento(s, vencido)
        s.commit()
        assert s.get(Cobro, c.id).estado == "pagado"


def test_26_segundo_pago_real_no_se_pisa_y_alerta():
    """pi_B sobre un cobro ya pagado con pi_A NO es replay: doble cargo real.
    No se pisa nada, discrepancia=True y Alert crítica."""
    with Session(db.engine) as s:
        c = _cobro(s, estado="pagado", llave_cobro_vivo=None,
                   stripe_payment_intent_id="pi_A", monto_pagado_centavos=125000,
                   ultimo_evento_created=100)
        ev = _evento(s, "payment_intent.succeeded", {
            "object": "payment_intent", "id": "pi_B", "amount_received": 125000,
            "currency": "mxn", "metadata": _meta(c),
        }, created=200)
        resultado = cobranza.aplicar_evento(s, ev)
        s.commit()
        assert resultado == "ignorado"
        assert ev.detalle == "segundo pago sobre cobro pagado"
        fila = s.get(Cobro, c.id)
        assert fila.discrepancia is True
        assert _alertas(s, "cobro:doble-pago")


def test_27_moneda_igual_no_enciende_la_alerta():
    """PREVIENE la alerta encendida en el 100% de los pagos: 'mxn' contra
    'mxn' (casefold) no escribe motivo ni Alert."""
    with Session(db.engine) as s:
        c = _cobro(s)
        ev = _evento(s, "checkout.session.completed", {
            "object": "checkout.session", "id": "cs_1", "payment_status": "paid",
            "amount_total": 125000, "currency": "mxn", "metadata": _meta(c),
        })
        cobranza.aplicar_evento(s, ev)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.estado == "pagado"
        assert fila.estado_motivo is None
        assert not _alertas(s, "cobro:monto-distinto")


def test_28_monto_distinto_aplica_con_discrepancia():
    """El dinero real se registra (monto_pagado), lo pedido queda intacto."""
    with Session(db.engine) as s:
        c = _cobro(s)
        ev = _evento(s, "checkout.session.completed", {
            "object": "checkout.session", "id": "cs_1", "payment_status": "paid",
            "amount_total": 120000, "currency": "mxn", "metadata": _meta(c),
        })
        cobranza.aplicar_evento(s, ev)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.estado == "pagado"
        assert fila.monto_pagado_centavos == 120000
        assert fila.monto_centavos == 125000  # lo pedido NUNCA se pisa
        assert fila.discrepancia is True
        assert _alertas(s, "cobro:monto-distinto")


def test_29_livemode_cruzado_no_mueve_nada(monkeypatch):
    """Llave sk_live con evento livemode=false: se persiste como ignorado con
    Alert y ni un estado se mueve."""
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_live_sintetica")
    with Session(db.engine) as s:
        c = _cobro(s)
        ev = _evento(s, "checkout.session.completed", {
            "object": "checkout.session", "id": "cs_1", "payment_status": "paid",
            "amount_total": 125000, "currency": "mxn", "metadata": _meta(c),
        }, livemode=False)
        resultado = cobranza.aplicar_evento(s, ev)
        s.commit()
        assert resultado == "ignorado"
        assert ev.detalle == "modo no coincide"
        assert s.get(Cobro, c.id).estado == "pendiente"
        assert _alertas(s, "cobro:modo-no-coincide")


def test_30_reembolso_parcial_y_total():
    """Parcial: estado sigue pagado con el acumulado ABSOLUTO (reentrega no
    duplica). Total: estado reembolsado."""
    with Session(db.engine) as s:
        c = _cobro(s, estado="pagado", llave_cobro_vivo=None,
                   stripe_charge_id="ch_1", monto_pagado_centavos=200000,
                   ultimo_evento_created=100)
        parcial = _evento(s, "charge.refunded", {
            "object": "charge", "id": "ch_1", "amount_refunded": 20000,
            "metadata": _meta(c),
        }, created=200)
        cobranza.aplicar_evento(s, parcial)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.monto_reembolsado_centavos == 20000
        assert fila.estado == "pagado"

        # Reentrega con el mismo acumulado: no duplica.
        replay = _evento(s, "charge.refunded", {
            "object": "charge", "id": "ch_1", "amount_refunded": 20000,
            "metadata": _meta(c),
        }, created=201)
        cobranza.aplicar_evento(s, replay)
        s.commit()
        assert s.get(Cobro, c.id).monto_reembolsado_centavos == 20000

        total = _evento(s, "charge.refunded", {
            "object": "charge", "id": "ch_1", "amount_refunded": 200000,
            "metadata": _meta(c),
        }, created=300)
        cobranza.aplicar_evento(s, total)
        s.commit()
        assert s.get(Cobro, c.id).estado == "reembolsado"


def test_45_refund_updated_trae_un_refund_y_no_borra_el_acumulado():
    """`charge.refund.updated` entrega un objeto **refund**, NO un charge: no
    trae `amount_refunded`. Leerlo con `or 0` ponía el acumulado en CERO sobre
    un cobro ya reembolsado, y el replay lo repetía en cada recomputación.

    Tampoco puede pisar la balance transaction: la del refund es otra, y
    negativa. Se borra a propósito para forzar la relectura del charge.
    """
    with Session(db.engine) as s:
        c = _cobro(s, estado="pagado", llave_cobro_vivo=None,
                   stripe_charge_id="ch_1", monto_pagado_centavos=200000,
                   monto_reembolsado_centavos=20000, neto_centavos=482000,
                   balance_transaction_id="txn_cargo", ultimo_evento_created=100)
        ev = _evento(s, "charge.refund.updated", {
            "object": "refund", "id": "re_1", "amount": 20000,
            "balance_transaction": "txn_refund", "status": "succeeded",
            "charge": "ch_1", "metadata": _meta(c),
        }, created=200)
        cobranza.aplicar_evento(s, ev)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.monto_reembolsado_centavos == 20000  # NO se borró
        assert fila.neto_centavos is None                # vuelve a la cola
        assert fila.balance_transaction_id is None       # fuerza releer el charge
        assert fila.stripe_charge_id == "ch_1"


def test_46_reembolso_invalida_el_neto_ya_conciliado():
    """Sin esto el neto quedaba inflado para siempre: conciliar_montos filtra
    por `neto_centavos IS NULL` y el cobro ya no volvía a entrar nunca."""
    with Session(db.engine) as s:
        c = _cobro(s, estado="pagado", llave_cobro_vivo=None,
                   stripe_charge_id="ch_1", monto_pagado_centavos=500000,
                   neto_centavos=482000, comision_centavos=18000,
                   balance_transaction_id="txn_cargo", payout_id="po_x",
                   ultimo_evento_created=100)
        ev = _evento(s, "charge.refunded", {
            "object": "charge", "id": "ch_1", "amount_refunded": 200000,
            "metadata": _meta(c),
        }, created=200)
        cobranza.aplicar_evento(s, ev)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.monto_reembolsado_centavos == 200000
        assert fila.neto_centavos is None
        # La BT del cargo NO se toca: el charge sigue siendo la fuente y el
        # payout donde ya cayó el dinero tampoco cambia.
        assert fila.balance_transaction_id == "txn_cargo"
        assert fila.payout_id == "po_x"


def test_48_disputa_cerrada_apaga_la_bandera():
    """Sin manejar `charge.dispute.closed` la bandera `disputado` no se
    limpiaba nunca: la alerta crítica quedaba encendida de por vida y la
    SIGUIENTE disputa solo actualizaba la misma fila, invisible."""
    with Session(db.engine) as s:
        c = _cobro(s, estado="pagado", llave_cobro_vivo=None,
                   stripe_charge_id="ch_1", monto_pagado_centavos=125000,
                   ultimo_evento_created=100)
        abierta = _evento(s, "charge.dispute.created", {
            "object": "dispute", "id": "dp_1", "charge": "ch_1",
            "status": "needs_response", "metadata": _meta(c),
        }, created=200)
        cobranza.aplicar_evento(s, abierta)
        s.commit()
        assert s.get(Cobro, c.id).disputado is True

        ganada = _evento(s, "charge.dispute.closed", {
            "object": "dispute", "id": "dp_1", "charge": "ch_1",
            "status": "won", "metadata": _meta(c),
        }, created=300)
        cobranza.aplicar_evento(s, ganada)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.disputado is False
        assert fila.estado == "pagado"  # la disputa nunca toca el estado
        assert fila.discrepancia is False


def test_48b_disputa_perdida_deja_rastro_y_alerta_propia():
    """Perder es distinto de ganar: el dinero se fue y Stripe cobró la
    penalización. La bandera se apaga igual, pero no en silencio."""
    with Session(db.engine) as s:
        c = _cobro(s, estado="pagado", llave_cobro_vivo=None,
                   stripe_charge_id="ch_1", monto_pagado_centavos=125000,
                   disputado=True, ultimo_evento_created=100)
        ev = _evento(s, "charge.dispute.closed", {
            "object": "dispute", "id": "dp_1", "charge": "ch_1",
            "status": "lost", "metadata": _meta(c),
        }, created=300)
        cobranza.aplicar_evento(s, ev)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.disputado is False
        assert fila.discrepancia is True
        assert "lost" in (fila.estado_motivo or "")
        assert _alertas(s, "cobro:disputa-perdida")


def test_47_evento_viejo_rellena_lo_que_falta_sin_pisar_lo_que_hay():
    """Un sintético del panel estampa el reloj del VPS en
    `ultimo_evento_created`. El `payment_intent.succeeded` REAL que llega
    después es "más viejo" y se descartaba entero: el cobro quedaba pagado sin
    `stripe_charge_id`, y conciliar_montos filtra por esa columna — pago real
    fuera del circuito de conciliación para siempre, sin comisión ni neto.
    """
    with Session(db.engine) as s:
        c = _cobro(s, ultimo_evento_created=300, metodo_usado="oxxo",
                   stripe_charge_id=None)
        ev = _evento(s, "payment_intent.succeeded", {
            "object": "payment_intent", "id": "pi_1", "amount_received": 125000,
            "currency": "mxn", "latest_charge": "ch_tardio",
            "payment_method_details": {"type": "card"},
            "metadata": _meta(c),
        }, created=100)  # MÁS VIEJO que ultimo_evento_created
        cobranza.aplicar_evento(s, ev)
        s.commit()
        fila = s.get(Cobro, c.id)
        assert fila.estado == "pagado"
        assert fila.stripe_charge_id == "ch_tardio"  # se rellenó lo que faltaba
        assert fila.metodo_usado == "oxxo"           # NO se pisó lo que ya había
        # Y el reloj no retrocede: si bajara, reabriría la puerta a otros aún
        # más viejos.
        assert fila.ultimo_evento_created == 300


def test_44_pago_con_folio_cerrado_avisa_pero_no_reabre():
    """Con OXXO el pago llega días después, muchas veces con el folio ya
    cerrado. El dinero se registra SIEMPRE; el folio NO se reabre solo (eso lo
    decide un humano) pero el dueño del cobro recibe un seguimiento para hoy."""
    from app.models import FollowUp, Ticket, TicketStatus

    with Session(db.engine) as s:
        s.add(Ticket(id=7, phone="5215511112222", category="Servicios básicos",
                     service_id="curp", service_name="CURP", price_info="$800",
                     status=TicketStatus.CERRADO, assigned_to=None))
        s.commit()
        c = _cobro(s, ticket_id=7, creado_por="ana")
        ev = _evento(s, "checkout.session.async_payment_succeeded", {
            "object": "checkout.session", "id": "cs_tarde", "payment_status": "paid",
            "amount_total": 125000, "currency": "mxn", "metadata": _meta(c),
        }, created=300)
        cobranza.aplicar_evento(s, ev)
        s.commit()

        assert s.get(Cobro, c.id).estado == "pagado"
        # El folio sigue cerrado: reabrirlo es decisión de un humano.
        assert s.get(Ticket, 7).status == TicketStatus.CERRADO
        seguimiento = s.exec(select(FollowUp).where(FollowUp.ticket_id == 7)).one()
        assert seguimiento.username == "ana"  # el dueño del cobro, no el asignado
        assert _alertas(s, "cobro:pago-tras-cierre")


def test_31_replay_completo_reproduce_la_fila():
    """PREVIENE la resurrección del cancelado: crear -> enviar -> pagar ->
    intento de cancelar (rango menor, ignorado). recomputar_estado devuelve
    exactamente lo que está en la fila y el candado sigue en NULL."""
    with Session(db.engine) as s:
        c = _cobro(s, estado="borrador")
        eventos = [
            ("cobro.creado", {"cobro_id": c.id}, 10),
            ("cobro.enviado", {
                "object": "checkout.session", "id": "cs_31",
                "url": "https://checkout.stripe.com/c/pay/cs_31",
                "expires_at": 1000, "metadata": _meta(c),
            }, 20),
            ("checkout.session.completed", {
                "object": "checkout.session", "id": "cs_31",
                "payment_status": "paid", "amount_total": 125000,
                "currency": "mxn", "payment_intent": "pi_31",
                "metadata": _meta(c),
            }, 30),
            ("payment_intent.canceled", {
                "object": "payment_intent", "id": "pi_31",
                "cancelado_por": "ana", "metadata": _meta(c),
            }, 40),
        ]
        for i, (tipo, obj, created) in enumerate(eventos):
            ev = _evento(s, tipo, obj, created=created, event_id=f"sint:31:{i}")
            cobranza.aplicar_evento(s, ev)
            s.commit()

        fila = s.get(Cobro, c.id)
        antes = {campo: getattr(fila, campo) for campo in cobranza._CAMPOS_REPLAY}
        antes["estado"] = fila.estado
        assert fila.estado == "pagado"  # cancelar (rango 3) no desmarca el 4
        assert fila.llave_cobro_vivo is None

        cobranza.recomputar_estado(s, c.id)
        s.commit()
        despues = s.get(Cobro, c.id)
        assert despues.estado == antes["estado"]
        assert despues.llave_cobro_vivo is None
        for campo in cobranza._CAMPOS_REPLAY:
            assert getattr(despues, campo) == antes[campo], campo
