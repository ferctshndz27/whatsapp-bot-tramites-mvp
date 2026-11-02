// Pasos de V8 (Solicitudes `/` y detalle `/tickets/{id}`) para probar-vista.mjs --modulo=solicitudes.
// Lista → filtro → abrir un folio → «Volver» (debe seguir embebido) → «Ficha del cliente».
const DESBORDADOS = `(() => {
  const ancho = document.documentElement.clientWidth;
  return [...document.querySelectorAll("main *, .main *")]
    .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.right > ancho + 1; })
    .filter((e) => { for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if (o === "auto" || o === "scroll" || o === "hidden") return false; } return true; })
    .slice(0, 8).map((e) => e.tagName.toLowerCase() + "." + String(e.className).split(" ")[0]);
})()`;
const ESTADO = `(() => ({
  ruta: location.pathname + location.search, embebido: document.body.classList.contains("embebido"),
  menu: Boolean(document.querySelector(".sidebar")), scroll_horizontal: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  filas: document.querySelectorAll("tr[data-href]").length, h1: document.querySelector("h1")?.textContent.trim() || "",
  rotulos_vacios: [...document.querySelectorAll(".tabla-tarjetas td")].filter((td) => !td.dataset.label).length,
}))()`;

export async function pasos({ evaluar, fotografiar, pausa, hasta, medido, fallos }) {
  const anotar = (cond, texto) => { if (!cond) fallos.push(texto); };
  const m = (medido.v8 = {});
  await hasta(() => evaluar(`document.querySelectorAll("tr[data-href]").length > 0`), 10000);
  m.lista = await evaluar(ESTADO); m.lista.desbordados = await evaluar(DESBORDADOS);
  anotar(m.lista.filas > 0, "la lista no trae solicitudes");
  anotar(m.lista.scroll_horizontal <= 0, `lista: scroll horizontal de ${m.lista.scroll_horizontal}px`);
  anotar(m.lista.desbordados.length === 0, `lista: se sale del marco ${m.lista.desbordados.join(", ")}`);
  anotar(m.lista.rotulos_vacios === 0, "lista: celdas sin rótulo");
  await fotografiar("1-lista");

  // Filtro por estado: sigue embebido.
  await evaluar(`document.querySelector(".pills a[href*='status=']").click()`);
  await pausa(1200);
  m.filtro = await evaluar(ESTADO);
  anotar(m.filtro.embebido && !m.filtro.menu, "el filtro salió del modo embebido");
  await evaluar(`document.querySelector(".pills a[href='/']").click()`);
  await pausa(1200);

  // Abrir el primer folio.
  await evaluar(`document.querySelector("tr[data-href] a").click()`);
  await hasta(() => evaluar(`location.pathname.startsWith("/tickets/")`), 8000);
  await pausa(600);
  m.detalle = await evaluar(ESTADO); m.detalle.desbordados = await evaluar(DESBORDADOS);
  anotar(m.detalle.embebido && !m.detalle.menu, "el detalle no quedó embebido");
  anotar(m.detalle.scroll_horizontal <= 0, `detalle: scroll horizontal de ${m.detalle.scroll_horizontal}px`);
  anotar(m.detalle.desbordados.length === 0, `detalle: se sale del marco ${m.detalle.desbordados.join(", ")}`);
  await fotografiar("2-detalle");

  // «Volver a solicitudes» regresa a la lista, embebida.
  await evaluar(`[...document.querySelectorAll("a.btn-nav")].find((a) => /Volver/.test(a.textContent)).click()`);
  await hasta(() => evaluar(`location.pathname === "/"`), 8000);
  await pausa(600);
  m.vuelta = await evaluar(ESTADO);
  anotar(m.vuelta.embebido && !m.vuelta.menu && m.vuelta.filas > 0, "«Volver» no regresó a la lista embebida");
}
