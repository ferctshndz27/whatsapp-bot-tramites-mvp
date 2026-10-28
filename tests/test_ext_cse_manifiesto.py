"""ext-cse E1: los manifiestos de la extensión piden lo mínimo y no traen nada de fuera.

La extensión vive junto al CRM con la sesión del colaborador abierta: un permiso de más, un host de
más o un script remoto convierten un panel lateral en una puerta a todos los clientes que ese
colaborador ve. Lo que el navegador le enseña a quien la instala tiene que ser todo lo que puede
hacer. `ext-cse/construir.py` arma los dos paquetes; estas pruebas lo corren como el operador, en una
carpeta temporal, y revisan lo que saldría. Desde E5 también el zip que se entrega: que traiga la misma
carpeta, con el manifiesto en la raíz, y que el mismo código dé siempre el mismo zip. Que se instala
en Chrome y Firefox reales lo juzgan ext-cse/prueba/, que instalan el zip y no la carpeta.
"""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from app.main import SECURITY_HEADERS

RAIZ = Path(__file__).resolve().parent.parent
CONSTRUIR = RAIZ / "ext-cse" / "construir.py"
CRM = "https://bot.esaumillan.cloud"
NAVEGADORES = ["chrome", "firefox"]


def _construir(*args, salida):
    return subprocess.run(
        [sys.executable, str(CONSTRUIR), *args, "--salida", str(salida)],
        capture_output=True,
        text=True,
        timeout=60,
    )


def _manifiesto(dist, navegador):
    return json.loads((dist / navegador / "manifest.json").read_text(encoding="utf-8"))


def _regla(dist):
    return json.loads((dist / "chrome" / "regla-marco.json").read_text(encoding="utf-8"))


def _directivas(csp):
    return [d.strip() for d in csp.split(";") if d.strip()]


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    salida = tmp_path_factory.mktemp("dist")
    r = _construir("--crm", CRM, salida=salida)
    assert r.returncode == 0, r.stderr
    return salida


@pytest.fixture(scope="module")
def construir():
    spec = importlib.util.spec_from_file_location("ext_cse_construir", CONSTRUIR)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


# --- Permisos ---------------------------------------------------------------------------------


def test_chrome_pide_solo_el_panel_lateral_y_la_regla_de_cabeceras(dist):
    """Ni `storage` (nada lo usa, y el teléfono de un cliente no se guarda en el navegador) ni
    `tabs`, `scripting` o `webRequest`, que leerían o tocarían otras páginas, WhatsApp Web incluido."""
    assert sorted(_manifiesto(dist, "chrome")["permissions"]) == ["declarativeNetRequest", "sidePanel"], (
        "un permiso de más amplía lo que la extensión puede hacer con la sesión del CRM"
    )


def test_firefox_no_pide_ningun_permiso(dist):
    """En Firefox el panel navega a la página del CRM misma (spike S1): no hay marco ni cabeceras
    que tocar, así que tampoco regla."""
    assert _manifiesto(dist, "firefox").get("permissions", []) == []


@pytest.mark.parametrize("navegador", NAVEGADORES)
def test_el_unico_sitio_que_toca_es_el_crm(dist, navegador):
    m = _manifiesto(dist, navegador)
    assert m["host_permissions"] == [f"{CRM}/*"], "la extensión solo debe alcanzar al CRM"
    for clave in (
        "optional_permissions",
        "optional_host_permissions",
        "content_scripts",
        "web_accessible_resources",
        "externally_connectable",
    ):
        assert clave not in m, f"{clave} abriría la extensión a otros sitios, WhatsApp Web incluido"
    texto = (dist / navegador / "manifest.json").read_text(encoding="utf-8")
    assert "<all_urls>" not in texto and "*://" not in texto


@pytest.mark.parametrize("navegador", NAVEGADORES)
def test_no_trae_codigo_ni_configuracion_de_fuera(dist, navegador):
    """MV3 prohíbe el código remoto con su CSP por omisión: aflojarla, o que un script pida su
    configuración a otro servidor, metería código que nadie revisó junto a la sesión del CRM."""
    m = _manifiesto(dist, navegador)
    assert "content_security_policy" not in m
    assert "update_url" not in m.get("browser_specific_settings", {}).get("gecko", {})
    for archivo in (dist / navegador).iterdir():
        if archivo.suffix in (".js", ".html", ".css"):
            assert not re.search(r"https?://", archivo.read_text(encoding="utf-8")), (
                f"{archivo.name} nombra una URL: el único sitio es el CRM y sale del manifiesto"
            )


# --- Paquete ----------------------------------------------------------------------------------


