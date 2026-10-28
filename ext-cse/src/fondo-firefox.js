// Fondo de la extensión en Firefox. Aquí el CRM no va en un marco: Firefox no deja enmarcarlo ni
// con regla de cabeceras (spike S1), así que el panel lateral ES la página del CRM
// (sidebarAction.setPanel). Por eso este manifiesto no pide ningún permiso.
import { PANEL } from "./crm.js";

function fijarPanel() {
  browser.sidebarAction
    .setPanel({ panel: PANEL })
    .catch((error) => console.error("ext-cse: no pude fijar el panel", error));
}

browser.runtime.onInstalled.addListener(fijarPanel);
browser.runtime.onStartup.addListener(fijarPanel);

// El ícono de la barra abre y cierra el panel, como en Chrome. toggle() debe llamarse dentro del
// clic mismo, sin esperar nada antes.
browser.action.onClicked.addListener(() => browser.sidebarAction.toggle());
