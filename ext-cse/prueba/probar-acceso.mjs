// Juez del acceso del panel (plan ext-vistas V3), en Chrome real y sin sesión previa:
//   node ext-cse/prueba/probar-acceso.mjs [--usuario=colab1|colab2|jefa1 (admin global)|jefe2 (admin de Puebla)] [--ancho=420] [--alto=900] [--captura=/ruta/acceso-%.png]
// Levanta un bot con la base sembrada de scripts/qa_crm_seed.py, carga la extensión armada contra él y
// recorre: sin sesión (login en el panel, sin barra) → clave mala (sigue en el login) → clave buena
// (aparece la barra) → Mi día con esa sesión → «Salir» (vuelve el login, la sesión se cierra) → entrar
// de nuevo. Termina en 1 si algún paso falla. «%» de --captura se cambia por el nombre del paso.
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

const USUARIO = arg("usuario", "colab1");
const ANCHO = Number(arg("ancho", 420));
const ALTO = Number(arg("alto", 900));
const CAPTURA = arg("captura", path.join(os.tmpdir(), "acceso-%.png"));

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

    // Flujo completo del acceso, todo con el panel ya abierto y SIN sesión previa.
    const fallos = [];
    const paso = async (nombre, ok, detalle) => {
      if (!ok) fallos.push(`${nombre}${detalle ? `: ${detalle}` : ""}`);
      medido.pasos.push({ nombre, ok: Boolean(ok) });
    };
    const foto = async (panel, nombre) => {
      const f = await cdp.enviar("Page.captureScreenshot", { format: "png" }, panel);
      const archivo = CAPTURA.replaceAll("%", nombre);
      fs.writeFileSync(archivo, Buffer.from(f.data, "base64"));
      medido.capturas.push(archivo);
    };
    const medido = { crm, usuario: USUARIO, pasos: [], capturas: [] };
    const marcoDe = async (inicio) => hasta(async () => {
      const { targetInfos } = await cdp.enviar("Target.getTargets");
      return targetInfos.find((t) => t.type === "iframe" && t.url.startsWith(`${crm}${inicio}`));
    });
    const visible = (panel, sel) => evaluar(panel, `(() => { const e = document.querySelector(${JSON.stringify(sel)}); return Boolean(e) && !e.hidden; })()`);

    const panel = await abrir(`chrome-extension://${id}/panel.html`);
    await cdp.enviar("Emulation.setDeviceMetricsOverride", { width: ANCHO, height: ALTO, deviceScaleFactor: 2, mobile: false }, panel);

    // 1. Sin sesión: acceso a la vista, barra oculta.
    const login = await marcoDe("/login");
    await pausa(1000);
    await paso("sin sesión aparece el login en un marco", login);
    await paso("sin sesión la barra no se ve", !(await visible(panel, "#app")) && (await visible(panel, "#acceso")));
    if (login) {
      const { sessionId } = await cdp.enviar("Target.attachToTarget", { targetId: login.targetId, flatten: true });
      const dentro = await evaluar(sessionId, "({menu: Boolean(document.querySelector('.sidebar')), form: Boolean(document.querySelector('#password'))})");
      await paso("el login del marco no trae el menú del ERP", dentro.form && !dentro.menu);
      await foto(panel, "1-login");

      // 2. Clave equivocada: sigue en el login, sin barra.
      await evaluar(sessionId, `(() => { document.querySelector('[name=username]').value = ${JSON.stringify(USUARIO)}; document.querySelector('#password').value = 'mala-clave'; document.querySelector('#password').form.submit(); })()`);
      await pausa(2000);
      await paso("con clave mala no aparece la barra", !(await visible(panel, "#app")));

      // 3. Clave buena: aparece la barra y el login desaparece.
      const login2 = await marcoDe("/login");
      const { sessionId: s2 } = await cdp.enviar("Target.attachToTarget", { targetId: login2.targetId, flatten: true });
      await evaluar(s2, `(() => { document.querySelector('[name=username]').value = ${JSON.stringify(USUARIO)}; document.querySelector('#password').value = ${JSON.stringify(CLAVE)}; document.querySelector('#password').form.submit(); })()`);
    }
    const barra = await hasta(() => visible(panel, "#app"), 15000);
    await paso("con clave buena aparece la barra", barra);
    await paso("al entrar el marco del login ya no está", !(await visible(panel, "#acceso")) && (await evaluar(panel, "document.querySelectorAll('#acceso iframe').length")) === 0);
    const tips = await evaluar(panel, "[...document.querySelectorAll('#barra li button')].map(b => b.dataset.tip)");
    medido.botones = tips.length;
    // V4: la barra solo ofrece lo que el rol puede abrir (colaborador 9, admin de oficina jefe2 13, global jefa1 17).
    const ESPERADOS = { colab1: 9, colab2: 9, jefa1: 17, jefe2: 13 };
    await paso(`la barra de ${USUARIO} trae ${ESPERADOS[USUARIO]} módulos`, tips.length === ESPERADOS[USUARIO], `trae ${tips.length}: ${tips.join(", ")}`);
    await paso("«Usuarios» solo para administradores", tips.includes("Usuarios") === (ESPERADOS[USUARIO] > 9));
    await paso("«Sistema» solo para el administrador global", tips.includes("Sistema") === (ESPERADOS[USUARIO] === 17));
    await foto(panel, "2-barra");

    // 4. Mi día abre dentro de la sesión recién iniciada.
    await evaluar(panel, "[...document.querySelectorAll('#barra li button')].find(b => b.dataset.tip === 'Mi día').click()");
    const dia = await marcoDe("/my");
    await paso("tras entrar Mi día abre con la sesión", dia);

    // 5. Salir: vuelve el login y la barra se oculta.
    await evaluar(panel, "document.querySelector('#salir').click()");
    const otraVez = await hasta(async () => (await visible(panel, "#acceso")) && !(await visible(panel, "#app")), 15000);
    await paso("«Salir» devuelve el login y oculta la barra", otraVez);
    await paso("tras salir ya no queda el marco de Mi día", !(await evaluar(panel, "[...document.querySelectorAll('iframe')].some(f => f.src.includes('/my'))")));
    const sesionCerrada = await evaluar(panel, `fetch(${JSON.stringify(crm + "/sesion")}, {credentials: "include"}).then(r => r.status)`);
    await paso("el servidor ya no reconoce la sesión", sesionCerrada === 401, `/sesion dio ${sesionCerrada}`);
    await foto(panel, "3-salir");

    // 6. Volver a entrar después de salir.
    const login3 = await marcoDe("/login");
    if (login3) {
      const { sessionId: s3 } = await cdp.enviar("Target.attachToTarget", { targetId: login3.targetId, flatten: true });
      await evaluar(s3, `(() => { document.querySelector('[name=username]').value = ${JSON.stringify(USUARIO)}; document.querySelector('#password').value = ${JSON.stringify(CLAVE)}; document.querySelector('#password').form.submit(); })()`);
    }
    await paso("después de salir se puede volver a entrar", await hasta(() => visible(panel, "#app"), 15000));
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
