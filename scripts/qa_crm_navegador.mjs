#!/usr/bin/env node
/**
 * qa_crm_navegador.mjs — QA del CRM Kanban del ERP (/crm) en Chrome headless vía CDP, contra el
 * backend real.
 *
 * static/js/crm.js no tiene pruebas unitarias (el harness de pytest solo revisa que cumpla la
 * separación HTML/CSS/JS); este juez es su verificación: levanta uvicorn con una base temporal
 * sembrada (scripts/qa_crm_seed.py), abre Chrome sin cabeza, entra con la sesión del panel como
 * admin global, colaborador y admin de oficina, y ejecuta los criterios de aceptación del CRM
 * (tablero, drag & drop persistente, pestañas, búsqueda, chat, envío por los endpoints de
 * app/inbox.py, bot, bloqueo, etapas, reordenar etapas arrastrando o con el teclado, tablero
 * personal de cada quien, alta de contacto, alcance por oficina, cajón del folio,
 * folios cerrados y «Mis respuestas», que sustituye a /replies) más una pasada responsiva. Imprime una línea ✅/❌ por criterio (⚠️ = observación) y termina con código 1 si
 * hay algún ❌. Node 22 sin dependencias npm; requiere google-chrome.
 *
 * Uso: node scripts/qa_crm_navegador.mjs [--verbose] [--keep] [--capturas=DIR]
 *   --capturas=DIR  guarda capturas PNG (tablero, chat, diálogo y anchos de 900 y 400 px) en DIR
 * Variables: CHROME_BIN (def. /usr/bin/google-chrome) y QA_PYTHON (def. .venv/bin/python). La imagen
 * de CI de Jenkins (ci/Dockerfile) las apunta a su Chromium y a su Python, que no viven en un venv.
 */

import { spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const AQUI = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(AQUI, "..");
const PYTHON = process.env.QA_PYTHON || path.join(REPO, ".venv", "bin", "python");
const SEED = path.join(AQUI, "qa_crm_seed.py");
const CHROME = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const CLAVE = "clave-qa-123";
const ANA = "525500000011";
const BETO = "525500000012";
const DIEGO = "525500000013";
const ELENA = "525500000014";
// Filtro de la pestaña «Folios cerrados» en static/js/crm.js.
const CERRADAS = "__cerradas__";
const CARLA = "522200000021";
const ALTA = { name: "Prueba Alta QA", phone: "+52 55 0000 9999", canonico: "525500009999" };
// Peticiones cuyo 404 es esperado y no cuenta como fallo.
const RUTAS_IGNORADAS = ["/favicon.ico", "/media/qa-no-existe.jpg"];

const args = process.argv.slice(2);
const verbose = args.includes("--verbose") || args.includes("-v");
const keep = args.includes("--keep");
const CAPTURAS = (args.find((a) => a.startsWith("--capturas=")) ?? "").slice("--capturas=".length) || null;
if (CAPTURAS) fs.mkdirSync(CAPTURAS, { recursive: true });

// ────────────────────────────────────────────────────────────────────────────
// Utilidades
// ────────────────────────────────────────────────────────────────────────────

const dormir = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitFor(fn, { timeout = 5000, interval = 100, label = "la condición" } = {}) {
  const limite = Date.now() + timeout;
  for (;;) {
    const valor = await fn();
    if (valor) return valor;
    if (Date.now() >= limite) throw new Error(`Tiempo agotado (${timeout} ms) esperando ${label}`);
    await dormir(interval);
  }
}

function puertoLibre() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.unref();
    s.on("error", reject);
    s.listen(0, "127.0.0.1", () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

function httpGet(url) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = http.request(
      { host: u.hostname, port: u.port, path: u.pathname + u.search, method: "GET", agent: false, timeout: 10000 },
      (res) => {
        const trozos = [];
        res.on("data", (c) => trozos.push(c));
        res.on("end", () => {
          const text = Buffer.concat(trozos).toString("utf8");
          let json = null;
          try { json = JSON.parse(text); } catch { /* no era JSON */ }
          resolve({ status: res.statusCode, json, text });
        });
      },
    );
    req.on("timeout", () => req.destroy(new Error(`timeout en GET ${url}`)));
    req.on("error", reject);
    req.end();
  });
}

function describirObjeto(o) {
  if (!o) return "";
  if (o.value !== undefined) return typeof o.value === "string" ? o.value : JSON.stringify(o.value);
  return o.description ?? o.unserializableValue ?? o.type ?? "";
}

function ubicacion(stackTrace) {
  const f = stackTrace?.callFrames?.[0];
  return f && f.url ? ` (${f.url}:${f.lineNumber + 1}:${f.columnNumber + 1})` : "";
}

function describirExcepcion(d) {
  if (!d) return "excepción sin detalle";
  const e = d.exception;
  const texto = String(e?.description ?? e?.value ?? d.text ?? "excepción").split("\n")[0];
  const donde = d.url ? ` (${d.url}:${(d.lineNumber ?? 0) + 1}:${(d.columnNumber ?? 0) + 1})` : ubicacion(d.stackTrace);
  return `${texto}${donde}`;
}

function mismoConjunto(a, b) {
  const sa = [...a].map(String).sort();
  const sb = [...b].map(String).sort();
  return sa.length === sb.length && sa.every((x, i) => x === sb[i]);
}

// ────────────────────────────────────────────────────────────────────────────
// Cliente CDP mínimo (WebSocket global de Node 22)
// ────────────────────────────────────────────────────────────────────────────

class ClienteCDP {
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.siguienteId = 0;
    this.pendientes = new Map();
    this.oyentes = new Map();
  }

  conectar() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.url);
      ws.addEventListener("open", () => resolve(), { once: true });
      ws.addEventListener("error", () => reject(new Error(`No se pudo abrir el WebSocket de CDP en ${this.url}`)), { once: true });
      ws.addEventListener("close", () => {
        for (const p of this.pendientes.values()) {
          clearTimeout(p.timer);
          p.reject(new Error(`La conexión CDP se cerró (${p.method})`));
        }
        this.pendientes.clear();
      });
      ws.addEventListener("message", (ev) => this._recibir(ev.data));
      this.ws = ws;
    });
  }

  _recibir(texto) {
    let msg;
    try { msg = JSON.parse(texto); } catch { return; }
    if (msg.id !== undefined) {
      const p = this.pendientes.get(msg.id);
      if (!p) return;
      this.pendientes.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.error) p.reject(new Error(`${p.method}: ${msg.error.message}${msg.error.data ? " — " + msg.error.data : ""}`));
      else p.resolve(msg.result ?? {});
    } else if (msg.method) {
      for (const cb of this.oyentes.get(msg.method) ?? []) {
        try { cb(msg.params ?? {}); } catch (e) { console.error(`Oyente CDP de ${msg.method} falló:`, e); }
      }
    }
  }

  send(method, params = {}, { timeout = 30000 } = {}) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return reject(new Error(`CDP no conectado (${method})`));
      const id = ++this.siguienteId;
      const timer = setTimeout(() => {
        this.pendientes.delete(id);
        reject(new Error(`${method}: sin respuesta en ${timeout} ms`));
      }, timeout);
      this.pendientes.set(id, { resolve, reject, timer, method });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, cb) {
    if (!this.oyentes.has(method)) this.oyentes.set(method, new Set());
    this.oyentes.get(method).add(cb);
    return () => this.oyentes.get(method)?.delete(cb);
  }

  esperarEvento(method, timeout = 15000) {
    return new Promise((resolve, reject) => {
      let timer = null;
      const quitar = this.on(method, (p) => {
        clearTimeout(timer);
        quitar();
        resolve(p);
      });
      timer = setTimeout(() => {
        quitar();
        reject(new Error(`${method}: no llegó en ${timeout} ms`));
      }, timeout);
    });
  }

  /** Ejecuta `fn(...args)` en la página. `fn` debe ser autocontenida (se serializa con toString). */
  async evaluate(fn, ...args) {
    const expression = `(${fn.toString()})(${args.map((a) => JSON.stringify(a)).join(", ")})`;
    const r = await this.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error(`Runtime.evaluate: ${describirExcepcion(r.exceptionDetails)}`);
    return r.result?.value;
  }

  cerrar() {
    try { this.ws?.close(); } catch { /* ya cerrado */ }
  }
}

