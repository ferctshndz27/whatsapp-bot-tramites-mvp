#!/usr/bin/env node
// Juez de ext-cse en un Chrome real (sin cabeza y con perfil temporal: nunca el del operador).
//
//   node ext-cse/prueba/juez-chrome.mjs
//
// Levanta el bot con una base temporal sembrada (scripts/qa_crm_seed.py, la del juez del CRM), arma
// la extensión con construir.py contra ese bot, la instala por CDP y comprueba:
//   1. Chrome la acepta sin errores ni advertencias del manifiesto.
//   2. El fondo instaló la regla con el id de la extensión (iniciador y frame-ancestors).
//   3. El panel muestra el CRM en su marco: /login sin sesión; tras entrar, /crm.
//   4. Fuera del panel el CRM sigue sin poder enmarcarse: ni otro sitio ni el propio CRM.
// Termina en 1 si algo falla. Sin cabeza no hay panel lateral: panel.html se abre en una pestaña,
// que para el marco es lo mismo (mismo origen chrome-extension://, mismo iniciador).
//
// Variables: CHROME_BIN (def. /usr/bin/google-chrome) y QA_PYTHON (def. .venv/bin/python).

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const EXT = path.dirname(AQUI);
const REPO = path.dirname(EXT);
const PYTHON = process.env.QA_PYTHON || path.join(REPO, ".venv", "bin", "python");
const CHROME = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const CLAVE = "juez-ext-cse-123";
const esperar = (ms) => new Promise((r) => setTimeout(r, ms));

const fallas = [];
function criterio(nombre, ok, detalle = "") {
  if (!ok) fallas.push(nombre);
  console.log(`${ok ? "✅" : "❌"} ${nombre}${detalle ? ` — ${detalle}` : ""}`);
}

async function hasta(fn, { timeout = 15000, intervalo = 250 } = {}) {
  const fin = Date.now() + timeout;
  for (;;) {
    const valor = await fn().catch(() => undefined);
    if (valor || Date.now() > fin) return valor;
    await esperar(intervalo);
  }
}

function puertoLibre() {
  return new Promise((ok, mal) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => ok(port));
    });
    s.on("error", mal);
  });
}

// --- CDP por WebSocket ------------------------------------------------------------------------

function conectar(url) {
  const ws = new WebSocket(url);
  const pendientes = new Map();
  let n = 0;
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    const p = m.id && pendientes.get(m.id);
    if (!p) return;
    pendientes.delete(m.id);
    m.error ? p.mal(new Error(`${m.error.message} ${m.error.data || ""}`)) : p.ok(m.result);
  };
  const enviar = (method, params = {}, sessionId) =>
    new Promise((ok, mal) => {
      const id = ++n;
      pendientes.set(id, { ok, mal });
      ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
  return new Promise((ok, mal) => {
    ws.onopen = () => ok({ enviar, cerrar: () => ws.close() });
    ws.onerror = () => mal(new Error(`no pude conectar a ${url}`));
  });
}

async function evaluar(cdp, sesion, expression) {
  const r = await cdp.enviar("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }, sesion);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}

async function pestana(cdp, url) {
  const { targetId } = await cdp.enviar("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId, flatten: true });
  await cdp.enviar("Page.enable", {}, sessionId);
  await cdp.enviar("Page.navigate", { url }, sessionId);
  sesiones.set(sessionId, targetId);
  return sessionId;
}
const sesiones = new Map(); // sesión → pestaña, para cerrarla

// El fondo (service worker) de la extensión: ahí vive la regla y el comportamiento del ícono.
async function fondoDe(cdp, id) {
  const objetivo = await hasta(async () => {
    const { targetInfos } = await cdp.enviar("Target.getTargets");
    return targetInfos.find((t) => t.type === "service_worker" && t.url.startsWith(`chrome-extension://${id}/`));
  });
  if (!objetivo) throw new Error("no encontré el service worker de la extensión");
  const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId: objetivo.targetId, flatten: true });
  return sessionId;
}

// El marco hacia el CRM es de otro sitio que su padre, así que Chrome lo aísla en su propio
// objetivo de CDP («iframe»): si aparece y carga, la regla dejó pasarlo. El objetivo existe desde
// que empieza la navegación, así que se espera a que el documento de esa ruta termine de cargar.
async function marcoDelCrm(cdp, crm, ruta) {
  return hasta(async () => {
    const { targetInfos } = await cdp.enviar("Target.getTargets");
    const objetivo = targetInfos.find((t) => t.type === "iframe" && t.url.startsWith(crm + ruta));
    if (!objetivo) return undefined;
    const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId: objetivo.targetId, flatten: true });
    const listo = await evaluar(cdp, sessionId,
      `location.href.startsWith(${JSON.stringify(crm + ruta)}) && document.readyState === "complete"`).catch(() => false);
    if (listo) return sessionId;
    await cdp.enviar("Target.detachFromTarget", { sessionId }).catch(() => {});
    return undefined;
  });
}

