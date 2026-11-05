#!/usr/bin/env node
// Prueba en un Chrome real que el ícono pone el CRM en la página, pegado a la derecha y con 1/3 del ancho.
//
//   node ext-cse/prueba/probar-panel.mjs [--ancho=1200] [--alto=800] [--usuario=colab1] [--captura=/ruta.png]
//
// La página de prueba es /health del propio bot: su CSP es default-src 'self', la más estricta que
// habrá, así que si el marco de la extensión entra ahí, entra en casi cualquier sitio. No se puede dar
// clic al ícono por CDP (activeTab no se concede así): se llama a la misma función que el clic, desde
// el service worker, con el permiso de sitio del CRM. Sale 1 si algo falla. No toca el Chrome del operador.
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

const ANCHO = Number(arg("ancho", 1200));
const ALTO = Number(arg("alto", 800));
const USUARIO = arg("usuario", "colab1");
const CAPTURA = arg("captura", path.join(os.tmpdir(), "panel-pagina.png"));

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
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "panelpag-"));
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
  let chrome, cdp, codigo = 1;
  const fallos = [];
  const medido = { crm, ventana: [ANCHO, ALTO] };
  try {
    if (!(await hasta(async () => (await fetch(`${crm}/login`)).ok, 40000))) throw new Error("el bot no atendió /login");
    medido.csp_de_la_pagina = (await fetch(`${crm}/health`)).headers.get("content-security-policy");
    const salida = path.join(dir, "ext");
    const armado = spawnSync(PYTHON, [path.join(EXT, "construir.py"), "--crm", crm, "--navegador", "chrome", "--salida", salida], { encoding: "utf8" });
    if (armado.status !== 0) throw new Error(`construir.py falló:\n${armado.stderr}`);
    const perfil = path.join(dir, "perfil");
    chrome = spawn(CHROME, ["--headless=new", `--user-data-dir=${perfil}`, "--remote-debugging-port=0", "--enable-unsafe-extension-debugging", "--no-first-run", "about:blank"], { stdio: "ignore" });
    const puertoCdp = await hasta(() => Number(fs.readFileSync(path.join(perfil, "DevToolsActivePort"), "utf8").split("\n")[0]), 20000);
    const info = await (await fetch(`http://127.0.0.1:${puertoCdp}/json/version`)).json();
    cdp = await conectar(info.webSocketDebuggerUrl);
    const { id } = await cdp.enviar("Extensions.loadUnpacked", { path: path.join(salida, "chrome") });
    const adjuntar = async (targetId) => (await cdp.enviar("Target.attachToTarget", { targetId, flatten: true })).sessionId;
    const abrir = async (url) => adjuntar((await cdp.enviar("Target.createTarget", { url })).targetId);
    const evaluar = async (s, expression) => {
      const r = await cdp.enviar("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, s);
      if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || "excepción");
      return r.result.value;
    };
    const objetivos = async () => (await cdp.enviar("Target.getTargets")).targetInfos;

    const login = await abrir(`${crm}/login`);
    await hasta(() => evaluar(login, "Boolean(document.querySelector('#password'))"));
    const entro = await evaluar(login, `fetch("/login",{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},body:new URLSearchParams({username:${JSON.stringify(USUARIO)},password:${JSON.stringify(CLAVE)}}),redirect:"manual"}).then(r=>r.type==="opaqueredirect"||r.ok)`);
    if (!entro) throw new Error(`no se pudo entrar como ${USUARIO}`);

    const pagina = await abrir(`${crm}/health`);
    await cdp.enviar("Emulation.setDeviceMetricsOverride", { width: ANCHO, height: ALTO, deviceScaleFactor: 1, mobile: false }, pagina);
    await hasta(() => evaluar(pagina, "document.readyState === 'complete'"));
    const ancho0 = await evaluar(pagina, "document.documentElement.getBoundingClientRect().width");

    const sw = await hasta(async () => (await objetivos()).find((t) => t.type === "service_worker" && t.url.startsWith(`chrome-extension://${id}/`)));
    if (!sw) throw new Error("no encontré el service worker de la extensión");
    const swSesion = await adjuntar(sw.targetId);
    const idPestana = await hasta(() => evaluar(swSesion, `chrome.tabs.query({url: ${JSON.stringify(crm + "/health*")}}).then(t => t[0]?.id)`));
    const alternar = () => evaluar(swSesion, `globalThis.alternarPanel(${idPestana})`);

    medido.al_abrir = await alternar();
    if (medido.al_abrir !== "abierto") fallos.push(`alternarPanel devolvió ${medido.al_abrir}, no «abierto»`);
    const marcoPanel = await hasta(async () => (await objetivos()).find((t) => t.type === "iframe" && t.url.startsWith(`chrome-extension://${id}/panel.html`)), 10000);
    if (!marcoPanel) fallos.push("el marco del panel no entró en la página (¿lo bloquea la CSP?)");
    else {
      const sp = await adjuntar(marcoPanel.targetId);
      medido.ancho_del_panel = await evaluar(sp, "innerWidth");
      medido.alto_del_panel = await evaluar(sp, "innerHeight");
      const esperado = Math.max(ANCHO / 3, 320);
      // Margen de 2 px por el borde de 1 px de la cabecera del panel y el redondeo.
      if (Math.abs(medido.ancho_del_panel - esperado) > 2) fallos.push(`el panel mide ${medido.ancho_del_panel}px y debía medir ${Math.round(esperado)}px (1/3 de ${ANCHO})`);
      const barra = await hasta(() => evaluar(sp, "Boolean(document.querySelector('#app') && !document.querySelector('#app').hidden)"), 20000);
      medido.panel_mostro_la_barra = Boolean(barra);
      if (!barra) fallos.push(`el panel no mostró la barra: ${await evaluar(sp, "document.body.innerText.slice(0,160)")}`);
    }
    medido.ancho_de_la_pagina = { antes: Math.round(ancho0), con_panel: Math.round(await evaluar(pagina, "document.documentElement.getBoundingClientRect().width")) };
    const esperadoPagina = ANCHO - Math.max(ANCHO / 3, 320);
    if (Math.abs(medido.ancho_de_la_pagina.con_panel - esperadoPagina) > 2) fallos.push(`la página debía quedar en ${Math.round(esperadoPagina)}px y mide ${medido.ancho_de_la_pagina.con_panel}px`);
    medido.scroll_horizontal_de_la_pagina = await evaluar(pagina, "document.documentElement.scrollWidth - document.documentElement.clientWidth");
    const foto = await cdp.enviar("Page.captureScreenshot", { format: "png" }, pagina);
    fs.writeFileSync(CAPTURA, Buffer.from(foto.data, "base64"));
    medido.captura = CAPTURA;

    medido.al_cerrar = await alternar();
    await pausa(500);
    medido.ancho_tras_cerrar = Math.round(await evaluar(pagina, "document.documentElement.getBoundingClientRect().width"));
    medido.estilo_html_tras_cerrar = await evaluar(pagina, "document.documentElement.getAttribute('style') || ''");
    if (medido.al_cerrar !== "cerrado") fallos.push(`al segundo clic devolvió ${medido.al_cerrar}, no «cerrado»`);
    if (medido.ancho_tras_cerrar !== Math.round(ancho0)) fallos.push("al cerrar, la página no recuperó su ancho");
    if ((await objetivos()).some((t) => t.type === "iframe" && t.url.startsWith(`chrome-extension://${id}/panel.html`))) fallos.push("al cerrar quedó el marco del panel");
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

main().catch((e) => { console.error(e); process.exit(1); });
