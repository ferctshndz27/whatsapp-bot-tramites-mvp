/* Nuevo cobro (/cobros/nuevo): buscador de cliente, alta en línea y botones de copiar.

   Mejora progresiva: sin JS el formulario se ve completo y se teclea a mano; con JS los campos de
   cliente quedan escondidos hasta elegir o registrar a alguien. Todo lo que viene del servidor se
   pinta con textContent: un nombre de cliente nunca se interpreta como HTML. */
(function () {
  'use strict';

  var forma = document.querySelector('[data-cn-form]');
  if (!forma) return;

  var buscar = forma.querySelector('[data-cn-buscar]');
  var estado = forma.querySelector('[data-cn-estado]');
  var lista = forma.querySelector('[data-cn-resultados]');
  var elegido = forma.querySelector('[data-cn-elegido]');
  var campoTel = forma.querySelector('[data-cn-phone]');
  var campoNombre = forma.querySelector('[data-cn-name]');
  var notaNombre = forma.querySelector('[data-cn-nombre-nota]');
  var casillaCartera = forma.querySelector('[data-cn-cartera]');
  var textoCartera = forma.querySelector('[data-cn-cartera-texto]');
  var btnCambiar = forma.querySelector('[data-cn-cambiar]');
  var btnNuevo = forma.querySelector('[data-cn-nuevo]');
  var btnEnviar = forma.querySelector('[data-cn-enviar]');

  var DEMORA_MS = 250;
  var temporizador = null;
  var ultima = 0;

  function mostrarElegido(si) {
    elegido.hidden = !si;
    buscar.closest('fieldset').classList.toggle('con-cliente', si);
    buscar.hidden = si;
    var rotulo = forma.querySelector('label[for="cn-buscar"]');
    if (rotulo) rotulo.hidden = si;
    if (si) { lista.hidden = true; btnNuevo.hidden = true; estado.textContent = ''; }
  }

  function elegirExistente(c) {
    campoTel.value = c.phone;
    campoNombre.value = c.name || c.alias || '';
    campoNombre.readOnly = true;
    notaNombre.textContent = '';
    // Ya en su cartera no hay nada que preguntar; si no, se ofrece (marcada, como en /clients).
    casillaCartera.checked = !c.mio;
    casillaCartera.closest('label').hidden = c.mio;
    textoCartera.textContent = 'Agregarlo a mi cartera';
    mostrarElegido(true);
    forma.querySelector('#cn-concepto').focus();
  }

  function registrarNuevo() {
    var q = buscar.value.trim();
    var digitos = q.replace(/\D/g, '');
    var esTelefono = digitos.length >= 8 && digitos.length === q.replace(/[\s()+-]/g, '').length;
    campoTel.value = esTelefono ? q : '';
    campoNombre.value = esTelefono ? '' : q;
    campoNombre.readOnly = false;
    notaNombre.textContent = '(cliente nuevo)';
    casillaCartera.checked = true;
    casillaCartera.closest('label').hidden = false;
    textoCartera.textContent = 'Agregarlo a mi cartera al registrarlo';
    mostrarElegido(true);
    (esTelefono ? campoNombre : campoTel).focus();
  }

  function cambiar() {
    campoTel.value = '';
    campoNombre.value = '';
    campoNombre.readOnly = false;
    mostrarElegido(false);
    buscar.value = '';
    buscar.focus();
  }

  function pintar(clientes, q) {
    lista.textContent = '';
    clientes.forEach(function (c) {
      var li = document.createElement('li');
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'cn-resultado';
      var quien = document.createElement('strong');
      quien.textContent = c.name || c.alias || 'Sin nombre';
      var detalle = document.createElement('span');
      detalle.textContent = c.phone + (c.alias && c.name ? ' · ' + c.alias : '');
      b.append(quien, detalle);
      if (c.mio) {
        var marca = document.createElement('span');
        marca.className = 'badge cerrado';
        marca.textContent = 'En mi cartera';
        b.append(marca);
      }
      b.addEventListener('click', function () { elegirExistente(c); });
      li.append(b);
      lista.append(li);
    });
    lista.hidden = clientes.length === 0;
    estado.textContent = clientes.length
      ? clientes.length + (clientes.length === 1 ? ' cliente' : ' clientes') + '. Elige uno o registra uno nuevo.'
      : 'No hay clientes con «' + q + '».';
    btnNuevo.hidden = false;
  }

  function consultar() {
    var q = buscar.value.trim();
    if (q.length < 2) {
      lista.hidden = true; lista.textContent = ''; btnNuevo.hidden = true;
      estado.textContent = q ? 'Escribe al menos 2 caracteres.' : '';
      return;
    }
    var turno = ++ultima;
    estado.textContent = 'Buscando…';
    fetch('/cobros/nuevo/clientes?q=' + encodeURIComponent(q), { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (datos) { if (turno === ultima) pintar(datos.clientes, q); })
      .catch(function () {
        if (turno !== ultima) return;
        estado.textContent = 'No se pudo buscar. Revisa tu conexión o registra al cliente a mano.';
        btnNuevo.hidden = false;
      });
  }

  buscar.addEventListener('input', function () {
    clearTimeout(temporizador);
    temporizador = setTimeout(consultar, DEMORA_MS);
  });
  // Enter en el buscador no envía el formulario: busca.
  buscar.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); clearTimeout(temporizador); consultar(); }
  });
  btnNuevo.addEventListener('click', registrarNuevo);
  btnCambiar.addEventListener('click', cambiar);

  forma.addEventListener('submit', function (e) {
    if (!campoTel.value.trim()) {
      e.preventDefault();
      mostrarElegido(false);
      estado.textContent = 'Elige o registra un cliente antes de generar la liga.';
      buscar.focus();
      return;
    }
    // Un doble clic no genera dos ligas (el token del formulario también lo corta en el servidor).
    btnEnviar.disabled = true;
    btnEnviar.textContent = 'Generando…';
  });
  // Al volver con «atrás» el navegador conserva el botón apagado.
  window.addEventListener('pageshow', function () {
    btnEnviar.disabled = false;
    btnEnviar.textContent = 'Generar liga';
  });

  // Con datos de vuelta (error del servidor, o ?phone=) el cliente ya viene puesto; si no, buscador.
  if (campoTel.value.trim()) {
    var nuevo = !!campoNombre.value.trim() && !campoNombre.readOnly;
    notaNombre.textContent = nuevo ? '(cliente nuevo)' : '';
    mostrarElegido(true);
  } else {
    mostrarElegido(false);
  }

  /* Copiar. Primero la API moderna; si el navegador la niega (marco de otro origen sin permiso),
     un textarea temporal y execCommand. Avisar al servidor es best-effort: la auditoría de «quién
     copió la liga» no debe romper el botón. */
  function copiar(texto) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(texto).catch(function () { return respaldo(texto); });
    }
    return respaldo(texto);
  }

  function respaldo(texto) {
    return new Promise(function (resolver, rechazar) {
      var t = document.createElement('textarea');
      t.value = texto;
      t.setAttribute('readonly', '');
      t.className = 'cn-oculto';
      document.body.append(t);
      t.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      t.remove();
      return ok ? resolver() : rechazar(new Error('copiar'));
    });
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-cn-copiar]') : null;
    if (!b) return;
    var original = b.dataset.cnTexto || b.textContent;
    copiar(b.dataset.cnCopiar).then(function () {
      b.textContent = 'Copiado ✓';
      if (b.dataset.cnAviso) {
        fetch(b.dataset.cnAviso, { method: 'POST', credentials: 'same-origin', keepalive: true })
          .catch(function () { /* best-effort */ });
      }
    }).catch(function () {
      b.textContent = 'No se pudo copiar';
      var campo = document.getElementById('cn-liga');
      if (campo) { campo.focus(); campo.select(); }
    }).then(function () {
      setTimeout(function () { b.textContent = original; }, 2000);
    });
  });
})();
