// Fondo de la extensión en Firefox. Aquí el CRM no va en un marco: Firefox no deja enmarcarlo ni
// con regla de cabeceras (spike S1). El panel lateral abre panel.html, que comprueba la red y el
// CRM y luego navega a la página del CRM misma; por eso el panel no se fija aquí con setPanel (se
// saltaría esa comprobación) y este manifiesto no pide ningún permiso.

// El ícono de la barra abre y cierra el panel, como en Chrome. toggle() debe llamarse dentro del
// clic mismo, sin esperar nada antes.
browser.action.onClicked.addListener(() => browser.sidebarAction.toggle());