class Recolector {
  constructor(cdp) {
    this.fallos = [];
    this.avisos = [];
    this.urls = new Map();
    cdp.on("Runtime.exceptionThrown", (p) => {
      this.fallos.push({ origen: "excepción", texto: describirExcepcion(p.exceptionDetails) });
    });
    cdp.on("Runtime.consoleAPICalled", (p) => {
      const texto = (p.args ?? []).map(describirObjeto).join(" ") + ubicacion(p.stackTrace);
      if (p.type === "error" || p.type === "assert") this.fallos.push({ origen: `console.${p.type}`, texto });
      else if (p.type === "warning") this.avisos.push({ origen: "console.warn", texto });
    });
    cdp.on("Log.entryAdded", ({ entry }) => {
      if (!entry) return;
      const texto = `${entry.text}${entry.url ? " — " + entry.url : ""}`;
      if (RUTAS_IGNORADAS.some((r) => (entry.url ?? "").includes(r))) return;
      if (entry.level === "error") this.fallos.push({ origen: `log.${entry.source}`, texto });
      else if (entry.level === "warning") this.avisos.push({ origen: `log.${entry.source}`, texto });
    });
    cdp.on("Network.requestWillBeSent", (p) => {
      this.urls.set(p.requestId, `${p.request?.method ?? ""} ${p.request?.url ?? ""}`.trim());
    });
    cdp.on("Network.responseReceived", (p) => {
      const r = p.response ?? {};
      if (r.status >= 400 && !RUTAS_IGNORADAS.some((x) => (r.url ?? "").includes(x))) {
        const peticion = this.urls.get(p.requestId) ?? r.url;
        this.fallos.push({ origen: "red", texto: `HTTP ${r.status} en ${peticion} (${p.type})` });
      }
    });
    cdp.on("Network.loadingFinished", (p) => this.urls.delete(p.requestId));
    cdp.on("Network.loadingFailed", (p) => {
      const peticion = this.urls.get(p.requestId) ?? p.requestId;
      this.urls.delete(p.requestId);
      if (!p.canceled && p.errorText !== "net::ERR_ABORTED") {
        this.avisos.push({ origen: "red", texto: `carga fallida (${p.errorText}) en ${peticion} (${p.type})` });
      }
    });
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Procesos
// ────────────────────────────────────────────────────────────────────────────

function capturarSalida(proc, etiqueta, maxLineas = 120) {
  const lineas = [];
  const alimentar = (trozo) => {
    for (const l of trozo.toString("utf8").split(/\r?\n/)) {
      if (!l) continue;
      lineas.push(l);
      if (lineas.length > maxLineas) lineas.shift();
      if (verbose) console.log(`   [${etiqueta}] ${l}`);
    }
  };
  proc.stdout?.on("data", alimentar);
  proc.stderr?.on("data", alimentar);
  return lineas;
}

async function matarProceso(proc) {
  if (!proc || proc.exitCode !== null || proc.signalCode !== null) return;
  const salio = new Promise((r) => proc.once("exit", r));
  try { proc.kill("SIGTERM"); } catch { return; }
  const aTiempo = await Promise.race([salio.then(() => true), dormir(2500).then(() => false)]);
  if (!aTiempo) {
    try { proc.kill("SIGKILL"); } catch { /* ya murió */ }
    await Promise.race([salio, dormir(1500)]);
  }
}

async function lanzarChrome(perfil) {
  if (!fs.existsSync(CHROME)) throw new Error(`No se encontró Chrome en ${CHROME}`);
  fs.mkdirSync(perfil, { recursive: true });
  const puerto = await puertoLibre();
  const flags = [
    "--headless=new", "--disable-gpu", "--no-sandbox", `--user-data-dir=${perfil}`,
    `--remote-debugging-port=${puerto}`, "--no-first-run", "--no-default-browser-check",
    "--disable-extensions", "--disable-background-networking", "--disable-sync",
    "--disable-features=Translate,BackForwardCache", "--remote-allow-origins=*",
    "--window-size=1400,900", "about:blank",
  ];
  const proc = spawn(CHROME, flags, { stdio: ["ignore", "pipe", "pipe"] });
  const salida = capturarSalida(proc, "chrome");
  let terminado = null;
  proc.on("exit", (code, signal) => (terminado = { code, signal }));
  proc.on("error", (e) => (terminado = { error: e }));
  const base = `http://127.0.0.1:${puerto}`;
  const lista = await waitFor(async () => {
    if (terminado) throw new Error(`Chrome terminó antes de abrir el puerto de depuración\n${salida.join("\n")}`);
    try {
      const r = await httpGet(`${base}/json/list`);
      return r.status === 200 && Array.isArray(r.json) ? r.json : null;
    } catch { return null; }
  }, { timeout: 20000, interval: 150, label: "el puerto de depuración de Chrome" });
  const objetivo = lista.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
  if (!objetivo) throw new Error("Chrome no expone ninguna pestaña por CDP");
  return { proc, wsUrl: objetivo.webSocketDebuggerUrl };
}

async function lanzarServidor(env, puerto) {
  const proc = spawn(PYTHON, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(puerto), "--log-level", "warning"],
    { cwd: REPO, env, stdio: ["ignore", "pipe", "pipe"] });
  const salida = capturarSalida(proc, "uvicorn");
  let terminado = null;
  proc.on("exit", (code, signal) => (terminado = { code, signal }));
  proc.on("error", (e) => (terminado = { error: e }));
  const base = `http://127.0.0.1:${puerto}`;
  await waitFor(async () => {
    if (terminado) throw new Error(`uvicorn terminó antes de atender\n${salida.join("\n")}`);
    try {
      const r = await httpGet(`${base}/login`);
      return r.status === 200 ? true : null;
    } catch { return null; }
  }, { timeout: 40000, interval: 250, label: "que uvicorn atienda /login" });
  return { proc, base, salida };
}

// ────────────────────────────────────────────────────────────────────────────
// Funciones que corren DENTRO de la página (autocontenidas)
// ────────────────────────────────────────────────────────────────────────────

const enPagina = {
  api: async (ruta) => {
    const r = await fetch(ruta, { headers: { Accept: "application/json" }, credentials: "same-origin" });
    return { status: r.status, json: await r.json().catch(() => null) };
  },

  tablero: () => ({
    columnas: document.querySelectorAll("#crm-board section.column").length,
    tarjetas: document.querySelectorAll("#crm-board article.card").length,
    tabs: [...document.querySelectorAll("#crm-tabs button.tab")].map((b) => ({
      filter: b.dataset.filter, nombre: b.querySelector(".tab-name")?.textContent.trim(), conteo: b.querySelector(".tab-count")?.textContent.trim(),
    })),
    bandeja: [...document.querySelectorAll("#crm-tray-list li.tray-item")].map((li) => li.dataset.contactId),
    conteoBandeja: document.querySelector("#crm-tray-count")?.textContent.trim(),
    nuevaEtapa: !!document.querySelector("#crm-btn-new-column"),
    menus: document.querySelectorAll("#crm-board .column-menu-btn").length,
    altas: document.querySelectorAll("#crm-board .btn-add-contact").length,
    miembrosTira: document.querySelectorAll("#crm-miembros .miembro").length,
    tiraVisible: (() => { const n = document.querySelector("#crm-miembros"); return !!n && getComputedStyle(n).display !== "none"; })(),
    vacioBandeja: document.querySelector("#crm-tray-list li.tray-empty")?.textContent.trim() ?? null,
    crmKanban: !!(window.CRMKanban && typeof window.CRMKanban.reload === "function"),
    openChat: typeof window.openChat === "function",
    cargando: !!document.querySelector("#crm-board .board-loading"),
  }),

  columnasDOM: () =>
    [...document.querySelectorAll("#crm-board section.column")].map((s) => ({
      id: s.dataset.columnId ?? null,
      nombre: (s.querySelector("h2.column-name")?.textContent ?? "").replace(/\s+/g, " ").trim(),
      conteo: (s.querySelector("span.column-count")?.textContent ?? "").trim(),
      color: s.style.getPropertyValue("--col-color"),
      tarjetas: [...s.querySelectorAll("article.card")].map((c) => c.dataset.contactId ?? null),
      menu: !!s.querySelector(".column-menu-btn"),
    })),

  tarjetas: () =>
    [...document.querySelectorAll("#crm-board article.card[data-contact-id]")].map((card) => {
      const q = (sel) => card.querySelector(sel);
      return {
        id: card.dataset.contactId,
        columna: card.closest("section.column")?.dataset.columnId ?? null,
        avatar: q(".avatar")?.textContent ?? null,
        nombre: q(".card-name")?.textContent ?? null,
        telefono: q(".card-phone")?.textContent ?? null,
        noLeidos: q(".card-unread")?.textContent.trim() ?? null,
        preview: q(".card-preview")?.textContent.trim() ?? null,
        ticks: q(".card-preview .ticks")?.textContent ?? null,
        ticksRead: !!q(".card-preview .ticks.read"),
        tiempo: q(".card-time")?.textContent.trim() ?? null,
        badges: [...card.querySelectorAll(".card-badges .chip")].map((c) => c.textContent.trim()),
        draggable: card.draggable === true,
        role: card.getAttribute("role"),
        aria: card.getAttribute("aria-label"),
        selects: card.querySelectorAll("select, .card-move").length,
        wa: q(".card-wa") ? {
          oculto: q(".card-wa").getAttribute("aria-hidden") === "true",
          svg: !!q(".card-wa > svg[focusable='false']"),
          enPie: q(".card-wa").parentElement?.classList.contains("card-bottom") ?? false,
          nuevos: q(".card-wa").classList.contains("has-unread"),
        } : null,
      };
    }),

  /* El ícono de WhatsApp de cada tarjeta del tablero: sus clases, las animaciones que corren
     DE VERDAD (getAnimations con subtree incluye la onda del ::before; sin caja —tablero
     oculto o «reducir movimiento»— no hay ninguna), cuándo acaban y su color sobre la tarjeta. */
  iconosWa: () =>
    [...document.querySelectorAll("#crm-board article.card[data-contact-id]")].map((card) => {
      const w = card.querySelector(".card-wa");
      if (!w) return { id: card.dataset.contactId, existe: false };
      const anims = w.getAnimations({ subtree: true }).filter((a) => a.playState === "running");
      const fines = anims.map((a) => a.effect.getComputedTiming().endTime);
      const restantes = anims.map((a) => {
        const t = a.effect.getComputedTiming();
        return t.endTime - (t.localTime ?? 0);
      });
      return {
        id: card.dataset.contactId,
        existe: true,
        nuevos: w.classList.contains("has-unread"),
        timbre: w.classList.contains("is-ringing"),
        corriendo: anims.map((a) => a.animationName),
        finitas: fines.every((f) => Number.isFinite(f)),
        finMs: fines.length ? Math.max(...fines.filter((f) => Number.isFinite(f)), 0) : 0,
        restanteMs: restantes.length ? Math.round(Math.max(...restantes.filter((f) => Number.isFinite(f)), 0)) : 0,
        color: getComputedStyle(w).color,
        fondo: getComputedStyle(card).backgroundColor,
      };
    }),

  columnaDeTarjeta: (id) =>
    document.querySelector(`#crm-board article.card[data-contact-id="${id}"]`)?.closest("section.column")?.dataset.columnId ?? null,

  soltar: (contactId, columnId) => {
    const card = document.querySelector(`#crm-board article.card[data-contact-id="${contactId}"]`);
    const col = document.querySelector(`#crm-board section.column[data-column-id="${columnId}"]`);
    if (!card) return { error: `no existe article.card[data-contact-id="${contactId}"]` };
    if (!col) return { error: `no existe section.column[data-column-id="${columnId}"]` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    const draggable = card.draggable === true;
    const dt = new DataTransfer();
    const disparar = (tipo, destino) => destino.dispatchEvent(new DragEvent(tipo, { bubbles: true, cancelable: true, dataTransfer: dt }));
    disparar("dragstart", card);
    const arrastrando = card.classList.contains("is-dragging");
    const dato = dt.getData("text/plain");
    disparar("dragenter", col);
    const dragoverCancelado = !disparar("dragover", col);
    const resaltada = col.classList.contains("is-over");
    const dropCancelado = !disparar("drop", col);
    disparar("dragend", card);
    return { ok: true, draggable, arrastrando, dato, resaltada, dragoverCancelado, dropCancelado, limpia: !col.classList.contains("is-over") };
  },

  /* Arrastre nativo de la CABECERA de `columnId` hasta el borde `lado` ("antes" | "despues")
     de `destinoId`, con coordenadas reales: crm.js decide el hueco por el punto medio de cada
     columna, no por el elemento que recibe el evento. */
  arrastrarColumna: (columnId, destinoId, lado) => {
    const board = document.querySelector("#crm-board");
    const col = board?.querySelector(`section.column[data-column-id="${columnId}"]`);
    const destino = board?.querySelector(`section.column[data-column-id="${destinoId}"]`);
    if (!col) return { error: `no existe section.column[data-column-id="${columnId}"]` };
    if (!destino) return { error: `no existe section.column[data-column-id="${destinoId}"]` };
    const header = col.querySelector(".column-header");
    if (!header) return { error: `la columna ${columnId} no tiene .column-header` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    const r = destino.getBoundingClientRect();
    const x = lado === "antes" ? r.left + 5 : r.right - 5;
    const y = r.top + 20;
    const dt = new DataTransfer();
    const disparar = (tipo, blanco) =>
      blanco.dispatchEvent(new DragEvent(tipo, { bubbles: true, cancelable: true, dataTransfer: dt, clientX: x, clientY: y }));
    const draggable = header.draggable === true;
    disparar("dragstart", header);
    const atenuada = col.classList.contains("is-col-dragging");
    disparar("dragenter", destino);
    const dragoverCancelado = !disparar("dragover", destino);
    const marcada = board.querySelector(".column.is-drop-before, .column.is-drop-after");
    const marca = marcada
      ? { id: marcada.dataset.columnId, lado: marcada.classList.contains("is-drop-before") ? "antes" : "despues" }
      : null;
    const dropCancelado = !disparar("drop", destino);
    disparar("dragend", header);
    const restos = board.querySelector(".column.is-drop-before, .column.is-drop-after, .column.is-col-dragging");
    return { ok: true, draggable, atenuada, dragoverCancelado, marca, dropCancelado, limpia: !restos };
  },

  foco: () => {
    const a = document.activeElement;
    return { clase: a?.className ?? null, columna: a?.closest?.("section.column")?.dataset.columnId ?? null };
  },

  /* Bitácora de #crm-toast desde ahora: cada aviso que aparece, aunque otro lo pise antes de
     que el sondeo de esperarToast (cada 80 ms) lo alcance a leer. */
  grabarToasts: () => {
    const t = document.querySelector("#crm-toast");
    window.__qaToastObs?.disconnect();
    window.__qaToasts = [];
    if (!t) return;
    const t0 = performance.now();
    const anotar = () => window.__qaToasts.push(
      `${Math.round(performance.now() - t0)}ms ${t.classList.contains("is-visible") ? "◉" : "○"} ${t.textContent.trim()}`);
    window.__qaToastObs = new MutationObserver(anotar);
    window.__qaToastObs.observe(t, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ["class"] });
  },

  toast: () => {
    const t = document.querySelector("#crm-toast");
    return t && t.classList.contains("is-visible") ? t.textContent.trim() : null;
  },

  ocultarToast: () => { document.querySelector("#crm-toast")?.classList.remove("is-visible"); return true; },

  pulsarTab: (filtro) => {
    const b = document.querySelector(`#crm-tabs button.tab[data-filter="${filtro}"]`);
    if (!b) return false;
    b.click();
    return true;
  },

  miembros: () =>
    [...document.querySelectorAll("#crm-miembros .miembro")].map((b) => ({
      username: b.dataset.username,
      nombre: b.querySelector(".miembro-nombre")?.textContent.trim(),
      estado: [...(b.querySelector(".miembro-estado")?.classList ?? [])].find((c) => ["disponible", "ocupado", "ausente"].includes(c)) ?? null,
      conteo: b.querySelector(".miembro-count")?.textContent.trim(),
      activo: b.classList.contains("is-active"),
      aria: b.getAttribute("aria-pressed"),
    })),

  oficinasEnTira: () => [...document.querySelectorAll("#crm-miembros .miembros-oficina")].map((s) => s.textContent.trim()),

  pulsarMiembro: (username) => {
    const b = document.querySelector(`#crm-miembros .miembro[data-username="${username}"]`);
    if (!b) return false;
    b.click();
    return true;
  },

  estadoTab: (filtro) => {
    const b = document.querySelector(`#crm-tabs button.tab[data-filter="${filtro}"]`);
    return {
      existe: !!b,
      clase: !!b && b.classList.contains("is-active"),
      aria: !!b && b.getAttribute("aria-selected") === "true",
      tabindex: b ? b.tabIndex : null,
      items: [...document.querySelectorAll("#crm-tray-list li.tray-item")].map((li) => li.dataset.contactId),
      vacio: !!document.querySelector("#crm-tray-list li.tray-empty"),
      enfocada: !!document.querySelector(`#crm-board section.column[data-column-id="${filtro}"].is-focused`),
    };
  },

  buscar: (texto) => {
    const i = document.querySelector("#crm-search");
    if (!i) return false;
    i.value = texto;
    i.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  },

  abrirTarjeta: (phone, desde) => {
    const sel = desde === "bandeja"
      ? `#crm-tray-list li.tray-item[data-contact-id="${phone}"]`
      : `#crm-board article.card[data-contact-id="${phone}"]`;
    const el = document.querySelector(sel);
    if (!el) return { error: `no existe ${sel}` };
    el.click();
    return { ok: true };
  },

  /* Apaga TODOS los setInterval de la página: es lo que vuelve determinista al criterio 7c.
     Sin sondeo de 8/20 s, lo único que puede refrescar el hilo es el stream de avisos. */
  matarSondeos: () => {
    const alto = setInterval(() => {}, 1000000);
    for (let i = 1; i <= alto; i++) clearInterval(i);
    return alto;
  },

  estadoChat: () => {
    const root = document.querySelector("#crm");
    const chat = document.querySelector("#crm-chat");
    const board = document.querySelector("#crm-board");
    // getClientRects: un elemento cuyo ANCESTRO está oculto conserva su propio display, así que
    // sin esto un botón dentro de una fila con display:none contaría como visible.
    const visible = (el) => !!el && !el.hidden && getComputedStyle(el).display !== "none" && el.getClientRects().length > 0;
    const note = document.querySelector("#crm-window-note");
    return {
      abierto: root.classList.contains("chat-abierto"),
      chatVisible: visible(chat),
      boardVisible: visible(board),
      btnBoardVisible: visible(document.querySelector("#crm-btn-board")),
      nombre: document.querySelector("#crm-chat-name")?.textContent.trim(),
      telefono: document.querySelector("#crm-chat-phone")?.textContent.trim(),
      avatar: document.querySelector("#crm-chat-avatar")?.textContent.trim(),
      badges: [...document.querySelectorAll("#crm-chat-badges .chip")].map((c) => c.textContent.trim()),
      enlaces: [...document.querySelectorAll("#crm-chat-badges a")].map((a) => ({ texto: a.textContent.trim(), href: a.getAttribute("href") })),
      botones: [...document.querySelectorAll("#crm-chat-badges button")].map((b) => ({ clase: b.className, texto: b.textContent.trim() })),
      burbujas: [...document.querySelectorAll("#crm-chat-thread .bubble")].map((b) => ({
        id: b.dataset.messageId,
        dir: b.classList.contains("out") ? "out" : "in",
        texto: b.querySelector(".bubble-text")?.textContent ?? "",
        meta: b.querySelector(".meta")?.textContent.trim() ?? "",
        folio: b.querySelector(".meta .folio")?.textContent.trim() ?? null,
        eliminar: !!b.querySelector(".msg-delete"),
        adjunto: b.querySelector(".bubble-adjunto")?.getAttribute("href") ?? null,
      })),
      estadoHilo: document.querySelector("#crm-chat-thread > p")?.className ?? null,
      composerVisible: visible(document.querySelector("#crm-composer-abierto")),
      // Varios trámites en curso: la fila de chips de la cabecera y el selector del compositor.
      folios: [...document.querySelectorAll("#crm-chat-folios a")].map((a) => ({
        texto: a.textContent.trim(), href: a.getAttribute("href"), ajeno: a.classList.contains("ajeno"),
      })),
      foliosVisible: visible(document.querySelector("#crm-chat-folios")),
      foliosTitulo: document.querySelector("#crm-chat-folios .folios-titulo")?.textContent.trim() ?? null,
      selectorVisible: visible(document.querySelector("#crm-composer-folio")),
      selectorOpciones: [...document.querySelectorAll("#crm-ticket-id option")].map((o) => ({ valor: o.value, texto: o.textContent.trim() })),
      selectorValor: document.querySelector("#crm-ticket-id")?.value ?? null,
      bannerVisible: visible(document.querySelector("#crm-banner-cerrada")),
      siNunca: visible(document.querySelector("#crm-banner-cerrada .si-nunca")),
      siCerrada: visible(document.querySelector("#crm-banner-cerrada .si-cerrada")),
      windowLeft: document.querySelector("#crm-window-left")?.textContent.trim(),
      urgente: !!note && note.classList.contains("urgente"),
      qr: document.querySelectorAll("#crm-qr-bar .qr").length,
      accionForm: document.querySelector("#crm-composer")?.getAttribute("action"),
      errorEnvio: visible(document.querySelector("#crm-send-error")) ? document.querySelector("#crm-send-error").textContent.trim() : null,
      url: location.pathname + location.search,
      bandejaAbierta: [...document.querySelectorAll("#crm-tray-list li.tray-item.is-open")].map((li) => li.dataset.contactId),
      tarjetaAbierta: [...document.querySelectorAll("#crm-board article.card.is-open")].map((c) => c.dataset.contactId),
      foco: document.activeElement ? (document.activeElement.id || document.activeElement.className || document.activeElement.tagName) : null,
      scrollAlFondo: (() => { const h = document.querySelector("#crm-chat-thread"); return h ? h.scrollHeight - h.scrollTop - h.clientHeight < 40 : null; })(),
    };
  },

  escribirYEnviar: (texto) => {
    const ta = document.querySelector("#crm-body");
    const form = document.querySelector("#crm-composer");
    if (!ta || !form) return { error: "no hay compositor" };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    ta.value = texto;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    form.requestSubmit();
    return { ok: true };
  },

  // Elige el folio del selector «Sobre el trámite» antes de enviar (varios trámites en curso).
  elegirFolio: (valor) => {
    const sel = document.querySelector("#crm-ticket-id");
    if (!sel || sel.disabled) return { error: "no hay selector de folio" };
    if (![...sel.options].some((o) => o.value === String(valor))) return { error: `el selector no ofrece ${valor}` };
    sel.value = String(valor);
    sel.dispatchEvent(new Event("change", { bubbles: true }));
    return { ok: true, valor: sel.value };
  },

  enviarVacio: () => {
    const ta = document.querySelector("#crm-body");
    const form = document.querySelector("#crm-composer");
    ta.value = "";
    form.requestSubmit();
    const chip = document.querySelector("#crm-attach-chip");
    return { chip: chip && !chip.hidden ? chip.textContent.trim() : null };
  },

  pulsarQr: () => {
    const b = document.querySelector("#crm-qr-bar .qr");
    if (!b) return { error: "no hay respuestas rápidas" };
    b.click();
    return { texto: document.querySelector("#crm-body").value, esperado: b.dataset.text };
  },

  pulsarEnCabecera: (clase) => {
    const b = document.querySelector(`#crm-chat-badges button.${clase}`);
    if (!b) return { error: `no hay button.${clase} en la cabecera` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    b.click();
    return { ok: true };
  },

  pulsarEliminarUltimo: () => {
    const botones = [...document.querySelectorAll("#crm-chat-thread .msg-delete")];
    if (!botones.length) return { error: "no hay ningún botón «eliminar» en el hilo" };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    botones[botones.length - 1].click();
    return { ok: true, total: botones.length };
  },

  cerrarChat: (como) => {
    const b = como === "tablero" ? document.querySelector("#crm-btn-board") : document.querySelector("#crm-chat .chat-close");
    if (!b) return { error: "no hay botón para cerrar" };
    b.click();
    return { ok: true };
  },

  teclaEscape: () => {
    document.body.focus();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
    return true;
  },

  abrirMenuColumna: (columnId) => {
    const col = document.querySelector(`#crm-board section.column[data-column-id="${columnId}"]`);
    if (!col) return { error: `no existe section.column[data-column-id="${columnId}"]` };
    const boton = col.querySelector("button.column-menu-btn");
    if (!boton) return { error: `la columna ${columnId} no tiene button.column-menu-btn` };
    boton.click();
    const menu = col.querySelector("div.column-menu");
    return { ok: true, abierto: !!menu && !menu.hidden, expandido: boton.getAttribute("aria-expanded") };
  },

  pulsarAccionColumna: (columnId, accion) => {
    const col = document.querySelector(`#crm-board section.column[data-column-id="${columnId}"]`);
    if (!col) return { error: `no existe section.column[data-column-id="${columnId}"]` };
    const boton = col.querySelector(`div.column-menu button.${accion}`);
    if (!boton) return { error: `la columna ${columnId} no tiene button.${accion}` };
    if (boton.disabled) return { error: `button.${accion} está deshabilitado (${boton.title})` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    boton.click();
    return { ok: true };
  },

  renombrarColumna: (columnId, nombre) => {
    const col = document.querySelector(`#crm-board section.column[data-column-id="${columnId}"]`);
    const input = col?.querySelector("input.column-rename");
    if (!input) return { error: `no apareció input.column-rename en la columna ${columnId}` };
    input.value = nombre;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, cancelable: true }));
    return { ok: true };
  },

  pulsarSwatch: (columnId, color) => {
    const col = document.querySelector(`#crm-board section.column[data-column-id="${columnId}"]`);
    const picker = col?.querySelector(".color-picker");
    if (!picker) return { error: `no apareció .color-picker en la columna ${columnId}` };
    const s = picker.querySelector(`.swatch[data-color="${color}"]`);
    if (!s) return { error: `no hay swatch ${color}` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    s.click();
    return { ok: true, swatches: picker.querySelectorAll(".swatch").length };
  },

  abrirDialogoColumna: () => {
    const b = document.querySelector("#crm-btn-new-column");
    if (!b) return { error: "no existe button#crm-btn-new-column" };
    b.click();
    const d = document.querySelector("dialog#crm-dlg-column");
    return { ok: true, abierto: !!d && d.open === true, swatches: d ? d.querySelectorAll(".swatch").length : 0 };
  },

  enviarDialogoColumna: (nombre) => {
    const d = document.querySelector("dialog#crm-dlg-column");
    const input = d?.querySelector("#crm-column-name");
    const form = d?.querySelector("form");
    if (!input || !form) return { error: "el diálogo no tiene input o form" };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    input.value = nombre;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    form.requestSubmit();
    return { ok: true };
  },

  dialogoAbierto: () => document.querySelector("dialog#crm-dlg-column")?.open === true,

  altaContacto: (columnId, nombre, telefono) => {
    const col = document.querySelector(`#crm-board section.column[data-column-id="${columnId}"]`);
    if (!col) return { error: `no existe section.column[data-column-id="${columnId}"]` };
    const boton = col.querySelector("button.btn-add-contact");
    if (!boton) return { error: `la columna ${columnId} no tiene button.btn-add-contact` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    boton.click();
    const form = col.querySelector("form.add-contact-form");
    if (!form) return { error: "no hay form.add-contact-form" };
    const visible = !form.hidden && getComputedStyle(form).display !== "none";
    const iNombre = form.querySelector("input[name=name]");
    const iTelefono = form.querySelector("input[name=phone]");
    for (const [input, valor] of [[iNombre, nombre], [iTelefono, telefono]]) {
      input.value = valor;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    form.requestSubmit();
    return { ok: true, visible };
  },

  pulsarToggleTray: () => {
    const b = document.querySelector("#crm-btn-toggle-tray");
    b.click();
    const tray = document.querySelector("#crm-tray");
    return { colapsada: tray.classList.contains("is-collapsed"), texto: b.textContent.trim(), aria: b.getAttribute("aria-expanded") };
  },

  login: (usuario, clave) => {
    document.querySelector("#username").value = usuario;
    document.querySelector("#password").value = clave;
    document.querySelector('form[action="/login"]').submit();
    return true;
  },

  ruta: () => location.pathname + location.search,

  // ---- Cajón del folio (static/js/crm-panel.js) ----
  panelEstado: () => {
    // visibility: un elemento dentro del cajón cerrado conserva su display y
    // sus rects, así que sin mirar visibility todo contaría como visible.
    const vis = (el) => {
      if (!el || el.hidden) return false;
      const cs = getComputedStyle(el);
      return cs.display !== "none" && cs.visibility !== "hidden" && el.getClientRects().length > 0;
    };
    const panel = document.querySelector("#crm-panel");
    const velo = document.querySelector("#crm-panel-scrim");
    const chat = document.querySelector("#crm-chat");
    const activo = document.activeElement;
    const secciones = ["gestion", "etiquetas", "notas", "cobro", "seguimiento", "transferir"];
    const num = (b) => {
      const n = b.querySelector(".accion-num");
      return n && !n.hidden ? n.textContent.trim() : null;
    };
    return {
      abierto: document.querySelector("#crm").classList.contains("panel-abierto"),
      panelVisible: vis(panel),
      cargado: !!panel && panel.hasAttribute("data-cargado"),
      inerte: !!panel && panel.hasAttribute("inert"),
      titulo: document.querySelector("#crm-panel-title")?.textContent.trim() ?? null,
      sub: document.querySelector("#crm-panel-sub")?.textContent.trim() ?? null,
      sinFolio: vis(document.querySelector("#crm-panel-sinfolio")),
      error: vis(document.querySelector("#crm-panel-error")),
      foco: activo ? (activo.id || activo.className || activo.tagName) : null,
      velo: (() => {
        if (!velo) return null;
        const cs = getComputedStyle(velo);
        return cs.display === "none" ? "oculto" : cs.visibility;
      })(),
      anchoPanel: panel ? Math.round(panel.getBoundingClientRect().width) : null,
      anchoChat: chat ? Math.round(chat.getBoundingClientRect().width) : null,
      empuje: chat ? getComputedStyle(chat).paddingRight : null,
      // Solo las píldoras del cajón: «Estrategia» comparte el riel pero no es una sección.
      riel: [...document.querySelectorAll("#crm-acciones .accion[data-seccion]")].map((b) => ({
        seccion: b.dataset.seccion,
        titulo: b.dataset.titulo,
        expandido: b.getAttribute("aria-expanded") === "true",
        deshabilitado: b.disabled,
        num: num(b),
      })),
      seccionVisible: secciones.filter((s) => vis(document.querySelector("#crm-sec-" + s))),
      folio: document.querySelector("#crm-g-folio")?.textContent.trim() ?? null,
      badges: [...document.querySelectorAll("#crm-g-badges .badge")].map((b) => b.textContent.trim()),
      estados: [...document.querySelectorAll("#crm-g-estados button")].map((b) => ({
        estado: b.dataset.estado,
        actual: b.getAttribute("aria-current") === "true",
        deshabilitado: b.disabled,
      })),
      estadoTile: vis(document.querySelector("#crm-g-estado-tile")),
      gestionBotones: ["crm-g-tomar", "crm-g-terminar", "crm-g-soltar", "crm-g-agente"]
        .filter((id) => vis(document.getElementById(id))),
      gAjeno: vis(document.querySelector("#crm-g-ajeno")),
      verFolio: document.querySelector("#crm-g-ver")?.getAttribute("href") ?? null,
      etiquetas: [...document.querySelectorAll("#crm-e-chips .chip-et")]
        .map((c) => c.querySelector(".chip-et-nombre").textContent.trim()),
      etiquetaForm: vis(document.querySelector("#crm-e-form")),
      notasTramite: document.querySelector("#crm-n-tramite")?.textContent.trim() ?? "",
      notaForm: vis(document.querySelector("#crm-n-form")),
      nAjeno: vis(document.querySelector("#crm-n-ajeno")),
      notasCliente: [...document.querySelectorAll("#crm-n-cliente .panel-nota-texto")]
        .map((p) => p.textContent.trim()),
      cobros: [...document.querySelectorAll("#crm-c-lista .panel-cobro")].map((c) => ({
        badge: c.querySelector(".cobro-badge")?.textContent.trim(),
        monto: c.querySelector(".cobro-monto")?.textContent.trim(),
        pie: c.querySelector(".cobro-pie")?.textContent.trim(),
        acciones: [...c.querySelectorAll(".cobro-acciones > *")]
          .filter((b) => !b.hidden).map((b) => b.textContent.trim()),
      })),
      cobroForm: vis(document.querySelector("#crm-c-form")),
      sinStripe: vis(document.querySelector("#crm-c-sin-stripe")),
      seguimientos: [...document.querySelectorAll("#crm-s-lista .panel-seg")]
        .map((p) => p.textContent.replace(/\s+/g, " ").trim()),
      transferForm: vis(document.querySelector("#crm-t-form")),
      companeros: [...document.querySelectorAll("#crm-t-user option")].map((o) => o.value),
      tAjeno: vis(document.querySelector("#crm-t-ajeno")),
    };
  },

  panelPulsar: (seccion) => {
    const b = document.querySelector(`#crm-acciones .accion[data-seccion="${seccion}"]`);
    if (!b) return { error: `no existe la píldora «${seccion}»` };
    if (b.disabled) return { error: `la píldora «${seccion}» está deshabilitada` };
    b.click();
    return { ok: true };
  },

  panelCerrar: (como) => {
    const sel = como === "velo" ? "#crm-panel-scrim" : "#crm-panel-close";
    const b = document.querySelector(sel);
    if (!b) return { error: `no existe ${sel}` };
    b.click();
    return { ok: true };
  },

  // ---- «Estrategia» (crm-estrategia.js) ----
  estrategiaEstado: () => {
    const vis = (el) => !!el && !el.hidden && getComputedStyle(el).display !== "none" && el.getClientRects().length > 0;
    const boton = document.querySelector("#crm-btn-estrategia");
    const menu = document.querySelector("#crm-estrategia");
    const activo = document.activeElement;
    const num = document.querySelector("#crm-estrategia-lista .estrategia-num");
    const cs = num ? getComputedStyle(num) : null;
    const rn = num ? num.getBoundingClientRect() : null;
    const rm = menu ? menu.getBoundingClientRect() : null;
    const rb = boton ? boton.getBoundingClientRect() : null;
    const sigue = (a, b) => !!a && !!b && (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
      && a.getBoundingClientRect().left < b.getBoundingClientRect().left;
    return {
      boton: boton ? {
        texto: boton.textContent.replace(/\s+/g, " ").trim().split(" ").pop(),
        antes: boton.previousElementSibling?.dataset.seccion ?? null,
        enRiel: !!boton.closest("#crm-acciones"),
        seccion: boton.dataset.seccion ?? null,
        deshabilitado: boton.disabled || boton.getAttribute("aria-disabled") === "true",
        expandido: boton.getAttribute("aria-expanded") === "true",
      } : null,
      abierto: vis(menu),
      chatAbierto: document.querySelector("#crm").classList.contains("chat-abierto"),
      cajonAbierto: document.querySelector("#crm").classList.contains("panel-abierto"),
      foco: activo ? (activo.id || activo.className || activo.tagName) : null,
      opciones: [...document.querySelectorAll("#crm-estrategia-lista .estrategia-opcion")].map((li) => {
        const c = li.querySelector(".estrategia-check");
        const nombre = li.querySelector(".estrategia-nombre");
        const n = li.querySelector(".estrategia-num");
        return {
          id: li.dataset.etapaId,
          tipo: c?.type ?? null,
          nombre: nombre?.textContent ?? null,
          num: n?.textContent ?? null,
          // Lo que lee un lector de pantalla en la etiqueta: nombre, número y «chats».
          leido: li.querySelector("label")?.textContent.replace(/\s+/g, " ").trim() ?? null,
          marcada: !!c && c.checked,
          bloqueada: c?.getAttribute("aria-disabled") === "true",
          enfocada: !!c && activo === c,
          // Casilla → nombre → contador, en el DOM y en pantalla.
          orden: sigue(c, nombre) && sigue(nombre, n),
        };
      }),
      circulo: cs ? {
        fondo: cs.backgroundColor, color: cs.color, radio: parseFloat(cs.borderTopLeftRadius),
        ancho: rn.width, alto: rn.height, texto: num.textContent,
      } : null,
      menuCaja: rm ? { izq: rm.left, der: rm.right, arriba: rm.top } : null,
      botonAbajo: rb ? rb.bottom : null,
      anchoVentana: document.documentElement.clientWidth,
      scrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    };
  },

  estrategiaPulsar: () => {
    const b = document.querySelector("#crm-btn-estrategia");
    if (!b) return { error: "no existe #crm-btn-estrategia" };
    if (b.disabled) return { error: "«Estrategia» está deshabilitado" };
    b.click();
    return { ok: true };
  },

  // Como el mouse: en la casilla o en el nombre, que como etiqueta le pasa el clic a la casilla.
  estrategiaMarcar: (id, donde) => {
    const li = document.querySelector(`#crm-estrategia-lista .estrategia-opcion[data-etapa-id="${id}"]`);
    if (!li) return { error: `el menú no tiene la etapa ${id}` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    li.querySelector(donde === "nombre" ? ".estrategia-nombre" : ".estrategia-check").click();
    return { ok: true, marcada: li.querySelector(".estrategia-check").checked };
  },

  estrategiaEnfocar: (id) => {
    const sel = id === "ultima"
      ? "#crm-estrategia-lista .estrategia-opcion:last-child .estrategia-check"
      : `#crm-estrategia-lista .estrategia-opcion[data-etapa-id="${id}"] .estrategia-check`;
    const c = document.querySelector(sel);
    if (!c) return { error: `no existe ${sel}` };
    document.querySelector("#crm-toast")?.classList.remove("is-visible");
    c.focus();
    return { ok: document.activeElement === c };
  },

  panelBoton: (id) => {
    const b = document.getElementById(id);
    if (!b || b.hidden) return { error: `el botón #${id} no está visible` };
    b.click();
    return { ok: true };
  },

  panelEstadoPulsar: (estado) => {
    const b = document.querySelector(`#crm-g-estados button[data-estado="${estado}"]`);
    if (!b) return { error: `no existe el estado ${estado}` };
    if (b.disabled) return { error: `el estado ${estado} está deshabilitado` };
    b.click();
    return { ok: true };
  },

  panelFormulario: (formSel, valores) => {
    const form = document.querySelector(formSel);
    if (!form) return { error: `no existe ${formSel}` };
    for (const [nombre, valor] of Object.entries(valores || {})) {
      const campo = form.elements.namedItem(nombre);
      if (!campo) return { error: `${formSel} no tiene el campo ${nombre}` };
      const proto = campo.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
        : campo.tagName === "SELECT" ? HTMLSelectElement.prototype
          : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(campo, valor);
      campo.dispatchEvent(new Event("input", { bubbles: true }));
      campo.dispatchEvent(new Event("change", { bubbles: true }));
    }
    form.requestSubmit();
    return { ok: true };
  },

  panelQuitarEtiqueta: (nombre) => {
    const chip = [...document.querySelectorAll("#crm-e-chips .chip-et")]
      .find((c) => c.querySelector(".chip-et-nombre").textContent.trim() === nombre);
    if (!chip) return { error: `no hay chip de la etiqueta «${nombre}»` };
    chip.click();
    return { ok: true };
  },

  // ---- Mis respuestas (el diálogo que sustituye a /replies) ----

  respuestasEstado: () => {
    const visible = (el) => !!el && !el.hidden && getComputedStyle(el).display !== "none" && el.getClientRects().length > 0;
    const d = document.querySelector("dialog#crm-dlg-respuestas");
    const bar = document.querySelector("#crm-qr-bar");
    const chip = document.querySelector("#crm-btn-respuestas-chat");
    const error = document.querySelector("#crm-resp-error");
    const a = document.activeElement;
    return {
      abierto: d?.open === true,
      modal: !!d && d.open && d.matches(":modal"),
      titulo: document.querySelector("#crm-resp-titulo")?.value ?? null,
      texto: document.querySelector("#crm-resp-texto")?.value ?? null,
      items: [...document.querySelectorAll("#crm-resp-lista .resp-item")].map((li) => ({
        id: li.dataset.replyId,
        titulo: li.querySelector(".resp-titulo")?.textContent ?? "",
        texto: li.querySelector(".resp-texto")?.textContent ?? "",
        etiqueta: li.querySelector(".resp-borrar")?.getAttribute("aria-label") ?? "",
      })),
      cuenta: document.querySelector("#crm-resp-cuenta")?.textContent.trim() ?? "",
      vacio: visible(document.querySelector("#crm-resp-vacio")),
      cargando: visible(document.querySelector("#crm-resp-cargando")),
      estado: document.querySelector("#crm-resp-estado")?.textContent.trim() ?? "",
      error: visible(error) ? error.textContent.trim() : null,
      foco: a ? (a.id || a.className || a.tagName) : null,
      chipAlFinal: !!chip && chip.parentElement === bar && bar.lastElementChild === chip,
      qr: [...document.querySelectorAll("#crm-qr-bar .qr")].map((b) => b.textContent),
      chatAbierto: document.querySelector("#crm")?.classList.contains("chat-abierto") ?? false,
      url: location.pathname + location.search,
      anchoDialogo: d?.open ? Math.round(d.getBoundingClientRect().width) : 0,
      anchoVentana: document.documentElement.clientWidth,
      scrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
      menuReplies: !!document.querySelector('a[href="/replies"]'),
    };
  },

  // Foco + clic, como quien lo activa con el teclado: el diálogo le devuelve el foco al cerrar.
  activarBoton: (id) => {
    const b = document.getElementById(id);
    if (!b) return { error: `no existe #${id}` };
    b.focus();
    b.click();
    return { ok: true };
  },

  escribirBorrador: (texto) => {
    const ta = document.querySelector("#crm-body");
    if (!ta) return { error: "no hay compositor" };
    ta.value = texto;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    return { ok: true };
  },

  guardarRespuesta: (titulo, texto) => {
    const t = document.querySelector("#crm-resp-titulo");
    const x = document.querySelector("#crm-resp-texto");
    const form = document.querySelector("#crm-form-respuesta");
    if (!t || !x || !form) return { error: "el diálogo no tiene título, texto o form" };
    t.value = titulo;
    if (texto !== undefined) x.value = texto;
    form.requestSubmit();
    return { ok: true };
  },

  // Con foco, como un clic real: así se comprueba a dónde va el foco cuando el botón desaparece.
  eliminarRespuesta: (titulo) => {
    const li = [...document.querySelectorAll("#crm-resp-lista .resp-item")]
      .find((x) => x.querySelector(".resp-titulo")?.textContent === titulo);
    if (!li) return { error: `«${titulo}» no está en la lista` };
    const b = li.querySelector(".resp-borrar");
    b.focus();
    b.click();
    return { ok: true };
  },

  enfocar: (selector) => {
    const el = document.querySelector(selector);
    if (!el) return { error: `no existe ${selector}` };
    el.focus();
    return { ok: document.activeElement === el };
  },

  pulsarQrTitulo: (titulo) => {
    const b = [...document.querySelectorAll("#crm-qr-bar .qr")].find((x) => x.textContent === titulo);
    if (!b) return { error: `no hay respuesta rápida «${titulo}»` };
    b.click();
    return { texto: document.querySelector("#crm-body").value, esperado: b.dataset.text };
  },
};

// ────────────────────────────────────────────────────────────────────────────
// Navegación y contexto
// ────────────────────────────────────────────────────────────────────────────

async function navegar(cdp, url) {
  const cargado = cdp.esperarEvento("Page.loadEventFired", 20000);
  const r = await cdp.send("Page.navigate", { url });
  if (r.errorText) throw new Error(`Page.navigate ${url}: ${r.errorText}`);
  await cargado;
}

async function recargar(cdp) {
  const cargado = cdp.esperarEvento("Page.loadEventFired", 20000);
  await cdp.send("Page.reload", { ignoreCache: true });
  await cargado;
}

const esperarTablero = (cdp) =>
  waitFor(async () => {
    const t = await cdp.evaluate(enPagina.tablero);
    return t.columnas > 0 && !t.cargando ? t : null;
  }, { timeout: 15000, label: "section.column en #crm-board" });

function crearContexto({ cdp, base, recolector, dialogos, env, db }) {
  const ctx = { cdp, base, recolector, dialogos, env, db, avisosPaso: [] };
  ctx.avisar = (t) => ctx.avisosPaso.push(t);
  ctx.log = (t) => { if (verbose) console.log(`   · ${t}`); };
  ctx.api = async (ruta) => {
    const r = await cdp.evaluate(enPagina.api, ruta);
    if (r.status < 200 || r.status >= 300) throw new Error(`GET ${ruta} → HTTP ${r.status}: ${JSON.stringify(r.json).slice(0, 200)}`);
    return r.json;
  };
  ctx.tablero = () => ctx.api("/crm/api/tablero");
  ctx.contacto = async (phone) => (await ctx.tablero()).contactos.find((c) => c.phone === phone) ?? null;
  ctx.chat = (phone) => ctx.api(`/crm/api/chat/${phone}`);
  ctx.esperarToast = async (prefijo, timeout = 4000) => {
    const t = await waitFor(async () => {
      const v = await cdp.evaluate(enPagina.toast);
      return v && v.includes(prefijo) ? v : null;
    }, { timeout, interval: 80, label: `el toast «${prefijo}…»` }).catch(() => null);
    if (!t) {
      const visto = await cdp.evaluate(enPagina.toast);
      throw new Error(`no apareció el toast «${prefijo}…» (visible: ${JSON.stringify(visto)})`);
    }
    ctx.log(`toast: ${t}`);
    return t;
  };
  ctx.esperarDialogo = async ({ desde = 0, tipo = "confirm", prefijo = "" } = {}) => {
    const d = await waitFor(() => ctx.dialogos.slice(desde).find((x) => !x.consumido) ?? null, { timeout: 3000, interval: 50 }).catch(() => null);
    if (!d) throw new Error(`no se abrió ningún ${tipo} tras la acción`);
    d.consumido = true;
    if (d.tipo !== tipo) throw new Error(`se abrió un ${d.tipo} y no un ${tipo}: ${JSON.stringify(d.mensaje)}`);
    if (prefijo && !d.mensaje.startsWith(prefijo)) throw new Error(`el ${tipo} dice ${JSON.stringify(d.mensaje)} y debía empezar por ${JSON.stringify(prefijo)}`);
    ctx.log(`${tipo} aceptado: ${d.mensaje}`);
    return d.mensaje;
  };
  ctx.esperarMovimiento = async (phone, destinoId, destinoNombre) => {
    await waitFor(async () => {
      const dom = await cdp.evaluate(enPagina.columnaDeTarjeta, phone);
      const c = await ctx.contacto(phone);
      return dom === String(destinoId) && c && String(c.etapa_id) === String(destinoId) ? true : null;
    }, { timeout: 6000, interval: 100, label: `que ${phone} quede en «${destinoNombre}» (DOM y API)` });
    const toast = await ctx.esperarToast("Movido a");
    if (!toast.includes(destinoNombre)) ctx.avisar(`el toast dice ${JSON.stringify(toast)} y no menciona «${destinoNombre}»`);
  };
  ctx.login = async (usuario) => {
    // Primero se descarga la página anterior: si las cookies se borran con ella viva, su sondeo
    // o un fetch en vuelo (p. ej. /crm/api/etapas) sale ya sin sesión, recibe 403 y el criterio
    // SIGUIENTE se lo cobra como error de consola. Un usuario real que cambia de cuenta sale de
    // la página; esto lo imita.
    await navegar(cdp, "about:blank");
    await cdp.send("Network.clearBrowserCookies");
    await navegar(cdp, base + "/login");
    const cargado = cdp.esperarEvento("Page.loadEventFired", 20000);
    await cdp.evaluate(enPagina.login, usuario, CLAVE);
    await cargado;
    const ruta = await cdp.evaluate(enPagina.ruta);
    if (ruta.startsWith("/login")) throw new Error(`el login de ${usuario} no entró (sigue en ${ruta})`);
    ctx.log(`sesión de ${usuario} abierta (${ruta})`);
  };
  ctx.abrirCrm = async (sufijo = "") => {
    await navegar(cdp, base + "/crm" + sufijo);
    return esperarTablero(cdp);
  };
  ctx.capturar = async (nombre) => {
    if (!CAPTURAS) return;
    const r = await cdp.send("Page.captureScreenshot", { format: "png" });
    fs.writeFileSync(path.join(CAPTURAS, `${nombre}.png`), Buffer.from(r.data, "base64"));
    ctx.log(`captura ${nombre}.png`);
  };
  ctx.ancho = async (width, height) => {
    await cdp.send("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 700 });
  };
  return ctx;
}

const resultados = [];

function imprimir(icono, titulo, detalle, avisos = []) {
  console.log(`${icono} ${titulo}${detalle ? " — " + detalle : ""}`);
  for (const a of avisos) console.log(`   ⚠️  ${a}`);
}

async function criterio(ctx, titulo, fn) {
  ctx.avisosPaso = [];
  const marca = ctx.recolector.fallos.length;
  try {
    const detalle = await fn(ctx);
    const nuevos = ctx.recolector.fallos.slice(marca);
    if (nuevos.length) {
      resultados.push({ titulo, ok: false });
      imprimir("❌", titulo, `${detalle ?? ""} · pero hubo ${nuevos.length} error(es) en consola/red: ${nuevos.map((f) => `[${f.origen}] ${f.texto}`).join(" | ")}`);
    } else {
      resultados.push({ titulo, ok: true });
      imprimir("✅", titulo, detalle, ctx.avisosPaso);
    }
  } catch (e) {
    resultados.push({ titulo, ok: false });
    imprimir("❌", titulo, e.message, ctx.avisosPaso);
  }
}

// ────────────────────────────────────────────────────────────────────────────
// Criterios
// ────────────────────────────────────────────────────────────────────────────

async function criterioCarga(ctx) {
  const t = await ctx.abrirCrm();
  const api = await ctx.tablero();
  const fallos = [];
  if (t.columnas !== api.etapas.length) fallos.push(`${t.columnas} columnas en el DOM vs ${api.etapas.length} etapas en la API`);
  if (t.tarjetas !== api.contactos.length) fallos.push(`${t.tarjetas} tarjetas vs ${api.contactos.length} contactos`);
  // «Todos», una por etapa y «Folios cerrados» al final, sin conteo mientras no se abre.
  if (t.tabs.length !== api.etapas.length + 2) fallos.push(`${t.tabs.length} pestañas (esperaba ${api.etapas.length + 2})`);
  const ultima = t.tabs[t.tabs.length - 1];
  if (ultima?.nombre !== "Folios cerrados" || ultima?.conteo !== "") fallos.push(`la última pestaña dice ${JSON.stringify(ultima)}`);
  if (t.tabs[0]?.nombre !== "Todos" || t.tabs[0]?.conteo !== String(api.total)) fallos.push(`la pestaña «Todos» dice ${JSON.stringify(t.tabs[0])}`);
  if (!mismoConjunto(t.bandeja, api.contactos.map((c) => c.phone))) fallos.push(`la bandeja trae ${JSON.stringify(t.bandeja)}`);
  if (!t.nuevaEtapa) fallos.push("el admin global no ve el botón «Nueva etapa»");
  if (t.menus !== api.etapas.length) fallos.push(`el admin global ve ${t.menus} menús ⋮ (esperaba ${api.etapas.length})`);
  if (t.altas !== api.etapas.length) fallos.push(`el admin ve ${t.altas} botones «Añadir contacto» (esperaba ${api.etapas.length})`);
  if (!t.crmKanban || !t.openChat) fallos.push("faltan window.CRMKanban.reload o window.openChat");
  const nombres = (await ctx.cdp.evaluate(enPagina.columnasDOM)).map((c) => c.nombre);
  const esperados = api.etapas.map((e) => e.nombre);
  if (JSON.stringify(nombres) !== JSON.stringify(esperados)) fallos.push(`orden de columnas ${JSON.stringify(nombres)} vs API ${JSON.stringify(esperados)}`);
  if (fallos.length) throw new Error(fallos.join("; "));
  await ctx.capturar("01-tablero-1400");
  return `${t.columnas} etapas (${esperados.join(" · ")}), ${t.tarjetas} tarjetas, ${t.tabs.length} pestañas, bandeja «${t.conteoBandeja}»`;
}

async function criterioTarjetas(ctx) {
  const api = await ctx.tablero();
  const tarjetas = await ctx.cdp.evaluate(enPagina.tarjetas);
  const columnas = await ctx.cdp.evaluate(enPagina.columnasDOM);
  const fallos = [];
  for (const c of api.contactos) {
    const t = tarjetas.find((x) => x.id === c.phone);
    if (!t) { fallos.push(`sin tarjeta para ${c.phone}`); continue; }
    if (t.columna !== String(c.etapa_id)) fallos.push(`${c.phone} está en la columna ${t.columna} y no en ${c.etapa_id}`);
    const nombreEsperado = c.name || "+52 55 0000 0015";
    if (t.nombre !== nombreEsperado) fallos.push(`${c.phone}: nombre ${JSON.stringify(t.nombre)} (esperaba ${JSON.stringify(nombreEsperado)})`);
    if (t.avatar !== c.initials) fallos.push(`${c.phone}: avatar ${JSON.stringify(t.avatar)} vs initials ${JSON.stringify(c.initials)}`);
    if (c.unread > 0 ? t.noLeidos !== String(c.unread) : t.noLeidos !== null) fallos.push(`${c.phone}: badge no leídos ${JSON.stringify(t.noLeidos)} vs unread ${c.unread}`);
    if (!t.draggable || t.role !== "button" || !t.aria?.startsWith("Abrir chat con")) fallos.push(`${c.phone}: draggable/role/aria ${JSON.stringify([t.draggable, t.role, t.aria])}`);
    if (t.selects) fallos.push(`${c.phone}: la tarjeta todavía trae un select («Mover a…»)`);
    if (!t.wa) fallos.push(`${c.phone}: sin el ícono de WhatsApp (.card-wa)`);
    else if (!t.wa.oculto || !t.wa.svg || !t.wa.enPie || t.wa.nuevos !== c.unread > 0) {
      fallos.push(`${c.phone}: ícono de WhatsApp ${JSON.stringify(t.wa)} con unread=${c.unread} (decorativo, en el pie y verde solo con pendientes)`);
    }
  }
  for (const e of api.etapas) {
    const col = columnas.find((x) => x.id === String(e.id));
    if (!col) { fallos.push(`sin columna ${e.id}`); continue; }
    if (col.conteo !== String(e.count)) fallos.push(`«${e.nombre}»: contador ${col.conteo} vs API ${e.count}`);
    if (col.tarjetas.length !== e.count) fallos.push(`«${e.nombre}»: ${col.tarjetas.length} tarjetas vs API ${e.count}`);
    if (col.color.toUpperCase() !== e.color.toUpperCase()) fallos.push(`«${e.nombre}»: --col-color ${col.color} vs ${e.color}`);
  }
  const ana = tarjetas.find((x) => x.id === ANA);
  if (ana) {
    if (!ana.badges.includes("tomado · colab1")) fallos.push(`Ana sin la insignia «tomado · colab1»: ${JSON.stringify(ana.badges)}`);
    if (!ana.badges.includes("bot pausado")) fallos.push(`Ana sin la insignia «bot pausado»`);
    if (ana.preview !== "Y ¿cuánto tarda?") fallos.push(`preview de Ana: ${JSON.stringify(ana.preview)}`);
    if (ana.telefono !== "+52 55 0000 0011") fallos.push(`teléfono de Ana: ${JSON.stringify(ana.telefono)}`);
  }
  // Diego tiene su único folio cerrado: ya no está en el panel. Sus insignias y palomitas
  // se revisan donde vive ahora, en «Folios cerrados» (criterio 26).
  if (tarjetas.some((x) => x.id === DIEGO)) fallos.push("Diego, con su folio cerrado, sigue en el tablero");
  const beto = tarjetas.find((x) => x.id === BETO);
  if (beto && beto.preview !== "Sin mensajes") fallos.push(`preview de Beto: ${JSON.stringify(beto.preview)}`);
  const felipe = tarjetas.find((x) => x.id === "525500000015");
  if (felipe && felipe.preview !== "📷 Imagen") fallos.push(`preview de Felipe (adjunto): ${JSON.stringify(felipe.preview)}`);
  if (fallos.length) throw new Error(fallos.join("; "));
  return `${tarjetas.length} tarjetas con nombre, iniciales, teléfono, preview, palomitas, no leídos e insignias correctos; contadores = API`;
}

async function criterioPestanas(ctx) {
  const api = await ctx.tablero();
  const etapa = api.etapas[1];
  const esperados = api.contactos.filter((c) => c.etapa_id === etapa.id).map((c) => c.phone);
  if (!(await ctx.cdp.evaluate(enPagina.pulsarTab, String(etapa.id)))) throw new Error(`no hay pestaña ${etapa.id}`);
  const e = await ctx.cdp.evaluate(enPagina.estadoTab, String(etapa.id));
  const fallos = [];
  if (!e.clase || !e.aria || e.tabindex !== 0) fallos.push(`la pestaña «${etapa.nombre}» no quedó activa: ${JSON.stringify(e)}`);
  if (!mismoConjunto(e.items, esperados)) fallos.push(`la bandeja filtrada trae ${JSON.stringify(e.items)} y no ${JSON.stringify(esperados)}`);
  if (!e.enfocada) fallos.push(`la columna «${etapa.nombre}» no recibió is-focused`);
  const vacia = api.etapas.find((x) => x.count === 0);
  if (vacia) {
    await ctx.cdp.evaluate(enPagina.pulsarTab, String(vacia.id));
    const v = await ctx.cdp.evaluate(enPagina.estadoTab, String(vacia.id));
    if (!v.vacio || v.items.length) fallos.push(`la etapa vacía «${vacia.nombre}» no muestra «Sin contactos en esta etapa»`);
  }
  await ctx.cdp.evaluate(enPagina.pulsarTab, "all");
  const todos = await ctx.cdp.evaluate(enPagina.estadoTab, "all");
  if (!mismoConjunto(todos.items, api.contactos.map((c) => c.phone))) fallos.push("«Todos» no restauró la bandeja completa");
  if (fallos.length) throw new Error(fallos.join("; "));
  return `«${etapa.nombre}» filtra a ${esperados.length} chat(s) y enfoca su columna; «Todos» restaura ${api.contactos.length}`;
}

async function criterioMiembros(ctx) {
  const api = await ctx.tablero();
  const tira = await ctx.cdp.evaluate(enPagina.miembros);
  const fallos = [];
  const esperados = api.miembros.map((m) => m.username);
  const enTira = tira.filter((m) => m.username !== "__sin_asesor__").map((m) => m.username);
  if (JSON.stringify(enTira) !== JSON.stringify(esperados)) fallos.push(`la tira trae ${JSON.stringify(enTira)} y la API ${JSON.stringify(esperados)}`);
  for (const m of api.miembros) {
    const t = tira.find((x) => x.username === m.username);
    if (!t) continue;
    if (t.conteo !== String(m.atiende)) fallos.push(`${m.username}: atiende ${t.conteo} en la tira vs ${m.atiende} en la API`);
    if (t.estado !== m.estado) fallos.push(`${m.username}: estado ${t.estado} en la tira vs ${m.estado} en la API`);
  }
  const oficinas = await ctx.cdp.evaluate(enPagina.oficinasEnTira);
  const oficinasApi = api.oficinas.map((o) => o.nombre);
  if (JSON.stringify(oficinas) !== JSON.stringify(oficinasApi)) fallos.push(`agrupación por oficina: ${JSON.stringify(oficinas)} vs ${JSON.stringify(oficinasApi)}`);
  const sin = tira.find((m) => m.username === "__sin_asesor__");
  const sinEsperado = api.contactos.filter((c) => !(c.ticket && c.ticket.assigned_to)).length;
  if (!sin || sin.conteo !== String(sinEsperado)) fallos.push(`«Sin asesor»: ${JSON.stringify(sin)} (esperaba ${sinEsperado})`);
  // Filtrar por colab1 deja solo sus chats, en tablero y bandeja, y «Todos» cuenta lo visible.
  if (!(await ctx.cdp.evaluate(enPagina.pulsarMiembro, "colab1"))) throw new Error("no hay chip de colab1 en la tira");
  const deColab1 = api.contactos.filter((c) => c.ticket && c.ticket.assigned_to === "colab1").map((c) => c.phone);
  const t = await ctx.cdp.evaluate(enPagina.tablero);
  const cards = (await ctx.cdp.evaluate(enPagina.tarjetas)).map((c) => c.id);
  if (!mismoConjunto(cards, deColab1)) fallos.push(`con el filtro de colab1 el tablero muestra ${JSON.stringify(cards)} y no ${JSON.stringify(deColab1)}`);
  if (!mismoConjunto(t.bandeja, deColab1)) fallos.push(`con el filtro de colab1 la bandeja muestra ${JSON.stringify(t.bandeja)}`);
  if (t.tabs[0]?.conteo !== String(deColab1.length)) fallos.push(`«Todos» dice ${t.tabs[0]?.conteo} con el filtro de colab1`);
  const activo = (await ctx.cdp.evaluate(enPagina.miembros)).find((m) => m.username === "colab1");
  if (!activo?.activo || activo.aria !== "true") fallos.push("el chip de colab1 no quedó marcado como activo (is-active / aria-pressed)");
  await ctx.cdp.evaluate(enPagina.pulsarMiembro, "colab1");
  const t2 = await ctx.cdp.evaluate(enPagina.tablero);
  if (t2.tarjetas !== api.contactos.length) fallos.push(`al quitar el filtro quedan ${t2.tarjetas} tarjetas y no ${api.contactos.length}`);
  if (fallos.length) throw new Error(fallos.join("; "));
  return `${esperados.length} miembros en ${oficinasApi.length} oficinas con estado y conteo iguales a la API; el filtro de colab1 deja ${deColab1.length} chat(s) y se quita al volver a pulsar`;
}

async function criterioEntranteEnVivo(ctx) {
  // Con el chat de Ana abierto, entra un mensaje del cliente por fuera del navegador —como lo
  // escribe el webhook— y debe verse sin recargar: en el hilo (sondeo de 8 s) y en la tarjeta y
  // la bandeja (sondeo de 20 s).
  const antes = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (!antes.abierto || antes.nombre !== "Ana Lucía Ramírez") throw new Error("hace falta el chat de Ana abierto");
  const unreadAntes = (await ctx.contacto(ANA)).unread;
  const texto = "¿Sigue disponible la cita de mañana?";
  const r = spawnSync(PYTHON, ["-c", `
import sys; sys.path.insert(0, ${JSON.stringify(REPO)})
from sqlmodel import Session
import app.db as db
from app.models import Client, Direction, Message, utcnow
with Session(db.engine) as s:
    s.add(Message(phone=${JSON.stringify(ANA)}, direction=Direction.IN, body=${JSON.stringify(texto)}))
    c = s.get(Client, ${JSON.stringify(ANA)}); c.last_activity_at = utcnow(); s.add(c); s.commit()
print("ok")`], { cwd: REPO, env: ctx.env, encoding: "utf8" });
  if (r.status !== 0 || !r.stdout.includes("ok")) throw new Error(`no se pudo insertar el mensaje: ${(r.stderr || "").slice(-300)}`);
  const t0 = Date.now();
  await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.burbujas.length === antes.burbujas.length + 1 && s.burbujas[s.burbujas.length - 1].texto === texto ? true : null;
  }, { timeout: 15000, interval: 250, label: "la burbuja del mensaje entrante en el hilo abierto (sondeo de 8 s)" });
  const hilo = Date.now() - t0;
  await waitFor(async () => {
    const t = (await ctx.cdp.evaluate(enPagina.tarjetas)).find((x) => x.id === ANA);
    return t && t.preview === texto && t.noLeidos === String(unreadAntes + 1) ? true : null;
  }, { timeout: 30000, interval: 500, label: "la tarjeta de Ana con el mensaje nuevo y un no leído más (sondeo de 20 s)" });
  const tarjeta = Date.now() - t0;
  return `el entrante apareció en el hilo a los ${(hilo / 1000).toFixed(1)} s y en la tarjeta (preview y ${unreadAntes + 1} sin leer) a los ${(tarjeta / 1000).toFixed(1)} s, sin recargar`;
}

async function criterioRutaVieja(ctx) {
  await navegar(ctx.cdp, `${ctx.base}/inbox/${ANA}?volver=//evil.com&folio=1`);
  await esperarTablero(ctx.cdp);
  const s = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    return e.abierto && e.burbujas.length > 0 ? e : null;
  }, { timeout: 8000, label: "que /inbox/{phone} aterrice en el chat del CRM" });
  if (s.url !== `/crm?chat=${ANA}`) throw new Error(`la URL final es ${s.url}`);
  if (s.nombre !== "Ana Lucía Ramírez") throw new Error(`abrió ${JSON.stringify(s.nombre)}`);
  return `/inbox/${ANA} (con volver y folio) aterriza en /crm?chat=${ANA} con el chat abierto y sin arrastrar parámetros`;
}

async function criterioBusqueda(ctx) {
  await ctx.cdp.evaluate(enPagina.buscar, "ramirez");
  await waitFor(async () => {
    const t = await ctx.cdp.evaluate(enPagina.tablero);
    return t.bandeja.length === 1 && t.bandeja[0] === ANA ? true : null;
  }, { timeout: 4000, label: "que la búsqueda «ramirez» deje solo a Ana" });
  const t1 = await ctx.cdp.evaluate(enPagina.tablero);
  await ctx.cdp.evaluate(enPagina.buscar, "+52 55 0000 0012");
  await waitFor(async () => {
    const t = await ctx.cdp.evaluate(enPagina.tablero);
    return t.bandeja.length === 1 && t.bandeja[0] === BETO ? true : null;
  }, { timeout: 4000, label: "que la búsqueda por teléfono con formato deje solo a Beto" });
  await ctx.cdp.evaluate(enPagina.buscar, "");
  const api = await ctx.tablero();
  await waitFor(async () => {
    const t = await ctx.cdp.evaluate(enPagina.tablero);
    return t.bandeja.length === api.contactos.length ? true : null;
  }, { timeout: 4000, label: "que al borrar la búsqueda vuelvan todos" });
  return `«ramirez» → solo Ana (tarjetas: ${t1.tarjetas}, «Todos»: ${t1.tabs[0].conteo}); «+52 55 0000 0012» → solo Beto; vacío → ${api.contactos.length}`;
}

async function criterioArrastre(ctx) {
  const api = await ctx.tablero();
  const origen = api.etapas[0];
  const destino = api.etapas[api.etapas.length - 1];
  const ana = api.contactos.find((c) => c.phone === ANA);
  if (ana.etapa_id !== origen.id) throw new Error(`Ana no está en «${origen.nombre}»`);
  const r = await ctx.cdp.evaluate(enPagina.soltar, ANA, String(destino.id));
  if (r.error) throw new Error(r.error);
  const fallos = [];
  if (!r.draggable) fallos.push("la tarjeta no es arrastrable");
  if (!r.arrastrando) fallos.push("sin is-dragging en dragstart");
  if (r.dato !== ANA) fallos.push(`dataTransfer trae ${JSON.stringify(r.dato)}`);
  if (!r.dragoverCancelado) fallos.push("dragover sin preventDefault()");
  if (!r.resaltada) fallos.push("la columna destino no recibió is-over");
  if (!r.dropCancelado) fallos.push("drop sin preventDefault()");
  if (!r.limpia) fallos.push("la columna conservó is-over tras dragend");
  if (fallos.length) throw new Error(fallos.join("; "));
  await ctx.esperarMovimiento(ANA, destino.id, destino.nombre);
  const c = await ctx.contacto(ANA);
  await recargar(ctx.cdp);
  await esperarTablero(ctx.cdp);
  const dom = await ctx.cdp.evaluate(enPagina.columnaDeTarjeta, ANA);
  if (dom !== String(destino.id)) throw new Error(`tras recargar, Ana está en la columna ${dom} y no en «${destino.nombre}»`);
  const cols = await ctx.cdp.evaluate(enPagina.columnasDOM);
  const colDestino = cols.find((x) => x.id === String(destino.id));
  const colOrigen = cols.find((x) => x.id === String(origen.id));
  if (colDestino.conteo !== String(destino.count + 1) || colOrigen.conteo !== String(origen.count - 1)) {
    throw new Error(`contadores tras mover: «${origen.nombre}»=${colOrigen.conteo} (esperaba ${origen.count - 1}), «${destino.nombre}»=${colDestino.conteo} (esperaba ${destino.count + 1})`);
  }
  return `Ana pasó de «${origen.nombre}» a «${destino.nombre}» por drag & drop, la API la guardó (etapa_id=${c.etapa_id}) y sigue ahí tras recargar; contadores al día`;
}

const timbresQuietos = (xs) => xs.length > 0 && xs.every((x) => !x.timbre && !x.corriendo.length);

async function criterioIconoWhatsApp(ctx) {
  // La tarjeta ya no trae el select «Mover a…»: mover sin arrastrar es «Estrategia», en el chat
  // (17c y 17d). En su lugar va el ícono de WhatsApp: verde con mensajes sin contestar, y un
  // timbre corto la primera vez que la pestaña los ve o cuando llegan más (el del mensaje en
  // vivo es el 28). Primero Ana vuelve a la primera etapa, que es lo que hacía el select, y
  // ahora arrastrando: el resto del recorrido la espera ahí.
  const api = await ctx.tablero();
  const primera = api.etapas[0];
  const r = await ctx.cdp.evaluate(enPagina.soltar, ANA, String(primera.id));
  if (r.error) throw new Error(r.error);
  await ctx.esperarMovimiento(ANA, primera.id, primera.nombre);

  await recargar(ctx.cdp);
  await esperarTablero(ctx.cdp);
  const fallos = [];
  const selects = await ctx.cdp.evaluate(() => document.querySelectorAll(".card-move, #crm-board article.card select").length);
  if (selects) fallos.push(`quedan ${selects} select «Mover a…» en el tablero`);
  const conPendientes = new Set((await ctx.tablero()).contactos.filter((c) => c.unread > 0).map((c) => c.phone));
  if (!conPendientes.size) throw new Error("la siembra no dejó ninguna tarjeta con mensajes sin contestar");

  // Recién cargada la página: las tarjetas con pendientes timbran (sacudida y onda), las demás
  // no se mueven, y ningún timbre pasa de 5 s.
  const recien = await ctx.cdp.evaluate(enPagina.iconosWa);
  for (const i of recien) {
    if (!i.existe) { fallos.push(`${i.id}: sin ícono de WhatsApp`); continue; }
    const espera = conPendientes.has(i.id);
    if (i.nuevos !== espera) fallos.push(`${i.id}: has-unread=${i.nuevos} y la API dice ${espera ? "con" : "sin"} pendientes`);
    if (espera && !(i.timbre && i.corriendo.includes("crm-wa-timbre") && i.corriendo.includes("crm-wa-onda"))) {
      fallos.push(`${i.id}: tiene pendientes y no timbró al cargar (${JSON.stringify(i.corriendo)})`);
    }
    if (!espera && (i.timbre || i.corriendo.length)) fallos.push(`${i.id}: sin pendientes y timbra (${JSON.stringify(i.corriendo)})`);
    if (!i.finitas || i.finMs > 5000) fallos.push(`${i.id}: el timbre acaba a los ${i.finMs} ms (finito: ${i.finitas}); WCAG 2.2.2 pide ≤ 5 s sin un control para pararlo`);
  }
  await ctx.capturar("06-tarjetas-timbre");

  // Se calma solo y se queda en verde; repintar sin mensajes nuevos no lo vuelve a despertar.
  const t0 = Date.now();
  await waitFor(async () => (timbresQuietos(await ctx.cdp.evaluate(enPagina.iconosWa)) ? true : null),
    { timeout: 8000, interval: 200, label: "que los timbres de la carga terminen solos" });
  const duro = Date.now() - t0;
  await ctx.cdp.evaluate(async () => { await window.CRMKanban.reload(); });
  const calma = await ctx.cdp.evaluate(enPagina.iconosWa);
  if (!timbresQuietos(calma)) fallos.push(`repintar sin mensajes nuevos volvió a timbrar: ${JSON.stringify(calma.filter((x) => x.timbre || x.corriendo.length).map((x) => x.id))}`);
  if (calma.some((x) => x.nuevos !== conPendientes.has(x.id))) fallos.push("tras el timbre, has-unread ya no coincide con la API");
  const verde = calma.find((x) => conPendientes.has(x.id));
  const gris = calma.find((x) => !conPendientes.has(x.id));
  const cVerde = verde ? contrasteRgb(verde.color, verde.fondo) : 0;
  const cGris = gris ? contrasteRgb(gris.color, gris.fondo) : 0;
  if (verde?.color !== "rgb(18, 140, 126)" || cVerde < 3) fallos.push(`con pendientes: ${verde?.color} sobre ${verde?.fondo} (${cVerde.toFixed(2)}:1; esperaba --wa-teal-2 ≥ 3:1)`);
  if (gris && (gris.color !== "rgb(102, 119, 129)" || cGris < 3)) fallos.push(`sin pendientes: ${gris.color} sobre ${gris.fondo} (${cGris.toFixed(2)}:1; esperaba --wa-muted ≥ 3:1)`);
  await ctx.capturar("06-tarjetas-whatsapp");
  if (fallos.length) throw new Error(fallos.join("; "));
  const fin = Math.round(Math.max(...recien.map((x) => x.finMs)));
  return `Ana volvió a «${primera.nombre}» arrastrando; ninguna tarjeta trae «Mover a…»; las ${conPendientes.size} con pendientes timbran al cargar (acaba a los ${fin} ms) y se calman solas en ${(duro / 1000).toFixed(1)} s en verde ${cVerde.toFixed(2)}:1; las demás, gris ${cGris.toFixed(2)}:1 y quietas; repintar no las despierta`;
}

async function criterioTimbreEnVivo(ctx) {
  // Llega un mensaje con el tablero a la vista: timbra SOLO esa tarjeta, se calma sola y se queda
  // en verde. Con «reducir movimiento» no se mueve nada, pero el verde sigue. Va al final porque
  // deja un mensaje más en la base.
  await ctx.login("jefa1");
  await ctx.abrirCrm();
  await waitFor(async () => (timbresQuietos(await ctx.cdp.evaluate(enPagina.iconosWa)) ? true : null),
    { timeout: 8000, interval: 200, label: "que terminen los timbres de la carga" });
  const api = await ctx.tablero();
  const objetivo = api.contactos.find((c) => c.unread === 0);
  if (!objetivo) throw new Error("no queda ninguna tarjeta sin pendientes que pueda recibir el mensaje");
  const texto = `Timbre QA ${Date.now()}`;
  const r = spawnSync(PYTHON, ["-c", `
import sys; sys.path.insert(0, ${JSON.stringify(REPO)})
from sqlmodel import Session
import app.db as db
from app.models import Client, Direction, Message, utcnow
with Session(db.engine) as s:
    s.add(Message(phone=${JSON.stringify(objetivo.phone)}, direction=Direction.IN, body=${JSON.stringify(texto)}))
    c = s.get(Client, ${JSON.stringify(objetivo.phone)}); c.last_activity_at = utcnow(); s.add(c); s.commit()
print("ok")`], { cwd: REPO, env: ctx.env, encoding: "utf8" });
  if (r.status !== 0 || !r.stdout.includes("ok")) throw new Error(`no se pudo insertar el mensaje: ${(r.stderr || "").slice(-300)}`);
  // El sondeo de 20 s lo traería igual (lo prueba el 7b); aquí se repinta ya.
  await ctx.cdp.evaluate(async () => { await window.CRMKanban.reload(); });
  const fallos = [];
  const llego = await waitFor(async () => {
    const xs = await ctx.cdp.evaluate(enPagina.iconosWa);
    return xs.find((i) => i.id === objetivo.phone)?.nuevos ? xs : null;
  }, { timeout: 6000, interval: 100, label: `la tarjeta de ${objetivo.phone} con el mensaje nuevo` });
  const suyo = llego.find((i) => i.id === objetivo.phone);
  const t0 = Date.now();
  if (!suyo.timbre || !suyo.corriendo.includes("crm-wa-timbre")) fallos.push(`el ícono de ${objetivo.phone} no timbró al llegarle el mensaje: ${JSON.stringify(suyo)}`);
  const otros = llego.filter((i) => i.id !== objetivo.phone && (i.timbre || i.corriendo.length)).map((i) => i.id);
  if (otros.length) fallos.push(`timbraron también ${JSON.stringify(otros)}, que no recibieron nada`);
  await ctx.capturar("44-timbre-en-vivo");
  // Repintar a media animación —la búsqueda, un filtro, un arrastre o el mensaje de otro
  // repintan el tablero entero— no vuelve a empezar el timbre: la tarjeta nueva lo retoma
  // donde iba, y el total sigue en 3.6 s aunque se repinte cada cuarto de segundo.
  await dormir(1200);
  await ctx.cdp.evaluate(async () => { await window.CRMKanban.reload(); });
  const transcurrido = Date.now() - t0;
  const repintado = (await ctx.cdp.evaluate(enPagina.iconosWa)).find((i) => i.id === objetivo.phone);
  if (!repintado?.timbre || !repintado.corriendo.includes("crm-wa-timbre")) {
    fallos.push(`repintar a media animación cortó el timbre de ${objetivo.phone}: ${JSON.stringify(repintado)}`);
  } else if (repintado.restanteMs > 3600 - transcurrido + 250) {
    fallos.push(`repintar a los ${transcurrido} ms volvió a empezar el timbre: le quedan ${repintado.restanteMs} ms (esperaba ≤ ${3600 - transcurrido + 250})`);
  }
  await waitFor(async () => {
    const x = (await ctx.cdp.evaluate(enPagina.iconosWa)).find((i) => i.id === objetivo.phone);
    return x && x.nuevos && !x.timbre && !x.corriendo.length ? true : null;
  }, { timeout: 6000, interval: 200, label: "que el timbre del mensaje nuevo termine y deje el verde" });

  // «Reducir movimiento»: al cargar no timbra nadie, pero el verde sigue donde hay pendientes.
  let quietos = [];
  await ctx.cdp.send("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] });
  try {
    await recargar(ctx.cdp);
    await esperarTablero(ctx.cdp);
    await dormir(300);
    quietos = await ctx.cdp.evaluate(enPagina.iconosWa);
  } finally {
    await ctx.cdp.send("Emulation.setEmulatedMedia", { media: "", features: [] });
  }
  const moviendose = quietos.filter((i) => i.corriendo.length).map((i) => i.id);
  if (moviendose.length) fallos.push(`con «reducir movimiento» se mueven ${JSON.stringify(moviendose)}`);
  if (!quietos.find((i) => i.id === objetivo.phone)?.nuevos) fallos.push(`con «reducir movimiento» el ícono de ${objetivo.phone} perdió el verde`);
  if (fallos.length) throw new Error(fallos.join("; "));
  return `entra un mensaje para ${objetivo.name || objetivo.phone}: timbra solo su tarjeta (${suyo.corriendo.join(" + ")}); repintada a los ${transcurrido} ms lo retoma con ${repintado.restanteMs} ms por delante, se calma sola y queda en verde; con «reducir movimiento» nada se mueve y el verde sigue (${quietos.filter((i) => i.nuevos).length} tarjetas)`;
}

async function criterioAbrirChat(ctx) {
  const r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "tarjeta");
  if (r.error) throw new Error(r.error);
  const chat = await ctx.chat(ANA);
  const e = await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.burbujas.length === chat.mensajes.length ? s : null;
  }, { timeout: 6000, label: `${chat.mensajes.length} burbujas en el hilo` });
  const fallos = [];
  if (!e.abierto || !e.chatVisible || e.boardVisible || !e.btnBoardVisible) fallos.push(`estado del panel: ${JSON.stringify({ abierto: e.abierto, chat: e.chatVisible, board: e.boardVisible, btn: e.btnBoardVisible })}`);
  // «← Tablero» es el primer elemento de la fila de pestañas, antes de «Todos» y fuera del
  // tablist, y sobrevive a un repintado del tablero: renderTabs() vacía la lista entera, así
  // que si estuviera dentro desaparecería aquí.
  const fila = await ctx.cdp.evaluate(async () => {
    const enFila = () => {
      const b = document.querySelector("#crm-tabs > #crm-btn-board");
      const todos = document.querySelector('#crm-tabs-list > button.tab[data-filter="all"]');
      return !!b && !!todos && !b.closest("[role=tablist]") && !b.hidden && b.getClientRects().length > 0
        && document.querySelector("#crm-tabs").firstElementChild === b
        && b.getBoundingClientRect().right <= todos.getBoundingClientRect().left;
    };
    const antes = enFila();
    await window.CRMKanban.reload();
    return { antes, despues: enFila(), pestanas: document.querySelectorAll("#crm-tabs-list > button.tab").length };
  });
  if (!fila.antes || !fila.despues) fallos.push(`«← Tablero» no está visible como primer elemento de #crm-tabs, antes de «Todos» y fuera del tablist (antes del repintado: ${fila.antes}, después: ${fila.despues})`);
  if (fila.pestanas < 2) fallos.push(`tras el repintado el tablist tiene ${fila.pestanas} pestañas`);
  if (e.nombre !== "Ana Lucía Ramírez" || e.telefono !== "+52 55 0000 0011" || e.avatar !== "AL") fallos.push(`cabecera: ${JSON.stringify([e.nombre, e.telefono, e.avatar])}`);
  if (!e.badges.some((b) => b.startsWith("escalado — el agente sigue activo"))) fallos.push(`sin insignia de escalado: ${JSON.stringify(e.badges)}`);
  if (!e.badges.some((b) => b.startsWith("ventana: "))) fallos.push(`sin insignia de ventana abierta: ${JSON.stringify(e.badges)}`);
  if (!e.badges.includes("responde por Meta")) fallos.push("sin «responde por Meta»");
  // La siembra deja a colab1 «ocupado»: la cabecera dice quién atiende y cómo está.
  if (!e.badges.includes("atiende: colab1 · ocupado")) fallos.push(`sin «atiende: colab1 · ocupado»: ${JSON.stringify(e.badges)}`);
  if (!e.botones.some((b) => b.clase.includes("chat-bot-on"))) fallos.push("sin botón «Reactivar bot»");
  if (!e.botones.some((b) => b.clase.includes("chat-block") && b.texto === "Bloquear cliente")) fallos.push("el admin no ve «Bloquear cliente»");
  const ticket = chat.ticket;
  const volver = encodeURIComponent(`/crm?chat=${ANA}`);
  if (!e.enlaces.some((a) => a.href === `/tickets/${ticket.id}?volver=${volver}`)) fallos.push(`enlace al folio: ${JSON.stringify(e.enlaces)}`);
  if (!e.enlaces.some((a) => a.href === `/clients/${ANA}?volver=${volver}`)) fallos.push("sin enlace a la ficha del cliente");
  const dirs = e.burbujas.map((b) => b.dir).join(",");
  if (dirs !== "in,out,in,in") fallos.push(`direcciones del hilo: ${dirs}`);
  if (e.burbujas[0].texto !== "Hola, ¿cuánto cuesta un acta?" || !e.burbujas[0].meta.startsWith("Cliente · ")) fallos.push(`primera burbuja: ${JSON.stringify(e.burbujas[0])}`);
  if (!e.burbujas[1].meta.startsWith("colab1 · ") || !e.burbujas[1].meta.endsWith("· read") || e.burbujas[1].eliminar) fallos.push(`burbuja saliente de otro asesor: ${JSON.stringify(e.burbujas[1])}`);
  if (!e.composerVisible || e.bannerVisible) fallos.push("el compositor debía estar visible y el aviso de ventana cerrada oculto");
  if (!e.windowLeft || e.urgente) fallos.push(`aviso de ventana: ${JSON.stringify([e.windowLeft, e.urgente])}`);
  if (e.qr !== chat.respuestas_rapidas.length) fallos.push(`${e.qr} respuestas rápidas (esperaba ${chat.respuestas_rapidas.length})`);
  if (e.accionForm !== `/inbox/${ANA}/send`) fallos.push(`action del compositor: ${e.accionForm}`);
  if (e.url !== `/crm?chat=${ANA}`) fallos.push(`URL: ${e.url}`);
  if (!e.bandejaAbierta.includes(ANA)) fallos.push("el ítem de bandeja no quedó marcado is-open");
  if (e.foco !== "crm-chat-name") fallos.push(`el foco quedó en ${e.foco} y no en el nombre del chat`);
  if (e.scrollAlFondo === false) fallos.push("el hilo no quedó al fondo al abrir");
  await ctx.capturar("02-chat-ana-1400");
  const qr = await ctx.cdp.evaluate(enPagina.pulsarQr);
  if (qr.error || qr.texto !== qr.esperado) fallos.push(`respuesta rápida: ${JSON.stringify(qr)}`);
  if (fallos.length) throw new Error(fallos.join("; "));
  return `chat de Ana: cabecera, ${e.burbujas.length} burbujas, insignias (${e.badges.length}), enlaces, compositor con ventana «${e.windowLeft}» y ${e.qr} respuestas rápidas; URL ${e.url}`;
}

async function sesionHttp(ctx, usuario) {
  // Sesión fuera del navegador: el POST tiene que venir de OTRO panel, no del JS de la pestaña
  // que estamos mirando (ese ya se refresca solo al enviar).
  const r = await fetch(`${ctx.base}/login`, {
    method: "POST", redirect: "manual",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username: usuario, password: CLAVE }),
  });
  if (r.status !== 303) throw new Error(`login HTTP de ${usuario} → HTTP ${r.status}`);
  const cookies = (r.headers.getSetCookie?.() ?? []).map((c) => c.split(";")[0]);
  if (!cookies.length) throw new Error("el login HTTP no devolvió cookie de sesión");
  return cookies.join("; ");
}

