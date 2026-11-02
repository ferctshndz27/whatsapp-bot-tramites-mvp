"""Cobro manual: el colaborador genera la liga de Stripe y la manda él, por su WhatsApp.

Es el hermano del cobro de un folio (`cobranza_panel.generar_cobro`) con tres diferencias
a propósito:

- **No hay folio.** El cliente se elige por buscador (nombre, alias o teléfono) y, si no está
  registrado, se da de alta aquí mismo y —si se quiere— entra a la cartera de quien lo registra.
- **El bot no manda nada.** La liga se copia y la pega el colaborador en su WhatsApp. Por eso
  tampoco depende de la ventana de 24 h: no hay envío del proveedor que la necesite.
- **Queda rastro de la copia.** `cobro:liga-copiada` en la auditoría dice quién copió la liga y
  cuándo; el pago lo confirma, como siempre, el webhook de Stripe y solo él.

La emisión (candado, idempotencia, red fuera de transacción) es la misma
`cobranza_panel.emitir_cobro` que usa el folio: un solo camino para mover dinero. El candado es
el de siempre —teléfono + servicio— y sin servicio de catálogo cae en `{teléfono}:libre`: un
cobro manual vivo por cliente a la vez. Es a propósito: dos ligas vivas al mismo cliente es el
cobro duplicado que todo el diseño de cobranza evita.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import or_, select

from app import cartera, cobranza
from app.audit import log_admin
from app.auth import alcance, is_admin
from app.cobranza_panel import (
    MSI_PLAZOS_VALIDOS,
    METODOS_VALIDOS,
    _cargar_para_accion,
    _modo_real,
    emitir_cobro,
    validar_emision,
)
from app.db import get_session
from app.dinero import OXXO_MAX_CENTAVOS, formato_monto, parse_monto
from app.models import Client, Cobro
from app.templating import templates
from app.utils import mask_phone, normalize_phone

logger = logging.getLogger(__name__)

router = APIRouter()

ORIGEN = "manual"
MAX_NOMBRE = 200
MAX_CONCEPTO = 300
MIN_BUSQUEDA = 2
MAX_RESULTADOS = 8
MAX_RECIENTES = 10

# Código -> texto. La ruta devuelve códigos, la plantilla los traduce (mismo criterio que
# `clients._ERRORES`): ningún texto del cliente ni de Stripe llega al HTML sin pasar por aquí.
ERRORES = {
    "telefono": "Escribe un teléfono válido (10 dígitos o con lada).",
    "nombre": "Este cliente no está registrado: escribe su nombre para registrarlo.",
    "otra-oficina": "Ese teléfono ya pertenece a otra oficina. No se puede cobrar desde aquí.",
    "inhabilitado": "Ese cliente está bloqueado. Desbloquéalo en su ficha antes de cobrarle.",
    "concepto": "Escribe el servicio que se cobra: es lo que ve el cliente.",
    "monto": "El monto no es válido. Escríbelo así: 1,250.00",
    "monto-minimo": "El monto es menor al mínimo que acepta Stripe.",
    "monto-maximo": "El monto pasa del máximo permitido por cobro.",
    "monto-oxxo": "OXXO no acepta fichas de ese monto. Quita OXXO o baja el importe.",
    "sin-stripe": "Stripe no está configurado: no se generó nada.",
    "sin-url-publica": "Falta PUBLIC_BASE_URL en la configuración: Stripe rechazaría el cobro. No se generó nada.",
    "cobro-vivo": "Este cliente ya tiene una liga manual sin pagar. Espera a que pague o cancélala en Mi cartera.",
    "stripe": "Stripe no devolvió la liga. El cobro quedó en borrador; revísalo en Mi cartera antes de reintentar.",
    "interno": "No se pudo guardar el cobro. Intenta de nuevo; si sigue, avisa al administrador.",
}


def _current_user(request: Request) -> str | None:
    return request.session.get("user")


def _busqueda(vista, user: str, termino: str):
    """Misma regla que el buscador de /clients (nombre, alias, teléfono parcial o canónico):
    si aquí se buscara distinto, el asesor concluiría que no existe y lo daría de alta dos veces."""
    patron = f"%{termino}%"
    canonico = normalize_phone(termino)
    condiciones = [Client.name.ilike(patron), Client.alias.ilike(patron), Client.phone.like(patron)]
    if canonico:
        condiciones.append(Client.phone == canonico)
    return (
        vista.filtrar(select(Client), Client.office_id)
        .where(Client.habilitado == True)  # noqa: E712
        .where(or_(*condiciones))
    )


def _recientes(session, user: str, vista) -> list[Cobro]:
    consulta = vista.filtrar(select(Cobro), Cobro.office_id).where(
        Cobro.origen == ORIGEN, Cobro.habilitado == True  # noqa: E712
    )
    if not is_admin(user):
        consulta = consulta.where(Cobro.creado_por == user)
    return list(session.exec(
        consulta.order_by(Cobro.created_at.desc()).limit(MAX_RECIENTES)  # type: ignore[union-attr]
    ).all())


def _contexto(request: Request, user: str, **extra) -> dict:
    vista = alcance(user)
    with get_session() as session:
        recientes = _recientes(session, user, vista)
        creado = None
        cobro_id = extra.pop("cobro_id", None)
        if cobro_id:
            candidato = session.get(Cobro, cobro_id)
            # Solo se muestra un cobro manual del alcance de quien mira: el id llega por la URL.
            if (candidato is not None and candidato.origen == ORIGEN
                    and vista.alcanza(candidato.office_id)
                    and (is_admin(user) or candidato.creado_por == user)):
                creado = candidato
        # Las filas ya están leídas: se sueltan para pintarlas con la sesión cerrada.
        session.expunge_all()
    contexto = {
        "request": request, "user": user, "errores": ERRORES, "recientes": recientes,
        "creado": creado, "es_admin": is_admin(user),
        "mensaje_creado": cobranza.mensaje_link(creado) if creado else "",
        "oxxo_max_centavos": OXXO_MAX_CENTAVOS, "modo": "real" if _modo_real() else "prueba",
        "formato_monto": formato_monto,
        "token_form": uuid4().hex,
        "form": {}, "err": "",
    }
    contexto.update(extra)
    return contexto


@router.get("/cobros/nuevo")
def cobro_nuevo(request: Request, cobro: int = 0, phone: str = ""):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)
    form = {"phone": normalize_phone(phone)} if phone else {}
    return templates.TemplateResponse(
        request, "cobro_nuevo.html", _contexto(request, user, cobro_id=cobro, form=form)
    )


@router.get("/cobros/nuevo/clientes")
def cobro_buscar_cliente(request: Request, q: str = ""):
    """Autocompletado del buscador: JSON mínimo, solo lo que el asesor ya ve en /clients."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "login"}, status_code=401)
    termino = q.strip()
    if len(termino) < MIN_BUSQUEDA:
        return JSONResponse({"clientes": [], "q": termino})
    vista = alcance(user)
    with get_session() as session:
        clientes = session.exec(
            _busqueda(vista, user, termino).order_by(Client.last_activity_at.desc().nulls_last(),  # type: ignore[union-attr]
                                                      Client.created_at.desc()).limit(MAX_RESULTADOS)
        ).all()
        suyos = cartera.clientes_de(session, user)
        datos = [
            {"phone": c.phone, "name": c.name or "", "alias": c.alias or "", "mio": c.phone in suyos}
            for c in clientes
        ]
    # Los suyos primero: es a quienes más les cobra.
    datos.sort(key=lambda d: not d["mio"])
    return JSONResponse({"clientes": datos, "q": termino})


