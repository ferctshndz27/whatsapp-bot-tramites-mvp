"""Consistencia estructural del espejo MSSQL: los CINCO lugares.

REGLA DE NEGOCIO: agregar una tabla espejada obliga a tocar cinco lugares
(CLAUDE.md, sección "El espejo MSSQL — the main footgun"). Si se olvida uno, el
espejo diverge EN SILENCIO — nadie se entera hasta el día que hace falta la
data en MSSQL. Ya pasó una vez: client_notes, followups y saved_replies tenían
DDL, función de upsert, listener y backfill, pero faltaban en MIRROR_TABLES, así
que /system las daba por buenas sin contarlas (arreglado en 5921bfd). Ninguna
prueba lo detectó.

Los cinco lugares:
  1. app/archive.py `_SCHEMA_DDL`        — el CREATE TABLE
  2. app/archive.py `archive_<algo>()`   — el upsert
  3. app/archive.py `_register_listeners()` — el after_insert/after_update
  4. app/archive.py `backfill()`         — el bucle idempotente SQLite -> MSSQL
  5. app/system.py  `MIRROR_TABLES`      — para que /system cuente la tabla

Estas pruebas leen el CÓDIGO (AST de app/archive.py + los objetos en runtime),
no la base: no necesitan MSSQL, no tocan red y corren en milisegundos. El lugar 1
es la fuente de verdad — los otros cuatro se verifican contra él.
"""

import ast
import re
from pathlib import Path

import pytest
from sqlmodel import Session

import app.archive as archive
import app.db as db
import app.system as system
from app.config import settings
from app.models import User

# --------------------------------------------------------------------------
# Lugar 1: el DDL declara qué tablas existen en el espejo. Es la fuente de verdad.
# --------------------------------------------------------------------------
_OBJECT_ID_RE = re.compile(r"IF\s+OBJECT_ID\(\s*'([A-Za-z_][A-Za-z0-9_]*)'")
_CREATE_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)")
# Identificadores SQL/Python dentro de un literal; se comparan como token
# completo para que "ticket_activity" nunca cuente como "tickets".
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _bloques_ddl() -> list[tuple[str, str]]:
    """(tabla protegida por el guard, tabla que el bloque realmente crea)."""
    bloques = []
    for ddl in archive._SCHEMA_DDL:
        guard = _OBJECT_ID_RE.search(ddl)
        creada = _CREATE_TABLE_RE.search(ddl)
        assert guard and creada, (
            "Bloque de _SCHEMA_DDL sin IF OBJECT_ID(...) / CREATE TABLE legibles; "
            f"esta prueba ya no puede auditar el espejo. Bloque: {ddl.strip()[:120]!r}"
        )
        bloques.append((guard.group(1), creada.group(1)))
    return bloques


BLOQUES_DDL = _bloques_ddl()
TABLAS_DDL = frozenset(creada for _, creada in BLOQUES_DDL)

# --------------------------------------------------------------------------
# Introspección de app/archive.py por AST (lugares 2, 3 y 4).
# --------------------------------------------------------------------------
_ARBOL = ast.parse(Path(archive.__file__).read_text(encoding="utf-8"))
_FUNCS = {n.name: n for n in _ARBOL.body if isinstance(n, ast.FunctionDef)}


def _tablas_en_literales(nodo: ast.AST) -> set[str]:
    """Tablas del espejo nombradas en algún literal de cadena del nodo.

    Cubre tanto el SQL a mano (`INSERT INTO wa_messages ...`) como el upsert
    genérico, donde la tabla viaja como primer argumento (`_upsert("tickets", ...)`).
    """
    encontradas: set[str] = set()
    for sub in ast.walk(nodo):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            encontradas |= {t for t in _IDENT_RE.findall(sub.value) if t in TABLAS_DDL}
    return encontradas


def _llamadas(nodo: ast.AST) -> set[str]:
    """Funciones del módulo invocadas por nombre simple dentro del nodo."""
    return {
        sub.func.id
        for sub in ast.walk(nodo)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
    }