@pytest.mark.parametrize("navegador", NAVEGADORES)
def test_viaja_todo_lo_que_se_usa_y_nada_mas(dist, navegador):
    """Un archivo nombrado que falta rompe la extensión al instalarla; uno que sobra viaja sin
    que nadie lo revise. Se sigue la cadena: manifiesto → HTML → <script>/<link> → import."""
    carpeta = dist / navegador
    m = _manifiesto(dist, navegador)
    usados = set(m["background"].get("scripts", []))
    usados |= {m["background"]["service_worker"]} if "service_worker" in m["background"] else set()
    usados |= {m["side_panel"]["default_path"]} if "side_panel" in m else set()
    usados |= {m["sidebar_action"]["default_panel"]} if "sidebar_action" in m else set()
    pendientes = list(usados)
    while pendientes:
        nombre = pendientes.pop()
        ruta = carpeta / nombre
        assert ruta.is_file(), f"{nombre} se nombra pero no viaja en el paquete de {navegador}"
        if ruta.suffix not in (".html", ".js"):
            continue
        texto = ruta.read_text(encoding="utf-8")
        for grupos in re.findall(r'(?:src|href)="([^"]+)"|from "\./([^"]+)"|getURL\("([^"]+)"\)', texto):
            referido = next(g for g in grupos if g)
            if referido not in usados:
                usados.add(referido)
                pendientes.append(referido)
    sobrantes = {p.name for p in carpeta.iterdir()} - usados - {"manifest.json"}
    assert not sobrantes, f"viajan archivos que nada usa: {sorted(sobrantes)}"


def test_los_dos_paquetes_son_la_misma_extension(dist):
    chrome, firefox = _manifiesto(dist, "chrome"), _manifiesto(dist, "firefox")
    for clave in ("manifest_version", "name", "version", "description", "action"):
        assert chrome[clave] == firefox[clave], f"{clave} difiere entre navegadores"
    assert chrome["manifest_version"] == 3
    assert re.fullmatch(r"\d+(\.\d+){0,3}", chrome["version"])
    assert len(chrome["name"]) <= 75 and len(chrome["description"]) <= 132, "topes de Chrome"


def test_chrome_no_arrastra_claves_de_firefox(dist):
    m = _manifiesto(dist, "chrome")
    assert set(m["background"]) == {"service_worker"}
    assert "sidebar_action" not in m and "browser_specific_settings" not in m


def test_firefox_trae_lo_que_exige_para_instalar_una_mv3(dist):
    """Firefox no asigna id a una MV3 (hay que darlo) y, desde nov. 2025, exige declarar qué datos
    transmite: aquí, ninguno. Tampoco tiene service workers de extensión."""
    m = _manifiesto(dist, "firefox")
    gecko = m["browser_specific_settings"]["gecko"]
    assert re.fullmatch(r"[a-zA-Z0-9-._]*@[a-zA-Z0-9-._]+", gecko["id"])
    assert gecko["data_collection_permissions"] == {"required": ["none"]}
    # Ese aviso de datos existe desde Firefox 140 (142 en Android): con una mínima menor,
    # web-ext lint advierte que un Firefox viejo instalaría la extensión sin mostrarlo.
    assert int(gecko["strict_min_version"].split(".")[0]) >= 140
    assert int(m["browser_specific_settings"]["gecko_android"]["strict_min_version"].split(".")[0]) >= 142
    assert "scripts" in m["background"] and "service_worker" not in m["background"]
    assert "side_panel" not in m


# --- Regla del marco (Chrome) -----------------------------------------------------------------


def test_la_regla_solo_toca_submarcos_hacia_el_crm(dist):
    """El bot prohíbe que lo enmarquen (clickjacking). La regla lo permite solo en el panel: con
    `main_frame` u otro tipo, o sin filtro de origen, quitaría esa protección en otros sitios."""
    regla = _regla(dist)
    condicion = regla["condition"]
    assert condicion["resourceTypes"] == ["sub_frame"]
    assert condicion["requestDomains"] == ["bot.esaumillan.cloud"]
    assert condicion["urlFilter"] == f"|{CRM}/", "sin ancla también valdría para subdominios u otro puerto"
    # fondo-chrome.js antepone el id de la extensión al instalarse. El CRM va porque el login
    # redirige dentro del marco y ahí quien inicia es el propio CRM (spike S1).
    assert condicion["initiatorDomains"] == ["bot.esaumillan.cloud"]
    assert regla["action"]["type"] == "modifyHeaders"
    assert "requestHeaders" not in regla["action"]
    cabeceras = {(c["header"], c["operation"]) for c in regla["action"]["responseHeaders"]}
    assert cabeceras == {("x-frame-options", "remove"), ("content-security-policy", "set")}