async function criterioAvisoEnVivo(ctx) {
  // El stream de avisos (GET /crm/api/eventos, app/eventos.py). Un compañero contesta desde su
  // propio panel: ese POST corre DENTRO del proceso de uvicorn —como el webhook—, así que el
  // listener publica y esta pestaña debe enterarse por el stream.
  //
  // Los sondeos se apagan antes a propósito: con ellos vivos, ver la burbuja no probaría nada
  // (el de 8 s la traería igual). Apagados, si aparece es porque el aviso llegó. Al final se
  // recarga la página para devolverlos, que es como sigue el resto del recorrido.
  const antes = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (!antes.abierto || antes.nombre !== "Ana Lucía Ramírez") throw new Error("hace falta el chat de Ana abierto");
  const ids = await ctx.cdp.evaluate(enPagina.matarSondeos);
  const texto = `Aviso en vivo QA ${Date.now()}`;
  const cookie = await sesionHttp(ctx, "jefa1");
  const t0 = Date.now();
  const envio = await fetch(`${ctx.base}/inbox/${ANA}/send`, {
    method: "POST", redirect: "manual",
    headers: { cookie, "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ body: texto }),
  });
  if (envio.status !== 303) throw new Error(`el envío del compañero devolvió HTTP ${envio.status}`);
  await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.burbujas.some((b) => b.texto === texto) ? true : null;
  }, { timeout: 6000, interval: 100, label: "la burbuja del compañero traída por el stream (sin sondeos)" });
  const ms = Date.now() - t0;
  await ctx.abrirCrm(`?chat=${ANA}`);  // devuelve los sondeos
  const s = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (!s.abierto) throw new Error("la página no volvió al chat de Ana tras recargar");
  return `con los ${ids} temporizadores apagados, el mensaje de otro panel se vio en ${ms} ms (el sondeo del hilo es de 8000 ms)`;
}

