"""Reglas de alerta de cobranza.

Lo que se defiende aquí: una alerta que NO se apaga sola es peor que no
tenerla. `evaluate_alerts()` reutiliza la fila mientras el código siga
disparando, así que una regla sin ventana deja la alerta encendida para
siempre y el caso siguiente —el que sí importa— no produce nada visible.
"""

from datetime import timedelta

from sqlmodel import Session

import app.cobranza as cobranza
import app.db as db
from app.cobranza_alertas import VENTANA_TRANSITORIA_DIAS, reglas_cobranza
from app.models import Cobro, EventoPasarela, Ticket, TicketStatus, utcnow


def _codigos(s: Session) -> set[str]:
    return {regla[0] for regla in reglas_cobranza(s)}


def _hace(dias: int):
    return cobranza.naive_utc(utcnow() - timedelta(days=dias))


def _cobro(s: Session, **kw) -> Cobro:
    base = dict(phone="5215511112222", concepto="CURP", monto_centavos=125000,
                token_verificacion=f"tok{kw.get('id', 0)}", creado_por="ana")
    base.update(kw)
    c = Cobro(**base)
    s.add(c)
    s.commit()
    return c


def test_doble_pago_se_apaga_pasada_la_ventana():
    """eventopasarela es append-only: sin ventana, UN doble cargo dejaba la
    alerta crítica encendida de por vida y el SEGUNDO caso, meses después,
    solo actualizaba la misma fila sin que nadie viera nada nuevo."""
    with Session(db.engine) as s:
        s.add(EventoPasarela(
            stripe_event_id="evt_viejo", tipo="payment_intent.succeeded",
            resultado="ignorado", detalle="segundo pago sobre cobro pagado",
            recibido_at=_hace(VENTANA_TRANSITORIA_DIAS + 3),
        ))
        s.commit()
        assert "cobro:doble-pago" not in _codigos(s)

        s.add(EventoPasarela(
            stripe_event_id="evt_reciente", tipo="payment_intent.succeeded",
            resultado="ignorado", detalle="segundo pago sobre cobro pagado",
            recibido_at=_hace(1),
        ))
        s.commit()
        assert "cobro:doble-pago" in _codigos(s)


def test_pago_tras_cierre_se_apaga_pasada_la_ventana():
    """`aviso_pago_enviado_at` solo se escribe si el WhatsApp SALE, y la adenda
    A.4 decidió que sin plantilla HSM el bot no escribe fuera de la ventana de
    24 h — justo este caso. Sin ventana el contador solo subía."""
    with Session(db.engine) as s:
        s.add(Ticket(id=1, phone="5215511112222", client_name="Cliente Prueba",
                     category="Servicios básicos", service_id="curp",
                     service_name="CURP certificada", price_info="$800",
                     status=TicketStatus.CERRADO))
        s.commit()
        _cobro(s, id=1, ticket_id=1, estado="pagado",
               monto_pagado_centavos=125000,
               pagado_at=_hace(VENTANA_TRANSITORIA_DIAS + 3))
        assert "cobro:pago-tras-cierre" not in _codigos(s)

        _cobro(s, id=2, ticket_id=1, estado="pagado",
               monto_pagado_centavos=125000, pagado_at=_hace(1))
        assert "cobro:pago-tras-cierre" in _codigos(s)


def test_monto_sobre_cotizado_dispara_y_se_apaga_pasada_la_ventana():
    """El panel ya clasificaba el override y se lo enseñaba al asesor que lo
    tecleaba, pero nadie más lo miraba: se podía pedir más de lo cotizado por
    WhatsApp sin que nada lo levantara."""
    with Session(db.engine) as s:
        _cobro(s, id=1, monto_centavos=250000, monto_cotizado_centavos=80000,
               monto_origen="override", estado="pendiente",
               created_at=_hace(VENTANA_TRANSITORIA_DIAS + 3))
        assert "cobro:monto-sobre-cotizado" not in _codigos(s)

        _cobro(s, id=2, monto_centavos=250000, monto_cotizado_centavos=80000,
               monto_origen="override", estado="pendiente", created_at=_hace(1))
        assert "cobro:monto-sobre-cotizado" in _codigos(s)


