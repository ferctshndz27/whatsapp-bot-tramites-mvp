// «Chispa»: los botones de la extensión en el pie del chat de WhatsApp Web (junto a la caja de «Escribe
// un mensaje»), como los íconos que MI CIERRO pone ahí (UI-MAPA.md §4 y §9: `FooterIconsRight`, anclado
// a `footer > div` y vuelto a montar con un MutationObserver si WhatsApp reconstruye el pie). Cada botón
// abre un módulo del CRM en un panel pegado al borde derecho, como el `MenuLateral` de MI CIERRO:
// WhatsApp se corre a la izquierda (padding de `#app`) y no hay fondo oscuro.
//
// Nombre clave: «chispa». Todo lo nuestro lo lleva: `#ext-cse-chispa` (el grupo de botones),
// `#ext-cse-chispa-panel` (el panel) y `data-chispa="<id>"` en cada botón.
//
// No lee ni toca nada de WhatsApp: solo agrega un nodo suyo al pie y un padding. Su CSS vive en un
// shadow DOM cerrado. Los íconos son texto estático de esta extensión.
import { ICONOS } from "./iconos.js";

export const CLAVE = "chispa";
const ID_GRUPO = `ext-cse-${CLAVE}`;
const ID_PANEL = `ext-cse-${CLAVE}-panel`;
const ANCHO = 420;

// Qué abre cada botón (id de barra.js). Para quitar o agregar uno basta tocar esta lista.
const BOTONES = [
  { id: "solicitudes", nombre: "Solicitudes" },
  { id: "nuevocobro", nombre: "Nuevo cobro" },
  { id: "cartera", nombre: "Mi cartera" },
];

const ESTILO_GRUPO = `
  :host { all: initial; display: flex; align-items: center; flex: 0 0 auto; }
  button { all: unset; box-sizing: border-box; display: grid; place-items: center; width: 40px; height: 40px;
           border-radius: 50%; cursor: pointer; color: #54656f; }
  button:hover { background: rgba(11, 20, 26, .08); }
  button:focus-visible { outline: 2px solid #00a884; outline-offset: -2px; }
  button[aria-pressed="true"] { color: #fff; background: #00a884; }
  svg, img { width: 24px; height: 24px; }`;

const ESTILO_PANEL = `
  :host { all: initial; }
  section { position: fixed; top: 0; right: 0; bottom: 0; width: ${ANCHO}px; z-index: 2147483645; display: flex; flex-direction: column;
            background: #fff; border-left: 1px solid #d1d7db; font: 14px system-ui, sans-serif; }
  section[hidden] { display: none; }
  header { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; height: 59px; padding: 0 16px; background: #f0f2f5; color: #111b21; }
  h2 { all: unset; flex: 1; font: 500 16px system-ui, sans-serif; }
  button { all: unset; width: 32px; height: 32px; line-height: 32px; text-align: center; border-radius: 50%; cursor: pointer; font-size: 22px; color: #54656f; }
  button:hover, button:focus-visible { background: rgba(11, 20, 26, .1); }
  iframe { flex: 1; width: 100%; border: 0; background: #fff; }`;

const pedirModulos = () => chrome.runtime.sendMessage({ tipo: "modulos" }).catch(() => undefined);

function hoja(css) {
  const h = new CSSStyleSheet(); // no cuenta como estilo en línea: la CSP de WhatsApp no la bloquea
  h.replaceSync(css);
  return h;
}

