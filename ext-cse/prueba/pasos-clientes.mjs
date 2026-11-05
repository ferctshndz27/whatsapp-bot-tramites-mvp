// Pasos de V9 (Clientes `/clients` y ficha `/clients/{phone}`) para probar-vista.mjs --modulo=clientes.
// Lista → búsqueda → alta de un cliente (POST 303, debe volver embebido) → ficha → «Volver» → ficha desde un folio.
const DESBORDADOS = `(() => {
  const ancho = document.documentElement.clientWidth;
  return [...document.querySelectorAll(".main *")]
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
  const comprobar = async (nombre) => {
    const e = (medido.v9[nombre] = await evaluar(ESTADO)); e.desbordados = await evaluar(DESBORDADOS);
    anotar(e.embebido && !e.menu, `${nombre}: salió del modo embebido`);
    anotar(e.scroll_horizontal <= 0, `${nombre}: scroll horizontal de ${e.scroll_horizontal}px`);
    anotar(e.desbordados.length === 0, `${nombre}: se sale del marco ${e.desbordados.join(", ")}`);
    anotar(e.rotulos_vacios === 0, `${nombre}: celdas sin rótulo`);
    return e;
  };
  medido.v9 = {};
  await hasta(() => evaluar(`location.pathname === "/clients"`), 10000);
  await pausa(500);
  anotar((await comprobar("lista")).filas > 0, "la lista no trae clientes");
  await fotografiar("1-lista");

  // Búsqueda (GET): sigue embebida.
  await evaluar(`(() => { const q = document.getElementById("q"); q.value = "a"; q.form.submit(); })()`);
  await hasta(() => evaluar(`location.search.includes("q=")`), 8000); await pausa(500);
  await comprobar("busqueda");

  // Alta (POST → 303 → /clients?alta=…): el marco la sigue y no sale al ERP entero.
  await evaluar(`(() => { document.getElementById("alta-phone").value = "5512345678"; document.getElementById("alta-name").value = "Cliente de Prueba V9"; document.getElementById("alta-phone").form.submit(); })()`);
  await hasta(() => evaluar(`location.pathname === "/clients" && !location.search.includes("q=")`), 8000); await pausa(500);
  const alta = await comprobar("alta");
  anotar(await evaluar(`Boolean(document.querySelector(".banner-warn"))`), "el alta no mostró su aviso");
  await fotografiar("2-alta");

  // Ficha del cliente.
  await evaluar(`document.querySelector("tr[data-href] a").click()`);
  await hasta(() => evaluar(`location.pathname.startsWith("/clients/")`), 8000); await pausa(600);
  await comprobar("ficha");
  await fotografiar("3-ficha");

  // «Volver a clientes» regresa a la lista embebida.
  await evaluar(`[...document.querySelectorAll("a.btn-nav")].find((a) => /Volver/.test(a.textContent)).click()`);
  await hasta(() => evaluar(`location.pathname === "/clients"`), 8000); await pausa(500);
  anotar((await comprobar("vuelta")).filas > 0, "«Volver» no regresó a la lista");

  // Ficha con historial de solicitudes (cliente sembrado con folios): la tabla pasa a tarjetas.
  await evaluar(`location.href = "/clients/525500000011"`);
  await hasta(() => evaluar(`location.pathname === "/clients/525500000011"`), 8000); await pausa(600);
  await comprobar("ficha_con_historial");
  anotar((await evaluar(`document.querySelectorAll(".tabla-tarjetas tr[data-href]").length`)) > 0, "la ficha de prueba no trae historial de solicitudes");
  await fotografiar("4-historial");
}