def test_la_csp_del_marco_es_la_del_servidor_sin_frame_ancestors(dist):
    """La regla REEMPLAZA la CSP del bot dentro del panel. Copiada a mano, el día que el servidor
    la apriete el panel seguiría con la vieja; por eso construir.py la lee de app/main.py.
    frame-ancestors no va aquí: fondo-chrome.js la pone con el id de la extensión."""
    regla = _regla(dist)
    csp = next(c["value"] for c in regla["action"]["responseHeaders"] if c["header"] == "content-security-policy")
    servidor = _directivas(SECURITY_HEADERS["Content-Security-Policy"])
    assert "frame-ancestors 'none'" in servidor, "el bot ya no prohíbe que lo enmarquen: ¿sigue haciendo falta la regla?"
    assert _directivas(csp) == [d for d in servidor if not d.startswith("frame-ancestors")]


def test_sin_csp_en_el_servidor_no_hay_paquete(construir, tmp_path):
    """Si un refactor mueve la CSP de app/main.py, construir.py no puede inventarse una: se detiene."""
    falso = tmp_path / "main.py"
    falso.write_text('SECURITY_HEADERS = {"X-Frame-Options": "SAMEORIGIN"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        construir.csp_del_servidor(falso)
    falso.write_text("OTRA_COSA = 1\n", encoding="utf-8")
    with pytest.raises(ValueError):
        construir.csp_del_servidor(falso)


# --- El origen del CRM ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "crm",
    [
        "http://bot.esaumillan.cloud",  # sin TLS la cookie de sesión viajaría en claro
        "https://bot.esaumillan.cloud/crm",  # una página, no un origen
        "https://*.esaumillan.cloud",
        "bot.esaumillan.cloud",
        "https://bot.esaumillan.cloud/?x=1",
        "https://alguien@bot.esaumillan.cloud",
        "<all_urls>",
    ],
)
def test_el_crm_es_un_solo_origen_https(tmp_path, crm):
    r = _construir("--crm", crm, salida=tmp_path)
    assert r.returncode != 0, f"aceptó {crm!r}"
    assert "--crm" in r.stderr, f"falló por otra cosa, no por el origen: {r.stderr}"
    assert not any(tmp_path.iterdir()), "dejó un paquete a medias"


@pytest.mark.parametrize(
    "crm, host",
    [
        ("https://bot.esaumillan.cloud/", "https://bot.esaumillan.cloud/*"),
        ("http://localhost:8765", "http://localhost:8765/*"),
        ("http://127.0.0.1:8765", "http://127.0.0.1:8765/*"),
    ],
)
def test_acepta_el_crm_de_produccion_y_uno_local_para_probar(tmp_path, crm, host):
    r = _construir("--crm", crm, "--navegador", "chrome", salida=tmp_path)
    assert r.returncode == 0, r.stderr
    assert _manifiesto(tmp_path, "chrome")["host_permissions"] == [host]


@pytest.mark.parametrize("navegador", NAVEGADORES)
@pytest.mark.parametrize("crm", [CRM, "http://127.0.0.1:8765"])
def test_el_codigo_toma_el_origen_exacto_de_homepage_url(tmp_path, navegador, crm):
    """ext-cse E2: crm.js arma /crm y /health con homepage_url. Es el origen exacto, con puerto,
    aunque el permiso de sitio de Firefox no lo lleve."""
    r = _construir("--crm", crm, "--navegador", navegador, salida=tmp_path)
    assert r.returncode == 0, r.stderr
    assert _manifiesto(tmp_path, navegador)["homepage_url"] == f"{crm}/"


def test_en_firefox_el_permiso_de_sitio_no_lleva_puerto(tmp_path):
    """ext-cse E2: Firefox no admite puertos en los patrones (MDN, «Match patterns»). Con
    «http://127.0.0.1:8765/*» el permiso no coincide con nada y la consulta del panel a /health cae
    en CORS (comprobado en Firefox 158). Sin puerto vale para cualquiera de ese host. Chrome sí
    respeta el puerto: lo comprueba la prueba anterior."""
    r = _construir("--crm", "http://127.0.0.1:8765", "--navegador", "firefox", salida=tmp_path)
    assert r.returncode == 0, r.stderr
    assert _manifiesto(tmp_path, "firefox")["host_permissions"] == ["http://127.0.0.1/*"]


# --- El zip que se entrega (E5) ---------------------------------------------------------------


def _zip(dist, navegador):
    zips = sorted(dist.glob(f"ext-cse-{navegador}-*.zip"))
    assert len(zips) == 1, f"debe quedar un solo zip de {navegador}: {[z.name for z in zips]}"
    return zips[0]


