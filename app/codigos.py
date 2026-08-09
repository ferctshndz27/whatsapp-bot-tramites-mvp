"""Códigos que el cliente pega en el chat y a qué requisito del trámite van.

Fuente única de los patrones: `conversation` los usa para las dos cosas que
hace con un código —anexarlo a la ficha del cliente y prellenar los requisitos
del folio— y no define ninguno por su cuenta. (`rag._CURP_RE` sigue aparte a
propósito: ahí el patrón es MÁS laxo porque su trabajo es bloquear fugas en la
respuesta del bot, no clasificar lo que escribió el cliente.)

La regla de oro del proyecto se respeta: esto NO aprende nada ni decide nada
del trámite; solo reconoce formatos y deja el dato escrito en el folio para
que el asesor lo vea.
"""

from __future__ import annotations

import re

from app import nlu

# Formatos oficiales. Se aplican sobre el texto en mayúsculas.
CURP_RE = re.compile(r"\b[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b")
RFC_RE = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b")
NSS_RE = re.compile(r"\b\d{11}\b")
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# Cadena con FORMA de clave oficial que no cuadra con ningún patrón exacto: una
# CURP mal tecleada, un folio institucional, una clave copiada de otro
# documento. No se clasifica —se guarda como "codigo"— y solo se usa cuando el
# requisito pide una clave y no llegó ninguna del tipo exacto. Exige 3 letras y
# 3 dígitos para no tragarse palabras largas ni números de teléfono.
CODIGO_RE = re.compile(
    r"\b(?=(?:[A-Z0-9]*[A-Z]){3})(?=(?:[A-Z0-9]*\d){3})[A-Z0-9]{10,20}\b"
)

# Tipos que son una clave alfanumérica: los únicos que admiten el respaldo
# "codigo" y los únicos donde vale la pena repreguntar si la respuesta no trae
# nada con esa forma.
TIPOS_CLAVE = ("CURP", "RFC", "NSS")
SUELTO = "codigo"
# Tope de códigos por tipo y por mensaje. Es a la vez el tope de trámites que
# se cotizan de un jalón: nadie pide 40 actas por WhatsApp, y un pegote enorme
# no debe acabar dentro del folio ni multiplicado en una cotización.
MAX_CODIGOS = 10

# Cómo se nombra cada dato en el texto libre de los requisitos del catálogo
# ("clave curp", "Nss y curp", "CORREO"). Palabra completa, sobre el texto
# normalizado por nlu._norm (minúsculas y sin acentos).
_PALABRAS_TIPO: dict[str, tuple[str, ...]] = {
    "CURP": ("curp",),
    "RFC": ("rfc",),
    "NSS": ("nss", "numero de seguridad social", "numero de seguro social"),
    "Email": ("correo", "email", "e mail"),
}


def detectar(text: str) -> dict[str, list[str]]:
    """Códigos del mensaje agrupados por tipo, sin repetidos y en orden de
    aparición. La clave "codigo" son las cadenas con forma de clave que no
    cuadran con ningún formato conocido."""
    upper = text.upper()
    encontrados: dict[str, list[str]] = {}

    def _agregar(tipo: str, valores: list[str]) -> None:
        actuales = encontrados.setdefault(tipo, [])
        for valor in valores:
            if valor not in actuales:
                actuales.append(valor)

    curps = CURP_RE.findall(upper)
    _agregar("CURP", curps)
    # El prefijo de una CURP tiene forma de RFC, pero no lo es.
    _agregar("RFC", [r for r in RFC_RE.findall(upper) if not any(r in c for c in curps)])
    _agregar("NSS", NSS_RE.findall(upper))
    _agregar("Email", [e.lower() for e in EMAIL_RE.findall(text)])

    exactos = {v.upper() for valores in encontrados.values() for v in valores}
    _agregar(SUELTO, [c for c in CODIGO_RE.findall(upper) if c not in exactos])
    return {
        tipo: valores[:MAX_CODIGOS]
        for tipo, valores in encontrados.items()
        if valores
    }


