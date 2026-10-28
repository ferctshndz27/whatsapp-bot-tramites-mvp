// Fondo de la extensión en Chrome (service worker). Dos trabajos, y solo dos:
//
//  1. Que el ícono de la barra abra el panel lateral.
//  2. Que el CRM se deje mostrar DENTRO del panel. El bot manda `X-Frame-Options: SAMEORIGIN` y
//     `frame-ancestors 'none'` para que nadie lo meta en un marco (clickjacking). La regla de
//     regla-marco.json (la arma construir.py) quita X-Frame-Options y pone la CSP del bot con un
//     solo cambio: frame-ancestors deja entrar a esta extensión y a nadie más. Solo aplica a
//     submarcos hacia el CRM que pide esta extensión o el propio CRM (el login redirige dentro
//     del marco); fuera del panel el CRM sigue sin poder enmarcarse.

const yo = chrome.runtime.id;

chrome.sidePanel
  .setPanelBehavior({ openPanelOnActionClick: true })
  .catch((error) => console.error("ext-cse: el ícono no abrirá el panel", error));

async function instalarRegla() {
  const regla = await (await fetch(chrome.runtime.getURL("regla-marco.json"))).json();
  // El id no existe hasta instalar la extensión, así que se le suma aquí y no en construir.py.
  regla.condition.initiatorDomains.unshift(yo);
  const csp = regla.action.responseHeaders.find((c) => c.header === "content-security-policy");
  csp.value += `; frame-ancestors chrome-extension://${yo}`;

  // Quedan exactamente las reglas del paquete: se quitan todas las anteriores, también las de una
  // versión vieja.
  const anteriores = await chrome.declarativeNetRequest.getDynamicRules();
  await chrome.declarativeNetRequest.updateDynamicRules({
    removeRuleIds: anteriores.map((r) => r.id),
    addRules: [regla],
  });
}

function alArrancar() {
  instalarRegla().catch((error) => console.error("ext-cse: el CRM no podrá mostrarse en el panel", error));
}

chrome.runtime.onInstalled.addListener(alArrancar);
chrome.runtime.onStartup.addListener(alArrancar);
