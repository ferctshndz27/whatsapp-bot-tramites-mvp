"""Cobro manual (/cobros/nuevo): el colaborador genera la liga de Stripe y la manda él.

Lo que se defiende: que buscar/registrar al cliente no abra una puerta (otra oficina, bloqueado),
que la liga salga por el MISMO camino que el cobro de un folio (un candado, una llamada a Stripe),
que el bot no mande nada por su cuenta, y que quede rastro de quién copió la liga.
"""

import re

import pytest
from sqlmodel import Session, func, select

import app.cobranza_panel as panel
from app.models import AdminAudit, Client, ClienteAsesor, Cobro, Message, Ticket
from tests.test_cobranza_panel import (  # noqa: F401  (fixtures y ayudas del panel de cobranza)
    ADMIN,
    ASESOR,
    OTRO,
    PHONE as PHONE_FOLIO,
    _cliente,
    entorno,
    stripe_doble,
)

# El teléfono canónico de la ficha (normalize_phone quita el «1» de móvil): el de las pruebas del
# folio (5215511112222) es el de un mensaje entrante, no el que guarda /clients.
PHONE = "525511112222"
NUEVO = "525533334444"


@pytest.fixture(autouse=True)
def _ficha_canonica(entorno):
    with Session(entorno) as s:
        s.delete(s.get(Client, PHONE_FOLIO))
        s.add(Client(phone=PHONE, name="Cliente Prueba"))
        s.commit()


def _crear(cliente, token="tok-1", **extra):
    data = {"phone": PHONE, "concepto": "Apostilla urgente 77", "monto": "800.00",
            "metodo": ["card"], "token_form": token}
    data.update(extra)
    return cliente.post("/cobros/nuevo", data=data, follow_redirects=False)


def _auditoria(entorno, accion):
    with Session(entorno) as s:
        return s.exec(select(AdminAudit).where(AdminAudit.action == accion)).all()


# ---------------------------------------------------------------------------
# Generar la liga
# ---------------------------------------------------------------------------


def test_cliente_registrado_genera_liga_sin_folio_y_sin_mandar_whatsapp(entorno, stripe_doble):
    r = _crear(_cliente(ASESOR))
    assert r.status_code == 303, re.findall(r"class=\"error\"[^>]*>([^<]*)", r.text)
    assert r.headers["location"].startswith("/cobros/nuevo?cobro=")
    assert len(stripe_doble["crear"]) == 1
    with Session(entorno) as s:
        cobro = s.exec(select(Cobro)).one()
        assert cobro.ticket_id is None and cobro.origen == "manual"
        assert cobro.creado_por == ASESOR and cobro.phone == PHONE
        assert cobro.estado == "pendiente" and cobro.url_pago
        assert cobro.enviado_at is None, "el bot no manda: la liga la pega el colaborador"
        assert s.exec(select(Message).where(Message.direction == "out")).all() == []
    assert len(_auditoria(entorno, "cobro:manual-generado")) == 1


def test_la_pantalla_del_resultado_trae_la_liga_y_los_botones_de_copiar(entorno, stripe_doble):
    c = _cliente(ASESOR)
    destino = _crear(c).headers["location"]
    html = c.get(destino).text
    assert "Liga lista" in html
    assert "https://checkout.stripe.com/c/pay/cs_panel_0" in html
    assert html.count("data-cn-copiar=") >= 2 and "Copiar mensaje" in html


def test_doble_envio_con_el_mismo_token_no_duplica(entorno, stripe_doble):
    c = _cliente(ASESOR)
    _crear(c, token="igual")
    segunda = _crear(c, token="igual")
    assert segunda.status_code == 303
    with Session(entorno) as s:
        assert s.exec(select(func.count()).select_from(Cobro)).one() == 1, "el doble envío duplicó el cobro"
    assert len(stripe_doble["crear"]) == 1


def test_segundo_cobro_manual_vivo_al_mismo_cliente_se_rechaza_con_texto_claro(entorno, stripe_doble):
    c = _cliente(ASESOR)
    _crear(c, token="a")
    r = _crear(c, token="b")
    assert r.status_code == 422
    assert "ya tiene una liga manual sin pagar" in r.text
    assert len(stripe_doble["crear"]) == 1


