// El agente en el WhatsApp Web del colaborador. Cuando la persona lo enciende en un chat, lo que el cliente
// escriba se le pasa al mismo cerebro del bot (por el CRM, `POST /crm/api/agente/entrante`) y la respuesta se
// escribe en ese chat, con la pausa y los límites de `agente-cola.js` (mínimo 20 s, nunca ráfagas).
//
// Reglas, pensadas para que se parezca lo menos posible a un programa y para no pisar a la persona:
//  - Apagado por defecto y POR CHAT: solo contesta donde la persona lo encendió. Se apaga al recargar.
//  - Solo contesta mensajes NUEVOS de texto, en chats individuales; nunca el historial ni los grupos.
//  - Si la persona está escribiendo (la caja tiene texto) o cambió de chat, espera; no escribe sobre ella.
//  - Una sola entrada de texto y un clic en «enviar», con una pausa corta entre las dos; sin teclas falsas.
//
// La llamada al servidor la hace un script del propio CRM dentro del marco del panel (relevo-crm.js): es el
// único sitio con la cookie de sesión (particionada). Por eso solo funciona con el panel abierto y con sesión.
//
// Los selectores de WhatsApp Web viven juntos en SEL: si WhatsApp los cambia, es lo único que se toca.
import { crearCola } from "./agente-cola.js";

const SEL = {
  mensajes: '#main [data-id]',
  texto: "span.selectable-text, [data-testid='selectable-text']",
  caja: 'footer div[contenteditable="true"]',
};

// El botón «enviar» no tiene un atributo estable (en WhatsApp Web es un <button aria-label="Enviar"> sin data-icon, y
// el texto cambia con el idioma): es el último botón del pie, a la derecha de la caja, que reemplaza al del micrófono
// en cuanto hay texto. Se busca solo DESPUÉS de escribir, y se rechaza el del micrófono («data-icon» ptt/mic).
function botonEnviar() {
  const pie = document.querySelector("footer");
  const caja = pie?.querySelector('[contenteditable="true"]');
  if (!pie || !caja) return null;
  const despues = [...pie.querySelectorAll("button, [role=button]")].filter(
    (b) => !b.hasAttribute("data-chispa-agente") && caja.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING,
  );
  const boton = pie.querySelector('[data-icon="send"]')?.closest("button, [role=button]") ?? despues.at(-1);
  return boton && !boton.querySelector('[data-icon="ptt"], [data-icon="mic"]') ? boton : null;
}
// WhatsApp Web ya no pone el teléfono ni la dirección en el `data-id` de los mensajes (son ids hexadecimales):
//  - La dirección sale del lado de la burbuja (las propias van a la derecha) o de su «cola» (tail-in / tail-out).
//  - El chat se identifica por el nombre de la cabecera, y su teléfono lo da la persona una vez (se recuerda).
//  - En un grupo, la etiqueta de quien escribe («Nombre:») no coincide con la cabecera: ahí no se contesta.
const CABECERA = "#main header";
const DEBOUNCE_MS = 1500;
const REINTENTOS_CRM = 3;
const ESPERA_CRM_MS = 20000;
const ESCRIBIR_MIN_MS = 800;
const ESCRIBIR_AZAR_MS = 1200;

const dormir = (ms) => new Promise((r) => setTimeout(r, ms));
const azar = (min, extra) => min + Math.floor(Math.random() * (extra + 1));

function tituloDelChat() {
  const el = document.querySelector(`${CABECERA} span[title]`) ?? document.querySelector(`${CABECERA} span[dir="auto"]`);
  return (el?.getAttribute("title") || el?.textContent || "").trim() || null;
}

function telefonoDeLaCabecera() {
  const t = tituloDelChat();
  return t && /^\+?[\d\s().-]{10,20}$/.test(t) ? t.replace(/\D/g, "") : null;
}