async function criterioVentanaCerrada(ctx) {
  // Beto nunca escribió: variante «nunca». Se abre desde la bandeja (sigue visible a 1400 px).
  let r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, BETO, "bandeja");
  if (r.error) throw new Error(r.error);
  const beto = await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.nombre === "Beto Nuñez" && s.estadoHilo === "chat-vacio" ? s : null;
  }, { timeout: 6000, label: "el chat vacío de Beto" });
  const fallos = [];
  if (beto.composerVisible || !beto.bannerVisible) fallos.push("Beto: compositor visible o aviso oculto");
  if (!beto.siNunca || beto.siCerrada) fallos.push(`Beto: variante equivocada del aviso ${JSON.stringify([beto.siNunca, beto.siCerrada])}`);
  if (!beto.badges.includes("ventana de 24 h cerrada")) fallos.push(`Beto: insignias ${JSON.stringify(beto.badges)}`);
  if (!beto.badges.includes("bot activo")) fallos.push("Beto: sin «bot activo»");
  if (beto.botones.some((b) => b.clase.includes("chat-bot-on"))) fallos.push("Beto: no debía haber «Reactivar bot»");
  // Diego: la ventana caducó (variante «cerrada»). Su único folio está cerrado, así que ya no
  // está en el panel: vive en «Folios cerrados» y se abre desde esa pestaña.
  if (!(await ctx.cdp.evaluate(enPagina.pulsarTab, CERRADAS))) throw new Error("no hay pestaña «Folios cerrados»");
  try {
    await waitFor(async () => {
      const e = await ctx.cdp.evaluate(enPagina.estadoTab, CERRADAS);
      return e.items.includes(DIEGO) ? true : null;
    }, { timeout: 6000, label: "a Diego en «Folios cerrados»" });
    r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, DIEGO, "bandeja");
    if (r.error) throw new Error(r.error);
    const diego = await waitFor(async () => {
      const s = await ctx.cdp.evaluate(enPagina.estadoChat);
      return s.nombre === "Diego Solís" && s.burbujas.length === 2 ? s : null;
    }, { timeout: 6000, label: "el chat de Diego" });
    if (diego.composerVisible || !diego.bannerVisible) fallos.push("Diego: compositor visible o aviso oculto");
    if (diego.siNunca || !diego.siCerrada) fallos.push(`Diego: variante equivocada del aviso ${JSON.stringify([diego.siNunca, diego.siCerrada])}`);
    await ctx.capturar("03-chat-diego-ventana-cerrada-1400");
    if (!diego.burbujas[1].meta.includes("· delivered")) fallos.push(`Diego: meta del saliente ${JSON.stringify(diego.burbujas[1].meta)}`);
    if (!diego.burbujas[1].eliminar) fallos.push("Diego: el saliente de jefa1 debía traer «eliminar» (es de quien tiene la sesión)");
  } finally {
    // Los criterios siguientes trabajan sobre la cola abierta.
    await ctx.cdp.evaluate(enPagina.pulsarTab, "all");
    await waitFor(async () => {
      const e = await ctx.cdp.evaluate(enPagina.estadoTab, "all");
      return e.items.includes(ANA) ? true : null;
    }, { timeout: 6000, label: "volver a «Todos»" }).catch(() => null);
  }
  if (fallos.length) throw new Error(fallos.join("; "));
  return "Beto (nunca escribió) y Diego (ventana caducada) muestran el aviso correcto en lugar del compositor";
}

async function criterioEnviar(ctx) {
  const r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "bandeja");
  if (r.error) throw new Error(r.error);
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.estadoChat)).composerVisible ? true : null, { timeout: 6000, label: "el compositor de Ana" });
  const vacio = await ctx.cdp.evaluate(enPagina.enviarVacio);
  const fallos = [];
  if (vacio.chip !== "Escribe un mensaje o adjunta un archivo.") fallos.push(`envío vacío: ${JSON.stringify(vacio)}`);
  const antes = (await ctx.chat(ANA)).mensajes.length;
  const texto = "Prueba QA desde el CRM ✓";
  const e = await ctx.cdp.evaluate(enPagina.escribirYEnviar, texto);
  if (e.error) throw new Error(e.error);
  await ctx.esperarToast("Mensaje enviado", 8000);
  const estado = await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.burbujas.length === antes + 1 ? s : null;
  }, { timeout: 8000, label: "la burbuja nueva en el hilo" });
  const ultima = estado.burbujas[estado.burbujas.length - 1];
  if (ultima.dir !== "out" || ultima.texto !== texto || !ultima.eliminar || !ultima.meta.startsWith("jefa1 · ")) fallos.push(`burbuja enviada: ${JSON.stringify(ultima)}`);
  if (estado.scrollAlFondo === false) fallos.push("el hilo no bajó al fondo tras enviar");
  const chat = await ctx.chat(ANA);
  const enApi = chat.mensajes[chat.mensajes.length - 1];
  if (enApi.body !== texto || !enApi.mio || enApi.sent_by !== "jefa1") fallos.push(`la API no registró el envío: ${JSON.stringify(enApi)}`);
  if (chat.contacto.unread !== 0) fallos.push(`tras contestar, unread debía ser 0 y es ${chat.contacto.unread}`);
  // El bot se apaga al contestar («quien responde se queda el chat»: inbox._pause_bot).
  if (!chat.bot_pausado) fallos.push("tras contestar, el bot debía quedar pausado");
  // La tarjeta del tablero se actualiza sin recargar.
  await waitFor(async () => {
    const t = (await ctx.cdp.evaluate(enPagina.tarjetas)).find((x) => x.id === ANA);
    return t && t.preview === texto && t.noLeidos === null ? true : null;
  }, { timeout: 8000, label: "que la tarjeta de Ana muestre el mensaje enviado y sin no leídos" });
  // Ocultar el mensaje propio.
  const desde = ctx.dialogos.length;
  const d = await ctx.cdp.evaluate(enPagina.pulsarEliminarUltimo);
  if (d.error) throw new Error(d.error);
  await ctx.esperarDialogo({ desde, prefijo: "¿Ocultar este mensaje?" });
  await ctx.esperarToast("Mensaje ocultado", 8000);
  await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.burbujas.length === antes ? true : null;
  }, { timeout: 8000, label: "que la burbuja ocultada desaparezca" });
  if ((await ctx.chat(ANA)).mensajes.length !== antes) fallos.push("la API sigue devolviendo el mensaje ocultado");
  if (fallos.length) throw new Error(fallos.join("; "));
  return `envío vacío avisado; «${texto}» enviado por POST /inbox/${ANA}/send, visto en hilo, API y tarjeta; ocultado con confirm y desaparecido`;
}

