"""Resolución de credenciales por oficina con herencia al .env — SPEC §3.

La prueba que sostiene el PR entero es `test_una_fila_viva_le_gana_al_env`: sin
ella, un error en el import, en el WHERE o en la derivación del provider deja
TODA la suite verde —porque la herencia devuelve el valor correcto— y producción
nunca lee una fila, para siempre. Es lo único que distingue "funciona" de
"no-op silencioso".

Nota de siembra: `conftest.py` le da a `db.engine` una base en memoria propia
por test, y la fixture `session` usa OTRO engine. Aquí se siembra SIEMPRE con
`Session(db.engine)` y se lee sin `session=`, para que ambos lados hablen de la
misma base — mezclarlos daría un verde que solo prueba la herencia.
"""

import pytest
from sqlalchemy import text
from sqlmodel import Session, select

import app.credenciales as credenciales
import app.db as db
from app.config import CREDENCIALES, settings
from app.credenciales import CredencialIlegible, cifrar, credencial
from app.models import (
    OFICINA_TLAXCALA_ID,
    Direction,
    Message,
    Office,
    OfficeCredential,
)

DEL_ENV = "sk_test_del_env"
DE_LA_FILA = "sk_test_de_la_fila"


@pytest.fixture()
def cryptography_instalada():
    """`cryptography` se importa AQUÍ, nunca a nivel de módulo.

    El import de arriba reabría el agujero exacto que el import perezoso e
    INDENTADO de `credenciales.py` está puesto para cerrar: `conftest.py`
    importa `app.db` en tiempo de COLECCIÓN, así que en un venv sin la
    dependencia —el escenario que ese docstring documenta como real— este
    archivo entero moría al recolectarse. Y con él las OCHO pruebas que
    demuestran que el camino sin cifrado sigue vivo (herencia, no-caché, tabla
    ausente, firma, clave desconocida, espejo), que son justamente las que
    tienen que seguir verdes ahí.

    Como fixture, solo se salta lo que de verdad necesita cifrar.
    """
    return pytest.importorskip("cryptography.fernet")


@pytest.fixture()
def llave_maestra(monkeypatch, cryptography_instalada):
    """CREDENTIALS_KEY real y descartable: las pruebas de cifrado no pueden
    depender del .env del desarrollador (patrón de las fixtures de conftest)."""
    llave = cryptography_instalada.Fernet.generate_key().decode()
    monkeypatch.setattr(settings, "credentials_key", llave)


@pytest.fixture()
def stripe_del_env(monkeypatch):
    monkeypatch.setattr(settings, "stripe_secret_key", DEL_ENV)


def _sembrar_oficina(office_id: int = OFICINA_TLAXCALA_ID) -> None:
    """`credencial()` no toca `office`, pero la fila es la que da sentido al
    office_id. conftest usa create_all(), no init_db(): no la siembra nadie."""
    with Session(db.engine) as s:
        s.add(
            Office(
                id=office_id,
                nombre=f"Oficina {office_id}",
                slug=f"oficina-{office_id}",
                ciudad="Tlaxcala",
            )
        )
        s.commit()


def _sembrar_credencial(
    clave: str,
    valor: str | None = None,
    *,
    office_id: int = OFICINA_TLAXCALA_ID,
    provider: str = "stripe",
    habilitado: bool = True,
    valor_cif: bytes | None = None,
) -> None:
    cif = valor_cif if valor_cif is not None else cifrar(valor or "")[0]
    with Session(db.engine) as s:
        s.add(
            OfficeCredential(
                office_id=office_id,
                provider=provider,
                clave=clave,
                valor_cif=cif,
                pista=(valor or "")[-4:],
                habilitado=habilitado,
                updated_by="prueba",
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# La invariante: la fila viva gana; si no hay fila, se hereda
# ---------------------------------------------------------------------------


def test_una_fila_viva_le_gana_al_env(llave_maestra, stripe_del_env):
    """LA prueba load-bearing: es lo único que distingue el PR de un no-op."""
    _sembrar_oficina()
    _sembrar_credencial("STRIPE_SECRET_KEY", DE_LA_FILA)

    valor = credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)

    assert valor == DE_LA_FILA
    assert valor != settings.stripe_secret_key  # no pasó por la herencia


def test_sin_filas_hereda_del_env(stripe_del_env):
    """El 100 % de producción hoy y de la suite: officecredential vacía."""
    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DEL_ENV


def test_la_herencia_se_reevalua_en_cada_llamada(monkeypatch):
    """Nada se memoiza: si alguien mete un caché, truena aquí y ruidoso, en vez
    de romper difusamente las monkeypatch.setattr(settings, ...) de la suite."""
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_primero")
    primera = credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)

    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_segundo")
    segunda = credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)

    assert primera == "sk_test_primero"
    assert segunda == "sk_test_segundo"