def _tablas_que_escribe(funcion: str) -> frozenset[str]:
    """Cierre transitivo: tablas que toca `funcion`, directa o vía helpers.

    Hace falta el cierre porque no toda función nombra su tabla: `sync_client_name`
    delega en `_upsert_client`, y ahí es donde aparece 'wa_clients'.
    """
    vistos: set[str] = set()
    pendientes = [funcion]
    tablas: set[str] = set()
    while pendientes:
        actual = pendientes.pop()
        if actual in vistos or actual not in _FUNCS:
            continue
        vistos.add(actual)
        tablas |= _tablas_en_literales(_FUNCS[actual])
        pendientes.extend(_llamadas(_FUNCS[actual]))
    return frozenset(tablas)


# Lugar 2 — funciones de upsert (el prefijo archive_/sync_ es la convención del
# módulo). Se excluye a propósito backfill(), que llama a todas: incluirla haría
# la prueba del lugar 2 trivialmente verde.
FUNCIONES_ESPEJO = {
    nombre: _tablas_que_escribe(nombre)
    for nombre in _FUNCS
    if nombre.startswith(("archive_", "sync_"))
}
TABLAS_CON_FUNCION = frozenset().union(*FUNCIONES_ESPEJO.values(), frozenset())


def _listeners() -> dict[str, set[str]]:
    """Modelo SQLModel -> funciones que dispara su listener en _register_listeners()."""
    registro: dict[str, set[str]] = {}
    for interna in _FUNCS["_register_listeners"].body:
        if not isinstance(interna, ast.FunctionDef):
            continue
        modelos = {
            dec.args[0].id
            for dec in interna.decorator_list
            if isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr == "listens_for"
            and dec.args
            and isinstance(dec.args[0], ast.Name)
        }
        # El cuerpo siempre es `_safe(archive_<algo>, target)`: el primer
        # argumento es la función que hace la escritura real.
        funciones = {
            sub.args[0].id
            for sub in ast.walk(interna)
            if isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "_safe"
            and sub.args
            and isinstance(sub.args[0], ast.Name)
        }
        for modelo in modelos:
            registro.setdefault(modelo, set()).update(funciones)
    return registro


# Lugar 3
LISTENERS = _listeners()
TABLAS_POR_MODELO = {
    modelo: frozenset().union(
        *(_tablas_que_escribe(f) for f in funciones), frozenset()
    )
    for modelo, funciones in LISTENERS.items()
}
TABLAS_CON_LISTENER = frozenset().union(*TABLAS_POR_MODELO.values(), frozenset())

# Lugar 4
TABLAS_EN_BACKFILL = frozenset().union(
    *(_tablas_que_escribe(f) for f in _llamadas(_FUNCS["backfill"])), frozenset()
)

# Lugar 5
TABLAS_EN_SYSTEM = [nombre for _, nombre in system.MIRROR_TABLES]
TABLA_POR_MODELO_EN_SYSTEM = {
    modelo.__name__: nombre for modelo, nombre in system.MIRROR_TABLES
}

# --------------------------------------------------------------------------
# Excepciones conocidas — declaradas, no escondidas.
# --------------------------------------------------------------------------

# team_users NO es tabla de dominio: es el catálogo de la FK de team_messages
# (`CONSTRAINT fk_team_messages_user`), y se puebla como efecto colateral dentro
# de archive_team_message (app/archive.py:464-468). No tiene modelo SQLModel
# propio, así que no puede aparecer ni en MIRROR_TABLES ni en backfill(): se
# llena sola cada vez que se espeja un mensaje de equipo.
SIN_ENTRADA_EN_SYSTEM = frozenset({"team_users"})

# backfill() excluye a propósito wa_messages y team_messages: esas tablas usan
# UNIQUE(source_id) + INSERT directo, así que reinsertar rompería la constraint
# (comentario en app/archive.py, dentro de backfill()). team_users va en el mismo
# paquete porque solo se escribe junto con team_messages.
SIN_BACKFILL = frozenset({"wa_messages", "team_messages", "team_users"})

_LUGARES = (
    "los CINCO lugares del espejo (CLAUDE.md): 1) _SCHEMA_DDL, 2) archive_<algo>(), "
    "3) _register_listeners(), 4) backfill(), 5) MIRROR_TABLES en app/system.py"
)