async function criterioBurbujaLigada(ctx) {
  // Ana: el bot ligó «¿Aceptan transferencia?» a su folio (siembra). Esa burbuja del cliente
  // lleva «#N · Acta de nacimiento»; las otras entrantes, no.
  const chat = await ctx.chat(ANA);
  const folio = chat.contacto.folios_abiertos?.[0] ?? chat.mensajes.find((m) => m.folio)?.folio;
  if (!folio) throw new Error("la API no trae el folio de Ana");
  const ligado = chat.mensajes.find((m) => m.body === "¿Aceptan transferencia?");
  if (ligado?.folio?.id !== folio.id) throw new Error(`la API no liga el mensaje: ${JSON.stringify(ligado)}`);
  const r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "bandeja");
  if (r.error) throw new Error(r.error);
  const s = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    return e.nombre === "Ana Lucía Ramírez" && e.burbujas.length >= 4 ? e : null;
  }, { timeout: 6000, label: "el chat de Ana" });
  const fallos = [];
  const esperado = `#${folio.id} · Acta de nacimiento`;
  const b = s.burbujas.find((x) => x.texto === "¿Aceptan transferencia?");
  if (b?.dir !== "in" || b.folio !== esperado || !b.meta.startsWith(`Cliente · ${esperado} · `)) fallos.push(`burbuja ligada: ${JSON.stringify(b)}`);
  const sueltas = s.burbujas.filter((x) => x.dir === "in" && x.texto !== "¿Aceptan transferencia?");
  if (sueltas.some((x) => x.folio !== null)) fallos.push(`una entrante sin ligar lleva folio: ${JSON.stringify(sueltas)}`);
  if (fallos.length) throw new Error(fallos.join("; "));
  await ctx.capturar("09c-burbuja-ligada-1400");
  return `«Cliente · ${esperado} · …» en la ligada; ${sueltas.length} entrantes sin ligar, sin folio`;
}

async function criterioVariosTramites(ctx) {
  // Elena tiene DOS trámites en curso (siembra): el acta, tomada por jefa1, y la cita de
  // pasaporte, NUEVA y sin dueño. Como admin, jefa1 puede contestar sobre los dos.
  const fallos = [];
  const chat = await ctx.chat(ELENA);
  const abiertos = chat.contacto.folios_abiertos;
  if (!Array.isArray(abiertos) || abiertos.length !== 2) throw new Error(`la API trae ${JSON.stringify(abiertos)} como folios abiertos de Elena`);
  const [acta, pasaporte] = abiertos;
  if (acta.service_name !== "Acta de nacimiento" || acta.assigned_to !== "jefa1" || !acta.puedo_escribir) fallos.push(`primer folio de la API: ${JSON.stringify(acta)}`);
  if (pasaporte.service_name !== "Cita de pasaporte" || pasaporte.assigned_to !== null || !pasaporte.puedo_escribir) fallos.push(`segundo folio de la API: ${JSON.stringify(pasaporte)}`);
  // Ana, con UN folio, no muestra ni la fila ni el selector: la cabecera ya lo dice.
  let r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "bandeja");
  if (r.error) throw new Error(r.error);
  const ana = await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.nombre === "Ana Lucía Ramírez" && s.composerVisible ? s : null;
  }, { timeout: 6000, label: "el chat de Ana" });
  if (ana.foliosVisible || ana.selectorVisible) fallos.push(`Ana (un folio) muestra fila de trámites o selector: ${JSON.stringify([ana.foliosVisible, ana.selectorVisible])}`);
  // Elena: la fila lista los dos, en orden, con estado y quién atiende; el selector ofrece los dos.
  r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ELENA, "bandeja");
  if (r.error) throw new Error(r.error);
  const e = await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.nombre === "Elena Ortiz" && s.composerVisible && s.foliosVisible ? s : null;
  }, { timeout: 6000, label: "el chat de Elena con la fila de trámites" });
  await ctx.capturar("09b-chat-elena-dos-tramites-1400");
  if (e.foliosTitulo !== "Trámites en curso (2):") fallos.push(`título de la fila: ${JSON.stringify(e.foliosTitulo)}`);
  if (e.folios.length !== 2) fallos.push(`${e.folios.length} chips de trámite (esperaba 2)`);
  const volver = encodeURIComponent(`/crm?chat=${ELENA}`);
  const esperados = [
    { texto: `#${acta.id} Acta de nacimiento · tomado · atiende: jefa1`, href: `/tickets/${acta.id}?volver=${volver}`, ajeno: false },
    { texto: `#${pasaporte.id} Cita de pasaporte · nuevo · sin asignar`, href: `/tickets/${pasaporte.id}?volver=${volver}`, ajeno: false },
  ];
  esperados.forEach((esp, i) => {
    const chip = e.folios[i];
    if (!chip || chip.texto !== esp.texto || chip.href !== esp.href || chip.ajeno !== esp.ajeno) fallos.push(`chip ${i + 1}: ${JSON.stringify(chip)} (esperaba ${JSON.stringify(esp)})`);
  });
  if (!e.selectorVisible) fallos.push("el selector «Sobre el trámite» no se ve con dos folios tomables");
  const opciones = e.selectorOpciones.map((o) => o.texto);
  if (JSON.stringify(opciones) !== JSON.stringify([`#${acta.id} · Acta de nacimiento`, `#${pasaporte.id} · Cita de pasaporte`])) fallos.push(`opciones del selector: ${JSON.stringify(opciones)}`);
  // El compositor queda bajo el pliegue a 750 px de alto: una segunda captura con él a la vista.
  await ctx.cdp.evaluate(() => document.querySelector("#crm-composer")?.scrollIntoView({ block: "end" }));
  await ctx.capturar("09b-chat-elena-selector-folio-1400");
  // Contestar sobre el segundo: la burbuja lleva «jefa1 · #N · Cita de pasaporte», la API
  // devuelve el folio, y el folio NUEVO sin dueño pasa a quien contestó.
  r = await ctx.cdp.evaluate(enPagina.elegirFolio, pasaporte.id);
  if (r.error) throw new Error(r.error);
  const antes = chat.mensajes.length;
  const texto = "Su cita de pasaporte queda para el lunes ✓";
  r = await ctx.cdp.evaluate(enPagina.escribirYEnviar, texto);
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Mensaje enviado", 8000);
  const estado = await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.burbujas.length === antes + 1 ? s : null;
  }, { timeout: 8000, label: "la burbuja nueva en el hilo de Elena" });
  const ultima = estado.burbujas[estado.burbujas.length - 1];
  if (ultima.dir !== "out" || ultima.texto !== texto || !ultima.meta.startsWith(`jefa1 · #${pasaporte.id} · Cita de pasaporte · `)) fallos.push(`burbuja con folio: ${JSON.stringify(ultima)}`);
  // La burbuja vieja de jefa1 (sin folio, anterior a la columna) sigue sin etiqueta.
  const vieja = estado.burbujas.find((b) => b.texto === "Sí, a las 10:00.");
  if (!vieja || !vieja.meta.startsWith("jefa1 · ") || vieja.meta.includes("#")) fallos.push(`burbuja sin folio: ${JSON.stringify(vieja)}`);
  const despues = await ctx.chat(ELENA);
  const enApi = despues.mensajes[despues.mensajes.length - 1];
  if (!enApi.folio || enApi.folio.id !== pasaporte.id || enApi.folio.service_name !== "Cita de pasaporte") fallos.push(`la API no trae el folio del mensaje: ${JSON.stringify(enApi.folio)}`);
  const pasaporteDespues = despues.contacto.folios_abiertos.find((f) => f.id === pasaporte.id);
  if (!pasaporteDespues || pasaporteDespues.assigned_to !== "jefa1") fallos.push(`contestar sobre el pasaporte debía dejárselo a jefa1: ${JSON.stringify(pasaporteDespues)}`);
  // El acta no se tocó.
  const actaDespues = despues.contacto.folios_abiertos.find((f) => f.id === acta.id);
  if (!actaDespues || actaDespues.assigned_to !== "jefa1" || actaDespues.status !== "tomado") fallos.push(`el acta cambió al contestar sobre el pasaporte: ${JSON.stringify(actaDespues)}`);
  // La elección se conserva tras el sondeo (el <select> solo se reconstruye si cambia la lista).
  const tras = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (tras.selectorValor !== String(pasaporte.id)) fallos.push(`el selector perdió la elección tras enviar: ${JSON.stringify(tras.selectorValor)}`);
  // Un ticket_id que no es de este chat se rechaza: nada se envía y el CRM avisa.
  const cookie = await sesionHttp(ctx, "jefa1");
  const ajeno = await fetch(`${ctx.base}/inbox/${ELENA}/send`, {
    method: "POST", redirect: "manual",
    headers: { cookie, "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ body: "esto no debe salir", ticket_id: String(acta.id + 1000) }),
  });
  const destino = ajeno.headers.get("location") ?? "";
  if (ajeno.status !== 303 || !destino.includes("err=folio")) fallos.push(`un ticket_id ajeno respondió ${ajeno.status} → ${destino}`);
  if ((await ctx.chat(ELENA)).mensajes.length !== antes + 1) fallos.push("un ticket_id ajeno mandó un mensaje de todos modos");
  // Los criterios siguientes (reactivar el bot, bloquear) trabajan sobre el chat de Ana.
  r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "bandeja");
  if (r.error) throw new Error(r.error);
  await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.nombre === "Ana Lucía Ramírez" && s.composerVisible ? true : null;
  }, { timeout: 6000, label: "volver al chat de Ana" });
  if (fallos.length) throw new Error(fallos.join("; "));
  return `Elena: fila con 2 trámites (#${acta.id} tomado por jefa1, #${pasaporte.id} sin asignar), selector con 2 opciones; contestar sobre #${pasaporte.id} etiqueta la burbuja, lo registra la API y se lo queda jefa1; Ana (un folio) no muestra nada extra; un ticket_id ajeno → err=folio`;
}

async function criterioReactivarBot(ctx) {
  const antes = await ctx.chat(ANA);
  if (!antes.bot_pausado) throw new Error("Ana debía tener el bot pausado antes de la prueba");
  const r = await ctx.cdp.evaluate(enPagina.pulsarEnCabecera, "chat-bot-on");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Bot reactivado", 8000);
  await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.badges.includes("bot activo") && !s.botones.some((b) => b.clase.includes("chat-bot-on")) ? true : null;
  }, { timeout: 8000, label: "la insignia «bot activo» en la cabecera" });
  const chat = await ctx.chat(ANA);
  if (chat.bot_pausado) throw new Error("la API sigue diciendo bot_pausado");
  await waitFor(async () => {
    const t = (await ctx.cdp.evaluate(enPagina.tarjetas)).find((x) => x.id === ANA);
    return t && !t.badges.includes("bot pausado") ? true : null;
  }, { timeout: 8000, label: "que la tarjeta pierda «bot pausado»" });
  return "POST /inbox/{phone}/bot-on desde la cabecera; la insignia y la tarjeta se actualizan sin recargar";
}

async function criterioBloquear(ctx) {
  let desde = ctx.dialogos.length;
  let r = await ctx.cdp.evaluate(enPagina.pulsarEnCabecera, "chat-block");
  if (r.error) throw new Error(r.error);
  await ctx.esperarDialogo({ desde, prefijo: "¿Bloquear a este cliente?" });
  await ctx.esperarToast("Cliente bloqueado", 8000);
  await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return s.badges.includes("cliente bloqueado") && s.botones.some((b) => b.texto === "Desbloquear cliente") ? true : null;
  }, { timeout: 8000, label: "la insignia «cliente bloqueado»" });
  if (!(await ctx.chat(ANA)).bloqueado) throw new Error("la API no marca bloqueado");
  desde = ctx.dialogos.length;
  r = await ctx.cdp.evaluate(enPagina.pulsarEnCabecera, "chat-block");
  if (r.error) throw new Error(r.error);
  await ctx.esperarDialogo({ desde, prefijo: "¿Desbloquear a este cliente?" });
  await ctx.esperarToast("Cliente desbloqueado", 8000);
  await waitFor(async () => {
    const s = await ctx.cdp.evaluate(enPagina.estadoChat);
    return !s.badges.includes("cliente bloqueado") ? true : null;
  }, { timeout: 8000, label: "que desaparezca «cliente bloqueado»" });
  if ((await ctx.chat(ANA)).bloqueado) throw new Error("la API sigue marcando bloqueado");
  return "bloqueo y desbloqueo con confirm, toast, insignia y API coherentes";
}

async function criterioCerrarChat(ctx) {
  let r = await ctx.cdp.evaluate(enPagina.cerrarChat, "cerrar");
  if (r.error) throw new Error(r.error);
  let s = await ctx.cdp.evaluate(enPagina.estadoChat);
  const fallos = [];
  if (s.abierto || s.chatVisible || !s.boardVisible || s.btnBoardVisible) fallos.push(`tras ✕: ${JSON.stringify({ abierto: s.abierto, chat: s.chatVisible, board: s.boardVisible })}`);
  if (s.url !== "/crm") fallos.push(`URL tras cerrar: ${s.url}`);
  if (s.bandejaAbierta.length || s.tarjetaAbierta.length) fallos.push("quedó algo marcado is-open");
  if (s.foco !== "card is-open" && !String(s.foco).includes("card")) ctx.avisar(`el foco quedó en ${JSON.stringify(s.foco)}`);
  // Volver a abrir y cerrar con «Tablero» y con Escape.
  r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "tarjeta");
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.estadoChat)).abierto ? true : null, { timeout: 4000 });
  r = await ctx.cdp.evaluate(enPagina.cerrarChat, "tablero");
  s = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (s.abierto) fallos.push("el botón «Tablero» no cerró el chat");
  r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "tarjeta");
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.estadoChat)).abierto ? true : null, { timeout: 4000 });
  await ctx.cdp.evaluate(enPagina.teclaEscape);
  s = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (s.abierto) fallos.push("Escape no cerró el chat");
  if (fallos.length) throw new Error(fallos.join("; "));
  return "✕, «Tablero» y Escape vuelven al tablero, limpian ?chat= y la marca is-open";
}

async function criterioEtapas(ctx) {
  const antes = await ctx.tablero();
  let r = await ctx.cdp.evaluate(enPagina.abrirDialogoColumna);
  if (r.error) throw new Error(r.error);
  const fallos = [];
  if (!r.abierto) fallos.push("el diálogo no se abrió");
  if (r.swatches !== 8) fallos.push(`${r.swatches} swatches en el diálogo (esperaba 8)`);
  await ctx.capturar("04-dialogo-nueva-etapa-1400");
  r = await ctx.cdp.evaluate(enPagina.enviarDialogoColumna, "Por cobrar");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Etapa «Por cobrar» creada", 8000);
  await waitFor(async () => !(await ctx.cdp.evaluate(enPagina.dialogoAbierto)) ? true : null, { timeout: 4000, label: "que el diálogo se cierre" });
  const nueva = await waitFor(async () => {
    const t = await ctx.tablero();
    return t.etapas.find((e) => e.nombre === "Por cobrar") ?? null;
  }, { timeout: 6000, label: "la etapa «Por cobrar» en la API" });
  await waitFor(async () => {
    const cols = await ctx.cdp.evaluate(enPagina.columnasDOM);
    const t = await ctx.cdp.evaluate(enPagina.tablero);
    return cols.some((c) => c.id === String(nueva.id) && c.nombre === "Por cobrar") && t.tabs.some((x) => x.nombre === "Por cobrar") ? true : null;
  }, { timeout: 6000, label: "la columna y la pestaña «Por cobrar»" });
  // Mover a Beto ahí arrastrando (la tarjeta ya no trae «Mover a…») y comprobar contadores.
  r = await ctx.cdp.evaluate(enPagina.soltar, BETO, String(nueva.id));
  if (r.error) throw new Error(r.error);
  await ctx.esperarMovimiento(BETO, nueva.id, "Por cobrar");
  // Renombrar desde el menú ⋮.
  r = await ctx.cdp.evaluate(enPagina.abrirMenuColumna, String(nueva.id));
  if (r.error) throw new Error(r.error);
  if (!r.abierto || r.expandido !== "true") fallos.push(`menú ⋮: ${JSON.stringify(r)}`);
  r = await ctx.cdp.evaluate(enPagina.pulsarAccionColumna, String(nueva.id), "act-rename");
  if (r.error) throw new Error(r.error);
  r = await ctx.cdp.evaluate(enPagina.renombrarColumna, String(nueva.id), "Cobrado");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Etapa renombrada a «Cobrado»", 8000);
  await waitFor(async () => (await ctx.tablero()).etapas.some((e) => e.id === nueva.id && e.nombre === "Cobrado") ? true : null, { timeout: 6000, label: "el nombre «Cobrado» en la API" });
  // Color desde el menú ⋮.
  r = await ctx.cdp.evaluate(enPagina.abrirMenuColumna, String(nueva.id));
  r = await ctx.cdp.evaluate(enPagina.pulsarAccionColumna, String(nueva.id), "act-color");
  if (r.error) throw new Error(r.error);
  r = await ctx.cdp.evaluate(enPagina.pulsarSwatch, String(nueva.id), "#F4511E");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Color de «Cobrado» actualizado", 8000);
  await waitFor(async () => (await ctx.tablero()).etapas.some((e) => e.id === nueva.id && e.color === "#F4511E") ? true : null, { timeout: 6000, label: "el color #F4511E en la API" });
  // Eliminar: Beto vuelve a la primera etapa restante.
  const desde = ctx.dialogos.length;
  r = await ctx.cdp.evaluate(enPagina.abrirMenuColumna, String(nueva.id));
  r = await ctx.cdp.evaluate(enPagina.pulsarAccionColumna, String(nueva.id), "act-delete");
  if (r.error) throw new Error(r.error);
  const msg = await ctx.esperarDialogo({ desde, prefijo: "¿Eliminar la etapa «Cobrado»?" });
  if (!msg.includes("Su único contacto pasará a")) fallos.push(`el confirm no explica a dónde van los contactos: ${msg}`);
  await ctx.esperarToast("Etapa «Cobrado» eliminada", 8000);
  await waitFor(async () => {
    const t = await ctx.tablero();
    return !t.etapas.some((e) => e.id === nueva.id) && t.etapas.length === antes.etapas.length ? t : null;
  }, { timeout: 6000, label: "que la etapa desaparezca de la API" });
  const beto = await ctx.contacto(BETO);
  if (beto.etapa_id !== antes.etapas[0].id) fallos.push(`Beto quedó en ${beto.etapa_id} y no en la primera etapa (${antes.etapas[0].id})`);
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.columnasDOM)).length === antes.etapas.length ? true : null, { timeout: 6000, label: "que la columna desaparezca del DOM" });
  if (fallos.length) throw new Error(fallos.join("; "));
  return "«Por cobrar» creada desde el diálogo, Beto movido ahí, renombrada a «Cobrado», recoloreada y eliminada desde ⋮ (Beto volvió a la primera etapa)";
}

const mismoOrden = (a, b) => JSON.stringify(a.map(Number)) === JSON.stringify(b.map(Number));

async function criterioReordenarEtapas(ctx) {
  // Reordenar es acomodar el tablero, no a los clientes: la primera etapa viaja al final
  // arrastrando su cabecera (con la barra marcando el hueco), el orden se guarda y sobrevive a
  // una recarga, ninguna tarjeta cambia de columna, y el teclado la devuelve con «Mover antes».
  const antes = await ctx.tablero();
  const ids = antes.etapas.map((e) => e.id);
  if (ids.length < 3) throw new Error(`hacen falta 3 etapas y hay ${ids.length}`);
  const columnaDe = Object.fromEntries(antes.contactos.map((c) => [c.phone, c.etapa_id]));
  const primera = antes.etapas[0];
  const fallos = [];

  let r = await ctx.cdp.evaluate(enPagina.arrastrarColumna, String(ids[0]), String(ids[ids.length - 1]), "despues");
  if (r.error) throw new Error(r.error);
  if (!r.draggable) fallos.push("la cabecera no es arrastrable");
  if (!r.atenuada) fallos.push("sin is-col-dragging en dragstart");
  if (!r.dragoverCancelado) fallos.push("dragover sin preventDefault()");
  if (!r.marca || r.marca.id !== String(ids[ids.length - 1]) || r.marca.lado !== "despues") {
    fallos.push(`la barra de destino no quedó detrás de la última: ${JSON.stringify(r.marca)}`);
  }
  if (!r.dropCancelado) fallos.push("drop sin preventDefault()");
  if (!r.limpia) fallos.push("quedaron marcas de arrastre tras soltar");
  const esperado = [...ids.slice(1), ids[0]];
  await ctx.esperarToast(`«${primera.nombre}» quedó en el lugar ${ids.length} de ${ids.length}`, 8000);
  await waitFor(async () => {
    const api = (await ctx.tablero()).etapas.map((e) => e.id);
    const dom = (await ctx.cdp.evaluate(enPagina.columnasDOM)).map((c) => c.id);
    return mismoOrden(api, esperado) && mismoOrden(dom, esperado) ? true : null;
  }, { timeout: 6000, label: "el orden nuevo en la API y en el DOM" });

  await recargar(ctx.cdp);
  const t = await esperarTablero(ctx.cdp);
  const dom = (await ctx.cdp.evaluate(enPagina.columnasDOM)).map((c) => c.id);
  if (!mismoOrden(dom, esperado)) fallos.push(`tras recargar el orden es ${JSON.stringify(dom)} y no ${JSON.stringify(esperado)}`);
  const tabs = t.tabs.map((x) => x.filter).filter((f) => /^\d+$/.test(f));
  if (!mismoOrden(tabs, esperado)) fallos.push(`las pestañas quedaron en ${JSON.stringify(tabs)}`);
  const movidas = (await ctx.tablero()).contactos
    .filter((c) => c.phone in columnaDe && columnaDe[c.phone] !== c.etapa_id)
    .map((c) => c.phone);
  if (movidas.length) fallos.push(`reordenar movió tarjetas de columna: ${movidas.join(", ")}`);

  // Teclado: «Mover antes» desde el ⋮, un lugar por vez, hasta el principio.
  for (let lugar = ids.length - 1; lugar >= 1; lugar--) {
    r = await ctx.cdp.evaluate(enPagina.abrirMenuColumna, String(ids[0]));
    if (r.error) throw new Error(r.error);
    r = await ctx.cdp.evaluate(enPagina.pulsarAccionColumna, String(ids[0]), "act-left");
    if (r.error) throw new Error(r.error);
    await ctx.esperarToast(`«${primera.nombre}» quedó en el lugar ${lugar} de ${ids.length}`, 8000);
  }
  await waitFor(async () => mismoOrden((await ctx.tablero()).etapas.map((e) => e.id), ids) ? true : null,
    { timeout: 6000, label: "el orden original de vuelta en la API" });
  const foco = await ctx.cdp.evaluate(enPagina.foco);
  if (foco.columna !== String(ids[0]) || !String(foco.clase).includes("column-menu-btn")) {
    fallos.push(`tras «Mover antes» el foco quedó en ${JSON.stringify(foco)} y no en el ⋮ de la etapa movida`);
  }
  // En un extremo el botón existe pero no actúa, y dice por qué.
  await ctx.cdp.evaluate(enPagina.abrirMenuColumna, String(ids[0]));
  r = await ctx.cdp.evaluate(enPagina.pulsarAccionColumna, String(ids[0]), "act-left");
  if (!r.error || !r.error.includes("deshabilitado")) fallos.push("«Mover antes» sigue activo en la primera etapa");
  await ctx.cdp.evaluate(enPagina.teclaEscape);
  if (fallos.length) throw new Error(fallos.join("; "));
  return `«${primera.nombre}» arrastrada al final (barra en el hueco, orden en API, DOM y pestañas, persiste al recargar, ninguna tarjeta se movió) y devuelta al principio con «Mover antes»; el foco sigue en su ⋮`;
}

