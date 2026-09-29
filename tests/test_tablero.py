"""Tablero personal del CRM (app/tablero.py): las columnas de cada quien y dónde tiene a cada cliente.

Decisión del operador (2026-09-29): cada usuario, admins incluidos, administra SU tablero; antes
era un embudo global que solo tocaba el admin global. Aquí se afirman las reglas sin HTTP (las
rutas y el alcance viven en tests/test_crm.py):

- el tablero nace como copia de las etapas globales vivas, una sola vez por persona;
- el primer día cada cliente se ve donde se veía en el embudo global;
- reordenar no mueve a nadie, ni siquiera a los clientes que nadie ha movido;
- la baja de una columna manda a sus clientes a la primera que queda y ahí se quedan;
- mover, crear y renombrar son de una persona y no tocan el embudo global ni el tablero de nadie.
"""

import logging
import threading

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import tablero
from app.config import settings
from app.models import (
    ETAPAS_CRM_DEFAULT,
    Client,
    CrmColumna,
    CrmEtapa,
    CrmUbicacion,
)


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    # init_db y no solo create_all: siembra las cuatro etapas globales y crea los índices únicos
    # del tablero (ux_crmcolumna_origen, ux_crmubicacion), que dos de estas pruebas necesitan.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(settings, "mssql_host", "")
    db.init_db()
    yield


def _globales(s) -> list[CrmEtapa]:
    return s.exec(
        select(CrmEtapa).where(CrmEtapa.habilitado == True).order_by(CrmEtapa.ord)  # noqa: E712
    ).all()


def _cliente(s, phone: str, etapa: int | None = None) -> Client:
    cliente = Client(phone=phone, name=f"Cliente {phone}", office_id=1, etapa_crm_id=etapa)
    s.add(cliente)
    s.commit()
    s.refresh(cliente)
    return cliente


def _copias(t: tablero.Tablero) -> dict[int, int]:
    """Etapa global → la columna de este tablero que la copió."""
    return {c.origen_id: c.id for c in t.activas}


# ---------------------------------------------------------------------------
# Siembra
# ---------------------------------------------------------------------------

def test_la_primera_carga_copia_las_etapas_globales_vivas_en_su_orden_una_sola_vez(caplog):
    """El tablero de cada quien nace como el embudo que ya veía: mismos nombres, colores y orden,
    y cada columna recuerda de qué etapa global salió —sin ese origen, el primer día todos los
    clientes caerían revueltos—. Cada persona recibe filas propias, y volver a abrir el tablero
    (o haber dado de baja una columna) no lo vuelve a sembrar: ni siquiera lo intenta, porque
    el índice único lo pararía, pero cada apertura de /crm pagaría un INSERT fallido."""
    with Session(db.engine) as s:
        globales = _globales(s)
        t = tablero.cargar(s, "ana")
        assert [(c.nombre, c.color, c.ord, c.origen_id) for c in t.activas] == [
            (g.nombre, g.color, i, g.id) for i, g in enumerate(globales)
        ]
        otra = tablero.cargar(s, "beto")
        assert not set(t.ids) & set(otra.ids), "dos personas comparten columnas"
        with caplog.at_level(logging.INFO, logger="app.tablero"):
            assert tablero.cargar(s, "ana").ids == t.ids, "la segunda carga volvió a sembrar"
        assert "ya lo había sembrado" not in caplog.text, "cada carga intenta sembrar de nuevo"
        tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", t.ids[3]))
        assert tablero.cargar(s, "ana").ids == t.ids[:3], "la columna dada de baja volvió al abrir"
        filas = s.exec(select(CrmColumna).where(CrmColumna.username == "ana")).all()
        assert len(filas) == 4


