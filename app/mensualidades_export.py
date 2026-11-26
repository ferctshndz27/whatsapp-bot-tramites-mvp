"""Exportación del calendario de cobranza a un .xlsx real (F6), con la biblioteca estándar.

REGLAS: se exporta todo lo que el usuario ve (su alcance), no solo lo filtrado en pantalla. CURP, NSS
y RFC son PII: por omisión NO van; con la casilla marcada van completos. Sin la casilla las columnas
se OMITEN, no se enmascaran. Todo texto pasa por `celda_csv` (inyección de fórmulas).
"""

import io
import re
import zipfile
from datetime import date, datetime
from xml.sax.saxutils import escape

from app.utils import celda_csv

_ILEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")  # XML 1.0 no admite estos controles

MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _letra(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _celda(ref: str, valor) -> str:
    valor = celda_csv(valor)
    if valor is None or valor == "":
        return ""
    if isinstance(valor, bool):
        return f'<c r="{ref}" t="b"><v>{int(valor)}</v></c>'
    if isinstance(valor, (int, float)):
        return f'<c r="{ref}"><v>{valor}</v></c>'
    if isinstance(valor, (date, datetime)):
        valor = valor.strftime("%Y-%m-%d %H:%M" if isinstance(valor, datetime) else "%Y-%m-%d")
    texto = escape(_ILEGAL.sub("", str(valor)))
    return f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{texto}</t></is></c>'


def _hoja(encabezado: list[str], filas: list[list]) -> str:
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>']
    out.append('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>')
    for n, fila in enumerate([encabezado, *filas], start=1):
        celdas = "".join(_celda(f"{_letra(i)}{n}", v) for i, v in enumerate(fila))
        out.append(f'<row r="{n}">{celdas}</row>')
    out.append("</sheetData></worksheet>")
    return "".join(out)


def construir_xlsx(hojas: list[tuple[str, list[str], list[list]]]) -> bytes:
    """hojas: [(nombre, encabezado, filas)] -> bytes de un .xlsx."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            + "".join(
                f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                for i in range(1, len(hojas) + 1)
            )
            + "</Types>",
        )
        z.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            "</Relationships>",
        )
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(
                f'<sheet name="{escape(nombre)}" sheetId="{i}" r:id="rId{i}"/>'
                for i, (nombre, _, _) in enumerate(hojas, start=1)
            )
            + "</sheets></workbook>",
        )
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(
                f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                f'Target="worksheets/sheet{i}.xml"/>'
                for i in range(1, len(hojas) + 1)
            )
            + "</Relationships>",
        )
        for i, (_, enc, filas) in enumerate(hojas, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _hoja(enc, filas))
    return buf.getvalue()


ENC_MENSUALIDADES = [
    "Código", "Titular", "Personas", "WhatsApp", "Responsable", "Periodo", "Vence",
    "Importe (MXN)", "Estatus", "Pagado el", "Pagado por",
]
ENC_PERSONAS_BASE = ["Código del titular", "Titular", "Nombre", "Modalidad", "Alta", "Estado"]
ENC_PII = ["CURP", "NSS", "RFC"]


def encabezado_personas(con_pii: bool) -> list[str]:
    return [*ENC_PERSONAS_BASE, *ENC_PII] if con_pii else list(ENC_PERSONAS_BASE)
