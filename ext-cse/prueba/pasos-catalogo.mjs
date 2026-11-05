// Pasos de V13 (Catálogo `/catalog`) para probar-vista.mjs --modulo=catalogo --usuario=jefa1.
// Medición → editar el precio de un servicio y «Guardar» (POST 303: debe volver embebido y con el valor guardado)
// → «Lo atiendo» (POST 303) → se restaura el precio para no ensuciar la base.
const ESTADO = `(() => ({ ruta: location.pathname + location.search, embebido: document.body.classList.contains("embebido"), menu: Boolean(document.querySelector(".sidebar")),
  scroll_horizontal: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  rotulos_vacios: [...document.querySelectorAll(".svc-row td")].filter((td) => !td.dataset.label).length }))()`;
export async function pasos({ evaluar, fotografiar, pausa, hasta, medido, fallos }) {
  const anotar = (c, t) => { if (!c) fallos.push(t); };
  medido.v13 = {};
  const ver = async (n) => { const e = (medido.v13[n] = await evaluar(ESTADO)); anotar(e.embebido && !e.menu, `${n}: salió del modo embebido`); anotar(e.scroll_horizontal <= 0, `${n}: scroll horizontal ${e.scroll_horizontal}px`); anotar(e.rotulos_vacios === 0, `${n}: celdas sin rótulo`); return e; };
  await hasta(() => evaluar(`location.pathname === "/catalog"`), 10000); await pausa(800);
  await ver("lista");
  const primero = `document.querySelector(".svc-row input[name=price_info]")`;
  const original = await evaluar(`${primero}.value`);
  const nuevo = "$777 V13";
  await evaluar(`(() => { const i = ${primero}; i.value = ${JSON.stringify(nuevo)}; i.dispatchEvent(new Event("input", { bubbles: true })); })()`);
  await pausa(200);
  anotar(await evaluar(`!document.querySelector(".dirty-tag").hidden`), "no apareció «sin guardar» al editar");
  await fotografiar("1-editado");
  await evaluar(`document.querySelector(".svc-row .col-actions button[type=submit]").click()`);
  await hasta(() => evaluar(`${primero}.value === ${JSON.stringify(nuevo)}`), 8000); await pausa(600);
  await ver("guardado");
  anotar((await evaluar(`${primero}.value`)) === nuevo, "el precio no se guardó");
  // «Lo atiendo» (fija el estado; otro POST 303).
  const antes = await evaluar(`document.querySelector(".svc-row .col-asesores button").getAttribute("aria-pressed")`);
  await evaluar(`document.querySelector(".svc-row .col-asesores button").click()`);
  await hasta(() => evaluar(`document.querySelector(".svc-row .col-asesores button").getAttribute("aria-pressed") !== ${JSON.stringify(antes)}`), 8000); await pausa(500);
  await ver("atencion");
  await fotografiar("2-atencion");
  // Deshacer: dejar la atención y el precio como estaban.
  await evaluar(`document.querySelector(".svc-row .col-asesores button").click()`);
  await hasta(() => evaluar(`document.querySelector(".svc-row .col-asesores button").getAttribute("aria-pressed") === ${JSON.stringify(antes)}`), 8000); await pausa(400);
  await evaluar(`(() => { const i = ${primero}; i.value = ${JSON.stringify(original)}; })()`);
  await evaluar(`document.querySelector(".svc-row .col-actions button[type=submit]").click()`);
  await hasta(() => evaluar(`${primero}.value === ${JSON.stringify(original)}`), 8000);
  await ver("restaurado");
}
