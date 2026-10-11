"""`nlu.pide_revision` y `FRASES_REVISION` (tarea 15, diseño §5.2 y §8.2; D5, D9).

El cliente que escribe «tengo una situación con mi pensión, qué se puede hacer» no pide un trámite
del catálogo: pide que un asesor revise su caso. `pide_revision` lo detecta con una lista de frases
en código (nada se aprende solo) y una compuerta: una frase basta solo si el texto NO nombra
exactamente un servicio, o si es largo; sin frases, dos o más servicios en un texto largo (D9).

Si estas pruebas se ponen rojas, un pedido normal («necesito apoyo para descargar mi CURP») se iría
a revisión —el cliente espera a un asesor por un trámite de diez pesos— o una frase que en
producción es el sinónimo de un servicio se comería ese servicio. Nada llama todavía a
`pide_revision` (llega con PR5, detrás de una perilla apagada): la conducta del bot no cambia.
"""

import pytest

import app.nlu as nlu
from app.catalog import SEED_PATH, Catalog, catalog

LARGO = " ".join(["palabra"] * 40)


@pytest.fixture()
def vivo(catalogo_vivo, monkeypatch):
    monkeypatch.setattr(nlu, "catalog", catalog)
    return catalog


def test_una_frase_sin_servicios_pide_revision(vivo):
    assert nlu.pide_revision("tengo una situación, qué se puede hacer?")
    assert nlu.pide_revision("ya corregí ante RENAPO, tengo dos números")


def test_un_pedido_normal_no_pide_revision(vivo):
    for texto in (
        "necesito apoyo para descargar mi CURP",
        "me apoyas para bajar mi curp?",
        "resolución de pensión, ya tengo todos mis documentos",
        "quiero acta y curp",
        "necesito un duplicado de mi acta de nacimiento",
    ):
        assert not nlu.pide_revision(texto), texto


def test_resolucion_sola_no_es_frase_solo_con_una_resolucion(vivo):
    """«Resolución de pensión» es un servicio: la palabra sola no puede mandar a revisión."""
    assert not nlu.pide_revision("necesito la resolucion")
    assert nlu.pide_revision("me llegó con una resolución que no entiendo")


def test_las_frases_se_buscan_por_palabra_completa(vivo):
    """«me revisas» vive dentro de «me revisaste»: por subcadena dispararía (mutación 4)."""
    assert not nlu.pide_revision("ya me revisaste lo que te mandé?")


def test_compuerta_un_servicio_corto_no_pide_revision_aunque_traiga_frase(vivo):
    """D1 reabierta: «me revisas una vinculación de nss y curp» nombra UN servicio, no tres."""
    assert len(nlu.servicios_nombrados("me revisas una vinculacion de nss y curp")) == 1
    assert not nlu.pide_revision("me revisas una vinculacion de nss y curp")
    assert not nlu.pide_revision("me revisas mi acta de nacimiento")


def test_compuerta_un_servicio_largo_con_frase_si_pide_revision(vivo):
    assert nlu.pide_revision(f"me revisas mi acta de nacimiento {LARGO}")


def test_compuerta_dos_servicios_con_frase_pide_revision(vivo):
    assert nlu.pide_revision("me revisas mi acta de nacimiento y mi curp")


def test_d9_dos_servicios_en_texto_largo_sin_frases_pide_revision(vivo):
    assert nlu.pide_revision(f"necesito acta de nacimiento y mi curp {LARGO}")
    assert not nlu.pide_revision("necesito acta de nacimiento y mi curp"), "corto: es un pedido doble"


def test_el_umbral_de_largo_es_parametro(vivo):
    texto = "me revisas mi acta de nacimiento uno dos tres"
    assert not nlu.pide_revision(texto, min_palabras=40)
    assert nlu.pide_revision(texto, min_palabras=5)


def _terminos(catalogo: Catalog) -> set[str]:
    return {
        nlu._norm(t)
        for c in catalogo.categories
        for s in c.services
        for t in (s.name, *s.synonyms)
        if t.strip()
    }


def test_ninguna_frase_es_nombre_ni_sinonimo_de_la_semilla():
    semilla = Catalog(SEED_PATH)
    choques = set(nlu.FRASES_REVISION) & _terminos(semilla)
    assert not choques, f"frases que son trámites en la semilla: {choques}"


def test_ninguna_frase_es_nombre_ni_sinonimo_de_produccion(vivo):
    choques = set(nlu.FRASES_REVISION) & _terminos(vivo)
    assert not choques, f"frases que son trámites en producción: {choques}"


def test_las_frases_estan_normalizadas():
    assert all(f == nlu._norm(f) for f in nlu.FRASES_REVISION)


def test_el_catalogo_normaliza_igual_que_nlu():
    """`catalog._norm_termino` es copia de `nlu._norm` (nlu importa catalog, no al revés). Si se
    separan, una frase que choca con un sinónimo dejaría de excluirse."""
    from app.catalog import _norm_termino

    for texto in ("Acta de Matrimonio", "Corrección de RFC", "  SITUACIÓN  ", "Ñandú", "ünico"):
        assert _norm_termino(texto) == nlu._norm(texto), texto


def test_terminos_vivos_trae_nombres_y_sinonimos_habilitados(vivo):
    terminos = catalog.terminos_vivos()
    assert "acta de matrimonio" in terminos
    assert "curp" in terminos
    assert "nss" in terminos


def test_una_frase_que_el_catalogo_vuelve_sinonimo_se_excluye_al_recargar(vivo):
    """Capa 2 de la colisión: si en /catalog alguien da de alta «controversia» como sinónimo de un
    trámite, esa palabra deja de mandar a revisión en cuanto el catálogo se recarga."""
    texto = "tengo una controversia con mi patron"
    assert nlu.pide_revision(texto)

    servicio = catalog.service_by_id("sb-curp")
    # El volcado de producción no trae precios (no salen del VPS): el panel exige uno al guardar.
    error = catalog.update_service(
        "sb-curp", name=servicio.name, price_info=servicio.price_info or "$10.00",
        synonyms=[*servicio.synonyms, "controversia"],
    )
    assert error is None, error

    assert "controversia" in catalog.terminos_vivos()
    assert not nlu.pide_revision(texto), "la frase siguió valiendo aunque ya es sinónimo de un trámite"
    # Con un solo servicio la compuerta ya lo frenaría; con dos, solo la capa 2 decide: «controversia»
    # ahora es un trámite pedido junto con el acta, no una petición de revisión.
    assert not nlu.pide_revision("tengo una controversia y necesito mi acta de nacimiento")


def test_pide_revision_no_usa_deduce_all_como_contador(vivo, monkeypatch):
    """Mutación 13 del diseño: con `deduce_all`, 1 servicio cuenta como 0 y la compuerta no cierra."""
    monkeypatch.setattr(nlu, "deduce_all", lambda texto: [])
    assert not nlu.pide_revision("me revisas mi acta de nacimiento")
