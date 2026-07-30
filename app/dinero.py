"""Fuente única de la representación del dinero en el sistema.

El dinero SIEMPRE es un entero de centavos (mapea 1:1 al unit_amount de
Stripe). Las únicas dos conversiones texto<->centavos del sistema viven aquí:
`parse_monto` en la frontera de entrada y `formato_monto` en la de salida.
El cliente, el asesor y la bitácora ven exactamente el mismo string.

Sin dependencias del proyecto: el dominio, las plantillas y los mensajes de
WhatsApp lo importan, nunca al revés.
"""

from decimal import Decimal, InvalidOperation

# Minúsculas: es lo que exige y devuelve la API de Stripe. El sufijo visible
# "MXN" lo agrega formato_monto, no la columna.
MONEDA_DEFAULT = "mxn"

# Abajo de $10 Stripe MX rechaza el cargo y OXXO no emite voucher.
MONTO_MIN_CENTAVOS = 1000
# El asesor teclea el monto a mano: un cero de más es el riesgo operativo #1.
# El tope operativo es configurable (COBRO_MONTO_MAXIMO_CENTAVOS); este es el
# default si no se define.
MONTO_MAX_CENTAVOS_DEFAULT = 10_000_000  # $100,000
# Tope por voucher de OXXO (~$10,000): se rechaza ANTES de llamar a Stripe.
OXXO_MAX_CENTAVOS = 1_000_000


def parse_monto(texto: str) -> int:
    """'1,250.00' -> 125000. Decimal, nunca float: int(float('19.99')*100)
    da 1998 y ese centavo perdido no cuadra contra el estado de cuenta.

    Rechaza vacío, cero, negativos, más de 2 decimales y notación científica:
    más de 2 decimales es un error de captura, no algo que redondear.
    """
    limpio = (texto or "").strip().replace("$", "").replace(" ", "").replace(",", "")
    # Decimal acepta '1e3', 'NaN' e 'Infinity'; en un monto tecleado a mano
    # cualquiera de esos es un error de captura.
    if not limpio or "e" in limpio.casefold():
        raise ValueError(f"monto inválido: {texto!r}")
    try:
        monto = Decimal(limpio)
    except InvalidOperation:
        raise ValueError(f"monto inválido: {texto!r}") from None
    if not monto.is_finite() or monto <= 0:
        raise ValueError(f"monto inválido: {texto!r}")
    if -monto.as_tuple().exponent > 2:
        raise ValueError(f"monto con más de 2 decimales: {texto!r}")
    return int(monto * 100)


def formato_monto(centavos: int, moneda: str = MONEDA_DEFAULT) -> str:
    """125050 -> '$1,250.50 MXN'. Única función que formatea dinero.

    PROHIBIDO volver a parsear su salida: los CSV llevan los centavos crudos.
    """
    signo = "-" if centavos < 0 else ""
    entero, resto = divmod(abs(centavos), 100)
    return f"{signo}${entero:,}.{resto:02d} {moneda.upper()}"


if __name__ == "__main__":  # pragma: no cover
    assert parse_monto("1,250.00") == 125000
    assert parse_monto("$1250") == 125000
    assert parse_monto("19.99") == 1999  # int(float(...)*100) daría 1998
    for malo in ("", "0", "-5", "1250.005", "abc", "1e3", "1.234,56", "NaN"):
        try:
            parse_monto(malo)
        except ValueError:
            pass
        else:
            raise AssertionError(f"parse_monto aceptó {malo!r}")
    assert formato_monto(125050) == "$1,250.50 MXN"
    assert formato_monto(-990) == "-$9.90 MXN"
    print("ok")