def test_fila_deshabilitada_no_gana(llave_maestra, stripe_del_env):
    """Baja = habilitado=0, nunca DELETE (§2.2). El WHERE la tiene que filtrar."""
    _sembrar_credencial("STRIPE_SECRET_KEY", DE_LA_FILA, habilitado=False)

    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DEL_ENV


def test_fila_de_otra_oficina_no_se_ve(llave_maestra, stripe_del_env):
    """El aislamiento entre oficinas: el riesgo #1 de §10."""
    _sembrar_credencial("STRIPE_SECRET_KEY", DE_LA_FILA, office_id=2)

    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DEL_ENV


def test_provider_distinto_no_casa_y_hereda(llave_maestra, stripe_del_env):
    """Constricción que este PR le hereda al PR 3: el panel DEBE escribir
    `provider` desde el inventario. Una fila con el provider equivocado no casa
    con el WHERE y hereda EN SILENCIO — por eso `provider` no está en la firma
    de credencial(), se deriva."""
    _sembrar_credencial("STRIPE_SECRET_KEY", DE_LA_FILA, provider="meta")

    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DEL_ENV


def test_una_fila_de_una_llave_compartida_SI_gana(llave_maestra, monkeypatch):
    """Fija el RECHAZO del atajo de llaves compartidas.

    `compartida=True` es metadata de render para el panel del PR 3 («heredada
    del global», §5), NO una regla del resolvedor. Con un atajo, una fila para
    la oficina 1 se ignoraría para siempre y el panel mentiría al rotar la llave.
    """
    assert CREDENCIALES["WHATSAPP_APP_SECRET"].compartida is True
    monkeypatch.setattr(settings, "app_secret", "secreto-del-env")
    _sembrar_credencial("WHATSAPP_APP_SECRET", "secreto-de-la-fila", provider="meta")

    valor = credencial("WHATSAPP_APP_SECRET", office_id=OFICINA_TLAXCALA_ID)

    assert valor == "secreto-de-la-fila"


# ---------------------------------------------------------------------------
# Fila ilegible: ruidosa SIEMPRE, jamás degrada a la herencia
# ---------------------------------------------------------------------------


def test_valor_cifrado_corrupto_levanta_y_no_hereda(llave_maestra, stripe_del_env):
    """La segunda mitad es la que importa: NO devolvió el valor del .env.

    Heredar aquí sería firmar o cobrar con el secreto de otra oficina; devolver
    "" haría que webhook/stripe_webhook lo lean como «no configurado» y tiren
    403 a todo el tráfico con un diagnóstico falso.
    """
    _sembrar_credencial("STRIPE_SECRET_KEY", valor_cif=b"basura")

    with pytest.raises(CredencialIlegible):
        credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)


def test_sin_credentials_key_con_fila_levanta(llave_maestra, stripe_del_env, monkeypatch):
    """Misma respuesta que el texto corrupto, causa distinta: llave maestra
    ausente. El operador tiene que ver CREDENTIALS_KEY, no «revisa Stripe»."""
    _sembrar_credencial("STRIPE_SECRET_KEY", DE_LA_FILA)
    monkeypatch.setattr(settings, "credentials_key", "")

    with pytest.raises(CredencialIlegible) as exc:
        credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)

    assert "CREDENTIALS_KEY" in str(exc.value)


def test_sin_credentials_key_y_sin_filas_no_truena(monkeypatch, stripe_del_env):
    """El estado del día del despliegue: 0 filas, CREDENTIALS_KEY ausente.
    _descifrar() no corre nunca, así que la llave maestra es inobservable."""
    monkeypatch.setattr(settings, "credentials_key", "")

    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DEL_ENV


def test_la_excepcion_no_lleva_el_valor_ni_el_cifrado(llave_maestra, monkeypatch):
    """El mensaje lleva oficina/proveedor/clave y NADA más: estas excepciones
    acaban en un log de producción."""
    secreto = "sk_live_SUPERSECRETO"
    _sembrar_credencial("STRIPE_SECRET_KEY", secreto)
    monkeypatch.setattr(settings, "credentials_key", "")

    with pytest.raises(CredencialIlegible) as exc:
        credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)

    texto = f"{exc.value!s} {exc.value!r}"
    assert secreto not in texto
    assert "SUPERSECRETO" not in texto
    assert "stripe" in texto and "STRIPE_SECRET_KEY" in texto
    assert str(OFICINA_TLAXCALA_ID) in texto