// URL de cada marco hijo del MISMO sitio que su pestaña, por su atributo name. Si Chrome bloquea
// un marco por X-Frame-Options o frame-ancestors, se queda con su página de error (chrome-error://).
// Un hijo de OTRO sitio no sale en este árbol: va aislado en su propio objetivo («iframe»).
async function marcosHijos(cdp, sesion, nombres) {
  await esperar(1000);
  return hasta(async () => {
    const { frameTree } = await cdp.enviar("Page.getFrameTree", {}, sesion);
    const urls = Object.fromEntries((frameTree.childFrames || []).map((c) => [c.frame.name, c.frame.url]));
    return nombres.every((n) => urls[n] && urls[n] !== "about:blank") ? urls : undefined;
  });
}
const esError = (url) => Boolean(url?.startsWith("chrome-error://"));

// URLs de los marcos aislados (de otro sitio que su padre) que hay en todo el navegador, con la
// URL que de verdad cargó cada uno: a un marco bloqueado Chrome le deja la URL pedida como
// objetivo, pero adentro solo está su página de error.
async function marcosAislados(cdp) {
  const { targetInfos } = await cdp.enviar("Target.getTargets");
  const marcos = [];
  for (const t of targetInfos.filter((t) => t.type === "iframe")) {
    const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId: t.targetId, flatten: true });
    marcos.push({ pedida: t.url, cargada: await evaluar(cdp, sessionId, "location.href").catch((e) => `¿? ${e.message}`) });
    await cdp.enviar("Target.detachFromTarget", { sessionId }).catch(() => {});
  }
  return marcos;
}