def test_monto_bajo_lo_cotizado_o_ya_muerto_no_dispara():
    """Cobrar MENOS de lo cotizado no es queja, y un cobro cancelado nunca le
    llegó a la cartera del cliente."""
    with Session(db.engine) as s:
        _cobro(s, id=1, monto_centavos=50000, monto_cotizado_centavos=80000,
               monto_origen="override", estado="pendiente", created_at=_hace(1))
        _cobro(s, id=2, monto_centavos=250000, monto_cotizado_centavos=80000,
               monto_origen="override", estado="cancelado", created_at=_hace(1))
        assert "cobro:monto-sobre-cotizado" not in _codigos(s)


def test_disputa_se_apaga_cuando_se_limpia_la_bandera():
    """La bandera `disputado` nunca se limpiaba porque no había quién manejara
    `charge.dispute.closed`: la alerta crítica quedaba prendida para siempre."""
    with Session(db.engine) as s:
        c = _cobro(s, id=1, estado="pagado", monto_pagado_centavos=125000,
                   stripe_charge_id="ch_1", disputado=True)
        assert "cobro:disputa" in _codigos(s)

        c.disputado = False  # lo que escribe la rama nueva del aplicador
        s.add(c)
        s.commit()
        assert "cobro:disputa" not in _codigos(s)


def test_las_alertas_adhoc_del_aplicador_tienen_regla_derivada():
    """PREVIENE el suicidio nocturno: `evaluate_alerts()` apaga toda alerta
    cuyo código no vuelva a disparar, y corre al final del MISMO nightly() que
    los barridos. Cinco códigos que el aplicador levanta ad-hoc no tenían
    regla derivada: se morían esa misma noche con el problema vivo."""
    with Session(db.engine) as s:
        # pago-parcial: SPEI transfirió parte y el cobro sigue vivo.
        _cobro(s, id=1, estado="pendiente", monto_centavos=200000,
               monto_pagado_centavos=50000)
        # pago-tardio: el dinero real ganó sobre un estado terminal.
        _cobro(s, id=2, estado="pagado", monto_pagado_centavos=125000,
               estado_motivo="pago tardío aceptado", pagado_at=_hace(1))
        # candado-en-conflicto: vivo que exige candado y no lo tiene.
        _cobro(s, id=3, estado="pendiente")  # llave_cobro_vivo = None
        # disputa-perdida y metadata-no-coincide: derivan del ledger de eventos.
        s.add(EventoPasarela(stripe_event_id="evt_lost", tipo="charge.dispute.closed",
                             resultado="aplicado", detalle="disputa lost",
                             recibido_at=_hace(1)))
        s.add(EventoPasarela(stripe_event_id="evt_meta", tipo="payment_intent.succeeded",
                             resultado="ignorado", detalle="metadata no coincide",
                             recibido_at=_hace(1)))
        s.commit()
        codigos = _codigos(s)
        assert "cobro:pago-parcial" in codigos
        assert "cobro:pago-tardio" in codigos
        assert "cobro:candado-en-conflicto" in codigos
        assert "cobro:disputa-perdida" in codigos
        assert "cobro:metadata-no-coincide" in codigos


def test_las_derivadas_con_ventana_se_apagan_solas():
    """Las de ledger (append-only) y el pago tardío llevan ventana: sin ella,
    un caso de hace meses dejaría la alerta encendida para siempre."""
    with Session(db.engine) as s:
        viejo = _hace(VENTANA_TRANSITORIA_DIAS + 3)
        _cobro(s, id=1, estado="pagado", monto_pagado_centavos=125000,
               estado_motivo="pago tardío aceptado", pagado_at=viejo)
        s.add(EventoPasarela(stripe_event_id="evt_lost_v", tipo="charge.dispute.closed",
                             resultado="aplicado", detalle="disputa lost",
                             recibido_at=viejo))
        s.add(EventoPasarela(stripe_event_id="evt_meta_v", tipo="payment_intent.succeeded",
                             resultado="ignorado", detalle="metadata no coincide",
                             recibido_at=viejo))
        s.commit()
        codigos = _codigos(s)
        assert "cobro:pago-tardio" not in codigos
        assert "cobro:disputa-perdida" not in codigos
        assert "cobro:metadata-no-coincide" not in codigos


