/* Liga del autocobro: Stripe Elements (campo de tarjeta) + SetupIntent.
   El servidor crea el SetupIntent en /preparar; la tarjeta la confirma Stripe desde el navegador
   (con 3D Secure si el banco lo pide) y de regreso /listo lo verifica DIRECTO con Stripe. */
(function () {
  "use strict";
  var form = document.getElementById("form-autocobro");
  if (!form || typeof Stripe === "undefined") return;

  var stripe = Stripe(form.dataset.pk);
  var tarjeta = stripe.elements({ locale: "es" }).create("card", { hidePostalCode: true });
  tarjeta.mount("#tarjeta");

  var boton = document.getElementById("enviar");
  var caja = document.getElementById("error-autocobro");

  function mostrar(mensaje) {
    caja.textContent = mensaje;
    caja.hidden = !mensaje;
  }
  function ocupado(si) {
    boton.disabled = si;
    boton.textContent = si ? "Procesando…" : "Registrar mi tarjeta";
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (boton.disabled) return; // doble clic
    mostrar("");
    var nombre = form.elements.nombre.value.trim();
    var email = form.elements.email.value.trim();
    if (!form.elements.acepto.checked) {
      mostrar("Marca la casilla para autorizar los cobros.");
      return;
    }
    ocupado(true);

    var cuerpo = new URLSearchParams({ nombre: nombre, email: email, acepto: "1" });
    fetch(form.dataset.preparar, {
      method: "POST",
      body: cuerpo,
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      credentials: "omit",
    })
      .then(function (r) {
        return r.json().then(function (datos) { return { ok: r.ok, datos: datos }; });
      })
      .then(function (res) {
        if (!res.ok) throw new Error(res.datos.error || "No pudimos preparar el formulario.");
        return stripe
          .confirmCardSetup(res.datos.client_secret, {
            payment_method: { card: tarjeta, billing_details: { name: nombre, email: email } },
          })
          .then(function (out) {
            if (out.error) throw new Error(out.error.message || "No se pudo registrar la tarjeta.");
            var url = new URL(res.datos.return_url);
            url.searchParams.set("setup_intent", out.setupIntent.id);
            window.location.assign(url.toString());
          });
      })
      .catch(function (err) {
        mostrar(err.message || "Algo salió mal. Intenta de nuevo.");
        ocupado(false);
      });
  });
})();
