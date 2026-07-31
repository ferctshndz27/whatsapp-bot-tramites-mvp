"""Reglas de salud de la cobranza, para `app.alerts.current_rules`.

TODAS las reglas se DERIVAN del estado de la base, nunca de un evento que
pasó. Esto no es un capricho: `evaluate_alerts()` resuelve (active=False)
cualquier alerta activa cuyo código no vuelva a disparar, así que una alerta
que solo se levantara ad-hoc desde el aplicador quedaría apagada por el
siguiente barrido nocturno aunque el problema siguiera vivo.

El aplicador SÍ levanta algunas de estas mismas alertas al vuelo (ver
`cobranza.alerta`) para que el panel las muestre en el minuto, no a la noche;
los códigos coinciden a propósito para que sea la MISMA fila y no dos.

Las condiciones transitorias (evento del modo equivocado, evento huérfano)
llevan ventana de tiempo para que se resuelvan solas cuando dejan de ser
noticia.
"""

import logging
from datetime import timedelta, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from app.cobranza import ESTADOS_VIVOS
from app.cobranza import naive_utc as _naive
from app.dinero import formato_monto
from app.models import (
    Cobro,
    EventoPasarela,
    IntentoCobro,
    Ticket,
    TicketStatus,
    utcnow,
)

logger = logging.getLogger(__name__)

# Sin un solo evento en 24 h teniendo cobros vivos, el webhook está roto o
# rotaron el secreto: es el fallo más caro porque el sistema se ve sano.
SIN_EVENTOS_HORAS = 24
# Un evento sin procesar recién llegado es normal (lo toma el barrido); a la
# hora ya es señal de que el aplicador está fallando.
EVENTO_ATORADO_HORAS = 1
# "No sé si Stripe lo creó" por más de 10 min = el barrido no lo resolvió.
INTENTO_HUERFANO_MINUTOS = 10
# Tras 3 barridos sin resolver, deja de ser automático y pasa a un humano.
INTENTO_IRRESOLUBLE_REINTENTOS = 3
# La firma de Stripe tolera 300 s; a los 120 s de desfase ya hay que actuar
# antes de que el reloj rechace el 100% de los webhooks con 403.
DESFASE_RELOJ_SEGUNDOS = 120
# El asesor marcó "confirmado" contra el recibo del cliente y Stripe sigue sin
# saber nada: o el recibo no era de este cobro o algo se atoró.
CONFIRMADO_SIN_STRIPE_HORAS = 48
# Ventana de las condiciones transitorias.
VENTANA_TRANSITORIA_DIAS = 7
# Estados donde un monto por arriba de lo cotizado todavía puede llegarle al
# cliente o ya le llegó. Cancelado, expirado y reembolsado no le costaron nada.
ESTADOS_COBRABLES = tuple(sorted(ESTADOS_VIVOS)) + ("pagado",)


def _contar(session: Session, modelo, *condiciones) -> int:
    consulta = select(func.count()).select_from(modelo)
    for condicion in condiciones:
        consulta = consulta.where(condicion)
    return session.exec(consulta).one()


