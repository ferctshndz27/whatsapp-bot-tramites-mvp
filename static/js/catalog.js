/* Edición del catálogo extraído de templates/catalog.html (antes en línea; la CSP ya no lo permite). */
// Detección de cambios sin guardar: resalta la fila, muestra "● sin guardar"
// y avisa antes de salir de la página si hay ediciones pendientes.
(function () {
  var dirty = new Set();

  function formIsDirty(fid) {
    var els = document.querySelectorAll('[form="' + fid + '"][name]');
    for (var i = 0; i < els.length; i++) {
      if (els[i].dataset.orig !== undefined && els[i].value !== els[i].dataset.orig) return true;
    }
    return false;
  }

  function refresh(fid) {
    var isDirty = formIsDirty(fid);
    isDirty ? dirty.add(fid) : dirty.delete(fid);
    document.querySelectorAll('.dirty-tag[data-for="' + fid + '"]').forEach(function (t) {
      t.hidden = !isDirty;
    });
    document.querySelectorAll('tr[data-form="' + fid + '"]').forEach(function (r) {
      r.classList.toggle('row-dirty', isDirty);
    });
  }

  document.querySelectorAll('input[form][name], textarea[form][name]').forEach(function (el) {
    el.dataset.orig = el.value;
    el.addEventListener('input', function () { refresh(el.getAttribute('form')); });
  });

  document.querySelectorAll('form[id^="f-"]').forEach(function (f) {
    f.addEventListener('submit', function () { dirty.delete(f.id); });
  });

  window.addEventListener('beforeunload', function (e) {
    if (dirty.size) { e.preventDefault(); e.returnValue = ''; }
  });
})();
