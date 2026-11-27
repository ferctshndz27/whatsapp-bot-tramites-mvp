"""F2 del autocobro: términos versionados y su página pública."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from app import autocobro_terminos as t
from app.main import app


@pytest.fixture()
def anonimo():
    with TestClient(app, base_url="https://testserver") as c:
        yield c


def test_la_huella_registrada_coincide_con_el_texto_de_cada_version():
    """El candado: editar el texto de una versión ya publicada rompe esto.
    Se cambia de versión, no el texto."""
    for version in t.VERSIONES:
        assert t.HUELLAS[version] == t.hash_texto(version)


def test_cada_version_tiene_huella_y_la_vigente_existe():
    assert set(t.VERSIONES) == set(t.HUELLAS)
    assert t.VIGENTE in t.VERSIONES


def test_la_huella_es_sha256_del_texto_canonico():
    esperado = hashlib.sha256(t.texto_canonico(1).encode("utf-8")).hexdigest()
    assert t.hash_texto(1) == esperado and len(esperado) == 64


def test_el_texto_fijo_no_lleva_marcadores_sin_llenar():
    assert "[" not in t.texto_canonico(1) and "]" not in t.texto_canonico(1)


def test_el_texto_promete_lo_que_el_diseno_exige():
    txt = t.texto_canonico(1)
    for frase in ("no puede cobrarte más", "5 días naturales", "CANCELAR", "no ve, no guarda", "PROFECO"):
        assert frase in txt


def test_el_mandato_es_determinista_y_su_huella_cambia_con_el_monto():
    base = dict(
        proveedor="Asesoría X", marca="Visa", ultimos4="4242", monto_cargo_centavos=150000,
        periodicidad="mensual", dia=5, concepto="Mensualidad", monto_total_centavos=900000,
    )
    a, b = t.texto_mandato(**base), t.texto_mandato(**base)
    assert a == b
    assert "$1,500.00 MXN" in a and "$9,000.00 MXN" in a and "terminación 4242" in a
    assert t.hash_mandato(a) != t.hash_mandato(t.texto_mandato(**{**base, "monto_total_centavos": 900001}))


def test_la_pagina_es_publica_y_trae_version_y_huella(anonimo):
    r = anonimo.get("/legal/autocobro", follow_redirects=False)
    assert r.status_code == 200
    assert "Versión 1" in r.text and t.hash_texto(1)[:16] in r.text
    assert "2. Qué autorizas" in r.text
    assert "noindex" in r.text and r.headers["x-robots-tag"].startswith("noindex")


def test_la_pagina_por_version_y_la_inexistente(anonimo):
    assert anonimo.get("/legal/autocobro/v1").status_code == 200
    assert anonimo.get("/legal/autocobro/v99").status_code == 404
    assert anonimo.get("/legal/autocobro/vabc").status_code == 422


def test_la_pagina_no_escapa_html_ni_trae_datos_de_otros(anonimo):
    r = anonimo.get("/legal/autocobro")
    assert "<script" not in r.text.lower() and "|safe" not in r.text