async function criterioAltaContacto(ctx) {
  const api = await ctx.tablero();
  const etapa = api.etapas[1];
  const r = await ctx.cdp.evaluate(enPagina.altaContacto, String(etapa.id), ALTA.name, ALTA.phone);
  if (r.error) throw new Error(r.error);
  if (!r.visible) throw new Error("el formulario de alta no se mostró");
  await ctx.esperarToast(`Contacto «${ALTA.name}» añadido a «${etapa.nombre}»`, 8000);
  const nuevo = await waitFor(async () => (await ctx.contacto(ALTA.canonico)) ?? null, { timeout: 6000, label: "el contacto nuevo en la API" });
  if (nuevo.etapa_id !== etapa.id) throw new Error(`el contacto nació en la etapa ${nuevo.etapa_id}`);
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.columnaDeTarjeta, ALTA.canonico)) === String(etapa.id) ? true : null, { timeout: 6000, label: "la tarjeta nueva en su columna" });
  // Repetido: 409 con mensaje del servidor en el toast (la petición 409 se descuenta del recolector).
  const marca = ctx.recolector.fallos.length;
  const r2 = await ctx.cdp.evaluate(enPagina.altaContacto, String(etapa.id), "Otra Ana", ANA);
  if (r2.error) throw new Error(r2.error);
  await ctx.esperarToast("Ya existe un cliente con ese teléfono", 8000);
  // El 409 lo provocó esta prueba a propósito: ni la respuesta ni su línea en el log de red cuentan.
  const extra = ctx.recolector.fallos.splice(marca).filter((f) => !/\b409\b/.test(f.texto));
  ctx.recolector.fallos.push(...extra);
  return `«${ALTA.name}» (${ALTA.phone} → ${nuevo.phone}) nació en «${etapa.nombre}»; el duplicado avisa con el 409 del servidor`;
}

async function criterioChatInicial(ctx) {
  await ctx.abrirCrm(`?chat=${ANA}`);
  const s = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    return e.abierto && e.burbujas.length > 0 ? e : null;
  }, { timeout: 8000, label: "que /crm?chat= abra el chat solo" });
  if (s.nombre !== "Ana Lucía Ramírez") throw new Error(`abrió ${JSON.stringify(s.nombre)}`);
  await ctx.abrirCrm("?chat=<script>");
  const t = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (t.abierto) throw new Error("un ?chat= inválido abrió un chat");
  return `/crm?chat=${ANA} abre el chat de Ana al cargar; un valor inválido se ignora`;
}

async function criterioToggleTray(ctx) {
  const a = await ctx.cdp.evaluate(enPagina.pulsarToggleTray);
  const b = await ctx.cdp.evaluate(enPagina.pulsarToggleTray);
  if (!a.colapsada || a.texto !== "Mostrar bandeja" || a.aria !== "false") throw new Error(`al ocultar: ${JSON.stringify(a)}`);
  if (b.colapsada || b.texto !== "Ocultar bandeja" || b.aria !== "true") throw new Error(`al mostrar: ${JSON.stringify(b)}`);
  return "«Ocultar bandeja» alterna is-collapsed, el texto y aria-expanded";
}

async function criterioColaborador(ctx) {
  await ctx.login("colab1");
  const t = await ctx.abrirCrm();
  const api = await ctx.tablero();
  const fallos = [];
  // Su tablero es suyo (2026-09-29): «Nueva etapa» y un ⋮ por columna, como cualquiera.
  if (!t.nuevaEtapa) fallos.push("el colaborador no ve «Nueva etapa» en su propio tablero");
  if (t.menus !== api.etapas.length) fallos.push(`el colaborador ve ${t.menus} menús ⋮ (esperaba ${api.etapas.length})`);
  if (t.altas !== 0) fallos.push(`el colaborador ve ${t.altas} botones «Añadir contacto»`);
  if (t.miembrosTira !== 0 || t.tiraVisible) fallos.push("el colaborador ve la tira de miembros");
  if (api.miembros.length) fallos.push("la API le da miembros a un colaborador");
  // Solo las conversaciones de las solicitudes que toma: Ana (folio tomado por colab1).
  if (!mismoConjunto(api.contactos.map((c) => c.phone), [ANA])) fallos.push(`la API le da a colab1 ${JSON.stringify(api.contactos.map((c) => c.phone))} y no solo a Ana`);
  if (!mismoConjunto(t.bandeja, [ANA])) fallos.push(`la bandeja muestra ${JSON.stringify(t.bandeja)}`);
  if (t.tarjetas !== 1) fallos.push(`${t.tarjetas} tarjetas en el tablero (esperaba 1)`);
  const destino = api.etapas[2];
  const r = await ctx.cdp.evaluate(enPagina.soltar, ANA, String(destino.id));
  if (r.error) throw new Error(r.error);
  await ctx.esperarMovimiento(ANA, destino.id, destino.nombre);
  // Abre el chat: no hay botón de bloqueo para quien no es admin.
  await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "tarjeta");
  const s = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    return e.burbujas.length > 0 ? e : null;
  }, { timeout: 6000, label: "el chat de Ana como colab1" });
  if (s.botones.some((b) => b.clase.includes("chat-block"))) fallos.push("el colaborador ve «Bloquear cliente»");
  // El saliente que mandó jefa1 no es «suyo»: sin «eliminar».
  if (s.burbujas.some((b) => b.dir === "out" && b.meta.startsWith("jefa1") && b.eliminar)) fallos.push("colab1 puede ocultar un mensaje de jefa1");
  // Un chat que no es suyo responde «no existe», aunque sea de su oficina. El 404 lo provoca
  // esta prueba a propósito: ni la respuesta ni su línea en el log de red cuentan como fallo.
  const marca = ctx.recolector.fallos.length;
  const ajeno = await ctx.cdp.evaluate(enPagina.api, `/crm/api/chat/${BETO}`);
  if (ajeno.status !== 404) fallos.push(`el chat de Beto (sin folio suyo) respondió ${ajeno.status} a colab1`);
  const extra = ctx.recolector.fallos.splice(marca).filter((f) => !(f.texto.includes(`/crm/api/chat/${BETO}`) && /\b404\b/.test(f.texto)));
  ctx.recolector.fallos.push(...extra);
  // Un colaborador sin conversaciones ve el aviso que lo explica.
  await ctx.login("colab2");
  const t2 = await ctx.abrirCrm();
  if (t2.tarjetas !== 0 || !(t2.vacioBandeja ?? "").startsWith("Sin conversaciones todavía")) fallos.push(`colab2 (sin folios) ve ${t2.tarjetas} tarjetas y la bandeja dice ${JSON.stringify(t2.vacioBandeja)}`);
  if (fallos.length) throw new Error(fallos.join("; "));
  return `colab1 solo ve a Ana (su folio), con los controles de SU tablero pero sin miembros ni altas, la mueve a «${destino.nombre}» y abre su chat; Beto le responde 404; colab2 sin folios ve el aviso`;
}

async function tableroHttp(ctx, cookie) {
  const r = await fetch(`${ctx.base}/crm/api/tablero`, { headers: { cookie, Accept: "application/json" } });
  if (r.status !== 200) throw new Error(`GET /crm/api/tablero de otra sesión → HTTP ${r.status}`);
  return r.json();
}

async function criterioTableroPersonal(ctx) {
  // colab1 organiza SU tablero —crea una etapa, mete a Ana, la renombra, la pone primera y la
  // elimina— mientras jefa1, desde su propia sesión, no ve cambiar nada del suyo.
  await ctx.login("colab1");
  await ctx.abrirCrm();
  const cookieJefa = await sesionHttp(ctx, "jefa1");
  const deJefa = (t) => ({
    etapas: t.etapas.map((e) => [e.id, e.nombre, e.color]),
    ana: t.contactos.find((c) => c.phone === ANA)?.etapa_id ?? null,
  });
  const jefaAntes = deJefa(await tableroHttp(ctx, cookieJefa));
  const antes = await ctx.tablero();
  const fallos = [];

  let r = await ctx.cdp.evaluate(enPagina.abrirDialogoColumna);
  if (r.error) throw new Error(r.error);
  r = await ctx.cdp.evaluate(enPagina.enviarDialogoColumna, "Mis urgentes");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Etapa «Mis urgentes» creada", 8000);
  const nueva = await waitFor(async () => (await ctx.tablero()).etapas.find((e) => e.nombre === "Mis urgentes") ?? null,
    { timeout: 6000, label: "la etapa «Mis urgentes» en el tablero de colab1" });
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.columnasDOM)).some((c) => c.id === String(nueva.id)) ? true : null,
    { timeout: 6000, label: "la columna «Mis urgentes» en el DOM" });
  r = await ctx.cdp.evaluate(enPagina.soltar, ANA, String(nueva.id));
  if (r.error) throw new Error(r.error);
  await ctx.esperarMovimiento(ANA, nueva.id, "Mis urgentes");
  r = await ctx.cdp.evaluate(enPagina.abrirMenuColumna, String(nueva.id));
  if (r.error) throw new Error(r.error);
  r = await ctx.cdp.evaluate(enPagina.pulsarAccionColumna, String(nueva.id), "act-rename");
  if (r.error) throw new Error(r.error);
  r = await ctx.cdp.evaluate(enPagina.renombrarColumna, String(nueva.id), "Urgentes");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Etapa renombrada a «Urgentes»", 8000);
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.columnasDOM)).some((c) => c.id === String(nueva.id) && c.nombre === "Urgentes") ? true : null,
    { timeout: 6000, label: "la columna renombrada en el DOM" });
  // Si el aviso no llega, que el fallo diga qué pasó: «visible: null» a los 8 s no distingue
  // «el arrastre no reordenó» de «llegó otro aviso» ni de «el PUT sigue en vuelo».
  await ctx.cdp.evaluate(enPagina.grabarToasts);
  r = await ctx.cdp.evaluate(enPagina.arrastrarColumna, String(nueva.id), String(antes.etapas[0].id), "antes");
  if (r.error) throw new Error(r.error);
  try {
    await ctx.esperarToast(`«Urgentes» quedó en el lugar 1 de ${antes.etapas.length + 1}`, 8000);
  } catch (e) {
    const avisos = await ctx.cdp.evaluate(() => window.__qaToasts ?? null);
    const columnas = (await ctx.cdp.evaluate(enPagina.columnasDOM)).map((c) => c.nombre);
    const enVuelo = [...ctx.recolector.urls.values()].filter((u) => !u.includes("/crm/api/eventos"));
    throw new Error(`${e.message}; arrastre ${JSON.stringify(r)}; avisos ${JSON.stringify(avisos)}; columnas ${JSON.stringify(columnas)}; en vuelo ${JSON.stringify(enVuelo)}`);
  }
  await waitFor(async () => (await ctx.tablero()).etapas[0]?.id === nueva.id ? true : null,
    { timeout: 6000, label: "«Urgentes» primera en el tablero de colab1" });

  const jefaEnMedio = deJefa(await tableroHttp(ctx, cookieJefa));
  if (JSON.stringify(jefaEnMedio) !== JSON.stringify(jefaAntes)) {
    fallos.push(`el tablero de jefa1 cambió con lo que hizo colab1: ${JSON.stringify(jefaAntes)} → ${JSON.stringify(jefaEnMedio)}`);
  }

  const desde = ctx.dialogos.length;
  await ctx.cdp.evaluate(enPagina.abrirMenuColumna, String(nueva.id));
  r = await ctx.cdp.evaluate(enPagina.pulsarAccionColumna, String(nueva.id), "act-delete");
  if (r.error) throw new Error(r.error);
  const msg = await ctx.esperarDialogo({ desde, prefijo: "¿Eliminar la etapa «Urgentes»?" });
  if (!msg.includes("Solo cambia tu tablero")) fallos.push(`el confirm no dice que solo cambia su tablero: ${msg}`);
  await ctx.esperarToast("Etapa «Urgentes» eliminada", 8000);
  const despues = await waitFor(async () => {
    const t = await ctx.tablero();
    return !t.etapas.some((e) => e.id === nueva.id) ? t : null;
  }, { timeout: 6000, label: "que «Urgentes» desaparezca del tablero de colab1" });
  const ana = despues.contactos.find((c) => c.phone === ANA);
  if (ana?.etapa_id !== despues.etapas[0].id) fallos.push(`Ana quedó en ${ana?.etapa_id} y no en la primera etapa de colab1 (${despues.etapas[0].id})`);
  const jefaDespues = deJefa(await tableroHttp(ctx, cookieJefa));
  if (JSON.stringify(jefaDespues) !== JSON.stringify(jefaAntes)) fallos.push("eliminar una etapa de colab1 cambió el tablero de jefa1");
  if (fallos.length) throw new Error(fallos.join("; "));
  return "colab1 creó, llenó, renombró, arrastró al principio y eliminó «Urgentes» en SU tablero (Ana volvió a su primera etapa); el de jefa1 no cambió en ningún momento";
}

// ---- «Estrategia»: mover el chat de etapa sin salir de él (crm-estrategia.js) --------

const esperarEstrategia = (ctx, condicion, label) =>
  waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estrategiaEstado);
    return condicion(e) ? e : null;
  }, { timeout: 8000, label });

const marcadasEstrategia = (e) => JSON.stringify(e.opciones.filter((o) => o.marcada).map((o) => o.id));

// Contraste WCAG entre dos colores calculados del navegador («rgb(r, g, b)»).
function contrasteRgb(a, b) {
  const lum = (s) => {
    const [r, g, bl] = s.match(/[\d.]+/g).slice(0, 3).map((c) => {
      const v = Number(c) / 255;
      return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * r + 0.7152 * g + 0.0722 * bl;
  };
  const [claro, oscuro] = [lum(a), lum(b)].sort((x, y) => y - x);
  return (claro + 0.05) / (oscuro + 0.05);
}

// Espacio de verdad sobre la casilla enfocada: así se marca una casilla con el teclado.
async function teclaEspacio(ctx) {
  const base = { key: " ", code: "Space", windowsVirtualKeyCode: 32, nativeVirtualKeyCode: 32 };
  await ctx.cdp.send("Input.dispatchKeyEvent", { type: "keyDown", text: " ", unmodifiedText: " ", ...base });
  await ctx.cdp.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

async function criterioEstrategiaColaborador(ctx) {
  // colab1 sitúa a Ana —su folio— en otra etapa de SU tablero sin salir del chat: una casilla
  // por etapa con su contador, una sola marcada, la actual no se desmarca, el menú se queda
  // abierto con los números nuevos, y el tablero de jefa1 no se entera.
  await ctx.login("colab1");
  const cookieJefa = await sesionHttp(ctx, "jefa1");
  const anaDeJefa = async () => (await tableroHttp(ctx, cookieJefa)).contactos.find((c) => c.phone === ANA)?.etapa_id ?? null;
  const jefaAntes = await anaDeJefa();
  await abrirChatDe(ctx, ANA);
  const etapas = await ctx.api("/crm/api/etapas");
  const origen = (await ctx.chat(ANA)).contacto.etapa_id;
  const destino = etapas.find((x) => x.id !== origen);
  if (!destino) throw new Error("colab1 necesita al menos dos etapas");
  const fallos = [];

  let e = await ctx.cdp.evaluate(enPagina.estrategiaEstado);
  if (!e.boton) throw new Error("no existe #crm-btn-estrategia");
  if (!e.boton.enRiel || e.boton.antes !== "transferir" || e.boton.texto !== "Estrategia") fallos.push(`la píldora no va justo después de «Transferir»: ${JSON.stringify(e.boton)}`);
  if (e.boton.seccion !== null) fallos.push("«Estrategia» lleva data-seccion: el cajón la tomaría como suya");
  if (e.abierto || e.boton.expandido) fallos.push("el menú nace abierto");

  let r = await ctx.cdp.evaluate(enPagina.estrategiaPulsar);
  if (r.error) throw new Error(r.error);
  e = await esperarEstrategia(ctx, (s) => s.abierto && s.opciones.length === etapas.length, "el menú con las etapas de colab1");
  const visto = e.opciones.map((o) => [o.id, o.nombre, o.num]);
  const esperado = etapas.map((x) => [String(x.id), x.nombre, String(x.count)]);
  if (JSON.stringify(visto) !== JSON.stringify(esperado)) fallos.push(`opciones ${JSON.stringify(visto)} ≠ GET /crm/api/etapas ${JSON.stringify(esperado)}`);
  if (!e.opciones.every((o) => o.tipo === "checkbox" && o.orden)) fallos.push(`no todas van casilla → nombre → contador: ${JSON.stringify(e.opciones)}`);
  if (marcadasEstrategia(e) !== JSON.stringify([String(origen)])) fallos.push(`marcadas ${marcadasEstrategia(e)} con Ana en ${origen}`);
  const actual = e.opciones.find((o) => o.id === String(origen));
  if (!actual?.bloqueada) fallos.push("la etapa actual no lleva aria-disabled");
  if (!actual?.enfocada) fallos.push(`al abrir, el foco quedó en ${JSON.stringify(e.foco)} y no en la etapa actual`);
  if (!/ \d+ chats?$/.test(actual?.leido ?? "")) fallos.push(`la etiqueta se lee ${JSON.stringify(actual?.leido)}: el número va sin «chats»`);
  if (!e.boton.expandido) fallos.push("abierto, el botón no dice aria-expanded=true");
  if (e.cajonAbierto) fallos.push("«Estrategia» abrió el cajón del folio");
  await ctx.capturar("40-estrategia-colab1");

  // La actual no se desmarca: el clic se cancela y no sale ningún PUT.
  r = await ctx.cdp.evaluate(enPagina.estrategiaMarcar, String(origen), "casilla");
  if (r.error) throw new Error(r.error);
  if (!r.marcada) fallos.push("un clic desmarcó la etapa actual");
  await dormir(700);
  if (await ctx.cdp.evaluate(enPagina.toast)) fallos.push("el clic en la etapa actual sacó un aviso");
  if ((await ctx.chat(ANA)).contacto.etapa_id !== origen) fallos.push("el clic en la etapa actual movió a Ana");

  // Marcar otra —clic en el nombre—: el PUT del arrastre mueve a Ana y el menú se queda.
  r = await ctx.cdp.evaluate(enPagina.estrategiaMarcar, String(destino.id), "nombre");
  if (r.error) throw new Error(r.error);
  const aviso = await ctx.esperarToast("Movido a");
  if (!aviso.includes(destino.nombre)) fallos.push(`el aviso dice ${JSON.stringify(aviso)}`);
  await waitFor(async () => (await ctx.chat(ANA)).contacto.etapa_id === destino.id ? true : null,
    { timeout: 6000, label: `Ana en «${destino.nombre}» según la API` });
  const nuevas = await ctx.api("/crm/api/etapas");
  const numsApi = JSON.stringify(nuevas.map((x) => [String(x.id), String(x.count)]));
  e = await esperarEstrategia(ctx, (s) => s.abierto
    && JSON.stringify(s.opciones.map((o) => [o.id, o.num])) === numsApi
    && marcadasEstrategia(s) === JSON.stringify([String(destino.id)]),
    "el menú abierto con la marca nueva y los contadores del servidor");
  const cuenta = (lista, id) => lista.find((x) => x.id === id)?.count;
  if (cuenta(nuevas, origen) !== cuenta(etapas, origen) - 1 || cuenta(nuevas, destino.id) !== cuenta(etapas, destino.id) + 1) {
    fallos.push(`los contadores no siguieron a Ana: ${JSON.stringify(etapas.map((x) => x.count))} → ${JSON.stringify(nuevas.map((x) => x.count))}`);
  }
  const columna = await ctx.cdp.evaluate(enPagina.columnaDeTarjeta, ANA);
  if (columna !== String(destino.id)) fallos.push(`la tarjeta de Ana quedó en la columna ${columna}`);
  await ctx.capturar("41-estrategia-movida");

  // Escape de verdad: cierra el menú y NADA más, y el foco vuelve al botón.
  await teclaReal(ctx, "Escape", 27);
  e = await esperarEstrategia(ctx, (s) => !s.abierto, "que Escape cierre el menú");
  if (!e.chatAbierto) fallos.push("Escape cerró el chat junto con el menú");
  if (e.foco !== "crm-btn-estrategia") fallos.push(`tras Escape el foco quedó en ${JSON.stringify(e.foco)}`);
  if (e.boton.expandido) fallos.push("cerrado, el botón sigue con aria-expanded=true");

  // Con el teclado: reabrir, ir a la etapa de origen y marcarla con Espacio. El foco se queda
  // en la casilla: la lista no se rehace al moverse.
  await ctx.cdp.evaluate(enPagina.estrategiaPulsar);
  await esperarEstrategia(ctx, (s) => s.abierto && marcadasEstrategia(s) === JSON.stringify([String(destino.id)]),
    "el menú reabierto con Ana en su etapa nueva");
  r = await ctx.cdp.evaluate(enPagina.estrategiaEnfocar, String(origen));
  if (r.error || !r.ok) throw new Error(r.error ?? "no se pudo enfocar la casilla de origen");
  await teclaEspacio(ctx);
  await ctx.esperarToast("Movido a");
  await waitFor(async () => (await ctx.chat(ANA)).contacto.etapa_id === origen ? true : null,
    { timeout: 6000, label: "Ana de vuelta en su etapa con Espacio" });
  e = await esperarEstrategia(ctx, (s) => s.abierto && marcadasEstrategia(s) === JSON.stringify([String(origen)]),
    "el menú con Ana de vuelta");
  if (!e.opciones.find((o) => o.id === String(origen))?.enfocada) fallos.push(`tras mover con Espacio el foco quedó en ${JSON.stringify(e.foco)}`);

  // Tab tras la última casilla: el foco sale del menú y el menú se cierra.
  r = await ctx.cdp.evaluate(enPagina.estrategiaEnfocar, "ultima");
  if (r.error) throw new Error(r.error);
  await teclaReal(ctx, "Tab", 9);
  await esperarEstrategia(ctx, (s) => !s.abierto, "que salir del menú con Tab lo cierre");

  // Un clic fuera también lo cierra, sin cerrar el chat.
  await ctx.cdp.evaluate(enPagina.estrategiaPulsar);
  await esperarEstrategia(ctx, (s) => s.abierto, "el menú reabierto");
  await ctx.cdp.evaluate(() => { document.querySelector("#crm-chat-thread").click(); return true; });
  e = await esperarEstrategia(ctx, (s) => !s.abierto, "que un clic fuera cierre el menú");
  if (!e.chatAbierto) fallos.push("el clic fuera cerró el chat");

  const jefaDespues = await anaDeJefa();
  if (jefaDespues !== jefaAntes) fallos.push(`mover a Ana en el tablero de colab1 la movió en el de jefa1: ${jefaAntes} → ${jefaDespues}`);
  if (fallos.length) throw new Error(fallos.join("; "));
  return `colab1 marcó «${destino.nombre}» y Ana se movió en SU tablero (API, tarjeta y contadores), con el menú abierto y una sola marcada; la actual no se desmarca; Escape, Tab y un clic fuera solo cierran el menú; con Espacio volvió a su etapa; el tablero de jefa1 no cambió`;
}

async function criterioEstrategiaAdmin(ctx) {
  // jefa1 con la búsqueda puesta: el menú cuenta TODO su tablero, no lo que se ve. Beto no tiene
  // folio y la píldora sigue viva; Diego —todos sus folios cerrados, fuera del tablero— se mueve
  // igual y no cuenta en ninguna columna. El círculo es oliva con número blanco y cabe a 400 px.
  await ctx.login("jefa1");
  await ctx.abrirCrm();
  const fallos = [];
  await ctx.cdp.evaluate(enPagina.buscar, "Beto");
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.tablero)).tarjetas === 1 ? true : null,
    { timeout: 6000, label: "el tablero filtrado a Beto" });
  let r = await ctx.cdp.evaluate(enPagina.abrirTarjeta, BETO, "tarjeta");
  if (r.error) throw new Error(r.error);
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.estadoChat)).abierto ? true : null,
    { timeout: 6000, label: "el chat de Beto" });
  const cajon = await esperarCajon(ctx);
  if (!cajon.riel.find((b) => b.seccion === "transferir")?.deshabilitado) ctx.avisar("sin folio «Transferir» no se apagó: la comparación con «Estrategia» no dice nada");
  const etapas = await ctx.api("/crm/api/etapas");
  const nums = (lista) => JSON.stringify(lista.map((x) => String(x.count)));
  if (nums(etapas) === nums((await ctx.api("/crm/api/tablero?q=Beto")).etapas)) ctx.avisar("con «Beto» el tablero cuenta igual que sin búsqueda: la prueba de «sin filtros» no discrimina");

  let e = await ctx.cdp.evaluate(enPagina.estrategiaEstado);
  if (e.boton?.deshabilitado) fallos.push("sin folio «Estrategia» quedó apagado: la etapa es del chat");
  r = await ctx.cdp.evaluate(enPagina.estrategiaPulsar);
  if (r.error) throw new Error(r.error);
  e = await esperarEstrategia(ctx, (s) => s.abierto && s.opciones.length === etapas.length, "el menú de jefa1");
  const vistos = JSON.stringify(e.opciones.map((o) => o.num));
  if (vistos !== nums(etapas)) fallos.push(`con la búsqueda puesta el menú cuenta ${vistos} y el tablero completo ${nums(etapas)}`);
  const c = e.circulo;
  if (!c || c.fondo !== "rgb(86, 95, 29)" || c.color !== "rgb(255, 255, 255)") {
    fallos.push(`el contador no es oliva con número blanco: ${JSON.stringify(c)}`);
  } else {
    const k = contrasteRgb(c.fondo, c.color);
    if (k < 4.5) fallos.push(`contraste del contador ${k.toFixed(2)}:1`);
    if (c.radio < c.alto / 2 - 0.5) fallos.push(`el contador no es redondo (radio ${c.radio}, alto ${c.alto})`);
    if (c.texto.length === 1 && Math.abs(c.ancho - c.alto) > 1) fallos.push(`con una cifra no es un círculo: ${c.ancho}×${c.alto}`);
  }
  if (e.menuCaja.arriba < e.botonAbajo) fallos.push("el menú no quedó debajo de la píldora");
  await ctx.capturar("42-estrategia-jefa1");
  await teclaReal(ctx, "Escape", 27);
  await esperarEstrategia(ctx, (s) => !s.abierto, "que Escape cierre el menú");

  // Diego: todos sus folios cerrados, sin tarjeta. Se mueve igual y no cambia ningún contador.
  await abrirChatDe(ctx, DIEGO);
  const antes = await ctx.api("/crm/api/etapas");
  const origen = (await ctx.chat(DIEGO)).contacto.etapa_id;
  const destino = antes.find((x) => x.id !== origen);
  if ((await ctx.cdp.evaluate(enPagina.columnaDeTarjeta, DIEGO)) !== null) ctx.avisar("Diego tiene tarjeta: el caso «chat fuera del tablero» no se está probando");
  await ctx.cdp.evaluate(enPagina.estrategiaPulsar);
  e = await esperarEstrategia(ctx, (s) => s.abierto && s.opciones.length === antes.length, "el menú en el chat de Diego");
  if (marcadasEstrategia(e) !== JSON.stringify([String(origen)])) fallos.push(`Diego: marcadas ${marcadasEstrategia(e)} y su etapa es ${origen}`);
  r = await ctx.cdp.evaluate(enPagina.estrategiaMarcar, String(destino.id), "casilla");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Movido a");
  await waitFor(async () => (await ctx.chat(DIEGO)).contacto.etapa_id === destino.id ? true : null,
    { timeout: 6000, label: `Diego en «${destino.nombre}» según la API` });
  const despues = await ctx.api("/crm/api/etapas");
  if (nums(despues) !== nums(antes)) fallos.push(`mover a Diego (fuera del tablero) cambió los contadores: ${nums(antes)} → ${nums(despues)}`);
  await esperarEstrategia(ctx, (s) => s.abierto && marcadasEstrategia(s) === JSON.stringify([String(destino.id)])
    && JSON.stringify(s.opciones.map((o) => o.num)) === nums(despues), "el menú de Diego con la marca nueva");

  // 400 px: el menú cabe en la pantalla, sin scroll horizontal de página.
  await ctx.ancho(400, 800);
  await dormir(300);
  e = await ctx.cdp.evaluate(enPagina.estrategiaEstado);
  if (!e.abierto) fallos.push("cambiar el ancho cerró el menú");
  if (e.menuCaja.izq < 0 || e.menuCaja.der > e.anchoVentana || e.scrollX) {
    fallos.push(`a 400 px el menú se sale: ${JSON.stringify({ caja: e.menuCaja, ventana: e.anchoVentana, scrollX: e.scrollX })}`);
  }
  await ctx.capturar("43-estrategia-400");

  // Diego de vuelta a su etapa: lo que sigue no cuenta con este cambio.
  r = await ctx.cdp.evaluate(enPagina.estrategiaMarcar, String(origen), "nombre");
  if (r.error) throw new Error(r.error);
  await waitFor(async () => (await ctx.chat(DIEGO)).contacto.etapa_id === origen ? true : null,
    { timeout: 6000, label: "Diego de vuelta en su etapa" });
  await ctx.cdp.send("Emulation.clearDeviceMetricsOverride");
  if (fallos.length) throw new Error(fallos.join("; "));
  return `con «Beto» buscado el menú cuenta todo el tablero (${nums(etapas)}); sin folio la píldora sigue viva; círculo oliva con blanco a ${contrasteRgb(c.fondo, c.color).toFixed(2)}:1; Diego, sin tarjeta, se movió y volvió sin tocar los contadores; a 400 px cabe`;
}

