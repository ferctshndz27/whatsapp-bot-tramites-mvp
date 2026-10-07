/* «Estrategia» en el chat del CRM (/crm): mover la conversación abierta a otra
   etapa del tablero de quien mira, sin volver al tablero.

   Un menú emergente bajo la píldora del riel con una casilla por etapa y su
   contador: todos los chats de esa etapa en TU tablero, sin búsqueda ni
   filtros (GET /crm/api/etapas). Hay una sola marcada —la etapa actual— y no
   se desmarca, porque un chat siempre está en una etapa. Marcar otra MUEVE el
   chat con CRMKanban.moverContacto, el mismo PUT que arrastrar la tarjeta (y
   el mismo aviso «Movido a…»), y el menú se queda abierto con la marca y los
   contadores nuevos (decisiones del operador, 2026-10-06).

   La píldora no lleva data-seccion: no abre el cajón (crm-panel.js) ni se
   apaga sin folio, porque la etapa es del chat y los leads sin folio también
   están en el tablero. Los contadores los da siempre el servidor: un chat con
   todos sus folios cerrados no cuenta en ninguna columna, y sumar y restar
   aquí lo contaría.

   Casillas nativas y no radios: en un grupo de radios las flechas cambian la
   selección, y aquí cada cambio es un movimiento guardado.

   El marcado está en templates/crm.html y el estilo en
   static/css/crm-estrategia.css. El DOM con datos se arma con <template> +
   cloneNode + textContent: el nombre de una etapa lo escribe un usuario. */
