"""Los cinco barridos. Diseño §9.35-40.

La regla que estas pruebas defienden: NINGÚN barrido crea un instrumento de
cobro a ciegas. Reintentar con la misma clave es seguro; crear sin saber si el
original existe es exactamente el doble cargo.
"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlmodel import Session, select

import app.cobranza as cobranza
import app.cobranza_jobs as jobs
import app.db as db
import app.stripe_client as stripe_client
from app.models import Alert, Cobro, EventoPasarela, IntentoCobro, utcnow


@pytest.fixture(autouse=True)
def _sin_red(monkeypatch):
    """Ningún barrido debe tocar la red si la prueba no lo autorizó."""
    def _explota(*a, **kw):
        raise AssertionError("el barrido hizo red sin doble")

    for nombre in ("crear_checkout_session", "obtener_payment_intent",
                   "obtener_session", "cancelar_payment_intent",
                   "obtener_charge", "obtener_balance_transaction",
                   "buscar_sesiones_por_ventana",
                   "buscar_payment_intents_por_metadata"):
        monkeypatch.setattr(jobs.stripe_client, nombre, _explota)


def _ok(objeto: dict) -> stripe_client.RespuestaStripe:
    return stripe_client.RespuestaStripe(ok=True, http_status=200, objeto=objeto)


def _vacio() -> stripe_client.RespuestaStripe:
    return stripe_client.RespuestaStripe(ok=True, http_status=200, objeto={"data": []})


def _cobro(s: Session, **kw) -> Cobro:
    base = dict(
        phone="5215511112222", concepto="CURP (folio #42)", monto_centavos=125000,
        estado="borrador", token_verificacion=uuid4().hex, creado_por="ana",
        llave_cobro_vivo=f"5215511112222:svc:{uuid4().hex[:6]}",
        metodos_ofrecidos="card,oxxo",
    )
    base.update(kw)
    c = Cobro(**base)
    s.add(c)
    s.commit()
    s.refresh(c)
    return c


def _intento(s: Session, cobro: Cobro, **kw) -> IntentoCobro:
    base = dict(
        cobro_id=cobro.id, numero=1,
        clave_idempotencia=f"cobro:{cobro.id}:1",
        metodos_solicitados=cobro.metodos_ofrecidos,
        monto_centavos=cobro.monto_centavos, moneda=cobro.moneda,
        estado="enviando", creado_por="ana",
    )
    base.update(kw)
    i = IntentoCobro(**base)
    s.add(i)
    s.commit()
    s.refresh(i)
    return i


def _alertas(s: Session, code: str) -> list[Alert]:
    return list(s.exec(select(Alert).where(Alert.code == code)).all())


# ---------------------------------------------------------------------------


def test_35_intento_reciente_reintenta_con_la_misma_clave(monkeypatch):
    """Menos de 24 h: se repite el MISMO POST con la MISMA clave. Stripe
    devuelve el objeto original, así que no aparece una segunda fila."""
    llamadas = []

    def _doble(**kw):
        llamadas.append(kw)
        return _ok({"object": "checkout.session", "id": "cs_original",
                    "url": "https://checkout.stripe.com/c/pay/cs_original",
                    "payment_intent": "pi_original", "livemode": False})

    monkeypatch.setattr(jobs.stripe_client, "crear_checkout_session", _doble)

    with Session(db.engine) as s:
        cobro = _cobro(s)
        _intento(s, cobro, solicitado_at=cobranza.naive_utc(utcnow() - timedelta(minutes=5)))
        cobro_id = cobro.id

    resultado = jobs.intentos_huerfanos()

    # `fallidos` se separó de `sin_resolver`: antes una excepción se contaba como
    # "todavía no empareja" y el resumen no distinguía un fallo de una noche
    # normal. Es la clave que jobs.errores() reconoce para sacar al cron con 1.
    assert resultado == {"huerfanos": 1, "resueltos": 1,
                         "sin_resolver": 0, "irresolubles": 0, "fallidos": 0}
    assert len(llamadas) == 1
    assert llamadas[0]["clave_idempotencia"] == f"cobro:{cobro_id}:1"

    with Session(db.engine) as s:
        intentos = s.exec(select(IntentoCobro)).all()
        assert len(intentos) == 1  # no se creó una segunda fila
        assert intentos[0].estado == "exitoso"
        assert intentos[0].stripe_id == "cs_original"
        fila = s.get(Cobro, cobro_id)
        assert fila.estado == "pendiente"
        assert fila.url_pago == "https://checkout.stripe.com/c/pay/cs_original"


def test_35b_los_parametros_del_reintento_son_identicos():
    """Stripe rechaza una clave reusada con parámetros distintos, así que
    nada en parametros_checkout puede depender del reloj de la llamada."""
    with Session(db.engine) as s:
        cobro = _cobro(s)
        intento = _intento(s, cobro)
        primero = cobranza.parametros_checkout(cobro, intento)
        segundo = cobranza.parametros_checkout(cobro, intento)
        token = cobro.token_verificacion

    assert primero == segundo
    assert primero["metadata"]["token"] == token
    assert primero["metodos"] == ["card", "oxxo"]


def test_36_intento_viejo_no_repite_el_post_solo_busca(monkeypatch):
    """Pasadas 24 h la clave CADUCÓ: reusarla crearía un objeto NUEVO. Solo
    se busca por metadata y por ventana de created."""
    busquedas = []

    def _sin_resultados_metadata(campo, valor):
        busquedas.append(("metadata", campo, valor))
        return _vacio()

    def _sin_resultados_ventana(desde, hasta):
        busquedas.append(("ventana", desde, hasta))
        return _vacio()

    monkeypatch.setattr(jobs.stripe_client, "buscar_payment_intents_por_metadata",
                        _sin_resultados_metadata)
    monkeypatch.setattr(jobs.stripe_client, "buscar_sesiones_por_ventana",
                        _sin_resultados_ventana)
    # crear_checkout_session sigue con el doble que revienta: si el barrido lo
    # llama, la prueba falla — que es justo lo que hay que impedir.

    with Session(db.engine) as s:
        cobro = _cobro(s)
        _intento(s, cobro, solicitado_at=cobranza.naive_utc(utcnow() - timedelta(hours=30)))

    resultado = jobs.intentos_huerfanos()

    assert [b[0] for b in busquedas] == ["metadata", "ventana"]
    assert resultado["resueltos"] == 0
    assert resultado["sin_resolver"] == 1


def test_36b_intento_viejo_encontrado_se_resuelve_sin_crear(monkeypatch):
    """Si la búsqueda SÍ lo encuentra, se adopta ese objeto: el cobro queda
    pendiente con su instrumento, sin haber creado nada."""
    with Session(db.engine) as s:
        cobro = _cobro(s)
        _intento(s, cobro, solicitado_at=cobranza.naive_utc(utcnow() - timedelta(hours=30)))
        token = cobro.token_verificacion
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "buscar_payment_intents_por_metadata",
        lambda campo, valor: _ok({"data": [{
            "object": "payment_intent", "id": "pi_encontrado",
            "metadata": {"cobro_id": str(cobro_id), "token": token},
        }]}),
    )

    resultado = jobs.intentos_huerfanos()

    assert resultado["resueltos"] == 1
    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.stripe_payment_intent_id == "pi_encontrado"
        assert fila.estado == "pendiente"


def test_37_tras_tres_barridos_alerta_y_cero_creacion(monkeypatch):
    """Búsqueda siempre vacía: nunca se crea nada y al tercer barrido se
    levanta la alerta para que lo revise un humano."""
    monkeypatch.setattr(jobs.stripe_client, "buscar_payment_intents_por_metadata",
                        lambda campo, valor: _vacio())
    monkeypatch.setattr(jobs.stripe_client, "buscar_sesiones_por_ventana",
                        lambda desde, hasta: _vacio())

    with Session(db.engine) as s:
        cobro = _cobro(s)
        _intento(s, cobro, solicitado_at=cobranza.naive_utc(utcnow() - timedelta(hours=30)))

    for _ in range(2):
        assert jobs.intentos_huerfanos()["sin_resolver"] == 1
    tercero = jobs.intentos_huerfanos()

    assert tercero["irresolubles"] == 1
    with Session(db.engine) as s:
        assert _alertas(s, "cobro:intento-irresoluble")
        # Cero creación: sigue habiendo UN intento y el cobro no tiene link.
        assert len(s.exec(select(IntentoCobro)).all()) == 1
        assert s.exec(select(Cobro)).one().url_pago is None


def test_38_voucher_vivo_no_se_mata_por_el_reloj_local(monkeypatch):
    """PREVIENE que el reloj local mate un instrumento todavía pagable: con
    voucher_expira_at pasado pero el PI en requires_action, sigue pendiente."""
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente", stripe_payment_intent_id="pi_vivo",
                       voucher_expira_at=cobranza.naive_utc(utcnow() - timedelta(hours=1)))
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "obtener_payment_intent",
        lambda pi: _ok({"object": "payment_intent", "id": pi,
                        "status": "requires_action"}),
    )

    resultado = jobs.reconciliar_pendientes()

    assert resultado["sin_cambio"] == 1
    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.estado == "pendiente"
        assert fila.llave_cobro_vivo is not None  # el candado sigue puesto
        assert fila.reconciliado_at is not None


def test_38c_el_lote_no_se_trunca_antes_de_filtrar(monkeypatch):
    """PREVIENE la inanición: con .limit(LOTE) ANTES del filtro, SQLite
    devuelve las primeras 200 filas por rowid —las más viejas— y el filtro de
    antigüedad corría en Python sobre lo ya truncado.

    Con LOTE zombis que no cambian nunca (SPEI en requires_action, sin reloj de
    vencimiento propio), el cobro generado hoy cuyo webhook se perdió no entra
    al lote NUNCA y su pago no se descubre jamás.
    """
    viejo = cobranza.naive_utc(utcnow() - timedelta(hours=5))
    with Session(db.engine) as s:
        for _ in range(jobs.LOTE):
            _cobro(s, estado="pendiente",
                   stripe_payment_intent_id=f"pi_zombi_{uuid4().hex[:8]}",
                   updated_at=viejo)
        # El último en entrar: rowid más alto, y por eso el que el truncado
        # dejaba fuera. Este SÍ está pagado en Stripe.
        pagado = _cobro(s, estado="pendiente", stripe_payment_intent_id="pi_el_bueno",
                        updated_at=viejo)
        pagado_id = pagado.id

    monkeypatch.setattr(
        jobs.stripe_client, "obtener_payment_intent",
        lambda pi: _ok(
            {"object": "payment_intent", "id": pi, "status": "succeeded",
             "amount_received": 125000, "currency": "mxn"}
            if pi == "pi_el_bueno"
            else {"object": "payment_intent", "id": pi, "status": "requires_action"}
        ),
    )

    # Dos corridas: la cola rota por reconciliado_at, así que aunque la primera
    # se llene de zombis, la segunda tiene que llegar al que importa.
    jobs.reconciliar_pendientes()
    jobs.reconciliar_pendientes()

    with Session(db.engine) as s:
        assert s.get(Cobro, pagado_id).estado == "pagado"


def test_38d_sin_respuesta_tambien_sella_reconciliado_at(monkeypatch):
    """Es lo que rota la cola. Sin sellar, un cobro que Stripe no contesta se
    queda al frente del ORDER BY para siempre y mata de hambre a los demás."""
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente", stripe_payment_intent_id="pi_mudo",
                       updated_at=cobranza.naive_utc(utcnow() - timedelta(hours=5)))
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "obtener_payment_intent",
        lambda pi: stripe_client.RespuestaStripe(ok=False, error_codigo="error_red",
                                                 indeterminado=True),
    )

    assert jobs.reconciliar_pendientes()["fallidos"] == 1
    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.reconciliado_at is not None
        assert fila.estado == "pendiente"  # indeterminado NO es un rechazo


def test_38b_pi_cancelado_por_stripe_vence_el_cobro(monkeypatch):
    """Cancelado automáticamente = el voucher venció. Entra como fila
    sintética por el MISMO aplicador y libera el candado."""
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente", stripe_payment_intent_id="pi_muerto",
                       voucher_expira_at=cobranza.naive_utc(utcnow() - timedelta(hours=1)))
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "obtener_payment_intent",
        lambda pi: _ok({"object": "payment_intent", "id": pi, "status": "canceled",
                        "cancellation_reason": "automatic"}),
    )

    assert jobs.reconciliar_pendientes()["aplicados"] == 1

    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.estado == "vencido"
        assert fila.llave_cobro_vivo is None
        sintetico = s.exec(
            select(EventoPasarela).where(EventoPasarela.cobro_id == cobro_id)
        ).one()
        assert sintetico.stripe_event_id.startswith(f"cron:recon:{cobro_id}:")
        assert sintetico.resultado == "aplicado"


def test_38c_cancelacion_pendiente_reintenta_y_libera_al_confirmar(monkeypatch):
    """El candado solo se libera cuando Stripe CONFIRMA que el instrumento
    murió; hasta entonces el cobro sigue bloqueando el par (tel, servicio)."""
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="cancelacion_solicitada",
                       stripe_payment_intent_id="pi_por_cancelar",
                       updated_at=cobranza.naive_utc(utcnow() - timedelta(hours=3)))
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "cancelar_payment_intent",
        lambda pi, clave_idempotencia=None: _ok({
            "object": "payment_intent", "id": pi, "status": "canceled",
            "cancellation_reason": "requested_by_customer",
        }),
    )

    assert jobs.reconciliar_pendientes()["aplicados"] == 1

    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.estado == "cancelado"
        assert fila.llave_cobro_vivo is None


def test_39_completar_voucher_escribe_ficha_y_limpia_la_cola(monkeypatch):
    """El aplicador no hace red: marca detalle_pendiente_at y este barrido
    trae hosted_voucher_url y el vencimiento REAL del voucher."""
    vence = int((utcnow() + timedelta(days=3)).timestamp())
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente", stripe_payment_intent_id="pi_oxxo",
                       detalle_pendiente_at=cobranza.naive_utc(utcnow()))
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "obtener_payment_intent",
        lambda pi: _ok({
            "object": "payment_intent", "id": pi, "status": "requires_action",
            "next_action": {"oxxo_display_details": {
                "hosted_voucher_url": "https://payments.stripe.com/oxxo/voucher/abc",
                "expires_after": vence,
            }},
        }),
    )

    assert jobs.completar_voucher()["completados"] == 1

    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.voucher_url == "https://payments.stripe.com/oxxo/voucher/abc"
        assert fila.voucher_expira_at is not None
        assert fila.detalle_pendiente_at is None  # sale de la cola
        assert fila.estado == "pendiente"  # traer el detalle NO es cobrar


def test_39b_pi_sin_ficha_sale_de_la_cola_igual(monkeypatch):
    """Si el PI ya no ofrece ficha (pagó, se canceló o era tarjeta) hay que
    sacarlo de la cola o el barrido lo miraría todas las noches para siempre."""
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente", stripe_payment_intent_id="pi_tarjeta",
                       detalle_pendiente_at=cobranza.naive_utc(utcnow()))
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "obtener_payment_intent",
        lambda pi: _ok({"object": "payment_intent", "id": pi, "status": "processing"}),
    )

    jobs.completar_voucher()

    with Session(db.engine) as s:
        assert s.get(Cobro, cobro_id).detalle_pendiente_at is None
    assert jobs.completar_voucher()["pendientes"] == 0


def test_eventos_pendientes_aplica_la_cola():
    """Un evento que el webhook persistió pero no pudo aplicar se resuelve en
    el barrido, sin que Stripe tenga que reentregarlo."""
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente")
        payload = {
            "id": "evt_cola", "type": "checkout.session.completed", "created": 500,
            "data": {"object": {
                "object": "checkout.session", "id": "cs_cola",
                "payment_status": "paid", "amount_total": 125000, "currency": "mxn",
                "metadata": {"cobro_id": str(cobro.id),
                             "token": cobro.token_verificacion},
            }},
        }
        s.add(EventoPasarela(
            stripe_event_id="evt_cola", tipo="checkout.session.completed",
            stripe_created=500, payload_json=json.dumps(payload),
        ))
        s.commit()
        cobro_id = cobro.id

    assert jobs.eventos_pendientes() == {"pendientes": 1, "aplicados": 1, "fallidos": 0}

    with Session(db.engine) as s:
        assert s.get(Cobro, cobro_id).estado == "pagado"
        assert s.exec(select(EventoPasarela)).one().procesado_at is not None
    # Idempotente: correrlo otra vez no encuentra nada que hacer.
    assert jobs.eventos_pendientes()["pendientes"] == 0


def test_conciliar_montos_lee_comision_y_neto(monkeypatch):
    """comisión y neto se LEEN de la balance transaction, nunca se calculan:
    con MSI la tarifa no es plana. El reembolso se resta del neto porque en
    México Stripe no devuelve la comisión."""
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pagado", llave_cobro_vivo=None,
                       stripe_charge_id="ch_1", monto_pagado_centavos=125000,
                       monto_reembolsado_centavos=20000)
        cobro_id = cobro.id

    monkeypatch.setattr(
        jobs.stripe_client, "obtener_charge",
        lambda ch: _ok({"object": "charge", "id": ch, "balance_transaction": "txn_1",
                        "amount_refunded": 20000}),
    )
    monkeypatch.setattr(
        jobs.stripe_client, "obtener_balance_transaction",
        lambda txn: _ok({"object": "balance_transaction", "id": txn,
                         "fee": 4350, "net": 120650, "currency": "mxn",
                         "payout": "po_semana"}),
    )

    assert jobs.conciliar_montos()["conciliados"] == 1

    with Session(db.engine) as s:
        fila = s.get(Cobro, cobro_id)
        assert fila.comision_centavos == 4350
        assert fila.neto_centavos == 120650 - 20000
        assert fila.payout_id == "po_semana"
        assert fila.moneda_liquidacion == "mxn"


def test_40_nightly_no_se_cae_si_un_barrido_revienta(monkeypatch):
    """Patrón de _evaluate_alerts_safe: el barrido que truena se reporta en el
    dict y el job nocturno sigue vivo."""
    import app.jobs as app_jobs

    def eventos_pendientes():  # el nombre es la clave del dict de resultados
        raise RuntimeError("Stripe caído")

    monkeypatch.setattr(jobs, "BARRIDOS", (eventos_pendientes, *jobs.BARRIDOS[1:]))

    resultado = app_jobs.nightly()

    assert "cobranza" in resultado
    assert resultado["cobranza"]["eventos_pendientes"] == {"error": "Stripe caído"}
    # Los otros cuatro corrieron igual.
    assert resultado["cobranza"]["conciliar_montos"]["conciliados"] == 0
    assert "alertas" in resultado


def test_barrer_todo_corre_los_cinco():
    resultado = jobs.barrer_todo()

    assert set(resultado) == {b.__name__ for b in jobs.BARRIDOS}
    assert all("error" not in r for r in resultado.values())


def test_41_una_excepcion_por_intento_no_se_disfraza_de_sin_resolver(monkeypatch):
    """REGLA: el barrido de huérfanos sobrevive a un intento que revienta —eso ya
    estaba— pero tiene que DECIRLO por un canal que el cron pueda leer.

    Antes la excepción se contaba como `sin_resolver`, el mismo casillero que un
    intento que simplemente todavía no empareja: una noche normal. Con las dos
    cosas en un solo número, ni el resumen ni `jobs.errores()` podían distinguir
    "no había nada que hacer" de "no pude hacerlo", y el cron salía 0 con el
    barrido roto.
    """
    import app.jobs as app_jobs

    with Session(db.engine) as s:
        cobro = _cobro(s)
        _intento(s, cobro, solicitado_at=cobranza.naive_utc(utcnow() - timedelta(minutes=5)))

    def _revienta(*_a, **_k):
        raise RuntimeError("MSSQL caído a media resolución")

    monkeypatch.setattr(jobs, "_resolver_intento", _revienta)

    resultado = jobs.intentos_huerfanos()

    assert resultado["fallidos"] == 1, "la excepción no se contó como fallo"
    assert resultado["sin_resolver"] == 0, (
        "la excepción se disfrazó de «todavía no empareja»: es el bug que este arreglo quita"
    )
    # Y lo que de verdad importa: que el cron pueda verlo.
    assert app_jobs.errores({"cobranza": {"intentos_huerfanos": resultado}}) == [
        "cobranza.intentos_huerfanos"
    ], "el fallo no llega al código de salida del cron"


def test_la_reconciliacion_sin_respuesta_deja_rastro(monkeypatch, caplog):
    """`reconciliado_at` se sella en los dos caminos, así que sin este log «pregunté y Stripe
    no contestó» y «pregunté y sigue igual» son indistinguibles desde fuera. Costó cinco días
    de diagnóstico: el cobro clavado, el panel culpando al webhook —que es la dirección
    contraria, esta consulta SALE hacia Stripe— y ninguna forma de saber cuál de las dos era.
    """
    monkeypatch.setattr(jobs.stripe_client, "obtener_session",
                        lambda _id: stripe_client.RespuestaStripe(
                            ok=False, http_status=None, indeterminado=True,
                            error_mensaje="timeout"))
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente",
                       stripe_checkout_session_id="cs_test_mudo",
                       updated_at=utcnow() - timedelta(days=3))

    with caplog.at_level("WARNING"):
        resultado = jobs.reconciliar_pendientes()

    assert resultado["revisados"] == 1 and resultado["fallidos"] == 1
    rastro = [r.getMessage() for r in caplog.records if "sin respuesta" in r.getMessage()]
    assert rastro, "el barrido consultó, no obtuvo nada y no lo dijo en ninguna parte"
    assert f"id={cobro.id}" in rastro[0] and "cs_test_mudo" in rastro[0]

    with Session(db.engine) as s:
        # Se sella igual: es lo que rota la cola y evita matar de hambre a los demás.
        assert s.get(Cobro, cobro.id).reconciliado_at is not None


def test_una_sesion_completada_con_pago_diferido_rescata_el_payment_intent(monkeypatch):
    """El caso del cobro #7 (sep-2026): se perdió el `checkout.session.completed`.

    Sin ese evento el cobro se queda sin `payment_intent`, y como la reconciliación solo miraba
    la sesión —que ya no dice ni «pagado» ni «expirado»— la daba por viva y devolvía sin-cambio
    cada noche. Siete días con una transferencia SPEI esperando que nadie sabía que existía.
    `complete` es la señal: el checkout terminó y la ficha está emitida.
    """
    sesion = {
        "object": "checkout.session", "id": "cs_test_diferido",
        "status": "complete", "payment_status": "unpaid",
        "payment_intent": "pi_test_spei", "url": "https://checkout.stripe.com/c/pay/cs_test",
    }
    monkeypatch.setattr(jobs.stripe_client, "obtener_session", lambda _id: _ok(sesion))
    with Session(db.engine) as s:
        cobro = _cobro(s, estado="pendiente", stripe_checkout_session_id="cs_test_diferido",
                       metodos_ofrecidos="card,spei",
                       updated_at=utcnow() - timedelta(days=3))

    assert jobs._reconciliar_uno(cobro.id) == "aplicado"

    with Session(db.engine) as s:
        rescatado = s.get(Cobro, cobro.id)
        assert rescatado.stripe_payment_intent_id == "pi_test_spei"
        # Sigue esperando el depósito —el estado no sube—, pero ahora el sistema sabe que hay
        # una ficha que buscar: es lo que dispara a completar_voucher.
        assert rescatado.estado == "pendiente"
        assert rescatado.detalle_pendiente_at is not None
        evento = s.exec(select(EventoPasarela)
                        .where(EventoPasarela.cobro_id == cobro.id)).all()[-1]
        assert evento.tipo == "checkout.session.completed"
        assert evento.resultado == "aplicado" and "voucher" in (evento.detalle or "")
