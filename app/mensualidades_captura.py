"""Captura de los datos de cobro de un titular y generación de sus mensualidades.

Sin rutas ni sesión propia: recibe la sesión del llamador para que la captura y la generación
sean UNA transacción. Todo es ORM (nunca `update()` de Core): el espejo MSSQL solo se entera de
lo que pasa por los listeners.

Lo que NO hace: no registra pagos, no da de baja y no toca a las personas. Cambiar importe, día o
primer pago afecta solo mensualidades futuras sin pagar; las pagadas y las ya vencidas se conservan.
"""

from datetime import date
from decimal import Decimal, InvalidOperation

from sqlmodel import Session, select

from app import mensualidades as dom
from app.models import ClienteMensualidad, CuentaCobranza, Mensualidad, MensualidadAsesor, User
from app.utils import normalize_phone

IMPORTE_MAXIMO_CENTAVOS = 10_000_000_00  # $10 millones: más es un dedazo, no una mensualidad


class DatoInvalido(ValueError):
    """Un campo mal capturado. `codigo` viaja en la URL; el texto sale de `MENSAJES`, nunca de la
    URL: así la pantalla no puede mostrar un mensaje armado por quien teclea el enlace."""

    def __init__(self, codigo: str):
        super().__init__(MENSAJES[codigo])
        self.codigo = codigo


MENSAJES = {
    "importe": "El importe debe ser un número mayor que cero, menor a 10 millones y con máximo dos decimales.",
    "dia": "El día de pago debe ser un número del 1 al 31.",
    "fecha": "La fecha del primer pago no es válida.",
    "whatsapp": "El WhatsApp no parece un número válido.",
    "responsable": "El responsable debe ser un usuario activo de la oficina.",
    "permiso": "Solo un administrador puede capturar los datos de cobro.",
}


def parse_importe(texto: str) -> int:
    """'1,250.50' o '$1250' -> centavos. Positivo y con tope."""
    limpio = texto.replace("$", "").replace(",", "").strip()
    try:
        pesos = Decimal(limpio)
    except InvalidOperation:
        raise DatoInvalido("importe") from None
    centavos = pesos * 100
    if not pesos.is_finite() or centavos != centavos.to_integral_value():
        raise DatoInvalido("importe")
    centavos = int(centavos)
    if not 0 < centavos <= IMPORTE_MAXIMO_CENTAVOS:
        raise DatoInvalido("importe")
    return centavos


def parse_dia(texto: str) -> int:
    try:
        dia = int(texto.strip())
    except ValueError:
        raise DatoInvalido("dia") from None
    if not 1 <= dia <= 31:
        raise DatoInvalido("dia")
    return dia


def parse_fecha(texto: str) -> date:
    try:
        return date.fromisoformat(texto.strip())
    except ValueError:
        raise DatoInvalido("fecha") from None


def parse_whatsapp(texto: str) -> str:
    numero = normalize_phone(texto)
    if not numero:
        raise DatoInvalido("whatsapp")
    return numero


def generar(session: Session, cuenta: CuentaCobranza, hoy: date) -> int:
    """Deja las mensualidades de la cuenta al día; devuelve cuántas creó. Idempotente.

    Sin importe, día, primer pago o sin personas vivas no genera nada. Las futuras sin pagar se
    ajustan al importe y día actuales; las que ya no corresponden (el primer pago se movió
    adelante) se apagan. Pagadas y vencidas no se tocan.
    """
    vivas = session.exec(
        select(ClienteMensualidad.id)
        .where(ClienteMensualidad.cuenta_id == cuenta.id)
        .where(ClienteMensualidad.habilitado == True)  # noqa: E712
        .where(ClienteMensualidad.baja_at == None)  # noqa: E711
    ).first()
    if vivas is None or None in (cuenta.importe_centavos, cuenta.dia_pago, cuenta.primer_pago):
        return 0

    deben = dict(dom.periodos_a_generar(cuenta.primer_pago, cuenta.dia_pago, hoy))
    existentes = {
        m.periodo: m
        for m in session.exec(
            select(Mensualidad)
            .where(Mensualidad.cuenta_id == cuenta.id)
            .where(Mensualidad.habilitado == True)  # noqa: E712
        ).all()
    }
    creadas = 0
    for periodo, vence in deben.items():
        m = existentes.get(periodo)
        if m is None:
            session.add(
                Mensualidad(
                    cuenta_id=cuenta.id,
                    periodo=periodo,
                    vence_el=vence,
                    importe_centavos=cuenta.importe_centavos,
                )
            )
            creadas += 1
        elif m.pagado_at is None and m.vence_el >= hoy:
            if (m.importe_centavos, m.vence_el) != (cuenta.importe_centavos, vence):
                m.importe_centavos, m.vence_el = cuenta.importe_centavos, vence
                session.add(m)
    for periodo, m in existentes.items():
        if periodo not in deben and m.pagado_at is None and m.vence_el >= hoy:
            m.habilitado = False
            session.add(m)
    return creadas


def asignar_responsable(session: Session, cuenta: CuentaCobranza, username: str, por: str) -> None:
    """Deja a `username` como el único responsable vivo de la cuenta.

    El responsable anterior se da de baja del puente (su historial no se borra). Si el nuevo ya
    era colaborador, ESA fila sube a responsable: un par vivo es único. Se hace flush entre una
    baja y un alta porque los índices parciales se evalúan fila a fila.
    """
    filas = session.exec(
        select(MensualidadAsesor)
        .where(MensualidadAsesor.cuenta_id == cuenta.id)
        .where(MensualidadAsesor.habilitado == True)  # noqa: E712
    ).all()
    actual = next((f for f in filas if f.rol == "responsable"), None)
    if actual is not None and actual.username == username:
        return
    if actual is not None:
        actual.habilitado = False
        session.add(actual)
        session.flush()
    propia = next((f for f in filas if f.username == username), None)
    if propia is not None:
        propia.rol = "responsable"
        session.add(propia)
    else:
        session.add(
            MensualidadAsesor(
                cuenta_id=cuenta.id, username=username, rol="responsable", created_by=por
            )
        )
    session.flush()


def usuario_de_la_oficina(session: Session, username: str, office_id: int) -> bool:
    """El responsable debe ser alguien habilitado de la oficina de la cuenta."""
    u = session.exec(select(User).where(User.username == username)).first()
    return bool(u and u.habilitado and u.office_id == office_id)
