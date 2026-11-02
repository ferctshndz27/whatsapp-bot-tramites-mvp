// Pasos de V5 (WhatsApp /crm) para probar-vista.mjs --modulo=whatsapp --pasos=ext-cse/prueba/pasos-whatsapp.mjs
// Recorre lo que usa un asesor en el panel angosto: tablero → abrir un chat → cajón del folio → «Mis respuestas».
// Mide desbordes (nada debe salirse del marco ni obligar a scroll lateral) y fotografía cada estado.

// Elementos visibles cuyo borde derecho pasa del ancho del marco: lo que el usuario vería cortado.
const DESBORDADOS = `(() => {
  const ancho = document.documentElement.clientWidth;
  return [...document.querySelectorAll(".crm *")]
    .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.right > ancho + 1; })
    // lo que está dentro de un contenedor con su propio scroll horizontal (pestañas, riel) no cuenta
    .filter((e) => { for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if (o === "auto" || o === "scroll" || o === "hidden") return false; } return true; })
    .slice(0, 8).map((e) => (e.id ? "#" + e.id : e.tagName.toLowerCase() + "." + String(e.className).split(" ")[0]));
})()`;

const ESTADO = `(() => ({
  ancho: document.documentElement.clientWidth,
  scroll_horizontal: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  chat_abierto: document.getElementById("crm").classList.contains("chat-abierto"),
  tarjetas: document.querySelectorAll("#crm-board .card").length,
  franja_superior: Math.round(document.querySelector(".crm-layout").getBoundingClientRect().top), // alto de lo que hay antes de crm-layout
  bandeja: document.querySelectorAll("#crm-tray-list .tray-item").length,
  panel_abierto: !document.getElementById("crm-panel").hasAttribute("inert"),
  panel_ancho: Math.round(document.getElementById("crm-panel").getBoundingClientRect().width),
  panel_seccion: document.querySelector("#crm-panel-title")?.textContent || "",
  dialogo: [...document.querySelectorAll("dialog")].filter((d) => d.open).map((d) => d.id),
  compositor_visible: (() => { const r = document.getElementById("crm-composer")?.getBoundingClientRect(); return Boolean(r && r.width > 0 && r.bottom <= innerHeight + 1); })(),
}))()`;

export async function pasos({ evaluar, fotografiar, pausa, hasta, medido, fallos }) {
  const anotar = (cond, texto) => { if (!cond) fallos.push(texto); };
  medido.v5 = {};

  // 1. Tablero y bandeja cargados, sin nada cortado.
  await hasta(() => evaluar(`document.querySelectorAll("#crm-board .card").length > 0`), 10000);
  medido.v5.tablero = await evaluar(ESTADO);
  medido.v5.tablero.desbordados = await evaluar(DESBORDADOS);
  anotar(medido.v5.tablero.tarjetas > 0, "el tablero no pintó tarjetas");
  anotar(medido.v5.tablero.scroll_horizontal <= 0, `tablero: scroll horizontal de ${medido.v5.tablero.scroll_horizontal}px`);
  anotar(medido.v5.tablero.desbordados.length === 0, `tablero: se sale del marco ${medido.v5.tablero.desbordados.join(", ")}`);
  await fotografiar("1-tablero");

  // 2. Abrir un chat desde la tarjeta del tablero.
  await evaluar(`document.querySelector("#crm-board .card").click()`);
  const abrio = await hasta(() => evaluar(`document.getElementById("crm").classList.contains("chat-abierto") && document.querySelectorAll("#crm-chat-thread .bubble").length > 0`), 10000);
  anotar(abrio, "al pulsar una tarjeta no se abrió el chat con mensajes");
  await pausa(600);
  medido.v5.chat = await evaluar(ESTADO);
  medido.v5.chat.desbordados = await evaluar(DESBORDADOS);
  anotar(medido.v5.chat.scroll_horizontal <= 0, `chat: scroll horizontal de ${medido.v5.chat.scroll_horizontal}px`);
  anotar(medido.v5.chat.desbordados.length === 0, `chat: se sale del marco ${medido.v5.chat.desbordados.join(", ")}`);
  anotar(medido.v5.chat.compositor_visible, "chat: el cuadro para escribir no queda a la vista");
  await fotografiar("2-chat");

  // 3. Cajón del folio: cada botón del riel abre su sección dentro del ancho del marco.
  const secciones = await evaluar(`[...document.querySelectorAll("#crm .accion[data-seccion]")].map((b) => b.dataset.seccion)`);
  medido.v5.cajon = {};
  for (const seccion of secciones) {
    await evaluar(`document.querySelector('#crm .accion[data-seccion="${seccion}"]').click()`);
    await hasta(() => evaluar(`!document.getElementById("crm-panel").hasAttribute("inert")`), 5000);
    await pausa(500);
    const e = await evaluar(ESTADO);
    e.desbordados = await evaluar(DESBORDADOS);
    medido.v5.cajon[seccion] = e;
    anotar(e.panel_abierto, `cajón «${seccion}»: no abrió`);
    anotar(e.panel_ancho <= e.ancho, `cajón «${seccion}»: mide ${e.panel_ancho}px en un marco de ${e.ancho}px`);
    anotar(e.scroll_horizontal <= 0, `cajón «${seccion}»: scroll horizontal de ${e.scroll_horizontal}px`);
    anotar(e.desbordados.length === 0, `cajón «${seccion}»: se sale del marco ${e.desbordados.join(", ")}`);
    if (seccion === secciones[0]) await fotografiar("3-cajon");
  }
  await evaluar(`document.getElementById("crm-panel-close").click()`);
  await pausa(400);
  anotar(await evaluar(`document.getElementById("crm-panel").hasAttribute("inert")`), "el cajón no se cerró con ✕");

  // 4. «Mis respuestas»: el diálogo cabe y se cierra.
  await evaluar(`document.getElementById("crm-btn-respuestas-chat")?.click()`);
  const dlg = await hasta(() => evaluar(`document.getElementById("crm-dlg-respuestas").open`), 5000);
  anotar(dlg, "«Mis respuestas» no abrió");
  await pausa(500);
  medido.v5.respuestas = await evaluar(`(() => { const r = document.getElementById("crm-dlg-respuestas").getBoundingClientRect(); return { ancho: Math.round(r.width), izq: Math.round(r.left), der: Math.round(r.right), marco: document.documentElement.clientWidth }; })()`);
  anotar(medido.v5.respuestas.izq >= 0 && medido.v5.respuestas.der <= medido.v5.respuestas.marco, `«Mis respuestas» se sale del marco: ${JSON.stringify(medido.v5.respuestas)}`);
  await fotografiar("4-respuestas");
  await evaluar(`document.getElementById("crm-resp-cerrar").click()`);
  await pausa(300);
  anotar(!(await evaluar(`document.getElementById("crm-dlg-respuestas").open`)), "«Mis respuestas» no se cerró");

  // 5. Volver al tablero con el botón de la barra.
  await evaluar(`document.getElementById("crm-btn-board").click()`);
  await pausa(500);
  anotar(!(await evaluar(`document.getElementById("crm").classList.contains("chat-abierto")`)), "«Tablero» no devolvió al tablero");
}