async function criterioAdminOficina(ctx) {
  await ctx.login("jefe2");
  const t = await ctx.abrirCrm();
  const api = await ctx.tablero();
  const fallos = [];
  if (!t.nuevaEtapa || t.menus !== api.etapas.length) fallos.push("el admin de oficina no ve los controles de etapas de SU tablero");
  if (!mismoConjunto(api.contactos.map((c) => c.phone), [CARLA])) fallos.push(`jefe2 ve ${JSON.stringify(api.contactos.map((c) => c.phone))}`);
  if (!mismoConjunto(t.bandeja, [CARLA])) fallos.push(`la bandeja de jefe2 trae ${JSON.stringify(t.bandeja)}`);
  await ctx.cdp.evaluate(enPagina.abrirTarjeta, CARLA, "tarjeta");
  const s = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    return e.burbujas.length === 1 ? e : null;
  }, { timeout: 6000, label: "el chat de Carla" });
  if (!s.botones.some((b) => b.clase.includes("chat-block"))) fallos.push("el admin de oficina no ve «Bloquear cliente»");
  if (fallos.length) throw new Error(fallos.join("; "));
  return "jefe2 (admin de Puebla) solo ve a Carla, con los controles de su tablero, y sí puede bloquear";
}

async function criterioResponsivo(ctx) {
  await ctx.login("jefa1");
  const fallos = [];
  // 900 px: la bandeja pasa arriba y se puede plegar; las columnas se apilan.
  await ctx.ancho(900, 1100);
  await ctx.abrirCrm();
  let m = await ctx.cdp.evaluate(() => ({
    toggle: getComputedStyle(document.querySelector("#crm-btn-toggle-tray")).display !== "none",
    apiladas: getComputedStyle(document.querySelector("#crm-board")).flexDirection === "column",
    filas: getComputedStyle(document.querySelector(".crm-layout")).gridTemplateColumns.split(" ").length,
    scrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  }));
  if (!m.toggle) fallos.push("a 900 px no se ve «Ocultar bandeja»");
  if (!m.apiladas) fallos.push("a 900 px las columnas no se apilan");
  if (m.filas !== 1) fallos.push(`a 900 px la rejilla tiene ${m.filas} columnas`);
  if (m.scrollX) fallos.push("a 900 px hay scroll horizontal de página");
  await ctx.capturar("05-tablero-900");
  await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "tarjeta");
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.estadoChat)).burbujas.length > 0 ? true : null, { timeout: 6000 });
  m = await ctx.cdp.evaluate(() => {
    const b = document.querySelector("#crm-btn-board");
    const r = b ? b.getBoundingClientRect() : null;
    const fila = document.querySelector("#crm-tabs").getBoundingClientRect();
    return {
      bandeja: getComputedStyle(document.querySelector("#crm-tray")).display,
      // Se oculta la lista de pestañas, no la fila: el botón «Tablero» vive en la fila.
      pestanas: getComputedStyle(document.querySelector("#crm-tabs-list")).display,
      tablero: !!b && !b.hidden && b.getClientRects().length > 0,
      alInicio: !!r && Math.abs(r.left - fila.left) < 24,
    };
  });
  if (m.bandeja !== "none" || m.pestanas !== "none") fallos.push(`a 900 px con el chat abierto siguen visibles bandeja/pestañas: ${JSON.stringify(m)}`);
  if (!m.tablero) fallos.push("a 900 px con el chat abierto no se ve el botón «Tablero»");
  if (!m.alInicio) fallos.push(`a 900 px el botón «Tablero» no quedó al inicio de la fila de pestañas: ${JSON.stringify(m)}`);
  await ctx.capturar("06-chat-900");
  // 400 px: teléfono.
  await ctx.ancho(400, 800);
  await ctx.abrirCrm();
  m = await ctx.cdp.evaluate(() => ({
    scrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    columnas: document.querySelectorAll("#crm-board section.column").length,
  }));
  if (m.scrollX) fallos.push("a 400 px hay scroll horizontal de página");
  await ctx.capturar("07-tablero-400");
  await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "tarjeta");
  await waitFor(async () => (await ctx.cdp.evaluate(enPagina.estadoChat)).burbujas.length > 0 ? true : null, { timeout: 6000 });
  m = await ctx.cdp.evaluate(() => {
    const b = document.querySelector("#crm-btn-board");
    return {
      tablero: !!b && !b.hidden && b.getClientRects().length > 0,
      scrollX: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    };
  });
  if (!m.tablero) fallos.push("a 400 px con el chat abierto no se ve el botón «Tablero»");
  if (m.scrollX) fallos.push("a 400 px con el chat abierto hay scroll horizontal de página");
  await ctx.capturar("08-chat-400");
  await ctx.cdp.send("Emulation.clearDeviceMetricsOverride");
  if (fallos.length) throw new Error(fallos.join("; "));
  return "a 900 px la bandeja sube y se pliega, las columnas se apilan y el chat ocupa todo; a 400 px sin scroll horizontal";
}

// ---- Cajón del folio (crm-panel.js) ------------------------------------------

async function abrirChatDe(ctx, phone, conBurbujas = true) {
  await ctx.abrirCrm(`?chat=${phone}`);
  return waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    return e.abierto && (!conBurbujas || e.burbujas.length > 0) ? e : null;
  }, { timeout: 8000, label: `el chat de ${phone}` });
}

const esperarCajon = (ctx) =>
  waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.cargado && e.riel.length === 6 ? e : null;
  }, { timeout: 8000, label: "los datos del cajón del folio" });

const esperarCajonCerrado = (ctx) =>
  waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return !e.abierto ? e : null;
  }, { timeout: 4000, label: "que el cajón se cierre" });

async function abrirSeccion(ctx, seccion) {
  const r = await ctx.cdp.evaluate(enPagina.panelPulsar, seccion);
  if (r.error) throw new Error(r.error);
  return waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.abierto && e.seccionVisible.length === 1 && e.seccionVisible[0] === seccion ? e : null;
  }, { timeout: 5000, label: `la sección «${seccion}» del cajón` });
}

async function criterioCajonAbrirCerrar(ctx) {
  await ctx.login("jefa1");
  await abrirChatDe(ctx, ANA);
  const fallos = [];
  let p = await esperarCajon(ctx);
  if (p.abierto) fallos.push("el cajón nace abierto");
  if (!p.inerte) fallos.push("el cajón cerrado no queda inerte");
  const cobro = p.riel.find((b) => b.seccion === "cobro");
  if (cobro.num !== "1") fallos.push(`la píldora de cobro dice ${JSON.stringify(cobro.num)} y hay 1 cobro sembrado`);

  // Cada píldora abre SU sección, con su título y una sola marcada.
  for (const { seccion, titulo } of p.riel) {
    p = await abrirSeccion(ctx, seccion);
    if (p.titulo !== titulo) fallos.push(`«${seccion}» se titula ${JSON.stringify(p.titulo)}`);
    if (!(p.sub ?? "").includes("Ana")) fallos.push(`la cabecera del cajón no dice de quién es: ${JSON.stringify(p.sub)}`);
    if (p.inerte) fallos.push(`«${seccion}» quedó inerte estando abierta`);
    if (p.foco !== "crm-panel-title") fallos.push(`al abrir «${seccion}» el foco quedó en ${JSON.stringify(p.foco)}`);
    const marcadas = p.riel.filter((b) => b.expandido).map((b) => b.seccion);
    if (marcadas.length !== 1 || marcadas[0] !== seccion) fallos.push(`aria-expanded marca ${JSON.stringify(marcadas)} con «${seccion}» abierta`);
  }

  // La píldora activa cierra, y el foco vuelve a ella.
  await ctx.cdp.evaluate(enPagina.panelPulsar, "transferir");
  p = await esperarCajonCerrado(ctx);
  if (!p.inerte) fallos.push("al cerrar no vuelve a quedar inerte");
  if (!(p.foco ?? "").includes("accion")) fallos.push(`al cerrar el foco quedó en ${JSON.stringify(p.foco)}`);

  // La ✕ también cierra.
  await abrirSeccion(ctx, "gestion");
  await ctx.cdp.evaluate(enPagina.panelCerrar, "boton");
  await esperarCajonCerrado(ctx);

  // Escape cierra el cajón y NO el chat; el segundo Escape sí vuelve al tablero.
  await abrirSeccion(ctx, "gestion");
  await ctx.cdp.evaluate(enPagina.teclaEscape);
  await esperarCajonCerrado(ctx);
  const chat = await ctx.cdp.evaluate(enPagina.estadoChat);
  if (!chat.abierto) fallos.push("Escape cerró el chat junto con el cajón");
  await ctx.cdp.evaluate(enPagina.teclaEscape);
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    return !e.abierto ? true : null;
  }, { timeout: 4000, label: "que el segundo Escape vuelva al tablero" });

  if (fallos.length) throw new Error(fallos.join("; "));
  return "las seis secciones abren con su título y su contador, el foco entra al cajón y vuelve a la píldora; la píldora activa, ✕ y Escape lo cierran sin cerrar el chat";
}

async function criterioCajonResponsivo(ctx) {
  const fallos = [];
  const medidas = [];
  for (const [ancho, alto] of [[1440, 900], [1100, 900], [400, 800]]) {
    await ctx.ancho(ancho, alto);
    await abrirChatDe(ctx, ANA);
    await esperarCajon(ctx);
    const p = await abrirSeccion(ctx, "gestion");
    const scrollX = await ctx.cdp.evaluate(() =>
      document.documentElement.scrollWidth > document.documentElement.clientWidth);
    if (scrollX) fallos.push(`a ${ancho} px hay scroll horizontal con el cajón abierto`);
    if (!p.panelVisible) fallos.push(`a ${ancho} px el cajón no se ve`);
    medidas.push(`${ancho}px → cajón ${p.anchoPanel}px, velo ${p.velo}, empuje ${p.empuje}`);
    if (ancho >= 1280) {
      // Empuja: el hilo y el compositor se encogen y nada queda tapado.
      if (p.empuje === "0px") fallos.push(`a ${ancho} px el cajón se encima en vez de empujar`);
      if (p.velo !== "oculto") fallos.push(`a ${ancho} px el velo está ${p.velo}: el chat debe seguir usable`);
    } else {
      if (p.empuje !== "0px") fallos.push(`a ${ancho} px el chat reserva ${p.empuje}: ahí debe encimarse`);
      if (p.velo !== "visible") fallos.push(`a ${ancho} px el cajón se abre sin velo`);
      // Donde se encima, el velo cierra.
      await ctx.cdp.evaluate(enPagina.panelCerrar, "velo");
      await esperarCajonCerrado(ctx);
    }
    if (ancho === 400 && p.anchoPanel !== p.anchoChat) {
      fallos.push(`a 400 px el cajón mide ${p.anchoPanel} px y el chat ${p.anchoChat}: debe ocupar todo`);
    }
    await ctx.capturar(`09-cajon-${ancho}`);
  }
  await ctx.cdp.send("Emulation.clearDeviceMetricsOverride");
  if (fallos.length) throw new Error(fallos.join("; "));
  return medidas.join(" · ");
}

async function criterioCajonGestion(ctx) {
  await ctx.login("colab1");
  await abrirChatDe(ctx, ANA);
  await esperarCajon(ctx);
  const fallos = [];
  let p = await abrirSeccion(ctx, "gestion");
  const folio = (await ctx.chat(ANA)).ticket;
  if (p.folio !== `Folio #${folio.id}`) fallos.push(`el cajón dice ${JSON.stringify(p.folio)} y el folio es #${folio.id}`);
  if (!p.estadoTile) fallos.push("quien atiende no ve el selector de estado");
  if (p.gAjeno) fallos.push("a quien atiende le sale el aviso de folio ajeno");
  if (!mismoConjunto(p.gestionBotones, ["crm-g-terminar", "crm-g-soltar"])) {
    fallos.push(`botones de gestión visibles: ${JSON.stringify(p.gestionBotones)}`);
  }
  if (!(p.verFolio ?? "").startsWith(`/tickets/${folio.id}?volver=`)) {
    fallos.push(`el enlace a la solicitud completa es ${JSON.stringify(p.verFolio)}`);
  }

  await ctx.capturar("10-cajon-gestion");

  // Cambiar el estado: la API y la píldora activa se enteran.
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r = await ctx.cdp.evaluate(enPagina.panelEstadoPulsar, "en_proceso");
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Estado actualizado", 8000);
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    const api = (await ctx.chat(ANA)).ticket;
    return api.status === "en_proceso" && e.estados.find((x) => x.estado === "en_proceso").actual ? true : null;
  }, { timeout: 8000, label: "que el folio quede «en proceso» en la API y en el cajón" });

  // Etiquetas: agregar y quitar, con el contador del riel al día.
  p = await abrirSeccion(ctx, "etiquetas");
  if (!p.etiquetaForm) fallos.push("no se ofrece agregar etiqueta");
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r2 = await ctx.cdp.evaluate(enPagina.panelFormulario, "#crm-e-form", { nombre: "urgente" });
  if (r2.error) throw new Error(r2.error);
  await ctx.esperarToast("Etiqueta agregada", 8000);
  p = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.etiquetas.includes("urgente") ? e : null;
  }, { timeout: 8000, label: "la etiqueta «urgente» en el cajón" });
  const numEt = p.riel.find((b) => b.seccion === "etiquetas").num;
  if (numEt !== "1") fallos.push(`la píldora de etiquetas dice ${JSON.stringify(numEt)}`);
  const tarjeta = await ctx.contacto(ANA);
  if (!tarjeta.labels.includes("urgente")) fallos.push(`la API del tablero trae ${JSON.stringify(tarjeta.labels)}`);
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r3 = await ctx.cdp.evaluate(enPagina.panelQuitarEtiqueta, "urgente");
  if (r3.error) throw new Error(r3.error);
  await ctx.esperarToast("Etiqueta quitada", 8000);
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.etiquetas.length === 0 ? true : null;
  }, { timeout: 8000, label: "que la etiqueta desaparezca" });

  if (fallos.length) throw new Error(fallos.join("; "));
  return `colab1 ve su folio #${folio.id}, lo pasa a «en proceso» y pone y quita la etiqueta «urgente» sin salir del chat`;
}

async function criterioCajonNotas(ctx) {
  const fallos = [];
  let p = await abrirSeccion(ctx, "notas");
  if (!p.notaForm) fallos.push("quien atiende no puede anotar en el folio");
  if (p.nAjeno) fallos.push("a quien atiende le sale el aviso de nota ajena");

  const nota = "QA: el cliente manda el acta del papá";
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r = await ctx.cdp.evaluate(enPagina.panelFormulario, "#crm-n-form", { note: nota });
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Nota guardada", 8000);
  p = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.notasTramite.includes(nota) ? e : null;
  }, { timeout: 8000, label: "la nota en «Notas del trámite»" });
  if (!p.notasTramite.includes("colab1")) fallos.push(`la nota no dice quién la escribió: ${JSON.stringify(p.notasTramite)}`);

  const notaCliente = "QA: prefiere llamadas después de las 6";
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r2 = await ctx.cdp.evaluate(enPagina.panelFormulario, "#crm-n-cliente-form", { body: notaCliente });
  if (r2.error) throw new Error(r2.error);
  await ctx.esperarToast("Nota guardada en la ficha", 8000);
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.notasCliente.includes(notaCliente) ? true : null;
  }, { timeout: 8000, label: "la nota en la ficha del cliente" });

  // Seguimiento: el recordatorio queda listado y contado.
  p = await abrirSeccion(ctx, "seguimiento");
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r3 = await ctx.cdp.evaluate(enPagina.panelFormulario, "#crm-s-form",
    { days: "3", note: "QA recontactar por documentos" });
  if (r3.error) throw new Error(r3.error);
  await ctx.esperarToast("Recordatorio creado", 8000);
  p = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.seguimientos.some((s) => s.includes("QA recontactar por documentos")) ? e : null;
  }, { timeout: 8000, label: "el recordatorio en «Pendientes»" });
  const numSeg = p.riel.find((b) => b.seccion === "seguimiento").num;
  if (numSeg !== "1") fallos.push(`la píldora de seguimiento dice ${JSON.stringify(numSeg)}`);
  if (!p.seguimientos[0].includes("colab1")) fallos.push(`el pendiente no dice de quién es: ${JSON.stringify(p.seguimientos)}`);

  if (fallos.length) throw new Error(fallos.join("; "));
  return "la nota del folio, la nota del cliente y el recordatorio se guardan desde el cajón y aparecen ahí mismo";
}

async function criterioCajonPermisos(ctx) {
  await ctx.login("jefa1");
  const fallos = [];

  // 1) Conversación sin folio: solo la nota del cliente.
  await abrirChatDe(ctx, BETO, false);
  let p = await esperarCajon(ctx);
  const apagadas = p.riel.filter((b) => b.deshabilitado).map((b) => b.seccion);
  if (!mismoConjunto(apagadas, ["gestion", "etiquetas", "cobro", "seguimiento", "transferir"])) {
    fallos.push(`sin folio quedan activas de más o de menos: apagadas ${JSON.stringify(apagadas)}`);
  }
  p = await abrirSeccion(ctx, "notas");
  if (!p.sinFolio) fallos.push("sin folio no se explica por qué no hay acciones");
  if (p.notaForm) fallos.push("sin folio se ofrece anotar en un folio que no existe");
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r = await ctx.cdp.evaluate(enPagina.panelFormulario, "#crm-n-cliente-form",
    { body: "QA: nota de un cliente sin folio" });
  if (r.error) throw new Error(r.error);
  await ctx.esperarToast("Nota guardada en la ficha", 8000);
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.panelEstado);
    return e.notasCliente.includes("QA: nota de un cliente sin folio") ? true : null;
  }, { timeout: 8000, label: "la nota del cliente sin folio" });

  // 2) Admin que NO atiende: puede terminar y soltar, no tocar estado ni notas.
  await abrirChatDe(ctx, ANA);
  await esperarCajon(ctx);
  p = await abrirSeccion(ctx, "gestion");
  if (p.estadoTile) fallos.push("un admin que no atiende ve el selector de estado");
  if (!p.gAjeno) fallos.push("no se explica quién atiende el folio");
  if (!mismoConjunto(p.gestionBotones, ["crm-g-terminar", "crm-g-soltar"])) {
    fallos.push(`botones del admin ajeno: ${JSON.stringify(p.gestionBotones)}`);
  }
  p = await abrirSeccion(ctx, "notas");
  if (p.notaForm || !p.nAjeno) fallos.push("el admin ajeno puede anotar en el folio de otro");
  p = await abrirSeccion(ctx, "transferir");
  if (p.transferForm || !p.tAjeno) fallos.push("el admin ajeno ve el formulario de transferencia");

  // 3) Cobro: resumen del cobro sembrado y aviso de Stripe apagado.
  p = await abrirSeccion(ctx, "cobro");
  if (p.cobros.length !== 1) throw new Error(`el cajón lista ${p.cobros.length} cobros y hay 1 sembrado`);
  const c = p.cobros[0];
  if (c.badge !== "⏳ Pendiente de pago") fallos.push(`la insignia del cobro es ${JSON.stringify(c.badge)}`);
  if (c.monto !== "$150.00 MXN") fallos.push(`el monto es ${JSON.stringify(c.monto)}`);
  if (!c.pie.includes("Generado por colab1")) fallos.push(`el pie del cobro es ${JSON.stringify(c.pie)}`);
  for (const esperado of ["Copiar link", "Reenviar por WhatsApp", "Cancelar cobro"]) {
    if (!c.acciones.includes(esperado)) fallos.push(`falta la acción «${esperado}»: ${JSON.stringify(c.acciones)}`);
  }
  await ctx.capturar("11-cajon-cobro");
  if (p.cobroForm) fallos.push("se ofrece generar un cobro con Stripe apagado");
  if (!p.sinStripe) fallos.push("no se dice que Stripe no está configurado");

  if (fallos.length) throw new Error(fallos.join("; "));
  return "sin folio solo queda la nota del cliente; un admin que no atiende puede terminar y soltar pero no tocar estado, notas ni transferencia; el cobro sembrado se resume con sus acciones";
}

async function criterioCajonTransferir(ctx) {
  await ctx.login("colab1");
  await abrirChatDe(ctx, ANA);
  await esperarCajon(ctx);
  const fallos = [];
  const p = await abrirSeccion(ctx, "transferir");
  if (!p.transferForm) fallos.push("quien atiende no ve el formulario de transferencia");
  if (!p.companeros.includes("jefa1")) fallos.push(`compañeros de la oficina del folio: ${JSON.stringify(p.companeros)}`);

  // Transferir la propia solicitud quita el acceso: el panel contesta 404 y eso
  // ES el éxito. El 404 lo provoca esta prueba, así que no cuenta como fallo.
  const marca = ctx.recolector.fallos.length;
  const desde = ctx.dialogos.length;
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r = await ctx.cdp.evaluate(enPagina.panelFormulario, "#crm-t-form",
    { to_user: "jefa1", note: "QA: se la paso con contexto" });
  if (r.error) throw new Error(r.error);
  await ctx.esperarDialogo({ desde, prefijo: "¿Transferir esta solicitud a jefa1?" });
  await ctx.esperarToast("Solicitud transferida a jefa1", 10000);
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    const t = await ctx.tablero();
    return !e.abierto && !t.contactos.some((c) => c.phone === ANA) ? true : null;
  }, { timeout: 10000, label: "que la conversación transferida salga del tablero de colab1" });
  const extra = ctx.recolector.fallos.splice(marca)
    .filter((f) => !(f.texto.includes("/crm/api/panel/") && /\b404\b/.test(f.texto)));
  ctx.recolector.fallos.push(...extra);

  // El dueño nuevo la ve, con la nota de contexto en el folio.
  await ctx.login("jefa1");
  await abrirChatDe(ctx, ANA);
  await esperarCajon(ctx);
  const q = await abrirSeccion(ctx, "notas");
  if (!q.notasTramite.includes("QA: se la paso con contexto")) {
    fallos.push(`la nota de la transferencia no quedó en el folio: ${JSON.stringify(q.notasTramite)}`);
  }
  const panel = await ctx.api(`/crm/api/panel/${ANA}`);
  if (panel.ticket.assigned_to !== "jefa1") fallos.push(`el folio quedó en ${JSON.stringify(panel.ticket.assigned_to)}`);
  if (!panel.permisos.atiende) fallos.push("el dueño nuevo no aparece como quien atiende");

  if (fallos.length) throw new Error(fallos.join("; "));
  return "colab1 transfiere su solicitud a jefa1 con nota de contexto, pierde el acceso y el CRM lo devuelve al tablero; jefa1 la recibe con la nota";
}

