"""Guardas de entrada (app/guardas_http.py) y comodines del buscador de /cobros/nuevo.

Lo que se defiende: un POST del panel que viene de otro sitio no llega al handler aunque la
cookie viaje, un cuerpo enorme no se lee, y «%»/«_» del buscador son texto y no comodines.
"""

import pytest

from app.guardas_http import MAX_CUERPO_BYTES, cookie_para_marco, origen_permitido
from tests.test_cobranza_panel import ASESOR, _cliente, entorno, stripe_doble  # noqa: F401
from tests.test_cobro_manual import PHONE, _ficha_canonica  # noqa: F401

BASE = "https://panel.example"


@pytest.mark.parametrize("origin,host,esperado", [
    (None, "panel.example", True),                       # curl, webhooks, formulario sin Origin
    ("https://panel.example", "panel.example", True),    # el propio panel
    ("https://panel.example", "interno:8000", True),     # detrás del proxy: vale PUBLIC_BASE_URL
    ("https://evil.example", "panel.example", False),
    ("https://panel.example.evil.example", "panel.example", False),
    ("null", "panel.example", False),
    ("chrome-extension://abc", "panel.example", False),
])
def test_origen_permitido(origin, host, esperado):
    assert origen_permitido(origin, host, BASE) is esperado


def test_post_con_origen_ajeno_no_genera_cobro(entorno, stripe_doble):
    c = _cliente(ASESOR)
    r = c.post("/cobros/nuevo", data={"phone": PHONE, "concepto": "x", "monto": "10.00",
                                      "token_form": "csrf", "metodo": ["card"]},
               headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert r.status_code == 403
    assert stripe_doble["crear"] == [], "ni siquiera debe llegar a Stripe"
    # Control positivo: el mismo POST con el Origin del panel sí procede.
    r = c.post("/cobros/nuevo", data={"phone": PHONE, "concepto": "x", "monto": "10.00",
                                      "token_form": "ok", "metodo": ["card"]},
               headers={"Origin": "https://testserver"}, follow_redirects=False)
    assert r.status_code == 303 and len(stripe_doble["crear"]) == 1


def test_get_con_origen_ajeno_no_se_toca(entorno):
    r = _cliente(ASESOR).get("/cobros/nuevo", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200


def test_cuerpo_gigante_se_rechaza_con_413(entorno):
    c = _cliente(ASESOR)
    r = c.post("/cobros/nuevo", content=b"a=" + b"x" * (MAX_CUERPO_BYTES + 1),
               headers={"Content-Type": "application/x-www-form-urlencoded"}, follow_redirects=False)
    assert r.status_code == 413


def test_multipart_no_se_mide_aqui(entorno):
    # Los adjuntos tienen su tope en media_store; el guard no debe romperlos.
    c = _cliente(ASESOR)
    r = c.post("/cobros/nuevo", content=b"x" * (MAX_CUERPO_BYTES + 1),
               headers={"Content-Type": "multipart/form-data; boundary=zz"}, follow_redirects=False)
    assert r.status_code != 413


def test_buscador_trata_comodines_como_texto(entorno):
    c = _cliente(ASESOR)
    for q in ("%%", "__", "%_"):
        assert c.get("/cobros/nuevo/clientes", params={"q": q}).json()["clientes"] == [], q
    # Control positivo: el término normal sigue encontrando.
    assert len(c.get("/cobros/nuevo/clientes", params={"q": "Cliente"}).json()["clientes"]) == 1


def _set_cookies(r):
    return [v for k, v in r.headers.multi_items() if k.lower() == "set-cookie" and v.startswith("session=")]


def test_cookie_de_sesion_dentro_de_un_marco_es_none_secure_partitioned(entorno):
    """La extensión muestra el panel dentro de web.whatsapp.com: ahí `Lax` es de terceros y el
    navegador descarta la cookie (el login vuelve a /login). Solo con Sec-Fetch-Dest: iframe."""
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_cobranza_panel import CLAVE

    def entrar(**cabeceras):
        c = TestClient(app, base_url="https://testserver")
        return c.post("/login", data={"username": ASESOR, "password": CLAVE},
                      headers=cabeceras, follow_redirects=False)

    marco = _set_cookies(entrar(**{"Sec-Fetch-Dest": "iframe"}))
    assert len(marco) == 1
    assert "samesite=none" in marco[0].lower() and "partitioned" in marco[0].lower()
    assert marco[0].lower().count("secure") == 1 and "samesite=lax" not in marco[0].lower()
    assert "httponly" in marco[0].lower()
    # Control: fuera de un marco (documento, fetch, curl) la cookie no cambia.
    for cab in ({"Sec-Fetch-Dest": "document"}, {"Sec-Fetch-Dest": "empty"}, {}):
        normal = _set_cookies(entrar(**cab))
        assert len(normal) == 1 and "samesite=lax" in normal[0].lower()
        assert "partitioned" not in normal[0].lower()


def test_cookie_para_marco_no_duplica_atributos():
    v = cookie_para_marco("session=abc; path=/; Max-Age=43200; httponly; samesite=lax; Secure")
    assert v.lower().count("secure") == 1 and v.lower().count("samesite") == 1
    assert v.endswith("SameSite=None; Partitioned")