// Cuando WhatsApp no da el número (casi siempre: el contacto está guardado), la persona lo escribe una vez
// y se recuerda por chat en este navegador.
const ALMACEN = "ext-cse-agente-telefonos";
const leerTelefonos = () => {
  try {
    return JSON.parse(localStorage.getItem(ALMACEN) || "{}");
  } catch {
    return {};
  }
};

/** Identifica el chat abierto: `clave` (el título de la cabecera) y `telefono` (o null si aún no se sabe). */
export function identidad() {
  const titulo = tituloDelChat();
  if (!titulo) return null;
  const clave = `t:${titulo}`;
  return { clave, telefono: telefonoDeLaCabecera() ?? leerTelefonos()[clave] ?? null };
}

/** El teléfono del chat individual abierto, o null. */
export const chatActual = () => identidad()?.telefono ?? null;

function guardarTelefono(clave, telefono) {
  const todos = leerTelefonos();
  todos[clave] = telefono;
  localStorage.setItem(ALMACEN, JSON.stringify(todos));
}

/** Mensajes de texto del cliente que hay en pantalla (sin los propios ni los de grupos). */
function entrantes() {
  const lista = [];
  const titulo = tituloDelChat();
  const caja = document.querySelector("#main")?.getBoundingClientRect();
  if (!titulo || !caja) return lista;
  for (const el of document.querySelectorAll(SEL.mensajes)) {
    const id = el.dataset.id ?? "";
    const burbuja = el.querySelector("[data-testid='msg-container']");
    if (!id || !burbuja) continue;
    const cola = el.querySelector("[data-icon^='tail-']")?.dataset.icon;
    const r = burbuja.getBoundingClientRect();
    const propio = cola ? cola === "tail-out" : r.left + r.width / 2 > caja.left + caja.width / 2;
    if (propio) continue;
    const quien = el.querySelector("span[aria-label$=':']")?.getAttribute("aria-label")?.replace(/:$/, "").trim();
    if (quien && quien !== titulo) continue; // grupo
    lista.push({ id, texto: el.querySelector(SEL.texto)?.innerText?.trim() || null });
  }
  return lista;
}

// Pone `texto` en la caja conservando los saltos de línea. El editor de WhatsApp (Lexical) conserva los
// renglones de un «pegado»; un insertLineBreak suelto a veces los pierde y el menú queda en una sola línea.
// Si el pegado no deja nada en la caja, se cae al método renglón por renglón.
function escribirTexto(caja, texto) {
  const dt = new DataTransfer();
  dt.setData("text/plain", texto);
  caja.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true, cancelable: true }));
  if (caja.innerText.trim() !== "") return;
  texto.split("\n").forEach((linea, i) => {
    if (i > 0) document.execCommand("insertLineBreak");
    if (linea) document.execCommand("insertText", false, linea);
  });
}

// Escribe `texto` en la caja del chat abierto y pulsa «enviar». Devuelve lo que la cola entiende.
async function escribirYEnviar({ chat, texto }) {
  if (chatActual() !== chat) return "reintentar"; // la persona está en otro chat
  const caja = document.querySelector(SEL.caja);
  if (!caja) return "reintentar";
  if (caja.innerText.trim() !== "") return "reintentar"; // la persona está escribiendo
  caja.focus();
  escribirTexto(caja, texto);
  await dormir(azar(ESCRIBIR_MIN_MS, ESCRIBIR_AZAR_MS));
  const boton = botonEnviar();
  if (!boton || chatActual() !== chat) {
    document.execCommand("selectAll");
    document.execCommand("delete"); // no deja el borrador en la caja de la persona
    return boton ? "reintentar" : "descartar";
  }
  boton.click();
  await dormir(1500);
  return caja.innerText.trim() === "" ? "ok" : "descartar";
}