(function () {
  "use strict";

  var API_ETAPAS = "/crm/api/etapas";
  var MARGEN = 8;      // px entre el menú y los bordes del chat
  var SEPARACION = 6;  // px entre el botón y el menú

  var els = {};
  var estado = {
    abierto: false,
    phone: null,       // la conversación para la que se abrió
    etapas: null,      // las del servidor en el orden del tablero; null = aún no leídas
    firma: null,       // ids y nombres de las filas pintadas
    actual: null,      // id de la etapa marcada
    movimiento: null,  // {phone, destino, anterior} mientras viaja el PUT
    enfocar: false,    // al abrir, el foco entra en cuanto haya casillas
    focoAlAbrir: null, // dónde estaba el foco al abrir (Safari no enfoca el botón al hacer clic)
    seq: 0             // descarta lecturas superadas
  };

  function $(sel, raiz) { return (raiz || document).querySelector(sel); }
  function $$(sel, raiz) { return Array.prototype.slice.call((raiz || document).querySelectorAll(sel)); }

  function kanban() { return window.CRMKanban || null; }

  function chatAbierto() {
    var k = kanban();
    var s = k && typeof k.getState === "function" ? k.getState() : null;
    return s && s.chat ? String(s.chat) : null;
  }

  /* Mientras viaja un movimiento manda su destino: un chat sin tarjeta no
     sabe la etapa nueva hasta que el servidor contesta. */
  function leerActual(phone) {
    var m = estado.movimiento;
    if (m && m.phone === phone) return m.destino;
    var k = kanban();
    var id = k && typeof k.etapaDe === "function" ? k.etapaDe(phone) : null;
    return id == null ? null : Number(id);
  }

  function etapaPorId(id) {
    var lista = estado.etapas || [];
    for (var i = 0; i < lista.length; i++) {
      if (lista[i].id === id) return lista[i];
    }
    return null;
  }

  function avisar(texto) {
    if (els.estado) els.estado.textContent = texto || "";
  }

  /* ------------------------------------------------------------------ */
  /* Abrir, cerrar y colocar                                             */
  /* ------------------------------------------------------------------ */

  function abrir() {
    var phone = chatAbierto();
    if (!phone) return;
    estado.abierto = true;
    estado.phone = phone;
    estado.actual = leerActual(phone);
    estado.enfocar = true;
    estado.focoAlAbrir = document.activeElement;
    els.boton.setAttribute("aria-expanded", "true");
    els.menu.hidden = false;
    /* Con etapas ya leídas se pinta al instante y se refresca encima. */
    if (estado.etapas) pintar();
    else avisar("Cargando tus etapas…");
    colocar();
    enfocarSiToca();
    cargar();
  }

  function cerrar(devolverFoco) {
    if (!estado.abierto) return;
    estado.abierto = false;
    estado.enfocar = false;
    els.menu.hidden = true;
    els.boton.setAttribute("aria-expanded", "false");
    avisar("");
    if (devolverFoco) {
      try { els.boton.focus({ preventScroll: true }); } catch (_) { els.boton.focus(); }
    }
  }

  /* Bajo el botón, sin salirse del chat: el menú es absoluto dentro de
     #crm-chat (position: relative en crm-panel.css). */
  function colocar() {
    if (!estado.abierto) return;
    var chat = els.chat.getBoundingClientRect();
    var b = els.boton.getBoundingClientRect();
    var izq = Math.min(b.left - chat.left, chat.width - els.menu.offsetWidth - MARGEN);
    els.menu.style.left = Math.round(Math.max(MARGEN, izq)) + "px";
    els.menu.style.top = Math.round(b.bottom - chat.top + SEPARACION) + "px";
  }

  /* Al abrir, el foco entra a la casilla marcada —la etapa actual—, pero solo
     si nadie lo movió desde que se abrió: las etapas pueden tardar en llegar, y
     a quien ya se fue a otra parte no se le jala. */
  function enfocarSiToca() {
    if (!estado.enfocar || !estado.abierto) return;
    var casilla = $(".estrategia-check:checked", els.lista) || $(".estrategia-check", els.lista);
    if (!casilla) return; // aún no hay casillas: se intenta al pintarlas
    estado.enfocar = false;
    var a = document.activeElement;
    var quieto = !a || a === document.body || a === els.boton || a === estado.focoAlAbrir || els.menu.contains(a);
    if (quieto) casilla.focus();
  }

  /* ------------------------------------------------------------------ */
  /* Lectura                                                             */
  /* ------------------------------------------------------------------ */

  function cargar() {
    var k = kanban();
    if (!k || typeof k.api !== "function") return;
    var mio = ++estado.seq;
    k.api("GET", API_ETAPAS).then(function (data) {
      if (mio !== estado.seq) return;
      estado.etapas = normalizar(data);
      if (!estado.abierto) return;
      /* Si al abrir el chat aún no traía su etapa, ya la trae. */
      if (estado.actual === null) estado.actual = leerActual(estado.phone);
      pintar();
      avisar(estado.etapas.length ? "" : "Tu tablero no tiene etapas.");
      colocar();
      enfocarSiToca();
    }, function (err) {
      if (mio !== estado.seq || !estado.abierto) return;
      avisar("No se pudieron cargar tus etapas: " + (err && err.message ? err.message : "error"));
    });
  }

  function normalizar(data) {
    if (!Array.isArray(data)) return [];
    return data.filter(function (e) {
      return e && isFinite(Number(e.id));
    }).map(function (e) {
      var n = Number(e.count);
      return { id: Number(e.id), nombre: String(e.nombre || ""), count: isFinite(n) && n > 0 ? n : 0 };
    });
  }

  /* ------------------------------------------------------------------ */
  /* Pintar                                                              */
  /* ------------------------------------------------------------------ */

  /* Rehace las filas solo si cambió el conjunto de etapas (nombre u orden); si
     no, pone marca y contadores EN SU LUGAR: rehacer la lista le quitaría el
     foco a la casilla que se acaba de marcar. */
  function pintar() {
    var lista = estado.etapas || [];
    var firma = JSON.stringify(lista.map(function (e) { return [e.id, e.nombre]; }));
    if (firma !== estado.firma) {
      var enfocada = idEnfocado();
      while (els.lista.firstChild) els.lista.removeChild(els.lista.firstChild);
      lista.forEach(function (e) {
        var li = els.tpl.content.firstElementChild.cloneNode(true);
        li.setAttribute("data-etapa-id", String(e.id));
        $(".estrategia-nombre", li).textContent = e.nombre;
        els.lista.appendChild(li);
      });
      estado.firma = firma;
      if (enfocada !== null) {
        var misma = $('.estrategia-opcion[data-etapa-id="' + enfocada + '"] .estrategia-check', els.lista);
        if (misma) misma.focus();
      }
    }
    $$(".estrategia-opcion", els.lista).forEach(function (li) {
      var id = Number(li.getAttribute("data-etapa-id"));
      var e = etapaPorId(id);
      var esActual = id === estado.actual;
      var casilla = $(".estrategia-check", li);
      casilla.checked = esActual;
      if (esActual) casilla.setAttribute("aria-disabled", "true");
      else casilla.removeAttribute("aria-disabled");
      li.classList.toggle("is-actual", esActual);
      var n = e ? e.count : 0;
      $(".estrategia-num", li).textContent = String(n);
      $(".estrategia-sr", li).textContent = n === 1 ? " chat" : " chats";
    });
  }

  function idEnfocado() {
    var a = document.activeElement;
    if (!a || !els.lista.contains(a)) return null;
    var li = a.closest(".estrategia-opcion");
    return li ? li.getAttribute("data-etapa-id") : null;
  }

  /* ------------------------------------------------------------------ */
  /* Mover                                                               */
  /* ------------------------------------------------------------------ */

  /* El clic en la etiqueta llega después a la casilla como otro clic: aquí
     solo cuenta ese. */
  function alHacerClic(e) {
    var casilla = e.target && e.target.closest ? e.target.closest(".estrategia-check") : null;
    if (!casilla || !els.lista.contains(casilla)) return;
    var id = Number(casilla.closest(".estrategia-opcion").getAttribute("data-etapa-id"));
    /* La actual no se desmarca, y con un movimiento en vuelo no se empieza otro. */
    if (estado.movimiento || id === estado.actual || !estado.phone) {
      e.preventDefault();
      return;
    }
    mover(id);
  }

  function mover(id) {
    var k = kanban();
    var etapa = etapaPorId(id);
    if (!k || typeof k.moverContacto !== "function" || !etapa) return;
    var phone = estado.phone;
    var mov = { phone: phone, destino: id, anterior: estado.actual };
    estado.movimiento = mov;
    estado.actual = id;
    pintar(); // una sola marcada desde ya
    els.lista.setAttribute("aria-busy", "true");
    k.moverContacto(phone, id, etapa.nombre).then(function (ok) {
      estado.movimiento = null;
      els.lista.removeAttribute("aria-busy");
      if (estado.phone !== phone) return; // se cambió de chat mientras tanto
      if (!ok) {
        /* crm.js ya revirtió la tarjeta y avisó el motivo en el toast. */
        estado.actual = mov.anterior;
        if (estado.abierto) pintar();
        return;
      }
      cargar(); // los contadores del servidor, ya con el chat en su lugar
    });
  }

  /* ------------------------------------------------------------------ */
  /* Cableado                                                            */
  /* ------------------------------------------------------------------ */

  function cablear() {
    els.boton.addEventListener("click", function () {
      if (estado.abierto) cerrar(true);
      else abrir();
    });
    els.lista.addEventListener("click", alHacerClic);

    /* Clic fuera: cierra sin mover el foco, que ya está donde se hizo clic. */
    document.addEventListener("click", function (e) {
      if (!estado.abierto) return;
      if (els.menu.contains(e.target) || els.boton.contains(e.target)) return;
      cerrar(false);
    });

    /* El foco que se va a otra parte —Tab tras la última casilla, Shift+Tab
       antes del botón— también lo cierra: abierto y sin nadie mirándolo solo
       tapa el hilo. Sin relatedTarget (otra ventana, nodo quitado) no se toca. */
    function alPerderFoco(e) {
      var a = e.relatedTarget;
      if (!estado.abierto || !a) return;
      if (els.menu.contains(a) || a === els.boton) return;
      cerrar(false);
    }
    els.menu.addEventListener("focusout", alPerderFoco);
    els.boton.addEventListener("focusout", alPerderFoco);

    /* Escape cierra el menú y NADA más: ni el chat (crm.js) ni el cajón
       (crm-panel.js), que escuchan en document. En captura sobre window este
       va antes que los dos, y ahí se corta. */
    window.addEventListener("keydown", function (e) {
      if (e.key !== "Escape" || !estado.abierto) return;
      if (document.querySelector("#crm dialog[open]")) return;
      e.preventDefault();
      e.stopPropagation();
      var a = document.activeElement;
      cerrar(!!a && (a === els.boton || els.menu.contains(a)));
    }, true);

    /* Otra conversación, o de vuelta al tablero: el menú era de la anterior. */
    document.addEventListener("crm:chat", function (e) {
      var d = e && e.detail ? e.detail : {};
      if (d.phone && d.mismo && d.phone === estado.phone) return;
      cerrar(false);
      estado.phone = d.phone || null;
    });

    /* El botón se mueve si cambia el ancho, si el riel se desplaza o si la
       cabecera del chat crece al llegar sus datos (la fila de trámites). */
    window.addEventListener("resize", colocar);
    if (els.riel) els.riel.addEventListener("scroll", colocar, { passive: true });
    var cabecera = $(".chat-header", els.chat);
    if (cabecera && typeof window.ResizeObserver === "function") {
      new window.ResizeObserver(colocar).observe(cabecera);
    }
  }

  function init() {
    els.chat = document.getElementById("crm-chat");
    els.riel = document.getElementById("crm-acciones");
    els.boton = document.getElementById("crm-btn-estrategia");
    els.menu = document.getElementById("crm-estrategia");
    els.lista = document.getElementById("crm-estrategia-lista");
    els.estado = document.getElementById("crm-estrategia-estado");
    els.tpl = document.getElementById("crm-tpl-estrategia-opcion");
    if (!els.chat || !els.boton || !els.menu || !els.lista || !els.tpl) return;
    cablear();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