async function criterioFoliosCerrados(ctx) {
  // Tras el criterio 25 el folio de Ana es de jefa1 y sigue abierto (tiene un cobro vivo).
  await ctx.login("jefa1");
  await abrirChatDe(ctx, ANA);
  await esperarCajon(ctx);
  const fallos = [];

  // Terminar la solicitud desde el cajón la saca del panel y devuelve al tablero.
  await abrirSeccion(ctx, "gestion");
  const desde = ctx.dialogos.length;
  await ctx.cdp.evaluate(enPagina.ocultarToast);
  const r = await ctx.cdp.evaluate(enPagina.panelBoton, "crm-g-terminar");
  if (r.error) throw new Error(r.error);
  await ctx.esperarDialogo({ desde, prefijo: "OJO: este cliente tiene un cobro SIN LIQUIDAR" });
  await ctx.esperarToast("Solicitud terminada", 10000);
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoChat);
    const t = await ctx.cdp.evaluate(enPagina.estadoTab, "all");
    return !e.abierto && t.items.length > 0 && !t.items.includes(ANA) ? true : null;
  }, { timeout: 10000, label: "que el chat se cierre y Ana salga de la bandeja" });
  const api = await ctx.tablero();
  if (api.contactos.some((c) => c.phone === ANA)) fallos.push("la API del tablero todavía trae a Ana");
  if (api.contactos.some((c) => c.phone === DIEGO)) fallos.push("la API del tablero trae a Diego, con su folio cerrado");

  // «Folios cerrados»: ahí viven Ana y Diego, con la insignia de su folio.
  if (!(await ctx.cdp.evaluate(enPagina.pulsarTab, CERRADAS))) throw new Error("no hay pestaña «Folios cerrados»");
  try {
    const tab = await waitFor(async () => {
      const e = await ctx.cdp.evaluate(enPagina.estadoTab, CERRADAS);
      return mismoConjunto(e.items, [ANA, DIEGO]) ? e : null;
    }, { timeout: 8000, label: "a Ana y Diego en «Folios cerrados»" });
    if (!tab.clase || !tab.aria) fallos.push("la pestaña «Folios cerrados» no quedó activa");
    const tarjetas = await ctx.cdp.evaluate(enPagina.tarjetas);
    const diego = tarjetas.find((x) => x.id === DIEGO);
    if (!diego) {
      fallos.push("Diego no está en el tablero de «Folios cerrados»");
    } else {
      if (!diego.badges.includes("caducó")) fallos.push(`Diego sin «caducó»: ${JSON.stringify(diego.badges)}`);
      if (!diego.badges.includes("cerrado · jefa1")) fallos.push(`Diego sin «cerrado · jefa1»: ${JSON.stringify(diego.badges)}`);
      if (diego.ticks !== "✓✓ " || diego.ticksRead) fallos.push(`palomitas de Diego (delivered): ${JSON.stringify([diego.ticks, diego.ticksRead])}`);
    }
    // Ocultar no es prohibir: una conversación cerrada se sigue abriendo desde aquí.
    const abrir = await ctx.cdp.evaluate(enPagina.abrirTarjeta, ANA, "bandeja");
    if (abrir.error) throw new Error(abrir.error);
    await waitFor(async () => {
      const e = await ctx.cdp.evaluate(enPagina.estadoChat);
      return e.abierto && e.nombre === "Ana Lucía Ramírez" && e.burbujas.length > 0 ? true : null;
    }, { timeout: 8000, label: "el chat de Ana desde «Folios cerrados»" });
    await ctx.capturar("12-folios-cerrados");
    await ctx.cdp.evaluate(enPagina.cerrarChat, "x");
  } finally {
    await ctx.cdp.evaluate(enPagina.pulsarTab, "all");
  }

  // De vuelta en «Todos», ninguna de las dos.
  await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.estadoTab, "all");
    return e.items.length > 0 && !e.items.includes(ANA) && !e.items.includes(DIEGO) ? true : null;
  }, { timeout: 8000, label: "que «Todos» vuelva a la cola abierta" });

  if (fallos.length) throw new Error(fallos.join("; "));
  return "terminar la solicitud desde el cajón cierra el chat y la saca del panel; «Folios cerrados» trae a Ana y a Diego con sus insignias y se abren desde ahí; «Todos» vuelve a la cola abierta";
}

// ---- Mis respuestas (antes /replies) -----------------------------------------

// Una tecla de verdad por CDP: un KeyboardEvent sintético no cierra un <dialog> modal.
async function teclaReal(ctx, key, codigo) {
  const base = { key, code: key, windowsVirtualKeyCode: codigo, nativeVirtualKeyCode: codigo };
  await ctx.cdp.send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base });
  await ctx.cdp.send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
}

// Si la espera se agota, el error dice qué estaba viendo: sin eso un tiempo agotado no explica nada.
async function esperarRespuestas(ctx, condicion, label) {
  let visto = null;
  try {
    return await waitFor(async () => {
      visto = await ctx.cdp.evaluate(enPagina.respuestasEstado);
      return condicion(visto) ? visto : null;
    }, { timeout: 8000, label });
  } catch (e) {
    const { abierto, cargando, items, error, estado, foco, url, chatAbierto, qr } = visto ?? {};
    throw new Error(`${e.message} — visto: ${JSON.stringify({ abierto, cargando, items: items?.length, error, estado, foco, url, chatAbierto, qr: qr?.length })}`);
  }
}

async function criterioMisRespuestas(ctx) {
  const fallos = [];
  let resumen;
  try {
    resumen = await recorridoMisRespuestas(ctx, fallos);
  } catch (e) {
    // Lo que ya había fallado antes de la excepción también se reporta.
    throw new Error(fallos.length ? `${e.message}; antes: ${fallos.join("; ")}` : e.message);
  }
  if (fallos.length) throw new Error(fallos.join("; "));
  return resumen;
}

async function recorridoMisRespuestas(ctx, fallos) {
  const TITULO = "Pedir CURP QA";
  const BORRADOR = "Para continuar necesito tu CURP y una identificación ✓";
  const MAX = 30;
  // Tras el criterio 26 el folio de Ana está cerrado, pero su chat abre por enlace y su ventana sigue viva.
  await ctx.cdp.send("Emulation.clearDeviceMetricsOverride");
  await ctx.login("jefa1");
  await abrirChatDe(ctx, ANA);
  // Con el cajón del folio abierto detrás: su Escape (en captura) no debe robarle la tecla al diálogo.
  await esperarCajon(ctx);
  await abrirSeccion(ctx, "notas");
  let s = await ctx.cdp.evaluate(enPagina.respuestasEstado);
  if (s.menuReplies) fallos.push("la página sigue enlazando a /replies");
  if (!s.chipAlFinal) fallos.push("«⚡ Mis respuestas» no es el último botón de la fila de respuestas rápidas");
  const nQr = s.qr.length;
  if (!nQr) throw new Error("la fila de respuestas rápidas está vacía: el compositor no se pintó");

  // Desde el chat: lo escrito en el compositor llega como texto de la respuesta.
  await ctx.cdp.evaluate(enPagina.escribirBorrador, BORRADOR);
  let r = await ctx.cdp.evaluate(enPagina.activarBoton, "crm-btn-respuestas-chat");
  if (r.error) throw new Error(r.error);
  s = await esperarRespuestas(ctx, (e) => e.abierto && !e.cargando, "«Mis respuestas» abierto y cargado");
  if (!s.modal) fallos.push("el diálogo no se abrió como modal");
  if (s.texto !== BORRADOR) fallos.push(`el texto no trae el borrador del compositor: ${JSON.stringify(s.texto)}`);
  if (s.foco !== "crm-resp-titulo") fallos.push(`al abrir, el foco quedó en ${s.foco} y no en el título`);
  if (!s.vacio || s.items.length || s.cuenta !== `(0 de ${MAX})`) fallos.push(`lista inicial: ${JSON.stringify({ vacio: s.vacio, items: s.items.length, cuenta: s.cuenta })}`);
  await ctx.capturar("09-mis-respuestas-1400");

  // Guardar: la lista y la fila del compositor se repintan sin recargar.
  r = await ctx.cdp.evaluate(enPagina.guardarRespuesta, TITULO);
  if (r.error) throw new Error(r.error);
  s = await esperarRespuestas(ctx, (e) => e.items.length === 1 && e.qr.length === nQr + 1, "la respuesta en la lista y en la fila del compositor");
  const item = s.items[0];
  if (item.titulo !== TITULO || item.texto !== BORRADOR) fallos.push(`ítem guardado: ${JSON.stringify(item)}`);
  if (item.etiqueta !== `Eliminar «${TITULO}»`) fallos.push(`aria-label de «Eliminar»: ${JSON.stringify(item.etiqueta)}`);
  if (s.qr[s.qr.length - 1] !== `★ ${TITULO}`) fallos.push(`la fila del compositor termina en ${JSON.stringify(s.qr.slice(-1))}`);
  if (!s.chipAlFinal) fallos.push("tras repintar la fila, «⚡ Mis respuestas» dejó de ser el último botón");
  if (s.estado !== `Respuesta «${TITULO}» guardada.`) fallos.push(`aviso de guardado: ${JSON.stringify(s.estado)}`);
  if (s.cuenta !== `(1 de ${MAX})` || s.vacio || s.titulo !== "" || s.texto !== "") fallos.push(`tras guardar: ${JSON.stringify({ cuenta: s.cuenta, vacio: s.vacio, titulo: s.titulo, texto: s.texto })}`);
  if (s.foco !== "crm-resp-titulo") fallos.push(`tras guardar, el foco quedó en ${s.foco}`);
  const api = await ctx.api("/crm/api/respuestas");
  if (api.mias.length !== 1 || api.mias[0].title !== TITULO) fallos.push(`la API tras guardar: ${JSON.stringify(api.mias)}`);
  await ctx.capturar("09b-mis-respuestas-guardada-1400");

  // Escape cierra solo el diálogo: el chat y el cajón siguen abiertos y el foco vuelve a quien lo abrió.
  // Con el foco en un BOTÓN del diálogo: en un campo de texto el chat ya ignora Escape por sí solo
  // (se está escribiendo) y el guardia del diálogo no se pondría a prueba.
  r = await ctx.cdp.evaluate(enPagina.enfocar, "#crm-resp-lista .resp-borrar");
  if (r.error || !r.ok) throw new Error(r.error ?? "no se pudo enfocar «Eliminar»");
  await teclaReal(ctx, "Escape", 27);
  s = await esperarRespuestas(ctx, (e) => !e.abierto, "que Escape cierre «Mis respuestas»");
  if (!s.chatAbierto) fallos.push("Escape en el diálogo cerró también el chat");
  if (!(await ctx.cdp.evaluate(enPagina.panelEstado)).abierto) fallos.push("Escape en el diálogo cerró también el cajón del folio");
  if (s.foco !== "crm-btn-respuestas-chat") fallos.push(`al cerrar, el foco quedó en ${s.foco} y no en el botón que lo abrió`);
  // La respuesta nueva se usa como cualquier otra.
  const qr = await ctx.cdp.evaluate(enPagina.pulsarQrTitulo, `★ ${TITULO}`);
  if (qr.error || qr.texto !== BORRADOR) fallos.push(`usar la respuesta guardada: ${JSON.stringify(qr)}`);

  // Desde la barra superior: sin borrador, y se elimina con confirm.
  r = await ctx.cdp.evaluate(enPagina.activarBoton, "crm-btn-respuestas");
  if (r.error) throw new Error(r.error);
  s = await esperarRespuestas(ctx, (e) => e.abierto && !e.cargando && e.items.length === 1, "el diálogo abierto desde la barra superior");
  if (s.texto !== "") fallos.push("abierto desde la barra superior, el texto no debía traer el borrador");
  const desde = ctx.dialogos.length;
  r = await ctx.cdp.evaluate(enPagina.eliminarRespuesta, TITULO);
  if (r.error) throw new Error(r.error);
  await ctx.esperarDialogo({ desde, prefijo: `¿Eliminar «${TITULO}»?` });
  s = await esperarRespuestas(ctx, (e) => e.items.length === 0 && e.qr.length === nQr, "que la respuesta salga de la lista y de la fila");
  if (!s.vacio || s.cuenta !== `(0 de ${MAX})`) fallos.push(`tras eliminar: ${JSON.stringify({ vacio: s.vacio, cuenta: s.cuenta })}`);
  if (s.estado !== `Respuesta «${TITULO}» eliminada.`) fallos.push(`aviso de baja: ${JSON.stringify(s.estado)}`);
  if (s.foco !== "crm-resp-titulo") fallos.push(`tras eliminar la única, el foco quedó en ${s.foco} y no en el título`);
  if ((await ctx.api("/crm/api/respuestas")).mias.length) fallos.push("la API sigue listando la respuesta eliminada");
  r = await ctx.cdp.evaluate(enPagina.activarBoton, "crm-resp-cerrar");
  if (r.error) throw new Error(r.error);
  s = await esperarRespuestas(ctx, (e) => !e.abierto, "que ✕ cierre el diálogo");
  if (s.foco !== "crm-btn-respuestas") fallos.push(`al cerrar con ✕, el foco quedó en ${s.foco}`);

  // La ruta vieja aterriza con el diálogo abierto; al cerrarlo la URL queda limpia.
  await navegar(ctx.cdp, ctx.base + "/replies");
  s = await esperarRespuestas(ctx, (e) => e.abierto && !e.cargando, "/replies con «Mis respuestas» abierto");
  if (s.url !== "/crm?respuestas=1") fallos.push(`/replies aterrizó en ${s.url}`);
  await teclaReal(ctx, "Escape", 27);
  s = await esperarRespuestas(ctx, (e) => !e.abierto, "cerrar el diálogo que abrió la ruta vieja");
  // ?respuestas=1 se quita en el evento close, que el navegador encola en una tarea aparte
  // DESPUÉS de poner open=false: leer la URL en la misma foto que vio el diálogo cerrado era una
  // carrera del juez (salía en rojo una de cada dos con tarjetas timbrando detrás). Se espera a
  // que se limpie; si nunca se limpia, sigue fallando igual.
  s = await waitFor(async () => {
    const e = await ctx.cdp.evaluate(enPagina.respuestasEstado);
    return !e.abierto && e.url === "/crm" ? e : null;
  }, { timeout: 2000, interval: 50, label: "que el cierre quite ?respuestas=1" }).catch(() => s);
  if (s.url !== "/crm") fallos.push(`al cerrar, la URL quedó en ${s.url} (recargar lo reabriría)`);

  // Teléfono: el diálogo cabe sin scroll horizontal.
  await ctx.ancho(400, 800);
  await ctx.abrirCrm("?respuestas=1");
  s = await esperarRespuestas(ctx, (e) => e.abierto && !e.cargando, "el diálogo a 400 px");
  if (s.scrollX || s.anchoDialogo > s.anchoVentana) fallos.push(`a 400 px: ${JSON.stringify({ scrollX: s.scrollX, dialogo: s.anchoDialogo, ventana: s.anchoVentana })}`);
  await ctx.capturar("10-mis-respuestas-400");
  await ctx.cdp.send("Emulation.clearDeviceMetricsOverride");

  // Un colaborador sin conversaciones también llega desde la barra superior y guarda la suya.
  await ctx.login("colab2");
  await ctx.abrirCrm();
  r = await ctx.cdp.evaluate(enPagina.activarBoton, "crm-btn-respuestas");
  if (r.error) throw new Error(r.error);
  await esperarRespuestas(ctx, (e) => e.abierto && !e.cargando, "«Mis respuestas» de colab2");
  r = await ctx.cdp.evaluate(enPagina.guardarRespuesta, "Saludo de colab2", "Hola, soy tu asesora.");
  if (r.error) throw new Error(r.error);
  await esperarRespuestas(ctx, (e) => e.items.length === 1, "la respuesta de colab2 en su lista");
  const suyas = await ctx.api("/crm/api/respuestas");
  if (suyas.mias.length !== 1 || suyas.mias[0].title !== "Saludo de colab2") fallos.push(`la API de colab2: ${JSON.stringify(suyas.mias)}`);

  return "guardada desde el chat con el borrador del compositor, aparece en la lista y en la fila sin recargar, se usa, Escape y ✕ cierran sin tocar el chat ni el cajón y devuelven el foco, se elimina con confirm; /replies abre el diálogo y limpia la URL al cerrar; cabe a 400 px; colab2 sin chats guarda la suya desde la barra";
}

function criterioConsola(recolector) {
  const titulo = "Consola y red limpias durante todo el recorrido";
  if (recolector.fallos.length) {
    resultados.push({ titulo, ok: false });
    imprimir("❌", titulo, recolector.fallos.map((f) => `[${f.origen}] ${f.texto}`).join(" | "));
  } else {
    resultados.push({ titulo, ok: true });
    imprimir("✅", titulo, "sin excepciones, sin console.error, sin respuestas ≥ 400 inesperadas", recolector.avisos.map((a) => `[${a.origen}] ${a.texto}`));
  }
}

// ────────────────────────────────────────────────────────────────────────────
// main
// ────────────────────────────────────────────────────────────────────────────

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "crm-qa-"));
  const db = path.join(dir, "crm-qa.db");
  const env = {
    ...process.env,
    DB_PATH: db, SECURE_COOKIES: "0", SESSION_SECRET: "qa-secreto-de-prueba-no-usar",
    MSSQL_HOST: "", MSSQL_SA_PASSWORD: "", OLLAMA_URL: "", ANTHROPIC_API_KEY: "", WHATSAPP_TOKEN: "",
    STRIPE_SECRET_KEY: "",
    GEOIP_ENABLED: "0", DASHBOARD_USERNAME: "", DASHBOARD_PASSWORD: "",
    CRM_QA_REPO: REPO, CRM_QA_CLAVE: CLAVE, PYTHONUNBUFFERED: "1",
  };
  console.log(`Base temporal: ${db}`);
  const siembra = spawnSync(PYTHON, [SEED], { cwd: REPO, env, encoding: "utf8" });
  if (siembra.status !== 0) {
    console.error("❌ La siembra falló:\n" + siembra.stdout + siembra.stderr);
    process.exit(1);
  }
  console.log("Siembra: " + siembra.stdout.trim().split("\n").pop());

  const puerto = await puertoLibre();
  let servidor = null;
  let chrome = null;
  let cdp = null;
  let codigo = 1;
  try {
    servidor = await lanzarServidor(env, puerto);
    console.log(`uvicorn en ${servidor.base}`);
    chrome = await lanzarChrome(path.join(dir, "perfil-chrome"));
    cdp = new ClienteCDP(chrome.wsUrl);
    await cdp.conectar();
    await cdp.send("Page.enable");
    await cdp.send("Runtime.enable");
    await cdp.send("Log.enable");
    await cdp.send("Network.enable");
    const dialogos = [];
    cdp.on("Page.javascriptDialogOpening", (p) => {
      dialogos.push({ tipo: p.type, mensaje: p.message, consumido: false });
      cdp.send("Page.handleJavaScriptDialog", { accept: true }).catch(() => {});
    });
    const recolector = new Recolector(cdp);
    const ctx = crearContexto({ cdp, base: servidor.base, recolector, dialogos, env, db });

    await ctx.login("jefa1");
    await criterio(ctx, "1. Carga del tablero (admin global)", criterioCarga);
    await criterio(ctx, "2. Tarjetas, contadores e insignias contra la API", criterioTarjetas);
    await criterio(ctx, "3. Pestañas-filtro y columna enfocada", criterioPestanas);
    await criterio(ctx, "3b. Miembros de la oficina: tira, estado, conteos y filtro", criterioMiembros);
    await criterio(ctx, "4. Búsqueda sin acentos y por teléfono con formato", criterioBusqueda);
    await criterio(ctx, "5. Drag & drop persistente tras recargar", criterioArrastre);
    await criterio(ctx, "6. La tarjeta trae el ícono de WhatsApp y no «Mover a…»: timbra con pendientes y se calma", criterioIconoWhatsApp);
    await criterio(ctx, "7. Abrir el chat de un contacto", criterioAbrirChat);
    await criterio(ctx, "7b. Un mensaje entrante aparece en el chat y en la tarjeta sin recargar", criterioEntranteEnVivo);
    await criterio(ctx, "7c. El stream avisa: un mensaje de otro panel entra sin sondeo", criterioAvisoEnVivo);
    await criterio(ctx, "8. Ventana cerrada: variantes «nunca» y «caducó»", criterioVentanaCerrada);
    await criterio(ctx, "9. Enviar un mensaje y ocultarlo", criterioEnviar);
    await criterio(ctx, "9b. Varios trámites en curso: cabecera, selector «Sobre el trámite» y burbuja con folio", criterioVariosTramites);
    await criterio(ctx, "9c. La burbuja del cliente ligada a su folio lleva «#N · servicio»", criterioBurbujaLigada);
    await criterio(ctx, "10. Reactivar el bot", criterioReactivarBot);
    await criterio(ctx, "11. Bloquear y desbloquear (admin)", criterioBloquear);
    await criterio(ctx, "12. Cerrar el chat: ✕, «Tablero» y Escape", criterioCerrarChat);
    await criterio(ctx, "13. Etapas: crear, mover, renombrar, color, eliminar", criterioEtapas);
    await criterio(ctx, "13b. Reordenar etapas: arrastrar la cabecera y «Mover antes» con el teclado", criterioReordenarEtapas);
    await criterio(ctx, "14. Alta de contacto desde una columna", criterioAltaContacto);
    await criterio(ctx, "15. /crm?chat= abre el chat al cargar", criterioChatInicial);
    await criterio(ctx, "15b. La ruta vieja /inbox/{phone} aterriza en el CRM", criterioRutaVieja);
    await criterio(ctx, "16. Ocultar/mostrar bandeja", criterioToggleTray);
    await criterio(ctx, "17. Colaborador: alcance y sin controles de admin", criterioColaborador);
    await criterio(ctx, "17b. Tablero personal: lo que colab1 hace con sus etapas no toca el de jefa1", criterioTableroPersonal);
    await criterio(ctx, "17c. «Estrategia» (colaborador): casillas de SU tablero, una sola marcada, mover sin salir del chat", criterioEstrategiaColaborador);
    await criterio(ctx, "17d. «Estrategia» (admin): contadores sin filtros, sin folio, chat fuera del tablero, círculo oliva y 400 px", criterioEstrategiaAdmin);
    await criterio(ctx, "18. Admin de oficina: solo su oficina", criterioAdminOficina);
    await criterio(ctx, "19. Pantallas de 900 y 400 px", criterioResponsivo);
    await criterio(ctx, "20. Cajón del folio: abrir cada sección, cambiar y cerrar", criterioCajonAbrirCerrar);
    await criterio(ctx, "21. El cajón a 1440, 1100 y 400 px", criterioCajonResponsivo);
    await criterio(ctx, "22. Gestión y etiquetas desde el cajón (quien atiende)", criterioCajonGestion);
    await criterio(ctx, "23. Notas del folio, del cliente y recordatorio", criterioCajonNotas);
    await criterio(ctx, "24. Sin folio, admin que no atiende y resumen del cobro", criterioCajonPermisos);
    await criterio(ctx, "25. Transferir la solicitud desde el cajón", criterioCajonTransferir);
    await criterio(ctx, "26. Folios cerrados: salen del panel y viven en su pestaña", criterioFoliosCerrados);
    await criterio(ctx, "27. Mis respuestas: guardar desde el chat, usar, eliminar y la ruta vieja /replies", criterioMisRespuestas);
    await criterio(ctx, "28. Llega un mensaje: timbra solo esa tarjeta, y con «reducir movimiento» nada se mueve", criterioTimbreEnVivo);
    criterioConsola(recolector);

    const ok = resultados.filter((r) => r.ok).length;
    console.log(`\n${ok}/${resultados.length} criterios en verde`);
    codigo = ok === resultados.length ? 0 : 1;
  } catch (e) {
    console.error("❌ El QA se interrumpió: " + (e.stack || e.message));
    if (servidor) console.error(servidor.salida.slice(-30).join("\n"));
  } finally {
    cdp?.cerrar();
    await matarProceso(chrome?.proc);
    await matarProceso(servidor?.proc);
    if (verbose && servidor) console.log("--- uvicorn ---\n" + servidor.salida.join("\n"));
    if (keep) console.log(`Se conserva ${dir}`);
    else fs.rmSync(dir, { recursive: true, force: true });
  }
  process.exit(codigo);
}

main();
