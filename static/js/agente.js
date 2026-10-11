/* Lienzo del Estudio extraído de templates/agente.html (antes en línea; la CSP ya no lo permite). */
"use strict";
// Los datos llegan como JSON inerte desde agente.html (#agente-datos).
const DATOS = JSON.parse(document.getElementById("agente-datos").textContent);
const ESTADO = DATOS.estado;
// Propuestas de sinónimo pendientes: ya las cuenta el middleware del nav para
// cada request (app.main._nav_context) — aquí solo se leen, sin otra consulta.
const PENDIENTES = DATOS.pendientes;
const post = (url, body) => fetch(url, {method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(body),credentials:"same-origin"})
  .then(async r => { const d = await r.json(); if (!r.ok) throw new Error(d.error || r.status); return d; });

const { dia, shapes } = joint;
// textContent→innerHTML escapa < > &, pero NO las comillas, y varias de estas
// interpolaciones caen DENTRO de un atributo (value="…", data-tool="…"): sin
// escapar la comilla, un valor con " cierra el atributo y lo que siga se
// interpreta como marcado. Es panel de admin, pero el descuido no se hereda.
const esc = t => { const d = document.createElement("div"); d.textContent = t ?? "";
  return d.innerHTML.replace(/"/g, "&quot;").replace(/'/g, "&#39;"); };
const graph = new dia.Graph({}, { cellNamespace: shapes });
const paper = new dia.Paper({ el: document.getElementById("lienzo"), model: graph,
  width: "100%", height: Math.max(640, Math.round(window.innerHeight * .78)), cellViewNamespace: shapes, background: { color: "transparent" },
  interactive: { linkMove: false } });
const MINI_W = 194, MINI_H = 92;
const mini = new dia.Paper({ el: document.getElementById("minimapa-paper"), model: graph,
  width: MINI_W, height: MINI_H, cellViewNamespace: shapes, interactive: false,
  background: { color: "transparent" } });

const POS_KEY = "agente-pos-v2";
const pos = JSON.parse(localStorage.getItem(POS_KEY) || "{}");
// Sin el nodo Claude: la cascada se recorre y cierra el hueco que dejó.
const BASE = { literal:[70,40], ollama:[70,155], fuzzy:[70,270], rag:[70,385],
  tools:[70,500], escalada:[380,500], corpus:[380,325], reparto:[380,155] };

// Tokens del demo ai-workflow-builder (tema oscuro), convertidos de oklch a
// hex: card / card-foreground / border / muted-foreground. El header del demo
// NO tiene fondo propio — el nodo es UNA sola superficie sobre bg-card; solo
// los nodos de tipo "tool" se tiñen con bg-muted/30 y llevan borde punteado.
const T = { card: "#1e2123", texto: "#f0f2f4", borde: "#2b2e32", tenue: "#b3b8be",
            cardTool: "#202325", bordeTool: "rgba(240,242,244,.45)",
            // Ámbar del ícono warning.svg del demo, con contraste suficiente
            // sobre la superficie oscura de la tarjeta.
            aviso: "#f0b429" };
const ICONO = { literal:"🔤", ollama:"🤖", fuzzy:"≈", rag:"📚", tools:"🔧", escalada:"🙋", reparto:"🎯" };

function nodoRico(n) {
  // Chrome del demo: rounded-lg (--radius .5rem = 8px), borde de 1px,
  // node-elevation por filtro SVG y animate-node-in por CSS.
  const esTool = n.id === "tools";  // el único de tipo herramienta: borde punteado
  const puerto = activo => ({ r: 4.5, fill: activo ? "#2ea043" : "#484f58",
    stroke: T.card, strokeWidth: 1.5, magnet: false });
  const conAviso = (n.avisos || []).length > 0;
  // Un nodo sin coordenada cae en el rincón, pero NO tumba el lienzo: sin este
  // respaldo el TypeError abortaba el script entero —nodos, flechas, panel y
  // los interruptores de las otras capas— y /agente quedaba en blanco.
  const base = BASE[n.id] || [380, 40];
  const el = new shapes.standard.Rectangle({ id: n.id,
    position: pos[n.id] ? pos[n.id] : { x: base[0], y: base[1] },
    size: { width: 256, height: 86 },
    markup: [
      { tagName: "rect", selector: "body" },
      { tagName: "text", selector: "titulo" },
      { tagName: "text", selector: "descripcion" },
      { tagName: "text", selector: "metricas" },
      // Aviso "encendido pero no puede trabajar" (idea del demo ai-agent-builder:
      // WarningEffect marca el nodo sin configurar). QUÉ cuenta como aviso lo
      // decide el servidor (agente_studio._avisos); aquí solo se pinta.
      { tagName: "text", selector: "aviso" },
    ],
    ports: { groups: {
      entrada: { position: "top", attrs: { portBody: puerto(n.activo) },
        markup: [{ tagName: "circle", selector: "portBody" }] },
      salida: { position: "bottom", attrs: { portBody: puerto(n.activo) },
        markup: [{ tagName: "circle", selector: "portBody" }] },
    }, items: [{ group: "entrada" }, { group: "salida" }] },
    attrs: {
      body: { fill: esTool ? T.cardTool : T.card, rx: 8, ry: 8,
        // El aviso engorda y tiñe el borde ADEMÁS del ícono: el color nunca
        // es el único indicador (WCAG 1.4.1) y a 100% de zoom el ícono solo
        // se pierde entre seis nodos.
        strokeWidth: conAviso ? 2 : 1,
        stroke: conAviso ? T.aviso : (esTool ? T.bordeTool : (n.activo ? "#2ea043" : T.borde)),
        strokeDasharray: esTool ? "6,4" : "none",
        filter: "url(#node-elevation)" },
      titulo: { text: `${ICONO[n.id] || "•"} ${n.activo ? "●" : "○"} ${n.nombre}`,
        fill: T.texto, fontSize: 13, fontWeight: 600,
        x: 14, y: 22, textAnchor: "start", textVerticalAnchor: "middle" },
      descripcion: { text: n.detalle || "", fill: T.tenue, fontSize: 11,
        x: 14, y: 44, textAnchor: "start", textVerticalAnchor: "middle",
        textWrap: { width: -28, maxLineCount: 1, ellipsis: "…" } },
      metricas: { text: `${n.mensajes} msgs · 14d${n.descartes ? `  ·  ${n.descartes} descartes` : ""}`,
        fill: T.tenue, fontSize: 10.5,
        x: 14, y: 68, textAnchor: "start", textVerticalAnchor: "middle" },
      aviso: { text: conAviso ? "⚠" : "", fill: T.aviso, fontSize: 15,
        x: 242, y: 22, textAnchor: "end", textVerticalAnchor: "middle" },
    },
  });
  el.prop("datos", n);
  return el;
}

const porId = {};
ESTADO.nodos.forEach(n => { porId[n.id] = nodoRico(n); graph.addCell(porId[n.id]); });

// height: max-content del demo. En SVG el alto es un atributo, no CSS: se mide
// el contenido ya renderizado (bbox local de cada <text>, antes del translate
// del nodo) y se ajusta la caja. Así el nodo crece si mañana el cuerpo lleva
// otra línea, en vez de arrastrar un alto mágico. El ancho sí queda fijo en
// 256 px, igual que en el demo (config.width).
const ALTO_MIN = 64, RELLENO_INFERIOR = 14;

function ajustarAltoAContenido(elementos) {
  elementos.forEach(el => {
    const vista = paper.findViewByModel(el);
    if (!vista) return;
    let fondo = 0;
    vista.el.querySelectorAll("text").forEach(t => {
      const b = t.getBBox();
      if (b.height) fondo = Math.max(fondo, b.y + b.height);
    });
    if (fondo) el.resize(256, Math.max(ALTO_MIN, Math.ceil(fondo + RELLENO_INFERIOR)));
  });
}

// Cada flecha lleva escrita la condición REAL de caída (nodo.entra_si, que
// arma el servidor). Es lo que el demo hace con `to: [{prompt, id}]`: la regla
// de ruteo se lee en la arista. Sin esto el lienzo eran seis cajas en fila y
// para saber por qué se pasa de una a otra había que abrir nlu.py.
ESTADO.nodos.slice(0, -1).forEach((n, i) => {
  const destino = ESTADO.nodos[i + 1];
  graph.addCell(new shapes.standard.Link({
    source: { id: n.id }, target: { id: destino.id },
    connector: { name: "smooth" },
    labels: destino.entra_si ? [{
      attrs: { text: { text: destino.entra_si, fill: T.tenue, fontSize: 9.5 },
               rect: { fill: "#0d1117" } }, position: 0.5 },
    ] : [],
    attrs: { line: { stroke: T.borde, strokeWidth: 1.6,
      targetMarker: { type: "path", d: "M 7 -3.5 0 0 7 3.5 z", fill: T.tenue } } },
  }));
});

const corpus = new shapes.standard.Rectangle({ id: "corpus",
  position: pos["corpus"] || { x: BASE.corpus[0], y: BASE.corpus[1] }, size: { width: 200, height: 58 },
  ports: { groups: { salida: { position: "right", attrs: { portBody: { r: 4.5, fill: "#2ea043", stroke: T.card, strokeWidth: 1.5 } }, markup: [{ tagName: "circle", selector: "portBody" }] } }, items: [{ group: "salida" }] },
  // Recurso adjuntable = mismo lenguaje visual que un nodo "tool" del demo:
  // superficie tenue y borde punteado, no una tarjeta sólida más.
  attrs: { body: { fill: T.cardTool, stroke: T.bordeTool, strokeWidth: 1, rx: 8, ry: 8,
    strokeDasharray: "6,4", filter: "url(#node-elevation)" },
    label: { text: `📄 Corpus informativo\n${ESTADO.parrafos.length} párrafos`, fill: T.tenue, fontSize: 11 } } });
graph.addCell(corpus);
graph.addCell(new shapes.standard.Link({ source: { id: "corpus" }, target: { id: "rag" },
  connector: { name: "smooth" },
  labels: [{ attrs: { text: { text: "contexto", fill: "#7ee787", fontSize: 9.5 },
    rect: { fill: "#0d1117" } }, position: 0.45 }],
  attrs: { line: { stroke: "#2ea043", strokeWidth: 1.4, strokeDasharray: "4,3",
    targetMarker: { type: "path", d: "M 7 -3.5 0 0 7 3.5 z", fill: "#2ea043" } } } }));

// Ya están todos los nodos en el lienzo: se miden juntos (el corpus incluido).
ajustarAltoAContenido([...Object.values(porId), corpus]);
// Las métricas del texto cambian cuando termina de cargar la tipografía: se
// vuelve a medir para que el alto no quede calculado con la fuente de respaldo.
if (document.fonts && document.fonts.ready)
  document.fonts.ready.then(() => ajustarAltoAContenido([...Object.values(porId), corpus]));

paper.on("element:pointerup", v => { pos[v.model.id] = v.model.position();
  localStorage.setItem(POS_KEY, JSON.stringify(pos)); });

// ---- Navegador del lienzo: zoom, arrastre, encuadre y minimapa ------------
//
// Portado del navigator-panel del demo ai-workflow-builder: deslizador de zoom
// con sus topes, botones que se deshabilitan al llegar al límite, ajustar a la
// pantalla y un minimapa que además de MOSTRAR dónde estás deja MOVERTE.
//
// Todo pasa por aplicarVista(): antes cada botón escribía por su cuenta y
// "Ajustar" no actualizaba el contador, así que el siguiente zoom multiplicaba
// una escala vieja y el lienzo pegaba un brinco. La escala vigente se lee del
// paper, que es quien la tiene de verdad; ya no hay copia en una variable.
const ZOOM_MIN = 0.3, ZOOM_MAX = 2, ZOOM_PASO = 0.2, MARGEN_FIT = 60, TICKS = 100;

const limitarZoom = z => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, z));
const aTick = z => Math.round((z - ZOOM_MIN) / (ZOOM_MAX - ZOOM_MIN) * TICKS);
const aZoom = t => ZOOM_MIN + (t / TICKS) * (ZOOM_MAX - ZOOM_MIN);

