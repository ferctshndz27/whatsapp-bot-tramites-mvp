// Pasos del botón «Nuevo cobro» para probar-vista.mjs --modulo=nuevocobro [--ancho=340].
// El botón de la barra abre /cobros/nuevo en el marco; ahí: medición → buscar a Ana → elegirla →
// «Generar liga» (el bot de prueba no tiene Stripe: debe volver al formulario con el motivo, sin salirse del
// modo embebido ni dejar scroll horizontal).
const ESTADO = `(() => ({ ruta: location.pathname + location.search, embebido: document.body.classList.contains("embebido"), menu: Boolean(document.querySelector(".sidebar")),
  scroll_horizontal: document.documentElement.scrollWidth - document.documentElement.clientWidth }))()`;
export async function pasos({ evaluar, fotografiar, pausa, hasta, medido, fallos }) {
  const anotar = (c, t) => { if (!c) fallos.push(t); };
  medido.nuevocobro = {};
  const ver = async (n) => { const e = (medido.nuevocobro[n] = await evaluar(ESTADO)); anotar(e.embebido && !e.menu, `${n}: salió del modo embebido`); anotar(e.scroll_horizontal <= 0, `${n}: scroll horizontal ${e.scroll_horizontal}px`); return e; };
  await hasta(() => evaluar(`location.pathname === "/cobros/nuevo"`), 10000); await pausa(600);
  await ver("inicio");
  const e = await evaluar(`document.querySelector("#cn-buscar").offsetParent !== null`);
  anotar(e, "el buscador no se ve al abrir");
  await evaluar(`(() => { const i = document.querySelector("#cn-buscar"); i.value = "Ana"; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  await hasta(() => evaluar(`document.querySelectorAll(".cn-resultado").length > 0`), 8000);
  await ver("resultados"); await fotografiar("1-resultados");
  await evaluar(`document.querySelector(".cn-resultado").click()`); await pausa(300);
  anotar(await evaluar(`document.querySelector("#cn-phone").value === "525500000011"`), "elegir a Ana no llenó el teléfono");
  await ver("elegido"); await fotografiar("2-elegido");
  await evaluar(`document.querySelector("[data-cn-enviar]").click()`);
  await hasta(() => evaluar(`document.readyState === "complete" && location.pathname === "/cobros/nuevo"`), 8000); await pausa(600);
  await ver("enviado"); await fotografiar("3-enviado");
}
