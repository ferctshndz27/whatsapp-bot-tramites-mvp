// Panel lateral: muestra la barra de módulos y, si el CRM no está a la mano, dice por qué y vuelve a
// intentarlo solo.
//
// Antes de mostrar la barra comprueba que haya red (navigator.onLine), que la extensión conserve su
// permiso de sitio (la persona puede retirarlo) y que el bot conteste (/health).
// No se carga el ERP entero al entrar: la vista de un módulo se pide solo al elegirlo. fondo-chrome.js
// asegura la regla del marco (Chrome); Firefox no deja enmarcar el CRM (spike S1, plan ext-vistas V0).
//
// Las fallas esperadas (sin red, sin permiso, CRM caído) no van a la consola: no son errores del
// código y la pantalla ya las explica.
import { CRM, PENDIENTES, PERMISO, SALIDA, SALUD, SESION } from "./crm.js";
import { marcarPendientes, pintarBarra } from "./barra.js";

// Módulo que se abre solo al entrar (id de barra.js).
const INICIAL = "dia";
const REINTENTO_MS = 5000;
const ESPERA_MS = 8000;
const CADA = `Volveremos a intentar cada ${REINTENTO_MS / 1000} segundos.`;

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

// Con el CRM a la vista no se carga su ERP completo: aparece la barra de módulos y, solo al elegir
// uno, el marco con SU vista (`?embed=1` no hace falta: el navegador marca el marco y el servidor
// lo reconoce, también en cada enlace y redirect de dentro).
const vista = document.querySelector("#vista");
const nombre = document.querySelector("#vista-titulo");
const texto = document.querySelector("#vista-detalle");
let marco;
let eleccion = 0;

function sinMarco(modulo, mensaje) {
  marco?.remove();
  marco = undefined;
  vista.classList.remove("con-marco");
  nombre.hidden = texto.hidden = false;
  nombre.textContent = modulo.nombre;
  texto.textContent = mensaje;
}

// La regla que deja enmarcar el CRM (Chrome) se asegura antes de crear un marco: uno que sale antes
// queda bloqueado (chrome-error://). Devuelve null si todo bien, o el motivo.
async function prepararMarcos() {
  const respuesta = await chrome.runtime.sendMessage({ tipo: "asegurar-regla" }).catch(() => undefined);
  return respuesta?.ok ? null : `${respuesta?.error || "el fondo de la extensión no contestó"}`;
}

async function abrirVista(modulo) {
  const esta = ++eleccion;
  document.title = `${modulo.nombre} — Trámites`;
  if (!modulo.listo) {
    sinMarco(modulo, `Aquí irá «${modulo.nombre}». Todavía no está conectada al CRM.`);
    return;
  }
  sinMarco(modulo, "Abriendo…");
  const fallo = await prepararMarcos();
  if (esta !== eleccion) return; // eligieron otro módulo mientras tanto
  if (fallo) {
    texto.textContent = `No se pudo preparar la vista: ${fallo}.`;
    return;
  }
  marco = document.createElement("iframe");
  marco.src = new URL(modulo.ruta, CRM).href;
  marco.title = modulo.nombre;
  // Sin esto «Copiar liga» falla dentro del marco (otro origen): el navegador niega el portapapeles.
  marco.allow = "clipboard-write";
  vista.append(marco);
  vista.classList.add("con-marco");
  nombre.hidden = texto.hidden = true;
}

const aviso = document.querySelector("#aviso");
const acceso = document.querySelector("#acceso");
const app = document.querySelector("#app");

// Contador de «WhatsApp»: se pregunta al entrar, cada 20 s y al volver a ver el panel. Un fallo de red no
// borra el número (sería un «ya no hay nada» falso); solo una respuesta del CRM lo actualiza. Si ya no hay
// sesión (401/403) se quita, que el aviso de otra persona no se quede pegado.
const CADA_PENDIENTES_MS = 20000;
let vigilia;

function dejarDeVigilar() {
  clearInterval(vigilia);
  vigilia = undefined;
}

