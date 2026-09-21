import re

# Formato canónico del teléfono en TODA la base: solo dígitos, con lada de país
# y sin el "1" que México inserta en los móviles.
#
# Por qué existe esto: Twilio entrega "whatsapp:+5215500000000" y Meta entrega
# "5215500000000". Sin normalizar, la MISMA persona genera dos filas en `client`
# (la PK es el teléfono), dos conversaciones y dos historiales. Se normaliza en
# el borde: el formato de cada proveedor solo vive dentro de su adaptador.
_SOLO_DIGITOS = re.compile(r"\D")
LADA_MX = "52"
LARGO_NACIONAL_MX = 10
# Rango de E.164 (mínimo 8 con lada, máximo 15). Fuera de ahí no es un teléfono:
# devolver "123" tal cual crearía un cliente con una PK basura, imposible de
# encontrar después y —en cobranza— un cobro que nadie puede conciliar.
LARGO_MIN_E164 = 8
LARGO_MAX_E164 = 15


def normalize_phone(raw: str | None) -> str:
    """Teléfono en formato canónico. Devuelve '' si no hay nada aprovechable.

    Acepta lo que mandan los proveedores y lo que teclea un humano:
      'whatsapp:+5215500000000' · '+52 155 0000 0000' · '(55) 0000-0000'
      '5215500000000' · '525500000000' · '5500000000'
    y todas resuelven al mismo valor.

    Solo se toca el "1" de México: en otras ladas los dígitos se respetan tal
    cual, porque no conocemos sus reglas y adivinar sería peor que no hacer nada.
    """
    if not raw:
        return ""

    digitos = _SOLO_DIGITOS.sub("", raw)
    if not digitos:
        return ""

    # Número nacional tecleado por un asesor ("5500000000"): se le pone la lada.
    if len(digitos) == LARGO_NACIONAL_MX:
        return LADA_MX + digitos

    if not LARGO_MIN_E164 <= len(digitos) <= LARGO_MAX_E164:
        return ""

    # México inserta un "1" entre lada y número en móviles (52-1-55…). WhatsApp
    # lo manda de las dos formas según el canal; se colapsa a la forma sin "1"
    # para que "5215500000000" y "525500000000" sean la MISMA persona.
    if digitos.startswith(LADA_MX + "1") and len(digitos) == len(LADA_MX) + 1 + LARGO_NACIONAL_MX:
        return LADA_MX + digitos[len(LADA_MX) + 1 :]

    return digitos


def mask_phone(phone: str) -> str:
    return f"***{phone[-4:]}" if len(phone) > 4 else "***"


# Caracteres de control (TAB, LF, CR, NUL…) y la barra invertida, en cualquier
# posición. El parser de URL del navegador (WHATWG) DESCARTA tabuladores y
# saltos de línea antes de interpretar la ruta, así que «/\t/evil.com» —que
# empieza con una sola barra y pasaba el filtro— se convierte en «//evil.com»,
# una URL protocolo-relativa. Auditoría 2026-09 (INJ-2).
_CONTROL_O_BARRA_RE = re.compile(r"[\x00-\x1f\x7f\\]")


def ruta_interna(ruta: str) -> str:
    """Acepta solo rutas del propio panel para enlaces de «volver».

    El valor viene de la URL, así que cualquiera puede fabricarlo. «//» y
    «/\\» son URLs protocolo-relativas para el navegador: aceptar cualquiera
    de las dos convertiría el botón de volver en un open redirect. Lo mismo
    vale para cualquier carácter de control en medio: el navegador los quita
    al parsear y lo que queda vuelve a ser «//…».
    """
    if not ruta.startswith("/") or ruta.startswith("//") or _CONTROL_O_BARRA_RE.search(ruta):
        return ""
    return ruta


# Inyección de fórmulas en CSV: un texto que empieza con = + - @ (o con un
# tabulador / retorno de carro, que Excel también interpreta como inicio de
# celda activa) se EJECUTA al abrir el archivo en Excel o Sheets. Una comilla
# inicial lo vuelve literal. Los números pasan intactos, incluidos los
# negativos: convertirlos en texto rompería las sumas de quien abre el archivo.
# Fuente única para todos los exportes (system, cobranza, no-entendidos).
_INICIO_FORMULA = ("=", "+", "-", "@", "\t", "\r")


def celda_csv(valor):
    """Neutraliza un valor antes de escribirlo en un CSV que abrirá una hoja de
    cálculo. Devuelve el valor tal cual salvo cuando es texto con forma de
    fórmula; entonces lo antepone con una comilla simple."""
    if valor is None or isinstance(valor, (bool, int, float)):
        return valor
    s = str(valor)
    return "'" + s if s[:1] in _INICIO_FORMULA else valor


if __name__ == "__main__":  # pragma: no cover
    # Check mínimo: las cinco formas en que el mismo humano puede llegar.
    _canonico = "525500000000"
    for _entrada in (
        "whatsapp:+5215500000000",
        "+52 155 0000 0000",
        "5215500000000",
        "525500000000",
        "5500000000",
    ):
        assert normalize_phone(_entrada) == _canonico, (_entrada, normalize_phone(_entrada))
    assert normalize_phone("") == ""
    assert normalize_phone(None) == ""
    assert normalize_phone("sin dígitos") == ""
    # Lada ajena: se respetan los dígitos, no se inventan reglas.
    assert normalize_phone("+1 415 555 0100") == "14155550100"
    print("ok")
