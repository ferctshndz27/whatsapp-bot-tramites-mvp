"""Ruteo de entrada por oficina — docs/SPEC-MULTI-OFICINA.md §4 (PR 4).

LA PRUEBA QUE DECIDE SI EL DESPLIEGUE ES SEGURO es la primera: con
`officecredential` VACÍA, sin `CREDENTIALS_KEY` y el identificador solo en el
`.env` —la forma EXACTA de producción hoy— el ruteo tiene que resolver la
oficina 1. Si eso falla, desplegar este PR antes de sembrar las filas del paso 5
de §7 descarta todo el tráfico entrante, que es el peor modo de fallo del
proyecto: el cliente escribe y nadie se entera.

El resto cubre el fail-closed, que es la otra mitad: un identificador
desconocido NO puede caer a Tlaxcala por omisión.
"""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import credenciales, ruteo
from app.config import settings
from app.models import Office, OfficeCredential

PNID_TLAXCALA = "109876543210987"
FROM_TLAXCALA = "whatsapp:+5212461234567"


@pytest.fixture()
def bd(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    yield engine
    engine.dispose()


# --- La prueba del despliegue ----------------------------------------------

def test_rutea_con_la_tabla_vacia_y_sin_credentials_key(bd, monkeypatch):
    """FORMA EXACTA DE PRODUCCIÓN el día del despliegue: cero filas, el
    identificador solo en el .env, y ninguna CREDENTIALS_KEY configurada.

    Que esto pase es lo que convierte la siembra del paso 5 en una optimización
    en vez de un requisito previo — y lo que hace que este PR se pueda desplegar
    sin coordinar un script con el deploy.
    """
    monkeypatch.setattr(settings, "credentials_key", "")
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", PNID_TLAXCALA)
    monkeypatch.setattr(settings, "twilio_whatsapp_from", FROM_TLAXCALA)
    with Session(bd) as s:
        assert s.exec(select(OfficeCredential)).all() == [], "la prueba dejó de valer"

    assert ruteo.resolver_oficina_meta(PNID_TLAXCALA) == 1
    assert ruteo.resolver_oficina_twilio(FROM_TLAXCALA) == 1


# --- Fail-closed -----------------------------------------------------------

@pytest.mark.parametrize("desconocido", ["", "000000000000000", "otro"])
def test_un_phone_number_id_desconocido_no_cae_a_tlaxcala(bd, monkeypatch, desconocido):
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", PNID_TLAXCALA)
    assert ruteo.resolver_oficina_meta(desconocido) is None


def test_un_to_desconocido_no_cae_a_tlaxcala(bd, monkeypatch):
    monkeypatch.setattr(settings, "twilio_whatsapp_from", FROM_TLAXCALA)
    assert ruteo.resolver_oficina_twilio("whatsapp:+5215599999999") is None


def test_sin_identificador_en_el_env_no_rutea_nada(bd, monkeypatch):
    # Un .env a medio configurar no puede hacer que CUALQUIER número resuelva a
    # la oficina 1: comparar "" == "" casaría con todo.
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", "")
    assert ruteo.resolver_oficina_meta(PNID_TLAXCALA) is None
    assert ruteo.resolver_oficina_meta("") is None


# --- Con filas reales ------------------------------------------------------

def test_cada_oficina_resuelve_a_la_suya(bd, monkeypatch):
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", PNID_TLAXCALA)
    with Session(bd) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        s.commit()
    credenciales.guardar("WHATSAPP_PHONE_NUMBER_ID", office_id=2,
                         valor="222222222222222", updated_by="qa")

    assert ruteo.resolver_oficina_meta(PNID_TLAXCALA) == 1, "la 1 sigue heredando del .env"
    assert ruteo.resolver_oficina_meta("222222222222222") == 2


def test_una_oficina_deshabilitada_no_recibe(bd, monkeypatch):
    """Dar de baja una oficina tiene que cortar su entrada, no solo esconderla
    del panel: si no, sus mensajes siguen creando folios que nadie atiende."""
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())
    with Session(bd) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla", habilitado=False))
        s.commit()
    credenciales.guardar("WHATSAPP_PHONE_NUMBER_ID", office_id=2,
                         valor="222222222222222", updated_by="qa")

    assert ruteo.resolver_oficina_meta("222222222222222") is None


