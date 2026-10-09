"""Set dorado de «casos para análisis» (diseño v3.2 §8.1), como datos.

Cada caso es UN mensaje de texto con su contexto —los folios abiertos del cliente y
el estado de la conversación— y dos etiquetas que no tienen por qué coincidir:

- `intencion`: lo que una persona lee en el mensaje.
- `accion`: lo que el bot debe hacer con él. Un texto informativo en `confirmar` no
  pregunta por el folio, porque ese estado espera un sí o un no (§5.1).

Quién puso cada etiqueta lo dice `nota`: «etiqueta N» son las 7 del operador
(2026-10-09); «deducida» es una etiqueta que §8.1 no fija y sale de las reglas del
diseño. Lo que pasa en varios mensajes (texto y archivo, un «ok» en medio, las
respuestas a la pregunta, siete folios en la lista, archivos sueltos) son las pruebas
de extremo a extremo de §8.3, no filas de aquí.

Las claves son sintéticas: las CURP del diseño y NSS inventados. El único texto que
viene de un chat real es el del 2026-09-24, recortado y sin el nombre del cliente.
"""

from dataclasses import dataclass
from enum import Enum


class Intencion(str, Enum):
    RESPUESTA_CORTA = "respuesta_corta"
    INFORMATIVO = "informativo"
    REVISION = "revision"
    NORMAL = "normal"


class Hace(str, Enum):
    """Qué hace el bot con el mensaje; cómo se verifica, en test_set_dorado_casos.py."""

    PREGUNTA_FOLIO = "pregunta_folio"  # «¿es para tu folio #N?»: `aclarando_folio` (§5.1)
    REVISION = "revision"  # «Revisión» + los servicios nombrados, o botones sin ellos (§5.2)
    CAMINO_DE_HOY = "camino_de_hoy"  # ni pregunta de folio ni revisión: el flujo de hoy
    ESTATUS = "estatus"  # el estatus de hoy, sin cambiar de estado
    SILENCIO = "silencio"  # un colaborador apagó el agente en un folio: no contesta


@dataclass(frozen=True)
class Folio:
    """Un folio abierto del cliente antes del mensaje."""

    servicio: str
    escalado: bool = False
    agente_activo: bool = True


@dataclass(frozen=True)
class Accion:
    hace: Hace
    # PREGUNTA_FOLIO: no aplica (se pregunta por todos los folios abiertos).
    # REVISION: las filas de servicio de la lista, en orden; vacío = botones.
    # CAMINO_DE_HOY: los servicios que el bot toma (por confirmar o ya anotados).
    servicios: tuple[str, ...] = ()
    # Estado final exacto, solo donde el diseño lo fija o donde el flujo de hoy no
    # depende de las sugerencias difusas del NLU.
    estado: str | None = None


@dataclass(frozen=True)
class Caso:
    id: str
    texto: str
    folios: tuple[Folio, ...]
    estado: str
    intencion: Intencion
    accion: Accion
    nota: str
    # Lo que espera confirmación cuando `estado` es "confirmar".
    pendientes: tuple[str, ...] = ()


# El caso del diseño §1 con claves y nombre sintéticos: las CURP son las del diseño y
# los NSS están inventados (11 dígitos con espacios, como los escribió el cliente). El
# original tenía 929 caracteres; este conserva lo que lee la capa literal.
CASO_2026_09_24 = (
    "Se solicita el apoyo para el C. <NOMBRE DEL TRABAJADOR>. El trabajador presenta la "
    "siguiente situación: CURP cancelada: AAAA000101HDFBBB01 asociada al NSS 12 34567890 1, "
    "el cual cuenta con 694 semanas cotizadas. CURP oficial vigente: AAAA000101HDFBBB09 "
    "asociada al NSS 98 76543210 0, el cual cuenta con 0 semanas cotizadas. El trabajador ya "
    "realizó la corrección ante RENAPO...\n"
    "Por lo anterior se solicita:\n"
    "1. La unificación de ambos Números de Seguridad Social, dejando como NSS único el más "
    "antiguo...\n"
    "2. Que dicho NSS quede vinculado a la CURP oficial y vigente...\n"
    "3. La cancelación definitiva en sistema del NSS ... con 0 semanas."
)