def test_cada_bloque_ddl_protege_la_tabla_que_crea():
    """REGLA: el guard IF OBJECT_ID debe nombrar la misma tabla del CREATE TABLE.

    Los bloques se agregan copiando el anterior; si se renombra el CREATE pero no
    el guard, la tabla nueva NUNCA se crea (el guard ve la vieja, que ya existe)
    y todas sus escrituras fallan en silencio dentro de _safe().
    """
    desalineados = [(g, c) for g, c in BLOQUES_DDL if g != c]

    assert not desalineados, (
        "Bloques de _SCHEMA_DDL cuyo IF OBJECT_ID no coincide con su CREATE TABLE "
        f"(guard, creada): {desalineados}. La tabla creada nunca se crearía si la "
        "tabla del guard ya existe."
    )


def test_las_excepciones_declaradas_siguen_existiendo_en_el_ddl():
    """REGLA: una lista blanca que apunta a tablas fantasma deja de proteger.

    Si alguien renombra o elimina team_users / wa_messages / team_messages, estas
    excepciones se vuelven letra muerta y taparían un olvido real.
    """
    fantasma = sorted((SIN_ENTRADA_EN_SYSTEM | SIN_BACKFILL) - TABLAS_DDL)

    assert not fantasma, (
        f"Excepciones declaradas en esta prueba que ya no existen en _SCHEMA_DDL: "
        f"{fantasma}. Actualiza (o borra) la lista blanca: mientras siga ahí, "
        "silencia olvidos reales."
    )


def test_lugar_2_toda_tabla_del_ddl_tiene_funcion_de_upsert():
    """REGLA: sin archive_<algo>() nadie escribe esa tabla; queda vacía para siempre."""
    faltantes = sorted(TABLAS_DDL - TABLAS_CON_FUNCION)

    assert not faltantes, (
        f"Tablas con CREATE TABLE en _SCHEMA_DDL pero sin ninguna función "
        f"archive_*/sync_* que las escriba: {faltantes}. Falta el LUGAR 2 de "
        f"{_LUGARES}."
    )


def test_lugar_3_toda_tabla_del_ddl_tiene_listener_registrado():
    """REGLA: sin listener after_insert/after_update, los cambios nuevos no se espejan."""
    faltantes = sorted(TABLAS_DDL - TABLAS_CON_LISTENER)

    assert not faltantes, (
        f"Tablas del espejo sin listener en _register_listeners(): {faltantes}. "
        f"Las escrituras nuevas en SQLite no llegarían a MSSQL. Falta el LUGAR 3 de "
        f"{_LUGARES}."
    )


def test_lugar_4_toda_tabla_del_ddl_se_repara_en_backfill():
    """REGLA: sin bucle en backfill(), 'Sincronizar ahora' no puede reparar la tabla
    tras una caída de MSSQL ni traer el histórico previo al espejo."""
    faltantes = sorted(TABLAS_DDL - TABLAS_EN_BACKFILL - SIN_BACKFILL)

    assert not faltantes, (
        f"Tablas del espejo que backfill() no recorre: {faltantes}. Tras una caída "
        f"de MSSQL quedarían desincronizadas para siempre. Falta el LUGAR 4 de "
        f"{_LUGARES}."
    )


def test_lugar_5_toda_tabla_del_ddl_esta_en_mirror_tables():
    """REGLA: lo que /system no cuenta, nadie lo audita. Ese fue exactamente el bug
    de client_notes/followups/saved_replies arreglado en 5921bfd."""
    faltantes = sorted(TABLAS_DDL - set(TABLAS_EN_SYSTEM) - SIN_ENTRADA_EN_SYSTEM)

    assert not faltantes, (
        f"Tablas del espejo ausentes de MIRROR_TABLES (app/system.py): {faltantes}. "
        f"/system no compara sus conteos, así que una divergencia sería invisible. "
        f"Falta el LUGAR 5 de {_LUGARES}."
    )


def test_mirror_tables_no_inventa_ni_duplica_tablas():
    """REGLA: /system consulta `SELECT COUNT(*) FROM <nombre>` con el nombre literal
    de MIRROR_TABLES; un nombre que no existe en el DDL revienta la consulta y deja
    TODA la sección del espejo sin datos (el except captura la fuga completa)."""
    inexistentes = sorted(set(TABLAS_EN_SYSTEM) - TABLAS_DDL)
    repetidas = sorted({n for n in TABLAS_EN_SYSTEM if TABLAS_EN_SYSTEM.count(n) > 1})

    assert not inexistentes, (
        f"MIRROR_TABLES nombra tablas que no existen en _SCHEMA_DDL: {inexistentes}. "
        "Revisa el nombre en app/system.py o agrega el CREATE TABLE."
    )
    assert not repetidas, (
        f"MIRROR_TABLES repite tablas: {repetidas}. /system las contaría dos veces."
    )