async function preguntarPendientes() {
  let respuesta;
  try {
    respuesta = await fetch(PENDIENTES, { cache: "no-store", credentials: "include", signal: AbortSignal.timeout(ESPERA_MS) });
  } catch {
    return;
  }
  const barra = document.querySelector("#barra");
  if (!respuesta.ok) {
    if (respuesta.status === 401 || respuesta.status === 403) marcarPendientes(barra, "whatsapp", 0);
    return;
  }
  const { mensajes } = await respuesta.json().catch(() => ({}));
  marcarPendientes(barra, "whatsapp", mensajes);
}

function vigilarPendientes(modulos) {
  dejarDeVigilar();
  if (!modulos.includes("whatsapp")) return;
  preguntarPendientes();
  vigilia = setInterval(() => {
    if (!document.hidden) preguntarPendientes();
  }, CADA_PENDIENTES_MS);
}
addEventListener("visibilitychange", () => {
  if (vigilia && !document.hidden) preguntarPendientes();
});

function mostrarBarra(modulos) {
  acceso.replaceChildren();
  acceso.hidden = aviso.hidden = true;
  app.hidden = false;
  document.title = "Trámites";
  // Se pinta de nuevo en cada entrada: quien entra ahora puede ser otro usuario con otros módulos.
  const elegir = pintarBarra(document.querySelector("#barra"), abrirVista, modulos);
  vigilarPendientes(modulos);
  ++eleccion;
  // Al entrar no se deja el panel en blanco: se abre «Mi día», lo primero que mira quien trabaja. Si esta sesión no
  // lo tiene permitido, queda el aviso de elegir un módulo.
  if (modulos.includes(INICIAL)) elegir(INICIAL);
  else sinMarco({ nombre: "Trámites" }, "Elige un módulo de la barra.");
}

// ¿Hay sesión en el CRM? Devuelve los módulos que puede abrir, o null sin sesión. Solo 200 y 401 son
// respuestas; cualquier otra cosa es un problema del CRM.
async function haySesion() {
  const respuesta = await fetch(SESION, { cache: "no-store", credentials: "include", signal: AbortSignal.timeout(ESPERA_MS) });
  if (respuesta.status === 401) return null;
  if (!respuesta.ok) throw new Error(`/sesion respondió ${respuesta.status}`);
  const { modulos } = await respuesta.json();
  return Array.isArray(modulos) ? modulos : [];
}

// Sin sesión: el login del CRM ocupa el panel. Cada vez que el marco termina de cargar se vuelve a
// preguntar; al entrar, el login redirige y esa carga es la que trae la barra.
async function mostrarAcceso() {
  dejarDeVigilar();
  // La vista que quedara abierta es de una sesión que ya no existe: se quita, no solo se esconde.
  ++eleccion;
  sinMarco({ nombre: "Trámites" }, "Elige un módulo de la barra.");
  app.hidden = aviso.hidden = true;
  acceso.hidden = false;
  document.title = "Iniciar sesión — Trámites";
  const fallo = await prepararMarcos();
  if (fallo) {
    mostrar({ titulo: "No se pudo preparar el acceso", detalle: `${fallo}.` });
    aviso.hidden = false;
    acceso.hidden = true;
    return;
  }
  const login = document.createElement("iframe");
  login.src = new URL("login", CRM).href;
  login.title = "Iniciar sesión";
  login.addEventListener("load", async () => {
    try {
      const modulos = await haySesion();
      if (modulos) mostrarBarra(modulos);
    } catch {
      // El siguiente load lo vuelve a intentar; el marco ya explica lo que pase.
    }
  });
  acceso.replaceChildren(login);
}

async function entrar() {
  const modulos = await haySesion();
  if (modulos) mostrarBarra(modulos);
  else await mostrarAcceso();
}

document.querySelector("#salir").addEventListener("click", async () => {
  // /logout responde un redirect al login; fetch lo sigue y deja la cookie borrada.
  await fetch(SALIDA, { cache: "no-store", credentials: "include", signal: AbortSignal.timeout(ESPERA_MS) }).catch(() => undefined);
  await entrar().catch(() => mostrarAcceso());
});

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
    await entrar();
  } catch (error) {
    abierto = false;
    aviso.hidden = false;
    app.hidden = acceso.hidden = true;
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
