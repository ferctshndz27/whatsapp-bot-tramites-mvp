// «Chispa»: los botones de la extensión en el chat de WhatsApp Web. Cada botón
// abre un módulo del CRM en un panel pegado al borde derecho, como el `MenuLateral` de MI CIERRO:
// WhatsApp se corre a la izquierda (padding de `#app`) y no hay fondo oscuro.
//
// Nombre clave: «chispa». Todo lo nuestro lo lleva: `#ext-cse-chispa` (el grupo de botones),
// `#ext-cse-chispa-panel` (el panel) y `data-chispa="<id>"` en cada botón.
//
// Además, el menú flotante `MenuVertical` de MI CIERRO: un círculo con el logo (interruptor) sobre la
// caja de texto, a la izquierda del chat, que despliega hacia arriba 6 botones de 36 px (en MI CIERRO:
// absolute bottom-4 left-4, flex-col-reverse, gap 8). Aquí: `#ext-cse-chispa-menu`, `data-chispa-menu`.
// Sus 6 botones abren las vistas del CRM ya hechas, en el mismo panel derecho.
//
// No lee ni toca nada de WhatsApp: solo agrega nodos suyos y un padding. Su CSS vive en un
// shadow DOM cerrado. Los íconos son texto estático de esta extensión.
import { MODULOS } from "./barra.js";
import { ICONOS } from "./iconos.js";

export const CLAVE = "chispa";
const ID_GRUPO = `ext-cse-${CLAVE}`;
const ID_PANEL = `ext-cse-${CLAVE}-panel`;
const ID_MENU = `ext-cse-${CLAVE}-menu`;
const ANCHO = 420; // ancho inicial; la persona lo cambia arrastrando el borde izquierdo del panel
const MIN_ANCHO = 320;
const maxAncho = () => Math.max(MIN_ANCHO, Math.floor(innerWidth * 0.7));

// Los módulos del CRM que ya tienen vista (`listo` en barra.js, la misma lista del panel de la extensión).
// No depende de la sesión: dentro de WhatsApp Web el panel va en un marco con su propia cookie (particionada),
// que el fondo no ve; el panel mismo pide el acceso y abre el módulo elegido al entrar.
const MENU = MODULOS.filter((m) => m.listo);
const LOGO =
  '<svg viewBox="0 0 24 24" width="22" height="22" fill="currentColor" aria-hidden="true" focusable="false"><path d="M13 2 4 14h6l-1 8 9-12h-6z"/></svg>';

const ESTILO_MENU = `
  :host { all: initial; }
  ul { all: unset; position: fixed; z-index: 2147483644; display: flex; flex-direction: column-reverse; align-items: center; gap: 8px; }
  li, ol { all: unset; display: block; }
  button { all: unset; box-sizing: border-box; display: grid; place-items: center; cursor: pointer; background: #fff; color: #565f1d;
           box-shadow: 0 4px 10px rgba(0, 0, 0, .25); transition: transform .15s, border-radius .15s; }
  button:hover { transform: scale(1.08); }
  button:focus-visible { outline: 2px solid #565f1d; outline-offset: 2px; }
  button:active { transform: scale(.95); }
  .accion { width: 36px; height: 36px; border-radius: 10px; }
  .accion:hover { border-radius: 50%; }
  .accion[aria-pressed="true"] { background: #dcf24c; }
  .accion svg, .accion img { width: 20px; height: 20px; }
  .interruptor { width: 40px; height: 40px; border-radius: 50%; background: #dcf24c; color: #22242a; }
  ul[data-cerrado] .acciones { display: none !important; }
  /* Los botones suben y aparecen uno tras otro al desplegar, como en MI CIERRO (transición de 300 ms). */
  .acciones { display: flex; flex-direction: column-reverse; align-items: center; gap: 8px; padding: 6px; margin: -6px; overflow-y: auto;
              overflow-x: hidden; scrollbar-width: none; }
  .acciones li { animation: sube .3s ease-out backwards; animation-delay: calc(var(--n) * 35ms); }
  @keyframes sube { from { opacity: 0; transform: translateY(8px); } }
  .interruptor { position: relative; transition: transform .3s, box-shadow .3s; }
  .interruptor:hover { transform: scale(1.05); box-shadow: 0 10px 20px rgba(0, 0, 0, .3); }
  .interruptor::after { content: ""; position: absolute; top: -2px; right: -2px; width: 8px; height: 8px; border-radius: 50%;
                        background: #565f1d; opacity: 0; transition: opacity .3s; animation: latido 2s ease-in-out infinite; }
  .interruptor:hover::after { opacity: 1; }
  @keyframes latido { 50% { opacity: .35; } }
  .accion { transition: transform .2s, border-radius .2s, box-shadow .2s; }
  .accion:hover { box-shadow: 0 8px 16px rgba(0, 0, 0, .3); }
  @media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }`;

