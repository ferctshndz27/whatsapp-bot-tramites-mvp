"""Panel de cobranza por la URL real. Diseño §9.9-12, 32-34, 41-46.

Lo que se defiende aquí es que el asesor NO pueda generar dos instrumentos
cobrables para el mismo cliente-trámite, ni con F5, ni con dos pestañas, ni
con el folio rotado, ni cancelando sin que Stripe confirme.
"""

from datetime import timedelta
from itertools import count

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.cobranza_panel as panel
import app.db as db
import app.stripe_client as stripe_client
from app.config import settings
from app.main import app
from app.models import (
    Client,
    Cobro,
    Direction,
    IntentoCobro,
    Message,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
    utcnow,
)

CLAVE = "contrasena-larga-123"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt()).decode()
ASESOR = "ana"
OTRO = "beto"
ADMIN = "jefa"
PHONE = "5215511112222"

SESION_STRIPE = {
    "object": "checkout.session", "id": "cs_panel",
    "url": "https://checkout.stripe.com/c/pay/cs_panel",
    "payment_intent": "pi_panel", "livemode": False,
}


def _sesion_ok(objeto: dict) -> stripe_client.RespuestaStripe:
    return stripe_client.RespuestaStripe(ok=True, http_status=200, objeto=objeto)


@pytest.fixture()
def entorno(_isolated_db, monkeypatch):
    # StaticPool: TestClient atiende en el hilo del portal de anyio y sin él
    # ese hilo vería una base :memory: distinta y VACÍA.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")  # apaga el espejo
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_panel")
    monkeypatch.setattr(settings, "public_base_url", "https://panel.example")

    with Session(engine) as s:
        s.add(User(username=ASESOR, password_hash=HASH_CLAVE, is_admin=False))
        s.add(User(username=OTRO, password_hash=HASH_CLAVE, is_admin=False))
        s.add(User(username=ADMIN, password_hash=HASH_CLAVE, is_admin=True))
        s.add(Client(phone=PHONE, name="Cliente Prueba"))
        s.add(Ticket(id=1, phone=PHONE, client_name="Cliente Prueba",
                     category="Servicios básicos", service_id="curp",
                     service_name="CURP certificada", price_info="$800",
                     status=TicketStatus.NUEVO, assigned_to=ASESOR))
        # Mensaje IN reciente: abre la ventana de 24 h para que el link salga.
        s.add(Message(phone=PHONE, direction=Direction.IN, body="hola"))
        s.commit()
    yield engine


@pytest.fixture()
def stripe_doble(monkeypatch):
    """Cuenta las llamadas y evita cualquier red.

    Los ids son únicos por llamada, como los de Stripe: reusarlos chocaría
    contra ux_cobro_session y escondería lo que la prueba quiere ver.
    """
    llamadas: dict[str, list] = {"crear": []}

    def _crear(**kw):
        n = len(llamadas["crear"])
        llamadas["crear"].append(kw)
        return _sesion_ok({
            **SESION_STRIPE,
            "id": f"cs_panel_{n}",
            "url": f"{SESION_STRIPE['url']}_{n}",
            "payment_intent": f"pi_panel_{n}",
        })

    enviados = {"n": 0}

    def _wamid(*a, **kw):
        enviados["n"] += 1
        return f"wamid.{enviados['n']}"  # unique: message.wa_message_id es UNIQUE

    monkeypatch.setattr(panel.stripe_client, "crear_checkout_session", _crear)
    monkeypatch.setattr(panel.whatsapp_client, "send_text", _wamid)
    return llamadas


def _cliente(username: str) -> TestClient:
    c = TestClient(app, base_url="https://testserver")
    r = c.post("/login", data={"username": username, "password": CLAVE},
               follow_redirects=False)
    assert r.status_code == 303, "el login de la prueba falló"
    return c


def _generar(cliente: TestClient, token: str = "tok-1", ticket_id: int = 1, **extra):
    data = {"monto": "800.00", "concepto": "CURP", "token_form": token,
            "metodo": ["card"]}
    data.update(extra)
    return cliente.post(f"/tickets/{ticket_id}/cobro", data=data, follow_redirects=False)


# ---------------------------------------------------------------------------


def test_9_doble_submit_con_el_mismo_token_no_duplica(entorno, stripe_doble):
    """PREVIENE el F5: el segundo envío choca contra ux_cobro_generacion, no
    crea una segunda fila, NO devuelve error y solo hay UNA llamada a Stripe."""
    cliente = _cliente(ASESOR)
    primera = _generar(cliente, token="mismo-token")
    segunda = _generar(cliente, token="mismo-token")

    assert primera.status_code == 303
    assert segunda.status_code == 303
    assert "err=" not in segunda.headers["location"]
    with Session(entorno) as s:
        assert len(s.exec(select(Cobro)).all()) == 1
    assert len(stripe_doble["crear"]) == 1


