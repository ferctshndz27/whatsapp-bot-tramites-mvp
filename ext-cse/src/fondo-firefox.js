// Fondo de la extensión en Firefox. Aquí el CRM no va en un marco: Firefox no deja enmarcarlo ni
// con regla de cabeceras (spike S1). El panel lateral abre panel.html, que comprueba la red y el
// CRM y luego navega a la página del CRM misma; por eso el panel no se fija aquí con setPanel (se
// saltaría esa comprobación) y este manifiesto no pide ningún permiso.

// El ícono de la barra abre y cierra el panel, como en Chrome. toggle() debe llamarse dentro del
// clic mismo, sin esperar nada antes.
browser.action.onClicked.addListener(() => browser.sidebarAction.toggle());

// panel.js (el mismo en los dos navegadores) pide «asegurar-regla» antes de abrir un marco. Firefox no usa
// regla de cabeceras (el CRM no va en un marco, ver arriba): no hay nada que asegurar, y sin respuesta el
// panel mostraba «No se pudo preparar el acceso».
browser.runtime.onMessage.addListener((mensaje) => (mensaje?.tipo === "asegurar-regla" ? Promise.resolve({ ok: true }) : undefined));
