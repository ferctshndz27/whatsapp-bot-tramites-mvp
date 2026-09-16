/* Copiar códigos de invitación extraído de templates/invitations.html (antes en línea; la CSP ya no lo permite). */
// Copiar al portapapeles con fallback para contextos no seguros (http local).
document.querySelectorAll('.copy-code').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var code = btn.dataset.code;
    var done = function () {
      var tag = btn.nextElementSibling;
      if (tag) {
        tag.hidden = false;
        setTimeout(function () { tag.hidden = true; }, 2000);
      }
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(code).then(done);
    } else {
      var ta = document.createElement('textarea');
      ta.value = code;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand('copy');
      document.body.removeChild(ta);
      done();
    }
  });
});