def test_10_segundo_cobro_del_mismo_servicio_es_rechazado(entorno, stripe_doble):
    """Dos pestañas (tokens distintos) sobre el mismo (teléfono, servicio):
    el candado lo corta con 303 ?err=cobro-vivo, NUNCA con un 500, y no se
    llama a Stripe la segunda vez."""
    cliente = _cliente(ASESOR)
    _generar(cliente, token="tok-a")
    segunda = _generar(cliente, token="tok-b")

    assert segunda.status_code == 303
    assert "err=cobro-vivo" in segunda.headers["location"]
    assert len(stripe_doble["crear"]) == 1
    with Session(entorno) as s:
        assert len(s.exec(select(Cobro)).all()) == 1


def test_11_folio_cerrado_con_voucher_vivo_bloquea_el_folio_nuevo(entorno, stripe_doble):
    """PREVIENE el folio que rota: con OXXO el folio se cierra y el bot crea
    otro. El candado va por (teléfono, servicio), así que el folio nuevo del
    MISMO servicio se bloquea — y el de OTRO servicio sí se permite."""
    cliente = _cliente(ASESOR)
    _generar(cliente, token="tok-viejo")

    with Session(entorno) as s:
        viejo = s.get(Ticket, 1)
        viejo.status = TicketStatus.CERRADO
        s.add(viejo)
        s.add(Ticket(id=2, phone=PHONE, client_name="Cliente Prueba",
                     category="Servicios básicos", service_id="curp",
                     service_name="CURP certificada", price_info="$800",
                     status=TicketStatus.NUEVO, assigned_to=ASESOR))
        s.add(Ticket(id=3, phone=PHONE, client_name="Cliente Prueba",
                     category="Servicios básicos", service_id="acta",
                     service_name="Acta de nacimiento", price_info="$300",
                     status=TicketStatus.NUEVO, assigned_to=ASESOR))
        s.commit()

    mismo = _generar(cliente, token="tok-nuevo", ticket_id=2)
    assert "err=cobro-vivo" in mismo.headers["location"]

    otro = _generar(cliente, token="tok-otro", ticket_id=3)
    assert "err=" not in otro.headers["location"]
    with Session(entorno) as s:
        assert len(s.exec(select(Cobro)).all()) == 2


def test_12_la_clave_de_idempotencia_se_commitea_antes_de_la_red(entorno, monkeypatch):
    """Si el proceso muere durante el POST, la fila del intento con su clave
    YA está en disco: por eso el barrido puede reintentar sin duplicar."""
    def _explota(**kw):
        raise RuntimeError("el proceso murió durante la llamada")

    monkeypatch.setattr(panel.stripe_client, "crear_checkout_session", _explota)
    cliente = _cliente(ASESOR)
    with pytest.raises(RuntimeError):
        _generar(cliente)

    with Session(entorno) as s:
        cobro = s.exec(select(Cobro)).one()
        intento = s.exec(select(IntentoCobro)).one()
        assert cobro.estado == "borrador"
        assert intento.clave_idempotencia == f"cobro:{cobro.id}:1"
        assert intento.estado == "enviando"  # "no sé qué pasó"


def test_32_cancelacion_sin_confirmar_no_libera_el_candado(entorno, stripe_doble,
                                                           monkeypatch):
    """PREVIENE los dos instrumentos vivos: Stripe rechaza la cancelación, el
    cobro queda en cancelacion_solicitada, el candado SIGUE puesto y no se
    puede generar otro."""
    cliente = _cliente(ASESOR)
    _generar(cliente, token="tok-cancel")
    with Session(entorno) as s:
        cobro_id = s.exec(select(Cobro)).one().id

    monkeypatch.setattr(
        panel.stripe_client, "cancelar_payment_intent",
        lambda pi, clave_idempotencia=None: stripe_client.RespuestaStripe(
            ok=False, http_status=500, error_codigo="api_error", indeterminado=True),
    )
    r = cliente.post(f"/cobros/{cobro_id}/cancelar", follow_redirects=False)
    assert "err=cancelacion-pendiente" in r.headers["location"]

    with Session(entorno) as s:
        cobro = s.get(Cobro, cobro_id)
        assert cobro.estado == "cancelacion_solicitada"
        assert cobro.llave_cobro_vivo is not None  # el voucher sigue cobrable

    segunda = _generar(cliente, token="tok-post-cancel")
    assert "err=cobro-vivo" in segunda.headers["location"]


