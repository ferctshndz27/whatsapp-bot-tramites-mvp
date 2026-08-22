/* Preservación del estado de la vista para TODO el panel.
 *
 * El panel es server-rendered: cada botón es un POST que redirige y vuelve a
 * pintar la página entera. Eso borraba, en cada acción, dónde estaba el
 * scroll, qué sección estaba desplegada y en qué campo estaba el cursor — y
 * en las tres vistas que se auto-recargaban (bandeja, chat de equipo,
 * solicitudes) borraba además el mensaje a medio escribir: un asesor que
 * tardara más de 20 segundos redactando perdía el texto sin aviso.
 *
 * Dos mecanismos, deliberadamente distintos:
 *
 * 1. RESTAURACIÓN. Antes de salir se guarda el estado en sessionStorage (por
 *    URL y por pestaña; se borra al cerrarla) y al volver se aplica.
 * 2. APLAZAMIENTO. La auto-recarga ya no la hace el navegador con
 *    <meta http-equiv="refresh"> —que no se puede detener— sino un contador
 *    propio que se reinicia mientras el usuario está trabajando.
 *
 * Qué se restaura y qué NO, a propósito:
 *   - Scroll, <details> abiertos y el foco: SIEMPRE. Son seguros en cualquier
 *     navegación.
 *   - Casillas, radios y <select>: SOLO tras una auto-recarga. Si el usuario
 *     mandó un formulario, el estado que vale es el que devolvió el servidor;
 *     reimponer la selección vieja pisaría el resultado de su propia acción.
 *   - El TEXTO de los campos: NUNCA se guarda. Son mensajes de clientes y no
 *     tienen por qué quedar en el almacenamiento del navegador. El texto sin
 *     enviar se protege impidiendo la recarga, que además de no perderlo
 *     tampoco lo copia a ningún lado.
 */