def test_descifrar_no_se_invoca_con_la_tabla_vacia(monkeypatch, stripe_del_env):
    """Versión determinista de «cryptography no se importa ni una vez», sin
    depender de sys.modules: con 0 filas el descifrado es inalcanzable."""

    def _explota(*_a, **_k):
        raise AssertionError("_descifrar no debe invocarse sin filas")

    monkeypatch.setattr(credenciales, "_descifrar", _explota)

    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DEL_ENV
    assert credencial("WHATSAPP_TOKEN", office_id=OFICINA_TLAXCALA_ID) is not None


# ---------------------------------------------------------------------------
# El SELECT puede fallar; el descifrado no se protege
# ---------------------------------------------------------------------------


def _tirar_la_tabla() -> None:
    with db.engine.connect() as c:
        c.execute(text("DROP TABLE officecredential"))
        c.commit()


def test_tabla_ausente_hereda_en_vez_de_fallar(stripe_del_env):
    """No es hipotético: el tramites.db local de este repo está exactamente en
    ese estado (base anterior al PR 1, sin init_db()). HOY ningún punto de
    lectura puede fallar al leer configuración, y este PR no estrena esa clase
    de fallo."""
    _tirar_la_tabla()

    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DEL_ENV


def test_un_select_fallido_no_envenena_la_sesion_del_llamador(stripe_del_env):
    """Es lo que hace seguro el parámetro `session=`: la escritura pendiente del
    llamador sobrevive al commit() aunque el SELECT de credenciales reviente."""
    _tirar_la_tabla()

    with Session(db.engine) as s:
        s.add(Message(phone="5215500000001", direction=Direction.OUT, body="pendiente"))

        valor = credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID, session=s)
        s.commit()

    assert valor == DEL_ENV
    with Session(db.engine) as s:
        fila = s.exec(select(Message).where(Message.phone == "5215500000001")).one()
        assert fila.body == "pendiente"


def test_una_lectura_anidada_TRAS_UN_FLUSH_pierde_la_escritura_pendiente(monkeypatch):
    """Por qué el ContentSid se lee ANTES del primer `session.add`, no después.

    Ésta es la forma real de `dashboard.py::_notify_finish`: una sesión abierta
    con escrituras pendientes y una lectura que abre su PROPIA sesión. Lo que
    decide el resultado es el FLUSH, y la versión anterior de esta prueba lo
    omitía — así que medía el caso benigno mientras su nombre prometía el otro:

    * sin flush, la fila vive todavía en la identity map de Python: la sesión
      anidada ni la ve, y el `commit()` posterior la escribe. Verde.
    * con flush —lo que provoca cualquier `session.exec()` intermedio— la fila
      ya está en la transacción abierta de la conexión DBAPI. Bajo
      `SingletonThreadPool` la sesión anidada usa ESA MISMA conexión y al
      cerrarse la devuelve al pool con rollback: la escritura pendiente se
      PIERDE y el `commit()` del llamador no protesta. Medido: 0 filas.

    Por eso la lectura se movió arriba del primer `add` en vez de "arreglarse"
    pasando `session=`. El guardia de COMPORTAMIENTO de ese flujo es
    `tests/test_claim.py::test_el_aviso_y_la_encuesta_se_guardan`; esta prueba
    fija el MECANISMO, para que nadie vuelva a bajar la lectura creyendo que da
    igual dónde va.
    """
    monkeypatch.setattr(settings, "twilio_content_sid_feedback", "HXdelenv")

    with Session(db.engine) as s:
        s.add(Message(phone="5215500000002", direction=Direction.OUT, body="pendiente"))
        s.flush()  # el detalle que decide todo: sin él la fila sí sobrevive

        valor = credencial("TWILIO_CONTENT_SID_FEEDBACK", office_id=OFICINA_TLAXCALA_ID)
        s.commit()

    # La lectura sí devuelve lo correcto: el daño es MUDO, que es lo peligroso.
    assert valor == "HXdelenv"
    with Session(db.engine) as s:
        perdidas = s.exec(select(Message).where(Message.phone == "5215500000002")).all()

    assert perdidas == [], (
        "la escritura pendiente sobrevivió a la lectura anidada: cambió el pool "
        "o el manejo de sesión de credencial(). Si leer después de un add ya es "
        "seguro, dilo aquí y revisa si dashboard.py todavía necesita leer el "
        "ContentSid antes del primer session.add."
    )


def test_sin_flush_la_lectura_anidada_no_estorba(monkeypatch):
    """El control de la de arriba: el MISMO código sin `flush()` y la fila vive.

    Es lo que vuelve al flush la variable que se está midiendo. Sin este par, la
    prueba de arriba podría estar en verde por cualquier otra razón —una fixture
    que limpia, un teléfono mal escrito— y nadie se enteraría."""
    monkeypatch.setattr(settings, "twilio_content_sid_feedback", "HXdelenv")

    with Session(db.engine) as s:
        s.add(Message(phone="5215500000003", direction=Direction.OUT, body="pendiente"))

        valor = credencial("TWILIO_CONTENT_SID_FEEDBACK", office_id=OFICINA_TLAXCALA_ID)
        s.commit()

    assert valor == "HXdelenv"
    with Session(db.engine) as s:
        assert s.exec(select(Message).where(Message.phone == "5215500000003")).one()