def test_una_fila_ilegible_no_tumba_a_las_demas_oficinas(bd, monkeypatch):
    """Una CREDENTIALS_KEY que no descifra la fila de UNA oficina no puede dejar
    sin servicio a las otras. La ilegible no rutea —fail-closed— y las demás sí.
    """
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())
    with Session(bd) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        s.commit()
    # La 1 con fila cifrada; la 2 hereda... al revés: la 1 queda ilegible.
    credenciales.guardar("WHATSAPP_PHONE_NUMBER_ID", office_id=1,
                         valor=PNID_TLAXCALA, updated_by="qa")
    credenciales.guardar("WHATSAPP_PHONE_NUMBER_ID", office_id=2,
                         valor="222222222222222", updated_by="qa")
    # Solo la fila de la 1 se vuelve ilegible: se re-cifra la de la 2 con la
    # llave nueva para que siga siendo legible.
    llave_nueva = Fernet.generate_key().decode()
    valor_2, _ = None, None
    monkeypatch.setattr(settings, "credentials_key", llave_nueva)
    with Session(bd) as s:
        fila_2 = s.exec(select(OfficeCredential).where(
            OfficeCredential.office_id == 2)).one()
        fila_2.valor_cif = credenciales.cifrar("222222222222222")[0]
        s.add(fila_2)
        s.commit()

    assert ruteo.resolver_oficina_meta(PNID_TLAXCALA) is None, "la ilegible no rutea"
    assert ruteo.resolver_oficina_meta("222222222222222") == 2, "las demás sí"


# --- Formato ---------------------------------------------------------------

@pytest.mark.parametrize("recibido", [
    "whatsapp:+5212461234567",   # como lo manda Twilio
    "+521 246 123 4567",         # tecleado con espacios en el panel
    "2461234567",                # nacional, sin lada
])
def test_el_to_de_twilio_se_compara_normalizado(bd, monkeypatch, recibido):
    """Comparar en crudo convertiría un espacio de más en un descarte silencioso
    de TODO el tráfico de esa oficina."""
    monkeypatch.setattr(settings, "twilio_whatsapp_from", FROM_TLAXCALA)
    assert ruteo.resolver_oficina_twilio(recibido) == 1


# --- Candados: que el ruteo no se pueda saltar por descuido ----------------

def test_el_webhook_nunca_deja_la_oficina_al_default():
    """handle_incoming lleva `office_id=OFICINA_TLAXCALA_ID` por default para no
    romper 229 call sites de la suite. Ese default es correcto en todos MENOS en
    uno: el webhook, el único camino donde la oficina es ambigua. Si alguien
    quita ahí el argumento, el ruteo se vuelve decorativo y todo aterriza en
    Tlaxcala en silencio — que es exactamente el fallo que §4 existe para evitar.
    """
    import ast
    from pathlib import Path

    fuente = (Path(__file__).resolve().parent.parent / "app" / "webhook.py").read_text("utf-8")
    llamadas = [
        nodo for nodo in ast.walk(ast.parse(fuente))
        if isinstance(nodo, ast.Call)
        and getattr(nodo.func, "id", getattr(nodo.func, "attr", "")) in
        ("handle_inbound", "handle_incoming", "_process_message")
    ]
    assert llamadas, "el detector dejó de encontrar las llamadas: candado ciego"
    for llamada in llamadas:
        nombre = getattr(llamada.func, "id", getattr(llamada.func, "attr", ""))
        pasa_oficina = (
            any(k.arg == "office_id" for k in llamada.keywords)
            # posicional: _process_message(msg, office_id) y handle_inbound(...6 args)
            or len(llamada.args) >= (2 if nombre == "_process_message" else 6)
        )
        assert pasa_oficina, (
            f"{nombre}() en webhook.py línea {llamada.lineno} no pasa office_id: "
            f"caería al default y el ruteo de §4 sería decorativo"
        )