(function () {
  "use strict";

  // La llave conserva los FILTROS (otra lista = otro scroll) pero descarta los
  // parámetros efímeros que el patrón POST→redirect agrega al volver: sin esto
  // "/catalog" y "/catalog?ok=guardado" son vistas distintas y no se restaura
  // nada justo después de una acción, que es cuando más importa.
  var EFIMEROS = ["ok", "err", "error", "msg", "volver"];
  // Holgura para considerar que un contenedor estaba "pegado al fondo": el
  // scroll con zoom o densidad de pantalla no cae en el píxel exacto.
  var MARGEN_FONDO = 24;

  function llaveDeVista() {
    var params;
    try {
      params = new URLSearchParams(location.search);
    } catch (e) {
      return "vista:" + location.pathname;
    }
    EFIMEROS.forEach(function (p) { params.delete(p); });
    var cola = params.toString();
    return "vista:" + location.pathname + (cola ? "?" + cola : "");
  }

  var CLAVE = llaveDeVista();
  var CLAVE_MOTIVO = "vista:motivo";
  var almacen;
  try {
    almacen = window.sessionStorage;
    almacen.getItem(CLAVE); // el modo privado estricto revienta aquí
  } catch (e) {
    return; // Sin almacén no hay nada que preservar; la página sigue igual.
  }

  // Sin esto el navegador restaura el scroll por su cuenta y pelea con el
  // nuestro (que sí conoce los contenedores internos).
  if ("scrollRestoration" in history) history.scrollRestoration = "manual";

  function esEditable(el) {
    if (!el) return false;
    if (el.tagName === "TEXTAREA" || el.isContentEditable) return true;
    if (el.tagName !== "INPUT") return false;
    return ["text", "search", "tel", "email", "url", "number", "password", "date"]
      .indexOf(el.type) !== -1;
  }

  function llave(el) {
    return el.id || el.name || "";
  }

  function hayTextoSinEnviar() {
    var campos = document.querySelectorAll("input, textarea, [contenteditable]");
    for (var i = 0; i < campos.length; i++) {
      var el = campos[i];
      if (!esEditable(el)) continue;
      var valor = el.isContentEditable ? el.textContent : el.value;
      if (valor && valor.trim()) return true;
    }
    return false;
  }

  // ---- 1. Guardar y restaurar -------------------------------------------

  function capturar() {
    var estado = { y: window.scrollY, contenedores: {}, detalles: [],
                   foco: null, controles: {} };

    var conId = document.querySelectorAll("[id]");
    for (var i = 0; i < conId.length; i++) {
      var caja = conId[i];
      if (caja.scrollTop <= 0) continue;
      // Además de la posición se guarda si estaba PEGADO AL FONDO. Un hilo de
      // mensajes que crece (mandaste uno, llegó otro) tiene un fondo nuevo:
      // restaurar el número viejo dejaría el mensaje reciente fuera de vista,
      // que es justo lo contrario de lo que espera quien está conversando.
      estado.contenedores[caja.id] = {
        top: caja.scrollTop,
        alFondo: caja.scrollTop + caja.clientHeight >= caja.scrollHeight - MARGEN_FONDO,
      };
    }

    var detalles = document.querySelectorAll("details");
    for (var j = 0; j < detalles.length; j++) {
      if (detalles[j].open && llave(detalles[j])) estado.detalles.push(llave(detalles[j]));
    }

    var activo = document.activeElement;
    if (activo && activo !== document.body && llave(activo)) {
      estado.foco = { llave: llave(activo) };
      if (esEditable(activo) && typeof activo.selectionStart === "number") {
        estado.foco.inicio = activo.selectionStart;
        estado.foco.fin = activo.selectionEnd;
      }
    }

    var controles = document.querySelectorAll(
      "input[type=checkbox], input[type=radio], select");
    for (var k = 0; k < controles.length; k++) {
      var c = controles[k];
      if (!llave(c)) continue;
      // El valor entra en la llave: varias casillas comparten el mismo name.
      estado.controles[llave(c) + "|" + (c.value || "")] =
        c.tagName === "SELECT" ? c.value : c.checked;
    }
    return estado;
  }

  function guardar() {
    try {
      almacen.setItem(CLAVE, JSON.stringify(capturar()));
    } catch (e) {
      /* almacén lleno: preservar es un lujo y jamás puede romper la página */
    }
  }

  function restaurar() {
    var estado;
    try {
      estado = JSON.parse(almacen.getItem(CLAVE) || "null");
    } catch (e) {
      return;
    }
    if (!estado) return;
    var veniaDeAutoRecarga = almacen.getItem(CLAVE_MOTIVO) === "auto";
    almacen.removeItem(CLAVE_MOTIVO);

    (estado.detalles || []).forEach(function (id) {
      var el = document.getElementById(id) ||
               document.querySelector('details[name="' + id + '"]');
      if (el) el.open = true;
    });

    var contenedores = estado.contenedores || {};
    Object.keys(contenedores).forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      var guardado = contenedores[id];
      // Formato viejo (número suelto) por si quedó algo de una sesión previa.
      if (typeof guardado === "number") { el.scrollTop = guardado; return; }
      el.scrollTop = guardado.alFondo ? el.scrollHeight : guardado.top;
    });

    if (veniaDeAutoRecarga) {
      // Solo aquí: la página se recargó sola, sin que el usuario pidiera nada,
      // así que su selección sigue siendo la vigente.
      var controles = document.querySelectorAll(
        "input[type=checkbox], input[type=radio], select");
      for (var i = 0; i < controles.length; i++) {
        var c = controles[i];
        var previo = (estado.controles || {})[llave(c) + "|" + (c.value || "")];
        if (previo === undefined) continue;
        if (c.tagName === "SELECT") c.value = previo;
        else c.checked = previo;
      }
    }

    if (typeof estado.y === "number") window.scrollTo(0, estado.y);

    if (estado.foco) {
      var destino = document.getElementById(estado.foco.llave) ||
                    document.querySelector('[name="' + estado.foco.llave + '"]');
      if (destino && typeof destino.focus === "function") {
        destino.focus({ preventScroll: true });
        if (typeof estado.foco.inicio === "number" && destino.setSelectionRange) {
          try {
            destino.setSelectionRange(estado.foco.inicio, estado.foco.fin);
          } catch (e) {
            /* el campo cambió de tipo entre cargas: con el foco basta */
          }
        }
      }
    }
  }

  // pagehide cubre también el paso al caché de retroceso, que unload no.
  window.addEventListener("pagehide", guardar);
  document.addEventListener("submit", guardar, true);

  // ---- 2. Auto-recarga que espera al usuario -----------------------------

  function montarAutoRecarga() {
    var meta = document.querySelector('meta[name="auto-recarga"]');
    if (!meta) return;
    var segundos = Math.max(5, Number(meta.content) || 30);
    var restante = segundos;
    var aviso = null;

    function ocupado() {
      if (esEditable(document.activeElement)) return true;
      if (hayTextoSinEnviar()) return true;
      var sel = window.getSelection();
      return !!(sel && !sel.isCollapsed && String(sel).trim());
    }

    function pintarAviso(pausada) {
      if (pausada && !aviso) {
        aviso = document.createElement("p");
        aviso.className = "muted auto-recarga-aviso";
        aviso.setAttribute("role", "status");
        aviso.textContent = "Actualización automática en pausa mientras escribes.";
        document.body.appendChild(aviso);
      } else if (!pausada && aviso) {
        aviso.remove();
        aviso = null;
      }
    }

    setInterval(function () {
      if (ocupado()) {
        restante = segundos; // el contador reinicia; la recarga no se cancela
        pintarAviso(true);
        return;
      }
      pintarAviso(false);
      restante -= 1;
      if (restante <= 0) {
        try {
          almacen.setItem(CLAVE_MOTIVO, "auto");
        } catch (e) {
          /* sin la marca se restaura lo seguro, que es suficiente */
        }
        guardar();
        location.reload();
      }
    }, 1000);
  }

  // Gancho para las vistas con estado propio que el DOM no refleja (el lienzo
  // del Estudio y su nodo seleccionado, por ejemplo). Guardan y leen por el
  // MISMO camino: una sola política de preservación para todo el panel.
  window.EstadoVista = {
    guardar: function (nombre, valor) {
      try {
        almacen.setItem(CLAVE + "#" + nombre, JSON.stringify(valor));
      } catch (e) { /* preservar nunca rompe */ }
    },
    leer: function (nombre) {
      try {
        return JSON.parse(almacen.getItem(CLAVE + "#" + nombre) || "null");
      } catch (e) {
        return null;
      }
    },
  };

  function arrancar() {
    restaurar();
    montarAutoRecarga();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", arrancar);
  } else {
    arrancar();
  }
})();