def reglas_cobranza(session: Session) -> list[tuple[str, str, str, str, float | None]]:
    """(code, level, title, detail, metric_value) por cada regla que dispara."""
    ahora = utcnow()
    disparadas: list[tuple[str, str, str, str, float | None]] = []

    # 1) El webhook está mudo teniendo dinero en la calle.
    vivos = _contar(session, Cobro, Cobro.estado == "pendiente",
                    Cobro.habilitado == True)  # noqa: E712
    if vivos:
        corte = _naive(ahora - timedelta(hours=SIN_EVENTOS_HORAS))
        eventos_recientes = _contar(session, EventoPasarela,
                                    EventoPasarela.recibido_at >= corte)
        if not eventos_recientes:
            disparadas.append((
                "cobro:sin-eventos", "critical",
                "El webhook de Stripe no responde",
                f"Hay {vivos} cobro(s) esperando pago y no llegó NINGÚN evento de "
                f"Stripe en {SIN_EVENTOS_HORAS} h. Revisa que el endpoint siga dado "
                "de alta y que STRIPE_WEBHOOK_SECRET sea el del modo correcto.",
                float(vivos),
            ))

    # 2) Eventos que llegaron pero nadie pudo aplicar.
    corte = _naive(ahora - timedelta(hours=EVENTO_ATORADO_HORAS))
    atorados = _contar(session, EventoPasarela,
                       EventoPasarela.procesado_at.is_(None),  # type: ignore[union-attr]
                       EventoPasarela.recibido_at < corte)
    if atorados:
        disparadas.append((
            "cobro:eventos-sin-procesar", "warn",
            "Eventos de Stripe sin procesar",
            f"{atorados} evento(s) llevan más de {EVENTO_ATORADO_HORAS} h sin "
            "aplicarse. El barrido nocturno los reintenta; si no bajan, el "
            "aplicador está fallando.",
            float(atorados),
        ))

    # 3) Llamadas a Stripe que quedaron en "no sé".
    corte = _naive(ahora - timedelta(minutes=INTENTO_HUERFANO_MINUTOS))
    huerfanos = _contar(session, IntentoCobro,
                        IntentoCobro.estado == "enviando",
                        IntentoCobro.solicitado_at < corte)
    if huerfanos:
        disparadas.append((
            "cobro:intentos-huerfanos", "warn",
            "Llamadas a Stripe sin respuesta confirmada",
            f"{huerfanos} intento(s) llevan más de {INTENTO_HUERFANO_MINUTOS} min "
            "sin saber si Stripe creó el cobro. El barrido los resuelve con la "
            "misma clave de idempotencia; NO generes otro link mientras tanto.",
            float(huerfanos),
        ))

    # 4) …y los que el barrido ya no pudo resolver solo.
    irresolubles = _contar(session, IntentoCobro,
                           IntentoCobro.estado == "enviando",
                           IntentoCobro.reintentos >= INTENTO_IRRESOLUBLE_REINTENTOS)
    if irresolubles:
        disparadas.append((
            "cobro:intento-irresoluble", "critical",
            "Cobro en limbo: requiere revisión manual",
            f"{irresolubles} intento(s) no se resolvieron tras "
            f"{INTENTO_IRRESOLUBLE_REINTENTOS} barridos. Búscalos en el dashboard "
            "de Stripe por metadata antes de generar nada nuevo: crear a ciegas "
            "es exactamente el cobro duplicado.",
            float(irresolubles),
        ))

    # 5) Reloj desfasado = 403 en el 100% de los webhooks, con el sistema
    #    viéndose perfectamente sano.
    corte = _naive(ahora - timedelta(days=1))
    reciente = session.exec(
        select(EventoPasarela)
        .where(EventoPasarela.recibido_at >= corte)
        .where(EventoPasarela.stripe_created > 0)
        .order_by(EventoPasarela.recibido_at.desc())  # type: ignore[union-attr]
    ).first()
    if reciente is not None:
        desfase = abs(
            reciente.recibido_at.replace(tzinfo=timezone.utc).timestamp()
            - reciente.stripe_created
        )
        if desfase > DESFASE_RELOJ_SEGUNDOS:
            disparadas.append((
                "cobro:desfase-reloj", "critical",
                "El reloj del servidor está desfasado",
                f"El último evento llegó con {int(desfase)} s de diferencia contra "
                "el reloj de Stripe. Pasando de 300 s la verificación de firma "
                "rechaza TODOS los webhooks. Corre `timedatectl set-ntp true`.",
                float(int(desfase)),
            ))

    # 6) Doble cargo real (no replay): lo detecta el aplicador y queda escrito
    #    en el detalle del evento, que es lo que aquí se re-deriva.
    #
    #    Con ventana y filtrando primero por `resultado` (que SÍ está
    #    indexado). Sin ventana esto era doblemente malo: (a) eventopasarela es
    #    append-only, así que UN doble pago dejaba la alerta crítica encendida
    #    para siempre y el SEGUNDO caso, meses después, solo actualizaba la
    #    fila existente sin que nadie viera nada nuevo; (b) `detalle` no tiene
    #    índice y está detrás de payload_json (32 KB), o sea que comparar por
    #    ahí obliga a recorrer las páginas de overflow de la tabla entera.
    corte_transitorio = _naive(ahora - timedelta(days=VENTANA_TRANSITORIA_DIAS))
    dobles = _contar(session, EventoPasarela,
                     EventoPasarela.resultado == "ignorado",
                     EventoPasarela.recibido_at >= corte_transitorio,
                     EventoPasarela.detalle == "segundo pago sobre cobro pagado")
    if dobles:
        disparadas.append((
            "cobro:doble-pago", "critical",
            "Segundo pago sobre un cobro ya pagado",
            f"{dobles} evento(s) de pago cayeron sobre cobros ya pagados con otro "
            "instrumento. Es un cargo doble al cliente: revisa y reembolsa desde "
            "el dashboard de Stripe.",
            float(dobles),
        ))

    # 7) Pagaron un monto distinto al pedido.
    distintos = session.exec(
        select(Cobro)
        .where(Cobro.estado == "pagado")
        .where(Cobro.monto_pagado_centavos.is_not(None))  # type: ignore[union-attr]
        .where(Cobro.monto_pagado_centavos != Cobro.monto_centavos)
    ).all()
    if distintos:
        ejemplo = distintos[0]
        disparadas.append((
            "cobro:monto-distinto", "warn",
            "Pagos con monto distinto al solicitado",
            f"{len(distintos)} cobro(s) se pagaron con un monto distinto. Ej. folio "
            f"{ejemplo.ticket_id or 's/folio'}: se pidió "
            f"{formato_monto(ejemplo.monto_centavos, ejemplo.moneda)} y se cobró "
            f"{formato_monto(ejemplo.monto_pagado_centavos or 0, ejemplo.moneda_pagada or ejemplo.moneda)}.",
            float(len(distintos)),
        ))

    # 8) Entró el dinero con el folio ya cerrado: o el trabajo se entregó sin
    #    cobrar, o el cliente pagó algo que ya recibió y nadie le avisó.
    #    Con ventana sobre `pagado_at`: `aviso_pago_enviado_at` solo se escribe
    #    si el WhatsApp SALE, y la adenda A.4 decidió que sin plantilla HSM el
    #    bot no escribe fuera de la ventana de 24 h — que es exactamente este
    #    caso. Sin ventana, cada pago de OXXO tardío se quedaba contado para
    #    siempre aunque el asesor ya lo hubiera trabajado, y a los dos meses la
    #    fila decía "20 cobros" sin bajar nunca: para cuando llegara el que sí
    #    importa, nadie la estaría mirando.
    tras_cierre = session.exec(
        select(func.count())
        .select_from(Cobro)
        .join(Ticket, Ticket.id == Cobro.ticket_id)
        .where(Cobro.estado == "pagado")
        .where(Ticket.status == TicketStatus.CERRADO)
        .where(Cobro.aviso_pago_enviado_at.is_(None))  # type: ignore[union-attr]
        .where(Cobro.pagado_at >= corte_transitorio)
    ).one()
    if tras_cierre:
        disparadas.append((
            "cobro:pago-tras-cierre", "warn",
            "Pagos que entraron con el folio ya cerrado",
            f"{tras_cierre} cobro(s) se pagaron sobre folios cerrados y el cliente "
            "no ha recibido aviso. Revisa si hay que reabrir el trámite.",
            float(tras_cierre),
        ))

    # 9) Le pedimos morir a Stripe y no lo confirmó: el voucher SIGUE cobrable.
    pendientes_cancelar = _contar(session, Cobro,
                                  Cobro.estado == "cancelacion_solicitada")
    if pendientes_cancelar:
        disparadas.append((
            "cobro:cancelacion-pendiente", "warn",
            "Cancelaciones que Stripe no confirmó",
            f"{pendientes_cancelar} cobro(s) siguen con un instrumento cobrable "
            "vivo porque Stripe no confirmó la cancelación. El barrido reintenta "
            "cada noche; el candado no se libera hasta que confirme.",
            float(pendientes_cancelar),
        ))

    # 10) Eventos del modo equivocado (prueba contra producción o al revés).
    corte = _naive(ahora - timedelta(days=VENTANA_TRANSITORIA_DIAS))
    modo = _contar(session, EventoPasarela,
                   EventoPasarela.detalle == "modo no coincide",
                   EventoPasarela.recibido_at >= corte)
    if modo:
        disparadas.append((
            "cobro:modo-no-coincide", "warn",
            "Eventos de Stripe del modo equivocado",
            f"{modo} evento(s) llegaron en un modo distinto al de la llave "
            "configurada y se ignoraron. Revisa que no haya un endpoint de prueba "
            "apuntando a producción.",
            float(modo),
        ))

    # 11) Disputas: no cambian el estado del cobro, pero hay plazo para responder.
    disputados = _contar(session, Cobro, Cobro.disputado == True)  # noqa: E712
    if disputados:
        disparadas.append((
            "cobro:disputa", "critical",
            "Cargos disputados por el cliente",
            f"{disputados} cobro(s) tienen una disputa abierta. Stripe da un plazo "
            "corto para mandar evidencia; pasado eso el dinero se pierde.",
            float(disputados),
        ))

    # 12) El asesor se adelantó a Stripe (A.1 del diseño).
    corte = _naive(ahora - timedelta(hours=CONFIRMADO_SIN_STRIPE_HORAS))
    adelantados = _contar(session, Cobro,
                          Cobro.confirmado_por_asesor == True,  # noqa: E712
                          Cobro.estado != "pagado",
                          Cobro.confirmado_at < corte)
    if adelantados:
        disparadas.append((
            "cobro:confirmado-sin-stripe", "warn",
            "Confirmados por asesor que Stripe nunca confirmó",
            f"{adelantados} cobro(s) llevan más de {CONFIRMADO_SIN_STRIPE_HORAS} h "
            "marcados como confirmados por un asesor sin que Stripe registre el "
            "pago. Coteja la referencia del recibo contra el estado de cuenta.",
            float(adelantados),
        ))

    # 13) Eventos que no resolvieron a ningún cobro: dinero sin dueño.
    corte = _naive(ahora - timedelta(days=VENTANA_TRANSITORIA_DIAS))
    huerfanos_evt = _contar(session, EventoPasarela,
                            EventoPasarela.resultado == "huerfano",
                            EventoPasarela.recibido_at >= corte)
    if huerfanos_evt:
        disparadas.append((
            "cobro:evento-huerfano", "warn",
            "Eventos de Stripe sin cobro que los reclame",
            f"{huerfanos_evt} evento(s) no resolvieron a ningún cobro local. "
            "Nada se perdió (siguen en el ledger), pero revisa si falta una fila.",
            float(huerfanos_evt),
        ))

    # 14) Se está pidiendo MÁS de lo que el bot le cotizó al cliente.
    #
    #     `landing/terminos.html` §2 es un compromiso público: cada trámite se
    #     cotiza antes de iniciarse y los precios salen del catálogo vigente.
    #     `monto_cotizado_centavos` guarda el número que el cliente LEYÓ en
    #     WhatsApp, y el panel ya lo clasifica (`monto_origen = "override"`) y
    #     se lo enseña al asesor — pero solo al que teclea. Nadie más lo veía.
    #
    #     Se compara contra la columna y no contra `monto_origen`: el origen se
    #     congela al crear el cobro y el monto se puede editar después.
    #
    #     La ventana no es opcional: `monto_cotizado_centavos` nunca cambia, así
    #     que sin ella un override legítimo de hace un año dejaría la alerta
    #     encendida para siempre y el caso nuevo no se vería (reglas 6 y 8).
    sobre_cotizado = session.exec(
        select(Cobro)
        .where(Cobro.monto_cotizado_centavos.is_not(None))  # type: ignore[union-attr]
        .where(Cobro.monto_centavos > Cobro.monto_cotizado_centavos)
        .where(Cobro.estado.in_(ESTADOS_COBRABLES))  # type: ignore[attr-defined]
        .where(Cobro.created_at >= corte_transitorio)
        .order_by((Cobro.monto_centavos - Cobro.monto_cotizado_centavos).desc())
    ).all()
    if sobre_cotizado:
        peor = sobre_cotizado[0]
        cotizado = peor.monto_cotizado_centavos or 0
        disparadas.append((
            "cobro:monto-sobre-cotizado", "warn",
            "Cobros por arriba de lo que se le cotizó al cliente",
            f"{len(sobre_cotizado)} cobro(s) de los últimos {VENTANA_TRANSITORIA_DIAS} "
            f"días piden más de lo cotizado. El peor, folio "
            f"{peor.ticket_id or 's/folio'}: por WhatsApp se le dijo "
            f"{formato_monto(cotizado, peor.moneda)} y el link pide "
            f"{formato_monto(peor.monto_centavos, peor.moneda)} "
            f"({formato_monto(peor.monto_centavos - cotizado, peor.moneda)} de más). "
            "Si el trámite se encareció, avísale ANTES de que pague: los "
            "términos del sitio prometen la cotización por adelantado.",
            float(len(sobre_cotizado)),
        ))

    return disparadas
