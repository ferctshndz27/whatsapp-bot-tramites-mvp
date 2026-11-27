"""Términos de la autorización de cobro recurrente (autocobro), por versión.

Cada versión del texto es INMUTABLE: el consentimiento del cliente guarda el
número de versión y la huella (`hash_texto`) de lo que leyó. Si el texto
cambiara sin cambiar de versión, esa huella dejaría de probar nada.

Para publicar una versión nueva: agrega `_V2` abajo, súbela a `VERSIONES` y a
`VIGENTE`, y registra su huella en `HUELLAS`. Nunca edites una versión ya
publicada: `tests/test_autocobro_terminos.py` falla si cambia el texto de una
versión sin cambiar su huella registrada.

El texto fijo no lleva datos del plan ni de la oficina: esos viajan en el
resumen que ve el cliente y en el mandato (`texto_mandato`), que también se
congela con su propia huella en `ConsentimientoCobro`.

BORRADOR: lo revisa un abogado antes de mostrarse a clientes reales
(02-DOCS/wiki/sdd/specs/autocobranza-terminos-v1.md).
"""

from __future__ import annotations

import hashlib

from app.dinero import formato_monto

# (título, [párrafos]). Un párrafo que empieza con «- » se pinta como viñeta.
Seccion = tuple[str, tuple[str, ...]]

_V1: tuple[Seccion, ...] = (
    ("1. Quiénes somos", (
        "El Proveedor es la empresa que aparece en el resumen de tu liga de autorización, con sus datos de "
        "contacto y los de tu asesor.",
    )),
    ("2. Qué autorizas", (
        "Autorizas al Proveedor a iniciar cargos a la tarjeta que registraste, sin que tengas que estar presente "
        "en cada cobro, por el concepto, monto, periodicidad y día indicados en el resumen. Este es un acuerdo "
        "que cubre un monto determinado: el Proveedor no puede cobrarte más de lo que aquí aceptaste.",
    )),
    ("3. Hasta cuándo se cobra", (
        "Los cargos se detienen cuando la suma de lo cobrado alcance el monto total acordado, cuando canceles, o "
        "cuando el Proveedor y tú acuerden por escrito otra cosa. Al terminar recibirás un mensaje de "
        "confirmación y la autorización queda cerrada.",
    )),
    ("4. Cambios", (
        "- Tu asesor puede pausar un cobro, bajar el monto de un cargo o mover el día de cobro. Recibirás un "
        "aviso por WhatsApp con al menos 5 días naturales de anticipación cuando el cambio afecte una fecha.",
        "- Si el cambio implica cobrar más por cargo, un monto total mayor o cargos más frecuentes, no se aplica "
        "hasta que lo aceptes de nuevo mediante una liga nueva.",
        "- Un cambio de tarjeta también requiere tu aceptación.",
    )),
    ("5. Aviso previo", (
        "Te avisaremos por WhatsApp al menos 5 días naturales antes de cada cargo, con el monto y la fecha. En "
        "ese aviso incluimos la liga para cancelar.",
    )),
    ("6. Tu tarjeta y tus datos", (
        "- Tu número de tarjeta y tu código de seguridad se capturan directamente en el sistema de nuestro "
        "procesador de pagos, Stripe, que cumple con PCI DSS. El Proveedor no ve, no guarda y no puede consultar "
        "esos datos.",
        "- El Proveedor conserva únicamente un identificador de la tarjeta (token), la marca, los últimos 4 "
        "dígitos y la fecha de vencimiento.",
        "- Tu nombre, correo y teléfono se usan para identificarte, enviarte avisos y comprobantes, y dar "
        "seguimiento a tu trámite, conforme al Aviso de Privacidad que se enlaza en tu liga. Puedes ejercer tus "
        "derechos de acceso, rectificación, cancelación y oposición con el Proveedor.",
        "- Si ya eres cliente, tus datos se vinculan a tu registro existente; no se duplican.",
    )),
    ("7. Cómo cancelar", (
        "- Desde la liga de baja que va en cada aviso, o respondiendo «CANCELAR» en WhatsApp. La cancelación se "
        "aplica de inmediato y no tiene costo ni penalización.",
        "- También puedes pedir a tu banco que cancele los cargos recurrentes.",
        "- Cancelar la autorización no cancela lo que ya debes por servicios prestados. El saldo pendiente se "
        "arreglará directamente con tu asesor por otro medio de pago.",
    )),
    ("8. Si un cobro no pasa", (
        "Si el cargo es rechazado (fondos insuficientes, tarjeta vencida o bloqueada), te avisaremos y lo "
        "reintentaremos hasta 2 veces más, en un plazo de 7 días. Si sigue sin pasar, se pausan los cargos y tu "
        "asesor te contactará. Tu banco puede aplicarte cargos por sus propias reglas; el Proveedor no cobra "
        "comisión por rechazo.",
    )),
    ("9. Aclaraciones y contracargos", (
        "Si ves un cargo que no reconoces, escríbenos primero por los medios de contacto de tu resumen. "
        "Conservas tu derecho de objetar el cargo con tu banco. Guardamos la constancia de tu aceptación (fecha, "
        "hora, versión de estos términos y datos técnicos de la conexión) para atender cualquier aclaración.",
    )),
    ("10. Comprobantes", (
        "Cada cobro exitoso genera un comprobante. Si necesitas factura, solicítala dentro del mes del cargo, "
        "con tus datos fiscales.",
    )),
    ("11. Vigencia y cambios a estos términos", (
        "Estos términos aplican desde que aceptas y hasta que termine o se cancele la autorización. Si cambiamos "
        "el texto, la versión nueva solo aplica a autorizaciones futuras o a las que tú aceptes de nuevo. Tu "
        "autorización queda ligada a la versión y a la huella de texto que se muestran en esta página.",
    )),
    ("12. Ley y jurisdicción", (
        "Estos términos se rigen por las leyes de los Estados Unidos Mexicanos. Para cualquier queja puedes "
        "acudir a la Procuraduría Federal del Consumidor (PROFECO), sin perjuicio de tus derechos como "
        "consumidor.",
    )),
)