def test_una_etapa_global_de_baja_no_se_copia_y_sin_ninguna_viva_nacen_las_del_plan():
    with Session(db.engine) as s:
        g = _globales(s)
        g[1].habilitado = False
        s.add(g[1])
        s.commit()
        assert [c.nombre for c in tablero.cargar(s, "ana").activas] == [
            g[0].nombre, g[2].nombre, g[3].nombre
        ]
        for etapa in _globales(s):
            etapa.habilitado = False
            s.add(etapa)
        s.commit()
        t = tablero.cargar(s, "beto")
        assert [(c.nombre, c.color, c.origen_id) for c in t.activas] == [
            (nombre, color, None) for nombre, color in ETAPAS_CRM_DEFAULT
        ], "sin etapas globales vivas el tablero nació vacío o con otra cosa"


def test_dos_siembras_a_la_vez_no_dejan_el_tablero_doble():
    """Dos primeras cargas simultáneas —dos pestañas, o el tablero y el chat inicial— siembran
    las dos. La segunda choca con ux_crmcolumna_origen, se descarta sin error y la persona queda
    con UN tablero: sin el índice abriría /crm con ocho columnas, cuatro repetidas."""
    with Session(db.engine) as s:
        tablero._sembrar(s, "ana")
        tablero._sembrar(s, "ana")  # la carrera: esta no vio las filas de la primera
        assert len(tablero.cargar(s, "ana").activas) == 4


# ---------------------------------------------------------------------------
# Dónde está cada cliente
# ---------------------------------------------------------------------------

def test_el_primer_dia_cada_cliente_se_ve_donde_estaba_en_el_embudo_global():
    """Un cliente que la persona no ha movido cae en la copia de su etapa global. Sin etapa, o
    con una etapa global ya dada de baja, en la copia de la primera: la regla de siempre."""
    with Session(db.engine) as s:
        g = _globales(s)
        vieja = CrmEtapa(nombre="Vieja", ord=9, habilitado=False)
        s.add(vieja)
        s.commit()
        en_tercera = _cliente(s, "521", etapa=g[2].id)
        sin_etapa = _cliente(s, "522")
        en_vieja = _cliente(s, "523", etapa=vieja.id)
        t = tablero.cargar(s, "ana")
        copia = _copias(t)
        assert t.columna_de(en_tercera) == copia[g[2].id]
        assert t.columna_de(sin_etapa) == copia[g[0].id]
        assert t.columna_de(en_vieja) == copia[g[0].id]


def test_mover_es_de_quien_mueve_y_no_toca_el_embudo_global():
    """Un cliente compartido: ana lo pone en su cuarta columna y beto lo sigue viendo donde
    estaba. `Client.etapa_crm_id` queda congelado: es el punto de partida de todos los demás,
    y reescribirlo movería al cliente en el tablero de quien nunca lo tocó."""
    with Session(db.engine) as s:
        g = _globales(s)
        cliente = _cliente(s, "521", etapa=g[2].id)
        de_ana = tablero.cargar(s, "ana")
        tablero.mover(s, "ana", cliente.phone, de_ana.ids[3])
        assert tablero.cargar(s, "ana").columna_de(cliente) == de_ana.ids[3]
        de_beto = tablero.cargar(s, "beto")
        assert de_beto.columna_de(cliente) == _copias(de_beto)[g[2].id], "el movimiento de ana movió a beto"
        s.refresh(cliente)
        assert cliente.etapa_crm_id == g[2].id, "mover en un tablero personal reescribió el embudo global"


def test_mover_dos_veces_deja_una_sola_ubicacion_con_la_ultima_columna():
    with Session(db.engine) as s:
        cliente = _cliente(s, "521")
        t = tablero.cargar(s, "ana")
        tablero.mover(s, "ana", cliente.phone, t.ids[1])
        tablero.mover(s, "ana", cliente.phone, t.ids[2])
        filas = s.exec(select(CrmUbicacion).where(CrmUbicacion.username == "ana")).all()
        assert [(u.phone, u.columna_id) for u in filas] == [(cliente.phone, t.ids[2])]


