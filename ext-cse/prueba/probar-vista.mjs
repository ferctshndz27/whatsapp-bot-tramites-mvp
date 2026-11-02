#!/usr/bin/env node
// Prueba una vista de la barra de ext-cse en un Chrome real (sin cabeza, perfil temporal) y la fotografía.
//
//   node ext-cse/prueba/probar-vista.mjs --modulo=dia [--usuario=colab1] [--ancho=420] [--alto=900] \
//        [--captura=/ruta/captura.png] [--pasos=ext-cse/prueba/pasos-whatsapp.mjs]
//
// Levanta el bot con la base sembrada del juez del CRM, arma la extensión contra él, la instala por
// CDP, entra con --usuario, abre panel.html (sin cabeza no hay panel lateral; para el marco es lo
// mismo), elige el módulo y comprueba, DENTRO del marco: que no haya menú ni barra superior del ERP,
// que no haya scroll horizontal y que los enlaces internos no saquen de la vista. Imprime un JSON con
// lo medido y guarda la captura. Sale 1 si algo del marco falla. No toca el Chrome del operador.
//
// Variables: CHROME_BIN (def. /usr/bin/google-chrome) y QA_PYTHON (def. .venv/bin/python).

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const EXT = path.dirname(AQUI);
const REPO = path.dirname(EXT);
const PYTHON = process.env.QA_PYTHON || path.join(REPO, ".venv", "bin", "python");
const CHROME = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const CLAVE = "clave-qa-123";
const pausa = (ms) => new Promise((r) => setTimeout(r, ms));
const arg = (n, d) => (process.argv.find((a) => a.startsWith(`--${n}=`)) || `=${d}`).split("=").slice(1).join("=");

const MODULO = arg("modulo", "dia");
const USUARIO = arg("usuario", "colab1");
const ANCHO = Number(arg("ancho", 420));
const ALTO = Number(arg("alto", 900));
const PASOS = arg("pasos", "");
const CAPTURA = arg("captura", path.join(os.tmpdir(), `vista-${MODULO}.png`));

function puertoLibre() {
  return new Promise((ok) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => ok(port));
    });
  });
}

async function hasta(fn, timeout = 20000) {
  const fin = Date.now() + timeout;
  while (Date.now() < fin) {
    try {
      const v = await fn();
      if (v) return v;
    } catch {}
    await pausa(250);
  }
  return null;
}