// --- Juez -------------------------------------------------------------------------------------

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "ext-cse-juez-"));
  // Sin credenciales ni servicios de fuera: el bot de prueba no debe hablar con nadie.
  const vacias = [
    "ANTHROPIC_API_KEY", "CREDENTIALS_KEY", "DASHBOARD_PASSWORD", "DASHBOARD_USERNAME", "EMBEDDINGS_TOKEN",
    "EMBEDDINGS_URL", "MSSQL_HOST", "MSSQL_SA_PASSWORD", "OLLAMA_TOKEN", "OLLAMA_URL", "PUBLIC_BASE_URL",
    "REGISTRATION_CODE", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM", "WHATSAPP_APP_SECRET", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN",
  ];
  const env = {
    ...process.env,
    ...Object.fromEntries(vacias.map((k) => [k, ""])),
    DB_PATH: path.join(dir, "juez.db"), MEDIA_DIR: path.join(dir, "media"), SECURE_COOKIES: "0",
    SESSION_SECRET: "juez-ext-cse-no-usar", GEOIP_ENABLED: "0", ALTA_MANUAL_ENABLED: "0", RAG_ENABLED: "0",
    CRM_QA_REPO: REPO, CRM_QA_CLAVE: CLAVE, PYTHONUNBUFFERED: "1",
  };
  let uvicorn, chrome, otro, cdp;
  try {
    const siembra = spawnSync(PYTHON, [path.join(REPO, "scripts", "qa_crm_seed.py")], { cwd: REPO, env, encoding: "utf8" });
    if (siembra.status !== 0) throw new Error(`la siembra falló:\n${siembra.stdout}${siembra.stderr}`);

    const puerto = await puertoLibre();
    const crm = `http://127.0.0.1:${puerto}`;
    const salida = path.join(dir, "dist");
    const armado = spawnSync(PYTHON, [path.join(EXT, "construir.py"), "--crm", crm, "--navegador", "chrome", "--salida", salida], { encoding: "utf8" });
    if (armado.status !== 0) throw new Error(`construir.py falló:\n${armado.stderr}`);

    uvicorn = spawn(PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(puerto), "--log-level", "warning"],
      { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
    const bitacora = [];
    uvicorn.stdout.on("data", (d) => bitacora.push(String(d)));
    uvicorn.stderr.on("data", (d) => bitacora.push(String(d)));
    if (!(await hasta(async () => (await fetch(`${crm}/login`)).ok, { timeout: 40000 }))) {
      throw new Error(`el bot no atendió /login:\n${bitacora.join("")}`);
    }

    // «Otro sitio» que intenta enmarcar el CRM: otro host (localhost no es 127.0.0.1) y sin CSP.
    // Enmarca también una página de un tercer sitio, que sí carga: prueba que el juez ve los
    // marcos aislados, y que si el del CRM no aparece es porque Chrome lo bloqueó.
    const puertoOtro = await puertoLibre();
    otro = http.createServer((req, res) => {
      res.setHeader("content-type", "text/html; charset=utf-8");
      res.end(req.url === "/hijo" ? "<!doctype html><title>hijo</title>"
        : `<!doctype html><title>otro sitio</title><iframe src="${crm}/login"></iframe><iframe src="http://127.0.0.1:${puertoOtro}/hijo"></iframe>`);
    }).listen(puertoOtro, "127.0.0.1");

    const perfil = path.join(dir, "perfil");
    chrome = spawn(CHROME, [
      "--headless=new", `--user-data-dir=${perfil}`, "--remote-debugging-port=0", "--enable-unsafe-extension-debugging",
      "--no-first-run", "--no-default-browser-check", "--no-sandbox", "about:blank",
    ], { stdio: "ignore" });
    const activo = await hasta(async () => fs.readFileSync(path.join(perfil, "DevToolsActivePort"), "utf8").split("\n"), { timeout: 20000 });
    if (!activo) throw new Error("Chrome no abrió el puerto de depuración");
    cdp = await conectar(`ws://127.0.0.1:${activo[0]}${activo[1]}`);
    const { product } = await cdp.enviar("Browser.getVersion");
    console.log(`${product} · bot en ${crm}`);

    // Modo desarrollador antes de instalar, para que Chrome junte los errores de la extensión.
    const ajustes = await pestana(cdp, "chrome://extensions");
    await hasta(async () => evaluar(cdp, ajustes, "typeof chrome.developerPrivate === 'object'"));
    await evaluar(cdp, ajustes, "chrome.developerPrivate.updateProfileConfiguration({ inDeveloperMode: true })");

    const { id } = await cdp.enviar("Extensions.loadUnpacked", { path: path.join(salida, "chrome") });
    criterio("Chrome instala la extensión", Boolean(id), id);

    // 2. La regla, leída en el fondo ANTES de abrir el panel: un panel abierto antes de que la regla
    // exista deja su marco bloqueado, y esa carrera del primer arranque no es lo que se juzga aquí.
    const fondo = await fondoDe(cdp, id);
    const reglas = await hasta(async () => {
      const r = await evaluar(cdp, fondo, "chrome.declarativeNetRequest.getDynamicRules()");
      return r?.length ? r : undefined;
    });
    const regla = reglas?.[0];
    const csp = regla?.action.responseHeaders.find((c) => c.header === "content-security-policy")?.value || "";
    criterio("hay una sola regla dinámica", reglas?.length === 1, `${reglas?.length ?? 0}`);
    criterio("la regla admite como iniciadores solo a la extensión y al CRM",
      JSON.stringify(regla?.condition.initiatorDomains) === JSON.stringify([id, "127.0.0.1"]),
      JSON.stringify(regla?.condition.initiatorDomains));
    criterio("la CSP del marco deja enmarcar solo a esta extensión", csp.endsWith(`; frame-ancestors chrome-extension://${id}`), csp);
    const comportamiento = await evaluar(cdp, fondo, "chrome.sidePanel.getPanelBehavior()");
    criterio("el ícono abre el panel lateral", comportamiento?.openPanelOnActionClick === true);

    // 3. El panel muestra el CRM: primero /login, y tras entrar, /crm con la sesión.
    const panel = await pestana(cdp, `chrome-extension://${id}/panel.html`);
    let marco = await marcoDelCrm(cdp, crm, "/login");
    const login = marco && (await hasta(async () => evaluar(cdp, marco, "Boolean(document.querySelector('#password'))")));
    criterio("sin sesión, el marco del panel muestra el login del CRM", Boolean(login));
    if (login) {
      await evaluar(cdp, marco, `(() => {
        document.querySelector("#username").value = "colab1";
        document.querySelector("#password").value = ${JSON.stringify(CLAVE)};
        document.querySelector('form[action="/login"]').submit();
      })()`);
      const fuera = await hasta(async () => {
        const marcos = await marcosAislados(cdp);
        return marcos.find((m) => m.cargada.startsWith(`${crm}/`) && !m.cargada.includes("/login"));
      });
      criterio("el login dentro del marco entra al CRM", Boolean(fuera), fuera?.cargada);
    } else {
      criterio("el login dentro del marco entra al CRM", false, "no hubo login que llenar");
    }
    await cdp.enviar("Page.navigate", { url: `chrome-extension://${id}/panel.html` }, panel);
    marco = await marcoDelCrm(cdp, crm, "/crm");
    const tablero = marco && (await hasta(async () => evaluar(cdp, marco, "Boolean(document.querySelector('#crm'))")));
    criterio("con sesión, el panel abre directo en /crm", Boolean(tablero));

    // 4. Fuera del panel, nada cambia: el CRM no se deja enmarcar. Primero se cierra el panel, cuyo
    // marco al CRM se confundiría con uno que se coló.
    await cdp.enviar("Target.closeTarget", { targetId: sesiones.get(panel) });
    await pestana(cdp, `http://localhost:${puertoOtro}/`);
    const aislados = await hasta(async () => {
      const marcos = await marcosAislados(cdp);
      return marcos.some((m) => m.cargada.endsWith("/hijo")) ? marcos : undefined;
    });
    criterio("otro sitio no puede enmarcar el CRM",
      Boolean(aislados) && !aislados.some((m) => m.cargada.startsWith(`${crm}/`)), JSON.stringify(aislados));
    const propio = await pestana(cdp, `${crm}/login`);
    await hasta(async () => evaluar(cdp, propio, "document.readyState === 'complete'"));
    await evaluar(cdp, propio, `(() => { const f = document.createElement("iframe"); f.name = "crm"; f.src = "/login"; document.body.append(f); })()`);
    const r2 = await marcosHijos(cdp, propio, ["crm"]);
    criterio("ni el propio CRM puede enmarcarse fuera del panel", esError(r2?.crm), JSON.stringify(r2));

    // 1. Lo que Chrome reportó de la extensión durante todo lo anterior.
    const info = await evaluar(cdp, ajustes, `chrome.developerPrivate.getExtensionInfo(${JSON.stringify(id)})`);
    const avisos = [...(info.installWarnings || []), ...(info.manifestErrors || []).map((e) => e.message)];
    criterio("sin errores ni advertencias del manifiesto", avisos.length === 0, avisos.join(" | "));
    const errores = (info.runtimeErrors || []).map((e) => `${e.source}: ${e.message}`);
    criterio("sin errores de ejecución de la extensión", errores.length === 0, errores.join(" | "));
  } finally {
    if (cdp) await cdp.enviar("Browser.close").catch(() => {});
    cdp?.cerrar();
    otro?.close();
    uvicorn?.kill();
    if (chrome && chrome.exitCode === null) await new Promise((r) => { chrome.once("exit", r); setTimeout(r, 5000); });
    for (let i = 0; i < 10; i++) {
      try { fs.rmSync(dir, { recursive: true, force: true }); break; } catch { await esperar(300); }
    }
  }
  console.log(fallas.length ? `\n${fallas.length} criterio(s) fallaron` : "\nTodo en orden");
  return fallas.length ? 1 : 0;
}

main().then((codigo) => process.exit(codigo), (error) => {
  console.error(`❌ ${error.stack || error}`);
  process.exit(1);
});
