/* Acciones declarativas del panel: el marcado dice QUÉ pasa (data-*) y este
   archivo dice CÓMO. Sustituye los onsubmit/onclick/onchange que vivían en las
   plantillas y el script en línea de base.html: la CSP (script-src 'self') ya
   no ejecuta JS en línea.

   Por qué data-confirm y no confirm('{{ x }}') dentro de un atributo: el
   navegador decodifica las entidades del atributo ANTES de compilar el JS, así
   que el escape de Jinja no protege ahí. Un nombre de cliente con comilla
   simple cerraba la cadena del confirm del cobro, y lo que siguiera se
   ejecutaba en la sesión del asesor. Leído con dataset, el texto nunca pasa por
   el intérprete de JS. De paso, una comilla en el nombre de una categoría ya no
   rompe el manejador (que se enviaba SIN pedir confirmación). */
(function () {
  'use strict';

  function cercano(e, selector) {
    return e.target && e.target.closest ? e.target.closest(selector) : null;
  }

  // Filas con data-href: toda la fila navega, como una tarjeta de
  // conversación. Mejora progresiva: sin JS el enlace interno de la fila
  // sigue funcionando. No navega si el click cayó en un control (enlace,
  // botón, campo), lleva modificadores o hay texto seleccionado.
  document.addEventListener('click', function (e) {
    var fila = cercano(e, 'tr[data-href]');
    if (!fila) return;
    if (e.target.closest('a, button, input, select, textarea, label')) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (String(window.getSelection())) return;
    window.location.assign(fila.dataset.href);
  });

  // Texto del confirm. data-confirm-campos="monto" sustituye {monto} por el
  // valor que tiene ese campo al momento de enviar: el aviso del cobro cita la
  // cantidad que el asesor acaba de escribir.
  function mensaje(el, form) {
    var texto = el.dataset.confirm;
    (el.dataset.confirmCampos || '').split(/\s+/).filter(Boolean).forEach(function (nombre) {
      var campo = form && form.elements.namedItem(nombre);
      texto = texto.split('{' + nombre + '}').join(campo ? campo.value : '');
    });
    return texto;
  }

  // data-confirm en un <form>. Fase de captura, igual que el guardado de
  // estado-vista.js (que se registró antes y sigue corriendo primero, como
  // cuando esto era un onsubmit). Cancelar solo evita el envío; no corta la
  // propagación, así que los demás listeners ven defaultPrevented.
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (form.matches && form.matches('form[data-confirm]') && !window.confirm(mensaje(form, form))) {
      e.preventDefault();
    }
  }, true);

  // data-confirm en un botón o enlace (el opt-out de client.html).
  document.addEventListener('click', function (e) {
    var el = cercano(e, 'button[data-confirm], a[data-confirm], input[data-confirm]');
    if (el && !window.confirm(mensaje(el, el.form || null))) e.preventDefault();
  }, true);

  // data-autoenvio en un <select>: envía su formulario al cambiar. Con
  // form.submit(), igual que el this.form.submit() de antes: no dispara submit
  // y por lo tanto no pasa por data-confirm.
  document.addEventListener('change', function (e) {
    var el = e.target;
    if (el.matches && el.matches('[data-autoenvio]') && el.form) el.form.submit();
  });

  // data-copiar: copia el valor al portapapeles y lo confirma en el botón.
  document.addEventListener('click', function (e) {
    var el = cercano(e, '[data-copiar]');
    if (!el) return;
    navigator.clipboard.writeText(el.dataset.copiar);
    el.textContent = 'Copiado ✓';
  });
})();
