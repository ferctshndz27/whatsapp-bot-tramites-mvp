"""El inventario `config.CREDENCIALES` amarrado a `config.py` y a la spec.

Estilo `tests/test_harness.py` / `tests/test_dependencias.py`: leen archivos del
repo como DATOS. `config.CREDENCIALES` duplica a propósito los literales de las
10 asignaciones de `Settings.__init__` —refactorizarlas a un bucle `setattr`
sobre el mapa está prohibido: `monkeypatch.setattr` corre con `raising=True`, un
dedazo en el dict borraría un atributo y volvería 22 pruebas un `AttributeError`
opaco, y hay 4 tests que re-instancian `Settings()`—. La duplicación la cierra
análisis estático, no metaprogramación.

Al final viven los CANDADOS INVERSOS del paso 6: solo pudieron ponerse verdes
cuando los 22 call sites ya habían migrado a `credenciales.credencial()`.
"""

import ast
from pathlib import Path

import pytest

from app.config import CREDENCIALES, Credencial, settings

RAIZ = Path(__file__).resolve().parent.parent
CONFIG_PY = (RAIZ / "app" / "config.py").read_text(encoding="utf-8")

# TODA variable de entorno de config.py que NO es una credencial por oficina,
# con su motivo escrito. Antes esta lista solo cubría cuatro prefijos
# (WHATSAPP/TWILIO/STRIPE/CREDENTIALS), así que una credencial de un proveedor
# NUEVO —un `SENDGRID_API_KEY`, un `MAPBOX_TOKEN`— entraba a config.py sin
# tocar el inventario y con la prueba inversa en verde. El barrido es ahora
# TODO nombre en MAYÚSCULAS, y el precio es este censo: agregar cualquier
# variable de entorno obliga a decidir de qué lado cae. Eso es lo que se quiere.
NO_SON_CREDENCIALES = {
    # Versión pineada de la API de Stripe: infra, no secreto (SPEC §4.2).
    "STRIPE_API_VERSION",
    # La llave MAESTRA que cifra la tabla: no puede ser fila de lo que protege.
    "CREDENTIALS_KEY",
    # §7 lo argumenta explícitamente: infra del contenedor (una URL pública por
    # servicio), no credencial; equivocarla tumba TODAS las firmas de Twilio, no
    # algunas. Entra al inventario el día que §4.3 la exija por oficina, con ese
    # motivo escrito — no "por simetría".
    "PUBLIC_BASE_URL",
    # Acceso al panel y a la sesión: son del DESPLIEGUE, no de una oficina. El
    # multi-oficina de §5 se resuelve con usuarios y permisos en la base, no
    # dándole a cada oficina su propio .env.
    "DASHBOARD_USERNAME",
    "DASHBOARD_PASSWORD",
    "REGISTRATION_CODE",
    "SESSION_SECRET",
    "SECURE_COOKIES",
    # Espejo MSSQL: una sola base de archivo para todo el despliegue (§9 lo
    # deja fuera del alcance, y officecredential ni siquiera se espeja).
    "MSSQL_HOST",
    "MSSQL_PORT",
    "MSSQL_USER",
    "MSSQL_SA_PASSWORD",
    "MSSQL_DB",
    # Modelos y RAG: un solo servicio de inferencia para el despliegue. Los
    # tokens son de ESE servicio, no de un proveedor contratado por oficina;
    # ANTHROPIC_API_KEY además está apagada en config.CLAUDE_HABILITADO.
    "ANTHROPIC_API_KEY",
    "NLU_MODEL",
    "OLLAMA_URL",
    "OLLAMA_TOKEN",
    "OLLAMA_MODEL",
    "OLLAMA_TIMEOUT",
    "EMBEDDINGS_URL",
    "EMBEDDINGS_MODEL",
    "EMBEDDINGS_TOKEN",
    "RAG_ENABLED",
    "RAG_TOP_K",
    "RAG_MIN_COSENO",
    "FUZZY_THRESHOLD",
    "AGENT_TOOLS_ENABLED",
    "AGENT_MAX_TURNOS",
    "AGENT_MAX_WORDS",
    "TOOL_COTIZAR_ENABLED",
    "TOOL_FOLIOS_ENABLED",
    # Rutas del contenedor: un proceso, un disco. §6 pone el catálogo por
    # oficina en la BASE (columna office_id), no en otro archivo semilla.
    "DB_PATH",
    "MEDIA_DIR",
    "INFO_DOCS_PATH",
    "CATALOG_PATH",
    # Reglas de negocio y topes operativos: configuración, no secretos. Si
    # alguna llega a ser por oficina, es columna de `office`, no fila de
    # `office_credential` — la tabla de §2.2 guarda valores CIFRADOS.
    "COBRO_MONTO_MAXIMO_CENTAVOS",
    "OXXO_DIAS_VENCIMIENTO",
    "CARGA_MAXIMA_ASESOR",
    "REPARTO_AUTOMATICO",
    "GEOIP_ENABLED",
}