def test_33_cancelacion_confirmada_libera_y_permite_otro(entorno, stripe_doble,
                                                         monkeypatch):
    cliente = _cliente(ASESOR)
    _generar(cliente, token="tok-c1")
    with Session(entorno) as s:
        cobro_id = s.exec(select(Cobro)).one().id

    monkeypatch.setattr(
        panel.stripe_client, "cancelar_payment_intent",
        lambda pi, clave_idempotencia=None: _sesion_ok({
            "object": "payment_intent", "id": pi, "status": "canceled"}),
    )
    r = cliente.post(f"/cobros/{cobro_id}/cancelar", follow_redirects=False)
    assert "ok=cancelado" in r.headers["location"]

    with Session(entorno) as s:
        cobro = s.get(Cobro, cobro_id)
        assert cobro.estado == "cancelado"
        assert cobro.llave_cobro_vivo is None
        assert cobro.cancelado_por == ASESOR

    # Con el candado liberado sí se puede generar el reemplazo. No se limpia
    # la lista: el contador es lo que hace únicos los ids del doble.
    antes = len(stripe_doble["crear"])
    segunda = _generar(cliente, token="tok-c2")
    assert "err=" not in segunda.headers["location"]
    assert len(stripe_doble["crear"]) == antes + 1


def test_34_sin_payment_intent_se_expira_la_sesion(entorno, stripe_doble, monkeypatch):
    """Sin instrumento emitido no hay voucher que matar: se expira la sesión y
    NO se llama a /payment_intents/cancel."""
    llamadas = {"expirar": 0, "cancelar": 0}

    def _no_debe_llamarse(pi, clave_idempotencia=None):
        llamadas["cancelar"] += 1
        raise AssertionError("no debe cancelarse un PI que no existe")

    def _expirar(cs, clave_idempotencia=None):
        llamadas["expirar"] += 1
        return _sesion_ok({"object": "checkout.session", "id": cs, "status": "expired"})

    monkeypatch.setattr(panel.stripe_client, "cancelar_payment_intent", _no_debe_llamarse)
    monkeypatch.setattr(panel.stripe_client, "expirar_sesion", _expirar)

    with Session(entorno) as s:
        s.add(Cobro(id=99, ticket_id=1, phone=PHONE, concepto="CURP",
                    monto_centavos=80000, estado="pendiente",
                    llave_cobro_vivo=f"{PHONE}:svc:curp",
                    token_verificacion="tok-sin-pi", creado_por=ASESOR,
                    stripe_checkout_session_id="cs_sin_pi"))
        s.commit()

    cliente = _cliente(ASESOR)
    cliente.post("/cobros/99/cancelar", follow_redirects=False)

    assert llamadas["expirar"] == 1
    assert llamadas["cancelar"] == 0


def test_41_solo_el_asignado_creado_por_o_admin_pueden(entorno, stripe_doble):
    """Un colaborador ajeno no genera nada (sonda: el conteo no cambia). El
    creado_por sí puede aunque el folio ya se haya soltado — un voucher de 3
    días sobrevive a release_ticket."""
    ajeno = _cliente(OTRO)
    ajeno_r = _generar(ajeno, token="tok-ajeno")
    assert ajeno_r.status_code == 303
    with Session(entorno) as s:
        assert s.exec(select(Cobro)).all() == []

    asesor = _cliente(ASESOR)
    _generar(asesor, token="tok-propio")
    with Session(entorno) as s:
        cobro_id = s.exec(select(Cobro)).one().id
        ticket = s.get(Ticket, 1)
        ticket.assigned_to = None  # se suelta el folio
        s.add(ticket)
        s.commit()

    r = asesor.post(f"/cobros/{cobro_id}/enviar", follow_redirects=False)
    assert "err=" not in r.headers["location"]


def test_43_no_se_cierra_a_ciegas_con_un_cobro_vivo(entorno, stripe_doble, monkeypatch):
    """Cerrar con dinero en la calle exige confirmación explícita, y el texto
    que recibe el cliente cambia: felicitarlo sin mencionar el pago es
    regalarle el argumento para no pagar."""
    import app.dashboard as dashboard

    enviados: list[str] = []
    # Contador aparte de la lista: `enviados` se limpia a media prueba y el
    # wamid tiene que seguir siendo único (message.wa_message_id es UNIQUE).
    contador = count(1)

    def _capturar(phone, body):
        enviados.append(body)
        return f"wamid.cierre-{next(contador)}"

    monkeypatch.setattr(panel.whatsapp_client, "send_text", _capturar)
    monkeypatch.setattr(dashboard.whatsapp_client, "send_text", _capturar)
    monkeypatch.setattr(dashboard.whatsapp_client, "send_buttons",
                        lambda *a, **kw: "wamid.botones")

    cliente = _cliente(ASESOR)
    _generar(cliente, token="tok-cierre")
    enviados.clear()

    sin_confirmar = cliente.post("/tickets/1/finish", data={}, follow_redirects=False)
    assert "err=cobro-vivo-al-cerrar" in sin_confirmar.headers["location"]
    with Session(entorno) as s:
        assert s.get(Ticket, 1).status != TicketStatus.CERRADO

    confirmado = cliente.post("/tickets/1/finish", data={"confirm": "1"},
                              follow_redirects=False)
    assert confirmado.status_code == 303
    with Session(entorno) as s:
        assert s.get(Ticket, 1).status == TicketStatus.CERRADO
        acciones = [a.action for a in s.exec(select(TicketActivity)).all()]
        assert "cobro:cierre-con-pago-pendiente" in acciones

    aviso = " ".join(enviados)
    assert "pago pendiente" in aviso
    assert "quedó terminada" not in aviso


