// El CRM es el único host que declara el manifiesto, y el código lo toma de ahí: no conoce más
// dominio que el que el navegador le enseña a quien instala la extensión (construir.py --crm).
const [patron] = chrome.runtime.getManifest().host_permissions;

export const CRM = new URL(patron.replace(/\*$/, ""));
export const PANEL = new URL("crm", CRM).href;