def test_cada_entrada_del_inventario_es_atributo_real_de_settings():
    """`hasattr` E `isinstance(..., str)`: el isinstance es load-bearing, es el
    respaldo del contrato "credencial() siempre devuelve str" del que dependen
    stripe_client.modo_real() y _filtro_modo en cada render de /cobros."""
    for clave, meta in CREDENCIALES.items():
        assert hasattr(settings, meta.atributo), (
            f"{clave}: config.CREDENCIALES apunta a settings.{meta.atributo}, "
            f"que no existe"
        )
        assert isinstance(getattr(settings, meta.atributo), str), (
            f"{clave}: settings.{meta.atributo} no es str"
        )


def test_cada_clave_aparece_como_environ_get_en_config_py():
    """La CLAVE del inventario es el nombre EXACTO de la variable de entorno:
    la forma que el operador ya conoce del .env y la que se guarda en
    officecredential.clave. Si alguien renombra una, esto truena."""
    for clave in CREDENCIALES:
        literal = f'os.environ.get("{clave}"'
        assert CONFIG_PY.count(literal) == 1, (
            f"{clave} debe aparecer exactamente una vez como {literal} en config.py"
        )


def test_config_no_tiene_credenciales_fuera_del_inventario():
    """Dirección INVERSA: una 11ª credencial en config.py no puede escaparse.

    §5 exige que el panel no deje huecos — una llave que existe en el .env pero
    no en el inventario sería invisible para el panel del PR 3 y no podría
    resolverse por oficina nunca."""
    import re

    # TODO nombre en MAYÚSCULAS, no cuatro prefijos: un proveedor nuevo no se
    # llama WHATSAPP_ ni STRIPE_, y con la lista de prefijos su llave entraba a
    # config.py sin tocar el inventario y sin que esta prueba dijera nada.
    encontradas = set(re.findall(r'os\.environ\.get\(\s*"([A-Z][A-Z0-9_]*)"', CONFIG_PY))

    # Piso ABSOLUTO, con su propio mensaje: un reformateo de config.py que el
    # regex deje de casar (comillas simples, por ejemplo) daría cero
    # coincidencias, y sin este piso el fallo saldría por la rama de las
    # obsoletas culpando a la lista de exclusiones en vez de al regex.
    assert len(encontradas) >= 40, (
        f"el regex solo encontró {len(encontradas)} variables ({sorted(encontradas)}): "
        f"config.py cambió de forma y esta prueba dejó de mirar lo que dice mirar"
    )

    # El regex de arriba reconoce UNA forma. `os.getenv(...)`, `os.environ[...]`
    # y la comilla simple son la MISMA lectura escrita distinto, y con cualquiera
    # de ellas una credencial nueva entraría al sistema sin pasar por el censo:
    # la prueba seguiría verde mirando a otro lado. Es el modo de fallo que el
    # piso de arriba cubre para el reformateo y este cubre para la variante.
    # `os.getenv` es además la forma que sale sola al escribir Python, así que no
    # es un caso rebuscado.
    otras_formas = re.findall(
        r"os\.getenv\(|os\.environ\[|environ\.get\(\s*'", CONFIG_PY
    )
    assert otras_formas == [], (
        f"config.py lee el entorno de {len(otras_formas)} forma(s) que el censo no reconoce "
        f"({sorted(set(otras_formas))}). Usa os.environ.get(\"CLAVE\", ...) con comilla doble, "
        "o esa credencial entra sin que nadie la audite."
    )

    huerfanas = encontradas - set(CREDENCIALES) - NO_SON_CREDENCIALES

    assert huerfanas == set(), (
        f"variables de entorno sin entrada en config.CREDENCIALES: "
        f"{sorted(huerfanas)}. Si son credenciales por oficina, agrégalas al "
        f"inventario; si no, justifícalas en NO_SON_CREDENCIALES con su motivo."
    )

    # Dirección contraria de la MISMA lista: una exclusión que ya no existe en
    # config.py es una decisión escrita sobre una variable borrada, y esconde
    # el caso feo — que vuelva a nacer, ahora sí como credencial, y siga excluida.
    obsoletas = NO_SON_CREDENCIALES - encontradas

    assert obsoletas == set(), (
        f"NO_SON_CREDENCIALES nombra variables que config.py ya no lee: "
        f"{sorted(obsoletas)}. Bórralas de la lista en el mismo commit."
    )


