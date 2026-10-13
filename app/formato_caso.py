"""El formato de caso: «qué pasó / servicio / ¿requiere investigar? / datos» (operador, 2026-10-13).

Módulo puro: lee un texto y devuelve lo que el cliente llenó. No toca la base ni decide nada;
`conversation._regla_del_formato` decide qué hacer con el resultado.

Lo que este módulo deliberadamente NO hace:
- No adivina etiquetas dentro de una frase: una etiqueta cuenta solo al inicio de una línea
  (después de un emoji o signos), y hacen falta al menos dos. «El servicio que necesito: …» en medio
  de una historia no vuelve formato a un mensaje.
- No interpreta la historia. Que nombre un trámite no lo convierte en uno: manda el campo «servicio».
- No inventa un «sí» o un «no»: lo que no se entiende queda en None y el bot hace la pregunta de
  siempre.

Las pistas del formato van ANTES de los dos puntos («¿Requiere investigar? (Sí / No):»), así el valor
es solo lo que el cliente escribe después y no hay que descartar instrucciones pegadas.
"""

import re
from dataclasses import dataclass

# Sin «asesor» ni «servicios» a propósito: el cliente copia este texto y `_is_escalation` y los
# atajos del catálogo se activan por esas palabras. Lo amarra la prueba del formato.
FORMATO_MENSAJE = (
    "Para atenderte mejor, copia este mensaje, llénalo y envíalo. "
    "Tus archivos puedes mandarlos antes o después:\n\n"
    "📖 Qué pasó: \n"
    "📋 Servicio que necesito: \n"
    "🔎 ¿Requiere investigar? (Sí / No): \n"
    "🪪 Datos que tengo (CURP, NSS, RFC): "
)
FORMATO_VACIO = (
    "Me llegó el formato sin llenar 🙏 Cuéntame qué pasó o qué necesitas y lo leo. "
    "Si quieres el formato otra vez, escribe *formato*."
)
COMANDOS = frozenset({"formato", "ayuda"})

_ETIQUETAS = {
    "historia": r"qu[eé]\s+pas[oó]|mi\s+historia|historia|qu[eé]\s+sucedi[oó]",
    "servicio": r"servicio(?:\s+que\s+(?:necesito|requiero|quiero))?|tr[aá]mite(?:\s+que\s+(?:necesito|requiero|quiero))?",
    "investigar": r"¿?\s*requiere\s+investigar\s*\??|¿?\s*hay\s+que\s+investigar\s*\??|investigar",
    "datos": r"datos(?:\s+que\s+tengo)?",
}
# Inicio de línea, emojis o signos, la etiqueta, una pista opcional entre paréntesis y los dos puntos.
_ETIQUETA_RE = re.compile(
    r"(?im)^[^\w¿\n]*(?P<etiqueta>"
    + "|".join(f"(?P<{k}>{v})" for k, v in _ETIQUETAS.items())
    + r")\s*(?:\([^)\n]*\))?\s*:"
)
_SI_RE = re.compile(r"(si|s|claro|por supuesto|investig\w*|revis\w*)\b")
_NO_SABE_RE = re.compile(r"no\s+se\b|no\s+estoy\s+seguro|\?+$|ni\s+idea|quiz\w*|tal\s+vez")


@dataclass(frozen=True)
class FormatoCaso:
    historia: str | None
    servicio: str | None
    investigar: bool | None  # None: en blanco o «no sé»
    datos: str | None

    @property
    def vacio(self) -> bool:
        """Sin nada que atender: ni historia ni servicio (los datos solos no dicen qué se pide)."""
        return self.historia is None and self.servicio is None

    def texto(self) -> str:
        """El formato como texto corrido, para las notas del caso o para la pregunta Revisión/Trámite."""
        partes = []
        if self.historia:
            partes.append(f"Qué pasó: {self.historia}")
        if self.servicio:
            partes.append(f"Servicio que pide: {self.servicio}")
        if self.datos:
            partes.append(f"Datos: {self.datos}")
        return "\n".join(partes)


def _norm(valor: str) -> str:
    from app import nlu  # import perezoso: nlu importa a conversation, que importa este módulo

    return nlu._norm(valor).strip()


def _limpio(valor: str) -> str | None:
    """None si no queda nada con letras o números (una línea en blanco, «-», «...»)."""
    valor = valor.strip()
    return valor if re.search(r"\w", valor) else None


def _investigar(valor: str | None) -> bool | None:
    if not valor:
        return None
    n = _norm(valor)
    if not n or _NO_SABE_RE.match(n):
        return None
    if n == "no" or re.match(r"no\b", n):
        return False
    if _SI_RE.match(n):
        return True
    return None


def leer(texto: str) -> FormatoCaso | None:
    """Lo que el cliente llenó, o None si el mensaje no es el formato (menos de dos etiquetas)."""
    marcas = list(_ETIQUETA_RE.finditer(texto))
    campos: dict[str, str] = {}
    for i, marca in enumerate(marcas):
        fin = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
        nombre = next(k for k in _ETIQUETAS if marca.group(k))
        campos.setdefault(nombre, texto[marca.end():fin])
    if len(campos) < 2:
        return None
    valores = {k: _limpio(v) for k, v in campos.items()}
    # «no sé» en el servicio es no haber dicho servicio: no se busca como si fuera un trámite.
    if valores.get("servicio") and _NO_SABE_RE.match(_norm(valores["servicio"])):
        valores["servicio"] = None
    return FormatoCaso(
        historia=valores.get("historia"),
        servicio=valores.get("servicio"),
        investigar=_investigar(valores.get("investigar")),
        datos=valores.get("datos"),
    )