def test_mover_cuando_otra_peticion_creo_la_ubicacion_primero_se_queda_con_la_ultima(monkeypatch):
    """Doble clic o dos pestañas: la otra petición crea la ubicación entre el SELECT y el INSERT
    de esta. El INSERT choca con ux_crmubicacion y, en vez de un 500 que regresa la tarjeta a su
    columna, se reescribe la fila que ganó con el último movimiento."""
    with Session(db.engine) as s:
        cliente = _cliente(s, "521")
        t = tablero.cargar(s, "ana")

        class _Filas:
            def __init__(self, filas):
                self.filas = filas

            def first(self):
                return self.filas[0] if self.filas else None

        exec_real = s.exec
        carrera = {"hecha": False}

        def exec_con_carrera(sentencia, *args, **kwargs):
            filas = exec_real(sentencia, *args, **kwargs).all()
            if not carrera["hecha"] and "crmubicacion" in str(sentencia):
                carrera["hecha"] = True
                with Session(db.engine) as otra:
                    otra.add(CrmUbicacion(username="ana", phone=cliente.phone, columna_id=t.ids[1]))
                    otra.commit()
            return _Filas(filas)

        # context() y no undo(): undo() deshace también los parches del fixture _base (engine,
        # espejo) y lo que siga correría contra la configuración real.
        with monkeypatch.context() as m:
            m.setattr(s, "exec", exec_con_carrera)
            tablero.mover(s, "ana", cliente.phone, t.ids[3])
        assert carrera["hecha"], "la prueba no provocó la carrera"
        filas = s.exec(select(CrmUbicacion).where(CrmUbicacion.username == "ana")).all()
        assert [(u.phone, u.columna_id) for u in filas] == [(cliente.phone, t.ids[3])]


# ---------------------------------------------------------------------------
# Reordenar
# ---------------------------------------------------------------------------

def test_reordenar_no_mueve_a_ningun_cliente():
    """Arrastrar columnas acomoda el tablero, no a los clientes: ni el que se movió a mano, ni
    el que viene del embudo global, ni el que nunca tuvo etapa cambian de columna. El riesgo es
    el último: si «sin etapa» fuera «la primera columna», poner otra al principio mudaría a
    todos los clientes nuevos sin que nadie los tocara."""
    with Session(db.engine) as s:
        g = _globales(s)
        vieja = CrmEtapa(nombre="Vieja", ord=9, habilitado=False)
        s.add(vieja)
        s.commit()
        movido = _cliente(s, "521")
        del_global = _cliente(s, "522", etapa=g[2].id)
        nuevo = _cliente(s, "523")
        en_vieja = _cliente(s, "524", etapa=vieja.id)
        t = tablero.cargar(s, "ana")
        tablero.mover(s, "ana", movido.phone, t.ids[1])
        clientes = (movido, del_global, nuevo, en_vieja)
        antes = {c.phone: tablero.cargar(s, "ana").columna_de(c) for c in clientes}
        activas = tablero.reordenar(s, "ana", list(reversed(t.ids)))
        assert [c.id for c in activas] == list(reversed(t.ids))
        despues = tablero.cargar(s, "ana")
        assert despues.ids == list(reversed(t.ids)), "el orden nuevo no persistió"
        assert {c.phone: despues.columna_de(c) for c in clientes} == antes
        assert [c.ord for c in despues.activas] == [0, 1, 2, 3]


def test_reordenar_exige_cada_columna_propia_exactamente_una_vez():
    """Una lista a medias dejaría dos columnas con el mismo lugar, y un id de otro tablero no
    se acepta aunque exista. `True` es 1 para un set de Python: sin el filtro de bool, [True, 2,
    3, 4] pasaría por las columnas 1..4."""
    with Session(db.engine) as s:
        t = tablero.cargar(s, "ana")
        ajena = tablero.cargar(s, "beto").ids[0]
        assert t.ids[0] == 1, "la prueba de bool necesita que la primera columna sea la 1"
        for malo in (
            t.ids[:-1],
            t.ids + [t.ids[0]],
            t.ids[:-1] + [ajena],
            [str(i) for i in t.ids],
            [True] + t.ids[1:],
            "1,2,3,4",
            None,
        ):
            with pytest.raises(ValueError):
                tablero.reordenar(s, "ana", malo)
        assert tablero.cargar(s, "ana").ids == t.ids


