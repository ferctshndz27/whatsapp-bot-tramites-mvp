#!/usr/bin/env node
// Juez de ext-cse en un Chrome real (sin cabeza y con perfil temporal: nunca el del operador).
//
//   node ext-cse/prueba/juez-chrome.mjs
//
// Levanta el bot con una base temporal sembrada (scripts/qa_crm_seed.py, la del juez del CRM), arma
// la extensión con construir.py contra ese bot, instala su zip (descomprimido, como a mano) por CDP y
// comprueba:
//   1. Chrome la acepta sin errores ni advertencias del manifiesto.
//   2. El fondo instaló la regla con el id de la extensión (iniciador y frame-ancestors).
//   3. Sin el CRM (en mantenimiento, luego caído), el panel lo dice, no abre el marco, el botón
//      Reintentar vuelve a preguntar y el aviso cambia solo (E2).
//   4. Al volver el CRM, el panel lo abre solo en su marco: /login sin sesión; tras entrar, /crm.
//   5. Si falta la regla (primer arranque), el panel la pide al fondo antes de crear el marco (E2).
//   6. Sin red, el panel lo dice y abre el CRM en cuanto vuelve (E2).
//   7. Sin permiso de sitio, el panel lo pide (no culpa al CRM) y abre el CRM al recibirlo (E2).
//   8. Fuera del panel el CRM sigue sin poder enmarcarse: ni otro sitio ni el propio CRM.
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
// Ana Lucía Ramírez en scripts/qa_crm_seed.py, con su folio tomado por colab1: entra en su alcance.
const ANA = "525500000011";
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

const RED = { latency: 0, downloadThroughput: -1, uploadThroughput: -1 };

// Pestaña nueva en `url`. Con sinRed, Chrome la deja fuera de línea ANTES de navegar: la página
// arranca con navigator.onLine en false y recibe «online» cuando se le devuelve la red.
async function pestana(cdp, url, { sinRed = false } = {}) {
  const { targetId } = await cdp.enviar("Target.createTarget", { url: "about:blank" });
  const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId, flatten: true });
  if (sinRed) {
    await cdp.enviar("Network.enable", {}, sessionId);
    await cdp.enviar("Network.emulateNetworkConditions", { offline: true, ...RED }, sessionId);
  }
  await cdp.enviar("Page.enable", {}, sessionId);
  await cdp.enviar("Page.navigate", { url }, sessionId);
  sesiones.set(sessionId, targetId);
  return sessionId;
}
const sesiones = new Map(); // sesión → pestaña, para cerrarla
const cerrar = (cdp, sesion) => cdp.enviar("Target.closeTarget", { targetId: sesiones.get(sesion) });

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

// Lo que el panel le muestra al colaborador mientras no puede abrir el CRM.
const leerAviso = (cdp, sesion) =>
  evaluar(cdp, sesion, `(() => {
    const visible = (selector) => Boolean(document.querySelector(selector) && !document.querySelector(selector).hidden);
    return {
      titulo: document.querySelector("#titulo")?.textContent ?? null,
      detalle: document.querySelector("#detalle")?.textContent ?? null,
      reintentar: visible("#reintentar"),
      permiso: visible("#dar-permiso"),
      marco: Boolean(document.querySelector("iframe")),
    };
  })()`);

// Clic de verdad (con gesto de usuario, como el de una persona) en el centro de un elemento.
async function clicReal(cdp, sesion, selector) {
  const { x, y } = await evaluar(cdp, sesion, `(() => {
    const r = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
  })()`);
  for (const type of ["mousePressed", "mouseReleased"]) {
    await cdp.enviar("Input.dispatchMouseEvent", { type, x, y, button: "left", clickCount: 1 }, sesion);
  }
}

// El acceso a sitios que la persona puede quitarle a la extensión desde chrome://extensions o el
// menú de extensiones. Se cambia desde esa página, que tiene la API.
const accesoASitios = (cdp, ajustes, id, hostAccess) =>
  evaluar(cdp, ajustes, `chrome.developerPrivate.updateExtensionConfiguration(${JSON.stringify({ extensionId: id, hostAccess })})`);
