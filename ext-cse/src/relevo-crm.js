// Content script del CRM, solo dentro del marco del panel de WhatsApp Web. El agente (whatsapp-agente.js)
// necesita llamar al CRM con la sesión de la persona, y esa cookie está particionada: solo la ve una página del
// propio CRM dentro de ese marco. Este script es esa página. Hace UNA cosa: reenviar el mensaje del cliente a
// `/crm/api/agente/entrante` (mismo origen) y devolver lo que conteste. Sin sesión (403) no responde nada: otro
// marco del CRM (o el aviso de «sin conexión») lo resuelve.
if (window !== window.top) {
  chrome.runtime.onMessage.addListener((mensaje, _remitente, responder) => {
    if (mensaje?.tipo !== "agente-consulta") return false;
    fetch("/crm/api/agente/entrante", {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ phone: mensaje.phone, text: mensaje.text, wa_id: mensaje.wa_id }),
    })
      .then(async (r) => (r.ok ? responder(await r.json()) : undefined))
      .catch(() => undefined);
    return true;
  });
}
