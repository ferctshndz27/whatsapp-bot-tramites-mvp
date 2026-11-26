"""/mensualidades: calendario de cobranza, lista por estatus y cola «Por completar» (solo lectura).

La unidad de cobro es la cuenta (el titular); las personas que cubre se listan debajo de ella.
Quién ve qué: un admin ve la oficina que alcanza (`alcance`); cualquier otro usuario, solo las
cuentas donde figura en `MensualidadAsesor`. El alcance se aplica ANTES que cualquier filtro, con
EXISTS y no con JOIN: una cuenta compartida entre asesores no debe duplicar filas ni descuadrar los
conteos. Las escrituras (alta, baja, pago) llegan en las fases siguientes del plan.
"""

import logging
from collections import Counter
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import exists, func, or_
from sqlmodel import select

from app import mensualidades as dom
from app.auth import alcance, is_admin
from app.config import settings
from app.db import get_session
from app.dinero import formato_monto
from app.mensualidades_import import PREFIJO
from app.models import ClienteMensualidad, CuentaCobranza, Mensualidad, MensualidadAsesor
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Tope de filas de cada lista: el calendario del mes ya resume el resto.
LIMITE_LISTA = 200
LIMITE_POR_COMPLETAR = 50
LIMITE_TITULARES = 200
FILTROS = ("todos", *dom.ESTATUS)


def _cuentas_visibles(user: str):
    """Las cuentas vivas que este usuario puede ver: su oficina si es admin, su cartera si no."""
    q = select(CuentaCobranza.id).where(CuentaCobranza.habilitado == True)  # noqa: E712
    if is_admin(user):
        return alcance(user).filtrar(q, CuentaCobranza.office_id)
    return q.where(
        exists().where(
            MensualidadAsesor.cuenta_id == CuentaCobranza.id,
            MensualidadAsesor.username == user,
            MensualidadAsesor.habilitado == True,  # noqa: E712
        )
    )


def _orden_codigo(c: CuentaCobranza):
    """Los códigos del piloto son números: 2 antes que 10."""
    return (0, int(c.codigo), "") if c.codigo.isdigit() else (1, 0, c.codigo)


def _titulo(c: CuentaCobranza) -> str:
    return c.nombre or f"Cliente {c.codigo}"


def _coincide(q: str, c: CuentaCobranza, personas: list[ClienteMensualidad]) -> bool:
    """La búsqueda acierta por código o nombre del titular, o por el nombre de alguna de sus personas."""
    q = q.lower()
    return q in c.codigo.lower() or q in _titulo(c).lower() or any(q in p.nombre.lower() for p in personas)


def _suma(centavos: int) -> str:
    return formato_monto(centavos)