const ctlPct = document.getElementById("mz-pct");
const ctlRango = document.getElementById("mz-rango");
const ctlMenos = document.getElementById("mz-menos");
const ctlMas = document.getElementById("mz-mas");
const cajaMini = document.getElementById("minimapa-caja");
const recuadro = document.getElementById("mini-vista");

// Encuadre del minimapa. Antes era una escala fija (0.11): al mover un nodo
// lejos, el dibujo se salía del recuadro y el minimapa dejaba de representar
// el lienzo. Ahora se calcula del contenido real.
let miniEscala = 1, miniTx = 0, miniTy = 0;

function encuadrarMini() {
  const caja = graph.getBBox();
  if (!caja || !caja.width || !caja.height) return;
  const margen = 8;
  miniEscala = Math.min((MINI_W - margen * 2) / caja.width,
                        (MINI_H - margen * 2) / caja.height);
  miniTx = -caja.x * miniEscala + (MINI_W - caja.width * miniEscala) / 2;
  miniTy = -caja.y * miniEscala + (MINI_H - caja.height * miniEscala) / 2;
  mini.scale(miniEscala);
  mini.translate(miniTx, miniTy);
}

// El recuadro = el área visible del lienzo, llevada a píxeles del minimapa.
function pintarRecuadro() {
  if (cajaMini.hidden) return;
  const s = paper.scale().sx, t = paper.translate();
  const area = paper.el.getBoundingClientRect();
  const x = (-t.tx / s) * miniEscala + miniTx;
  const y = (-t.ty / s) * miniEscala + miniTy;
  const w = (area.width / s) * miniEscala;
  const h = (area.height / s) * miniEscala;
  // Se recorta a la caja: con el lienzo muy alejado el recuadro cubriría todo
  // y desbordaría el minimapa.
  const x0 = Math.max(0, x), y0 = Math.max(0, y);
  recuadro.style.left = x0 + "px";
  recuadro.style.top = y0 + "px";
  recuadro.style.width = Math.max(8, Math.min(MINI_W - x0, w + Math.min(0, x))) + "px";
  recuadro.style.height = Math.max(8, Math.min(MINI_H - y0, h + Math.min(0, y))) + "px";
}

