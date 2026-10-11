"""`nlu.es_informativo` (tarea 14, diseño §5.1 y §8.2): ¿el mensaje trae información (puede ser de
un folio abierto) o es una respuesta corta al flujo?

Falso para las respuestas cortas: sí/no, un número, omitir, menú, solo emojis. Verdadero por tres
vías: (i) largo (≥ `min_palabras`, contadas con `_norm(text).split()`), (ii) `pide_revision`,
(iii) D17: nombra 2+ servicios. Una clave sola («mi CURP es …») no es señal.

Si se pone roja, con la regla del folio prendida (PR4) un «sí» preguntaría «¿es para tu folio?»
(se secuestra la confirmación), o un caso largo pasaría sin que el bot ofrezca ligarlo al folio.
Nada la llama todavía: la conducta del bot no cambia.
"""

import pytest

import app.nlu as nlu
from app.catalog import catalog

LARGO = " ".join(["palabra"] * 40)


@pytest.fixture()
def vivo(catalogo_vivo, monkeypatch):
    monkeypatch.setattr(nlu, "catalog", catalog)
    return catalog


@pytest.mark.parametrize(
    "texto",
    ["sí", "Si", "no", "N", "3", " 12 ", "omitir", "no lo tengo", "menú", "inicio", "👍", "🙏🙏 ", "", "  "],
)
def test_respuestas_cortas_no_son_informativas(texto, vivo):
    assert not nlu.es_informativo(texto)


def test_las_respuestas_cortas_no_cuentan_aunque_el_umbral_sea_uno(vivo):
    """Con `min_palabras=1` cualquier palabra sería «larga»: la exclusión va primero."""
    for texto in ("si", "no", "menu", "omitir", "7", "?? !!", "👍 ..."):
        assert not nlu.es_informativo(texto, min_palabras=1), texto


def test_una_clave_sola_no_es_senal(vivo):
    assert not nlu.es_informativo("mi CURP es GOMA800101HDFRRN09")
    assert not nlu.es_informativo("quiero mi acta de nacimiento")


def test_via_i_largo_es_informativo(vivo):
    assert nlu.es_informativo(LARGO)
    assert not nlu.es_informativo(" ".join(["palabra"] * 39))


def test_via_i_espacios_dobles_y_saltos_no_cuentan_como_palabras(vivo):
    treinta_y_nueve = "palabra  \n\n  " * 39
    assert not nlu.es_informativo(treinta_y_nueve)


def test_via_i_el_umbral_es_parametro(vivo):
    texto = "ya mandé los papeles que me pidieron ayer"
    assert not nlu.es_informativo(texto)
    assert nlu.es_informativo(texto, min_palabras=8)


def test_via_ii_corto_con_frase_de_revision(vivo):
    assert nlu.es_informativo("ya corregí ante RENAPO, qué se puede hacer")


def test_via_iii_corto_con_dos_servicios(vivo):
    """D17: con un folio abierto, dos servicios pueden ser información del folio."""
    assert nlu.es_informativo("Ya quedó lo de CURP; también revisé semanas cotizadas")
    assert nlu.es_informativo("quiero acta y curp")
