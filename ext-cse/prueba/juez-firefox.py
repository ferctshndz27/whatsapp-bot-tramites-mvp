#!/usr/bin/env python3
"""Juez de ext-cse en un Firefox real (sin cabeza y con perfil temporal: nunca el del operador).

    python3 ext-cse/prueba/juez-firefox.py

Levanta el bot con una base temporal sembrada (scripts/qa_crm_seed.py), arma la extensión con
construir.py contra ese bot, la instala como temporal por geckodriver (Marionette) y abre su panel
lateral de verdad. Desde el contexto «chrome» de Firefox lee la URL y el título del panel (panel.js
pone en el título el aviso que muestra) y comprueba:
  1. Sin el CRM (en mantenimiento, luego caído), el panel lo dice y cambia el aviso solo; el código
     503 prueba que Firefox concedió el permiso de sitio al instalar.
  2. Al volver el CRM, el panel navega solo a él: /login sin sesión; con sesión, /crm.
  3. Sin red, el panel lo dice y abre el CRM en cuanto vuelve.
  4. Sin permiso de sitio (about:addons lo deja quitar), el panel lo pide y abre el CRM al volver.
  5. Ni la extensión ni el panel dejan errores en la consola del navegador.
Termina en 1 si algo falla.

Variables: FIREFOX_BIN (def. la de geckodriver), GECKODRIVER (def. /snap/bin/geckodriver o el del
PATH) y QA_PYTHON (def. .venv/bin/python).
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
EXT = AQUI.parent
REPO = EXT.parent
PYTHON = os.environ.get("QA_PYTHON") or str(REPO / ".venv" / "bin" / "python")
GECKODRIVER = os.environ.get("GECKODRIVER") or shutil.which("geckodriver") or "/snap/bin/geckodriver"
ID = json.loads((EXT / "manifiesto" / "firefox.json").read_text(encoding="utf-8"))["browser_specific_settings"]["gecko"]["id"]
CLAVE = "juez-ext-cse-123"
# Ana Lucía Ramírez en scripts/qa_crm_seed.py, con su folio tomado por colab1: entra en su alcance.
ANA = "525500000011"
# Sin credenciales ni servicios de fuera: el bot de prueba no debe hablar con nadie.
VACIAS = (
    "ANTHROPIC_API_KEY", "CREDENTIALS_KEY", "DASHBOARD_PASSWORD", "DASHBOARD_USERNAME", "EMBEDDINGS_TOKEN",
    "EMBEDDINGS_URL", "MSSQL_HOST", "MSSQL_SA_PASSWORD", "OLLAMA_TOKEN", "OLLAMA_URL", "PUBLIC_BASE_URL",
    "REGISTRATION_CODE", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM", "WHATSAPP_APP_SECRET", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN",
)

fallas: list[str] = []


def criterio(nombre: str, ok: bool, detalle: str = "") -> None:
    if not ok:
        fallas.append(nombre)
    print(f"{'✅' if ok else '❌'} {nombre}{f' — {detalle}' if detalle else ''}", flush=True)


def hasta(fn, timeout: float = 15, intervalo: float = 0.25):
    fin = time.monotonic() + timeout
    while True:
        try:
            valor = fn()
        except Exception:
            valor = None
        if valor or time.monotonic() > fin:
            return valor
        time.sleep(intervalo)


def puerto_libre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def pedir_local(puerto: int, metodo: str, ruta: str, cuerpo: bytes | None = None, timeout: float = 90) -> tuple[int, bytes]:
    """Una petición HTTP a 127.0.0.1. Con http.client y no urllib: aquí solo se habla HTTP local, y
    urllib abriría también file:// si algún día le llegara otra cosa."""
    conexion = http.client.HTTPConnection("127.0.0.1", puerto, timeout=timeout)
    try:
        cabeceras = {"Content-Type": "application/json"} if cuerpo is not None else {}
        conexion.request(metodo, ruta, body=cuerpo, headers=cabeceras)
        respuesta = conexion.getresponse()
        return respuesta.status, respuesta.read()
    finally:
        conexion.close()