function aplicarVista() {
  const z = paper.scale().sx;
  ctlPct.textContent = Math.round(z * 100) + "%";
  ctlRango.value = aTick(z);
  // El deslizador trabaja en "ticks" 0–100; un lector de pantalla debe oír el
  // porcentaje, no el tick.
  ctlRango.setAttribute("aria-valuetext", Math.round(z * 100) + "%");
  ctlMenos.disabled = z <= ZOOM_MIN + 0.001;
  ctlMas.disabled = z >= ZOOM_MAX - 0.001;
  pintarRecuadro();
}

// Zoom alrededor del CENTRO de lo que se está viendo (o de un punto dado):
// escalar desde la esquina, como se hacía antes, empuja el contenido fuera de
// la pantalla en cuanto te acercas un poco.
function zoomA(nivel, centro) {
  const antes = paper.scale().sx, z = limitarZoom(nivel);
  if (Math.abs(z - antes) > 1e-6) {
    const area = paper.el.getBoundingClientRect();
    const cx = centro ? centro.x : area.width / 2;
    const cy = centro ? centro.y : area.height / 2;
    const t = paper.translate();
    const px = (cx - t.tx) / antes, py = (cy - t.ty) / antes;
    paper.scale(z);
    paper.translate(cx - px * z, cy - py * z);
  }
  aplicarVista();
}

function ajustar() {
  paper.transformToFitContent({ padding: MARGEN_FIT, maxScale: 1, minScale: ZOOM_MIN });
  aplicarVista();
}

// ---- Arrastrar el lienzo -------------------------------------------------
// Requisito del minimapa: sin desplazamiento, el recuadro no tendría a dónde
// mover. blank:pointerdown es el evento correcto porque excluye los nodos, que
// siguen arrastrándose por su cuenta.
let panDesde = null;

