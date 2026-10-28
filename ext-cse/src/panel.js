// Panel lateral: muestra el CRM y nada más.
//
// Chrome lo mete en un marco, que la regla de fondo-chrome.js deja pasar. Firefox no deja
// enmarcarlo, así que ahí el panel se cambia por la página del CRM misma.
import { PANEL } from "./crm.js";

if (globalThis.browser?.sidebarAction) {
  browser.sidebarAction.setPanel({ panel: PANEL });
} else {
  const marco = document.createElement("iframe");
  marco.src = PANEL;
  marco.title = "CRM de Trámites";
  document.body.replaceChildren(marco);
}