const avisoQue = (cdp, sesion, titulo, timeout = 10000) =>
  hasta(async () => {
    const aviso = await leerAviso(cdp, sesion);
    return aviso.titulo === titulo ? aviso : undefined;
  }, { timeout });

// El marco hacia el CRM es de otro sitio que su padre, así que Chrome lo aísla en su propio
// objetivo de CDP («iframe»): si aparece y carga, la regla dejó pasarlo. El objetivo existe desde
// que empieza la navegación, así que se espera a que el documento de esa ruta termine de cargar.
async function marcoDelCrm(cdp, crm, ruta, timeout = 15000) {
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
  }, { timeout });
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
  let uvicorn, chrome, otro, mantenimiento, cdp;
  try {
    const siembra = spawnSync(PYTHON, [path.join(REPO, "scripts", "qa_crm_seed.py")], { cwd: REPO, env, encoding: "utf8" });
    if (siembra.status !== 0) throw new Error(`la siembra falló:\n${siembra.stdout}${siembra.stderr}`);

    const puerto = await puertoLibre();
    const crm = `http://127.0.0.1:${puerto}`;
    const salida = path.join(dir, "dist");
    const armado = spawnSync(PYTHON, [path.join(EXT, "construir.py"), "--crm", crm, "--navegador", "chrome", "--salida", salida], { encoding: "utf8" });
    if (armado.status !== 0) throw new Error(`construir.py falló:\n${armado.stderr}`);
    // Se instala el zip que se entrega (E5), no la carpeta. Chrome no instala un zip fuera de su
    // tienda: se descomprime, como lo hará quien lo instale a mano, y se borra la carpeta armada para
    // que no tape lo que al zip le falte.
    const zips = fs.readdirSync(salida).filter((n) => /^ext-cse-chrome-.+\.zip$/.test(n));
    if (zips.length !== 1) throw new Error(`construir.py dejó ${zips.length} zips de Chrome: ${zips.join(", ")}`);
    const instalada = path.join(dir, "instalada");
    const extraido = spawnSync(PYTHON, ["-m", "zipfile", "-e", path.join(salida, zips[0]), instalada], { encoding: "utf8" });
    if (extraido.status !== 0) throw new Error(`no se pudo descomprimir ${zips[0]}:\n${extraido.stderr}`);
    fs.rmSync(path.join(salida, "chrome"), { recursive: true, force: true });

    // El CRM «en mantenimiento»: hasta que se levanta el bot, su puerto contesta 503 y cuenta las
    // consultas a /health, para ver que el panel lo dice y que el botón Reintentar vuelve a preguntar.
    let consultas = 0;
    mantenimiento = http.createServer((req, res) => {
      if (req.url.startsWith("/health")) consultas += 1;
      res.writeHead(503, { "content-type": "text/plain; charset=utf-8" });
      res.end("en mantenimiento");
    });
    await new Promise((ok) => mantenimiento.listen(puerto, "127.0.0.1", ok));

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

    const { id } = await cdp.enviar("Extensions.loadUnpacked", { path: instalada });
    criterio(`Chrome instala el zip ${zips[0]}`, Boolean(id), id);
    const urlPanel = `chrome-extension://${id}/panel.html`;

    // 2. La regla, leída en el fondo ANTES de abrir el panel: así se ve que la puso onInstalled y no
    // el panel (que la pide igual: eso se juzga en el punto 5).
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

    // 3. Sin el CRM: el panel lo dice con el código que recibió y no abre el marco; el botón vuelve
    // a preguntar, y cuando el puerto deja de contestar el aviso cambia sin que nadie toque nada.
    const panel = await pestana(cdp, urlPanel);
    let aviso = await avisoQue(cdp, panel, "El CRM no está disponible");
    criterio("con el CRM en mantenimiento, el panel lo dice con su código y no abre el marco",
      Boolean(aviso?.detalle.includes("503") && aviso.reintentar && !aviso.marco), JSON.stringify(aviso ?? (await leerAviso(cdp, panel))));
    // Tres clics seguidos: el reintento automático (cada 5 s) suma a lo más una consulta.
    const antes = consultas;
    for (let i = 0; i < 3; i++) {
      await evaluar(cdp, panel, `document.querySelector("#reintentar").click()`);
      await esperar(400);
    }
    criterio("el botón Reintentar vuelve a consultar al CRM", consultas - antes >= 3, `${consultas - antes} consultas tras 3 clics`);
    mantenimiento.closeAllConnections();
    await new Promise((ok) => mantenimiento.close(ok));
    aviso = await avisoQue(cdp, panel, "No se pudo conectar con el CRM", 12000);
    criterio("si el CRM deja de contestar, el panel cambia el aviso solo",
      Boolean(aviso?.detalle.includes(`127.0.0.1:${puerto}`) && aviso.reintentar && !aviso.marco), JSON.stringify(aviso ?? (await leerAviso(cdp, panel))));

    uvicorn = spawn(PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(puerto), "--log-level", "warning"],
      { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
    const bitacora = [];
    uvicorn.stdout.on("data", (d) => bitacora.push(String(d)));
    uvicorn.stderr.on("data", (d) => bitacora.push(String(d)));
    if (!(await hasta(async () => (await fetch(`${crm}/login`)).ok, { timeout: 40000 }))) {
      throw new Error(`el bot no atendió /login:\n${bitacora.join("")}`);
    }

    // 4. El mismo panel abre el CRM solo en cuanto vuelve: primero /login, y tras entrar, /crm.
    let marco = await marcoDelCrm(cdp, crm, "/login");
    const login = marco && (await hasta(async () => evaluar(cdp, marco, "Boolean(document.querySelector('#password'))")));
    criterio("al volver el CRM, el panel lo abre solo: sin sesión, el login", Boolean(login));
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
    await cdp.enviar("Page.navigate", { url: urlPanel }, panel);
    marco = await marcoDelCrm(cdp, crm, "/crm");
    const tablero = marco && (await hasta(async () => evaluar(cdp, marco, "Boolean(document.querySelector('#crm'))")));
    criterio("con sesión, el panel abre directo en /crm", Boolean(tablero));

    // E3. «Ir al cliente»: el número como lo pega un asesor de WhatsApp Web abre su conversación en
    // el panel. El envío es otra navegación del marco, iniciada por el CRM: la regla también la deja.
    const enviado = marco && (await evaluar(cdp, marco, `(() => {
      const campo = document.querySelector("#crm-ir-cliente-tel");
      const boton = document.querySelector("#crm-ir-cliente button[type=submit]");
      if (!campo || !boton) return false;
      campo.value = "+52 1 55 0000 0011";
      boton.click();
      return true;
    })()`));
    const conChat = enviado && (await marcoDelCrm(cdp, crm, "/crm?chat="));
    const chat = conChat && (await hasta(async () => evaluar(cdp, conChat, `(() => {
      const nombre = document.querySelector("#crm-chat-name")?.textContent.trim();
      const abierto = document.querySelector("#crm")?.classList.contains("chat-abierto");
      return abierto && nombre ? { nombre, url: location.pathname + location.search } : undefined;
    })()`).catch(() => undefined)));
    criterio("«Ir al cliente» abre en el panel la conversación del número pegado",
      chat?.nombre === "Ana Lucía Ramírez" && chat?.url === `/crm?chat=${ANA}`,
      enviado ? JSON.stringify(chat ?? null) : "el CRM del panel no tiene el campo «Ir al cliente»");

    // E4. La ayuda «¿Folio manual?» también vive en la barra del CRM: dentro del marco abre como
    // modal y, al ancho de un panel lateral (360 px), cabe sin scroll horizontal; Escape (tecla real,
    // que Chrome entrega al marco con el foco) la cierra sin cerrar el chat de atrás.
    const ESTADO_AYUDA = `(() => {
      const d = document.querySelector("#crm-dlg-ayuda-folio");
      const r = d?.open ? d.getBoundingClientRect() : null;
      return {
        abierto: d?.open === true, modal: !!d?.open && d.matches(":modal"),
        pasos: d ? d.querySelectorAll("ol.ayuda-pasos > li").length : 0,
        cabe: !r || (r.left >= 0 && r.right <= document.documentElement.clientWidth),
        scrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
        foco: document.activeElement?.id || null,
        chat: document.querySelector("#crm")?.classList.contains("chat-abierto") ?? false,
      };
    })()`;
    await cdp.enviar("Emulation.setDeviceMetricsOverride", { width: 360, height: 800, deviceScaleFactor: 1, mobile: false }, panel);
    let ayuda = null;
    let trasEscape = null;
    try {
      ayuda = conChat && (await hasta(async () => evaluar(cdp, conChat, `(() => {
        const b = document.querySelector("#crm-btn-ayuda-folio");
        if (!b || !b.getClientRects().length) return undefined;
        if (!document.querySelector("#crm-dlg-ayuda-folio")?.open) { b.focus(); b.click(); }
        return ${ESTADO_AYUDA};
      })()`).catch(() => undefined), { timeout: 8000 }));
      if (ayuda?.abierto) {
        for (const type of ["rawKeyDown", "keyUp"]) {
          await cdp.enviar("Input.dispatchKeyEvent", { type, key: "Escape", code: "Escape", windowsVirtualKeyCode: 27, nativeVirtualKeyCode: 27 }, panel);
        }
        trasEscape = await hasta(async () => {
          const e = await evaluar(cdp, conChat, ESTADO_AYUDA);
          return e.abierto ? undefined : e;
        }, { timeout: 5000 });
      }
    } finally {
      await cdp.enviar("Emulation.clearDeviceMetricsOverride", {}, panel);
    }
    criterio("«¿Folio manual?» abre la ayuda en el panel: modal, 6 pasos y cabe a 360 px",
      Boolean(ayuda?.abierto && ayuda.modal && ayuda.pasos === 6 && ayuda.cabe && !ayuda.scrollX && ayuda.foco === "crm-ayuda-folio-cerrar"),
      JSON.stringify(ayuda ?? "el CRM del panel no tiene el botón «¿Folio manual?»"));
    criterio("Escape cierra la ayuda del panel sin cerrar el chat y el foco vuelve al botón",
      Boolean(trasEscape && trasEscape.chat && trasEscape.foco === "crm-btn-ayuda-folio"), JSON.stringify(trasEscape));

    // 5. Primer arranque: si el panel se abre antes de que onInstalled ponga la regla, se la pide al
    // fondo antes de crear el marco. La regla se borra desde la página del panel, que tiene la API.
    await evaluar(cdp, panel, "chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [1] })");
    const quedan = await evaluar(cdp, panel, "chrome.declarativeNetRequest.getDynamicRules().then((r) => r.length)");
    await cerrar(cdp, panel);
    const repuesto = await pestana(cdp, urlPanel);
    marco = await marcoDelCrm(cdp, crm, "/crm");
    const despues = await evaluar(cdp, repuesto, "chrome.declarativeNetRequest.getDynamicRules().then((r) => r.length)");
    criterio("si falta la regla, el panel la pide al fondo y el marco carga", quedan === 0 && Boolean(marco) && despues === 1,
      `reglas antes del panel: ${quedan}; después: ${despues}; marco: ${marco ? "cargó" : "no cargó"}`);
    // En el primer arranque la piden a la vez onInstalled y el panel: no deben chocar por id repetido.
    await evaluar(cdp, repuesto, "chrome.declarativeNetRequest.updateDynamicRules({ removeRuleIds: [1] })");
    const simultaneos = await evaluar(cdp, repuesto,
      `Promise.all([1, 2, 3].map(() => chrome.runtime.sendMessage({ tipo: "asegurar-regla" })))`);
    const tras = await evaluar(cdp, repuesto, "chrome.declarativeNetRequest.getDynamicRules().then((r) => r.length)");
    criterio("dos pedidos de la regla a la vez no chocan", simultaneos.every((r) => r?.ok) && tras === 1,
      JSON.stringify({ simultaneos, reglas: tras }));
    await cerrar(cdp, repuesto);

    // 6. Sin red: el panel lo dice y no abre el marco; al volver la red abre el CRM solo.
    const sinRed = await pestana(cdp, urlPanel, { sinRed: true });
    aviso = await avisoQue(cdp, sinRed, "Sin conexión a internet");
    criterio("sin red, el panel lo dice y no abre el marco", Boolean(aviso?.reintentar && !aviso.marco),
      JSON.stringify(aviso ?? (await leerAviso(cdp, sinRed))));
    await cdp.enviar("Network.emulateNetworkConditions", { offline: false, ...RED }, sinRed);
    marco = await marcoDelCrm(cdp, crm, "/crm");
    criterio("al volver la red, el panel abre el CRM solo", Boolean(marco));
    await cerrar(cdp, sinRed);

    // 7. Sin permiso de sitio la regla no aplica y /health no contesta: el panel no culpa al CRM,
    // pide el permiso, y vuelve a empezar si se lo quitan con el CRM ya abierto.
    await accesoASitios(cdp, ajustes, id, "ON_CLICK");
    const sinPermiso = await pestana(cdp, urlPanel);
    aviso = await avisoQue(cdp, sinPermiso, "Falta el permiso para conectarse con el CRM");
    criterio("sin permiso de sitio, el panel lo pide en vez de culpar al CRM",
      Boolean(aviso?.permiso && !aviso.reintentar && !aviso.marco && aviso.detalle.includes(`127.0.0.1:${puerto}`)),
      JSON.stringify(aviso ?? (await leerAviso(cdp, sinPermiso))));
    // Sin cabeza, el aviso del navegador no se ve ni se puede aceptar: se comprueba que el botón lo
    // pide (espía sobre la API) y luego se da el permiso como desde el menú de extensiones.
    await evaluar(cdp, sinPermiso, `(() => {
      const original = chrome.permissions.request.bind(chrome.permissions);
      globalThis.pedidos = [];
      chrome.permissions.request = (p) => (pedidos.push(p), original(p));
    })()`);
    await clicReal(cdp, sinPermiso, "#dar-permiso");
    const pedidos = await hasta(async () => {
      const p = await evaluar(cdp, sinPermiso, "globalThis.pedidos");
      return p?.length ? p : undefined;
    }, { timeout: 3000 });
    criterio("el botón Dar permiso le pide al navegador el permiso del CRM",
      JSON.stringify(pedidos) === JSON.stringify([{ origins: [`${crm}/*`] }]), JSON.stringify(pedidos));
    await accesoASitios(cdp, ajustes, id, "ON_ALL_SITES");
    marco = await marcoDelCrm(cdp, crm, "/crm");
    criterio("al dar el permiso, el panel abre el CRM solo", Boolean(marco));
    await accesoASitios(cdp, ajustes, id, "ON_CLICK");
    aviso = await avisoQue(cdp, sinPermiso, "Falta el permiso para conectarse con el CRM");
    criterio("si quitan el permiso con el CRM abierto, el panel lo cierra y lo explica",
      Boolean(aviso?.permiso && !aviso.marco), JSON.stringify(aviso ?? (await leerAviso(cdp, sinPermiso))));
    await accesoASitios(cdp, ajustes, id, "ON_ALL_SITES");
    await cerrar(cdp, sinPermiso);

    // 8. Fuera del panel, nada cambia: el CRM no se deja enmarcar. Los paneles ya están cerrados:
    // su marco al CRM se confundiría con uno que se coló.
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
    mantenimiento?.closeAllConnections();
    mantenimiento?.close();
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