def test_mirror_tables_empareja_cada_modelo_con_la_tabla_que_su_listener_escribe():
    """REGLA: /system compara el COUNT del modelo SQLite contra el COUNT de la tabla
    MSSQL del par. Si el par está cruzado (modelo A con tabla B) el tablero muestra
    un desfase permanente que nadie puede explicar, o peor: da por buena una tabla
    que nadie espeja."""
    cruzados = []
    for modelo, tabla in TABLA_POR_MODELO_EN_SYSTEM.items():
        escribe = TABLAS_POR_MODELO.get(modelo, frozenset())
        if tabla not in escribe:
            cruzados.append((modelo, tabla, sorted(escribe) or "sin listener"))

    assert not cruzados, (
        "Pares (modelo, tabla) de MIRROR_TABLES que no coinciden con lo que el "
        f"listener de ese modelo escribe realmente — (modelo, tabla declarada, "
        f"tablas que sí escribe): {cruzados}."
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG B3 (deuda conocida, NO se arregla aquí): wa_messages y team_messages "
        "están en MIRROR_TABLES pero backfill() los excluye a propósito por la "
        "constraint UNIQUE(source_id). Consecuencia: tras una caída de MSSQL "
        "/system los marca ok=False para siempre y 'Sincronizar ahora' no puede "
        "repararlos. Al arreglarlo (p.ej. upsert por source_id) esta prueba pasará "
        "en XPASS y hay que quitar el marcador."
    ),
)
def test_lo_que_system_reporta_lo_puede_reparar_sincronizar_ahora():
    """REGLA: si /system muestra una tabla como desincronizada, el botón
    'Sincronizar ahora' (que corre backfill()) debería poder repararla."""
    irreparables = sorted(set(TABLAS_EN_SYSTEM) - TABLAS_EN_BACKFILL)

    assert not irreparables, (
        f"Tablas que /system audita pero backfill() no repara: {irreparables}."
    )


def test_mirror_status_sin_mssql_reporta_una_fila_por_tabla_sin_reventar(monkeypatch):
    """REGLA: /system es la única ventana a la salud del espejo. Con MSSQL apagado
    (o caído) tiene que seguir renderizando: una fila por cada entrada de
    MIRROR_TABLES, con el conteo local real y el remoto en desconocido.
    Nunca debe propagar la excepción — el espejo es best-effort.
    """
    # Espejo apagado de forma explícita: la prueba no depende del .env local.
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "mssql_password", "")
    assert archive.enabled() is False

    # db.engine es el que parcha el fixture autouse _isolated_db.
    with Session(db.engine) as s:
        s.add(User(username="jefa", password_hash="x", is_admin=True))
        s.commit()

    filas = system.mirror_status()

    # Una fila por entrada, ni una menos: se compara el conjunto y la cantidad
    # (no el orden, que la vista puede reordenar sin romper nada).
    assert {f["table"] for f in filas} == set(TABLAS_EN_SYSTEM), (
        "mirror_status() debe reportar exactamente las tablas de MIRROR_TABLES; "
        f"sobran/faltan: {sorted(set(TABLAS_EN_SYSTEM) ^ {f['table'] for f in filas})}"
    )
    assert len(filas) == len(TABLAS_EN_SYSTEM), (
        f"mirror_status() devolvió {len(filas)} filas para "
        f"{len(TABLAS_EN_SYSTEM)} entradas de MIRROR_TABLES."
    )
    # Conteo local real: app_users tiene el colaborador recién creado.
    por_tabla = {f["table"]: f for f in filas}
    assert por_tabla["app_users"]["sqlite"] == 1
    for fila in filas:
        assert fila["mssql"] is None, (
            f"Sin MSSQL no hay conteo remoto que reportar: {fila}"
        )
        # Sin conexión no se puede afirmar que la tabla está al día.
        assert not fila["ok"], f"Fila marcada como sincronizada sin MSSQL: {fila}"
