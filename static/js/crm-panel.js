/* Cajón lateral del chat del CRM (/crm): gestión del folio, etiquetas, notas,
   cobro, seguimiento y transferencia sin salir de la conversación.
   Marcado en templates/crm.html, estilos en static/css/crm-panel.css.

   LEE de GET /crm/api/panel/{phone} y ESCRIBE por los mismos POST que la vista
   del folio (app/dashboard.py, app/my.py, app/cobranza_panel.py) y la ficha del
   cliente (app/clients.py): ni un endpoint de escritura nuevo, así que cada
   regla sigue teniendo un solo dueño.

   Como esos endpoints contestan un redirect 303 —y varios RECHAZAN EN SILENCIO
   cuando la acción no te toca— aquí no se pinta nada de forma optimista: se
   ejecuta, se vuelve a pedir el panel y se compara contra lo que quedó.

   Módulo aparte de crm.js a propósito (ese ya son 2000 líneas): se entera de la
   conversación abierta por el evento crm:chat y habla con él por window.CRMKanban. */
(function () {
  "use strict";

  var API = "/crm/api";
  /* Con el cajón abierto se refresca cada tanto: los cobros y los recordatorios
     los puede mover otra persona. Nunca mientras hay algo escrito sin enviar. */
  var SONDEO_PANEL_MS = 30000;

  var SECCIONES = ["gestion", "etiquetas", "notas", "cobro", "seguimiento", "transferir"];
  /* La conversación dejó de ser visible para este usuario: pasa cada vez que
     uno transfiere su propia solicitud (un colaborador solo ve las que toma). */
  var PERDIDO = "perdido";

  /* Mismo catálogo que traduce templates/ticket_detail.html (bloque de avisos):
     los códigos los pone el redirect de los endpoints de cobranza. */
  var ERRORES = {
    monto: "El monto no se entiende.",
    "monto-minimo": "El monto es menor al mínimo que acepta la pasarela.",
    "monto-maximo": "El monto pasa del máximo permitido.",
    "monto-oxxo": "OXXO no acepta un monto tan alto.",
    "sin-stripe": "Stripe no está configurado.",
    "sin-url-publica": "Falta la URL pública para armar el link de pago.",
    "envio-fallo": "El cobro se generó, pero no se pudo enviar por WhatsApp.",
    "cobro-vivo": "Ese cliente ya tiene un cobro vivo.",
    stripe: "Stripe rechazó la operación.",
    ventana: "La ventana de 24 h está cerrada: no se pudo enviar por WhatsApp.",
    "cancelacion-pendiente": "La cancelación quedó pedida; Stripe la confirma después.",
    "cobro-vivo-al-cerrar": "Ese folio tiene un cobro sin liquidar: confirma para cerrarlo.",
    "sin-novedad": "Stripe no reportó nada nuevo.",
    "no-pagado": "Ese cobro todavía no está pagado.",
    "ya-pagado": "Ese cobro ya está pagado.",
    "cobro-no-cancelable": "Ese cobro ya no se puede cancelar.",
    interno: "No se pudo completar la acción."
  };

  var EXITOS = {
    cobro: "Cobro generado y enviado",
    enviado: "Link reenviado por WhatsApp",
    cancelado: "Cobro cancelado",
    reconciliado: "Stripe actualizó el cobro",
    confirmado: "Queda constancia de tu confirmación",
    avisado: "Se le avisó al cliente"
  };

  var CONFIRMAR_COBRO =
    "Esto NO marca el cobro como pagado: solo deja constancia de que viste el " +
    "comprobante. Stripe sigue siendo la única confirmación.";

  var els = {};
  var estado = {
    phone: null,
    datos: null,
    seccion: null,
    disparador: null,
    enviando: false,
    tocado: false,
    seq: 0
  };

  /* ------------------------------------------------------------------ */
  /* Utilidades                                                          */
  /* ------------------------------------------------------------------ */

  function $(sel, raiz) { return (raiz || document).querySelector(sel); }
  function $$(sel, raiz) {
    return Array.prototype.slice.call((raiz || document).querySelectorAll(sel));
  }

  function setText(sel, texto) {
    var el = $(sel);
    if (el) el.textContent = texto == null ? "" : String(texto);
  }

  function mostrar(sel, visible) {
    var el = $(sel);
    if (el) el.hidden = !visible;
  }

  function tpl(id) {
    var t = document.getElementById(id);
    if (!t || !t.content || !t.content.firstElementChild) return null;
    return t.content.firstElementChild.cloneNode(true);
  }

  function vaciar(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function badge(texto, clase) {
    var span = document.createElement("span");
    span.className = "badge " + clase;
    span.textContent = texto;
    return span;
  }

  function toast(msg) {
    if (window.CRMKanban && typeof window.CRMKanban.toast === "function") {
      window.CRMKanban.toast(msg);
    }
  }

  function sesionCaducada() {
    toast("Tu sesión terminó. Vuelvo a la pantalla de acceso…");
    setTimeout(function () { window.location.reload(); }, 900);
  }

  function rutaFolio(id) {
    return "/tickets/" + id + "?volver=" + encodeURIComponent("/crm?chat=" + estado.phone);
  }

  /* ------------------------------------------------------------------ */
  /* Abrir y cerrar                                                      */
  /* ------------------------------------------------------------------ */

  function abrir(seccion, disparador) {
    if (SECCIONES.indexOf(seccion) === -1) return;
    estado.seccion = seccion;
    estado.disparador = disparador || null;
    setText("#crm-panel-title", titulo(seccion));
    pintarSecciones();
    els.acciones.forEach(function (btn) {
      btn.setAttribute("aria-expanded", String(btn.getAttribute("data-seccion") === seccion));
    });
    els.raiz.classList.add("panel-abierto");
    els.panel.removeAttribute("inert");
    /* Con el cajón encima, la píldora activa puede quedar tapada: se acerca. */
    if (disparador) {
      try { disparador.scrollIntoView({ inline: "start", block: "nearest" }); } catch (_) { /* viejo */ }
    }
    try { els.titulo.focus({ preventScroll: true }); } catch (_) { els.titulo.focus(); }
    if (!estado.datos) cargar({ forzar: true });
  }

  function cerrar(sinFoco) {
    if (!estado.seccion) return;
    var disparador = estado.disparador;
    estado.seccion = null;
    estado.disparador = null;
    els.raiz.classList.remove("panel-abierto");
    els.panel.setAttribute("inert", "");
    els.acciones.forEach(function (btn) { btn.setAttribute("aria-expanded", "false"); });
    if (!sinFoco && disparador) {
      try { disparador.focus({ preventScroll: true }); } catch (_) { disparador.focus(); }
    }
  }

  function titulo(seccion) {
    var btn = els.acciones.filter(function (b) {
      return b.getAttribute("data-seccion") === seccion;
    })[0];
    /* El nombre va en data-titulo: el texto de la píldora trae emoji y contador. */
    return btn ? btn.getAttribute("data-titulo") || "" : "";
  }

  /* ------------------------------------------------------------------ */
  /* Lectura                                                             */
  /* ------------------------------------------------------------------ */

  function cargar(opts) {
    opts = opts || {};
    if (!estado.phone) return Promise.resolve(null);
    if (!opts.forzar && panelOcupado()) return Promise.resolve(estado.datos);
    var phone = estado.phone;
    var mio = ++estado.seq;
    return fetch(API + "/panel/" + phone, {
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (res) {
      if (res.status === 403) { sesionCaducada(); return null; }
      if (res.status === 404) return PERDIDO;
      if (!res.ok) throw new Error("http" + res.status);
      return res.json();
    }).then(function (datos) {
      if (mio !== estado.seq || phone !== estado.phone) return estado.datos;
      if (datos === PERDIDO) { estado.datos = null; return PERDIDO; }
      if (!datos) return estado.datos;
      estado.datos = datos;
      estado.tocado = false;
      pintar(datos);
      return datos;
    }, function () {
      if (mio !== estado.seq) return estado.datos;
      mostrar("#crm-panel-cargando", false);
      var err = $("#crm-panel-error");
      if (err) {
        err.textContent = "No se pudo cargar la solicitud. Se reintenta al abrirla de nuevo.";
        err.hidden = false;
      }
      return null;
    });
  }

  /* No se repinta encima de lo que alguien está escribiendo: el sondeo espera. */
  function panelOcupado() {
    if (estado.enviando || estado.tocado) return true;
    var a = document.activeElement;
    return !!(a && els.panel.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }

  /* ------------------------------------------------------------------ */
  /* Pintado                                                             */
  /* ------------------------------------------------------------------ */

  function pintar(d) {
    /* Marca para el juez de navegador: con el cajón cerrado nada de dentro es
       visible, así que «ya cargó» no se puede deducir mirando el DOM. */
    els.panel.setAttribute("data-cargado", "si");
    pintarRiel(d);
    mostrar("#crm-panel-cargando", false);
    mostrar("#crm-panel-error", false);
    setText("#crm-panel-sub", subtitulo(d));
    mostrar("#crm-panel-sinfolio", !d.ticket);
    pintarGestion(d);
    pintarEtiquetas(d);
    pintarNotas(d);
    pintarCobro(d);
    pintarSeguimiento(d);
    pintarTransferir(d);
    pintarSecciones();
  }

  function subtitulo(d) {
    var quien = d.nombre || d.phone || "";
    return d.ticket ? quien + " · folio #" + d.ticket.id : quien;
  }

  function pintarRiel(d) {
    var hayFolio = !!(d && d.ticket);
    els.acciones.forEach(function (btn) {
      var seccion = btn.getAttribute("data-seccion");
      /* Sin folio solo quedan las notas: son del cliente y no dependen de él. */
      btn.disabled = !!d && !hayFolio && seccion !== "notas";
      var num = $(".accion-num", btn);
      if (!num) return;
      var n = d && d.contadores ? d.contadores[seccion] : 0;
      num.textContent = n ? String(n) : "";
      num.hidden = !n;
    });
    /* Si la sección abierta se quedó sin folio, el cajón se cierra solo. */
    var activo = estado.seccion;
    if (activo && !hayFolio && activo !== "notas") cerrar(true);
  }

  function pintarSecciones() {
    SECCIONES.forEach(function (s) {
      mostrar("#crm-sec-" + s, s === estado.seccion);
    });
  }

  function pintarGestion(d) {
    var t = d.ticket;
    var p = d.permisos;
    mostrar("#crm-g-estado-tile", !!t && p.estado);
    var hayAcciones = !!t && (p.tomar || p.terminar || p.soltar || p.agente);
    mostrar("#crm-g-acciones-tile", hayAcciones);
    mostrar("#crm-g-ajeno", !!t && !!t.assigned_to && !p.atiende);
    if (!t) return;

    setText("#crm-g-folio", "Folio #" + t.id);
    setText("#crm-g-servicio", "· " + (t.service_name || ""));
    setText("#crm-g-meta", [t.category, t.price_info].filter(Boolean).join(" · "));
    setText("#crm-g-asesor", t.assigned_to ? "Atiende " + t.assigned_to : "Sin asignar");
    setText("#crm-g-ajeno-quien", t.assigned_to || "");

    var badges = $("#crm-g-badges");
    vaciar(badges);
    badges.appendChild(badge(t.status.replace("_", " "), t.status));
    if (t.escalado) badges.appendChild(badge("escalado", "escalado"));
    badges.appendChild(t.agent_active
      ? badge("agente activo", "disponible")
      : badge("agente apagado", "ausente"));
    if (!t.habilitado) badges.appendChild(badge("eliminada", "caducada"));

    $$("#crm-g-estados button").forEach(function (btn) {
      var suyo = btn.getAttribute("data-estado") === t.status;
      btn.setAttribute("aria-current", String(suyo));
      btn.disabled = !p.estado || suyo;
    });

    mostrar("#crm-g-tomar", p.tomar);
    mostrar("#crm-g-terminar", p.terminar);
    mostrar("#crm-g-soltar", p.soltar);
    mostrar("#crm-g-agente", p.agente);
    mostrar("#crm-g-agente-nota", p.agente);
    setText("#crm-g-agente", t.agent_active ? "Apagar agente IA" : "Reanudar agente IA");

    /* El aviso de cerrar con un cobro sin liquidar es el mismo de la vista del
       folio, y el texto depende de los datos: lo pone el JS, no la plantilla. */
    var vivos = d.cobro && (d.cobro.otros_folios.length || d.cobro.cobros.some(function (c) {
      return c.puede_cancelar;
    }));
    var terminar = $("#crm-g-terminar");
    if (terminar) {
      terminar.dataset.confirm = vivos
        ? "OJO: este cliente tiene un cobro SIN LIQUIDAR. Si cierras, se le avisa que su pago sigue pendiente. ¿Continuar?"
        : "¿Terminar esta solicitud? Se cierra el ticket y se le avisa al cliente.";
    }
    var soltar = $("#crm-g-soltar");
    if (soltar) {
      soltar.dataset.confirm =
        "¿Soltar esta solicitud? Vuelve a quedar sin dueño y el agente retoma al cliente.";
    }
    var ver = $("#crm-g-ver");
    if (ver) ver.setAttribute("href", rutaFolio(t.id));
  }

  function pintarEtiquetas(d) {
    var chips = $("#crm-e-chips");
    vaciar(chips);
    d.etiquetas.forEach(function (et) {
      var chip = tpl("crm-tpl-etiqueta");
      if (!chip) return;
      setTextEn(chip, ".chip-et-nombre", et.nombre);
      chip.style.setProperty("--c", et.color);
      chip.setAttribute("aria-label", "Quitar etiqueta " + et.nombre);
      chip.addEventListener("click", function () { quitarEtiqueta(et); });
      chips.appendChild(chip);
    });
    mostrar("#crm-e-vacio", d.etiquetas.length === 0);
    mostrar("#crm-e-form", d.permisos.etiquetas);

    var lista = $("#crm-e-lista");
    vaciar(lista);
    d.catalogo_etiquetas.forEach(function (nombre) {
      var op = document.createElement("option");
      op.value = nombre;
      lista.appendChild(op);
    });
  }

  function setTextEn(raiz, sel, texto) {
    var el = raiz.querySelector(sel);
    if (el) el.textContent = texto == null ? "" : String(texto);
  }

  function pintarNotas(d) {
    var t = d.ticket;
    mostrar("#crm-n-tramite-tile", !!t);
    mostrar("#crm-n-form", !!t && d.permisos.notas);
    mostrar("#crm-n-ajeno", !!t && !d.permisos.notas && !!t.assigned_to);
    if (t) {
      setText("#crm-n-tramite", t.notes);
      setText("#crm-n-ajeno-quien", t.assigned_to || "");
      mostrar("#crm-n-tramite", !!t.notes);
      mostrar("#crm-n-tramite-vacio", !t.notes);
    }

    var lista = $("#crm-n-cliente");
    vaciar(lista);
    d.notas_cliente.forEach(function (n) {
      var fila = tpl("crm-tpl-nota");
      if (!fila) return;
      setTextEn(fila, ".panel-nota-texto", n.texto);
      setTextEn(fila, ".when", n.autor + " · " + n.fecha);
      lista.appendChild(fila);
    });
    mostrar("#crm-n-cliente-vacio", d.notas_cliente.length === 0);
  }

  function pintarCobro(d) {
    var c = d.cobro;
    var lista = $("#crm-c-lista");
    vaciar(lista);
    mostrar("#crm-c-form", !!c && d.permisos.generar_cobro);
    mostrar("#crm-c-sin-stripe", !!c && d.permisos.cobrar && !c.stripe_configurado);
    mostrar("#crm-c-sin-permiso", !!c && !d.permisos.cobrar);
    mostrar("#crm-c-vacio", !c || c.cobros.length === 0);
    mostrar("#crm-c-otros-tile", !!c && c.otros_folios.length > 0);
    if (!c) return;

    c.cobros.forEach(function (cobro) { lista.appendChild(filaCobro(cobro)); });

    var otros = $("#crm-c-otros");
    vaciar(otros);
    c.otros_folios.forEach(function (o) {
      var fila = tpl("crm-tpl-cobro-otro");
      if (!fila) return;
      setTextEn(fila, ".otro-monto", o.monto);
      setTextEn(fila, ".otro-concepto", o.concepto);
      var enlace = fila.querySelector(".otro-folio");
      if (enlace) enlace.setAttribute("href", rutaFolio(o.ticket_id));
      otros.appendChild(fila);
    });

    /* El formulario se rellena solo cuando nadie lo está usando: si el asesor
       ya tecleó un monto, el sondeo no se lo borra. */
    var form = $("#crm-c-form");
    if (form && !estado.tocado) {
      form.reset();
      $("#crm-c-monto").value = c.monto_sugerido;
      $("#crm-c-concepto").value = c.concepto_sugerido;
    }
    $("#crm-c-token").value = c.token_form;
    setText("#crm-c-oxxo", c.oxxo_max);
    setText("#crm-c-cotizado-monto", c.monto_cotizado);
    mostrar("#crm-c-cotizado", !!c.monto_cotizado);
    if (form) {
      form.dataset.confirm =
        "Se va a generar un cobro por {monto} para " + (d.nombre || d.phone) +
        ". ¿Confirmas el monto?";
      form.dataset.confirmCampos = "monto";
    }
  }

  function filaCobro(cobro) {
    var fila = tpl("crm-tpl-cobro");
    if (!fila) return document.createDocumentFragment();
    var insignia = fila.querySelector(".cobro-badge");
    insignia.textContent = cobro.badge_texto;
    insignia.className = "badge cobro-badge " + cobro.badge_clase;
    setTextEn(fila, ".cobro-monto", cobro.monto);
    setTextEn(fila, ".cobro-concepto", cobro.concepto);
    setTextEn(fila, ".cobro-pie", cobro.pie);
    var motivo = fila.querySelector(".cobro-motivo");
    motivo.textContent = cobro.motivo;
    motivo.hidden = !cobro.motivo;

    var avisos = fila.querySelector(".cobro-avisos");
    vaciar(avisos);
    cobro.avisos.forEach(function (a) { avisos.appendChild(badge(a.texto, a.clase)); });
    avisos.hidden = cobro.avisos.length === 0;

    var enlace = fila.querySelector(".cobro-link");
    if (cobro.url) {
      enlace.setAttribute("href", cobro.url);
      enlace.textContent = cobro.url_texto;
      enlace.hidden = false;
    }
    var copiar = fila.querySelector(".cobro-copiar");
    if (cobro.url) {
      copiar.dataset.copiar = cobro.url;   /* lo atiende static/js/acciones.js */
      copiar.hidden = false;
    }

    accionCobro(fila, ".cobro-enviar", cobro.puede_enviar, "/cobros/" + cobro.id + "/enviar");
    accionCobro(fila, ".cobro-reconciliar", cobro.puede_reconciliar, "/cobros/" + cobro.id + "/reconciliar");
    accionCobro(fila, ".cobro-avisar", cobro.puede_avisar, "/cobros/" + cobro.id + "/avisar-pago");
    accionCobro(fila, ".cobro-confirmar", cobro.puede_confirmar, "/cobros/" + cobro.id + "/confirmar",
      CONFIRMAR_COBRO);
    accionCobro(fila, ".cobro-cancelar", cobro.puede_cancelar, "/cobros/" + cobro.id + "/cancelar",
      "¿Cancelar este cobro? Si el cliente ya tiene una ficha de OXXO, solo se cancela cuando Stripe lo confirme.");
    return fila;
  }

  function accionCobro(fila, sel, puede, url, confirmar) {
    var btn = fila.querySelector(sel);
    if (!btn || !puede) return;
    btn.hidden = false;
    if (confirmar) btn.dataset.confirm = confirmar;
    btn.addEventListener("click", function (e) {
      if (e.defaultPrevented) return;   /* el usuario dijo que no en el confirm */
      ejecutar(url, null, {});
    });
  }

  function pintarSeguimiento(d) {
    var lista = $("#crm-s-lista");
    vaciar(lista);
    d.seguimientos.forEach(function (f) {
      var fila = tpl("crm-tpl-seguimiento");
      if (!fila) return;
      setTextEn(fila, ".seg-fecha", f.fecha);
      setTextEn(fila, ".seg-texto", f.username + (f.note ? " — " + f.note : ""));
      lista.appendChild(fila);
    });
    mostrar("#crm-s-vacio", d.seguimientos.length === 0);
    mostrar("#crm-s-form", d.permisos.seguimiento);
  }

  function pintarTransferir(d) {
    var select = $("#crm-t-user");
    var anterior = select.value;
    vaciar(select);
    d.companeros.forEach(function (c) {
      var op = document.createElement("option");
      op.value = c.username;
      op.textContent = c.estado === "disponible" ? c.username : c.username + " — " + c.estado;
      select.appendChild(op);
    });
    if (anterior) select.value = anterior;
    mostrar("#crm-t-form", d.permisos.transferir);
    mostrar("#crm-t-ajeno", !!d.ticket && !d.permisos.transferir && !!d.ticket.assigned_to
      && d.companeros.length > 0);
    mostrar("#crm-t-sin-companeros", !!d.ticket && d.companeros.length === 0);
    setText("#crm-t-ajeno-quien", d.ticket && d.ticket.assigned_to ? d.ticket.assigned_to : "");
    var form = $("#crm-t-form");
    if (form) {
      form.dataset.confirm = "¿Transferir esta solicitud a {to_user}?";
      form.dataset.confirmCampos = "to_user";
    }
  }

  /* ------------------------------------------------------------------ */
  /* Escritura                                                           */
  /* ------------------------------------------------------------------ */

  /* Ejecuta un POST de los de siempre y comprueba el resultado contra el panel
     recién recargado: `verifica` recibe los datos nuevos y dice si de verdad
     pasó lo que se pidió. Sin eso, un rechazo silencioso se vería como éxito. */
  function ejecutar(url, cuerpo, opciones) {
    opciones = opciones || {};
    if (estado.enviando) return Promise.resolve(false);
    estado.enviando = true;
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "text/html" },
      body: cuerpo || undefined
    }).then(function (res) {
      var destino = null;
      try { destino = new URL(res.url, window.location.href); } catch (_) { destino = null; }
      if (destino && destino.pathname === "/login") { sesionCaducada(); return false; }
      if (destino && destino.pathname === "/") {
        toast("Esa solicitud ya no está a tu alcance");
        return false;
      }
      var err = destino ? destino.searchParams.get("err") : null;
      var ok = destino ? destino.searchParams.get("ok") : null;
      if (err) {
        toast(ERRORES[err] || "No se pudo completar la acción.");
        return refrescar(false).then(function () { return false; });
      }
      if (!res.ok) { toast("No se pudo completar la acción (" + res.status + ")"); return false; }
      return refrescar(true).then(function (datos) {
        /* Transferir la solicitud propia quita el acceso a la conversación: el
           panel contesta «no existe» y eso ES el éxito. Se vuelve al tablero. */
        if (datos === PERDIDO) {
          toast(opciones.exito || "Listo");
          if (window.CRMKanban && typeof window.CRMKanban.closeChat === "function") {
            window.CRMKanban.closeChat();
          }
          return true;
        }
        if (ok) { toast(EXITOS[ok] || "Listo"); return true; }
        var logrado = !opciones.verifica || (!!datos && opciones.verifica(datos));
        toast(logrado
          ? (opciones.exito || "Listo")
          : (opciones.fallo || "No se pudo aplicar el cambio; el panel quedó actualizado"));
        return logrado;
      });
    }, function () {
      toast("Sin conexión: no se pudo completar la acción");
      return false;
    }).then(function (r) {
      estado.enviando = false;
      return r;
    });
  }

  /* Tras cada acción se repinta el cajón y se avisa al tablero y al chat: el
     estado del folio también vive en la cabecera y en la tarjeta. */
  function refrescar(tocarChat) {
    estado.tocado = false;
    if (window.CRMKanban && typeof window.CRMKanban.reload === "function") {
      window.CRMKanban.reload();
    }
    return cargar({ forzar: true }).then(function (datos) {
      /* El chat se recarga DESPUÉS y solo si la conversación sigue siendo
         nuestra: si no, su API contestaría 404 y ensuciaría la consola. */
      if (datos !== PERDIDO && tocarChat && estado.phone
          && window.CRMKanban && typeof window.CRMKanban.openChat === "function") {
        window.CRMKanban.openChat(estado.phone);
      }
      return datos;
    });
  }

  function campos(pares) {
    var fd = new FormData();
    pares.forEach(function (par) { fd.append(par[0], par[1]); });
    return fd;
  }

  function folioId() {
    return estado.datos && estado.datos.ticket ? estado.datos.ticket.id : null;
  }

  function cambiarEstado(nuevo) {
    var id = folioId();
    if (id === null) return;
    ejecutar("/tickets/" + id + "/status", campos([["status", nuevo]]), {
      exito: "Estado actualizado",
      fallo: "Solo quien atiende la solicitud puede cambiarle el estado",
      verifica: function (d) { return !!d.ticket && d.ticket.status === nuevo; }
    });
  }

  function quitarEtiqueta(et) {
    var id = folioId();
    if (id === null) return;
    ejecutar("/tickets/" + id + "/labels/" + et.id + "/remove", null, {
      exito: "Etiqueta quitada",
      verifica: function (d) {
        return !d.etiquetas.some(function (x) { return x.id === et.id; });
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Cableado                                                            */
  /* ------------------------------------------------------------------ */

  /* acciones.js corre en captura y ya decidió si el confirm se aceptó: si lo
     canceló, el evento llega con defaultPrevented y aquí no se envía nada. */
  function alEnviar(sel, fn) {
    var form = $(sel);
    if (!form) return;
    form.addEventListener("submit", function (e) {
      var cancelado = e.defaultPrevented;
      e.preventDefault();
      if (cancelado) return;
      fn(form);
    });
  }

  function alHacerClic(sel, fn) {
    var btn = $(sel);
    if (!btn) return;
    btn.addEventListener("click", function (e) {
      if (e.defaultPrevented) return;
      fn();
    });
  }

  function cablear() {
    els.acciones.forEach(function (btn) {
      btn.addEventListener("click", function () {
        var seccion = btn.getAttribute("data-seccion");
        if (estado.seccion === seccion) cerrar();
        else abrir(seccion, btn);
      });
    });

    alHacerClic("#crm-panel-close", function () { cerrar(); });
    var velo = $("#crm-panel-scrim");
    if (velo) velo.addEventListener("click", function () { cerrar(); });

    $$("#crm-g-estados button").forEach(function (btn) {
      btn.addEventListener("click", function () {
        cambiarEstado(btn.getAttribute("data-estado"));
      });
    });

    alHacerClic("#crm-g-tomar", function () {
      var id = folioId();
      if (id === null) return;
      ejecutar("/tickets/" + id + "/claim", null, {
        exito: "La solicitud es tuya",
        fallo: "Alguien más la tomó primero",
        verifica: function (d) {
          return !!d.ticket && d.ticket.assigned_to === usuario();
        }
      });
    });

    alHacerClic("#crm-g-terminar", function () {
      var id = folioId();
      if (id === null) return;
      ejecutar("/tickets/" + id + "/finish", campos([["confirm", "1"]]), {
        exito: "Solicitud terminada",
        verifica: function (d) { return !!d.ticket && d.ticket.status === "cerrado"; }
      });
    });

    alHacerClic("#crm-g-soltar", function () {
      var id = folioId();
      if (id === null) return;
      ejecutar("/tickets/" + id + "/release", null, {
        exito: "Solicitud liberada",
        verifica: function (d) { return !!d.ticket && !d.ticket.assigned_to; }
      });
    });

    alHacerClic("#crm-g-agente", function () {
      var id = folioId();
      var antes = estado.datos && estado.datos.ticket ? estado.datos.ticket.agent_active : null;
      if (id === null) return;
      ejecutar("/tickets/" + id + "/agent-toggle", null, {
        exito: antes ? "Agente apagado" : "Agente reanudado",
        verifica: function (d) { return !!d.ticket && d.ticket.agent_active !== antes; }
      });
    });

    alEnviar("#crm-e-form", function (form) {
      var id = folioId();
      var campo = $("#crm-e-nueva");
      var nombre = campo.value.trim();
      if (id === null || !nombre) return;
      ejecutar("/tickets/" + id + "/labels", campos([["nombre", nombre]]), {
        exito: "Etiqueta agregada",
        verifica: function (d) {
          return d.etiquetas.some(function (x) {
            return x.nombre === nombre.toLowerCase();
          });
        }
      }).then(function (ok) { if (ok) form.reset(); });
    });

    alEnviar("#crm-n-form", function (form) {
      var id = folioId();
      var texto = $("#crm-n-texto").value.trim();
      if (id === null || !texto) return;
      ejecutar("/tickets/" + id + "/notes", campos([["note", texto]]), {
        exito: "Nota guardada",
        fallo: "Solo quien atiende la solicitud puede anotar en el folio",
        verifica: function (d) {
          return !!d.ticket && d.ticket.notes.indexOf(texto) !== -1;
        }
      }).then(function (ok) { if (ok) form.reset(); });
    });

    alEnviar("#crm-n-cliente-form", function (form) {
      var texto = $("#crm-n-cliente-texto").value.trim();
      if (!estado.phone || !texto) return;
      ejecutar("/clients/" + estado.phone + "/notes", campos([["body", texto]]), {
        exito: "Nota guardada en la ficha",
        verifica: function (d) {
          return d.notas_cliente.some(function (n) { return n.texto === texto; });
        }
      }).then(function (ok) { if (ok) form.reset(); });
    });

    alEnviar("#crm-s-form", function (form) {
      var id = folioId();
      if (id === null) return;
      var antes = estado.datos ? estado.datos.seguimientos.length : 0;
      ejecutar("/tickets/" + id + "/followup", campos([
        ["days", $("#crm-s-dias").value],
        ["note", $("#crm-s-nota").value.trim()]
      ]), {
        exito: "Recordatorio creado",
        verifica: function (d) { return d.seguimientos.length > antes; }
      }).then(function (ok) { if (ok) form.reset(); });
    });

    alEnviar("#crm-t-form", function () {
      var id = folioId();
      var destino = $("#crm-t-user").value;
      if (id === null || !destino) return;
      ejecutar("/tickets/" + id + "/reassign", campos([
        ["to_user", destino],
        ["note", $("#crm-t-nota").value.trim()]
      ]), {
        exito: "Solicitud transferida a " + destino,
        fallo: "Solo quien atiende la solicitud puede transferirla",
        verifica: function (d) { return !!d.ticket && d.ticket.assigned_to === destino; }
      });
    });

    alEnviar("#crm-c-form", function (form) {
      var id = folioId();
      if (id === null) return;
      var datos = new FormData(form);
      var antes = estado.datos && estado.datos.cobro ? estado.datos.cobro.cobros.length : 0;
      ejecutar("/tickets/" + id + "/cobro", datos, {
        exito: "Cobro generado",
        verifica: function (d) {
          return !!d.cobro && d.cobro.cobros.length > antes;
        }
      });
    });

    /* Lo escrito sin enviar frena el sondeo: no se repinta encima. */
    els.panelBody.addEventListener("input", function () { estado.tocado = true; });

    /* Escape cierra PRIMERO el cajón: en captura, para que el de crm.js —que
       cierra el chat entero— no llegue a correr. Escribiendo no hace nada, que
       es el mismo criterio del chat. */
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !estado.seccion) return;
      var a = document.activeElement;
      if (a && els.panel.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
      e.stopPropagation();
      cerrar();
    }, true);

    /* La conversación la manda crm.js: cambiarla cierra el cajón y recarga. */
    document.addEventListener("crm:chat", function (e) {
      var detalle = e && e.detail ? e.detail : {};
      var phone = detalle.phone || null;
      if (phone && detalle.mismo && phone === estado.phone) return;
      cerrar(true);
      estado.phone = phone;
      estado.datos = null;
      estado.tocado = false;
      estado.seq++;
      els.panel.removeAttribute("data-cargado");
      pintarRiel(null);
      mostrar("#crm-panel-cargando", true);
      if (phone) cargar({ forzar: true });
    });

    setInterval(function () {
      if (!estado.seccion || !estado.phone) return;
      if (document.visibilityState !== "visible") return;
      cargar({});
    }, SONDEO_PANEL_MS);
  }

  function usuario() {
    return window.CRMKanban ? window.CRMKanban.usuario : null;
  }

  function init() {
    els.raiz = document.getElementById("crm");
    els.panel = document.getElementById("crm-panel");
    els.panelBody = document.getElementById("crm-panel-body");
    els.titulo = document.getElementById("crm-panel-title");
    els.acciones = $$("#crm-acciones .accion");
    if (!els.raiz || !els.panel || !els.panelBody || !els.acciones.length) return;
    cablear();
    /* Si crm.js ya abrió una conversación —se llega con ?chat= y la abre dentro
       de su propio DOMContentLoaded, antes de que este módulo exista— su evento
       salió al vacío: el estado inicial se recoge de su API pública. */
    var abierto = window.CRMKanban && typeof window.CRMKanban.getState === "function"
      ? window.CRMKanban.getState().chat
      : null;
    if (abierto) {
      estado.phone = String(abierto);
      cargar({ forzar: true });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
