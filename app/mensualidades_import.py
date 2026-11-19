"""Importación de titulares y personas desde el Excel del piloto (columnas No, NOMBRE, CURP, NSS,
RFC, ESTATUS, MODALIDAD, CLIENTE, ALTA). CLIENTE es el código del titular: crea o reutiliza una
`CuentaCobranza`; cada fila es una persona (`ClienteMensualidad`) bajo ella.

Reglas de limpieza (el Excel de origen perdió datos al guardarse como número):
  - Nombre: sin espacios sobrantes. CURP y RFC: sin espacios y en mayúsculas.
  - NSS de 10 dígitos: se completa con un cero a la izquierda (Excel lo comió) y se avisa.
  - CURP que no mide 18: se CONSERVA (puede corregirse a mano) y se avisa. NSS inservible y RFC
    inservible («SIN RFC», 2 letras): se omiten, porque no informan nada, y se avisa.
Los avisos van en `observaciones` de la persona con el prefijo «Revisar (importación):»; nunca
llevan el valor del dato personal. Es idempotente: repetirla no duplica cuentas ni personas.
Sin CURP o NSS en ningún log ni en el resultado: solo conteos.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from sqlmodel import Session, select

from app.models import ClienteMensualidad, CuentaCobranza, utcnow

PREFIJO = "Revisar (importación):"
MODALIDADES = ("BASICA", "SEMIESTABLE", "ALTA_ESTABLE")
_CURP = re.compile(r"[A-Z]{4}\d{6}[HM][A-Z]{5}[A-Z0-9]\d")
_RFC = re.compile(r"[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}")


@dataclass
class Resultado:
    cuentas_nuevas: int = 0
    personas_nuevas: int = 0
    personas_existentes: int = 0
    a_revisar: int = 0
    errores: list[str] = field(default_factory=list)  # «fila N: motivo», sin datos personales


def _texto(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _sin_espacios(v) -> str:
    return re.sub(r"\s+", "", str(v or "")).upper()


def _fecha(v) -> date | None:
    v = _texto(v)
    if not v:
        return None
    if re.fullmatch(r"\d+(\.0+)?", v):  # serial de Excel
        return date(1899, 12, 30) + timedelta(days=int(float(v)))
    return datetime.strptime(v[:10], "%Y-%m-%d").date()


def _codigo(v) -> str:
    v = _texto(v)
    return v[:-2] if re.fullmatch(r"\d+\.0", v) else v


def normalizar(fila: dict) -> tuple[dict, list[str]]:
    """Una fila cruda → campos limpios + avisos. Lanza ValueError si la fila no sirve."""
    avisos: list[str] = []
    nombre = _texto(fila.get("NOMBRE"))
    codigo = _codigo(fila.get("CLIENTE"))
    if not nombre:
        raise ValueError("sin nombre")
    if not codigo:
        raise ValueError("sin código de cliente")

    curp = _sin_espacios(fila.get("CURP")) or None
    if curp and not _CURP.fullmatch(curp):
        avisos.append("CURP con formato inválido")

    nss = re.sub(r"\D", "", re.sub(r"\.0+$", "", _texto(fila.get("NSS")))) or None
    if nss and len(nss) == 10:
        nss = nss.zfill(11)
        avisos.append("NSS completado con un cero a la izquierda")
    elif nss and len(nss) != 11:
        nss = None
        avisos.append("NSS inválido, omitido")

    rfc = _sin_espacios(fila.get("RFC")) or None
    if rfc and not _RFC.fullmatch(rfc):
        rfc = None
        avisos.append("RFC inválido, omitido")

    modalidad = _sin_espacios(fila.get("MODALIDAD")) or "BASICA"
    if modalidad not in MODALIDADES:
        raise ValueError("modalidad desconocida")
    estatus = _sin_espacios(fila.get("ESTATUS")) or "ALTA"
    if estatus not in ("ALTA", "BAJA"):
        raise ValueError("estatus desconocido")
    try:
        alta = _fecha(fila.get("ALTA"))
    except ValueError:
        alta = None
        avisos.append("fecha de alta ilegible, omitida")

    return (
        dict(nombre=nombre, codigo=codigo, curp=curp, nss=nss, rfc=rfc,
             modalidad=modalidad, baja=estatus == "BAJA", fecha_inicio=alta),
        avisos,
    )


def importar(session: Session, filas: list[dict], office_id: int, usuario: str = "importacion") -> Resultado:
    """Crea cuentas y personas. No hace commit: lo decide quien llama (así --ensayo es un rollback).
    Una fila con error no corta la corrida; queda en `errores` y las demás se importan."""
    res = Resultado()
    cuentas: dict[str, CuentaCobranza] = {
        c.codigo: c
        for c in session.exec(
            select(CuentaCobranza).where(
                CuentaCobranza.office_id == office_id, CuentaCobranza.habilitado == True  # noqa: E712
            )
        )
    }
    for n, fila in enumerate(filas, start=2):  # la fila 1 del Excel es el encabezado
        try:
            d, avisos = normalizar(fila)
        except ValueError as e:
            res.errores.append(f"fila {n}: {e}")
            continue
        cuenta = cuentas.get(d["codigo"])
        if cuenta is None:
            cuenta = CuentaCobranza(codigo=d["codigo"], office_id=office_id, created_by=usuario)
            session.add(cuenta)
            session.flush()
            cuentas[d["codigo"]] = cuenta
            res.cuentas_nuevas += 1

        previa = None
        if d["curp"]:
            previa = session.exec(
                select(ClienteMensualidad).where(
                    ClienteMensualidad.curp == d["curp"], ClienteMensualidad.habilitado == True  # noqa: E712
                )
            ).first()
        if previa is None:
            previa = session.exec(
                select(ClienteMensualidad).where(
                    ClienteMensualidad.cuenta_id == cuenta.id,
                    ClienteMensualidad.nombre == d["nombre"],
                    ClienteMensualidad.habilitado == True,  # noqa: E712
                )
            ).first()
        if previa is not None:
            res.personas_existentes += 1
            continue

        p = ClienteMensualidad(
            nombre=d["nombre"], curp=d["curp"], nss=d["nss"], rfc=d["rfc"],
            modalidad=d["modalidad"], cuenta_id=cuenta.id, fecha_inicio=d["fecha_inicio"],
            observaciones=f"{PREFIJO} " + "; ".join(avisos) if avisos else None,
            office_id=office_id, created_by=usuario,
        )
        if d["baja"]:
            p.baja_at = utcnow()
            p.baja_motivo = "Importada con estatus BAJA"
        session.add(p)
        res.personas_nuevas += 1
        res.a_revisar += bool(avisos)
    session.flush()
    return res
