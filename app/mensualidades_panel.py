"""/mensualidades: calendario de cobranza, lista por estatus y cola «Por completar» (solo lectura).

La unidad de cobro es la cuenta (el titular); las personas que cubre se listan debajo de ella.
Quién ve qué: un admin ve la oficina que alcanza (`alcance`); cualquier otro usuario, solo las
cuentas donde figura en `MensualidadAsesor`. El alcance se aplica ANTES que cualquier filtro, con
EXISTS y no con JOIN: una cuenta compartida entre asesores no debe duplicar filas ni descuadrar los
conteos. Escribir los datos de cobro de un titular (importe, día, primer pago, WhatsApp, responsable) es
solo de un admin de la oficina de la cuenta; al guardar se generan sus mensualidades. El alta, la
baja y el pago llegan en las fases siguientes del plan.
"""

import logging
from urllib.parse import urlencode
from collections import Counter
from datetime import date

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import exists, func, or_
from sqlmodel import select

from app import mensualidades as dom
from app import mensualidades_captura as captura
from app import mensualidades_export as export
from app import mensualidades_pago as pago
from app.audit import log_admin
from app.auth import alcance, is_admin
from app.config import settings
from app.db import get_session
from app.dinero import formato_monto
from app.mensualidades_import import PREFIJO
from app.models import ClienteMensualidad, CuentaCobranza, Mensualidad, MensualidadAsesor, User
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


def _puede_capturar(user: str) -> bool:
    """Capturar los datos de cobro es de un admin. Aparte de la ruta para que el GET de la vista, que
    abre a cualquiera con cartera, no parezca una ruta de solo admin."""
    return is_admin(user)


def _puede_deshacer(user: str, responsable: str | None) -> bool:
    """Deshacer un pago: el responsable de la cuenta o un admin (no un colaborador). Aparte de la
    vista por la misma razón que `_puede_capturar`."""
    return is_admin(user) or responsable == user


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
    request: Request,
    mes: str = "",
    estatus: str = "todos",
    dia: int = 0,
    q: str = "",
    ok: str = "",
    err: str = "",
    pagada: str = "",
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
        responsables = {
            f.cuenta_id: f.username
            for f in session.exec(
                select(MensualidadAsesor)
                .where(MensualidadAsesor.cuenta_id.in_(ids))
                .where(MensualidadAsesor.habilitado == True)  # noqa: E712
                .where(MensualidadAsesor.rol == "responsable")
            ).all()
        }
        puede_capturar = _puede_capturar(user)
        asesores = (
            [
                u.username
                for u in session.exec(
                    select(User)
                    .where(User.habilitado == True)  # noqa: E712
                    .where(User.office_id.in_({c.office_id for c in cuentas}))
                    .order_by(User.username)
                ).all()
            ]
            if puede_capturar and cuentas
            else []
        )

    # Una sola pasada: el estatus de cada fila y el conteo de cada día salen del mismo cálculo.
    filas = []
    por_dia: dict[int, Counter] = {}
    cuenta = Counter()
    for m, c in del_mes:
        est = dom.estado_de(m, hoy, aviso)
        cuenta[est] += 1
        por_dia.setdefault(m.vence_el.day, Counter())[est] += 1
        filas.append(
            {
                "m": m, "c": c, "estatus": est, "etiqueta": dom.ETIQUETAS[est],
                # Deshacer un pago: el responsable de la cuenta o un admin (no un colaborador).
                "puede_deshacer": _puede_deshacer(user, responsables.get(c.id)),
            }
        )

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
            "responsable": responsables.get(c.id),
            "asesores": asesores,
            "importe_texto": f"{c.importe_centavos / 100:.2f}" if c.importe_centavos is not None else "",
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
            "puede_capturar": puede_capturar,
            "ok": int(ok) if ok.isdigit() and len(ok) < 5 else None,
            "error": captura.MENSAJES.get(err, ""),  # solo códigos conocidos: la URL no inyecta texto
            "error_pago": pago.MENSAJES_PAGO.get(err, ""),
            "pagada": pagada in ("1", "0"),
            "pagada_deshecha": pagada == "0",
            "mes_param": mes if mes else "",
            "aviso": aviso,
        },
    )