class Mantenimiento(http.server.BaseHTTPRequestHandler):
    """El CRM «en mantenimiento»: su puerto contesta 503 hasta que se levanta el bot."""

    def do_GET(self):
        self.send_response(503)
        self.send_header("content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("en mantenimiento".encode())

    def log_message(self, format, *args):  # noqa: A002 (el nombre lo pone http.server)
        pass


class Marionette:
    """Lo mínimo de WebDriver clásico por geckodriver: sesión, contextos, script y navegación."""

    def __init__(self, puerto: int):
        self.puerto = puerto
        self.sesion = None

    def _pedir(self, metodo: str, ruta: str, cuerpo=None, timeout: float = 90):
        datos = json.dumps(cuerpo).encode() if cuerpo is not None else None
        estado, respuesta = pedir_local(self.puerto, metodo, ruta, datos, timeout)
        if estado >= 400:
            raise RuntimeError(f"{metodo} {ruta}: {respuesta.decode(errors='replace')[:500]}")
        return json.loads(respuesta)["value"]

    def abrir(self, capacidades: dict) -> dict:
        valor = self._pedir("POST", "/session", {"capabilities": {"alwaysMatch": capacidades}})
        self.sesion = valor["sessionId"]
        return valor["capabilities"]

    def __call__(self, metodo: str, ruta: str, cuerpo=None):
        return self._pedir(metodo, f"/session/{self.sesion}{ruta}", cuerpo)

    def contexto(self, cual: str) -> None:
        self("POST", "/moz/context", {"context": cual})

    def js(self, script: str, *args):
        return self("POST", "/execute/sync", {"script": script, "args": list(args)})

    def js_async(self, script: str, *args):
        return self("POST", "/execute/async", {"script": script, "args": list(args)})

    def cerrar(self) -> None:
        if self.sesion:
            try:
                self._pedir("DELETE", f"/session/{self.sesion}", timeout=20)
            except Exception:
                pass


# En el contexto «chrome»: el panel lateral es un <browser> dentro de webext-panels.xhtml.
LEER_PANEL = """
const sb = document.getElementById("sidebar");
const b = sb && sb.contentDocument && sb.contentDocument.getElementById("webext-panels-browser");
return { abierto: SidebarController.isOpen, actual: SidebarController.currentID,
         url: b ? b.currentURI.spec : null, titulo: b ? b.contentTitle : null };
"""
# Cerrar y volver a abrir el panel: Firefox lo carga de nuevo desde default_panel (panel.html).
REABRIR = "SidebarController.hide(); SidebarController.show(arguments[0]); return true;"
# Navegar el panel como lo hace un formulario de la página que muestra (con su principal).
NAVEGAR_PANEL = """
const [url] = arguments;
const b = document.getElementById("sidebar").contentDocument.getElementById("webext-panels-browser");
const uri = Services.io.newURI(url);
b.loadURI(uri, { triggeringPrincipal: Services.scriptSecurityManager.createContentPrincipal(uri, {}) });
return true;
"""
# Lo que hace about:addons al quitar o dar el permiso de un sitio.
PERMISO = """
const [accion, id, origen, listo] = arguments;
const { ExtensionPermissions } = ChromeUtils.importESModule("resource://gre/modules/ExtensionPermissions.sys.mjs");
const extension = WebExtensionPolicy.getByID(id).extension;
ExtensionPermissions[accion](id, { permissions: [], origins: [origen] }, extension).then(() => listo(true), (e) => listo(String(e)));
"""
# En el contexto «content»: el estado de la ayuda «¿Folio manual?» (E4) en la página del CRM.
ESTADO_AYUDA = """
const d = document.querySelector("#crm-dlg-ayuda-folio");
const r = d && d.open ? d.getBoundingClientRect() : null;
const ancho = document.documentElement.clientWidth;
return { abierto: !!d && d.open, modal: !!d && d.open && d.matches(":modal"),
         pasos: d ? d.querySelectorAll("ol.ayuda-pasos > li").length : 0, ancho,
         cabe: !r || (r.left >= 0 && r.right <= ancho),
         scrollX: document.documentElement.scrollWidth > ancho,
         foco: document.activeElement ? document.activeElement.id : null };
"""
# Foco + clic en el botón de la barra, como quien lo activa con el teclado; null si no se ve.
ABRIR_AYUDA = """
const b = document.querySelector("#crm-btn-ayuda-folio");
if (!b || !b.getClientRects().length) return null;
b.focus(); b.click();
""" + ESTADO_AYUDA
ESCAPE = "\ue00c"  # tecla Escape en las acciones de WebDriver
# Errores que juntó la consola del navegador del código de la extensión: excepciones y console.error.
ERRORES = """
const [raiz] = arguments;
const excepciones = Services.console.getMessageArray()
  .filter((m) => m instanceof Ci.nsIScriptError && !(m.flags & Ci.nsIScriptError.warningFlag))
  .filter((m) => (m.sourceName || "").startsWith(raiz))
  .map((m) => `${m.sourceName}:${m.lineNumber} ${m.errorMessage}`);
const consola = Cc["@mozilla.org/consoleAPI-storage;1"].getService(Ci.nsIConsoleAPIStorage).getEvents()
  .filter((e) => e.level === "error" && (e.filename || "").startsWith(raiz))
  .map((e) => `${e.filename}:${e.lineNumber} ${e.arguments.map(String).join(" ")}`);
return [...excepciones, ...consola];
"""


def main() -> int:
    dir_tmp = Path(tempfile.mkdtemp(prefix="ext-cse-juez-ff-"))
    salida = EXT / "dist" / f"juez-firefox-{os.getpid()}"
    env = {
        **os.environ,
        **{k: "" for k in VACIAS},
        "DB_PATH": str(dir_tmp / "juez.db"), "MEDIA_DIR": str(dir_tmp / "media"), "SECURE_COOKIES": "0",
        "SESSION_SECRET": "juez-ext-cse-no-usar", "GEOIP_ENABLED": "0", "ALTA_MANUAL_ENABLED": "0", "RAG_ENABLED": "0",
        "CRM_QA_REPO": str(REPO), "CRM_QA_CLAVE": CLAVE, "PYTHONUNBUFFERED": "1",
    }
    uvicorn = gecko = None
    mantenimiento = None
    ff = None
    try:
        siembra = subprocess.run([PYTHON, str(REPO / "scripts" / "qa_crm_seed.py")], cwd=REPO, env=env, capture_output=True, text=True)
        if siembra.returncode:
            raise RuntimeError(f"la siembra falló:\n{siembra.stdout}{siembra.stderr}")

        puerto = puerto_libre()
        crm = f"http://127.0.0.1:{puerto}"
        # Firefox de snap no lee carpetas ocultas de HOME (ahí suele caer TMPDIR) ni el /tmp del
        # sistema: la extensión se arma dentro de ext-cse/dist, que git ignora.
        armado = subprocess.run([PYTHON, str(EXT / "construir.py"), "--crm", crm, "--navegador", "firefox", "--salida", str(salida)],
                                capture_output=True, text=True)
        if armado.returncode:
            raise RuntimeError(f"construir.py falló:\n{armado.stderr}")

        mantenimiento = http.server.ThreadingHTTPServer(("127.0.0.1", puerto), Mantenimiento)
        threading.Thread(target=mantenimiento.serve_forever, daemon=True).start()

        # El contexto «chrome» de Marionette (para leer el panel lateral) exige este permiso explícito.
        puerto_gd = puerto_libre()
        gecko = subprocess.Popen([GECKODRIVER, "--port", str(puerto_gd), "--allow-system-access"],
                                 env={**os.environ, "MOZ_REMOTE_ALLOW_SYSTEM_ACCESS": "1"},
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not hasta(lambda: pedir_local(puerto_gd, "GET", "/status", timeout=2)[0] == 200, timeout=20):
            raise RuntimeError("geckodriver no arrancó")
        ff = Marionette(puerto_gd)
        opciones = {"args": ["-headless"], "prefs": {"devtools.console.stdout.chrome": False}}
        if os.environ.get("FIREFOX_BIN"):
            opciones["binary"] = os.environ["FIREFOX_BIN"]
        capacidades = ff.abrir({"browserName": "firefox", "moz:firefoxOptions": opciones})
        print(f"Firefox {capacidades.get('browserVersion')} · bot en {crm}", flush=True)

        instalado = ff("POST", "/moz/addon/install", {"path": str(salida / "firefox"), "temporary": True})
        criterio("Firefox instala la extensión", instalado == ID, str(instalado))
        # El permiso de sitio tal como lo declara el paquete (en Firefox, sin puerto).
        sitio = json.loads((salida / "firefox" / "manifest.json").read_text(encoding="utf-8"))["host_permissions"][0]
        ff.contexto("chrome")
        raiz = "moz-extension://" + ff.js("return WebExtensionPolicy.getByID(arguments[0]).mozExtensionHostname", ID) + "/"
        clave = hasta(lambda: ff.js("return [...SidebarController.sidebars.keys()].find((k) => k.endsWith('-sidebar-action'))"))
        criterio("la extensión trae su panel lateral", bool(clave), str(clave))

        def panel():
            return ff.js(LEER_PANEL)

        def panel_con(titulo: str, timeout: float = 12):
            return hasta(lambda: (p := panel()) and p["titulo"] == f"{titulo} — Trámites" and p["url"].startswith(raiz) and p, timeout=timeout)

        def panel_en(ruta: str, timeout: float = 15):
            return hasta(lambda: (p := panel()) and p["url"].startswith(crm + ruta) and p["titulo"] and p, timeout=timeout)

        # 1. Sin el CRM.
        ff.js(REABRIR, clave)
        p = panel_con("El CRM no está disponible")
        criterio("con el CRM en mantenimiento, el panel lo dice (y el permiso de sitio vino al instalar)", bool(p), json.dumps(p or panel()))
        mantenimiento.shutdown()
        mantenimiento.server_close()
        mantenimiento = None
        p = panel_con("No se pudo conectar con el CRM")
        criterio("si el CRM deja de contestar, el panel cambia el aviso solo", bool(p), json.dumps(p or panel()))

        # 2. Al volver el CRM, el panel navega solo a él. Su salida va a un archivo: un tubo que nadie
        # lee acaba por llenarse y detener al bot.
        bitacora = dir_tmp / "uvicorn.log"
        with bitacora.open("wb") as salida_bot:
            uvicorn = subprocess.Popen([PYTHON, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(puerto), "--log-level", "warning"],
                                       cwd=REPO, env=env, stdout=salida_bot, stderr=subprocess.STDOUT)
        if not hasta(lambda: pedir_local(puerto, "GET", "/login", timeout=2)[0] == 200, timeout=40):
            raise RuntimeError(f"el bot no atendió /login:\n{bitacora.read_text(errors='replace')[-2000:]}")
        p = panel_en("/login")
        criterio("al volver el CRM, el panel lo abre solo: sin sesión, el login", bool(p), json.dumps(p or panel()))

        # La sesión se abre en una pestaña: el panel es una página de primer nivel del mismo sitio
        # y comparte sus cookies. En el panel mismo no se puede escribir desde aquí.
        ff.contexto("content")
        ff("POST", "/url", {"url": f"{crm}/login"})
        ff.js("""
          document.querySelector("#username").value = "colab1";
          document.querySelector("#password").value = arguments[0];
          document.querySelector('form[action="/login"]').submit();
        """, CLAVE)
        dentro = hasta(lambda: (u := ff("GET", "/url")) and "/login" not in u and u)
        criterio("el login en una pestaña entra al CRM", bool(dentro), str(dentro))
        ff.contexto("chrome")
        ff.js(REABRIR, clave)
        p = panel_en("/crm")
        criterio("con sesión, el panel abre directo en /crm", bool(p), json.dumps(p or panel()))

        # E3. En Firefox el panel ES la página del CRM: su campo «Ir al cliente» es el mismo que en
        # la web (lo prueban el juez del CRM y el de Chrome). Desde aquí no se escribe en el panel,
        # así que se navega a lo que manda ese formulario: un GET con el número tal como se pegó. Al
        # abrir el chat, crm.js deja en la URL el número canónico que le dio el servidor.
        ff.js(NAVEGAR_PANEL, f"{crm}/crm?chat=%2B52+1+55+0000+0011")
        p = panel_en(f"/crm?chat={ANA}")
        criterio("«Ir al cliente»: el número pegado abre su conversación en el panel", bool(p), json.dumps(p or panel()))

        # E4. La ayuda «¿Folio manual?» es parte de la página del CRM que el panel muestra, y el panel
        # no se toca desde aquí: se prueba esa misma página en una pestaña tan angosta como Firefox la
        # deja (no baja de 500 px; los 360–400 px de un panel los cubren el juez del CRM y el de Chrome),
        # con el panel cerrado para que no le quite ancho. Firefox debe abrirla como modal, sin scroll
        # horizontal, y cerrarla con una tecla Escape real. El paso 3 vuelve a abrir el panel.
        ff.js("SidebarController.hide(); return true;")
        ff.contexto("content")
        rect = ff("GET", "/window/rect")
        ayuda = tras_escape = None
        try:
            ff("POST", "/window/rect", {"width": 420, "height": 900})
            ff("POST", "/url", {"url": f"{crm}/crm"})
            ayuda = hasta(lambda: ff.js(ABRIR_AYUDA))
            if ayuda and ayuda["abierto"]:
                tecla = [{"type": "keyDown", "value": ESCAPE}, {"type": "keyUp", "value": ESCAPE}]
                ff("POST", "/actions", {"actions": [{"type": "key", "id": "teclado", "actions": tecla}]})
                tras_escape = hasta(lambda: (e := ff.js(ESTADO_AYUDA)) and not e["abierto"] and e, timeout=5)
                ff("DELETE", "/actions")
        finally:
            ff("POST", "/window/rect", {"width": rect["width"], "height": rect["height"]})
            ff.contexto("chrome")
        criterio("«¿Folio manual?» abre la ayuda del CRM en Firefox: modal, 6 pasos y cabe en la ventana más angosta",
                 bool(ayuda and ayuda["abierto"] and ayuda["modal"] and ayuda["pasos"] == 6 and ayuda["cabe"]
                      and not ayuda["scrollX"] and ayuda["ancho"] <= 520 and ayuda["foco"] == "crm-ayuda-folio-cerrar"),
                 json.dumps(ayuda))
        criterio("Escape cierra la ayuda y el foco vuelve al botón",
                 bool(tras_escape and tras_escape["foco"] == "crm-btn-ayuda-folio"), json.dumps(tras_escape))

        # 3. Sin red.
        ff.js("Services.io.offline = true; return true;")
        ff.js(REABRIR, clave)
        p = panel_con("Sin conexión a internet")
        criterio("sin red, el panel lo dice", bool(p), json.dumps(p or panel()))
        ff.js("Services.io.offline = false; return true;")
        p = panel_en("/crm")
        criterio("al volver la red, el panel abre el CRM solo", bool(p), json.dumps(p or panel()))

        # 4. Sin permiso de sitio.
        quitado = ff.js_async(PERMISO, "remove", ID, sitio)
        ff.js(REABRIR, clave)
        p = panel_con("Falta el permiso para conectarse con el CRM")
        criterio("sin permiso de sitio, el panel lo pide en vez de culpar al CRM", quitado is True and bool(p), json.dumps(p or panel()))
        dado = ff.js_async(PERMISO, "add", ID, sitio)
        p = panel_en("/crm")
        criterio("al dar el permiso, el panel abre el CRM solo", dado is True and bool(p), json.dumps(p or panel()))

        # 5. La consola, después de todo lo anterior.
        errores = ff.js(ERRORES, raiz)
        criterio("sin errores de la extensión en la consola", not errores, " | ".join(errores))
    finally:
        if ff:
            ff.cerrar()
        if gecko:
            gecko.terminate()
            try:
                gecko.wait(10)
            except subprocess.TimeoutExpired:
                gecko.kill()
        if mantenimiento:
            mantenimiento.shutdown()
            mantenimiento.server_close()
        if uvicorn:
            uvicorn.terminate()
            try:
                uvicorn.wait(10)
            except subprocess.TimeoutExpired:
                uvicorn.kill()
        shutil.rmtree(dir_tmp, ignore_errors=True)
        shutil.rmtree(salida, ignore_errors=True)
    print(f"\n{len(fallas)} criterio(s) fallaron" if fallas else "\nTodo en orden")
    return 1 if fallas else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # el juez reporta y sale en 1: nunca deja procesos colgados
        print(f"❌ {error}", file=sys.stderr)
        sys.exit(1)
