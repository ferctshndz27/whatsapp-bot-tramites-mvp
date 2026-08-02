"""Representación del dinero: entero de centavos, Decimal en la frontera.

Cada prueba nombra el bug exacto que previene (diseño §6 y §9.1-4).
"""

import pytest
from sqlalchemy import text
from sqlmodel import Session

import app.db as db
from app.dinero import formato_monto, parse_monto
from app.models import Cobro


def test_parse_monto_valores_validos():
    assert parse_monto("1,250.00") == 125000
    assert parse_monto("$1250") == 125000
    assert parse_monto("$ 1,250.50") == 125050
    assert parse_monto("10") == 1000


def test_parse_monto_1999_no_1998():
    """PREVIENE: int(float("19.99")*100) == 1998 — el centavo perdido del float."""
    assert parse_monto("19.99") == 1999


@pytest.mark.parametrize(
    "texto", ["", "0", "-5", "1250.005", "abc", "1e3", "1E2", "1.234,56", "NaN", "Infinity"]
)
def test_parse_monto_rechaza_capturas_invalidas(texto):
    """Más de 2 decimales es error de captura, no algo que redondear; la
    notación científica y NaN/Infinity son Decimal válidos pero montos absurdos."""
    with pytest.raises(ValueError):
        parse_monto(texto)


def test_monto_se_guarda_como_integer_fisico():
    """PREVIENE el Decimal->REAL: un Decimal(12,2) genera NUMERIC en el DDL pero
    SQLite lo guarda físicamente como REAL. El modelo usa int y el typeof físico
    debe ser 'integer' — con REAL los centavos dejan de cuadrar."""
    with Session(db.engine) as s:
        s.add(
            Cobro(
                phone="5215500001111", concepto="CURP", monto_centavos=125000,
                token_verificacion="tok-typeof", creado_por="ana",
            )
        )
        s.commit()
        tipo = s.exec(
            text("SELECT typeof(monto_centavos) FROM cobro LIMIT 1")
        ).scalar()
    assert tipo == "integer"


def test_formato_monto():
    assert formato_monto(125050, "mxn") == "$1,250.50 MXN"
    assert formato_monto(1000) == "$10.00 MXN"
    # Reembolsos: el signo no puede quedar pegado dentro del número.
    assert formato_monto(-990) == "-$9.90 MXN"