# El mismo caso sin ninguna frase de revisión de §5.2: lo detecta D9, tres servicios
# nombrados en un texto largo.
CASO_2026_09_24_SIN_FRASES = (
    "Escribo por el C. <NOMBRE DEL TRABAJADOR>. El trabajador tiene dos registros: CURP "
    "anterior: AAAA000101HDFBBB01 asociada al NSS 12 34567890 1, el cual cuenta con 694 "
    "semanas cotizadas. CURP oficial vigente: AAAA000101HDFBBB09 asociada al NSS 98 76543210 "
    "0, el cual cuenta con 0 semanas cotizadas. El trabajador ya corrigió sus datos en el "
    "registro...\n"
    "Por lo anterior pedimos:\n"
    "1. La unificación de los dos Números de Seguridad Social, dejando como NSS único el más "
    "antiguo...\n"
    "2. Que dicho NSS quede vinculado a la CURP oficial y vigente...\n"
    "3. La cancelación definitiva en sistema del NSS ... con 0 semanas."
)

# Más de 80 palabras sin frases de revisión ni servicios nombrados.
NEUTRO_80 = (
    "Buenas tardes, le comparto lo que pasó la semana pasada. Fui a la oficina del centro con "
    "mi hermana porque nos dijeron que ahí nos podían ayudar con los papeles de mi mamá. "
    "Llegamos temprano, hicimos fila casi dos horas y cuando por fin nos atendieron la "
    "señorita nos pidió un papel que no traíamos. Nos dijo que regresáramos otro día con todo "
    "completo y con copias. Mi mamá ya está grande y le cuesta salir, por eso quiero dejar "
    "todo listo desde aquí."
)

# Los pares de D12 (`contexto_min_palabras` = 40): el mismo texto con 40 y con 39
# palabras, contadas con `_norm(texto).split()` como en el código. Una palabra no
# cambia la intención; la acción sí, y es el salto que el diseño acepta.
NEUTRO_40 = (
    "Le cuento que ayer fui a la oficina con mi hermana porque nos dijeron que ahí nos "
    "podían ayudar con los papeles de mi mamá, pero la señorita nos pidió otro papel que no "
    "traíamos y nos regresamos sin nada."
)
NEUTRO_39 = NEUTRO_40.replace("que ayer fui", "que fui")
ACTA_40 = (
    "me revisas mi acta de nacimiento por favor. En la oficina me dijeron que mi segundo "
    "apellido aparece con una letra distinta a la de mis papeles de la escuela y no sé si así "
    "me la van a aceptar."
)
ACTA_39 = ACTA_40.replace("mi segundo apellido", "mi apellido")

# Un pedido legítimo de dos servicios en 45 palabras y sin frases: D9 lo manda a la
# lista, y el cliente elige uno y vuelve a pedir el otro (coste aceptado de D9 y D18).
MULTI_45 = (
    "Buenos días, quiero sacar mi acta de nacimiento y también la descarga de mi CURP porque "
    "me las piden para entrar a mi empleo nuevo la próxima semana; si me pueden decir cuánto "
    "cuesta cada una y cuándo me las entregan, se los agradezco mucho."
)

YA_PAGUE = (
    "Ya pagué, ¿cómo va mi folio? Hice la transferencia ayer por la tarde desde la aplicación "
    "del banco y me salió el comprobante, pero nadie me ha contestado y necesito tenerlo listo "
    "antes del viernes porque me lo piden en la oficina de mi esposo."
)

# El folio del caso real: «Unificación de NSS», el #63.
CON_FOLIO = (Folio("imss-unificacion-nss"),)
TRES_DEL_CASO = ("sb-curp", "con-semanas-cotizadas", "con-nss-pdf")

