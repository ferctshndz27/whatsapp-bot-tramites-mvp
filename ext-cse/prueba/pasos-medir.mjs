// Medición genérica de una vista en el marco: qué se sale del ancho (fuera de contenedores con su propio scroll
// lateral), qué contenedores scrollean de lado y cuánto mide la página. Para vistas sin recorrido propio.
export async function pasos({ evaluar, fotografiar, pausa, medido, fallos }) {
  await pausa(1500);
  medido.medida = await evaluar(`(() => {
    const ancho = document.documentElement.clientWidth;
    const fuera = [...document.querySelectorAll(".main *")]
      .filter((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.right > ancho + 1; })
      .filter((e) => { for (let p = e.parentElement; p && p !== document.body; p = p.parentElement) { const o = getComputedStyle(p).overflowX; if (o === "auto" || o === "scroll" || o === "hidden") return false; } return true; })
      .slice(0, 8).map((e) => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + "." + String(e.className.baseVal ?? e.className).split(" ")[0]);
    const scrollean = [...document.querySelectorAll(".main *")].filter((e) => e.scrollWidth > e.clientWidth + 1 && ["auto","scroll"].includes(getComputedStyle(e).overflowX))
      .map((e) => (e.id ? "#" + e.id : e.tagName.toLowerCase() + "." + String(e.className).split(" ")[0]) + ":" + (e.scrollWidth - e.clientWidth));
    return { ruta: location.pathname, ancho, alto_pagina: document.documentElement.scrollHeight, h1: document.querySelector("h1")?.textContent.trim(), fuera, scrollean, svg: [...document.querySelectorAll("svg")].filter((s) => s.getBoundingClientRect().width > ancho + 1).length };
  })()`);
  if (medido.medida.fuera.length) fallos.push("se sale del marco: " + medido.medida.fuera.join(", "));
  await fotografiar("medida");
  // Segunda foto: lo que hay más abajo (la primera gráfica o tabla), porque la captura solo cubre el marco visible.
  await evaluar(`(document.querySelector(".line-wrap, .table-wrap, .catalog-grid") || document.body).scrollIntoView()`);
  await pausa(300);
  await fotografiar("abajo");
}
