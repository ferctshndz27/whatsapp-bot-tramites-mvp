// Pensado para colab1 (--usuario=colab1, el predeterminado): con jefa1 la cuenta de partida es otra porque ve más chats.
// Pasos del contador de «WhatsApp» en la barra (probar-vista.mjs --modulo=dia --pasos=ext-cse/prueba/pasos-pendientes.mjs).
// La siembra deja a Ana con 2 mensajes sin contestar y a colab1 como su asesor. Se simula que escribe otra vez
// y que un asesor contesta, y se comprueba que el globo del botón sigue a la base sin recargar el panel.

const LEER = `(() => {
  const b = document.querySelector('#barra button[data-id="whatsapp"]');
  const g = b?.querySelector(".pendientes");
  return b ? { globo: g ? g.textContent : null, etiqueta: b.getAttribute("aria-label"), rotulo: b.dataset.tip, estado: b.classList.contains("con-pendientes"),
               recortado: g ? (g.getBoundingClientRect().left < 0 || g.getBoundingClientRect().top < 0) : false } : null;
})()`;

export async function pasos({ evaluarPanel, escribirMensaje, pausa, hasta, medido, fallos, fotografiar }) {
  const anotar = (cond, texto) => { if (!cond) fallos.push(texto); };
  const refrescar = async () => { await evaluarPanel(`dispatchEvent(new Event("visibilitychange"))`); await pausa(900); };
  medido.pendientes = {};

  medido.pendientes.inicio = await hasta(async () => { const v = await evaluarPanel(LEER); return v?.globo ? v : null; }, 8000);
  anotar(medido.pendientes.inicio?.globo === "2", `al entrar, el globo debía decir 2 y dice ${medido.pendientes.inicio?.globo}`);
  anotar(medido.pendientes.inicio?.etiqueta === "WhatsApp: 2 mensajes sin atender", `etiqueta: ${medido.pendientes.inicio?.etiqueta}`);
  anotar(medido.pendientes.inicio?.estado, "el botón no cambió de estado (con-pendientes)");
  anotar(!medido.pendientes.inicio?.recortado, "el globo queda recortado por el borde de la barra");
  await fotografiar("pendientes-2");

  escribirMensaje("in", "¿Siguen ahí?");
  await refrescar();
  medido.pendientes.llega_otro = await evaluarPanel(LEER);
  anotar(medido.pendientes.llega_otro?.globo === "3", `llegó un mensaje y el globo dice ${medido.pendientes.llega_otro?.globo}, no 3`);

  escribirMensaje("out", "Aquí estamos");
  await refrescar();
  medido.pendientes.contestado = await evaluarPanel(LEER);
  anotar(medido.pendientes.contestado?.globo === null, `contestado y el globo sigue: ${medido.pendientes.contestado?.globo}`);
  anotar(medido.pendientes.contestado?.etiqueta === "WhatsApp" && !medido.pendientes.contestado?.estado, "sin pendientes el botón no volvió a su estado normal");

  escribirMensaje("in", "Gracias");
  await refrescar();
  medido.pendientes.uno = await evaluarPanel(LEER);
  anotar(medido.pendientes.uno?.globo === "1" && medido.pendientes.uno?.etiqueta === "WhatsApp: 1 mensaje sin atender", `con uno: ${JSON.stringify(medido.pendientes.uno)}`);
}