def test_resolver_es_lo_unico_que_decide_la_oficina_del_entrante():
    """El webhook NO puede fabricar un office_id por su cuenta (un `or 1`, un
    literal de respaldo): tiene que salir del resolvedor o descartarse."""
    import ast
    from pathlib import Path

    fuente = (Path(__file__).resolve().parent.parent / "app" / "webhook.py").read_text("utf-8")
    inline = [
        f"línea {n.lineno}"
        for n in ast.walk(ast.parse(fuente))
        if isinstance(n, ast.keyword) and n.arg == "office_id" and isinstance(n.value, ast.Constant)
    ]
    assert inline == [], f"webhook.py fija una oficina a mano en {inline}"


def test_nadie_le_pasa_office_id_a_un_modelo_que_no_lo_tiene():
    """SQLModel ACEPTA Y DESCARTA en silencio un kwarg que no es columna: un
    `Message(office_id=...)` no da error, simplemente no hace nada, y la suite
    entera puede pasar sobre una asignación que se evapora. (Se descubrió así,
    al escribir el PR 4.)

    Se comprueba contra las columnas REALES de cada modelo, no contra una lista
    a mano que envejecería.
    """
    import ast
    from pathlib import Path

    from sqlmodel import SQLModel

    con_columna = {
        m.__name__ for m in SQLModel.__subclasses__()
        if getattr(m, "__tablename__", None) and "office_id" in m.model_fields
    }
    assert con_columna, "el detector no encontró modelos: candado ciego"

    raiz = Path(__file__).resolve().parent.parent / "app"
    errores = []
    for ruta in sorted(raiz.glob("*.py")):
        for nodo in ast.walk(ast.parse(ruta.read_text("utf-8"))):
            if not isinstance(nodo, ast.Call):
                continue
            nombre = getattr(nodo.func, "id", getattr(nodo.func, "attr", ""))
            if nombre in SQLModel.__subclasses__.__self__.__dict__:  # pragma: no cover
                continue
            if any(k.arg == "office_id" for k in nodo.keywords) and nombre and nombre[0].isupper():
                if nombre not in con_columna:
                    errores.append(f"{ruta.name}:{nodo.lineno} {nombre}(office_id=…)")
    assert errores == [], (
        f"office_id a un modelo que no tiene la columna — se descarta en silencio: {errores}"
    )


def test_el_ruteo_abre_UNA_sesion_sin_importar_cuantas_oficinas_haya(bd, monkeypatch):
    """REGLA: esto corre en el camino de CADA mensaje entrante.

    `credencial()` abre su propia sesión si no le prestan una, así que el bucle
    de `_buscar` hacía 1 + N aperturas por webhook: una para listar las oficinas
    y otra por cada oficina consultada. Con la única oficina de hoy el ahorro es
    una conexión y no se nota; el número sube con cada oficina que se dé de alta
    desde el panel, y para entonces nadie estaría mirando este bucle.

    Se cuenta la apertura, no el tiempo: un test de latencia sobre SQLite en
    memoria mediría el ruido de la máquina, no el cambio.
    """
    monkeypatch.setattr(settings, "whatsapp_phone_number_id", PNID_TLAXCALA)
    with Session(bd) as s:
        for i in range(2, 7):  # cinco oficinas más: seis en total
            s.add(Office(id=i, nombre=f"of{i}", slug=f"of{i}", ciudad="x"))
        s.commit()

    aperturas = 0
    original = db.get_session

    def contando(*args, **kwargs):
        nonlocal aperturas
        aperturas += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(db, "get_session", contando)
    monkeypatch.setattr(ruteo.db, "get_session", contando)

    assert ruteo.resolver_oficina_meta("nada-que-casa") is None  # recorre las seis

    assert aperturas == 1, (
        f"el ruteo abrió {aperturas} sesiones para 6 oficinas; con la sesión "
        "compartida es 1 sin importar cuántas haya"
    )