export function crearAgente({ alCambiar = () => undefined } = {}) {
  const activos = new Map(); // chat → Set de ids de mensajes ya vistos
  const estados = new Map(); // chat → lo último que pasó ahí (se muestra junto al botón)
  const cambiar = (nuevo, chat = chatActual() ?? "sin-chat") => {
    estados.set(chat, nuevo);
    alCambiar();
  };
  const cola = crearCola({
    enviar: async (d) => {
      const r = await escribirYEnviar(d);
      if (r === "ok") cambiar(`Última respuesta enviada a las ${new Date().toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit" })}.`, d.chat);
      return r;
    },
    alDescartar: (item, motivo) => cambiar(`Se descartó una respuesta (${motivo}).`, item.chat ?? item.dato?.chat),
  });

  async function consultar(chat, texto, wa_id) {
    for (let i = 0; i < REINTENTOS_CRM; i++) {
      if (!chrome.runtime?.id) return null;
      const r = await chrome.runtime.sendMessage({ tipo: "agente-entrante", phone: chat, text: texto, wa_id }).catch(() => undefined);
      if (r?.respuestas) return r.respuestas;
      cambiar("Sin conexión con el CRM: abre el panel y entra con tu sesión.", chat);
      await dormir(ESPERA_CRM_MS);
    }
    return null;
  }

  let cadena = Promise.resolve(); // un mensaje a la vez, en orden
  function revisar() {
    const chat = chatActual();
    const vistos = chat && activos.get(chat);
    if (!vistos) return;
    for (const m of entrantes()) {
      if (vistos.has(m.id)) continue;
      vistos.add(m.id);
      if (!m.texto) continue; // fotos, notas de voz…: el agente solo lee texto
      cadena = cadena.then(async () => {
        if (!activos.has(chat)) return;
        cambiar("Mensaje nuevo: consultando al agente…", chat);
        const respuestas = await consultar(chat, m.texto, m.id);
        if (!respuestas) return;
        cambiar(respuestas.length ? "Respuesta lista: se escribirá en 20–30 s." : "El agente no tiene nada que responder (¿ya tomaste el folio?).", chat);
        for (const texto of respuestas) if (activos.has(chat)) cola.encolar({ chat, texto });
      });
    }
  }

  let reloj;
  const observador = new MutationObserver(() => {
    clearTimeout(reloj);
    reloj = setTimeout(revisar, DEBOUNCE_MS);
  });
  observador.observe(document.body, { childList: true, subtree: true });

  return {
    /** ¿Está encendido en el chat abierto? */
    activo: () => {
      const chat = chatActual();
      return !!chat && activos.has(chat);
    },
    get estado() {
      const base = estados.get(chatActual() ?? "sin-chat") ?? "";
      const n = entrantes().length;
      // Diagnóstico a la vista: si dice 0 con mensajes del cliente en pantalla, los selectores de WhatsApp cambiaron.
      return chatActual() && activos.has(chatActual()) ? `${base} (veo ${n} mensajes del cliente en pantalla)` : base;
    },
    /** Enciende o apaga el agente en el chat abierto. */
    alternar() {
      let chat = chatActual();
      if (!chat) {
        const id = identidad();
        if (!id) {
          cambiar("Abre un chat con un cliente para encender el agente.");
          return { ok: false };
        }
        // WhatsApp Web no da el número de este contacto: se pide una vez y se recuerda.
        const dado = (prompt("WhatsApp no muestra el número de este contacto.\nEscríbelo con lada (ej. 5217551234567):") ?? "").replace(/\D/g, "");
        if (dado.length < 10 || dado.length > 15) {
          cambiar("Sin número válido: el agente sigue apagado.");
          return { ok: false };
        }
        guardarTelefono(id.clave, dado);
        chat = dado;
      }
      if (activos.delete(chat)) {
        cola.quitar((d) => d.chat === chat);
        cambiar("Agente apagado en este chat.");
        return { ok: true, activo: false };
      }
      // Lo que ya está en pantalla es historial: no se contesta.
      activos.set(chat, new Set(entrantes().map((m) => m.id)));
      cambiar("Esperando mensajes nuevos del cliente.");
      return { ok: true, activo: true };
    },
    parar() {
      observador.disconnect();
      clearTimeout(reloj);
      activos.clear();
      cola.parar();
    },
  };
}
