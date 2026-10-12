"""Un cliente no abre folios repetidos ni sin tope (auditoría 2026-09, EXT-2).

`_handle_confirmar` creaba un folio por servicio confirmado sin mirar los
abiertos del mismo cliente: un número, 20 mensajes → 50 folios, cada uno
pasando por el reparto a asesores y por el espejo MSSQL, e inundando la
bandeja. El rate limit no lo frena: son mensajes legítimos en cantidad
legítima.

Todo por `handle_incoming`, el camino que no puede mentir: un helper que
filtre bien pero que nadie llame dejaría el alta igual de abierta. Los
servicios son reales del catálogo semilla, con los requisitos limpiados para
que la confirmación termine en `menu_categoria` y el siguiente pedido no caiga
en la captura de datos.

El bot NO cierra folios —eso es del asesor—; solo deja de multiplicarlos. Por
eso el control positivo es que, cerrado el folio por el asesor, el mismo
servicio vuelve a abrir uno.
"""

from __future__ import annotations

from sqlmodel import select

from app import conversation
from app.catalog import catalog
from app.conversation import MAX_FOLIOS_ABIERTOS, handle_incoming
from app.models import Conversation, Ticket, TicketStatus

PHONE = "5215500000000"
DIVORCIO, NACIMIENTO = "sb-acta-divorcio", "sb-acta-nacimiento"


def _sin_requisitos(*service_ids: str) -> None:
    for sid in service_ids:
        svc = catalog.service_by_id(sid)
        assert catalog.update_service(svc.id, svc.name, svc.price_info, requirements="", admin="prueba") is None


def _pedir(session, texto: str) -> list[str]:
    handle_incoming(session, PHONE, texto)
    return handle_incoming(session, PHONE, "sí")


def _folios(session, *, abiertos_solo: bool = False) -> list[Ticket]:
    consulta = select(Ticket).where(Ticket.phone == PHONE)
    if abiertos_solo:
        consulta = consulta.where(Ticket.status != TicketStatus.CERRADO, Ticket.habilitado == True)  # noqa: E712
    return list(session.exec(consulta.order_by(Ticket.id)).all())


def test_el_mismo_tramite_dos_veces_abre_un_solo_folio_y_dice_cual(session):
    """El PoC del informe: repetir «necesito un acta de divorcio» + «sí» abría
    un folio por vuelta. Ahora la segunda vuelta contesta con el número del
    folio que ya tiene y no toca la base."""
    _sin_requisitos(DIVORCIO)
    primera = _pedir(session, "Necesito un acta de divorcio")
    (folio,) = _folios(session)
    assert f"folio #{folio.id}" in primera[0]

    segunda = _pedir(session, "Necesito un acta de divorcio")

    assert _folios(session) == [folio], "el segundo «sí» abrió otro folio del mismo trámite"
    assert f"folio #{folio.id}" in segunda[0] and "Acta de divorcio" in segunda[0], segunda
    assert "Registré" not in segunda[0], "le dijo al cliente que registró algo que no registró"
    assert session.get(Conversation, PHONE).state == "menu_categoria"


def test_un_tramite_distinto_si_abre_folio_aunque_haya_otro_abierto(session):
    """Control positivo de la regla por servicio: sin él, un filtro que
    descartara TODO con folio abierto pasaría la prueba de arriba."""
    _sin_requisitos(DIVORCIO, NACIMIENTO)
    _pedir(session, "Necesito un acta de divorcio")
    respuesta = _pedir(session, "Necesito un acta de nacimiento")

    servicios = [t.service_id for t in _folios(session)]
    assert servicios == [DIVORCIO, NACIMIENTO]
    assert "Registré" in respuesta[0] and "Acta de nacimiento" in respuesta[0]


def test_cerrado_por_el_asesor_el_mismo_tramite_vuelve_a_abrir_folio(session):
    """El bot no cierra folios; el asesor sí, y desde ahí el cliente puede
    volver a pedir el mismo trámite. Un folio deshabilitado (borrado suave
    desde el panel) tampoco cuenta como abierto."""
    _sin_requisitos(DIVORCIO)
    _pedir(session, "Necesito un acta de divorcio")
    (primero,) = _folios(session)
    primero.status = TicketStatus.CERRADO
    session.add(primero)
    session.commit()

    _pedir(session, "Necesito un acta de divorcio")
    assert len(_folios(session)) == 2, "cerrado el folio, el mismo trámite no volvió a abrir"

    segundo = _folios(session)[-1]
    segundo.habilitado = False
    session.add(segundo)
    session.commit()
    _pedir(session, "Necesito un acta de divorcio")
    assert len(_folios(session)) == 3, "un folio deshabilitado bloqueó el alta"


def test_con_el_tope_de_abiertos_no_se_abre_uno_mas_y_se_le_dice(session):
    """El tope por cliente, sembrado por debajo de la conversación: cinco
    folios abiertos de otros servicios, y el sexto no entra ni pasa por el
    reparto. La respuesta lo dice y ofrece *estado*."""
    _sin_requisitos(DIVORCIO)
    for i in range(MAX_FOLIOS_ABIERTOS):
        session.add(Ticket(phone=PHONE, category="c", service_id=f"otro-{i}", service_name=f"Otro {i}",
                           price_info="$1", status=TicketStatus.TOMADO, assigned_to="ana"))
    session.commit()

    respuesta = _pedir(session, "Necesito un acta de divorcio")

    assert len(_folios(session, abiertos_solo=True)) == MAX_FOLIOS_ABIERTOS, "se abrió el folio de más"
    assert "máximo" in respuesta[0] and "*estado*" in respuesta[0], respuesta
    assert "Registré" not in respuesta[0]