// El panel: se crea una vez. `abrir(boton)` carga la vista; el mismo botón otra vez la cierra.
function crearPanel() {
  const anfitrion = document.createElement("div");
  anfitrion.id = ID_PANEL;
  const sombra = anfitrion.attachShadow({ mode: "closed" });
  sombra.adoptedStyleSheets = [hoja(ESTILO_PANEL)];
  const seccion = document.createElement("section");
  seccion.hidden = true;
  seccion.setAttribute("role", "complementary");
  const cabecera = document.createElement("header");
  const titulo = document.createElement("h2");
  const cerrar = document.createElement("button");
  cerrar.type = "button";
  cerrar.title = cerrar.ariaLabel = "Cerrar";
  cerrar.textContent = "×";
  cabecera.append(titulo, cerrar);
  seccion.append(cabecera);
  sombra.append(seccion);

  const app = () => document.querySelector("#app") ?? document.body;
  let abierto;
  const avisar = () => document.dispatchEvent(new CustomEvent(`${ID_GRUPO}-cambio`));
  const api = {
    anfitrion,
    get abierto() {
      return abierto;
    },
    cerrar() {
      seccion.hidden = true;
      seccion.querySelector("iframe")?.remove();
      app().style.paddingRight = "";
      abierto = undefined;
      avisar();
    },
    abrir(boton) {
      if (abierto === boton.id) return api.cerrar();
      seccion.querySelector("iframe")?.remove();
      const marco = document.createElement("iframe");
      marco.title = boton.nombre;
      marco.allow = "clipboard-write";
      marco.src = `${chrome.runtime.getURL("panel.html")}#solo=${encodeURIComponent(boton.id)}`;
      titulo.textContent = boton.nombre;
      seccion.append(marco);
      seccion.hidden = false;
      app().style.paddingRight = `${ANCHO}px`; // WhatsApp se corre, como #app en MI CIERRO
      app().style.boxSizing = "border-box";
      abierto = boton.id;
      avisar();
    },
  };
  cerrar.addEventListener("click", api.cerrar);
  // Escape cierra el panel sin llegar a WhatsApp (que con Escape cierra el chat).
  document.addEventListener(
    "keydown",
    (e) => {
      if (e.key !== "Escape" || seccion.hidden) return;
      e.stopPropagation();
      api.cerrar();
    },
    true,
  );
  return api;
}

// El grupo de botones que va dentro del pie.
function crearGrupo(panel, permitidos) {
  const anfitrion = document.createElement("div");
  anfitrion.id = ID_GRUPO;
  const sombra = anfitrion.attachShadow({ mode: "closed" });
  sombra.adoptedStyleSheets = [hoja(ESTILO_GRUPO)];
  // Sin sesión del CRM queda un solo botón, que lleva a entrar; con sesión, los que ese rol puede abrir.
  const lista = permitidos ? BOTONES.filter((b) => permitidos.includes(b.id)) : BOTONES.slice(0, 1);
  const botones = [];
  for (const def of lista) {
    const nombre = permitidos ? def.nombre : "Abrir Trámites";
    const boton = document.createElement("button");
    boton.type = "button";
    boton.dataset.chispa = def.id;
    boton.title = boton.ariaLabel = nombre;
    boton.setAttribute("aria-pressed", "false");
    boton.insertAdjacentHTML("beforeend", ICONOS[def.id]);
    boton.addEventListener("click", () => panel.abrir({ ...def, nombre }));
    sombra.append(boton);
    botones.push([def.id, boton]);
  }
  const marcar = () => botones.forEach(([id, b]) => b.setAttribute("aria-pressed", String(panel.abierto === id)));
  document.addEventListener(`${ID_GRUPO}-cambio`, marcar);
  return anfitrion;
}

// La fila del pie de WhatsApp: `footer > div`. Solo se usan la etiqueta `footer` y el campo
// `contenteditable`, no las clases (que WhatsApp cambia).
function filaDelPie() {
  const pie = document.querySelector("footer");
  if (!pie?.querySelector("div[contenteditable=true]")) return null;
  return pie.querySelector(":scope > div") ?? pie;
}

export async function montar() {
  if (document.getElementById(ID_PANEL)) return;
  const panel = crearPanel();
  document.documentElement.append(panel.anfitrion);
  let permitidos = (await pedirModulos())?.modulos ?? null;
  let nodo;
  const montarGrupo = () => {
    const fila = filaDelPie();
    if (!fila || (nodo?.isConnected && fila.contains(nodo))) return;
    nodo?.remove();
    nodo = crearGrupo(panel, permitidos);
    fila.append(nodo);
  };
  // WhatsApp reconstruye el pie al cambiar de chat: se vuelve a montar.
  new MutationObserver(montarGrupo).observe(document.body, { childList: true, subtree: true });
  montarGrupo();
  // Alguien entró o salió en el panel: los botones cambian.
  chrome.runtime.onMessage.addListener(async (m) => {
    if (m?.tipo !== "sesion-cambio") return;
    permitidos = (await pedirModulos())?.modulos ?? null;
    nodo?.remove();
    nodo = undefined;
    montarGrupo();
  });
}