# ---------------------------------------------------------------------------
# Baja
# ---------------------------------------------------------------------------

def test_la_baja_manda_a_sus_clientes_a_la_primera_que_queda_y_ahi_se_quedan():
    """La baja escribe una sola fila (destino_id) y aun así los clientes de la columna —el que
    la persona puso ahí y el que llegó del embudo global— terminan en la primera que queda. Y
    se quedan: reordenar después, o dar de baja también ese destino, no los suelta en «la
    primera columna» de turno."""
    with Session(db.engine) as s:
        g = _globales(s)
        puesto = _cliente(s, "521")
        del_global = _cliente(s, "522", etapa=g[2].id)
        t = tablero.cargar(s, "ana")
        tercera = t.ids[2]
        assert _copias(t)[g[2].id] == tercera
        tablero.mover(s, "ana", puesto.phone, tercera)

        destino = tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", tercera))
        assert destino.id == t.ids[0]
        despues = tablero.cargar(s, "ana")
        assert despues.columna_de(puesto) == despues.columna_de(del_global) == t.ids[0]

        tablero.reordenar(s, "ana", [t.ids[1], t.ids[3], t.ids[0]])
        reordenado = tablero.cargar(s, "ana")
        assert reordenado.columna_de(puesto) == reordenado.columna_de(del_global) == t.ids[0], (
            "reordenar soltó en la primera columna a los clientes de la baja"
        )

        tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", t.ids[0]))
        final = tablero.cargar(s, "ana")
        assert final.columna_de(puesto) == final.columna_de(del_global) == t.ids[1]

        filas = s.exec(select(CrmUbicacion).where(CrmUbicacion.username == "ana")).all()
        assert [(u.phone, u.columna_id) for u in filas] == [(puesto.phone, tercera)], (
            "la baja reescribió ubicaciones: una copia por cliente en el espejo"
        )


def _baja_con_foto_vieja(monkeypatch, s, foto: tablero.Tablero, columna_id: int):
    """La otra pestaña da de baja `columna_id`: su PRIMERA lectura del tablero es `foto`, de
    antes de la baja que ya ocurrió (las dos peticiones leyeron a la vez); las siguientes ya ven
    la base real. context() y no undo(): undo() deshace también los parches del fixture _base."""
    real = tablero.cargar
    llamadas = {"n": 0}

    def cargar(session, usuario):
        llamadas["n"] += 1
        return foto if llamadas["n"] == 1 else real(session, usuario)

    with monkeypatch.context() as m:
        m.setattr(tablero, "cargar", cargar)
        return tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", columna_id))


def test_dos_bajas_a_la_vez_no_dejan_un_ciclo_ni_clientes_que_brincan(monkeypatch):
    """Dos pestañas dan de baja A y B al mismo tiempo, y cada una eligió como destino la columna
    que la otra estaba quitando: A→B y B→A. Con ese ciclo sus clientes caían en «la primera
    columna» de turno y brincaban al reordenar. La que llega tarde lo ve al releer después de su
    commit y manda lo suyo a una columna viva."""
    with Session(db.engine) as s:
        en_a = _cliente(s, "521")
        en_b = _cliente(s, "522")
        t = tablero.cargar(s, "ana")
        a, b, c, d = t.ids
        tablero.mover(s, "ana", en_a.phone, a)
        tablero.mover(s, "ana", en_b.phone, b)
        foto = tablero.cargar(s, "ana")                                  # las dos leen aquí
        tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", a))  # A → B
        _baja_con_foto_vieja(monkeypatch, s, foto, b)                    # creía viva a A
        assert s.get(CrmColumna, b).destino_id == c, "la baja tardía dejó un destino muerto"
        final = tablero.cargar(s, "ana")
        assert final.columna_de(en_a) == final.columna_de(en_b) == c
        tablero.reordenar(s, "ana", [d, c])
        reordenado = tablero.cargar(s, "ana")
        assert reordenado.columna_de(en_a) == reordenado.columna_de(en_b) == c, (
            "tras reordenar, los clientes de las bajas brincaron de columna"
        )


