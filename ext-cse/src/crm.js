// El CRM es el único sitio que declara el manifiesto, y el código lo toma de ahí: no conoce más
// dominio que el que el navegador le enseña a quien instala la extensión (construir.py --crm).
// El origen exacto va en homepage_url: en Firefox el permiso de sitio no lleva puerto.
const { homepage_url: inicio, host_permissions: sitios } = chrome.runtime.getManifest();

export const CRM = new URL(inicio);
export const PANEL = new URL("crm", CRM).href;
// Pública y sin sesión: dice si el bot contesta antes de abrirle el panel.
export const SALUD = new URL("health", CRM).href;
// El permiso de sitio que la persona puede retirarle a la extensión (Chrome y Firefox lo dejan).
export const PERMISO = { origins: sitios };
