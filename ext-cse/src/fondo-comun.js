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
      default:
        return false;
    }
  });
}