def _hace_horas(horas: int):
    return cobranza.naive_utc(utcnow() - timedelta(hours=horas))


def _dentro_de_horas(horas: int):
    return cobranza.naive_utc(utcnow() + timedelta(hours=horas))


def test_los_eventos_sinteticos_de_casa_no_son_senal_de_vida_del_webhook():
    """La regla contaba CUALQUIER fila de eventopasarela, y ahí caen los sintéticos.

    Con el endpoint muerto bastaba un toque en el panel —que escribe `panel:…`— para apagar la
    crítica; y al revés, su ausencia la encendía cuando lo único cierto era que nadie había
    hecho nada. Solo un `evt_…` prueba que Stripe nos habla.
    """
    with Session(db.engine) as s:
        _cobro(s, id=1, estado="pendiente", token_verificacion="tok-a")
        s.add(EventoPasarela(stripe_event_id="panel:cancel:1:2026-09-17",
                             tipo="cobro.cancelado", recibido_at=_hace_horas(2)))
        s.add(EventoPasarela(stripe_event_id="cron:recon:1:2026-09-17",
                             tipo="checkout.session.expired", recibido_at=_hace_horas(1)))
        s.commit()
        assert "cobro:sin-eventos" in _codigos(s)

        s.add(EventoPasarela(stripe_event_id="evt_1Nabc", tipo="checkout.session.expired",
                             recibido_at=_hace_horas(3)))
        s.commit()
        assert "cobro:sin-eventos" not in _codigos(s)


def test_una_ficha_vigente_de_oxxo_no_es_un_webhook_roto():
    """El pago diferido mexicano espera al CLIENTE, no a Stripe: mientras el voucher viva,
    Stripe no tiene nada que mandar y la crítica sería ruido cada noche."""
    with Session(db.engine) as s:
        cobro = _cobro(s, id=1, estado="pendiente", token_verificacion="tok-b",
                       voucher_url="https://pay.stripe.com/voucher/x",
                       voucher_expira_at=_dentro_de_horas(48))
        assert "cobro:sin-eventos" not in _codigos(s)

        # SPEI: la ficha NO trae vencimiento propio (se transfiere mientras viva el PI), así
        # que mirar solo la fecha daba por caducada la única ficha que nunca caduca.
        cobro.voucher_expira_at = None
        s.add(cobro)
        s.commit()
        assert "cobro:sin-eventos" not in _codigos(s)

        cobro.voucher_expira_at = _hace_horas(1)  # la ficha venció: ya no hay a quién esperar
        s.add(cobro)
        s.commit()
        assert "cobro:sin-eventos" in _codigos(s)


def test_un_cobro_que_no_se_mueve_pese_a_la_reconciliacion_se_avisa_aparte():
    """El caso que costó cinco días de diagnóstico: el cobro clavado y el panel culpando al
    webhook. Son dos cosas distintas y ahora tienen dos alertas distintas — esta solo dispara
    si ALGUIEN lo está consultando, así que un `reconciliado_at` vacío no la enciende."""
    with Session(db.engine) as s:
        cobro = _cobro(s, id=1, estado="pendiente", token_verificacion="tok-c",
                       updated_at=_hace(3))
        assert "cobro:atascado" not in _codigos(s), "sin reconciliar, el culpable es otro"

        cobro.reconciliado_at = _hace_horas(2)  # el barrido sí lo revisó anoche
        s.add(cobro)
        s.commit()
        assert "cobro:atascado" in _codigos(s)

        cobro.reconciliado_at = _hace(4)  # el barrido dejó de correr
        s.add(cobro)
        s.commit()
        assert "cobro:atascado" not in _codigos(s)