def test_las_dos_irregulares_no_se_derivan_con_lower():
    """Guardia contra un refactor de «limpieza» que derive el atributo con
    clave.lower(): fallaría en 2 de 10 y devolvería "" para el secreto que
    valida la firma de Meta — un fail-closed que descarta TODO el tráfico
    entrante sin decir por qué."""
    assert CREDENCIALES["WHATSAPP_VERIFY_TOKEN"].atributo == "verify_token"
    assert CREDENCIALES["WHATSAPP_APP_SECRET"].atributo == "app_secret"

    irregulares = {
        clave for clave, meta in CREDENCIALES.items() if meta.atributo != clave.lower()
    }
    assert irregulares == {"WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET"}


def test_credentials_key_no_esta_en_el_inventario():
    """La llave maestra no puede ser fila de la tabla que cifra: resolverla por
    oficina sería pedirle a la cerradura que se abra a sí misma."""
    assert "CREDENTIALS_KEY" not in CREDENCIALES
    assert all(meta.atributo != "credentials_key" for meta in CREDENCIALES.values())
    assert isinstance(settings.credentials_key, str)


@pytest.mark.parametrize("clave", sorted(CREDENCIALES))
def test_cada_entrada_declara_un_provider_conocido(clave):
    """`provider` es columna de officecredential y entra al WHERE: un valor
    inventado no casa con ninguna fila y hereda EN SILENCIO."""
    meta = CREDENCIALES[clave]

    assert isinstance(meta, Credencial)
    assert meta.provider in {"meta", "twilio", "stripe"}
    assert isinstance(meta.es_secreto, bool)
    assert isinstance(meta.compartida, bool)


# SPEC §7, pasos 4 y 5: el documento las enumera por NOMBRE. Copiadas aquí a
# mano y a propósito — derivarlas de config.py sería preguntarle al acusado.
COMPARTIDAS_SEGUN_LA_SPEC = {
    "WHATSAPP_TOKEN",
    "WHATSAPP_APP_SECRET",
    "WHATSAPP_VERIFY_TOKEN",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_CONTENT_SID_FEEDBACK",
}

# §7 paso 5: las que SÍ son por oficina, con el `es_secreto` que el documento
# fija para cada una (las compartidas no lo declara, así que no se afirma).
POR_OFICINA_SEGUN_LA_SPEC = {
    "WHATSAPP_PHONE_NUMBER_ID": False,
    "TWILIO_WHATSAPP_FROM": False,
    "STRIPE_SECRET_KEY": True,
    "STRIPE_WEBHOOK_SECRET": True,
}


