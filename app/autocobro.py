"""Autocobro, F3: la liga con token y el alta de la tarjeta (spec autocobranza §3, §6).

Este módulo es la REGLA: crear y validar ligas, armar el resumen que ve el cliente y cerrar el alta con
lo que Stripe confirmó. No habla con Stripe ni con FastAPI (eso es `app/autocobro_publico.py`), y
ninguna de sus funciones abre una sesión: reciben la del llamador, que NO debe sostenerla durante una
llamada de red (SQLite tiene un solo escritor).

Seguridad:
- El token (256 bits) solo existe en la liga que se manda al cliente; en la base vive su SHA-256.
- Nunca hay PAN ni CVV: la tarjeta vive en Stripe (token pm_). Aquí solo marca, últimos 4 y vencimiento.
- Lo que el cliente aceptó (texto del mandato y su hash) se recalcula al cerrar el alta y debe coincidir
  con lo que se le mostró: si el plan cambió a medias, no se registra nada.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum

from sqlmodel import Session, select

from app import autocobro_terminos as terminos
from app.config import settings
from app.dinero import formato_monto
from app.models import Client, ConsentimientoCobro, LigaAutocobro, Office, PaymentMethod, PlanCobro, utcnow

TTL_LIGA = timedelta(hours=72)
MAX_APERTURAS = 40  # tope contra fuerza bruta y contra una liga reenviada a medio mundo
ESTADOS_PLAN_CON_LIGA = ("borrador", "esperando_cliente")
PERIODICIDADES = ("mensual", "quincenal")
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


class EstadoLiga(str, Enum):
    OK = "ok"
    NO_EXISTE = "no_existe"
    VENCIDA = "vencida"
    USADA = "usada"
    INACTIVA = "inactiva"  # liga reemplazada, plan cancelado o plan que ya no espera al cliente
    DEMASIADAS = "demasiadas"


class AltaInvalida(Exception):
    """El SetupIntent no sirve para dar de alta el cobro; el mensaje es para el log, no para el cliente."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _naive_utc(dt: datetime) -> datetime:
    """SQLite devuelve datetimes sin zona; en la app siempre son UTC."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _ahora() -> datetime:
    return _naive_utc(utcnow())


def plan_completo(plan: PlanCobro) -> str | None:
    """Por qué el plan no puede llevar liga todavía (None = está completo)."""
    if plan.estado not in ESTADOS_PLAN_CON_LIGA:
        return f"el plan está «{plan.estado}»"
    if not plan.monto_centavos or plan.monto_centavos <= 0:
        return "falta el monto por cargo"
    if not plan.monto_total_centavos or plan.monto_total_centavos < plan.monto_centavos:
        return "falta el monto total (debe cubrir al menos un cargo)"
    if plan.periodicidad not in PERIODICIDADES:
        return "periodicidad no soportada"
    if plan.dia_cobro is None or not 1 <= plan.dia_cobro <= 28:
        return "el día de cobro debe estar entre 1 y 28"
    if not plan.concepto.strip():
        return "falta el concepto"
    return None


def crear_liga(session: Session, plan: PlanCobro, creado_por: str) -> tuple[LigaAutocobro, str]:
    """Genera la liga y devuelve (fila, TOKEN). El token no se vuelve a poder leer: solo se manda al cliente."""
    motivo = plan_completo(plan)
    if motivo:
        raise ValueError(motivo)
    # Una sola liga viva por plan: reenviar invalida la anterior.
    for vieja in session.exec(
        select(LigaAutocobro).where(LigaAutocobro.plan_id == plan.id, LigaAutocobro.habilitado == True)  # noqa: E712
    ):
        vieja.habilitado = False
        session.add(vieja)
    token = secrets.token_urlsafe(32)
    liga = LigaAutocobro(
        plan_id=plan.id, token_hash=hash_token(token), expira_at=_ahora() + TTL_LIGA, creada_por=creado_por,
    )
    plan.estado = "esperando_cliente"
    session.add(liga)
    session.add(plan)
    session.flush()
    return liga, token


def liga_por_token(session: Session, token: str) -> tuple[EstadoLiga, LigaAutocobro | None, PlanCobro | None]:
    """Estado de la liga SIN contar la apertura. La búsqueda es por hash: el token no se compara en claro."""
    if not token or len(token) > 100:
        return EstadoLiga.NO_EXISTE, None, None
    liga = session.exec(select(LigaAutocobro).where(LigaAutocobro.token_hash == hash_token(token))).first()
    if liga is None:
        return EstadoLiga.NO_EXISTE, None, None
    plan = session.get(PlanCobro, liga.plan_id)
    if plan is None:
        return EstadoLiga.NO_EXISTE, None, None
    if liga.usada_at is not None:
        return EstadoLiga.USADA, liga, plan
    if not liga.habilitado or plan.estado != "esperando_cliente":
        return EstadoLiga.INACTIVA, liga, plan
    if _naive_utc(liga.expira_at) <= _ahora():
        return EstadoLiga.VENCIDA, liga, plan
    if liga.intentos_abiertos > MAX_APERTURAS:
        return EstadoLiga.DEMASIADAS, liga, plan
    return EstadoLiga.OK, liga, plan


def abrir_liga(session: Session, token: str) -> tuple[EstadoLiga, LigaAutocobro | None, PlanCobro | None]:
    """Como `liga_por_token`, pero una liga vigente cuenta la apertura."""
    estado, liga, plan = liga_por_token(session, token)
    if estado is EstadoLiga.OK:
        liga.intentos_abiertos += 1
        session.add(liga)
        session.flush()
    return estado, liga, plan


# ---------------------------------------------------------------------------
# Resumen y mandato: lo que el cliente lee es lo que se guarda
# ---------------------------------------------------------------------------


def proveedor_de(session: Session, plan: PlanCobro) -> str:
    cliente = session.get(Client, plan.phone)
    oficina = session.get(Office, cliente.office_id) if cliente else None
    return oficina.nombre if oficina else "tu asesor"


@dataclass(frozen=True)
class Resumen:
    proveedor: str
    concepto: str
    monto_cargo: str
    monto_total: str
    cargos: int
    periodicidad: str
    dia: int
    fecha_inicio: date | None
    version: int
    mandato: str
    hash_mandato: str


def resumen(session: Session, plan: PlanCobro) -> Resumen:
    proveedor = proveedor_de(session, plan)
    mandato = terminos.texto_mandato(
        proveedor=proveedor, monto_cargo_centavos=plan.monto_centavos, periodicidad=plan.periodicidad,
        dia=plan.dia_cobro, concepto=plan.concepto, monto_total_centavos=plan.monto_total_centavos,
    )
    return Resumen(
        proveedor=proveedor, concepto=plan.concepto, monto_cargo=formato_monto(plan.monto_centavos),
        monto_total=formato_monto(plan.monto_total_centavos),
        cargos=math.ceil(plan.monto_total_centavos / plan.monto_centavos),
        periodicidad=plan.periodicidad, dia=plan.dia_cobro, fecha_inicio=plan.fecha_inicio,
        version=terminos.VIGENTE, mandato=mandato, hash_mandato=terminos.hash_mandato(mandato),
    )


def validar_datos(nombre: str, email: str) -> str | None:
    """Mensaje de error para el cliente, o None si los datos sirven."""
    if not 2 <= len(nombre.strip()) <= 120:
        return "Escribe tu nombre completo."
    if not _EMAIL.match(email.strip()):
        return "Escribe un correo válido."
    return None


def hash_ip(ip: str | None) -> str | None:
    """HMAC de la IP: dato personal que solo hace falta COMPARAR, no leer."""
    if not ip:
        return None
    return hmac.new(settings.session_secret.encode(), ip.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Límite por IP (en memoria, por proceso: basta contra un script, no es antifraude)
# ---------------------------------------------------------------------------

_VENTANA_S = 600
_MAX_POR_VENTANA = 20
_golpes: dict[str, deque] = defaultdict(deque)


def limite_excedido(ip: str | None) -> bool:
    """Registra un golpe y dice si la IP ya pasó el tope en la ventana."""
    clave = ip or "?"
    ahora = time.monotonic()
    fila = _golpes[clave]
    while fila and ahora - fila[0] > _VENTANA_S:
        fila.popleft()
    if len(fila) >= _MAX_POR_VENTANA:
        return True
    fila.append(ahora)
    return False


def limpiar_limite() -> None:
    _golpes.clear()


# ---------------------------------------------------------------------------
# Cierre del alta
# ---------------------------------------------------------------------------


def completar_alta(
    session: Session, liga: LigaAutocobro, plan: PlanCobro, si: dict, *, ip: str | None, user_agent: str | None,
) -> ConsentimientoCobro:
    """Cierra el alta con un SetupIntent que Stripe confirmó (objeto leído del servidor, nunca del navegador).

    Idempotente: el mismo SetupIntent no crea un segundo consentimiento. Levanta `AltaInvalida` si algo
    no cuadra; entonces no se escribe nada.
    """
    si_id = si.get("id") or ""
    previo = session.exec(
        select(ConsentimientoCobro).where(ConsentimientoCobro.stripe_setup_intent_id == si_id)
    ).first()
    if previo is not None:
        return previo

    meta = si.get("metadata") or {}
    if si.get("status") != "succeeded":
        raise AltaInvalida(f"SetupIntent {si_id} en estado {si.get('status')}")
    if meta.get("liga_id") != str(liga.id) or meta.get("plan_id") != str(plan.id):
        raise AltaInvalida("el SetupIntent no pertenece a esta liga")
    if not plan.stripe_customer_id or si.get("customer") != plan.stripe_customer_id:
        raise AltaInvalida("el SetupIntent es de otro customer")
    pm = si.get("payment_method")
    tarjeta = (pm or {}).get("card") if isinstance(pm, dict) else None
    if not tarjeta or not pm.get("id", "").startswith("pm_"):
        raise AltaInvalida("el SetupIntent no trae una tarjeta")

    vista = resumen(session, plan)
    if meta.get("mandato_hash") != vista.hash_mandato:
        raise AltaInvalida("el plan cambió después de mostrar el mandato")

    # Solo se llenan los datos del cliente que estén vacíos: un formulario público no pisa la ficha.
    cliente = session.get(Client, plan.phone)
    nombre, email = (meta.get("nombre") or "").strip() or None, (meta.get("email") or "").strip().lower() or None
    if cliente is None:
        cliente = Client(phone=plan.phone, name=nombre, email=email, origen="autocobro")
    else:
        cliente.name = cliente.name or nombre
        cliente.email = cliente.email or email
    session.add(cliente)

    metodo = session.exec(
        select(PaymentMethod).where(PaymentMethod.stripe_payment_method_id == pm["id"])
    ).first()
    if metodo is None:
        metodo = PaymentMethod(phone=plan.phone, stripe_customer_id=plan.stripe_customer_id,
                               stripe_payment_method_id=pm["id"])
    metodo.marca = tarjeta.get("brand")
    metodo.ultimos4 = tarjeta.get("last4")
    metodo.exp_mes = tarjeta.get("exp_month")
    metodo.exp_anio = tarjeta.get("exp_year")
    metodo.funding = tarjeta.get("funding")
    metodo.predeterminado = True
    metodo.autorizado_por = nombre or plan.phone
    metodo.autorizado_at = utcnow()
    session.add(metodo)
    session.flush()

    consentimiento = ConsentimientoCobro(
        plan_id=plan.id, phone=plan.phone, version_terminos=str(vista.version),
        hash_texto=terminos.hash_texto(vista.version), texto_mandato=vista.mandato,
        tope_cargo_centavos=plan.monto_centavos, monto_total_centavos=plan.monto_total_centavos,
        periodicidad=plan.periodicidad, dia_cobro=plan.dia_cobro, ip_hash=hash_ip(ip),
        user_agent=(user_agent or "")[:300] or None, stripe_setup_intent_id=si_id,
        stripe_mandate_id=si.get("mandate") if isinstance(si.get("mandate"), str) else None,
    )
    session.add(consentimiento)
    session.flush()

    plan.paymentmethod_id = metodo.id
    plan.consentimiento_id = consentimiento.id
    plan.saldo_pendiente_centavos = plan.monto_total_centavos
    plan.cargos_previstos = vista.cargos
    plan.estado = "activo"
    session.add(plan)
    liga.usada_at = utcnow()
    session.add(liga)
    session.flush()
    return consentimiento