VERSIONES: dict[int, tuple[Seccion, ...]] = {1: _V1}
VIGENTE = 1

# Huella registrada de cada versión publicada. Se escribe A MANO: es el candado
# que impide editar el texto de una versión que ya firmó alguien.
HUELLAS: dict[int, str] = {
    1: "216f99a94ba4f01e93995da8a5fa05ce2f63c35be4ae52945796c1d586f55458",
}


def texto_canonico(version: int) -> str:
    """El texto de la versión como un solo string estable (lo que se hashea)."""
    secciones = VERSIONES[version]
    return "\n\n".join(f"{titulo}\n" + "\n".join(parrafos) for titulo, parrafos in secciones)


def hash_texto(version: int) -> str:
    return hashlib.sha256(texto_canonico(version).encode("utf-8")).hexdigest()


def secciones(version: int) -> tuple[Seccion, ...]:
    return VERSIONES[version]


_PERIODOS = {"mensual": "mensual", "quincenal": "quincenal"}


def texto_mandato(
    *,
    proveedor: str,
    marca: str | None = None,
    ultimos4: str | None = None,
    monto_cargo_centavos: int,
    periodicidad: str,
    dia: int,
    concepto: str,
    monto_total_centavos: int,
) -> str:
    """Texto exacto del mandato que se guarda con cada aceptación.

    Lo que el cliente lee en pantalla y lo que se guarda son el MISMO string:
    la huella (`hash_mandato`) se calcula sobre este resultado.
    """
    # La liga muestra el mandato ANTES de que exista la tarjeta: sin marca ni últimos 4 habla de «la
    # tarjeta que registro en este formulario». Lo que se guarda es exactamente lo que se mostró.
    tarjeta = f"la tarjeta {marca} terminación {ultimos4}" if marca and ultimos4 else "la tarjeta que registro en este formulario"
    return (
        f"Autorizo a {proveedor} a cobrar a {tarjeta} cargos de hasta "
        f"{formato_monto(monto_cargo_centavos)} con periodicidad {_PERIODOS.get(periodicidad, periodicidad)} "
        f"el día {dia}, por el concepto {concepto}, hasta cubrir {formato_monto(monto_total_centavos)} en "
        "total. Puedo cancelar esta autorización en cualquier momento."
    )


def hash_mandato(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()