const ESTILO_PANEL = `
  :host { all: initial; }
  section { position: fixed; top: 0; right: 0; bottom: 0; width: var(--ancho, ${ANCHO}px); z-index: 2147483645; display: flex; flex-direction: column;
            background: #fff; border-left: 1px solid #d1d7db; font: 14px system-ui, sans-serif; }
  section[hidden] { display: none; }
  /* En el flujo de WhatsApp (como MenuLateral de MI CIERRO): es una columna más, a la derecha del chat. */
  :host([data-enflujo]) section { position: relative; width: auto; height: 100%; z-index: auto; }
  header { flex: 0 0 auto; display: flex; align-items: center; gap: 8px; height: 59px; padding: 0 16px; background: #f0f2f5; color: #111b21; }
  h2 { all: unset; flex: 1; font: 500 16px system-ui, sans-serif; }
  button { all: unset; width: 32px; height: 32px; line-height: 32px; text-align: center; border-radius: 50%; cursor: pointer; font-size: 22px; color: #54656f; }
  button:hover, button:focus-visible { background: rgba(11, 20, 26, .1); }
  iframe { flex: 1; width: 100%; border: 0; background: #fff; }
  section[data-arrastrando] iframe { pointer-events: none; }
  .asa { position: absolute; top: 0; bottom: 0; left: -3px; width: 7px; cursor: col-resize; touch-action: none; }
  .asa:hover, section[data-arrastrando] .asa { background: rgba(0, 168, 132, .45); }`;

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
  const asa = document.createElement("div");
  asa.className = "asa";
  asa.title = "Arrastra para ajustar el ancho";
  seccion.append(cabecera, asa);
  sombra.append(seccion);

  const app = () => document.querySelector("#app") ?? document.body;
  let abierto;
  // La columna del chat dentro de la fila de WhatsApp (`div.two`): el panel se inserta justo después, como
  // MI CIERRO inserta MenuLateral `afterend` de esa columna. Sin esa fila (WhatsApp cambió) queda fijo al borde.
  const columna = () => {
    const fila = document.querySelector("#main")?.closest(".two");
    let hijo = document.querySelector("#main");
    while (fila && hijo && hijo.parentElement !== fila) hijo = hijo.parentElement;
    return fila && hijo ? hijo : null;
  };
  const ubicar = () => {
    if (!abierto) return;
    const col = columna();
    const est = anfitrion.style;
    if (col) {
      if (col.nextElementSibling !== anfitrion) col.after(anfitrion);
      anfitrion.toggleAttribute("data-enflujo", true);
      est.display = "block";
      est.flex = `0 0 ${ancho}px`;
      est.width = `${ancho}px`;
      est.height = "100%";
      est.position = "relative";
      app().style.paddingRight = "";
    } else {
      if (anfitrion.parentElement !== document.documentElement) document.documentElement.append(anfitrion);
      anfitrion.removeAttribute("data-enflujo");
      est.cssText = "";
      app().style.paddingRight = `${ancho}px`;
      app().style.boxSizing = "border-box";
    }
  };
  const avisar = () => document.dispatchEvent(new CustomEvent(`${ID_GRUPO}-cambio`));
  let ancho = ANCHO;
  const ajustar = (px) => {
    ancho = Math.min(maxAncho(), Math.max(MIN_ANCHO, Math.round(px)));
    seccion.style.setProperty("--ancho", `${ancho}px`);
    ubicar(); // WhatsApp se acomoda al ancho elegido
  };
  // Los movimientos se escuchan en la ventana (no en el asa): el panel se mueve con el ratón y, con el
  // marco encima, el asa perdería los eventos. El marco no recibe el ratón mientras se arrastra.
  let derecha = innerWidth; // borde derecho del panel al empezar a arrastrar (no se mueve mientras se arrastra)
  const mover = (e) => ajustar(derecha - e.clientX);
  const soltar = () => {
    delete seccion.dataset.arrastrando;
    removeEventListener("pointermove", mover, true);
    removeEventListener("pointerup", soltar, true);
    removeEventListener("pointercancel", soltar, true);
  };
  asa.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    derecha = anfitrion.getBoundingClientRect().right;
    seccion.dataset.arrastrando = "";
    addEventListener("pointermove", mover, true);
    addEventListener("pointerup", soltar, true);
    addEventListener("pointercancel", soltar, true);
  });
  addEventListener("resize", () => ajustar(ancho)); // si la ventana se achica, el panel también
  const api = {
    anfitrion,
    ubicar,
    get abierto() {
      return abierto;
    },
    cerrar() {
      seccion.hidden = true;
      seccion.querySelector("iframe")?.remove();
      app().style.paddingRight = "";
      abierto = undefined;
      anfitrion.removeAttribute("data-enflujo");
      anfitrion.style.cssText = "";
      document.documentElement.append(anfitrion);
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
      abierto = boton.id;
      ubicar();
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

// El menú vertical: interruptor + botones. Se muestra sobre la caja de texto, a la izquierda del chat.
function crearMenu(panel, permitidos) {
  const anfitrion = document.createElement("div");
  anfitrion.id = ID_MENU;
  const sombra = anfitrion.attachShadow({ mode: "closed" });
  sombra.adoptedStyleSheets = [hoja(ESTILO_MENU)];
  const ul = document.createElement("ul");
  ul.setAttribute("aria-label", "Trámites");
  const fila = (...hijos) => {
    const li = document.createElement("li");
    li.append(...hijos);
    return li;
  };
  const interruptor = document.createElement("button");
  interruptor.type = "button";
  interruptor.className = "interruptor";
  interruptor.title = interruptor.ariaLabel = "Abrir/Cerrar Menú";
  interruptor.insertAdjacentHTML("beforeend", LOGO);
  ul.append(fila(interruptor));
  const lista = permitidos ? MENU.filter((b) => permitidos.includes(b.id)) : MENU;
  const botones = [];
  const acciones = document.createElement("ol");
  acciones.className = "acciones";
  // column-reverse: el primero del DOM queda abajo; se agrega la lista tal cual y la primera queda cerca
  // del interruptor.
  for (const [n, def] of lista.entries()) {
    const nombre = def.nombre;
    const boton = document.createElement("button");
    boton.type = "button";
    boton.className = "accion";
    boton.dataset.chispaMenu = def.id;
    boton.title = boton.ariaLabel = nombre;
    boton.setAttribute("aria-pressed", "false");
    boton.insertAdjacentHTML("beforeend", ICONOS[def.id]);
    boton.addEventListener("click", () => panel.abrir({ ...def, nombre }));
    const li = fila(boton);
    li.style.setProperty("--n", n);
    acciones.append(li);
    botones.push([def.id, boton]);
  }
  const contenedor = document.createElement("li");
  contenedor.append(acciones);
  ul.append(contenedor);
  interruptor.addEventListener("click", () => {
    if (ul.toggleAttribute("data-cerrado") && panel.abierto) panel.cerrar();
  });
  const marcar = () => botones.forEach(([id, b]) => b.setAttribute("aria-pressed", String(panel.abierto === id)));
  document.addEventListener(`${ID_GRUPO}-cambio`, marcar);
  sombra.append(ul);
  // Va pegado a la esquina inferior izquierda del chat, 16 px por encima del pie (bottom-4 left-4).
  anfitrion.colocar = () => {
    const pie = document.querySelector("footer")?.getBoundingClientRect();
    const chat = document.querySelector("#main")?.getBoundingClientRect();
    if (!pie || !chat) return;
    ul.style.left = `${Math.round(chat.left + 16)}px`;
    ul.style.bottom = `${Math.round(innerHeight - pie.top + 16)}px`;
    // Con muchos botones la columna se desplaza en vez de salirse de la ventana (deja libre la cabecera).
    acciones.style.maxHeight = `${Math.max(100, Math.round(pie.top - 16 - 40 - 8 - 60 - 12))}px`;
  };
  return anfitrion;
}

export async function montar() {
  // Si la extensión se recargó sin recargar WhatsApp, el script anterior sigue en la página con su contexto
  // invalidado (sus llamadas a chrome.runtime fallan con «Extension context invalidated»). Éste lo reemplaza:
  // quita sus nodos y el anterior se apaga solo al ver que ya no tiene contexto.
  document.getElementById(ID_PANEL)?.remove();
  document.getElementById(ID_MENU)?.remove();
  const panel = crearPanel();
  document.documentElement.append(panel.anfitrion);
  let permitidos = (await pedirModulos())?.modulos ?? null;
  let menu;
  const montarMenu = () => {
    if (!chrome.runtime?.id) return apagar(); // contexto invalidado: este script quedó huérfano
    const hayChat = !!document.querySelector("footer div[contenteditable=true]");
    if (hayChat && !menu?.isConnected) {
      menu = crearMenu(panel, permitidos);
      document.documentElement.append(menu);
    } else if (!hayChat && menu) {
      menu.remove(); // sin chat abierto no hay dónde ponerlo
      menu = undefined;
    }
    menu?.colocar();
    panel.ubicar(); // WhatsApp reconstruye su fila al cambiar de chat: el panel vuelve a su sitio
  };
  const alCambiarTamano = () => menu?.colocar();
  const observador = new MutationObserver(montarMenu);
  function apagar() {
    observador.disconnect();
    removeEventListener("resize", alCambiarTamano);
    menu?.remove();
    panel.anfitrion.remove();
  }
  addEventListener("resize", alCambiarTamano);
  // WhatsApp reconstruye el pie al cambiar de chat: se vuelve a colocar.
  observador.observe(document.body, { childList: true, subtree: true });
  montarMenu();
  // Alguien entró o salió en el panel: los botones cambian.
  chrome.runtime.onMessage.addListener(async (m) => {
    if (m?.tipo !== "sesion-cambio" || !chrome.runtime?.id) return;
    permitidos = (await pedirModulos())?.modulos ?? null;
    menu?.remove();
    menu = undefined;
    montarMenu();
  });
}