@pytest.mark.parametrize("campos,esperado", [
    ({"monto": "abc"}, "El monto no es válido"),
    ({"monto": "0.50"}, "menor al mínimo"),
    ({"concepto": "  "}, "Escribe el servicio"),
    ({"phone": "12"}, "teléfono válido"),
    ({"metodo": ["oxxo"], "monto": "20000.00"}, "OXXO no acepta"),
])
def test_entradas_malas_vuelven_al_formulario_con_lo_tecleado_y_sin_tocar_stripe(
    entorno, stripe_doble, campos, esperado
):
    r = _crear(_cliente(ASESOR), **campos)
    assert r.status_code == 422 and esperado in r.text
    assert "Apostilla urgente 77" in r.text or "concepto" in campos
    assert stripe_doble["crear"] == []
    with Session(entorno) as s:
        assert s.exec(select(Cobro)).all() == []


# ---------------------------------------------------------------------------
# Cliente: buscar y registrar
# ---------------------------------------------------------------------------


def test_cliente_nuevo_se_registra_en_su_oficina_y_entra_a_la_cartera(entorno, stripe_doble):
    r = _crear(_cliente(ASESOR), phone="55 3333 4444", name="Luis Pérez", cartera="1")
    assert r.status_code == 303
    with Session(entorno) as s:
        cliente = s.get(Client, NUEVO)
        assert cliente is not None and cliente.name == "Luis Pérez"
        assert s.exec(select(Cobro)).one().client_name == "Luis Pérez"
        filas = s.exec(select(ClienteAsesor).where(ClienteAsesor.phone == NUEVO)).all()
        assert [f.username for f in filas if f.habilitado] == [ASESOR]
    assert len(_auditoria(entorno, "cliente:alta")) == 1


def test_cliente_nuevo_sin_casilla_de_cartera_no_entra_a_ella(entorno, stripe_doble):
    _crear(_cliente(ASESOR), phone=NUEVO, name="Luis Pérez")
    with Session(entorno) as s:
        assert s.exec(select(ClienteAsesor).where(ClienteAsesor.phone == NUEVO)).all() == []


def test_cliente_nuevo_sin_nombre_no_se_registra_ni_cobra(entorno, stripe_doble):
    r = _crear(_cliente(ASESOR), phone=NUEVO, name="")
    assert r.status_code == 422 and "escribe su nombre" in r.text
    with Session(entorno) as s:
        assert s.get(Client, NUEVO) is None and s.exec(select(Cobro)).all() == []


def test_monto_malo_no_deja_un_cliente_registrado_a_medias(entorno, stripe_doble):
    _crear(_cliente(ASESOR), phone=NUEVO, name="Luis", monto="abc")
    with Session(entorno) as s:
        assert s.get(Client, NUEVO) is None


def test_cliente_existente_se_agrega_a_mi_cartera_si_marco_la_casilla(entorno, stripe_doble):
    _crear(_cliente(ASESOR), cartera="1")
    with Session(entorno) as s:
        filas = s.exec(select(ClienteAsesor).where(ClienteAsesor.phone == PHONE)).all()
        assert [f.username for f in filas] == [ASESOR]


def test_cliente_bloqueado_no_se_cobra(entorno, stripe_doble):
    with Session(entorno) as s:
        s.get(Client, PHONE).habilitado = False
        s.commit()
    r = _crear(_cliente(ASESOR))
    assert r.status_code == 422 and "bloqueado" in r.text
    assert stripe_doble["crear"] == []


def test_cliente_de_otra_oficina_responde_como_ajeno(entorno, stripe_doble):
    with Session(entorno) as s:
        s.add(Client(phone=NUEVO, name="Ajeno", office_id=2))
        s.commit()
    r = _crear(_cliente(ASESOR), phone=NUEVO)
    assert r.status_code == 422 and "otra oficina" in r.text
    assert stripe_doble["crear"] == []


def test_buscador_encuentra_por_nombre_alias_y_telefono_con_formato_distinto(entorno):
    with Session(entorno) as s:
        s.get(Client, PHONE).alias = "la señora de las actas"
        s.commit()
    c = _cliente(ASESOR)
    for q in ("Prueba", "señora", "55 1111 2222", "+52 1 55 1111 2222"):
        datos = c.get("/cobros/nuevo/clientes", params={"q": q}).json()
        assert [x["phone"] for x in datos["clientes"]] == [PHONE], q


