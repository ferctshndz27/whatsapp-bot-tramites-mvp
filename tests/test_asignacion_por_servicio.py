"""Asignación por servicio: un mensaje con varios trámites se parte en folios de dueños distintos.

Decisiones del operador (2026-09-24/25): cada folio va a quien atiende ESE trámite; el titular
de la cartera gana si lo atiende; un cliente compartido va al titular que lo atendió último; un
servicio que el titular no atiende va a quien sí. La cadena, por servicio:

    cartera (titulares ∩ quien atiende) → pegajosa (quien llevó ese servicio, luego cualquiera)
    → mismo mensaje (no partir sin necesidad) → reparto (solo en horario, bajo el tope)

Cartera, pegajosa y mismo mensaje no exigen horario ni respetan el tope: son continuidad, como
la pegajosa de la fase 3b. Un folio que sigue en NUEVO no es historial: nadie lo trabajó.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

import app.db as db
from app import asignacion, horario
from app.catalog import catalog
from app.config import settings
from app.conversation import handle_incoming
from app.models import (
    CatalogAsesor,
    Client,
    ClienteAsesor,
    Ticket,
    TicketActivity,
    TicketStatus,
    User,
    utcnow,
)
from app.templating import BUSINESS_TZ

PHONE = "5215511113333"
OTRO = "5215599991111"  # solo para cargar a alguien sin tocar el historial de PHONE
ACTA = "sb-acta-nacimiento"
MATRIMONIO = "sb-acta-matrimonio"


@pytest.fixture(autouse=True)
def _reparto_encendido(monkeypatch):
    """El reparto nace apagado; sin esto el archivo mediría el interruptor, no la regla."""
    monkeypatch.setattr(settings, "reparto_automatico", True)


@pytest.fixture
def base(session, monkeypatch):
    """El catálogo, las inscripciones y la cartera en la MISMA base que la sesión (ver el
    fixture `misma_base` de test_fase3b_asignacion.py: con dos bases, varias pruebas pasaban
    en verde sin medir nada)."""
    monkeypatch.setattr(db, "engine", session.get_bind())
    session.add(Client(phone=PHONE, name="Cliente"))
    session.commit()
    return session


def _usuarios(s, *nombres: str, habilitado: bool = True) -> None:
    for n in nombres:
        s.add(User(username=n, password_hash="x", habilitado=habilitado))
    s.commit()


def _atiende(s, usuario: str, *servicios: str) -> None:
    for sid in servicios:
        s.add(CatalogAsesor(service_id=sid, username=usuario))
    s.commit()


def _cartera(s, *usuarios: str) -> None:
    for u in usuarios:
        s.add(ClienteAsesor(phone=PHONE, username=u, created_by=u))
    s.commit()


def _folio(s, servicio: str, dueno: str | None, status=TicketStatus.CERRADO,
           dias: int = 0, phone: str = PHONE) -> Ticket:
    t = Ticket(phone=phone, category="c", service_id=servicio, service_name=servicio,
               price_info="$1", status=status, assigned_to=dueno,
               created_at=utcnow() - timedelta(days=dias))
    s.add(t)
    s.commit()
    s.refresh(t)
    return t


def _decide(s, *servicios: str) -> dict[str, tuple[str | None, str]]:
    return {
        sid: (d.asesor, d.motivo)
        for sid, d in asignacion.elegir_por_servicio(s, PHONE, list(servicios)).items()
    }


# --- Cada servicio con quien lo atiende --------------------------------------


def test_dos_servicios_que_atienden_personas_distintas_van_a_dos_duenos(base):
    """El corazón del cambio: antes el mensaje entero iba a UNA persona."""
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", "visa")
    _atiende(base, "bob", "pasaporte")
    assert _decide(base, "visa", "pasaporte") == {
        "visa": ("ana", "reparto"),
        "pasaporte": ("bob", "reparto"),
    }


def test_el_titular_de_la_cartera_gana_aunque_otro_lo_atendio_despues(base):
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", "visa")
    _atiende(base, "bob", "visa")
    _folio(base, "visa", "bob", dias=1)  # bob llevó la última visa
    _cartera(base, "ana")
    assert _decide(base, "visa") == {"visa": ("ana", "cartera")}


def test_dos_titulares_de_la_misma_cartera_se_reparten_el_mensaje_por_carga(base):
    """Sin la mejora del paso 7, el titular que gana el primer servicio se queda también el
    segundo aunque ya tenga más carga que el otro titular DENTRO de este mismo mensaje — beto
    gana por orden alfabético y la regla de cartera nunca lo nota (no consulta en_mensaje).
    Con la mejora, carla debe ganar el segundo: es la única explicación posible, porque beto
    sigue siendo alfabéticamente anterior y seguiría ganando si el empate no se rompiera por
    carga."""
    _usuarios(base, "beto", "carla")
    _atiende(base, "beto", "s1", "s2")
    _atiende(base, "carla", "s1", "s2")
    _cartera(base, "beto", "carla")
    assert _decide(base, "s1", "s2") == {
        "s1": ("beto", "cartera"),
        "s2": ("carla", "cartera"),
    }


def test_si_el_titular_no_atiende_el_servicio_va_a_quien_si(base):
    """La cartera nunca pasa por encima de la cobertura: mandarle un trámite a quien no lo
    lleva es un pase de mano que el cliente sufre después."""
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", "visa")
    _atiende(base, "bob", "pasaporte")
    _cartera(base, "ana")
    assert _decide(base, "visa", "pasaporte") == {
        "visa": ("ana", "cartera"),
        "pasaporte": ("bob", "reparto"),
    }


def test_un_cliente_compartido_va_al_titular_que_lo_atendio_ultimo(base):
    """Decisión del operador. «Lo atendió» es cualquier trámite trabajado, no solo este."""
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", "visa")
    _atiende(base, "bob", "visa")
    _cartera(base, "ana", "bob")
    _folio(base, "acta", "ana", dias=10)
    _folio(base, "pasaporte", "bob", dias=2)
    assert _decide(base, "visa") == {"visa": ("bob", "cartera")}
    _folio(base, "acta", "ana", dias=1)
    assert _decide(base, "visa") == {"visa": ("ana", "cartera")}, "no siguió al más reciente"


def test_un_compartido_sin_historial_va_al_titular_con_menos_carga_aunque_este_al_tope(
    base, monkeypatch
):
    monkeypatch.setattr(settings, "carga_maxima_asesor", 1)
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", "visa")
    _atiende(base, "bob", "visa")
    _cartera(base, "ana", "bob")
    for _ in range(2):
        _folio(base, "x", "ana", status=TicketStatus.TOMADO, phone=OTRO)
    _folio(base, "x", "bob", status=TicketStatus.TOMADO, phone=OTRO)
    assert _decide(base, "visa") == {"visa": ("bob", "cartera")}


# --- La pegajosa, por servicio -----------------------------------------------


def test_la_pegajosa_prefiere_a_quien_llevo_el_mismo_servicio(base):
    _usuarios(base, "ana", "bob")
    _folio(base, "visa", "ana", dias=30)
    _folio(base, "pasaporte", "bob", dias=1)
    assert _decide(base, "visa") == {"visa": ("ana", "pegajosa")}
    # Control: un servicio que nadie ha llevado cae con el más reciente.
    assert _decide(base, "acta") == {"acta": ("bob", "pegajosa")}


def test_la_pegajosa_mira_mas_atras_del_ultimo_folio(base):
    """Antes, si el último asesor ya no estaba (o no atendía lo pedido), no se buscaba al
    anterior: el cliente caía al reparto aunque ana lo conociera."""
    _usuarios(base, "ana", "bob")
    _usuarios(base, "carlos", habilitado=False)
    _folio(base, "acta", "ana", dias=30)
    _folio(base, "acta", "carlos", dias=1)
    assert _decide(base, "visa") == {"visa": ("ana", "pegajosa")}


def test_un_folio_que_nadie_ha_trabajado_no_cuenta_como_historial(base):
    """Un folio auto-asignado que sigue en NUEVO nadie lo abrió: su dueño no «atendió» al
    cliente. Contarlo pegaría al cliente a alguien que no lo conoce."""
    _usuarios(base, "ana", "bob")
    intocado = _folio(base, "visa", "bob", status=TicketStatus.NUEVO)  # bob carga 1, ana 0
    assert _decide(base, "pasaporte") == {"pasaporte": ("ana", "reparto")}
    # Control: el mismo folio ya trabajado sí es historial.
    intocado.status = TicketStatus.TOMADO
    base.add(intocado)
    base.commit()
    assert _decide(base, "pasaporte") == {"pasaporte": ("bob", "pegajosa")}


# --- No partir sin necesidad ------------------------------------------------


def test_si_uno_atiende_ambos_el_mensaje_no_se_parte(base):
    """ana atiende visa y pasaporte; bob solo pasaporte y carga menos. Sin la regla del mismo
    mensaje el pasaporte se iría con bob: dos asesores para un cliente sin necesidad."""
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", "visa", "pasaporte")
    _atiende(base, "bob", "pasaporte")
    _folio(base, "x", "ana", status=TicketStatus.TOMADO, phone=OTRO)
    assert _decide(base, "visa", "pasaporte") == {
        "visa": ("ana", "reparto"),
        "pasaporte": ("ana", "mismo-mensaje"),
    }


# --- Los bordes de siempre ----------------------------------------------------


def test_apagado_ningun_servicio_tiene_dueno(base, monkeypatch):
    monkeypatch.setattr(settings, "reparto_automatico", False)
    _usuarios(base, "ana")
    _cartera(base, "ana")
    assert _decide(base, "visa", "pasaporte") == {
        "visa": (None, "apagado"), "pasaporte": (None, "apagado"),
    }


def test_sin_asesores_en_la_oficina_nadie(base):
    assert _decide(base, "visa") == {"visa": (None, "sin-asesores")}


def test_fuera_de_horario_solo_se_asigna_por_continuidad(base, monkeypatch):
    monkeypatch.setattr(
        horario, "ahora", lambda: datetime(2026, 8, 30, 3, 0, tzinfo=BUSINESS_TZ)
    )
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", "visa")
    _atiende(base, "bob", "pasaporte")
    _cartera(base, "ana")
    assert _decide(base, "visa", "pasaporte") == {
        "visa": ("ana", "cartera"),
        "pasaporte": (None, "fuera-de-horario"),
    }


def test_ninguna_asignacion_va_a_quien_no_atiende_el_servicio(base):
    """Invariante 2, recorrida entera sobre un espacio chico: tres asesores, dos servicios,
    toda combinación de quién atiende qué, tres carteras y dos historiales. Solo se permite
    un dueño que no atiende el servicio cuando nadie de la oficina lo atiende."""
    usuarios = ("ana", "bob", "carlos")
    _usuarios(base, *usuarios)
    subconjuntos = [(), ("s1",), ("s2",), ("s1", "s2")]
    carteras = [(), ("ana",), ("ana", "bob")]
    historiales = [(), (("bob", "s1"),)]
    revisadas = 0
    for inscripciones in itertools.product(subconjuntos, repeat=len(usuarios)):
        for titulares in carteras:
            for historial in historiales:
                for modelo in (CatalogAsesor, ClienteAsesor, Ticket):
                    for fila in base.exec(select(modelo)).all():
                        base.delete(fila)
                base.commit()
                for u, servicios in zip(usuarios, inscripciones):
                    _atiende(base, u, *servicios)
                _cartera(base, *titulares)
                for u, sid in historial:
                    _folio(base, sid, u)
                atienden = {
                    sid: {u for u, servicios in zip(usuarios, inscripciones) if sid in servicios}
                    for sid in ("s1", "s2")
                }
                for sid, (asesor, motivo) in _decide(base, "s1", "s2").items():
                    revisadas += 1
                    assert asesor is not None, f"{sid} quedó sin dueño ({motivo}) en horario"
                    assert not atienden[sid] or asesor in atienden[sid], (
                        f"{sid} fue a {asesor} ({motivo}) y lo atienden {atienden[sid]}; "
                        f"cartera={titulares} historial={historial}"
                    )
    assert revisadas == 4 ** 3 * 3 * 2 * 2


# --- Por el camino de verdad ---------------------------------------------------


def _sin_requisitos(*service_ids: str) -> None:
    for sid in service_ids:
        svc = catalog.service_by_id(sid)
        assert catalog.update_service(
            svc.id, svc.name, svc.price_info, requirements="", admin="prueba"
        ) is None


def test_el_alta_real_parte_el_mensaje_por_servicio(base, caplog):
    """Sin esta prueba, dejar `_handle_confirmar` con la llamada única de antes pasaba con
    todas las de arriba en verde."""
    _usuarios(base, "ana", "bob")
    _atiende(base, "ana", ACTA)
    _atiende(base, "bob", MATRIMONIO)
    _cartera(base, "ana")  # el acta sale por cartera y el matrimonio por reparto: motivos distintos
    _sin_requisitos(ACTA, MATRIMONIO)
    with caplog.at_level(logging.INFO, logger="app.asignacion"):
        for texto in ("menu", "1", "1 y 2", "sí"):
            handle_incoming(base, PHONE, texto)
    folios = {t.service_id: t for t in base.exec(select(Ticket).where(Ticket.phone == PHONE)).all()}
    assert {sid: t.assigned_to for sid, t in folios.items()} == {ACTA: "ana", MATRIMONIO: "bob"}
    acciones = {(a.ticket_id, a.action) for a in base.exec(select(TicketActivity)).all()}
    assert (folios[ACTA].id, "asignado-auto:ana") in acciones
    assert (folios[MATRIMONIO].id, "asignado-auto:bob") in acciones
    # Cada folio con SU motivo en la bitácora del log, no el de otro.
    assert f"Folio #{folios[ACTA].id} asignado automáticamente a ana (cartera)" in caplog.text
    assert f"Folio #{folios[MATRIMONIO].id} asignado automáticamente a bob (reparto)" in caplog.text


def test_la_escalada_prefiere_al_titular_de_la_cartera(base):
    """El segundo sitio que crea folios. bob carga más, pero el cliente es de su cartera."""
    _usuarios(base, "ana", "bob")
    for _ in range(2):
        _folio(base, "x", "bob", status=TicketStatus.TOMADO, phone=OTRO)
    _cartera(base, "bob")
    handle_incoming(base, PHONE, "hola")
    handle_incoming(base, PHONE, "quiero hablar con un asesor")
    folio = base.exec(select(Ticket).where(Ticket.phone == PHONE)).first()
    assert folio is not None and folio.escalado is True
    assert folio.assigned_to == "bob"