def test_el_reparto_compartida_es_el_que_enumera_la_spec():
    """«Seis compartidas + cuatro por oficina = las diez» (§7).

    Sin esto, `compartida` solo tenía `isinstance(..., bool)`: el reparto entero
    se podía INVERTIR sin que nada tronara. No es cosmético — el panel del PR 3
    renderiza «heredada del global» con este campo (§5), así que invertirlo le
    miente al operador sobre qué llave está rotando; y §7 paso 5 lo usa para
    decidir qué se siembra como fila de Tlaxcala y qué no se copia.

    `TWILIO_CONTENT_SID_FEEDBACK` es el único juicio, y la propia spec lo dice:
    compartida porque el ContentSid pertenece a la cuenta de Twilio y §0 fijó
    una sola cuenta. Revertirlo es legítimo — y son DOS líneas en el MISMO
    commit, la de `config.CREDENCIALES` y la de aquí. Mover solo una, no.
    """
    assert len(CREDENCIALES) == 10, "§7: seis compartidas + cuatro por oficina"
    assert COMPARTIDAS_SEGUN_LA_SPEC | set(POR_OFICINA_SEGUN_LA_SPEC) == set(
        CREDENCIALES
    ), "el inventario y la enumeración de §7 dejaron de hablar de las mismas llaves"

    compartidas = {clave for clave, meta in CREDENCIALES.items() if meta.compartida}
    assert compartidas == COMPARTIDAS_SEGUN_LA_SPEC, (
        f"§7 paso 4 enumera {sorted(COMPARTIDAS_SEGUN_LA_SPEC)} como compartidas; "
        f"config.CREDENCIALES dice {sorted(compartidas)}"
    )

    for clave, es_secreto in POR_OFICINA_SEGUN_LA_SPEC.items():
        assert CREDENCIALES[clave].compartida is False, (
            f"{clave} es por oficina en §7 paso 5: se siembra como fila de "
            f"Tlaxcala, no se hereda del global"
        )
        assert CREDENCIALES[clave].es_secreto is es_secreto, (
            f"{clave}: §7 paso 5 la fija con es_secreto={es_secreto}"
        )


# ---------------------------------------------------------------------------
# Candados INVERSOS (paso 6). Los de arriba miran el inventario; estos miran
# `app/` y exigen que nadie se salga de él.
# ---------------------------------------------------------------------------

# credenciales.py queda FUERA: es el módulo que DEFINE credencial(), y su
# llamada interna (`estado()` -> `credencial(clave, office_id=office_id)`) es
# paramétrica por diseño — las dos cosas que estos candados persiguen, una clave
# en variable y un office_id inline, son ahí lo correcto y no la deuda del PR 4.
# El hueco que abre esta exclusión lo tapa
# test_el_resolvedor_no_fija_ninguna_oficina_a_mano.
MODULOS_APP = sorted(
    p for p in (RAIZ / "app").glob("*.py") if p.name != "credenciales.py"
)

_ATRIBUTOS = {meta.atributo for meta in CREDENCIALES.values()}

# Los dos únicos que pueden nombrar `settings.<credencial>`: config.py es donde
# viven y credenciales.py es quien hereda. Hoy la excepción es VACUA en ambos
# —config.py escribe `self.stripe_secret_key` y credenciales.py resuelve con
# `getattr(settings, meta.atributo, "")`, que no es literal— y así debe
# quedarse: se listan porque son los dos lugares donde una lectura directa
# sería legítima, no porque hoy la haya.
PUEDEN_LEER_SETTINGS_DIRECTO = {"config.py", "credenciales.py"}


def _alias_de_settings(arbol: ast.Module) -> set[str]:
    """Nombres locales que apuntan al objeto `settings` en este archivo.

    `from app.config import settings as s` ata `settings` al nombre `s`, y
    `s.stripe_secret_key` es la MISMA lectura directa escrita de otra forma. Sin
    resolver el alias, el candado de abajo —que afirma un conjunto vacío— no
    podía demostrar lo que promete: bastaba renombrar en el import para pasar.

    Se resuelve el nombre, no el atributo: marcar cualquier `ast.Attribute` cuyo
    `.attr` esté en el inventario daría falsos positivos en cuanto el PR 3/4 le
    ponga a `office` (o a una fila de `officecredential`) un campo con el nombre
    de una credencial.
    """
    alias = {"settings"}
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom):
            alias |= {
                (n.asname or n.name) for n in nodo.names if n.name == "settings"
            }
    return alias


def _es_settings(nodo: ast.expr, alias: set[str]) -> bool:
    """`settings`/su alias, o `<algo>.settings` (`config.settings`, `app.config.settings`)."""
    if isinstance(nodo, ast.Name):
        return nodo.id in alias
    return isinstance(nodo, ast.Attribute) and nodo.attr == "settings"