def test_el_alta_multiple_recorta_al_cupo_y_avisa_de_lo_que_no_cupo(session, monkeypatch):
    """Con cupo para uno y dos servicios confirmados a la vez, se abre el
    primero y el segundo se avisa: nada se registra en silencio ni se
    pierde en silencio."""
    _sin_requisitos(DIVORCIO, NACIMIENTO)
    divorcio, nacimiento = catalog.service_by_id(DIVORCIO), catalog.service_by_id(NACIMIENTO)

    # Sin tope de por medio: el mismo servicio dos veces en UNA confirmación cuenta una.
    # (Con el tope en 1 esta mutación sobrevivía: el recorte escondía el duplicado.)
    nuevos, avisos = conversation._filtrar_folios_nuevos(session, PHONE, [divorcio, divorcio])
    assert [s.id for s in nuevos] == [DIVORCIO] and avisos == [], "el duplicado del mismo mensaje pasó"

    monkeypatch.setattr(conversation, "MAX_FOLIOS_ABIERTOS", 1)
    nuevos, avisos = conversation._filtrar_folios_nuevos(session, PHONE, [divorcio, nacimiento])

    assert [s.id for s in nuevos] == [DIVORCIO], "el cupo no recortó"
    assert len(avisos) == 1 and "máximo" in avisos[0]

    # Pide JUSTO lo que le cabe: se abre y NO se le dice que llegó al máximo. Un
    # `>=` en vez de `>` registraba el folio y a la vez avisaba «ya tienes el
    # máximo»: mensaje contradictorio (mutación que la refutación encontró viva).
    monkeypatch.setattr(conversation, "MAX_FOLIOS_ABIERTOS", 2)
    nuevos, avisos = conversation._filtrar_folios_nuevos(session, PHONE, [divorcio, nacimiento])
    assert [s.id for s in nuevos] == [DIVORCIO, NACIMIENTO] and avisos == [], avisos


def _sembrar_casos(session, cuantos: int) -> None:
    for _ in range(cuantos):
        session.add(Ticket(phone=PHONE, category="(análisis)", service_id="analisis", service_name="Caso para análisis",
                           price_info="N/A", status=TicketStatus.NUEVO, escalado=True, assigned_to="ana"))
    session.commit()


def test_los_casos_para_analisis_no_consumen_el_cupo_de_los_tramites(session):
    """Decisión del operador (2026-10-12): sin tope de tickets por cliente. Un gestor con más casos
    abiertos que el cupo no se puede quedar sin poder pedir un trámite. Si se pone roja, el cliente
    con seis casos oye «Tienes 6 solicitudes abiertas, que es el máximo a la vez» al pedir un acta."""
    _sin_requisitos(DIVORCIO)
    _sembrar_casos(session, MAX_FOLIOS_ABIERTOS + 1)

    respuesta = _pedir(session, "Necesito un acta de divorcio")

    assert "máximo" not in respuesta[0], respuesta
    assert [t.service_id for t in _folios(session) if t.service_id != "analisis"] == [DIVORCIO], "no se abrió"


def test_los_tramites_normales_siguen_topando_aunque_haya_casos(session):
    """El control del otro lado: lo que sale del cupo son los casos, no el tope. Con el cupo de
    trámites lleno y tres casos encima, el siguiente trámite no entra, y el aviso cuenta los
    trámites (5), no el total (8). Si se pone roja, o se quitó el tope de los trámites (el operador
    solo pidió quitarlo a los casos) o los casos volvieron a contar."""
    _sin_requisitos(DIVORCIO)
    for i in range(MAX_FOLIOS_ABIERTOS):
        session.add(Ticket(phone=PHONE, category="c", service_id=f"otro-{i}", service_name=f"Otro {i}",
                           price_info="$1", status=TicketStatus.TOMADO, assigned_to="ana"))
    _sembrar_casos(session, 3)

    respuesta = _pedir(session, "Necesito un acta de divorcio")

    assert "máximo" in respuesta[0] and "*estado*" in respuesta[0], respuesta
    assert f"Tienes {MAX_FOLIOS_ABIERTOS} solicitudes" in respuesta[0], "el aviso cuenta los casos"
    assert not [t for t in _folios(session) if t.service_id == DIVORCIO], "se abrió el trámite de más"


def test_con_el_cupo_a_medias_los_casos_no_lo_ocupan(session):
    """Cuatro trámites y seis casos: cabe uno más (el quinto trámite); el sexto ya no. Si se pone
    roja, el cupo se calcula sobre el total y el cliente se queda sin su quinto trámite."""
    _sin_requisitos(DIVORCIO, NACIMIENTO)
    for i in range(MAX_FOLIOS_ABIERTOS - 1):
        session.add(Ticket(phone=PHONE, category="c", service_id=f"otro-{i}", service_name=f"Otro {i}",
                           price_info="$1", status=TicketStatus.TOMADO, assigned_to="ana"))
    _sembrar_casos(session, 6)

    primero = _pedir(session, "Necesito un acta de divorcio")
    segundo = _pedir(session, "Necesito un acta de nacimiento")

    assert "máximo" not in primero[0], primero
    assert "máximo" in segundo[0], segundo