async function conectar(url) {
  const ws = new WebSocket(url);
  await new Promise((r, x) => ((ws.onopen = r), (ws.onerror = x)));
  let n = 0;
  const pend = new Map();
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m.id && pend.has(m.id)) (pend.get(m.id)(m), pend.delete(m.id));
  };
  const enviar = (method, params = {}, sessionId) =>
    new Promise((r, x) => {
      const id = ++n;
      pend.set(id, (m) => (m.error ? x(new Error(`${method}: ${m.error.message}`)) : r(m.result)));
      ws.send(JSON.stringify({ id, method, params, sessionId }));
    });
  return { enviar, cerrar: () => ws.close() };
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "vista-"));
  const env = {
    ...process.env, DB_PATH: path.join(dir, "qa.db"), SECURE_COOKIES: "0", SESSION_SECRET: "qa-secreto-de-prueba-no-usar",
    MSSQL_HOST: "", MSSQL_SA_PASSWORD: "", OLLAMA_URL: "", ANTHROPIC_API_KEY: "", WHATSAPP_TOKEN: "", STRIPE_SECRET_KEY: "",
    ALTA_MANUAL_ENABLED: "0", GEOIP_ENABLED: "0", DASHBOARD_USERNAME: "", DASHBOARD_PASSWORD: "",
    CRM_QA_REPO: REPO, CRM_QA_CLAVE: CLAVE, PYTHONUNBUFFERED: "1",
  };
  const siembra = spawnSync(PYTHON, [path.join(REPO, "scripts", "qa_crm_seed.py")], { cwd: REPO, env, encoding: "utf8" });
  if (siembra.status !== 0) throw new Error(`la siembra falló:\n${siembra.stdout}${siembra.stderr}`);

  const puerto = await puertoLibre();
  const crm = `http://127.0.0.1:${puerto}`;
  const bot = spawn(PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(puerto), "--log-level", "warning"], { cwd: REPO, env, stdio: "ignore" });
  let chrome;
  let cdp;
  let codigo = 1;
  try {
    if (!(await hasta(async () => (await fetch(`${crm}/login`)).ok, 40000))) throw new Error("el bot no atendió /login");

    const salida = path.join(dir, "ext");
    const armado = spawnSync(PYTHON, [path.join(EXT, "construir.py"), "--crm", crm, "--navegador", "chrome", "--salida", salida], { encoding: "utf8" });
    if (armado.status !== 0) throw new Error(`construir.py falló:\n${armado.stderr}`);
    const instalada = path.join(salida, "chrome");

    const perfil = path.join(dir, "perfil");
    chrome = spawn(CHROME, ["--headless=new", `--user-data-dir=${perfil}`, "--remote-debugging-port=0", "--enable-unsafe-extension-debugging", "--no-first-run", "about:blank"], { stdio: "ignore" });
    const puertoCdp = await hasta(() => {
      const [p] = fs.readFileSync(path.join(perfil, "DevToolsActivePort"), "utf8").split("\n");
      return Number(p);
    }, 20000);
    const info = await (await fetch(`http://127.0.0.1:${puertoCdp}/json/version`)).json();
    cdp = await conectar(info.webSocketDebuggerUrl);

    const { id } = await cdp.enviar("Extensions.loadUnpacked", { path: instalada });
    const abrir = async (url) => {
      const { targetId } = await cdp.enviar("Target.createTarget", { url });
      const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId, flatten: true });
      return sessionId;
    };
    const evaluar = async (s, expression) => {
      const r = await cdp.enviar("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, s);
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "excepción en la página");
      return r.result.value;
    };

    // Entra por una pestaña normal: la cookie queda en el perfil y el marco la manda.
    const login = await abrir(`${crm}/login`);
    await hasta(() => evaluar(login, "Boolean(document.querySelector('#password'))"));
    const entro = await evaluar(login, `fetch("/login",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams({username:${JSON.stringify(USUARIO)},password:${JSON.stringify(CLAVE)}}),redirect:"manual"}).then(r=>r.type==="opaqueredirect"||r.ok)`);
    if (!entro) throw new Error(`no se pudo entrar como ${USUARIO}`);

    const panel = await abrir(`chrome-extension://${id}/panel.html`);
    await cdp.enviar("Emulation.setDeviceMetricsOverride", { width: ANCHO, height: ALTO, deviceScaleFactor: 2, mobile: false }, panel);
    if (!(await hasta(() => evaluar(panel, "document.querySelector('#app') && !document.querySelector('#app').hidden")))) {
      throw new Error(`el panel no mostró la barra: ${await evaluar(panel, "document.body.innerText.slice(0,200)")}`);
    }
    const antes = await evaluar(panel, "document.querySelectorAll('iframe').length");
    const botones = await evaluar(panel, "[...document.querySelectorAll('#barra li button')].map(b=>b.dataset.tip)");
    const medido = { crm, usuario: USUARIO, modulo: MODULO, botones: botones.length, marcos_antes_de_elegir: antes };

    // El módulo se elige por su rótulo (dataset.tip) a partir del id de barra.js.
    const rotulo = await evaluar(panel, `import("./barra.js").then(m => m.MODULOS.find(x => x.id === ${JSON.stringify(MODULO)})?.nombre)`);
    await evaluar(panel, `document.querySelector('#barra li button[data-id=${JSON.stringify(MODULO)}]').click()`);
    medido.rotulo = rotulo;
    // Al abrir el panel ya se abrió «Mi día» (vista inicial): se espera el marco de ESTE módulo, no ese.
    const ruta = await evaluar(panel, `import("./barra.js").then(m => m.MODULOS.find(x => x.id === ${JSON.stringify(MODULO)})?.ruta ?? "")`);
    const rutaMarco = new URL(ruta, `${crm}/`).pathname;

    const marco = await hasta(async () => {
      const { targetInfos } = await cdp.enviar("Target.getTargets");
      return targetInfos.find((t) => t.type === "iframe" && t.url.startsWith(`${crm}/`) && new URL(t.url).pathname === rutaMarco);
    });
    medido.marco = marco ? marco.url : null;
    let fallos = [];
    if (marco) {
      const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId: marco.targetId, flatten: true });
      await pausa(1500);
      medido.dentro = await evaluar(sessionId, `(() => ({
        url: location.pathname + location.search,
        titulo: document.title,
        menu: Boolean(document.querySelector(".sidebar")),
        barra_superior: Boolean(document.querySelector(".topbar")),
        embebido: document.body.classList.contains("embebido"),
        scroll_horizontal: document.documentElement.scrollWidth - document.documentElement.clientWidth,
        enlaces_internos: [...document.querySelectorAll("a[href^='/']")].length,
      }))()`);
      if (medido.dentro.menu || medido.dentro.barra_superior) fallos.push("el marco muestra el menú o la barra superior del ERP");
      if (!medido.dentro.embebido) fallos.push("el servidor no marcó la vista como embebida");
      if (medido.dentro.scroll_horizontal > 0) fallos.push(`scroll horizontal de ${medido.dentro.scroll_horizontal}px`);
      // Pasos propios de la vista (--pasos=archivo.mjs, que exporta `async function pasos(ctx)`): hacen clic,
      // miden y fotografían dentro del marco. Cada fallo que anoten cuenta igual que los de arriba.
      if (PASOS) {
        const fotografiar = async (nombre) => {
          const f = await cdp.enviar("Page.captureScreenshot", { format: "png" }, panel);
          const ruta = CAPTURA.replace(/\.png$/, `-${nombre}.png`);
          fs.writeFileSync(ruta, Buffer.from(f.data, "base64"));
          return ruta;
        };
        // `escribirMensaje`: simula que el cliente escribe (in) o que un asesor contesta (out) en la base
        // de la prueba, para ver cómo reacciona el panel sin WhatsApp de verdad.
        const escribirMensaje = (direccion, cuerpo, telefono = "525500000011") => {
          const r = spawnSync(PYTHON, ["-c", `
from app.db import get_session
from app.models import Direction, Message
with get_session() as s:
    s.add(Message(phone=${JSON.stringify(telefono)}, direction=Direction.${direccion === "in" ? "IN" : "OUT"}, body=${JSON.stringify(cuerpo)}))
    s.commit()`], { cwd: REPO, env, encoding: "utf8" });
          if (r.status !== 0) throw new Error(`no pude escribir el mensaje de prueba:\n${r.stderr}`);
        };
        const ctx = { evaluar: (js) => evaluar(sessionId, js), evaluarPanel: (js) => evaluar(panel, js), escribirMensaje, fotografiar, pausa, hasta, medido, fallos, usuario: USUARIO };
        await (await import(path.resolve(PASOS))).pasos(ctx);
      }
    } else {
      fallos.push("no apareció el marco de la vista");
    }
    const foto = await cdp.enviar("Page.captureScreenshot", { format: "png" }, panel);
    fs.writeFileSync(CAPTURA, Buffer.from(foto.data, "base64"));
    medido.captura = CAPTURA;
    medido.fallos = fallos;
    console.log(JSON.stringify(medido, null, 2));
    codigo = fallos.length ? 1 : 0;
  } finally {
    cdp?.cerrar();
    chrome?.kill();
    bot.kill();
    await pausa(300);
    fs.rmSync(dir, { recursive: true, force: true });
  }
  process.exit(codigo);
}

main().catch((e) => {
  console.error(`❌ ${e.message}`);
  process.exit(1);
});