@pytest.mark.parametrize("navegador", NAVEGADORES)
def test_el_zip_trae_lo_mismo_que_la_carpeta_byte_por_byte(dist, navegador):
    """Todo lo de arriba se revisa en la carpeta, pero lo que se instala es el zip: si difirieran, el
    colaborador instalaría algo que ninguna prueba vio."""
    with zipfile.ZipFile(_zip(dist, navegador)) as z:
        assert z.testzip() is None, "el zip está dañado"
        en_zip = {nombre: z.read(nombre) for nombre in z.namelist()}
    en_carpeta = {p.name: p.read_bytes() for p in (dist / navegador).iterdir()}
    assert sorted(en_zip) == sorted(en_carpeta), "el zip no trae los mismos archivos que la carpeta"
    for nombre, contenido in en_carpeta.items():
        assert en_zip[nombre] == contenido, f"{nombre} cambió dentro del zip"


@pytest.mark.parametrize("navegador", NAVEGADORES)
def test_el_manifiesto_va_en_la_raiz_del_zip(dist, navegador):
    """Chrome, Firefox y sus tiendas buscan manifest.json en la raíz: un zip hecho desde fuera de la
    carpeta (chrome/manifest.json) lo rechazan por no tener manifiesto."""
    with zipfile.ZipFile(_zip(dist, navegador)) as z:
        nombres = z.namelist()
    assert "manifest.json" in nombres
    assert not [n for n in nombres if "/" in n or "\\" in n], f"entradas dentro de una carpeta: {nombres}"


@pytest.mark.parametrize("navegador", NAVEGADORES)
def test_el_zip_lleva_la_version_del_manifiesto(dist, navegador):
    """Con la versión en el nombre, el zip que alguien tiene instalado se distingue del nuevo."""
    version = _manifiesto(dist, navegador)["version"]
    assert _zip(dist, navegador).name == f"ext-cse-{navegador}-{version}.zip"


def test_armar_dos_veces_da_el_mismo_zip(tmp_path):
    """El SHA-256 que imprime construir.py dice si lo instalado es lo revisado solo si el mismo código
    da el mismo zip. Con la hora del armado en sus entradas cambiaría cada vez; la espera pasa los
    2 s de resolución de la fecha de un zip, para que esa hora no pueda coincidir."""
    a, b = tmp_path / "a", tmp_path / "b"
    assert _construir("--crm", CRM, salida=a).returncode == 0
    time.sleep(2.1)
    r = _construir("--crm", CRM, salida=b)
    assert r.returncode == 0, r.stderr
    for navegador in NAVEGADORES:
        contenido = _zip(b, navegador).read_bytes()
        assert _zip(a, navegador).read_bytes() == contenido, f"el zip de {navegador} cambió sin cambiar el código"
        assert hashlib.sha256(contenido).hexdigest() in r.stdout, f"no imprime el SHA-256 del zip de {navegador}"


# --- Reconstruir ------------------------------------------------------------------------------


def test_reconstruir_no_deja_archivos_viejos(tmp_path):
    assert _construir("--crm", CRM, salida=tmp_path).returncode == 0
    viejo = tmp_path / "chrome" / "viejo.js"
    viejo.write_text("// de una versión anterior\n", encoding="utf-8")
    assert _construir("--crm", CRM, salida=tmp_path).returncode == 0
    assert not viejo.exists(), "un script viejo viajaría en el paquete sin que nadie lo revise"


def test_reconstruir_no_deja_zips_de_otra_version(tmp_path):
    """Tras subir la versión, el zip anterior junto al nuevo se instalaría por error. Un zip que no es
    de la extensión, en cambio, no se toca."""
    assert _construir("--crm", CRM, salida=tmp_path).returncode == 0
    viejo = tmp_path / "ext-cse-chrome-0.0.1.zip"
    viejo.write_bytes(b"zip de una version anterior")
    ajeno = tmp_path / "otra-cosa.zip"
    ajeno.write_bytes(b"no es de la extension")
    assert _construir("--crm", CRM, salida=tmp_path).returncode == 0
    assert not viejo.exists(), "quedó el zip de una versión anterior"
    assert ajeno.exists(), "borró un zip que no era suyo"
    assert len(list(tmp_path.glob("ext-cse-chrome-*.zip"))) == 1


def test_no_borra_una_carpeta_que_no_es_una_construccion(tmp_path):
    ajena = tmp_path / "chrome"
    ajena.mkdir()
    (ajena / "importante.txt").write_text("no es de la extensión\n", encoding="utf-8")
    r = _construir("--crm", CRM, salida=tmp_path)
    assert r.returncode != 0
    assert "no borro" in r.stderr, f"falló por otra cosa: {r.stderr}"
    assert (ajena / "importante.txt").exists(), "borró una carpeta que no era suya"
    assert not (tmp_path / "firefox").exists(), "con un navegador fallido no se arma el otro"
    assert not list(tmp_path.glob("*.zip")), "con un navegador fallido no se escribe ningún zip"