def _volver(**params) -> RedirectResponse:
    return RedirectResponse(f"/mensualidades?{urlencode(params)}#titulares", status_code=303)


@router.post("/mensualidades/cuentas/{cuenta_id}/datos")
def guardar_datos(
    request: Request,
    cuenta_id: int,
    whatsapp: str = Form(""),
    importe: str = Form(""),
    dia_pago: str = Form(""),
    primer_pago: str = Form(""),
    responsable: str = Form(""),
):
    """Captura los datos de cobro del titular y genera sus mensualidades. Solo admin.

    Todo o nada: si un campo viene mal no se escribe ninguno. Un campo vacío no cambia lo que ya
    había (no se puede borrar un dato desde aquí). Una cuenta fuera del alcance contesta 404, igual
    que una que no existe.
    """
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    with get_session() as session:
        visible = session.exec(
            select(CuentaCobranza.id)
            .where(CuentaCobranza.id == cuenta_id)
            .where(CuentaCobranza.id.in_(_cuentas_visibles(user)))
        ).first()
        if visible is None:
            raise HTTPException(status_code=404)
        if not is_admin(user):
            return _volver(err="permiso")
        cuenta = session.get(CuentaCobranza, cuenta_id)
        cambios: dict = {}
        resp = responsable.strip()
        try:
            if whatsapp.strip():
                cambios["whatsapp"] = captura.parse_whatsapp(whatsapp)
            if importe.strip():
                cambios["importe_centavos"] = captura.parse_importe(importe)
            if dia_pago.strip():
                cambios["dia_pago"] = captura.parse_dia(dia_pago)
            if primer_pago.strip():
                cambios["primer_pago"] = captura.parse_fecha(primer_pago)
            if resp and not captura.usuario_de_la_oficina(session, resp, cuenta.office_id):
                raise captura.DatoInvalido("responsable")
        except captura.DatoInvalido as e:
            return _volver(err=e.codigo)
        for campo, valor in cambios.items():
            setattr(cuenta, campo, valor)
        session.add(cuenta)
        if resp:
            captura.asignar_responsable(session, cuenta, resp, por=user)
        creadas = captura.generar(session, cuenta, dom.hoy_mx())
        session.commit()
        codigo = cuenta.codigo
    # Sin valores: el importe y el WhatsApp son datos del cliente.
    log_admin(user, "mensualidades-datos", f"cuenta:{codigo} campos:{','.join(sorted(cambios)) or '-'}")
    return _volver(ok=creadas)


def _volver_lista(mes: str, **params) -> RedirectResponse:
    hoy = dom.hoy_mx()
    params["mes"] = dom.parse_mes(mes, hoy).strftime("%Y-%m")  # solo un mes válido vuelve a la URL
    return RedirectResponse(f"/mensualidades?{urlencode(params)}#lista", status_code=303)


def _mensualidad_visible(session, user: str, mensualidad_id: int):
    """La mensualidad y su cuenta si el usuario alcanza la cuenta; si no, None (el llamador da 404)."""
    return session.exec(
        select(Mensualidad, CuentaCobranza)
        .join(CuentaCobranza, CuentaCobranza.id == Mensualidad.cuenta_id)
        .where(Mensualidad.id == mensualidad_id)
        .where(Mensualidad.habilitado == True)  # noqa: E712
        .where(Mensualidad.cuenta_id.in_(_cuentas_visibles(user)))
    ).first()


@router.post("/mensualidades/{mensualidad_id}/pago")
def registrar_pago(request: Request, mensualidad_id: int, mes: str = Form("")):
    """Registra el pago de una mensualidad. Cualquiera con la cuenta en su cartera, o el admin."""
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    with get_session() as session:
        fila = _mensualidad_visible(session, user, mensualidad_id)
        if fila is None:
            raise HTTPException(status_code=404)
        codigo, periodo = fila[1].codigo, fila[0].periodo
        try:
            pago.registrar_pago(session, mensualidad_id, por=user)
        except pago.PagoRechazado as e:
            return _volver_lista(mes, err=e.codigo)
    log_admin(user, "mensualidades-pago", f"cuenta:{codigo} periodo:{periodo}")
    return _volver_lista(mes, pagada=1)