def _lecturas_directas(fuente: str) -> set[str]:
    """Atributos del inventario leídos de `settings` en `fuente`.

    Cubre las DOS formas a propósito: `settings.stripe_secret_key`
    (`ast.Attribute`) y `getattr(settings, "stripe_secret_key")`. Cerrar solo la
    primera deja abierta la puerta de atrás obvia, y un `getattr` con literal se
    detecta con el mismo esfuerzo. Un `getattr` con variable (el de
    `credenciales.py`) no se puede resolver estáticamente y no se intenta.

    El alias del import vale para las DOS ramas: `getattr(s, "app_secret")` es
    tan lectura directa como `s.app_secret`.
    """
    arbol = ast.parse(fuente)
    alias = _alias_de_settings(arbol)
    encontrados: set[str] = set()
    for nodo in ast.walk(arbol):
        if (
            isinstance(nodo, ast.Attribute)
            and nodo.attr in _ATRIBUTOS
            and _es_settings(nodo.value, alias)
        ):
            encontrados.add(nodo.attr)
        elif (
            isinstance(nodo, ast.Call)
            and isinstance(nodo.func, ast.Name)
            and nodo.func.id == "getattr"
            and len(nodo.args) >= 2
            and _es_settings(nodo.args[0], alias)
            and isinstance(nodo.args[1], ast.Constant)
            and nodo.args[1].value in _ATRIBUTOS
        ):
            encontrados.add(nodo.args[1].value)
    return encontrados


def _es_credencial(func: ast.expr) -> bool:
    """`credencial(...)` o `<algo>.credencial(...)` (`credenciales.credencial`).

    La segunda forma no es hipotética: `import app.credenciales as credenciales`
    y llamar por el módulo es un estilo que este repo ya usa, y el docstring de
    `stripe_client.py` ya nombra la función así. Con solo `ast.Name` los TRES
    candados de abajo quedaban ciegos A LA VEZ —el par (archivo, clave) no
    entraba a la lista de trabajo del PR 4, la llamada de nivel de módulo no se
    veía y la clave no literal tampoco—. Mismo criterio que `_es_settings` con
    `<algo>.settings`.
    """
    if isinstance(func, ast.Name):
        return func.id == "credencial"
    return isinstance(func, ast.Attribute) and func.attr == "credencial"