paper.on("blank:pointerdown", evt => {
  const t = paper.translate();
  panDesde = { x: evt.clientX, y: evt.clientY, tx: t.tx, ty: t.ty };
  paper.el.classList.add("agarrando");
});

document.addEventListener("pointermove", e => {
  if (!panDesde) return;
  paper.translate(panDesde.tx + e.clientX - panDesde.x,
                  panDesde.ty + e.clientY - panDesde.y);
  pintarRecuadro();
});

document.addEventListener("pointerup", () => {
  if (!panDesde) return;
  panDesde = null;
  paper.el.classList.remove("agarrando");
});

// ---- Arrastrar el recuadro del minimapa ----------------------------------
let miniDesde = null;

recuadro.addEventListener("pointerdown", e => {
  e.preventDefault();
  const t = paper.translate();
  miniDesde = { x: e.clientX, y: e.clientY, tx: t.tx, ty: t.ty };
  recuadro.setPointerCapture(e.pointerId);
  recuadro.classList.add("agarrando");
});

recuadro.addEventListener("pointermove", e => {
  if (!miniDesde) return;
  // Un píxel del minimapa son (escalaLienzo / escalaMini) píxeles de lienzo, y
  // el lienzo se mueve al REVÉS que el recuadro.
  const factor = paper.scale().sx / miniEscala;
  paper.translate(miniDesde.tx - (e.clientX - miniDesde.x) * factor,
                  miniDesde.ty - (e.clientY - miniDesde.y) * factor);
  pintarRecuadro();
});

recuadro.addEventListener("pointerup", () => {
  miniDesde = null;
  recuadro.classList.remove("agarrando");
});

// Clic en el minimapa (fuera del recuadro): centra el lienzo ahí.
cajaMini.addEventListener("pointerdown", e => {
  if (e.target === recuadro) return;
  const caja = cajaMini.getBoundingClientRect();
  const px = ((e.clientX - caja.left) - miniTx) / miniEscala;
  const py = ((e.clientY - caja.top) - miniTy) / miniEscala;
  const z = paper.scale().sx, area = paper.el.getBoundingClientRect();
  paper.translate(area.width / 2 - px * z, area.height / 2 - py * z);
  pintarRecuadro();
});

// ---- Plegar el minimapa --------------------------------------------------
const PLEGADO = "minimapa-plegado";
const ctlToggle = document.getElementById("mz-toggle");

function pintarPlegado(plegado) {
  cajaMini.hidden = plegado;
  ctlToggle.setAttribute("aria-expanded", String(!plegado));
  ctlToggle.setAttribute("aria-label", plegado ? "Mostrar minimapa" : "Ocultar minimapa");
  ctlToggle.textContent = plegado ? "🗺" : "▾";
  if (!plegado) { encuadrarMini(); pintarRecuadro(); }
}

ctlToggle.onclick = () => {
  const plegado = ctlToggle.getAttribute("aria-expanded") === "true";
  pintarPlegado(plegado);
  // Por el mismo camino que el resto del panel: si lo pliegas, sigue plegado
  // después de guardar un párrafo o de cualquier recarga.
  if (window.EstadoVista) EstadoVista.guardar(PLEGADO, plegado);
};

// ---- Controles -----------------------------------------------------------
ctlMenos.onclick = () => zoomA(paper.scale().sx - ZOOM_PASO);
ctlMas.onclick = () => zoomA(paper.scale().sx + ZOOM_PASO);
ctlRango.oninput = () => zoomA(aZoom(Number(ctlRango.value)));
document.getElementById("mz-fit").onclick = ajustar;
// La barra de arriba y el minimapa mandan al MISMO sitio: dos controles que
// hacen lo mismo no pueden acabar contando escalas distintas.
document.getElementById("zoom-ajustar").onclick = ajustar;
document.getElementById("zoom-mas").onclick = () => zoomA(paper.scale().sx + ZOOM_PASO);
document.getElementById("zoom-menos").onclick = () => zoomA(paper.scale().sx - ZOOM_PASO);

document.getElementById("reacomodar").onclick = () => {
  localStorage.removeItem(POS_KEY);
  Object.entries(BASE).forEach(([id, [x, y]]) => graph.getCell(id)?.position(x, y));
  encuadrarMini();
  aplicarVista();
};

// Mover un nodo cambia el contenido: el minimapa se reencuadra para seguir
// representando el lienzo completo.
paper.on("element:pointerup", () => { encuadrarMini(); pintarRecuadro(); });
window.addEventListener("resize", () => { encuadrarMini(); pintarRecuadro(); });

encuadrarMini();
aplicarVista();
// EstadoVista lo publica static/js/estado-vista.js, que todavía no ha corrido
// mientras se parsea este script: la preferencia se recupera después.
document.addEventListener("DOMContentLoaded", () => {
  if (window.EstadoVista && EstadoVista.leer(PLEGADO) === true) pintarPlegado(true);
});

// ---- Panel de propiedades ----
const titulo = document.getElementById("panel-titulo");
const cuerpo = document.getElementById("panel-cuerpo");
const err = msg => { const e = document.getElementById("err"); if (e) e.textContent = msg; };
// Varias perillas en UNA acción: se mandan en orden y la vista se recarga al
// final. Antes cada guardado recargaba por su cuenta, así que un "Guardar" que
// tocaba dos claves perdía la segunda: la página se iba antes de mandarla.
function guardarVarios(pares) {
  return pares
    .reduce((cadena, [key, value]) =>
      cadena.then(() => post("/agente/config", { key, value: String(value) })),
      Promise.resolve())
    .then(() => location.reload())
    .catch(e => err(e.message));
}