def test_buscador_marca_los_de_mi_cartera_y_los_pone_primero(entorno):
    with Session(entorno) as s:
        s.add(Client(phone=NUEVO, name="Cliente Prueba Dos"))
        s.commit()
        s.add(ClienteAsesor(phone=NUEVO, username=ASESOR))
        s.commit()
    datos = _cliente(ASESOR).get("/cobros/nuevo/clientes", params={"q": "Prueba"}).json()["clientes"]
    assert [d["phone"] for d in datos] == [NUEVO, PHONE]
    assert [d["mio"] for d in datos] == [True, False]


def test_buscador_no_devuelve_clientes_de_otra_oficina_ni_con_menos_de_2_letras(entorno):
    with Session(entorno) as s:
        s.add(Client(phone=NUEVO, name="Prueba Ajena", office_id=2))
        s.commit()
    c = _cliente(ASESOR)
    assert [d["phone"] for d in c.get("/cobros/nuevo/clientes", params={"q": "Prueba"}).json()["clientes"]] == [PHONE]
    assert c.get("/cobros/nuevo/clientes", params={"q": "P"}).json()["clientes"] == []


def test_buscador_sin_sesion_no_filtra_datos(entorno):
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app, base_url="https://testserver").get("/cobros/nuevo/clientes", params={"q": "Prueba"})
    assert r.status_code == 401 and "Cliente" not in r.text


def test_la_pagina_sin_sesion_manda_al_login(entorno):
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app, base_url="https://testserver").get("/cobros/nuevo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Historial y rastro de la copia
# ---------------------------------------------------------------------------


def test_cada_colaborador_ve_solo_sus_cobros_manuales_y_el_admin_los_de_la_oficina(entorno, stripe_doble):
    _crear(_cliente(ASESOR))
    assert "Apostilla urgente 77" in _cliente(ASESOR).get("/cobros/nuevo").text
    assert "Apostilla urgente 77" not in _cliente(OTRO).get("/cobros/nuevo").text
    assert "Apostilla urgente 77" in _cliente(ADMIN).get("/cobros/nuevo").text


def test_no_se_abre_la_liga_de_otro_con_el_id_en_la_url(entorno, stripe_doble):
    destino = _crear(_cliente(ASESOR)).headers["location"]
    assert "Liga lista" not in _cliente(OTRO).get(destino).text


def test_copiar_la_liga_deja_rastro_y_no_marca_enviado(entorno, stripe_doble):
    c = _cliente(ASESOR)
    cobro_id = int(_crear(c).headers["location"].rsplit("=", 1)[1])
    r = c.post(f"/cobros/{cobro_id}/liga-copiada")
    assert r.status_code == 204
    assert len(_auditoria(entorno, "cobro:liga-copiada")) == 1
    with Session(entorno) as s:
        assert s.get(Cobro, cobro_id).enviado_at is None


def test_copiar_la_liga_de_otro_es_404_y_no_deja_rastro(entorno, stripe_doble):
    cobro_id = int(_crear(_cliente(ASESOR)).headers["location"].rsplit("=", 1)[1])
    assert _cliente(OTRO).post(f"/cobros/{cobro_id}/liga-copiada").status_code == 404
    assert _auditoria(entorno, "cobro:liga-copiada") == []


def test_el_cobro_de_un_folio_sigue_igual_tras_extraer_la_emision(entorno, stripe_doble):
    """La emisión se compartió con el cobro de un folio: éste sigue creando su cobro con folio."""
    with Session(entorno) as s:
        assert s.get(Ticket, 1) is not None
    r = _cliente(ASESOR).post("/tickets/1/cobro", data={
        "monto": "800.00", "concepto": "CURP", "token_form": "folio-1", "metodo": ["card"]},
        follow_redirects=False)
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    with Session(entorno) as s:
        cobro = s.exec(select(Cobro)).one()
        assert cobro.ticket_id == 1 and cobro.origen == "panel"
    assert panel.emitir_cobro is not None