def _nombres_locales(definicion: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Parámetros y nombres asignados dentro de la función, anidadas incluidas.

    Es la definición de «viene de la función» que usa `_oficina_fijada_a_mano`.
    Une lo de las funciones anidadas a propósito: una clausura que lee el
    `office_id` de su envolvente lo tiene igual de resuelto.
    """
    nombres = {n.arg for n in ast.walk(definicion) if isinstance(n, ast.arg)}
    nombres |= {
        n.id
        for n in ast.walk(definicion)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    }
    return nombres


def _oficina_fijada_a_mano(valor: ast.expr, locales: set[str]) -> bool:
    """¿Este `office_id=` es una oficina escrita a mano en el call site?

    Lo es todo lo que NO sale de un parámetro ni de una variable de la función:
    `OFICINA_TLAXCALA_ID`, `models.OFICINA_TLAXCALA_ID` y el literal `1`. Mirar
    solo el nombre `OFICINA_TLAXCALA_ID` dejaba a los otros dos FUERA de la
    lista de trabajo del PR 4 con la prueba en verde — y `office_id=1` es justo
    lo que alguien escribe cuando la constante no está importada.
    """
    # ponytail: `oficina = OFICINA_TLAXCALA_ID` y luego `office_id=oficina` sí
    # se lava — el nombre es local y aquí no se sigue la asignación. El techo es
    # deliberado: seguirla pide análisis de flujo, y hoy los 15 puntos pasan la
    # constante directo. Si aparece esa forma, la salida es resolver una
    # asignación constante de un solo nivel, NO ampliar esto a «cualquier Name»
    # (marcaría `office_id=office_id` y volvería inútil la lista de trabajo).
    if isinstance(valor, ast.Constant):
        return True
    if isinstance(valor, ast.Name):
        return valor.id not in locales
    if isinstance(valor, ast.Attribute):
        raiz: ast.expr = valor
        while isinstance(raiz, ast.Attribute):
            raiz = raiz.value
        # `folio.office_id` sale de una variable; `models.OFICINA_TLAXCALA_ID` no.
        return not (isinstance(raiz, ast.Name) and raiz.id in locales)
    # Una llamada o un subscript se resuelven en tiempo de ejecución: no es a mano.
    return False


def _llamadas_a_credencial(fuente: str) -> list[tuple[str | None, bool, bool]]:
    """Por cada `credencial(...)`: (clave literal, oficina a mano, nivel módulo).

    `nivel de módulo` se decide por pertenencia al CUERPO de alguna función: un
    decorador o un default de argumento de una función de nivel superior corre
    al importar, y así queda clasificado como lo que es.
    """
    arbol = ast.parse(fuente)
    # id(Call) -> nombres locales de las funciones que la envuelven. Las CLAVES
    # de este mapa son, exactamente, las llamadas que no son de nivel de módulo:
    # se recorre `definicion.body`, no `ast.walk(definicion)`, para no tragarse
    # decoradores ni defaults de argumento, que corren al importar.
    locales: dict[int, set[str]] = {}
    for definicion in ast.walk(arbol):
        if not isinstance(definicion, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nombres = _nombres_locales(definicion)
        for sentencia in definicion.body:
            for hijo in ast.walk(sentencia):
                if isinstance(hijo, ast.Call):
                    locales.setdefault(id(hijo), set()).update(nombres)

    salida: list[tuple[str | None, bool, bool]] = []
    for nodo in ast.walk(arbol):
        if not (isinstance(nodo, ast.Call) and _es_credencial(nodo.func)):
            continue
        primer = nodo.args[0] if nodo.args else None
        clave = (
            primer.value
            if isinstance(primer, ast.Constant) and isinstance(primer.value, str)
            else None
        )
        oficina_a_mano = any(
            kw.arg == "office_id"
            and _oficina_fijada_a_mano(kw.value, locales.get(id(nodo), set()))
            for kw in nodo.keywords
        )
        salida.append((clave, oficina_a_mano, id(nodo) not in locales))
    return salida


def test_el_detector_de_lecturas_directas_si_ve_lo_que_dice_ver():
    """Control positivo. La prueba de abajo afirma un conjunto VACÍO, así que un
    detector roto —un `ast.walk` que deja de casar, un rename de `settings`— la
    dejaría verde PARA SIEMPRE mientras un módulo vuelve a `settings` por la
    puerta de atrás. Mismo motivo que el piso de
    `test_config_no_tiene_credenciales_fuera_del_inventario`."""
    muestra = (
        "from app.config import settings\n"
        "def f():\n"
        "    a = settings.stripe_secret_key\n"
        '    b = getattr(settings, "app_secret")\n'
        "    c = config.settings.whatsapp_token\n"
        "    return a, b, c\n"
    )

    assert _lecturas_directas(muestra) == {
        "stripe_secret_key",
        "app_secret",
        "whatsapp_token",
    }

    # Y el alias del import, que es la puerta de atrás barata: renombrar
    # `settings` en el `from ... import` bastaba para pasar el candado.
    con_alias = (
        "from app.config import settings as s\n"
        "def f():\n"
        "    return s.twilio_auth_token, getattr(s, 'verify_token')\n"
    )

    assert _lecturas_directas(con_alias) == {"twilio_auth_token", "verify_token"}


def test_el_colector_de_llamadas_si_ve_lo_que_dice_ver():
    """Control positivo DIRECTO de `_llamadas_a_credencial`.

    Hasta ahora solo se comprobaba de rebote, por el conjunto no vacío de
    `PUNTOS_CON_OFICINA_INLINE`: un colector que no viera una forma NUEVA no
    aparecía como error en ninguna de las tres pruebas que lo usan —simplemente
    esa llamada no existía para ellas—. Estas cuatro formas son las que se le
    escapaban."""
    muestra = (
        "import app.credenciales as credenciales\n"
        "from app import models\n"
        "from app.credenciales import credencial\n"
        "from app.models import OFICINA_TLAXCALA_ID\n"
        "\n"
        "def por_el_modulo():\n"
        "    return credenciales.credencial('WHATSAPP_TOKEN',"
        " office_id=OFICINA_TLAXCALA_ID)\n"
        "\n"
        "def literal():\n"
        "    return credencial('STRIPE_SECRET_KEY', office_id=1)\n"
        "\n"
        "def por_atributo():\n"
        "    return credencial('TWILIO_ACCOUNT_SID',"
        " office_id=models.OFICINA_TLAXCALA_ID)\n"
        "\n"
        "def resuelta(office_id: int):\n"
        "    return credencial('TWILIO_AUTH_TOKEN', office_id=office_id)\n"
        "\n"
        "def desde_una_fila(folio):\n"
        "    return credencial('STRIPE_WEBHOOK_SECRET', office_id=folio.office_id)\n"
        "\n"
        "AL_IMPORTAR = credenciales.credencial('WHATSAPP_APP_SECRET', office_id=1)\n"
    )

    # (clave, oficina a mano, nivel de módulo)
    assert sorted(_llamadas_a_credencial(muestra)) == sorted(
        [
            ("WHATSAPP_TOKEN", True, False),  # credenciales.credencial(...)
            ("STRIPE_SECRET_KEY", True, False),  # office_id=1
            ("TWILIO_ACCOUNT_SID", True, False),  # models.OFICINA_TLAXCALA_ID
            ("TWILIO_AUTH_TOKEN", False, False),  # parámetro: ya resuelta
            ("STRIPE_WEBHOOK_SECRET", False, False),  # folio.office_id: resuelta
            ("WHATSAPP_APP_SECRET", True, True),  # y además al importar
        ]
    )


def test_ningun_modulo_lee_una_credencial_de_settings_directo():
    """La razón de ser del PR 2: `credencial()` es la ÚNICA puerta.

    Un punto nuevo que lea `settings.whatsapp_token` funcionaría hoy —hereda lo
    mismo con la tabla vacía— y quedaría invisible para el panel del PR 3 y para
    el ruteo por oficina del PR 4/5: ni el panel podría rotarlo ni la oficina 2
    tendría cómo tener el suyo. Se descubriría en producción, con la llave
    equivocada."""
    assert len(MODULOS_APP) >= 40, (
        f"solo se parsearon {len(MODULOS_APP)} módulos de app/: el glob dejó de "
        f"mirar lo que dice mirar y esta prueba pasaría en vacío"
    )

    culpables = {
        ruta.name: sorted(hallado)
        for ruta in MODULOS_APP
        if ruta.name not in PUEDEN_LEER_SETTINGS_DIRECTO
        and (hallado := _lecturas_directas(ruta.read_text(encoding="utf-8")))
    }

    assert culpables == {}, (
        f"leen una credencial de settings sin pasar por credencial(): {culpables}. "
        f"Usa credenciales.credencial(<CLAVE>, office_id=...) — si no, el panel "
        f"del PR 3 no puede rotarla y el ruteo del PR 4 no puede resolverla."
    )


# El conjunto (archivo, clave) de los puntos que hoy fijan la oficina a mano.
# ES la lista de trabajo del PR 4 y del PR 5, y el diff de esta lista es el
# artefacto de auditoría de ambos. 15 pares para 22 llamadas: tres funciones
# leen la misma llave más de una vez (whatsapp_client._send/send_media,
# twilio_client.enabled/verify_signature/_post_message, stripe_client).
PUNTOS_CON_OFICINA_INLINE = {
    ("cobranza.py", "STRIPE_SECRET_KEY"),
    ("cobranza_panel.py", "STRIPE_SECRET_KEY"),
    ("dashboard.py", "STRIPE_SECRET_KEY"),
    ("dashboard.py", "TWILIO_CONTENT_SID_FEEDBACK"),
    ("stripe_client.py", "STRIPE_SECRET_KEY"),
    # ("stripe_webhook.py", "STRIPE_WEBHOOK_SECRET") — RESUELTO en el PR 5: la
    # oficina sale del slug de /webhooks/stripe/{slug} (§4.3) y la ruta legada le
    # pasa OFICINA_TLAXCALA_ID a _procesar, no a credencial(). Es el primer punto
    # de esta lista que se salda; el diff de este conjunto es el artefacto de
    # auditoría que dice cuánto falta.
    ("system.py", "WHATSAPP_PHONE_NUMBER_ID"),
    ("system.py", "WHATSAPP_TOKEN"),
    ("twilio_client.py", "TWILIO_ACCOUNT_SID"),
    ("twilio_client.py", "TWILIO_AUTH_TOKEN"),
    ("twilio_client.py", "TWILIO_WHATSAPP_FROM"),
    ("webhook.py", "WHATSAPP_APP_SECRET"),
    ("webhook.py", "WHATSAPP_VERIFY_TOKEN"),
    ("whatsapp_client.py", "WHATSAPP_PHONE_NUMBER_ID"),
    ("whatsapp_client.py", "WHATSAPP_TOKEN"),
}


def test_los_puntos_con_office_id_inline_son_exactamente_estos():
    """Correspondencia, NO un conteo: un número se sube sin pensar. Un par que
    aparece o desaparece obliga a mirar QUÉ archivo y QUÉ llave cambiaron.

    Es también el control positivo de `_llamadas_a_credencial`: compara contra
    un conjunto no vacío, así que un colector roto trona aquí y no en silencio
    en `test_ninguna_llamada_a_credencial_a_nivel_de_modulo`."""
    encontrados: set[tuple[str, str]] = set()
    sin_clave_literal: list[str] = []

    for ruta in MODULOS_APP:
        for clave, oficina_inline, _ in _llamadas_a_credencial(
            ruta.read_text(encoding="utf-8")
        ):
            if clave is None:
                sin_clave_literal.append(ruta.name)
            elif oficina_inline:
                encontrados.add((ruta.name, clave))

    assert sin_clave_literal == [], (
        f"credencial() con la clave en una variable en {sorted(set(sin_clave_literal))}: "
        f"la clave debe ser literal o esta auditoría deja de ser exhaustiva"
    )

    faltan = PUNTOS_CON_OFICINA_INLINE - encontrados
    sobran = encontrados - PUNTOS_CON_OFICINA_INLINE

    assert (faltan, sobran) == (set(), set()), (
        f"faltan (ya no fijan Tlaxcala a mano): {sorted(faltan)} · "
        f"sobran (nuevos puntos que el PR 4/5 tendrá que resolver): {sorted(sobran)}. "
        f"Actualiza PUNTOS_CON_OFICINA_INLINE en el MISMO commit que mueve el punto."
    )


def test_ninguna_llamada_a_credencial_a_nivel_de_modulo():
    """Un `TOKEN = credencial(...)` de nivel de módulo se resuelve UNA vez, al
    importar: la llave que el admin edite en el panel del PR 3 quedaría
    invisible hasta reiniciar el contenedor, y el «no caché» del resolvedor se
    volvería mentira sin que ninguna prueba de `credenciales.py` se enterara.

    Hoy es cierto por accidente —los 22 puntos migraron dentro de funciones—;
    esto lo vuelve contrato."""
    a_nivel_de_modulo = sorted(
        ruta.name
        for ruta in MODULOS_APP
        for _, _, nivel_modulo in _llamadas_a_credencial(
            ruta.read_text(encoding="utf-8")
        )
        if nivel_modulo
    )

    assert a_nivel_de_modulo == [], (
        f"credencial() llamada al importar en {a_nivel_de_modulo}: muévela dentro "
        f"de la función que la usa, o el panel del PR 3 editará llaves que nadie lee."
    )


def test_el_resolvedor_no_fija_ninguna_oficina_a_mano():
    """Compensa la exclusión de credenciales.py de MODULOS_APP.

    Ahí una clave en variable es correcta (el módulo resuelve CUALQUIER clave),
    pero un `office_id=1` literal no lo sería nunca: significaría que el
    resolvedor —o el panel que escribe a través de él— decidió por su cuenta una
    oficina en vez de recibirla. Es el mismo fail-closed de §3 aplicado a quien
    implementa la herencia.
    """
    arbol = ast.parse((RAIZ / "app" / "credenciales.py").read_text(encoding="utf-8"))
    inline = [
        f"línea {nodo.lineno}"
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.keyword)
        and nodo.arg == "office_id"
        and isinstance(nodo.value, ast.Constant)
    ]
    assert inline == [], (
        f"credenciales.py fija una oficina a mano en {inline}: el resolvedor la "
        f"recibe, nunca la elige"
    )
