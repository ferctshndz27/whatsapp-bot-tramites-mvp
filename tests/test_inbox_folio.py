"""El chat se acota al folio que se está atendiendo.

Un mensaje no guarda a qué folio pertenece (cuando el cliente pide el trámite el
folio todavía no existe), así que la pertenencia se deriva del tiempo entre
altas. Estas pruebas fijan las fronteras de esa derivación.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import app.db as db
from app.inbox import inbox_chat
from app.models import Direction, Message, Ticket


def _request(user: str = "ana") -> SimpleNamespace:
    return SimpleNamespace(session={"user": user}, url=SimpleNamespace(path="/inbox/000"))


def _hace(minutos: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutos)


def _msg(session, phone: str, body: str, minutos: float, direction=Direction.IN) -> None:
    session.add(
        Message(phone=phone, direction=direction, body=body, created_at=_hace(minutos))
    )


def _folio(session, phone: str, minutos: float, service_name: str = "CURP certificada") -> Ticket:
    ticket = Ticket(
        phone=phone,
        category="Servicios básicos",
        service_id="curp",
        service_name=service_name,
        price_info="$80",
        created_at=_hace(minutos),
    )
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def _bodies(response) -> list[str]:
    return [m.body for m in response.context["messages"]]


def test_por_defecto_solo_se_ve_la_conversacion_del_ultimo_folio(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _msg(session, "600", "quiero mi curp", minutos=200)
    _folio(session, "600", minutos=190)
    _msg(session, "600", "gracias", minutos=180)
    _msg(session, "600", "ahora necesito un acta", minutos=20)
    nuevo = _folio(session, "600", minutos=15, service_name="Acta de nacimiento")
    _msg(session, "600", "omitir", minutos=10)
    session.commit()

    response = inbox_chat(_request(), "600")

    assert _bodies(response) == ["ahora necesito un acta", "omitir"]
    assert [t.id for t in response.context["folios"]] == [nuevo.id]
    assert response.context["ocultos"] == 2  # los del folio viejo siguen ahí


def test_folio_en_la_url_abre_ese_folio_y_no_el_ultimo(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _msg(session, "601", "quiero mi curp", minutos=200)
    viejo = _folio(session, "601", minutos=190)
    _msg(session, "601", "gracias", minutos=180)
    _msg(session, "601", "ahora necesito un acta", minutos=20)
    _folio(session, "601", minutos=15, service_name="Acta de nacimiento")
    session.commit()

    response = inbox_chat(_request(), "601", folio=viejo.id)

    assert _bodies(response) == ["quiero mi curp", "gracias"]
    assert response.context["ticket"].id == viejo.id


def test_dos_folios_del_mismo_turno_comparten_la_conversacion(monkeypatch, session):
    """«quiero un acta y un rfc» levanta dos folios con segundos de diferencia.

    Si cada uno cortara por su cuenta, el segundo abriría vacío."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _msg(session, "602", "quiero un acta y un rfc", minutos=30)
    acta = _folio(session, "602", minutos=25, service_name="Acta de nacimiento")
    rfc = _folio(session, "602", minutos=24.9, service_name="RFC")
    _msg(session, "602", "omitir", minutos=20)
    session.commit()

    for elegido in (acta, rfc):
        response = inbox_chat(_request(), "602", folio=elegido.id)
        assert _bodies(response) == ["quiero un acta y un rfc", "omitir"]
        assert {t.id for t in response.context["folios"]} == {acta.id, rfc.id}


def test_la_ventana_de_24h_se_calcula_sobre_todo_el_historial(monkeypatch, session):
    """Abrir un folio viejo no debe apagar el compositor de un chat vivo: la
    ventana de 24 h es del número, no del folio."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _msg(session, "603", "quiero mi curp", minutos=60 * 40)
    viejo = _folio(session, "603", minutos=60 * 39)
    _msg(session, "603", "sigo esperando", minutos=5)
    _folio(session, "603", minutos=4, service_name="Acta de nacimiento")
    session.commit()

    response = inbox_chat(_request(), "603", folio=viejo.id)

    assert _bodies(response) == ["quiero mi curp"]  # acotado
    assert response.context["window_open"] is True  # pero se puede contestar


def test_numero_sin_folios_muestra_todo(monkeypatch, session):
    """Los números que llegan desde /unmatched no tienen folio: si se acotara,
    la vista saldría vacía."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _msg(session, "604", "ola k ase", minutos=30)
    _msg(session, "604", "no entendí", minutos=29, direction=Direction.OUT)
    session.commit()

    response = inbox_chat(_request(), "604")

    assert _bodies(response) == ["ola k ase", "no entendí"]
    assert response.context["ver_todo"] is True
    assert response.context["ocultos"] == 0


def test_todo_1_devuelve_la_conversacion_completa(monkeypatch, session):
    monkeypatch.setattr(db, "engine", session.get_bind())
    _msg(session, "605", "quiero mi curp", minutos=200)
    _folio(session, "605", minutos=190)
    _msg(session, "605", "ahora necesito un acta", minutos=20)
    _folio(session, "605", minutos=15, service_name="Acta de nacimiento")
    session.commit()

    response = inbox_chat(_request(), "605", todo=1)

    assert _bodies(response) == ["quiero mi curp", "ahora necesito un acta"]
    assert response.context["ver_todo"] is True


def test_un_folio_de_otro_numero_no_abre_su_conversacion(monkeypatch, session):
    """?folio se resuelve DENTRO de los folios del número: un id ajeno se ignora
    y cae al folio más reciente de este chat, no filtra el otro."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    _msg(session, "606", "hola soy el 606", minutos=30)
    _folio(session, "606", minutos=25)
    _msg(session, "607", "hola soy el 607", minutos=30)
    ajeno = _folio(session, "607", minutos=25)
    session.commit()

    response = inbox_chat(_request(), "606", folio=ajeno.id)

    assert _bodies(response) == ["hola soy el 606"]
    assert response.context["ticket"].phone == "606"