const guardar = (key, value) => guardarVarios([[key, value]]);

function ocultarCorpus() {
  document.getElementById("bloque-corpus").hidden = true;
  document.getElementById("corpus-hr").hidden = true;
}

function mostrarCorpus() {
  titulo.textContent = "Corpus informativo";
  cuerpo.innerHTML = "";
  document.getElementById("bloque-corpus").hidden = false;
  document.getElementById("corpus-hr").hidden = false;
}

// ---- Match literal: sinónimos del catálogo (se piden al clic, no en cada carga) ----
let CATALOGO = null;

function errPanel(msg) {
  const e = document.getElementById("err"); if (e) e.textContent = msg;
}

function pintarSinonimos(servicio) {
  const cont = document.getElementById("lista-sinonimos");
  if (!cont) return;
  cont.innerHTML = "";
  if (!servicio.sinonimos.length) {
    const p = document.createElement("p"); p.className = "muted mini";
    p.textContent = "Sin sinónimos: este servicio solo empata por su nombre oficial.";
    cont.append(p); return;
  }
  servicio.sinonimos.forEach(s => {
    const chip = document.createElement("span"); chip.className = "sinonimo-chip";
    chip.textContent = s;
    const q = document.createElement("button"); q.className = "btn-mini";
    q.textContent = "×"; q.setAttribute("aria-label", `Quitar el sinónimo ${s}`);
    q.onclick = () => { if (confirm(`¿Quitar «${s}» de ${servicio.nombre}?`))
      post("/agente/sinonimos", { service_id: servicio.id, texto: s, quitar: true })
        .then(() => { servicio.sinonimos = servicio.sinonimos.filter(x => x !== s);
                      pintarSinonimos(servicio); })
        .catch(e => errPanel(e.message)); };
    chip.append(q); cont.append(chip);
  });
}

function montarEditorSinonimos() {
  const sel = document.getElementById("f-servicio");
  if (!sel || !CATALOGO) return;
  sel.innerHTML = "";
  CATALOGO.servicios.forEach((s, i) => {
    const o = document.createElement("option"); o.value = String(i);
    o.textContent = `${s.nombre} · ${s.categoria}`; sel.append(o);
  });
  const actual = () => CATALOGO.servicios[Number(sel.value) || 0];
  sel.onchange = () => pintarSinonimos(actual());
  document.getElementById("btn-sinonimo").onclick = () => {
    const input = document.getElementById("f-sinonimo");
    const texto = input.value.trim(); if (!texto) return;
    const s = actual();
    post("/agente/sinonimos", { service_id: s.id, texto })
      .then(() => { s.sinonimos = s.sinonimos.concat(texto); input.value = "";
                    errPanel(""); pintarSinonimos(s); })
      .catch(e => errPanel(e.message));
  };
  pintarSinonimos(actual());
}

// ---- Controles armados desde el descriptor del servidor -------------------
// El tipo, las cotas, el paso y la ayuda de cada perilla los publica
// agent_settings.CONFIG_EDITABLE. Antes estaban escritos aquí a mano (min="1"
// max="5"…) y podían quedar por debajo o por encima de lo que el servidor
// acepta; ahora agregar una perilla no toca este archivo.

function ayudaDe(p) {
  return p.ayuda ? `<p class="muted mini">${esc(p.ayuda)}</p>` : "";
}

function campo(clave) {
  const p = ESTADO.config[clave], id = "f-" + clave, out = id + "-out";
  const comun = `id="${id}" data-clave="${clave}"`;
  if (p.tipo === "texto")
    return `<label for="${id}">${esc(p.etiqueta)}</label>
      <input ${comun} maxlength="${esc(p.maximo)}" value="${esc(String(p.valor))}">` + ayudaDe(p);
  if (p.tipo === "entero")
    // Deslizador con su <output>: el valor se lee sin arrastrar, y el output
    // es un live region nativo para lectores de pantalla.
    return `<label for="${id}">${esc(p.etiqueta)} (${esc(p.minimo)}–${esc(p.maximo)}):
        <output id="${out}">${esc(p.valor)}</output></label>
      <input ${comun} type="range" min="${esc(p.minimo)}" max="${esc(p.maximo)}" step="${esc(p.paso)}"
        value="${esc(p.valor)}">` + ayudaDe(p);
  return `<label for="${id}">${esc(p.etiqueta)}${
      p.unidad ? ` (${esc(p.unidad.trim())})` : ""}</label>
    <input ${comun} type="number" min="${esc(p.minimo)}" max="${esc(p.maximo)}" step="${esc(p.paso)}"
      value="${esc(p.valor)}">` + ayudaDe(p);
}

function grupoCampos(claves, etiquetaBoton) {
  return `<div class="panel-grupo">` + claves.map(campo).join("") +
    `<p><button class="btn-mini primario" data-accion="guardar-grupo">${
       etiquetaBoton || "Guardar"}</button></p></div>`;
}

window.guardarGrupo = function (boton) {
  const campos = Array.from(
    boton.closest(".panel-grupo").querySelectorAll("[data-clave]"));
  return guardarVarios(campos.map(el => [el.dataset.clave, el.value]));
};

