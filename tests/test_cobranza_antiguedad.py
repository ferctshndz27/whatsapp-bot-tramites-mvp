"""`cobranza.antiguedad()` — cuánto lleva un cobro en la calle.

Lo que se defiende aquí, en orden de qué tan caro sale:

1. Que la resta no truene. `utcnow()` es aware y las columnas salen NAIVE de
   SQLite: la resta cruda levanta TypeError, y como el cálculo cae dentro de un
   parcial COMPARTIDO se lleva /cobros y /my/cobros al mismo tiempo. Por eso la
   prueba lee la fila de la base en OTRA sesión, que es como llega a la vista —
   con el objeto recién creado (todavía aware) el fallo no se reproduce.
2. Que el umbral se mida con un reloj INYECTADO. Sin `ahora=`, una prueba no
   distingue un cálculo correcto de uno corrido seis horas por zona horaria, y
   ese es el bug que pasa verde en CI y solo existe en el VPS (que corre con
   TZ=America/Mexico_City).
3. Que un estado terminal NUNCA escale. Un pagado de hace ocho meses en rojo
   convierte la lista en una pared roja, y entonces el color deja de significar
   nada también donde sí importa.
4. Que no le diga «4 días sin pagar» a un link que ya no abre. Eso manda al
   asesor a gastar llamadas en un pago que es imposible.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import cobranza
from app.config import settings
from app.models import Cobro, utcnow

# Reloj fijo y AWARE. Fijo para que el umbral no dependa del día en que corra la
# suite; aware porque es lo que devuelve utcnow() en producción.
AHORA = datetime(2026, 9, 9, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def engine(_isolated_db, monkeypatch):
    motor = create_engine("sqlite://", connect_args={"check_same_thread": False},
                          poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", motor)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    return motor


def _cobro(**kw) -> Cobro:
    base = dict(phone="5215500000000", concepto="CURP", monto_centavos=100_000,
                estado="pendiente", token_verificacion=f"ant-{kw.pop('tok', '1')}")
    base.update(kw)
    return Cobro(**base)


def _guardado(motor, **kw) -> Cobro:
    """El cobro tal y como lo ve la vista: escrito, y releído en OTRA sesión.

    Es la única forma de que `created_at` llegue NAIVE. Con el objeto que
    todavía cuelga de la sesión que lo creó, el valor sigue siendo el aware que
    se le asignó y el fallo de zona horaria no se reproduce.
    """
    with Session(motor) as s:
        s.add(_cobro(**kw))
        s.commit()
    with Session(motor) as s:
        return s.exec(select(Cobro)).one()


# ---------------------------------------------------------------------------
# El fallo caro: naive contra aware
# ---------------------------------------------------------------------------


def test_no_truena_con_una_fila_leida_de_la_base(engine):
    """REGLA: la resta pasa por aware_utc en los DOS extremos.

    Sin eso: TypeError -> 500 -> se pierden las dos vistas de dinero a la vez.
    """
    guardado = _guardado(engine, created_at=AHORA - timedelta(days=4))
    assert guardado.created_at.tzinfo is None, (
        "la fila ya no llega naive de SQLite; esta prueba dejó de vigilar lo suyo"
    )
    assert cobranza.antiguedad(guardado, ahora=AHORA).dias == 4


def test_tambien_sirve_con_una_fila_todavia_aware(engine):
    """El otro lado: un cobro recién creado, sin pasar por la base, sigue aware.
    La función tiene que dar lo mismo — si solo funcionara con uno de los dos,
    el bug aparecería según por dónde entre el objeto."""
    fresco = _cobro(created_at=AHORA - timedelta(days=4))
    assert fresco.created_at.tzinfo is not None
    assert cobranza.antiguedad(fresco, ahora=AHORA).dias == 4


def test_un_reloj_naive_inyectado_tampoco_truena(engine):
    """El otro extremo de la resta, y el que casi se queda sin vigilar.

    `utcnow()` ya es aware, así que el `aware_utc()` del lado del reloj no hace
    nada en el camino normal — parecía código defensivo de adorno. No lo es:
    protege exactamente del caller que pasa un `datetime.now()` naive, que es
    el fallo que pasa VERDE en CI (sin TZ) y sale corrido seis horas solo en el
    VPS (TZ=America/Mexico_City). Sin esta prueba, alguien lo borra por
    limpieza y el agujero vuelve sin que nada se ponga rojo.
    """
    guardado = _guardado(engine, created_at=AHORA - timedelta(days=4))
    reloj_naive = AHORA.replace(tzinfo=None)
    assert cobranza.antiguedad(guardado, ahora=reloj_naive).dias == 4
    assert cobranza.antiguedad(guardado, ahora=AHORA).dias == 4


def test_el_reloj_por_omision_es_utcnow_y_no_now_local(engine):
    """Sin `ahora=` la función usa utcnow(). Si alguien lo cambiara por un
    `datetime.now()` sin tzinfo «para que compile», en el VPS —que corre con
    TZ=America/Mexico_City— daría 6 h de menos, y en CI (sin TZ) pasaría verde.
    Un cobro de hace 3 días y 1 hora tiene que seguir contando 3 días aquí."""
    recien = _guardado(engine, created_at=utcnow() - timedelta(days=3, hours=1))
    assert cobranza.antiguedad(recien).dias == 3
    assert cobranza.antiguedad(recien).viejo is True


# ---------------------------------------------------------------------------
# El umbral
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delta,dias,viejo", [
    (timedelta(hours=1), 0, False),
    (timedelta(days=1), 1, False),
    (timedelta(days=2, hours=23), 2, False),   # justo antes del corte
    (timedelta(days=3), 3, True),              # justo en el corte
    (timedelta(days=4), 4, True),
])
def test_el_corte_son_dias_completos_de_24h(engine, delta, dias, viejo):
    """Días completos, no hojas de calendario: contar fechas UTC contra el
    calendario de CDMX se equivoca por un día entero en todo cobro generado
    después de las 18:00, que es cuando más links se mandan.

    Si mañana alguien mueve COBRO_VIEJO_DIAS de 3 a otra cosa, esta prueba se
    cae y obliga a decirlo en voz alta — que es el punto, porque el número
    sigue siendo una decisión abierta.
    """
    a = cobranza.antiguedad(_cobro(created_at=AHORA - delta), ahora=AHORA)
    assert (a.dias, a.viejo) == (dias, viejo)


def test_el_umbral_del_modulo_es_el_que_se_aplica(engine, monkeypatch):
    """El 3 no está cableado en la comparación: sale de la constante. Si se
    duplicara el número en dos lados, este monkeypatch lo destaparía."""
    monkeypatch.setattr(cobranza, "COBRO_VIEJO_DIAS", 10)
    a = cobranza.antiguedad(_cobro(created_at=AHORA - timedelta(days=5)), ahora=AHORA)
    assert a.viejo is False, "el umbral está cableado en vez de leerse de la constante"


# ---------------------------------------------------------------------------
# Cada estado con su reloj
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("estado", ["pagado", "reembolsado", "cancelado",
                                    "vencido", "fallido"])
def test_un_estado_terminal_nunca_escala(engine, estado):
    """Ese dinero ya no está en la calle: marcarlo en rojo a los 200 días llena
    la lista de alarmas inaccionables y quema la señal para las filas vivas."""
    a = cobranza.antiguedad(
        _cobro(estado=estado, created_at=AHORA - timedelta(days=200)), ahora=AHORA)
    assert a.vivo is False
    assert a.viejo is False


def test_el_pagado_reporta_cuanto_tardo_no_su_edad(engine):
    """created_at → pagado_at, no → ahora. Un cobro que cayó en 2 días hace
    ocho meses tardó 2 días, no 240: esa cifra es la que un día calibra el 3."""
    a = cobranza.antiguedad(_cobro(
        estado="pagado",
        created_at=AHORA - timedelta(days=240),
        pagado_at=AHORA - timedelta(days=238),
    ), ahora=AHORA)
    assert a.tardo == 2
    assert a.dias == 240  # su edad sigue disponible, pero no es lo que se pinta


def test_pagado_el_mismo_dia(engine):
    a = cobranza.antiguedad(_cobro(
        estado="pagado", created_at=AHORA - timedelta(days=5),
        pagado_at=AHORA - timedelta(days=5) + timedelta(hours=3),
    ), ahora=AHORA)
    assert a.tardo == 0


# ---------------------------------------------------------------------------
# El instrumento: si el cliente ya no puede pagar, la acción es otra
# ---------------------------------------------------------------------------


def test_voucher_vencido_pide_regenerar(engine):
    a = cobranza.antiguedad(_cobro(
        created_at=AHORA - timedelta(days=4),
        voucher_expira_at=AHORA - timedelta(days=1),
    ), ahora=AHORA)
    assert a.instrumento_vencido is True


def test_voucher_vivo_no_pide_nada(engine):
    a = cobranza.antiguedad(_cobro(
        created_at=AHORA - timedelta(days=4),
        voucher_expira_at=AHORA + timedelta(days=1),
    ), ahora=AHORA)
    assert a.instrumento_vencido is False


def test_spei_con_payment_intent_vivo_no_se_declara_muerto(engine):
    """La MISMA regla que la rama checkout.session.expired de _aplicar_a_cobro:
    con un PaymentIntent vivo, la sesión de Checkout puede expirar sin matar el
    cobro. Fecharlo con la sesión mataría todo SPEI 24 h después de nacer — en
    la pantalla y solo ahí, que es la peor clase de mentira."""
    a = cobranza.antiguedad(_cobro(
        created_at=AHORA - timedelta(days=4),
        stripe_payment_intent_id="pi_vivo",
        url_expira_at=AHORA - timedelta(days=3),
    ), ahora=AHORA)
    assert a.instrumento_vencido is False


def test_sin_payment_intent_manda_la_sesion(engine):
    """Sin PaymentIntent la sesión ES el instrumento: si expiró, no hay nada que
    pagar y hay que generar otro link."""
    a = cobranza.antiguedad(_cobro(
        created_at=AHORA - timedelta(days=4),
        url_expira_at=AHORA - timedelta(days=3),
    ), ahora=AHORA)
    assert a.instrumento_vencido is True


def test_un_cobro_terminal_no_tiene_instrumento_que_regenerar(engine):
    a = cobranza.antiguedad(_cobro(
        estado="pagado", created_at=AHORA - timedelta(days=4),
        voucher_expira_at=AHORA - timedelta(days=1),
    ), ahora=AHORA)
    assert a.instrumento_vencido is False


# ---------------------------------------------------------------------------
# Bordes de reloj
# ---------------------------------------------------------------------------


def test_una_fecha_futura_dice_hoy_y_no_un_negativo(engine):
    """Pasa de verdad: un respaldo restaurado o el reloj del contenedor movido.
    «hoy» es feo; «hace -1 días» es un reporte de bug que nadie sabe contestar."""
    a = cobranza.antiguedad(_cobro(created_at=AHORA + timedelta(days=2)), ahora=AHORA)
    assert a.dias == 0
    assert a.viejo is False


def test_un_pagado_at_anterior_al_alta_no_da_tardanza_negativa(engine):
    a = cobranza.antiguedad(_cobro(
        estado="pagado", created_at=AHORA - timedelta(days=1),
        pagado_at=AHORA - timedelta(days=3),
    ), ahora=AHORA)
    assert a.tardo == 0


# ---------------------------------------------------------------------------
# El corte para SQL
# ---------------------------------------------------------------------------


def test_corte_viejo_es_naive_porque_sqlite_guarda_naive(engine):
    """Va directo a un WHERE. Si saliera aware, SQLAlchemy lo compararía contra
    columnas naive y el filtro devolvería cualquier cosa sin fallar."""
    corte = cobranza.corte_viejo(AHORA)
    assert corte.tzinfo is None
    assert corte == datetime(2026, 9, 6, 12, 0, 0)
