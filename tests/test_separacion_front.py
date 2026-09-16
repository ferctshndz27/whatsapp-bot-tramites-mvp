"""Separación de HTML, CSS y JS en el panel.

La regla del operador (skill frontend-senior) convertida en contrato: las
plantillas marcan y le pasan datos al JS; el estilo vive en static/css y el
comportamiento en static/js. No es estética. Con JS en línea la CSP tenía que
permitir 'unsafe-inline', y un confirm('{{ nombre }}') dentro de un onsubmit
dejaba que el nombre que un cliente escribe por WhatsApp se ejecutara en la
sesión del asesor: el navegador decodifica las entidades del atributo ANTES de
compilar el JS, así que el escape de Jinja no protege ahí.
"""

import os
import pathlib
import re

from fastapi.testclient import TestClient

from app import main, templating

PLANTILLAS = sorted(pathlib.Path("templates").rglob("*.html"))
JS_PROPIO = sorted(pathlib.Path("static/js").glob("*.js"))


def _leer(p: pathlib.Path) -> str:
    return p.read_text(encoding="utf-8")


def _marcado(p: pathlib.Path) -> str:
    """La plantilla sin comentarios {# #}: lo que nunca llega al navegador no cuenta."""
    return re.sub(r"\{#.*?#\}", "", _leer(p), flags=re.S)


def test_ninguna_plantilla_trae_manejadores_de_evento_en_linea():
    """Un on*= vuelve a abrir la puerta del XSS por atributo y, con la CSP
    cerrada, además deja el botón muerto sin que nadie lo note."""
    culpables = {str(p): re.findall(r"(?<![\w-])(on[a-z]+)\s*=\s*[\"']", _marcado(p)) for p in PLANTILLAS}
    assert not {k: v for k, v in culpables.items() if v}, culpables


def test_el_js_no_arma_html_con_manejadores_en_linea():
    """agente.js arma formularios con template strings: un oninput="…" ahí
    dentro tampoco lo ejecuta la CSP, y el deslizador dejaba de mostrar su valor."""
    for p in JS_PROPIO:
        assert not re.search(r"""\son[a-z]+=["'`]""", _leer(p)), p


def test_ninguna_plantilla_ejecuta_scripts_en_linea():
    """Los datos para el JS viajan como JSON inerte (type="application/json"),
    que la CSP no tiene que ejecutar."""
    for p in PLANTILLAS:
        for atributos, cuerpo in re.findall(r"<script\b([^>]*)>(.*?)</script>", _marcado(p), re.S):
            assert "src=" in atributos or 'type="application/json"' in atributos, (
                f"{p}: <script> en línea: {cuerpo.strip()[:60]!r}"
            )


def test_ninguna_plantilla_trae_css_en_linea():
    """El estilo vive en static/css. Lo único que se tolera en un style="" es un
    color que llega como dato (--c), cuya regla está en utilidades.css."""
    for p in PLANTILLAS:
        s = _marcado(p)
        assert "<style" not in s, f"{p}: bloque <style>"
        # (?<![\w-]) y no \s: también atrapa {% if x %}style="…"{% endif %}.
        for valor in re.findall(r'(?<![\w-])style\s*=\s*"([^"]*)"', s):
            assert re.fullmatch(r"--[a-z-]+:\s*\{\{[^{}]+\}\}", valor.strip()), f'{p}: style="{valor}"'


def test_la_csp_no_deja_ejecutar_js_en_linea():
    """Si alguien vuelve a poner 'unsafe-inline' para que funcione un script
    rápido, un nombre de cliente malicioso vuelve a poder ejecutarse."""
    csp = main.SECURITY_HEADERS["Content-Security-Policy"]
    script_src = next(d for d in csp.split(";") if d.strip().startswith("script-src"))
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src

    r = TestClient(main.app, base_url="https://testserver").get("/login")
    assert r.headers["content-security-policy"] == csp


def test_los_estaticos_de_las_plantillas_existen_y_llevan_huella():
    """Sin huella en la URL, tras un deploy el navegador puede usar el JS viejo
    con el HTML nuevo: botones sin listener hasta que alguien limpie la caché."""
    for p in PLANTILLAS:
        s = _marcado(p)
        assert '"/static/' not in s, f"{p}: estático sin huella; usa {{{{ static('…') }}}}"
        for ruta in re.findall(r"static\('([^']+)'\)", s):
            assert (pathlib.Path("static") / ruta).is_file(), f"{p}: static('{ruta}') no existe"


def test_la_huella_cambia_cuando_cambia_el_archivo(tmp_path, monkeypatch):
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    archivo = tmp_path / "x.js"
    archivo.write_text("uno")
    os.utime(archivo, ns=(1_000_000_000, 1_000_000_000))
    antes = templating.static_url("x.js")

    archivo.write_text("otro contenido")
    os.utime(archivo, ns=(2_000_000_000, 2_000_000_000))
    despues = templating.static_url("x.js")

    assert antes.startswith("/static/x.js?v=") and despues.startswith("/static/x.js?v=")
    assert antes != despues, "la huella no siguió al contenido: el navegador serviría el archivo viejo"
    assert templating.static_url("no-existe.js") == "/static/no-existe.js"


def test_las_acciones_declarativas_tienen_quien_las_atienda():
    """Cada data-* que sustituyó a un on*= necesita su listener; si falta, el
    botón se ve igual y no hace nada (o envía sin pedir confirmación)."""
    acciones = _leer(pathlib.Path("static/js/acciones.js"))
    for gancho in ("form[data-confirm]", "button[data-confirm]", "confirmCampos",
                   "[data-autoenvio]", "[data-copiar]", "tr[data-href]"):
        assert gancho in acciones, gancho
    assert "static('js/acciones.js')" in _leer(pathlib.Path("templates/base.html"))

    usados = set()
    for p in PLANTILLAS:
        usados |= set(re.findall(r"\s(data-confirm|data-autoenvio|data-copiar)\b", _leer(p)))
    assert usados == {"data-confirm", "data-autoenvio", "data-copiar"}, usados


def test_el_confirm_del_cobro_trata_el_nombre_del_cliente_como_dato():
    """El caso que sí era explotable: el nombre del cliente dentro del confirm
    que ve el asesor al generar un cobro."""
    cobro = _leer(pathlib.Path("templates/_cobro.html"))
    assert ('data-confirm="Se va a generar un cobro por {monto} para '
            '{{ ticket.client_name or ticket.phone }}. ¿Confirmas el monto?" '
            'data-confirm-campos="monto"') in cobro