function panelNodo(n) {
  ocultarCorpus();
  titulo.textContent = n.nombre;
  const c = ESTADO.config;
  let h = `<p class="muted mini">${esc(n.detalle)}</p>
    <p><strong>${esc(n.mensajes)}</strong> mensajes en 14 días` +
    (n.descartes ? ` · <strong>${esc(n.descartes)}</strong> descartes de guardrail` : "") + `</p>`;
  // Los avisos van ARRIBA de todo: si el nodo no puede trabajar, eso es lo
  // primero que hay que leer, no la configuración.
  if ((n.avisos || []).length) h += `<ul class="avisos" role="alert">` +
    n.avisos.map(a => `<li>⚠ ${esc(a)}</li>`).join("") + `</ul>`;
  if (n.flag) {
    // La clave de configuración la manda el servidor junto con el nodo
    // (`flag`): tenerla también aquí era un segundo catálogo que se
    // desincroniza el día que se agrega un nodo con interruptor.
    const key = n.flag, que = n.id === "reparto" ? "reparto" : "capa";
    h += `<p><button class="btn-mini primario" id="btn-toggle">${n.activo ? "Apagar" : "Encender"} ${que}</button></p>`;
    setTimeout(() => document.getElementById("btn-toggle").onclick =
      () => guardar(key, n.activo ? 0 : 1), 0);
  }
  if (n.id === "tools") {
    // Herramientas: uso real (14 d) + apagador por herramienta. Las de permiso
    // "deny" se muestran BLOQUEADAS y sin control: que se vea el candado.
    h += `<h3 class="mini-titulo">Herramientas</h3><div class="tools-lista">` +
      (ESTADO.herramientas || []).map(t => `
        <div class="tool-fila${t.bloqueada ? " bloqueada" : ""}">
          <div><strong>${esc(t.nombre)}</strong>
            <span class="muted mini">${esc(t.usos)} usos · ${esc(t.errores)} errores · 14d</span></div>
          ${t.bloqueada
            ? `<span class="tool-candado" title="Mover dinero es del panel del asesor, nunca del modelo">🔒 bloqueada</span>`
            : `<button class="btn-mini" data-tool="${esc(t.nombre)}">${
                 t.activa ? "Apagar" : "Encender"}</button>`}
        </div>`).join("") + `</div>` +
      grupoCampos(["agent_max_turnos", "agent_max_words"], "Guardar topes");
    setTimeout(() => document.querySelectorAll("[data-tool]").forEach(b => {
      const clave = b.dataset.tool === "cotizar_servicio" ? "tool_cotizar_enabled" : "tool_folios_enabled";
      b.onclick = () => guardar(clave, c[clave].valor ? 0 : 1);
    }), 0);
  }
  if (n.id === "ollama" && !n.fijo)
    h += grupoCampos(["ollama_model", "ollama_timeout"]);
  if (n.id === "fuzzy") h += grupoCampos(["fuzzy_threshold"]);
  if (n.perillas) {
    // Perillas de casos-analisis (regla del folio y de la revisión): las claves y la matriz de
    // conducta las publica el servidor (agent_settings.MATRIZ_CASOS); aquí solo se pintan.
    const bools = n.perillas.filter(k => c[k] && c[k].tipo === "bool");
    const numeros = n.perillas.filter(k => c[k] && c[k].tipo !== "bool");
    h += `<h3 class="mini-titulo">Folio y revisión</h3>` + bools.map(k => `
      <div class="tool-fila"><div><strong>${esc(c[k].etiqueta)}</strong>
        <span class="muted mini">${c[k].valor ? "encendida" : "apagada"}</span>${ayudaDe(c[k])}</div>
        <button class="btn-mini" data-perilla="${esc(k)}">${c[k].valor ? "Apagar" : "Encender"}</button>
      </div>`).join("") + (numeros.length ? grupoCampos(numeros) : "");
    h += `<div class="table-wrap"><table class="mini"><caption class="muted mini">Qué hace el bot
      con cada combinación</caption><thead><tr><th scope="col">Folio</th><th scope="col">Revisión</th>
      <th scope="col">Conducta</th></tr></thead><tbody>` +
      (ESTADO.matriz_casos || []).map(f => `<tr>${f.map(x => `<td>${esc(x)}</td>`).join("")}</tr>`).join("") +
      `</tbody></table></div>`;
    setTimeout(() => document.querySelectorAll("[data-perilla]").forEach(b => {
      b.onclick = () => guardar(b.dataset.perilla, c[b.dataset.perilla].valor ? 0 : 1);
    }), 0);
  }
  if (n.id === "literal") {
    // Esta capa no tiene flags: la alimentan los DATOS del catálogo.
    h += `<p class="muted mini">No se configura con flags: la alimenta el <strong>catálogo</strong>
        — el nombre oficial del servicio y sus sinónimos aprobados.</p>
      <p><a class="btn-mini" href="/unmatched">Propuestas por aprobar (${esc(PENDIENTES)})</a></p>
      <label for="f-servicio">Sinónimos por servicio</label>
      <select id="f-servicio"><option>cargando…</option></select>
      <div id="lista-sinonimos" class="sinonimos"></div>
      <div class="fila-sinonimo">
        <input id="f-sinonimo" maxlength="80" placeholder="nuevo sinónimo">
        <button class="btn-mini primario" id="btn-sinonimo">Agregar</button>
      </div>
      <p class="muted mini">La sub-capa <em>keyword</em> no tiene campo propio: empata subcadenas
        del nombre oficial («…divorcio…» encuentra «Acta de divorcio»). Para renombrar un servicio,
        <a href="/catalog">Catálogo</a>.</p>`;
    setTimeout(() => {
      if (CATALOGO) return montarEditorSinonimos();
      fetch("/agente/sinonimos", { credentials: "same-origin" })
        .then(r => r.json())
        .then(d => { CATALOGO = d; montarEditorSinonimos(); })
        .catch(() => { const s = document.getElementById("f-servicio");
                       if (s) s.innerHTML = "<option>error al cargar</option>"; });
    }, 0);
  }
  if (n.id === "ollama" && n.fijo)
    h += `<p class="muted mini">Apagado: falta OLLAMA_URL en el .env del servidor.</p>`;
  if (n.id === "escalada")
    h += `<p class="muted mini">Siempre activa: es el último recurso de la cascada.</p>
      <p><a class="btn-mini" href="/?status=escalados">Ver folios escalados</a></p>`;
  cuerpo.innerHTML = h + `<p class="agente-error" id="err"></p>`;
}
window.guardar = guardar;