def test_la_baja_simultanea_de_las_dos_ultimas_deja_una_viva(monkeypatch):
    """Con dos columnas vivas, dos pestañas quitan una cada quien: las dos pasaban la guarda de
    «la única» con su propia foto y el tablero se quedaba sin columnas. La que llega tarde se
    deshace y contesta lo mismo que cuando se intenta quitar la única."""
    with Session(db.engine) as s:
        t = tablero.cargar(s, "ana")
        a, b, c, d = t.ids
        for columna_id in (c, d):
            tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", columna_id))
        foto = tablero.cargar(s, "ana")
        tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", a))
        with pytest.raises(ValueError, match="única etapa"):
            _baja_con_foto_vieja(monkeypatch, s, foto, b)
        s.expire_all()
        assert tablero.cargar(s, "ana").ids == [b], "el tablero quedó sin columnas"
        assert s.get(CrmColumna, b).destino_id is None


def test_resolver_una_cadena_larga_cuesta_lineal_y_no_una_vuelta_por_cliente():
    """Una cadena de bajas se recorre UNA vez por carga, no una por cliente: con 200 bajas
    encadenadas y 300 clientes en la cabeza eran 60 000 pasos en cada sondeo del tablero (cada
    20 s), CPU que el único proceso del bot le quita al webhook."""

    class _Contador(dict):
        lecturas = 0

        def get(self, llave, defecto=None):
            _Contador.lecturas += 1
            return super().get(llave, defecto)

    viva = CrmColumna(id=201, username="ana", nombre="Viva")
    todas = _Contador({i: CrmColumna(id=i, username="ana", nombre=f"B{i}", habilitado=False,
                                     destino_id=i + 1) for i in range(1, 201)})
    todas[201] = viva
    clientes = [Client(phone=f"52{i:010d}", name="x", office_id=1) for i in range(300)]
    t = tablero.Tablero(
        usuario="ana", activas=[viva], todas=todas, por_origen={},
        ubicaciones={c.phone: 1 for c in clientes}, etapas_globales=[],
    )
    assert {t.columna_de(c) for c in clientes} == {201}
    assert _Contador.lecturas <= 200 + 300, f"{_Contador.lecturas} lecturas: la cadena se recorre por cliente"


def test_el_tope_de_filas_frena_el_crecimiento_por_altas_y_bajas(monkeypatch):
    """El tope de columnas vivas no frena a quien crea y da de baja en bucle: cada vuelta deja
    una fila de baja (el espejo no tiene DELETE, así que no se purgan). El tope de filas del
    tablero sí, y lejos de cualquier uso real."""
    monkeypatch.setattr(tablero, "MAX_FILAS", 8)
    with Session(db.engine) as s:
        tablero.cargar(s, "ana")  # 4 filas sembradas
        for n in range(4):
            tablero.dar_de_baja(s, "ana", tablero.crear(s, "ana", f"Vuelta {n}", "#FFFFFF"))
        with pytest.raises(tablero.ColumnasAlTope, match="máximo"):
            tablero.crear(s, "ana", "Una más", "#FFFFFF")
        assert len(s.exec(select(CrmColumna).where(CrmColumna.username == "ana")).all()) == 8


def test_la_unica_columna_no_se_da_de_baja():
    with Session(db.engine) as s:
        t = tablero.cargar(s, "ana")
        for columna_id in t.ids[1:]:
            tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", columna_id))
        with pytest.raises(ValueError, match="única etapa"):
            tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", t.ids[0]))
        assert tablero.cargar(s, "ana").ids == [t.ids[0]]


