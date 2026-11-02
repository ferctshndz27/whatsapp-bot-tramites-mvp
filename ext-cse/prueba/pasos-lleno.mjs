// Mide cómo se acomoda crm-layout con el tablero lleno. Uso: CRM_QA_MUCHOS=1 node ext-cse/prueba/probar-vista.mjs --modulo=whatsapp --pasos=ext-cse/prueba/pasos-lleno.mjs
export async function pasos({ evaluar, medido, fotografiar, pausa }) {
  await pausa(1500);
  medido.lleno = await evaluar(`(() => {
    const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return { arriba: Math.round(b.top), alto: Math.round(b.height), ancho: Math.round(b.width), scroll_y: e.scrollHeight - e.clientHeight, scroll_x: e.scrollWidth - e.clientWidth }; };
    return { ventana: [innerWidth, innerHeight], pagina_scroll_y: document.documentElement.scrollHeight - innerHeight,
      toolbar: r(".crm-toolbar"), tabs: r("#crm-tabs"), miembros: r("#crm-miembros"), layout: r(".crm-layout"), tray: r("#crm-tray"), board: r("#crm-board"),
      tarjetas: document.querySelectorAll("#crm-board .card").length, columnas: document.querySelectorAll("#crm-board .column, #crm-board [data-col]").length,
      bandeja: document.querySelectorAll("#crm-tray-list .tray-item").length, chips: document.querySelectorAll("#crm-miembros > *").length };
  })()`);
  await fotografiar("lleno");
}