def _error(request: Request, user: str, codigo: str, form: dict):
    """Se pinta en la misma página (no redirige) para no perder lo que el asesor tecleó."""
    return templates.TemplateResponse(
        request, "cobro_nuevo.html", _contexto(request, user, err=codigo, form=form),
        status_code=422,
    )


@router.post("/cobros/nuevo")
def cobro_crear(
    request: Request,
    phone: str = Form(""),
    name: str = Form(""),
    concepto: str = Form(""),
    monto: str = Form(""),
    metodo: list[str] = Form(default=[]),
    msi: str = Form(""),
    cartera_propia: str = Form("", alias="cartera"),
    token_form: str = Form(""),
):
    user = _current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=303)

    metodos = [m for m in metodo if m in METODOS_VALIDOS] or ["card"]
    form = {"phone": phone.strip(), "name": name.strip(), "concepto": concepto.strip(),
            "monto": monto.strip(), "metodos": metodos, "msi": msi, "cartera": bool(cartera_propia)}

    canonico = normalize_phone(phone)
    if not canonico:
        return _error(request, user, "telefono", form)
    form["phone"] = canonico
    texto_concepto = concepto.strip()[:MAX_CONCEPTO]
    if not texto_concepto:
        return _error(request, user, "concepto", form)
    try:
        monto_centavos = parse_monto(monto)
    except ValueError:
        return _error(request, user, "monto", form)
    # Antes de registrar a nadie: un monto o una llave mal puestos no deben dejar un cliente
    # dado de alta a medias.
    problema = validar_emision(monto_centavos, metodos)
    if problema:
        return _error(request, user, problema, form)
    if not token_form:
        return _error(request, user, "interno", form)

    vista = alcance(user)
    nombre = name.strip()[:MAX_NOMBRE]
    alta = False
    with get_session() as session:
        cliente = session.get(Client, canonico)
        if cliente is not None and not vista.alcanza(cliente.office_id):
            # El teléfono es la llave: un cliente es de UNA oficina. Cobrarle desde otra
            # mezclaría el dinero de dos libros.
            return _error(request, user, "otra-oficina", form)
        if cliente is not None and not cliente.habilitado:
            return _error(request, user, "inhabilitado", form)
        if cliente is None:
            if not nombre:
                return _error(request, user, "nombre", form)
            # Nace en la oficina de quien lo registra (misma regla que /clients).
            cliente = Client(phone=canonico, name=nombre, office_id=vista.office_id, origen="manual")
            session.add(cliente)
            session.commit()
            alta = True
        nombre_cobro = cliente.name
        if cartera_propia:
            # Después del commit: la cartera valida contra la fila Client ya guardada.
            cartera.agregar(session, canonico, user, por=user)
    if alta:
        log_admin(user, "cliente:alta", f"{canonico} · {nombre}")
        logger.info("Cliente %s registrado desde cobro manual por %s", mask_phone(canonico), user)

    resultado, cobro_id = emitir_cobro(
        user=user, phone=canonico, client_name=nombre_cobro, concepto=texto_concepto,
        monto_centavos=monto_centavos, metodos=metodos,
        msi_plazos=msi if msi in MSI_PLAZOS_VALIDOS else None,
        token_form=token_form, origen=ORIGEN,
    )
    if resultado == "duplicado":
        # F5 o doble clic: el cobro ya existe; se manda a la lista, donde está su liga.
        return RedirectResponse("/cobros/nuevo", status_code=303)
    if resultado != "ok":
        return _error(request, user, resultado, form)
    log_admin(user, "cobro:manual-generado", f"#{cobro_id} {mask_phone(canonico)} {monto_centavos}")
    return RedirectResponse(f"/cobros/nuevo?cobro={cobro_id}", status_code=303)


@router.post("/cobros/{cobro_id}/liga-copiada")
def liga_copiada(request: Request, cobro_id: int):
    """El botón «Copiar» avisa aquí. La copia no prueba que se envió, solo que se sacó la liga:
    por eso queda en la auditoría y NO toca `enviado_at`, que significa «el bot lo mandó»."""
    user, _ticket_id, permitido = _cargar_para_accion(request, cobro_id)
    if not user:
        return Response(status_code=401)
    if not permitido:
        return Response(status_code=404)
    log_admin(user, "cobro:liga-copiada", f"#{cobro_id}")
    return Response(status_code=204)
