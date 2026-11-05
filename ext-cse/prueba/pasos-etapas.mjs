// Mide las pestañas de etapa (#crm-tabs-list): columnas por fila, filas, desbordes.
export async function pasos({ evaluar, medido, pausa, fotografiar }) {
  await pausa(1500);
  medido.etapas = await evaluar(`(() => {
    const l = document.querySelector("#crm-tabs-list"), t = [...l.querySelectorAll(".tab")];
    const tops = [...new Set(t.map((e) => Math.round(e.getBoundingClientRect().top)))];
    const porFila = tops.map((y) => t.filter((e) => Math.round(e.getBoundingClientRect().top) === y).length);
    const rl = l.getBoundingClientRect();
    return { ventana: [innerWidth, innerHeight], pestanas: t.length, filas: tops.length, por_fila: porFila, lista: { alto: Math.round(rl.height), ancho: Math.round(rl.width), scroll_x: l.scrollWidth - l.clientWidth },
      anchos: t.slice(0,4).map((e) => Math.round(e.getBoundingClientRect().width)), desbordadas: t.filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.textContent.trim()),
      textos: t.map((e) => e.textContent.replace(/\\s+/g, " ").trim()) };
  })()`);
  await fotografiar("tabs");
}
