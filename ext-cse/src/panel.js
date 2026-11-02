// Panel lateral: muestra el CRM y, si no puede, dice por qué y vuelve a intentarlo solo.
//
// Antes de abrir el CRM comprueba que haya red (navigator.onLine), que la extensión conserve su
// permiso de sitio (la persona puede retirarlo) y que el bot conteste (/health).
// Chrome mete el CRM en un marco, que la regla de fondo-chrome.js deja pasar; el panel le pide al
// fondo que la asegure antes de crearlo, porque en el primer arranque puede abrirse antes de que
// onInstalled la ponga y un marco que sale antes que la regla queda bloqueado (chrome-error://).
// Firefox no deja enmarcar el CRM (spike S1): ahí el panel navega a la página del CRM misma.
// Sin sesión no hay nada que hacer aquí: el CRM muestra su propio login.
//
// Las fallas esperadas (sin red, sin permiso, CRM caído) no van a la consola: no son errores del
// código y la pantalla ya las explica.
import { CRM, PANEL, PERMISO, SALUD } from "./crm.js";

const REINTENTO_MS = 5000;
const ESPERA_MS = 8000;
const CADA = `Volveremos a intentar cada ${REINTENTO_MS / 1000} segundos.`;
const enFirefox = Boolean(globalThis.browser?.sidebarAction);

const titulo = document.querySelector("#titulo");
const detalle = document.querySelector("#detalle");
const reintentar = document.querySelector("#reintentar");
const darPermiso = document.querySelector("#dar-permiso");

let temporizador;
let ocupado = false;
let otraVez = false;
let abierto = false;

// Solo toca lo que cambió: el aviso es una región viva y un reintento que da el mismo resultado
// no debe volver a anunciarse cada 5 segundos.
function mostrar(problema) {
  if (titulo.textContent !== problema.titulo) titulo.textContent = problema.titulo;
  if (detalle.textContent !== problema.detalle) detalle.textContent = problema.detalle;
  document.title = `${problema.titulo} — Trámites`;
  reintentar.hidden = Boolean(problema.permiso);
  darPermiso.hidden = !problema.permiso;
}

// Qué impide abrir el CRM ahora mismo, o null si nada. `periodico`: se reintenta solo cada 5 s;
// sin red o sin permiso no se consulta en vano, porque el navegador avisa cuando vuelven.
async function impedimento() {
  if (!navigator.onLine) {
    return { titulo: "Sin conexión a internet", detalle: "El CRM se abrirá solo en cuanto vuelva la conexión." };
  }
  // Sin el permiso, la regla del marco no aplica (Chrome) y la consulta a /health no sale (los
  // dos): el panel diría que el CRM no contesta, y no es cierto.
  if (!(await chrome.permissions.contains(PERMISO))) {
    return {
      titulo: "Falta el permiso para conectarse con el CRM",
      detalle: `Da clic en «Dar permiso» y acepta el aviso del navegador para que la extensión pueda abrir ${CRM.host}.`,
      permiso: true,
    };
  }
  let respuesta;
  try {
    respuesta = await fetch(SALUD, { cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(ESPERA_MS) });
  } catch {
    return { titulo: "No se pudo conectar con el CRM", detalle: `${CRM.host} no responde. ${CADA}`, periodico: true };
  }
  if (!respuesta.ok) {
    return {
      titulo: "El CRM no está disponible",
      detalle: `${CRM.host} respondió con el error ${respuesta.status}; puede estar actualizándose. ${CADA}`,
      periodico: true,
    };
  }
  return null;
}

async function abrirEnMarco() {
  const respuesta = await chrome.runtime.sendMessage({ tipo: "asegurar-regla" });
  if (!respuesta?.ok) throw new Error(respuesta?.error || "el fondo de la extensión no contestó");
  const marco = document.createElement("iframe");
  marco.src = PANEL;
  marco.title = "CRM de Trámites";
  document.body.replaceChildren(marco);
}

async function intentar() {
  if (abierto) return;
  // Un aviso del navegador (volvió la red, volvió el permiso) que llega a media consulta no se
  // pierde: la consulta se repite al terminar, porque su resultado ya puede ser viejo.
  if (ocupado) {
    otraVez = true;
    return;
  }
  ocupado = true;
  reintentar.disabled = true;
  clearTimeout(temporizador);
  try {
    const problema = await impedimento();
    if (problema) {
      mostrar(problema);
      if (problema.periodico) temporizador = setTimeout(intentar, REINTENTO_MS);
      return;
    }
    abierto = true;
    if (enFirefox) location.replace(PANEL);
    else await abrirEnMarco();
  } catch (error) {
    abierto = false;
    mostrar({ titulo: "No se pudo preparar el panel", detalle: `${CADA} Detalle: ${error.message}` });
    temporizador = setTimeout(intentar, REINTENTO_MS);
  } finally {
    ocupado = false;
    reintentar.disabled = false;
    if (otraVez) {
      otraVez = false;
      intentar();
    }
  }
}

reintentar.addEventListener("click", intentar);
// Firefox solo deja pedir un permiso dentro del clic mismo, sin esperar nada antes. Si la persona
// lo da desde el aviso o desde el menú de extensiones, permissions.onAdded abre el CRM.
darPermiso.addEventListener("click", () => {
  chrome.permissions.request(PERMISO).catch((error) => console.error("ext-cse: no pude pedir el permiso", error));
});
chrome.permissions.onAdded.addListener(() => intentar());
// Sin el permiso, la regla del marco deja de aplicar y lo próximo que cargue el CRM saldría
// bloqueado: el panel vuelve a empezar y explica por qué.
chrome.permissions.onRemoved.addListener(() => location.reload());
addEventListener("online", intentar);
intentar();