@router.post("/mensualidades/{mensualidad_id}/pago/deshacer")
def deshacer_pago(request: Request, mensualidad_id: int, mes: str = Form("")):
    """Deshace un pago registrado por error. Solo el responsable de la cuenta o un admin."""
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    with get_session() as session:
        fila = _mensualidad_visible(session, user, mensualidad_id)
        if fila is None:
            raise HTTPException(status_code=404)
        cuenta = fila[1]
        if not (is_admin(user) or pago.es_responsable(session, cuenta.id, user)):
            return _volver_lista(mes, err="permiso-deshacer")
        codigo, periodo = cuenta.codigo, fila[0].periodo
        try:
            pago.deshacer_pago(session, mensualidad_id)
        except pago.PagoRechazado as e:
            return _volver_lista(mes, err=e.codigo)
    log_admin(user, "mensualidades-pago-deshecho", f"cuenta:{codigo} periodo:{periodo}")
    return _volver_lista(mes, pagada=0)


@router.get("/mensualidades/export.xlsx")
def exportar(request: Request, pii: str = ""):
    """.xlsx con todo lo que el usuario ve (su cartera, o su oficina si es admin), no solo lo filtrado.

    CURP, NSS y RFC solo salen con `pii=1` (la casilla, desmarcada por omisión); si no, las columnas
    se omiten. Queda en la bitácora con la bandera, nunca con los valores.
    """
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login", status_code=303)
    con_pii = pii == "1"
    hoy = dom.hoy_mx()
    with get_session() as session:
        ids = _cuentas_visibles(user)
        cuentas = sorted(session.exec(select(CuentaCobranza).where(CuentaCobranza.id.in_(ids))).all(), key=_orden_codigo)
        por_cuenta = {c.id: c for c in cuentas}
        personas: dict[int, list[ClienteMensualidad]] = {}
        for p in session.exec(
            select(ClienteMensualidad)
            .where(ClienteMensualidad.cuenta_id.in_(ids))
            .where(ClienteMensualidad.habilitado == True)  # noqa: E712
            .order_by(ClienteMensualidad.nombre)
        ).all():
            personas.setdefault(p.cuenta_id, []).append(p)
        responsables = {
            f.cuenta_id: f.username
            for f in session.exec(
                select(MensualidadAsesor)
                .where(MensualidadAsesor.cuenta_id.in_(ids))
                .where(MensualidadAsesor.habilitado == True)  # noqa: E712
                .where(MensualidadAsesor.rol == "responsable")
            ).all()
        }
        mens = session.exec(
            select(Mensualidad)
            .where(Mensualidad.cuenta_id.in_(ids))
            .where(Mensualidad.habilitado == True)  # noqa: E712
            .order_by(Mensualidad.vence_el, Mensualidad.cuenta_id)
        ).all()

        filas_m = []
        for m in mens:
            c = por_cuenta[m.cuenta_id]
            vivas = sum(1 for p in personas.get(c.id, []) if not p.baja_at)
            filas_m.append([
                c.codigo, _titulo(c), vivas, c.whatsapp, responsables.get(c.id), m.periodo,
                m.vence_el, m.importe_centavos / 100,
                dom.ETIQUETAS[dom.estado_de(m, hoy, settings.mensualidad_aviso_dias)],
                m.pagado_at, m.pagado_por,
            ])
        filas_p = []
        for c in cuentas:
            for p in personas.get(c.id, []):
                fila = [
                    c.codigo, _titulo(c), p.nombre, p.modalidad.replace("_", " ").lower().capitalize(),
                    p.fecha_inicio, "Baja" if p.baja_at else "Alta",
                ]
                if con_pii:
                    fila += [p.curp, p.nss, p.rfc]
                filas_p.append(fila)
        contenido = export.construir_xlsx([
            ("Mensualidades", export.ENC_MENSUALIDADES, filas_m),
            ("Personas", export.encabezado_personas(con_pii), filas_p),
        ])
    log_admin(user, "mensualidades-export", f"filas:{len(filas_m)} personas:{len(filas_p)} pii:{int(con_pii)}")
    return Response(
        content=contenido,
        media_type=export.MIME_XLSX,
        headers={"Content-Disposition": f'attachment; filename="mensualidades-{hoy.isoformat()}.xlsx"'},
    )