def test_45_ventana_cerrada_genera_el_cobro_pero_no_manda_nada(entorno, stripe_doble,
                                                               monkeypatch):
    """Que no se pueda avisar por WhatsApp NUNCA afecta el estado del cobro:
    el link queda guardado y el panel ofrece copiarlo."""
    enviados = []
    monkeypatch.setattr(panel.whatsapp_client, "send_text",
                        lambda *a, **kw: enviados.append(a) or "wamid.X")

    with Session(entorno) as s:
        viejo = s.exec(select(Message)).one()
        viejo.created_at = utcnow() - timedelta(hours=30)
        s.add(viejo)
        s.commit()

    cliente = _cliente(ASESOR)
    r = _generar(cliente, token="tok-ventana")

    assert "err=ventana" in r.headers["location"]
    assert enviados == []
    with Session(entorno) as s:
        cobro = s.exec(select(Cobro)).one()
        assert cobro.estado == "pendiente"
        assert cobro.url_pago.startswith(SESION_STRIPE["url"])
        assert cobro.enviado_at is None


def test_46_toda_accion_de_cobranza_tiene_texto_legible():
    """PREVIENE que /activity muestre el slug crudo (app/activity.py cae a
    `return action` si falta la clave)."""
    from app.activity import ACTION_LABELS, _label

    acciones = [
        "cobro:generado", "cobro:enviado", "cobro:voucher", "cobro:pagado",
        "cobro:fallido", "cobro:vencido", "cobro:cancelado",
        "cobro:cancelacion-pendiente", "cobro:confirmado-asesor",
        "cobro:cierre-con-pago-pendiente",
    ]
    for accion in acciones:
        assert accion in ACTION_LABELS, accion
        assert _label(accion) != accion, accion


def test_confirmado_por_asesor_no_marca_pagado(entorno, stripe_doble):
    """El rango 4 es EXCLUSIVO de Stripe: la bandera del asesor desbloquea el
    trabajo pero no puede inventar un pago."""
    cliente = _cliente(ASESOR)
    _generar(cliente, token="tok-conf")
    with Session(entorno) as s:
        cobro_id = s.exec(select(Cobro)).one().id

    r = cliente.post(f"/cobros/{cobro_id}/confirmar",
                     data={"referencia": "OXXO-4471"}, follow_redirects=False)
    assert "ok=confirmado" in r.headers["location"]

    with Session(entorno) as s:
        cobro = s.get(Cobro, cobro_id)
        assert cobro.confirmado_por_asesor is True
        assert cobro.confirmado_por == ASESOR
        assert cobro.referencia_recibo == "OXXO-4471"
        assert cobro.estado == "pendiente"  # NO pagado


def test_monto_invalido_no_escribe_nada(entorno, stripe_doble):
    cliente = _cliente(ASESOR)
    r = _generar(cliente, token="tok-malo", monto="mil pesos")

    assert "err=monto" in r.headers["location"]
    assert stripe_doble["crear"] == []
    with Session(entorno) as s:
        assert s.exec(select(Cobro)).all() == []


def test_oxxo_sobre_el_tope_se_rechaza_antes_de_stripe(entorno, stripe_doble):
    """El voucher no se emitiría: se corta aquí en vez de gastar una llamada
    y dejar un cobro en borrador."""
    cliente = _cliente(ASESOR)
    r = _generar(cliente, token="tok-oxxo", monto="15000.00", metodo=["card", "oxxo"])

    assert "err=monto-oxxo" in r.headers["location"]
    assert stripe_doble["crear"] == []
    with Session(entorno) as s:
        assert s.exec(select(Cobro)).all() == []


def test_sin_stripe_configurado_avisa_y_no_escribe(entorno, stripe_doble, monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", "")
    cliente = _cliente(ASESOR)
    r = _generar(cliente, token="tok-sin-stripe")

    assert "err=sin-stripe" in r.headers["location"]
    with Session(entorno) as s:
        assert s.exec(select(Cobro)).all() == []
