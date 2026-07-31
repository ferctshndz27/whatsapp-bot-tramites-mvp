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