def aplicar_clasificacion(detectados: dict, veredicto: dict[str, str]) -> dict:
    """Mueve los códigos sin clasificar al tipo que les asignó el modelo local.

    `veredicto` es {código: tipo}; lo que no venga en él, o venga con un tipo
    desconocido, se queda como SUELTO — que es el comportamiento de siempre.
    Devuelve un dict NUEVO."""
    sueltos = detectados.get(SUELTO) or []
    if not sueltos or not veredicto:
        return dict(detectados)
    resultado = {tipo: list(v) for tipo, v in detectados.items() if tipo != SUELTO}
    quedan = []
    for codigo in sueltos:
        tipo = veredicto.get(codigo)
        if tipo in TIPOS_CLAVE:
            valores = resultado.setdefault(tipo, [])
            if codigo not in valores:
                valores.append(codigo)
        else:
            quedan.append(codigo)
    if quedan:
        resultado[SUELTO] = quedan
    return resultado


def cuantos_tramites(requisitos: list[str], detectados: dict) -> int:
    """Cuántas veces se está pidiendo el trámite, según los códigos recibidos.

    Un código de la clave que el trámite pide = una unidad ("tres CURP" son
    tres actas). Solo cuentan los códigos del tipo que discrimina: tres correos
    NO son tres actas. Si el trámite no pide una sola clave, o no llegó ninguna
    aplicable, es UNA unidad — que es lo que el cliente pidió.

    Esto NO fija el precio final: alimenta un estimado que el asesor confirma.
    """
    claves = [t for t in _tipos(requisitos) if t in TIPOS_CLAVE]
    if len(claves) != 1:
        return 1
    aplicables = detectados.get(claves[0]) or detectados.get(SUELTO) or []
    return min(max(len(aplicables), 1), MAX_CODIGOS)


def _tipos(requisitos: list[str]) -> list[str | None]:
    return [tipo_del_requisito(r) for r in requisitos]


def tipo_del_requisito(requisito: str) -> str | None:
    """El ÚNICO tipo de dato que pide ese requisito del catálogo, o None.

    None también cuando pide varios ("nss y curp", "rfc o clave curp"): ahí un
    solo código no lo satisface y darlo por recibido escondería lo que falta.
    Esos los sigue preguntando el agente, como hasta ahora."""
    normalizado = nlu._norm(requisito)
    tipos = [
        tipo
        for tipo, palabras in _PALABRAS_TIPO.items()
        if any(
            re.search(rf"(?<![a-z0-9]){re.escape(p)}(?![a-z0-9])", normalizado)
            for p in palabras
        )
    ]
    return tipos[0] if len(tipos) == 1 else None


def trae_el_dato(requisito: str, text: str) -> bool:
    """¿La respuesta del cliente trae algo con forma de la clave que se le pidió?

    Solo opina de requisitos que piden UNA clave; de cualquier otro dato (un
    nombre, una dirección) no hay formato que validar y contesta True."""
    tipo = tipo_del_requisito(requisito)
    if tipo not in TIPOS_CLAVE:
        return True
    detectados = detectar(text)
    return bool(detectados.get(tipo) or detectados.get(SUELTO))


def prellenar(requisitos: list[dict], detectados: dict[str, list[str]]) -> list[dict]:
    """Copia en los requisitos lo que el cliente ya escribió en el mismo mensaje
    con que pidió el trámite. Devuelve una lista NUEVA; no toca la de entrada.

    Varios códigos del mismo tipo se guardan juntos en el mismo requisito: el
    agente no abre un folio por cada uno — quien los desdobla es el asesor."""
    tipos = _tipos([r["req"] for r in requisitos])
    sueltos = detectados.get(SUELTO) or []
    # Un código sin clasificar solo se puede asignar sin ambigüedad cuando el
    # trámite pide UNA sola clave: si pide NSS y CURP en líneas aparte no hay
    # forma de saber cuál es cuál, y se prefiere preguntar.
    if len([t for t in tipos if t in TIPOS_CLAVE]) != 1:
        sueltos = []
    llenos = []
    for requisito, tipo in zip(requisitos, tipos):
        valores = (detectados.get(tipo) if tipo else None) or (
            sueltos if tipo in TIPOS_CLAVE else []
        )
        nuevo = dict(requisito)
        if valores and not nuevo.get("valor"):
            nuevo["valor"] = ", ".join(valores)
        llenos.append(nuevo)
    return llenos
