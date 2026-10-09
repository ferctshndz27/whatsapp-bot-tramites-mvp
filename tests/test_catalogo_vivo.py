"""El catálogo de producción —volcado de solo lectura del VPS— como dato de prueba.

El NLU literal decide con los nombres y sinónimos del catálogo VIVO, que el panel
edita sin pasar por el repo. El 2026-09-24 el bot de producción troceó en tres
trámites el caso de un cliente porque «NSS en PDF» trae ahí el sinónimo "nss", que
la semilla (`app/catalog.json`) no tiene: medida contra la semilla, la misma frase
nombra dos, y una prueba habría aprobado una conducta que producción no tiene. Aquí
se fija que el volcado entra completo a la base de la prueba, que el `catalog` del
bot lo lee y que reproduce lo que vio producción (diseño §2.7 c).

El fixture `catalogo_vivo` vive en conftest.py porque también lo usan el set dorado
y las fases del plan «casos para análisis». Si al refrescar el volcado una de estas
pruebas cambia, lo que cambió es el catálogo de producción, no el código: se dice
en el PR.
"""

import app.nlu as nlu
from app.catalog import catalog

# El caso del diseño §1 con claves y nombre sintéticos: las CURP son las del diseño
# y los NSS están inventados (11 dígitos con espacios, como los escribió el
# cliente). El original tenía 929 caracteres; este conserva lo que lee la capa
# literal.
CASO_2026_09_24 = (
    "Se solicita el apoyo para el C. <NOMBRE DEL TRABAJADOR>. El trabajador presenta la "
    "siguiente situación: CURP cancelada: AAAA000101HDFBBB01 asociada al NSS 12 34567890 1, "
    "el cual cuenta con 694 semanas cotizadas. CURP oficial vigente: AAAA000101HDFBBB09 "
    "asociada al NSS 98 76543210 0, el cual cuenta con 0 semanas cotizadas. El trabajador ya "
    "realizó la corrección ante RENAPO...\n"
    "Por lo anterior se solicita:\n"
    "1. La unificación de ambos Números de Seguridad Social, dejando como NSS único el más "
    "antiguo...\n"
    "2. Que dicho NSS quede vinculado a la CURP oficial y vigente...\n"
    "3. La cancelación definitiva en sistema del NSS ... con 0 semanas."
)


def test_el_bot_lee_el_catalogo_de_produccion_y_no_la_semilla(catalogo_vivo):
    """Las pruebas que pasan por handle_incoming() leen el singleton `catalog`, no el
    volcado. Si el singleton siguiera con la semilla, el set dorado mediría un
    catálogo que ningún cliente ve."""
    vivos = [s["id"] for c in catalogo_vivo["categories"] for s in c["services"]]
    del_bot = [s.id for c in catalog.categories for s in c.services]
    assert del_bot == vivos, "el catálogo que lee el bot no es el de producción"


def test_el_caso_real_nombra_los_tres_tramites_que_anoto_produccion(catalogo_vivo):
    """El 2026-09-24 el bot anotó «Descarga de CURP», «Semanas cotizadas» y «NSS en
    PDF» sobre un texto que era información para el folio #63. Es la línea base que
    corrige la regla del folio: si el volcado deja de dar estos tres, la regla se
    estaría probando contra otro problema que el que vio el cliente."""
    ids = [s.id for s in nlu.deduce_all(CASO_2026_09_24)]
    assert ids == ["sb-curp", "con-semanas-cotizadas", "con-nss-pdf"], (
        f"el volcado ya no reproduce el troceo de producción (diseño §2.7 c): {ids}"
    )
