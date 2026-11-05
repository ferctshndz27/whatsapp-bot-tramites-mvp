// Lo que los fondos de Chrome y Firefox comparten: hablar con la barra de WhatsApp Web y con el panel.
//  - «modulos»: la barra pregunta qué módulos puede abrir la sesión (el fondo consulta al CRM; la
//    página de WhatsApp no puede, por CORS).
//  - «sesion-cambio»: el panel avisa que alguien entró o salió; las barras vuelven a pedir sus módulos.
import { SESION } from "./crm.js";

const WHATSAPP = "https://web.whatsapp.com/*";

export function registrarMensajes() {
  chrome.runtime.onMessage.addListener((mensaje, _remitente, responder) => {
    switch (mensaje?.tipo) {
      case "modulos":
        fetch(SESION, { cache: "no-store", credentials: "include" })
          .then(async (r) => responder({ modulos: r.ok ? ((await r.json()).modulos ?? null) : null }))
          .catch(() => responder({ modulos: null }));
        return true;
      case "sesion-cambio":
        chrome.tabs.query({ url: WHATSAPP }).then((tabs) => {
          for (const t of tabs) chrome.tabs.sendMessage(t.id, { tipo: "sesion-cambio" }).catch(() => undefined);
        });
        return false;
      case "agente-entrante": {
        // El agente (página de WhatsApp) pide una respuesta: se la pasa al script del CRM que vive en el marco
        // del panel de ESA pestaña (relevo-crm.js), el único con la cookie de sesión. Sin panel o sin sesión
        // nadie contesta: a los 15 s se responde «nada» y el agente lo reintenta.
        const tab = _remitente?.tab?.id;
        if (tab === undefined) return false;
        const { phone, text, wa_id } = mensaje;
        const espera = new Promise((r) => setTimeout(() => r({}), 15000));
        const consulta = chrome.tabs.sendMessage(tab, { tipo: "agente-consulta", phone, text, wa_id }).catch(() => ({}));
        Promise.race([consulta, espera]).then((r) => responder(r ?? {}));
        return true;
      }
      default:
        return false;
    }
  });
}