// Selección del lienzo: qué nodo está abierto NO vive en el DOM, así que se
// preserva por el gancho compartido (static/js/estado-vista.js). Sin esto,
// cualquier botón que recargue —guardar un párrafo, quitar un sinónimo— te
// devolvía al panel genérico y había que volver a buscar el nodo.
const SELECCION = "nodo-abierto";

function grupoDe(id) {
  const vista = paper.findViewByModel(id === "corpus" ? corpus : porId[id]);
  return vista && vista.el;
}

function abrirNodo(id, opciones) {
  if (window.EstadoVista) EstadoVista.guardar(SELECCION, id);
  // Un solo nodo seleccionado a la vez, y el estado se refleja también en
  // aria-current: un lector de pantalla anuncia cuál está abierto.
  document.querySelectorAll("#lienzo .joint-cell.est-sel").forEach(g => {
    g.classList.remove("est-sel");
    g.removeAttribute("aria-current");
  });
  const g = grupoDe(id);
  if (g) {
    g.classList.add("est-sel");
    g.setAttribute("aria-current", "true");
    // El foco sigue a la selección, como en el demo: quien navega con teclado
    // continúa desde el nodo que acaba de abrir.
    if (opciones && opciones.enfocar) g.focus();
  }
  if (id === "corpus") { mostrarCorpus(); return; }
  const d = porId[id] && porId[id].prop("datos");
  if (d) panelNodo(d);
}

paper.on("element:pointerclick", v => abrirNodo(v.model.id));

// ---- Teclado: el lienzo deja de ser solo para ratón --------------------
// Cada nodo entra en el orden de tabulación con su rol y su nombre, y se
// abre con Enter o Espacio. Sin esto, toda la configuración del agente era
// inalcanzable sin ratón.
[...ESTADO.nodos.map(n => n.id), "corpus"].forEach(id => {
  const g = grupoDe(id);
  if (!g) return;
  const datos = porId[id] && porId[id].prop("datos");
  g.setAttribute("tabindex", "0");
  g.setAttribute("role", "button");
  // Sin prefijo cuando el nombre ya empieza por "Capa" ("Capa Capa informativa").
  const nombre = id === "corpus" ? "Corpus informativo" : (datos ? datos.nombre : id);
  const base = /^capa/i.test(nombre) ? nombre : `Capa ${nombre}`;
  // El ⚠ del lienzo es puramente visual: sin esto, un lector de pantalla
  // recorre los nodos sin enterarse de cuál está roto.
  const cuantos = (datos && datos.avisos || []).length;
  g.setAttribute("aria-label", cuantos
    ? `${base} — ${cuantos} aviso${cuantos > 1 ? "s" : ""} de configuración`
    : base);
  g.addEventListener("keydown", e => {
    if (e.key !== "Enter" && e.key !== " " && e.key !== "Spacebar") return;
    e.preventDefault();  // Espacio no debe desplazar la página
    abrirNodo(id);
  });
  // Llegar por teclado (Tab) también selecciona, como el onFocus del demo.
  g.addEventListener("focus", () => { if (!g.classList.contains("est-sel")) abrirNodo(id); });
});

// Al cargar, se reabre el último nodo que el usuario tenía abierto. Va en
// DOMContentLoaded porque estado-vista.js se carga con defer: cuando este
// script corre (durante el parseo) window.EstadoVista todavía no existe.
document.addEventListener("DOMContentLoaded", () => {
  if (!window.EstadoVista) return;
  const previo = EstadoVista.leer(SELECCION);
  if (previo && (previo === "corpus" || porId[previo])) abrirNodo(previo);
});

// ---- Corpus: chips con edición inline ----
const lista = document.getElementById("lista-parrafos");

function chipLectura(div, pf) {
  div.innerHTML = "";
  const t = document.createElement("p"); t.textContent = pf.texto;   // sin inyección
  const b = document.createElement("button"); b.className = "btn-mini"; b.textContent = "Editar";
  b.onclick = () => chipEdicion(div, pf);
  const q = document.createElement("button"); q.className = "btn-mini"; q.textContent = "🗑";
  q.setAttribute("aria-label", "Quitar párrafo");
  q.onclick = () => { if (confirm("¿Quitar este párrafo del corpus?"))
    post(`/agente/corpus/${pf.id}`, { borrar: true }).then(() => location.reload()); };
  div.append(t, b, q);
}