@router.get("/mensualidades")
def mensualidades(
    request: Request, mes: str = "", estatus: str = "todos", dia: int = 0, q: str = ""
):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)

    hoy = dom.hoy_mx()
    aviso = settings.mensualidad_aviso_dias
    primero = dom.parse_mes(mes, hoy)
    ultimo = dom.mes_vecino(primero, 1)
    estatus = estatus if estatus in FILTROS else "todos"
    q = q.strip()[:80]
    dia = dia if 1 <= dia <= 31 else 0

    with get_session() as session:
        ids = _cuentas_visibles(user)
        pesos = Mensualidad.importe_centavos

        # Todo lo vencido sin pagar, de cualquier mes: es lo que el asesor tiene que cobrar ya.
        vencidas_n, vencidas_monto = session.exec(
            select(func.count(Mensualidad.id), func.coalesce(func.sum(pesos), 0))
            .where(Mensualidad.cuenta_id.in_(ids))
            .where(Mensualidad.habilitado == True)  # noqa: E712
            .where(Mensualidad.pagado_at == None)  # noqa: E711
            .where(Mensualidad.vence_el < hoy)
        ).one()

        del_mes = session.exec(
            select(Mensualidad, CuentaCobranza)
            .join(CuentaCobranza, CuentaCobranza.id == Mensualidad.cuenta_id)
            .where(Mensualidad.cuenta_id.in_(ids))
            .where(Mensualidad.habilitado == True)  # noqa: E712
            .where(Mensualidad.vence_el >= primero, Mensualidad.vence_el < ultimo)
            .order_by(Mensualidad.vence_el, CuentaCobranza.codigo)
        ).all()

        cuentas = sorted(
            session.exec(select(CuentaCobranza).where(CuentaCobranza.id.in_(ids))).all(),
            key=_orden_codigo,
        )
        personas: dict[int, list[ClienteMensualidad]] = {}
        for p in session.exec(
            select(ClienteMensualidad)
            .where(ClienteMensualidad.cuenta_id.in_(ids))
            .where(ClienteMensualidad.habilitado == True)  # noqa: E712
            .order_by(ClienteMensualidad.nombre)
        ).all():
            personas.setdefault(p.cuenta_id, []).append(p)

        por_completar = [c for c in cuentas if dom.faltantes(c)]

    # Una sola pasada: el estatus de cada fila y el conteo de cada día salen del mismo cálculo.
    filas = []
    por_dia: dict[int, Counter] = {}
    cuenta = Counter()
    for m, c in del_mes:
        est = dom.estado_de(m, hoy, aviso)
        cuenta[est] += 1
        por_dia.setdefault(m.vence_el.day, Counter())[est] += 1
        filas.append({"m": m, "c": c, "estatus": est, "etiqueta": dom.ETIQUETAS[est]})

    visibles = [
        f
        for f in filas
        if (estatus == "todos" or f["estatus"] == estatus)
        and (not dia or f["m"].vence_el.day == dia)
        and (not q or _coincide(q, f["c"], personas.get(f["c"].id, [])))
    ]
    for f in filas:
        f["titulo"] = _titulo(f["c"])
        f["n_personas"] = sum(1 for p in personas.get(f["c"].id, []) if not p.baja_at)

    titulares = [
        {
            "c": c,
            "titulo": _titulo(c),
            "personas": personas.get(c.id, []),
            "vivas": sum(1 for p in personas.get(c.id, []) if not p.baja_at),
            "falta": dom.faltantes(c),
        }
        for c in cuentas
        if not q or _coincide(q, c, personas.get(c.id, []))
    ]
    n_personas = sum(len(v) for v in personas.values())

    semanas = [
        [
            None
            if d is None
            else {
                "dia": d.day,
                "hoy": d == hoy,
                # Orden de gravedad: lo vencido se lee primero.
                "chips": [(e, por_dia[d.day][e]) for e in dom.ESTATUS if por_dia.get(d.day, {}).get(e)],
            }
            for d in semana
        ]
        for semana in dom.semanas_del_mes(primero)
    ]

    pendientes = [(c, _titulo(c), dom.faltantes(c)) for c in por_completar]

    return templates.TemplateResponse(
        request,
        "mensualidades.html",
        {
            "user": user,
            "mes_titulo": f"{dom.MESES[primero.month - 1].capitalize()} {primero.year}",
            "mes_valor": primero.strftime("%Y-%m"),
            "mes_anterior": dom.mes_vecino(primero, -1).strftime("%Y-%m"),
            "mes_siguiente": ultimo.strftime("%Y-%m"),
            "es_mes_actual": primero == hoy.replace(day=1),
            "semanas": semanas,
            "dias_semana": ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"),
            "estatus": estatus,
            "filtros": FILTROS,
            "etiquetas": dom.ETIQUETAS,
            "cuenta": cuenta,
            "dia": dia,
            "q": q,
            "filas": visibles[:LIMITE_LISTA],
            "truncada": len(visibles) > LIMITE_LISTA,
            "vencidas_n": vencidas_n,
            "vencidas_monto": _suma(vencidas_monto),
            "por_completar": pendientes[:LIMITE_POR_COMPLETAR],
            "por_completar_n": len(pendientes),
            "titulares": titulares[:LIMITE_TITULARES],
            "titulares_n": len(titulares),
            "cuentas_n": len(cuentas),
            "personas_n": n_personas,
            "prefijo_revisar": PREFIJO,
            "aviso": aviso,
        },
    )
