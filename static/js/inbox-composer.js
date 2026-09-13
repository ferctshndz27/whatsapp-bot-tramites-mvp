/* Compositor del chat extraído de templates/inbox_chat.html (antes en línea; la CSP ya no lo permite). */
(function () {
  var form = document.getElementById('composer');
  var ta = document.getElementById('body');
  var file = document.getElementById('file');
  var chip = document.getElementById('attach-chip');

  document.querySelectorAll('.qr').forEach(function (b) {
    b.addEventListener('click', function () {
      ta.value = b.dataset.text;
      ta.focus();
      crecer();
    });
  });

  // El textarea crece con el contenido y se detiene en max-height (CSS).
  function crecer() {
    ta.style.height = 'auto';
    ta.style.height = Math.min(ta.scrollHeight, 140) + 'px';
  }
  ta.addEventListener('input', crecer);

  // Enter envía, Shift+Enter salta de línea (como WhatsApp Web). Solo con
  // teclado físico: en un teclado táctil no hay Shift+Enter y el asesor
  // se quedaría sin poder escribir varias líneas desde el celular.
  var tecladoFisico = window.matchMedia('(pointer: fine)').matches;
  ta.addEventListener('keydown', function (e) {
    if (tecladoFisico && e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      form.requestSubmit();
    }
  });

  function peso(bytes) {
    return bytes < 1048576
      ? Math.max(1, Math.round(bytes / 1024)) + ' KB'
      : (bytes / 1048576).toFixed(1) + ' MB';
  }

  function pintarChip() {
    chip.textContent = '';
    var f = file.files && file.files[0];
    if (!f) {
      chip.hidden = true;
      ta.placeholder = 'Escribe un mensaje…';
      return;
    }
    // Se revela ANTES de llenarlo: lo que cambia dentro de un subárbol
    // oculto no lo anuncia la región aria-live.
    chip.hidden = false;
    if (f.type.indexOf('image/') === 0) {
      // data: y no blob: — el CSP del panel es `img-src 'self' data:`
      // (app/main.py), así que una URL blob: no se pintaría.
      var img = document.createElement('img');
      img.alt = '';
      var lector = new FileReader();
      lector.onload = function () { img.src = lector.result; };
      lector.readAsDataURL(f);
      chip.appendChild(img);
    } else {
      var ico = document.createElement('span');
      ico.textContent = '📄';
      ico.setAttribute('aria-hidden', 'true');
      chip.appendChild(ico);
    }
    var nombre = document.createElement('span');
    nombre.className = 'name';
    nombre.textContent = f.name;
    var tam = document.createElement('span');
    tam.className = 'peso';
    tam.textContent = peso(f.size);
    var quitar = document.createElement('button');
    quitar.type = 'button';
    quitar.className = 'chip-x';
    quitar.textContent = '✕ Quitar';
    quitar.addEventListener('click', function () {
      file.value = '';
      pintarChip();
      file.focus();
    });
    chip.appendChild(nombre);
    chip.appendChild(tam);
    chip.appendChild(quitar);
    ta.placeholder = 'Agrega un texto (opcional)…';
  }
  file.addEventListener('change', pintarChip);

  // Sin texto ni archivo no hay nada que enviar: se dice por qué en vez
  // de cancelar en silencio (el textarea ya no lleva `required`).
  form.addEventListener('submit', function (e) {
    if (!ta.value.trim() && !(file.files && file.files.length)) {
      e.preventDefault();
      chip.textContent = 'Escribe un mensaje o adjunta un archivo.';
      chip.hidden = false;
      ta.focus();
    }
  });
})();