function chipEdicion(div, pf) {
  div.innerHTML = "";
  const ta = document.createElement("textarea"); ta.maxLength = 1200; ta.value = pf ? pf.texto : "";
  const g = document.createElement("button"); g.className = "btn-mini primario"; g.textContent = "Guardar";
  const c = document.createElement("button"); c.className = "btn-mini"; c.textContent = "Cancelar";
  const e = document.createElement("p"); e.className = "agente-error";
  g.onclick = () => (pf ? post(`/agente/corpus/${pf.id}`, { texto: ta.value })
                        : post("/agente/corpus", { texto: ta.value }))
    .then(() => location.reload()).catch(x => e.textContent = x.message);
  c.onclick = () => pf ? chipLectura(div, pf) : div.remove();
  div.append(ta, g, c, e);
  ta.focus();
}

ESTADO.parrafos.forEach(pf => {
  const div = document.createElement("div"); div.className = "parrafo-chip";
  chipLectura(div, pf); lista.append(div);
});
document.getElementById("btn-nuevo-parrafo").onclick = () => {
  const div = document.createElement("div"); div.className = "parrafo-chip";
  chipEdicion(div, null); lista.prepend(div);
};

// ---- Probador (el "Run" del demo, contra la cascada real) ----
const res = document.getElementById("probar-resultado");
document.getElementById("btn-probar").onclick = () => {
  const texto = document.getElementById("probar-texto").value.trim();
  if (!texto) return;
  res.innerHTML = `<em>Probando contra el pipeline real… (la capa local tarda ~5 s)</em>`;
  post("/agente/probar", { texto }).then(r => {
    // Por clase y no por atributo: así el ganador convive con la selección y
    // el foco en una sola cascada, en vez de dos sistemas pisándose el borde.
    document.querySelectorAll("#lienzo .joint-cell.est-ganador")
      .forEach(g => g.classList.remove("est-ganador"));
    // Sin entrada para "llm": esa capa ya no existe en el lienzo, y un evento
    // histórico suyo cae al fallback (escalada) en vez de no resaltar nada.
    const capaNodo = { nombre:"literal", sinonimo:"literal", keyword:"literal", ollama:"ollama",
      fuzzy:"fuzzy", categoria:"literal", multi:"literal", ninguna:"escalada" }[r.capa] || "escalada";
    const gana = r.respuesta_rag ? "rag" : capaNodo;
    grupoDe(gana)?.classList.add("est-ganador");
    let msg = `Capa <strong>${r.capa}</strong> en ${r.ms} ms`;
    if (r.servicio) msg += ` → servicio <strong>${esc(r.servicio)}</strong>`;
    if (r.categoria) msg += ` → categoría <strong>${esc(r.categoria)}</strong>`;
    if (r.es_vago) msg += ` · <em>mensaje vago (guardrail activo)</em>`;
    if (r.respuesta_rag) msg += `<br>RAG respondería: “${esc(r.respuesta_rag)}”`;
    if (r.rag_descartada) msg += ` · guardrail descartó la respuesta (${r.rag_descartada})`;
    if (r.rag_sin_respuesta) msg += `<br><em>La capa RAG corrió pero no dio respuesta (NO_SE del modelo, timeout o red — revisar logs del bot).</em>`;
    // Desglose de la recuperación: qué párrafos viajaron y por qué vía
    // (léxico/vectorial + coseno). Sin esto el umbral se calibra a ciegas.
    if (r.retrieval) {
      if (r.retrieval.docs && r.retrieval.docs.length) {
        const filas = r.retrieval.docs.map(d => {
          const cos = d.coseno === null ? "" : ` cos ${d.coseno}`;
          return `· ${esc(d.texto)}… <em>[${d.vias.join("+") || "fusión"}${cos}]</em>`;
        });
        msg += `<br><small>Recuperación ${esc(r.retrieval.retriever)}` +
               (r.retrieval.umbral !== undefined ? ` (umbral ${r.retrieval.umbral})` : "") +
               `:<br>${filas.join("<br>")}</small>`;
      }
      if (r.retrieval.error) msg += `<br><small>⚠ retrieval: ${esc(r.retrieval.error)}</small>`;
    }
    // El motivo exacto cuando el servidor no contestó: antes un timeout y un
    // "no sé" del modelo se veían idénticos desde aquí.
    if (r.ollama_error) msg += `<br>⚠ <strong>Ollama falló</strong> (${esc(r.ollama_error)}): la respuesta salió de una capa de abajo.`;
    if (r.sugerencias && r.sugerencias.length && !r.respuesta_rag)
      msg += `<br>Mini-menú: ${r.sugerencias.map(esc).join(", ")}`;
    if (r.le_tocaria_tools) msg += `<br><em>Le tocaría al nodo agéntico (no se ejecuta en pruebas).</em>`;
    res.innerHTML = `<span class="ok">${msg}</span>`;
  }).catch(e => res.innerHTML = `<span class="err">${esc(e.message)}</span>`);
};
document.getElementById("probar-texto").addEventListener("keydown",
  e => { if (e.key === "Enter") document.getElementById("btn-probar").click(); });

// Antes eran oninput/onclick dentro del HTML que arman campo() y grupoCampos():
// la CSP ya no ejecuta manejadores en línea, así que se conectan aquí.
document.addEventListener("input", e => {
  const rango = e.target.closest && e.target.closest('input[type="range"][data-clave]');
  const salida = rango && document.getElementById(rango.id + "-out");
  if (salida) salida.value = rango.value;
});
document.addEventListener("click", e => {
  const boton = e.target.closest && e.target.closest('button[data-accion="guardar-grupo"]');
  if (boton) window.guardarGrupo(boton);
});
