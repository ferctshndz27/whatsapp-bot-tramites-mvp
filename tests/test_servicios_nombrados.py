"""`nlu.servicios_nombrados`: el contador de servicios que nombra un texto (tarea 13, diseño §5.2).

`deduce_all` no sirve de contador: devuelve `[]` tanto para 0 como para 1 servicio, así que una
compuerta «0 o ≥ 2» montada sobre él nunca cierra (mutación 13 del diseño). `servicios_nombrados`
es la misma búsqueda literal —con la regla de la mención más larga de PR9— sin el filtro `≥ 2` ni
el recorte a `MULTI_MAX_SERVICES`, y `deduce_all` queda escrita encima.

La caracterización (`tests/datos/caracterizacion_deduce_all.json`) es la salida de `deduce_all` en
`main` antes de este cambio, sobre el set dorado, todos los nombres y sinónimos de la semilla y del
volcado de producción, y combinaciones de dos términos. Si se pone roja, el cambio movió qué
trámites se le confirman a un cliente real.
"""

import json
from pathlib import Path

import pytest

import app.nlu as nlu
from app.catalog import SEED_PATH, Catalog

CARACTERIZACION = json.loads(
    (Path(__file__).parent / "datos" / "caracterizacion_deduce_all.json").read_text(encoding="utf-8")
)


def _ids(servicios) -> list[str]:
    return [s.id for s in servicios]


@pytest.fixture()
def semilla(monkeypatch):
    nuevo = Catalog(SEED_PATH)
    monkeypatch.setattr(nlu, "catalog", nuevo)
    nuevo.categories
    return nuevo


@pytest.fixture()
def vivo(catalogo_vivo, monkeypatch):
    from app.catalog import catalog

    monkeypatch.setattr(nlu, "catalog", catalog)
    return catalog


@pytest.mark.parametrize("nombre", ["semilla", "vivo"])
def test_deduce_all_no_cambia_sobre_la_caracterizacion(nombre, request):
    request.getfixturevalue(nombre)
    esperado = CARACTERIZACION[nombre]

    distintos = {t: (_ids(nlu.deduce_all(t)), e) for t, e in esperado.items() if _ids(nlu.deduce_all(t)) != e}

    assert not distintos, f"{len(distintos)} textos cambiaron (actual, antes): {list(distintos.items())[:5]}"
    assert len(esperado) > 200, "la caracterización se quedó corta"


def test_servicios_nombrados_cuenta_cero_uno_dos_y_cinco(vivo):
    assert nlu.servicios_nombrados("hola buenas tardes") == []
    assert _ids(nlu.servicios_nombrados("quiero mi acta de nacimiento")) == ["sb-acta-nacimiento"]
    assert sorted(_ids(nlu.servicios_nombrados("quiero acta y curp"))) == ["sb-acta-nacimiento", "sb-curp"]
    cinco = "necesito acta de matrimonio, mi curp, recibo cfe, nss en pdf y semanas cotizadas"
    assert len(nlu.servicios_nombrados(cinco)) == 5


def test_con_un_servicio_deduce_all_sigue_vacio_y_servicios_nombrados_no(vivo):
    """La diferencia que justifica la función: 1 servicio es 1, no 0."""
    texto = "me revisas mi acta de nacimiento"
    assert nlu.deduce_all(texto) == []
    assert len(nlu.servicios_nombrados(texto)) == 1


def test_servicios_nombrados_no_recorta_al_tope_de_deduce_all(vivo):
    seis = "acta de matrimonio, curp, recibo cfe, nss en pdf, semanas cotizadas y acta de divorcio"
    assert len(nlu.servicios_nombrados(seis)) == 6
    assert len(nlu.deduce_all(seis)) == nlu.MULTI_MAX_SERVICES


def test_servicios_nombrados_respeta_la_mencion_mas_larga(vivo):
    """La regla de PR9 vale también para el contador: «Acta de matrimonio» es UN servicio."""
    assert _ids(nlu.servicios_nombrados("Acta de matrimonio")) == ["sb-acta-matrimonio"]