PREGUNTA = Accion(Hace.PREGUNTA_FOLIO)
ESTATUS = Accion(Hace.ESTATUS)
SILENCIO = Accion(Hace.SILENCIO)


def camino(*servicios: str, estado: str | None = None) -> Accion:
    return Accion(Hace.CAMINO_DE_HOY, servicios, estado)


def revision(*servicios: str) -> Accion:
    return Accion(Hace.REVISION, servicios)


CASOS: tuple[Caso, ...] = (
    # --- Reales de producción --------------------------------------------------------
    Caso(
        "real-24-09-con-folio", CASO_2026_09_24, CON_FOLIO, "menu_categoria",
        Intencion.INFORMATIVO, PREGUNTA,
        "§1: la información del #63, que el bot troceó en tres trámites",
    ),
    Caso(
        "real-24-09-sin-folio", CASO_2026_09_24, (), "inicio",
        Intencion.REVISION, revision(*TRES_DEL_CASO),
        "§8.1: sin folio, la lista con los tres servicios",
    ),
    Caso(
        "vinculacion-nss-curp", "Tambien ocuparé una vinculación de nss y curp", (), "inicio",
        Intencion.NORMAL, camino("imss-vinculacion-nss-curp", estado="confirmar"),
        "etiqueta 5: un servicio (PR9); hoy «¿Confirmas estos 3?»",
    ),
    Caso(
        "acta-y-rfc", "hola quiero un acta de nacimiento y un rfc", (), "inicio",
        Intencion.NORMAL, camino("sb-acta-nacimiento", "sat-csf-original", estado="confirmar"),
        "§8.1: dos pedidos cortos, «¿Confirmas estos 2?»",
    ),
    Caso(
        "nss-y-clave-curp", "nss y clave curp", (), "inicio",
        Intencion.NORMAL, camino("sb-curp", "con-nss-pdf", estado="confirmar"),
        "§8.1: sin folio",
    ),
    Caso(
        "ya-pague-largo-en-escalado", YA_PAGUE,
        (Folio("imss-unificacion-nss", escalado=True),), "escalado",
        Intencion.INFORMATIVO, ESTATUS,
        "§8.1: el estatus gana a la pregunta (§5.4); intención deducida (≥ 40 palabras)",
    ),
    # --- Sintéticos con un folio abierto ---------------------------------------------
    Caso(
        "si-con-folio", "sí", CON_FOLIO, "menu_categoria",
        Intencion.RESPUESTA_CORTA, camino(estado="menu_categoria"), "§8.1",
    ),
    Caso(
        "numero-con-folio", "2", CON_FOLIO, "menu_categoria",
        Intencion.RESPUESTA_CORTA, camino(estado="menu_servicio"), "§8.1: elige la categoría 2",
    ),
    Caso(
        "omitir-con-folio", "omitir", CON_FOLIO, "menu_categoria",
        Intencion.RESPUESTA_CORTA, camino(estado="menu_categoria"), "§8.1",
    ),
    Caso(
        "pulgar-con-folio", "👍", CON_FOLIO, "menu_categoria",
        Intencion.RESPUESTA_CORTA, camino(estado="menu_categoria"), "§8.1",
    ),
    Caso(
        "clave-curp-sola", "mi CURP es AAAA000101HDFBBB01", CON_FOLIO, "menu_categoria",
        Intencion.INFORMATIVO, camino("sb-curp", estado="confirmar"),
        "§8.1: una clave sola no es señal",
    ),
    Caso(
        "clave-nss-sola", "mi NSS es 12345678901", CON_FOLIO, "menu_categoria",
        Intencion.INFORMATIVO, camino("con-nss-pdf", estado="confirmar"),
        "§8.1: una clave sola no es señal",
    ),
    Caso(
        "si-con-curp-en-confirmar", "sí, mi curp es AAAA000101HDFBBB01", CON_FOLIO, "confirmar",
        Intencion.INFORMATIVO, camino("sb-curp", estado="confirmar"),
        "§8.1: re-pregunta como hoy; intención deducida (la de «mi CURP es…»)",
        pendientes=("sb-curp",),
    ),
    Caso(
        "ochenta-en-confirmar", NEUTRO_80, CON_FOLIO, "confirmar",
        Intencion.INFORMATIVO, camino("sb-acta-nacimiento", estado="confirmar"),
        "§8.1: re-pregunta; intención deducida (≥ 40 palabras)",
        pendientes=("sb-acta-nacimiento",),
    ),
    Caso(
        "situacion-del-trabajador",
        "Buenas tardes, le escribo porque el trabajador presenta la siguiente situación con su "
        "registro y quisiera saber cómo seguir.",
        CON_FOLIO, "menu_categoria", Intencion.INFORMATIVO, PREGUNTA,
        "§8.1: una frase de revisión con folio abierto",
    ),
    Caso(
        "ya-quedo-lo-de-curp", "Ya quedó lo de CURP; también revisé semanas cotizadas",
        CON_FOLIO, "menu_categoria", Intencion.INFORMATIVO, ESTATUS,
        "deducida: «ya quedó» pide estatus y el estatus va antes que la regla (§5.4)",
    ),
    Caso(
        "lo-de-curp-ya-lo-tengo", "Lo de la CURP ya lo tengo; también revisé semanas cotizadas",
        CON_FOLIO, "menu_categoria", Intencion.INFORMATIVO, PREGUNTA,
        "D17: dos servicios en un texto corto, sin palabras de estatus",
    ),
    Caso(
        "acta-y-curp-con-folio", "quiero acta y curp", CON_FOLIO, "menu_categoria",
        Intencion.INFORMATIVO, PREGUNTA, "etiqueta 4: D17 pregunta por el folio",
    ),
    Caso(
        "ochenta-con-folio", NEUTRO_80, CON_FOLIO, "menu_categoria",
        Intencion.INFORMATIVO, PREGUNTA, "§8.1: 80 palabras sin frases ni servicios",
    ),
    Caso(
        "ochenta-con-agente-apagado", NEUTRO_80,
        (Folio("imss-unificacion-nss", agente_activo=False),), "menu_categoria",
        Intencion.INFORMATIVO, SILENCIO,
        "§8.1: silencio, como hoy; intención deducida (≥ 40 palabras)",
    ),
    Caso(
        "cuarenta-con-folio", NEUTRO_40, CON_FOLIO, "menu_categoria",
        Intencion.INFORMATIVO, PREGUNTA, "par de D12: con 40 palabras pregunta",
    ),
    Caso(
        "treinta-y-nueve-con-folio", NEUTRO_39, CON_FOLIO, "menu_categoria",
        Intencion.INFORMATIVO, camino(estado="menu_categoria"),
        "par de D12: con 39 palabras sigue el camino de hoy",
    ),
    # --- Sintéticos sin folio --------------------------------------------------------
    Caso(
        "tres-actas-con-curps",
        "quiero 3 actas, AAAA000101HDFBBB01, AAAA000101HDFBBB02, AAAA000101HDFBBB03",
        (), "inicio", Intencion.NORMAL, camino("sb-acta-nacimiento", estado="confirmar"),
        "§8.1: tres unidades de un servicio",
    ),
    Caso(
        "situacion-con-mi-pension", "tengo una situación con mi pensión, qué se puede hacer",
        (), "inicio", Intencion.REVISION, revision(),
        "§8.1: ningún servicio nombrado, botones",
    ),
    Caso(
        "resolucion-de-pension", "resolución de pensión, ya tengo todos mis documentos",
        (), "inicio", Intencion.NORMAL, camino("pen-resolucion", estado="confirmar"),
        "§8.1: «resolucion» sola no es frase",
    ),
    Caso(
        "apoyo-para-descargar-curp", "necesito apoyo para descargar mi CURP", (), "inicio",
        Intencion.NORMAL, camino("sb-curp"), "§8.1: un servicio, corto",
    ),
    Caso(
        "me-apoyas-con-la-curp", "me apoyas para bajar mi curp?", (), "inicio",
        Intencion.NORMAL, camino("sb-curp"), "§8.1",
    ),
    Caso(
        "revisen-mi-curp-cancelada", "necesito que revisen mi CURP cancelada", (), "inicio",
        Intencion.NORMAL, camino("sb-curp", estado="confirmar"),
        "etiqueta 1: un servicio, corto, dos frases",
    ),
    Caso(
        "me-revisas-mi-acta-40", ACTA_40, (), "inicio",
        Intencion.REVISION, revision("sb-acta-nacimiento"),
        "etiqueta 2: lista con «Revisión» primera y el acta segunda; par de D12",
    ),
    Caso(
        "me-revisas-mi-acta-39", ACTA_39, (), "inicio",
        Intencion.REVISION, camino("sb-acta-nacimiento", estado="confirmar"),
        "par de D12: con 39 palabras un servicio sigue el camino de hoy",
    ),
    Caso(
        "me-orientas-licencia", "me orientas para sacar mi licencia de conducir", (), "inicio",
        Intencion.REVISION, revision(), "etiqueta 3: ningún servicio nombrado, botones",
    ),
    Caso(
        "acta-con-duplicidad", "necesito mi acta; el registro tiene duplicidad", (), "inicio",
        Intencion.NORMAL, camino("sb-acta-nacimiento"), "etiqueta 6: un servicio, corto",
    ),
    Caso(
        "hola-situacion-acta-urgente",
        "hola, tengo una situación: necesito un acta de nacimiento urgente", (), "inicio",
        Intencion.NORMAL, camino("sb-acta-nacimiento"), "etiqueta 7: un servicio, corto",
    ),
    Caso(
        "dos-curp-y-dos-actas",
        "quisiera dos curp y dos actas, una para mí y otra para mi hermana", (), "inicio",
        Intencion.NORMAL, camino("sb-curp"), "§8.1: «dos curp» no es frase",
    ),
    Caso(
        "duplicado-de-acta", "necesito un duplicado de mi acta de nacimiento", (), "inicio",
        Intencion.NORMAL, camino("sb-acta-nacimiento", estado="confirmar"),
        "§8.1: «duplicado» no es frase",
    ),
    Caso(
        "acta-y-curp-sin-folio", "quiero acta y curp", (), "inicio",
        Intencion.NORMAL, camino("sb-acta-nacimiento", "sb-curp", estado="confirmar"), "§8.1",
    ),
    Caso(
        "corregi-ante-renapo", "ya corregí ante RENAPO, tengo dos números", (), "inicio",
        Intencion.REVISION, revision(), "§8.1: «ante renapo»",
    ),
    Caso(
        "oficina-rechazo-expediente",
        "la oficina rechazó mi expediente y necesito entender la causa", (), "inicio",
        Intencion.REVISION, camino(estado="menu_categoria"),
        "§8.1: sin frase; falso negativo aceptado (§10)",
    ),
    Caso(
        "real-24-09-sin-frases", CASO_2026_09_24_SIN_FRASES, (), "inicio",
        Intencion.REVISION, revision(*TRES_DEL_CASO),
        "§8.1: D9, tres servicios en un texto largo",
    ),
    Caso(
        "dos-servicios-en-45-palabras", MULTI_45, (), "inicio",
        Intencion.NORMAL, revision("sb-acta-nacimiento", "sb-curp"),
        "§8.1: D9 manda a la lista un pedido legítimo (coste de D9 y D18)",
    ),
    Caso(
        "se-solicita-apoyo-ante-renapo",
        "Se solicita apoyo para corregir un registro; ya hice el trámite ante RENAPO",
        (), "inicio", Intencion.REVISION, revision(),
        "§8.1: «ante renapo»; «se solicita apoyo» no es la frase",
    ),
)
