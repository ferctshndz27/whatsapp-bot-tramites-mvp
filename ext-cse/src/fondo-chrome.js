// Fondo de la extensión en Chrome (service worker). Dos trabajos, y solo dos:
//
//  1. Que el ícono de la barra abra el CRM en la página de la pestaña, pegado a la derecha y con 1/3 del
//     ancho de la ventana (Chrome no deja fijar el ancho del panel lateral: lo decide quien lo arrastra).
//     Lo hace con activeTab: el permiso vale solo para la pestaña donde se hizo clic, solo ese momento, y
//     por eso el manifiesto no pide acceso a ningún otro sitio. Donde no se puede inyectar (chrome://,
//     la tienda de extensiones…) se abre el panel lateral de siempre.
//  2. Que el CRM se deje mostrar DENTRO del panel. El bot manda `X-Frame-Options: SAMEORIGIN` y
//     `frame-ancestors 'none'` para que nadie lo meta en un marco (clickjacking). La regla de
//     regla-marco.json (la arma construir.py) quita X-Frame-Options y pone la CSP del bot con un
//     solo cambio: frame-ancestors deja entrar a esta extensión y a nadie más. Solo aplica a
//     submarcos hacia el CRM que pide esta extensión o el propio CRM (el login redirige dentro
//     del marco); fuera del panel el CRM sigue sin poder enmarcarse.

const yo = chrome.runtime.id;

// Se ejecuta DENTRO de la página (executeScript la serializa): no puede usar nada de este archivo.
// Pone o quita el panel. Vive en un shadow DOM cerrado para que ni el CSS de la página lo deforme ni su
// JS lo toque; el marco es la página de la extensión (cross-origin para la página: no puede leerlo).
function alternarPanelEnPagina(urlPanel) {
  const ID = "ext-cse-panel";
  const raiz = document.documentElement;
  const previo = document.getElementById(ID);
  if (previo) {
    raiz.style.width = previo.dataset.anchoPrevio;
    raiz.style.boxSizing = previo.dataset.cajaPrevia;
    previo.remove();
    return "cerrado";
  }
  const anfitrion = document.createElement("div");
  anfitrion.id = ID;
  anfitrion.dataset.anchoPrevio = raiz.style.width;
  anfitrion.dataset.cajaPrevia = raiz.style.boxSizing;
  const sombra = anfitrion.attachShadow({ mode: "closed" });
  const estilo = document.createElement("style");
  estilo.textContent = `
    :host { all: initial; }
    aside { position: fixed; top: 0; right: 0; bottom: 0; width: calc(100vw / 3); min-width: 320px;
            z-index: 2147483647; display: flex; flex-direction: column; background: #fff;
            border-left: 1px solid #d0d7de; box-shadow: -2px 0 8px rgba(0,0,0,.12); font: 13px system-ui, sans-serif; }
    header { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between; height: 28px;
             padding: 0 4px 0 10px; background: #075e54; color: #fff; }
    button { all: unset; cursor: pointer; width: 24px; height: 24px; line-height: 24px; text-align: center;
             border-radius: 4px; font-size: 16px; }
    button:hover, button:focus-visible { background: rgba(255,255,255,.2); }
    iframe { flex: 1 1 auto; width: 100%; border: 0; background: #fff; }`;
  const caja = document.createElement("aside");
  const cabecera = document.createElement("header");
  const titulo = document.createElement("span");
  titulo.textContent = "Trámites — CRM";
  const cerrar = document.createElement("button");
  cerrar.type = "button";
  cerrar.title = "Cerrar el panel";
  cerrar.setAttribute("aria-label", "Cerrar el panel");
  cerrar.textContent = "×";
  cerrar.addEventListener("click", () => {
    raiz.style.width = anfitrion.dataset.anchoPrevio;
    raiz.style.boxSizing = anfitrion.dataset.cajaPrevia;
    anfitrion.remove();
  });
  cabecera.append(titulo, cerrar);
  const marco = document.createElement("iframe");
  marco.src = urlPanel;
  marco.title = "Trámites — CRM";
  caja.append(cabecera, marco);
  sombra.append(estilo, caja);
  // La página se encoge a los 2/3 que quedan: el panel no tapa nada. min() respeta el mínimo del panel.
  raiz.style.boxSizing = "border-box";
  raiz.style.width = "calc(100% - max(100vw / 3, 320px))";
  raiz.appendChild(anfitrion);
  return "abierto";
}

// Para las pruebas (prueba/probar-panel.mjs): llaman a esto desde el service worker; en uso real
// lo llama el clic del ícono. Devuelve "abierto", "cerrado" o "sin-permiso".
globalThis.alternarPanel = async function alternarPanel(tabId) {
  try {
    const [r] = await chrome.scripting.executeScript({
      target: { tabId },
      func: alternarPanelEnPagina,
      args: [chrome.runtime.getURL("panel.html")],
    });
    return r?.result ?? "sin-permiso";
  } catch (error) {
    console.warn("ext-cse: no se pudo poner el panel en la página", String(error));
    return "sin-permiso";
  }
};

chrome.action.onClicked.addListener(async (pestana) => {
  if ((await globalThis.alternarPanel(pestana.id)) !== "sin-permiso") return;
  // chrome://, la tienda, visores de PDF…: ahí no se puede inyectar; panel lateral como respaldo.
  chrome.sidePanel.open({ tabId: pestana.id }).catch((error) => console.error("ext-cse: no abre el panel", error));
});

async function instalarRegla() {
  const regla = await (await fetch(chrome.runtime.getURL("regla-marco.json"))).json();
  // El id no existe hasta instalar la extensión, así que se le suma aquí y no en construir.py.
  regla.condition.initiatorDomains.unshift(yo);
  const csp = regla.action.responseHeaders.find((c) => c.header === "content-security-policy");
  csp.value += `; frame-ancestors chrome-extension://${yo}`;

  // Quedan exactamente las reglas del paquete: se quitan todas las anteriores, también las de una
  // versión vieja, y el id de esta aunque no esté, para que dos instalaciones a la vez (onInstalled
  // y el panel en el primer arranque) no choquen por id repetido.
  const anteriores = await chrome.declarativeNetRequest.getDynamicRules();
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: [...new Set([regla.id, ...anteriores.map((r) => r.id)])],
    addRules: [regla],
  });
}

function alArrancar() {
  instalarRegla().catch((error) => console.error("ext-cse: el CRM no podrá mostrarse en el panel", error));
}

chrome.runtime.onInstalled.addListener(alArrancar);
chrome.runtime.onStartup.addListener(alArrancar);

// El panel la pide antes de crear el marco: en el primer arranque puede abrirse antes de que
// onInstalled termine, y un marco que sale antes que la regla queda bloqueado (chrome-error://).
chrome.runtime.onMessage.addListener((mensaje, _remitente, responder) => {
  if (mensaje?.tipo !== "asegurar-regla") return false;
  instalarRegla().then(
    () => responder({ ok: true }),
    (error) => responder({ ok: false, error: String(error) }),
  );
  return true; // la respuesta llega después, cuando la regla ya está
});