# ---------------------------------------------------------------------------
# Contrato de la firma
# ---------------------------------------------------------------------------


def test_devuelve_str_nunca_none(monkeypatch, llave_maestra):
    """stripe_client.modo_real() es `"_live_" in <valor>` y _filtro_modo corre en
    CADA render de /cobros: un None sería TypeError y un 500 en cobranza."""
    for meta in CREDENCIALES.values():
        monkeypatch.setattr(settings, meta.atributo, "")

    for clave in CREDENCIALES:
        valor = credencial(clave, office_id=OFICINA_TLAXCALA_ID)
        assert isinstance(valor, str), clave
        assert valor == "", clave

    # Y por el camino de la FILA, que es donde el contrato puede romperse de
    # verdad: un _descifrar() al que le quiten el .decode() devolvería bytes, y
    # con la tabla vacía esta prueba pasaría igual sin notarlo.
    _sembrar_credencial("STRIPE_SECRET_KEY", DE_LA_FILA)
    de_la_fila = credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID)
    assert isinstance(de_la_fila, str)
    assert de_la_fila == DE_LA_FILA


def test_una_clave_desconocida_falla_ruidosa():
    """Un typo muere en el call site. Un "" mudo sería el fail-closed que
    descarta todo el tráfico sin decir por qué."""
    with pytest.raises(KeyError, match="config.CREDENCIALES"):
        credencial("WHATSAPP_TOKEM", office_id=OFICINA_TLAXCALA_ID)


def test_office_id_es_keyword_only_y_sin_default():
    """fail-closed de SPEC §4.1: NUNCA cae a Tlaxcala por omisión. Sin default
    no hay nada que «olvidar pasar», y grep OFICINA_TLAXCALA_ID es la lista de
    trabajo exhaustiva de los PR 4 y 5."""
    import inspect

    firma = inspect.signature(credencial)
    param = firma.parameters["office_id"]

    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    assert firma.parameters["session"].default is None

    with pytest.raises(TypeError):
        credencial("STRIPE_SECRET_KEY")


# ---------------------------------------------------------------------------
# cifrar(): round-trip y la regla de la pista
# ---------------------------------------------------------------------------


def test_cifrar_y_descifrar_ida_y_vuelta(llave_maestra):
    """Y que dos cifrados del mismo valor dan bytes DISTINTOS (Fernet lleva IV +
    timestamp): que nadie asuma determinismo para deduplicar o comparar."""
    _sembrar_credencial("STRIPE_SECRET_KEY", DE_LA_FILA)

    assert credencial("STRIPE_SECRET_KEY", office_id=OFICINA_TLAXCALA_ID) == DE_LA_FILA
    assert cifrar(DE_LA_FILA)[0] != cifrar(DE_LA_FILA)[0]


def test_cifrar_devuelve_pista_de_cuatro_caracteres(llave_maestra):
    """La regla de §3.2 vive en un solo lugar, para el panel del PR 3."""
    _, pista = cifrar("sk_live_51ABCDEF")

    assert pista == "CDEF"
    assert len(pista) == 4


def test_cifrar_sin_llave_maestra_es_ruidoso(monkeypatch, cryptography_instalada):
    """Pide la fixture aunque no cifre nada: `cifrar()` hace el import perezoso
    ANTES de revisar la llave, así que sin la dependencia esto daría ImportError
    en vez del RuntimeError que la prueba afirma."""
    monkeypatch.setattr(settings, "credentials_key", "")

    with pytest.raises(RuntimeError, match="CREDENTIALS_KEY"):
        cifrar("lo-que-sea")


# ---------------------------------------------------------------------------
# El espejo MSSQL NO conoce esta tabla — §2.2 y §9
# ---------------------------------------------------------------------------


def test_officecredential_no_esta_en_el_espejo_mssql():
    """Fija la excepción a la regla de los cinco lugares ANTES de que alguien
    «complete» el espejo por costumbre: los secretos por oficina no salen de
    SQLite."""
    from app import archive, system

    assert "officecredential" not in "\n".join(archive._SCHEMA_DDL).lower()
    assert OfficeCredential not in [modelo for modelo, _ in system.MIRROR_TABLES]
    assert "officecredential" not in [nombre.lower() for _, nombre in system.MIRROR_TABLES]