def test_una_cadena_de_destinos_con_ciclo_no_cuelga_el_tablero():
    """Cinturón: la cadena de bajas no puede cerrarse sola —el destino siempre está vivo al
    darse la baja, y una columna de baja no revive—, pero una base restaurada a mano podría
    traer un ciclo. Sin el tope de saltos, pintar el tablero de esa persona colgaría un hilo
    del servidor para siempre."""
    a = CrmColumna(id=1, username="ana", nombre="A", habilitado=False, destino_id=2)
    b = CrmColumna(id=2, username="ana", nombre="B", habilitado=False, destino_id=1)
    viva = CrmColumna(id=3, username="ana", nombre="C")
    t = tablero.Tablero(
        usuario="ana", activas=[viva], todas={1: a, 2: b, 3: viva},
        por_origen={}, ubicaciones={}, etapas_globales=[],
    )
    resultado = []
    hilo = threading.Thread(target=lambda: resultado.append(t.viva(1)), daemon=True)
    hilo.start()
    hilo.join(timeout=2)
    assert resultado == [3], "la cadena con ciclo colgó la resolución"


# ---------------------------------------------------------------------------
# Crear y renombrar
# ---------------------------------------------------------------------------

def test_crear_va_al_final_no_revive_una_de_baja_y_respeta_el_tope():
    with Session(db.engine) as s:
        tablero.cargar(s, "ana")
        with pytest.raises(tablero.NombreRepetido):
            tablero.crear(s, "ana", "nuevo LEAD", "#FFFFFF")
        nueva = tablero.crear(s, "ana", "Por cobrar", "#FFB300")
        assert nueva.ord == 4 and nueva.origen_id is None and nueva.username == "ana"
        # Los nombres son por tablero: otra persona puede tener su propia «Por cobrar».
        assert tablero.crear(s, "beto", "Por cobrar", "#FFB300").id != nueva.id

        tablero.dar_de_baja(s, "ana", nueva)
        otra = tablero.crear(s, "ana", "Por cobrar", "#FFB300")
        assert otra.id != nueva.id, "revivió la columna de baja: le devolvería clientes que ya se fueron"

        n = 0
        while len(tablero.cargar(s, "ana").activas) < tablero.MAX_COLUMNAS:
            n += 1
            tablero.crear(s, "ana", f"Extra {n}", "#FFFFFF")
        with pytest.raises(tablero.ColumnasAlTope):
            tablero.crear(s, "ana", "Una más", "#FFFFFF")
        assert len(tablero.cargar(s, "ana").activas) == tablero.MAX_COLUMNAS


def test_renombrar_no_choca_consigo_misma_y_si_con_otra_viva():
    with Session(db.engine) as s:
        t = tablero.cargar(s, "ana")
        primera = t.activas[0]
        assert tablero.editar(s, "ana", primera, {"nombre": primera.nombre.upper()}).nombre == primera.nombre.upper()
        with pytest.raises(tablero.NombreRepetido):
            tablero.editar(s, "ana", primera, {"nombre": t.activas[2].nombre.lower()})
        # Una de baja ya no ocupa su nombre.
        cuarta = t.activas[3]
        nombre = cuarta.nombre
        tablero.dar_de_baja(s, "ana", cuarta)
        assert tablero.editar(s, "ana", primera, {"nombre": nombre}).nombre == nombre


def test_columna_propia_no_distingue_entre_ajena_de_baja_e_inexistente():
    with Session(db.engine) as s:
        de_ana = tablero.cargar(s, "ana")
        de_beto = tablero.cargar(s, "beto")
        assert tablero.columna_propia(s, "ana", de_ana.ids[0]).id == de_ana.ids[0]
        assert tablero.columna_propia(s, "ana", de_beto.ids[0]) is None
        tablero.dar_de_baja(s, "ana", tablero.columna_propia(s, "ana", de_ana.ids[1]))
        assert tablero.columna_propia(s, "ana", de_ana.ids[1]) is None
        assert tablero.columna_propia(s, "ana", 9999) is None
