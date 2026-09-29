/* CRM Kanban del ERP (/crm): embudo por etapas, bandeja de chats y panel de
 * conversación en una sola página.
 *
 * Port de crm-kanban/static/js/kanban.js (mismo tablero: pestañas-filtro,
 * bandeja, drag & drop nativo, menú de columna, diálogo de nueva etapa, foco
 * restaurado tras cada repintado y `loadSeq` contra respuestas fuera de orden)
 * más el panel de chat que sustituye a /inbox/{phone}. El tablero es de quien
 * lo mira (app/tablero.py): todos crean, renombran, recolorean, reordenan
 * —arrastrando la cabecera o con «Mover antes/después» del menú ⋮— y dan de
 * baja SUS etapas, sin tocar el tablero de nadie. Un solo archivo, sin
 * frameworks ni build: la CSP del panel es `script-src 'self'`, así que aquí
 * vive TODO el comportamiento y templates/crm.html solo marca.
 *
 * Datos: la página deja un JSON inerte en #crm-datos (usuario, permisos, chat
 * inicial, ventana y respuestas rápidas); el tablero y el chat llegan por
 * GET /crm/api/tablero y GET /crm/api/chat/{phone} (app/crm.py). Todo el DOM
 * con datos se arma con <template> + cloneNode + textContent — nunca innerHTML
 * con datos: un nombre que un cliente escribe por WhatsApp no puede convertirse
 * en marcado dentro de la sesión del asesor.
 *
 * Enviar, reactivar el bot, bloquear y ocultar un mensaje siguen siendo los
 * endpoints de app/inbox.py (fase A de la sustitución): se llaman por fetch con
 * la cookie de sesión y contestan con una redirección 303 que fetch sigue; el
 * resultado se lee de res.url (?err=window|tipo|peso, /login si la sesión
 * caducó, / si no hay acceso a esa conversación).
 *
 * «Mis respuestas» (las guardadas personales, antes /replies) es un <dialog> de
 * esta página: lo abren la barra superior y el final de la fila de respuestas
 * rápidas, y habla con GET/POST/DELETE /crm/api/respuestas. Cada alta o baja
 * devuelve la lista nueva y la fila del compositor, que se repinta sin recargar.
 *
 * Nada aquí recarga la página: el tablero se sondea cada 20 s y el chat abierto
 * cada 8 s, solo con la pestaña visible, nunca a media edición (arrastre,
 * formulario de alta, renombrado, selector de color, menú abierto) y sin tocar
 * jamás la caja de texto del compositor. */
(function () {
  "use strict";

  var VERSION = "1.0.0";
  var API = "/crm/api";
  var DEFAULT_COLOR = "#25D366";
  /* Paleta del prototipo: los dos últimos tonos son el ámbar y el verde
     WhatsApp de las etapas con las que nace el embudo. */
  var PALETTE = ["#1E88E5", "#8E24AA", "#F4511E", "#00897B", "#3949AB", "#D81B60", "#FFB300", "#25D366"];
  var TOAST_MS = 2200;
  var SEARCH_DEBOUNCE_MS = 250;
  var SONDEO_TABLERO_MS = 20000;
  var SONDEO_CHAT_MS = 8000;
  /* Agrupan la ráfaga de avisos del stream (/crm/api/eventos): cinco mensajes seguidos son un
     refetch, no cinco. El chat es lo que se está mirando; el tablero es contexto y pesa más. */
  var COALESCENCIA_CHAT_MS = 350;
  var COALESCENCIA_TABLERO_MS = 2000;
  var ZONA_HORARIA = "America/Mexico_City";
  var ESTADOS_FOLIO = { nuevo: "nuevo", tomado: "tomado", en_proceso: "en proceso", cerrado: "cerrado" };
  /* Miembros: la disponibilidad es la de «Mi día» (User.estado). */
  var ESTADOS_MIEMBRO = { disponible: "disponible", ocupado: "ocupado", ausente: "ausente" };
  var SIN_ASESOR = "__sin_asesor__"; // chip virtual: contactos sin folio asignado
  /* Pestaña «Folios cerrados»: no es una etapa sino otro CONJUNTO. Una conversación con
     todos sus folios cerrados sale del panel (app/crm.py, _tablero) y solo se ve aquí,
     en la búsqueda o abriéndola por enlace, hasta que se abra otro folio. */
  var CERRADAS = "__cerradas__";

  var DATOS = leerDatos();
  var USUARIO = DATOS.usuario || "";
  var ES_ADMIN = !!DATOS.admin;
  var VENTANA_HORAS = DATOS.ventana_horas || 24;
  /* Largos de app/replies.py; la plantilla ya los puso de maxlength. */
  var LIMITES_RESPUESTAS = DATOS.respuestas_limites || {};
  /* Mismos textos que templates/inbox_chat.html: el asesor ve el mismo aviso
     desde cualquiera de las dos vistas mientras conviven. */
  var ERRORES_ENVIO = {
    window: "No se pudo enviar: la ventana de " + VENTANA_HORAS + " h está cerrada. El cliente debe escribir primero.",
    tipo: "Tipo de archivo no permitido. Solo PDF, JPG, PNG, Word o Excel.",
    peso: "Archivo demasiado grande (máx. 5 MB imágenes, 16 MB documentos).",
    acceso: "Sin acceso a esta conversación.",
    sesion: "Tu sesión caducó; vuelve a entrar.",
    red: "No se pudo enviar. Revisa tu conexión."
  };

  var state = {
    etapas: [], contactos: [], counts: {}, total: 0,
    miembros: [], oficinas: [],
    filter: "all", search: "",
    miembro: null,     // username del asesor por el que se filtra (o SIN_ASESOR); null = todos
    chat: null,        // teléfono de la conversación abierta; null = tablero
    chatDatos: null,   // última respuesta de GET /crm/api/chat/{phone}
    conteoAbiertas: null // {counts, total} de la última carga ABIERTA: lo que muestran las
                         // pestañas de etapa mientras se ve «Folios cerrados»
  };

  var els = {};
  var draggingId = null;     // teléfono de la tarjeta en arrastre; null = no hay arrastre propio
  var draggingColumnId = null; // id de la etapa cuya cabecera se arrastra; null = ninguna
  var toastTimer = null;
  var searchTimer = null;
  var loadSeq = 0;           // ignora respuestas del tablero ya superadas
  var chatSeq = 0;           // ídem para el chat
  var ultimoTablero = null;  // JSON del último tablero pintado: sin cambios no se repinta
  var tableroPendiente = null; // tablero recibido mientras había una edición a medias
  var timerPendiente = null;
  var ultimoHilo = null;     // firma del hilo pintado
  var ultimaCabecera = null; // firma de la cabecera del chat pintada
  var timerAvisoChat = null;    // agrupan los avisos del stream (conectarEventos)
  var timerAvisoTablero = null;
  var ultimasRespuestas = null;
  var enviando = false;
  var respuestasSeq = 0;     // ignora una lista de «Mis respuestas» ya superada
  var guardandoRespuesta = false;
  var totalRespuestas = 0;   // cuántas guardadas pinta el diálogo…
  var topeRespuestas = LIMITES_RESPUESTAS.max || 0; // …y cuántas caben (lo repite cada respuesta del servidor)
  var abridorRespuestas = null; // a quién le vuelve el foco al cerrar el diálogo

  var rtf = (typeof Intl !== "undefined" && typeof Intl.RelativeTimeFormat === "function")
    ? new Intl.RelativeTimeFormat("es-MX", { numeric: "auto", style: "short" })
    : null;
  /* Para 2–6 días se quiere «hace N días»; con numeric:"auto" ICU diría
     «anteayer», así que esos van con numeric:"always". */
  var rtfDays = rtf ? new Intl.RelativeTimeFormat("es-MX", { numeric: "always", style: "short" }) : null;
  var dtf = null;
  var dtfHora = null;
  try {
    dtf = new Intl.DateTimeFormat("es-MX", { day: "numeric", month: "short", timeZone: ZONA_HORARIA });
    dtfHora = new Intl.DateTimeFormat("es-MX", {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: ZONA_HORARIA
    });
  } catch (_) { /* sin datos de zona horaria: se usa la hora local del navegador */ }

  /* ------------------------------------------------------------------ */
  /* Utilidades                                                          */
  /* ------------------------------------------------------------------ */

  function leerDatos() {
    var el = document.getElementById("crm-datos");
    if (!el) return {};
    try { return JSON.parse(el.textContent || "{}") || {}; } catch (_) { return {}; }
  }

  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function tpl(id) {
    var t = document.getElementById(id);
    if (!t || !t.content || !t.content.firstElementChild) return null;
    return t.content.firstElementChild.cloneNode(true);
  }

  function setText(root, sel, text) {
    var el = $(sel, root);
    if (el) el.textContent = text;
  }

  /* Etiqueta del alias interno (Client.alias) debajo del nombre: mismo markup que
     templates/partials/alias.html —🏷️ decorativo y «Alias:» para el lector de
     pantalla—, armado con nodos de texto: el alias lo escribe una persona. */
  function fillAlias(el, c) {
    if (!el) return;
    clear(el);
    var alias = c && c.alias;
    el.hidden = !alias;
    if (!alias) return;
    var ico = document.createElement("span");
    ico.setAttribute("aria-hidden", "true");
    ico.textContent = "🏷️ ";
    var sr = document.createElement("span");
    sr.className = "sr-only";
    sr.textContent = "Alias: ";
    el.appendChild(ico);
    el.appendChild(sr);
    el.appendChild(document.createTextNode(alias));
  }

  function clear(el) {
    while (el && el.firstChild) el.removeChild(el.firstChild);
  }

  function quitar(el) {
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }

  function toast(msg) {
    var el = els.toast;
    if (!el) return;
    el.textContent = msg;
    el.classList.add("is-visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.classList.remove("is-visible"); }, TOAST_MS);
  }

  function mensajeDe(err, porDefecto) {
    return (err && err.message) ? err.message : porDefecto;
  }

  /* Sesión vencida: mejor volver al login que seguir pintando errores. */
  function sesionCaducada() {
    toast(ERRORES_ENVIO.sesion);
    setTimeout(function () { window.location.reload(); }, 900);
  }

  /* Llamada JSON a la API del CRM. Rechaza con Error(mensaje en español) si la
     respuesta no es 2xx; el mensaje sale del {"error": …} del servidor. */
  function api(method, url, body) {
    var opts = { method: method, headers: { Accept: "application/json" }, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    return fetch(url, opts).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (res.status === 403 && data && data.error === "Sesión requerida") {
          sesionCaducada();
          throw new Error(ERRORES_ENVIO.sesion);
        }
        if (!res.ok) {
          throw new Error((data && typeof data.error === "string") ? data.error : ("Error " + res.status));
        }
        return data;
      });
    }, function () {
      throw new Error("Sin conexión con el servidor");
    });
  }

  /* Acciones de la bandeja (app/inbox.py): POST con redirección 303 que fetch
     sigue. Devuelve {ok, err} leído de la URL final, nunca lanza. */
  function accionBandeja(url, cuerpo) {
    /* X-Requested-With: fetch → el servidor contesta 204 con el destino en una cabecera en vez
       del 303 (app/main.py::_fetch_sin_html). Sin eso, seguir el redirect descargaba /crm
       entero, 32 KB, por cada acción, solo para leer el ?err= de la URL final. */
    var opts = {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "text/html", "X-Requested-With": "fetch" }
    };
    if (cuerpo !== undefined) opts.body = cuerpo;
    return fetch(url, opts).then(function (res) {
      var destino;
      /* Si el 204 no llegara —un proxy que se coma la cabecera, el middleware apagado—, res.url
         sigue siendo la URL final del redirect seguido y todo lo de abajo funciona igual. */
      var anunciado = res.headers.get("X-Destino");
      try { destino = new URL(anunciado || res.url, window.location.href); } catch (_) { destino = null; }
      if (destino && destino.pathname === "/login") return { ok: false, err: "sesion" };
      if (destino && destino.pathname === "/") return { ok: false, err: "acceso" };
      var err = destino ? destino.searchParams.get("err") : null;
      if (err) return { ok: false, err: err };
      if (!res.ok) return { ok: false, err: "http" + res.status };
      return { ok: true, err: null };
    }, function () {
      return { ok: false, err: "red" };
    });
  }

  function textoError(err) {
    if (err === "sesion") sesionCaducada();
    return ERRORES_ENVIO[err] || ("No se pudo completar la acción (" + err + ").");
  }

  function initialsOf(name) {
    var words = String(name || "").trim().split(/\s+/).filter(Boolean);
    if (!words.length) return "?";
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0].charAt(0) + words[1].charAt(0)).toUpperCase();
  }

  function hexToRgba(hex, alpha) {
    var m = /^#([0-9a-f]{6})$/i.exec(String(hex || "").trim());
    if (!m) return "rgba(37, 211, 102, " + alpha + ")";
    var n = parseInt(m[1], 16);
    return "rgba(" + ((n >> 16) & 255) + ", " + ((n >> 8) & 255) + ", " + (n & 255) + ", " + alpha + ")";
  }

  function normalizeHex(color) {
    var v = String(color || "").trim().toUpperCase();
    return /^#[0-9A-F]{6}$/.test(v) ? v : DEFAULT_COLOR;
  }

  /* Teléfono canónico (52 + 10 dígitos) a la forma en que lo lee una persona. */
  function formatoTelefono(phone) {
    var d = String(phone || "").replace(/\D/g, "");
    if (d.length === 12 && d.slice(0, 2) === "52") {
      return "+52 " + d.slice(2, 4) + " " + d.slice(4, 8) + " " + d.slice(8);
    }
    if (d.length === 13 && d.slice(0, 3) === "521") {
      return "+52 1 " + d.slice(3, 5) + " " + d.slice(5, 9) + " " + d.slice(9);
    }
    if (d.length === 10) return d.slice(0, 2) + " " + d.slice(2, 6) + " " + d.slice(6);
    return String(phone || "");
  }

  function nombreDe(c) {
    return (c && c.name) ? c.name : formatoTelefono(c ? c.phone : "");
  }

  /* Tiempo relativo en es-MX: «ahora», «hace N min», «hace N h», «ayer»,
     «hace N días» o fecha corta «13 sep». */
  function relativeTime(iso, now) {
    if (!iso) return "";
    var date = new Date(iso);
    var t = date.getTime();
    if (isNaN(t)) return "";
    var diff = Math.max(0, now - t);
    var min = Math.floor(diff / 60000);
    if (min < 1) return "ahora";
    if (min < 60) return rtf ? rtf.format(-min, "minute") : "hace " + min + " min";
    var hours = Math.floor(min / 60);
    if (hours < 24) return rtf ? rtf.format(-hours, "hour") : "hace " + hours + " h";
    var days = Math.floor(hours / 24);
    if (days === 1) return rtf ? rtf.format(-1, "day") : "ayer";
    if (days < 7) return rtfDays ? rtfDays.format(-days, "day") : "hace " + days + " días";
    return dtf ? dtf.format(date).replace(/\.$/, "") : (date.getDate() + "/" + (date.getMonth() + 1));
  }

  /* Hora local del negocio para el pie de cada burbuja: «13 sep, 15:04». */
  function fechaHora(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return "";
    if (dtfHora) return dtfHora.format(date).replace(/\.,/, ",").replace(/\.$/, "");
    return date.toLocaleString("es-MX");
  }

  function setTiempo(el, iso, now) {
    if (!el) return;
    if (iso) el.dataset.iso = iso; else delete el.dataset.iso;
    el.textContent = relativeTime(iso, now);
  }

  /* Los «hace 5 min» envejecen aunque el tablero no cambie: se refrescan solos. */
  function refrescarTiempos() {
    var now = Date.now();
    $$("[data-iso]", els.root).forEach(function (el) {
      el.textContent = relativeTime(el.dataset.iso, now);
    });
  }

  /* Vista previa del último mensaje y sus palomitas. */
  function previewOf(msg) {
    if (!msg) return { ticks: "", read: false, text: "Sin mensajes" };
    var media = msg.media_path ? String(msg.media_path) : "";
    var mime = msg.media_mime ? String(msg.media_mime) : "";
    var text;
    if (mime) text = /^image\//.test(mime) ? "📷 Imagen" : "📄 Documento";
    else if (media && /\.pdf$/i.test(media)) text = "📄 Documento";
    else if (media) text = "📷 Imagen";
    else if (msg.body === "[archivo adjunto]") text = "📎 Adjunto";
    else text = (msg.body !== null && msg.body !== undefined && String(msg.body).trim()) ? String(msg.body) : "Mensaje";
    var ticks = "";
    var read = false;
    if (msg.direction === "out") {
      if (msg.wa_status === "sent") ticks = "✓ ";
      else if (msg.wa_status === "delivered") ticks = "✓✓ ";
      else if (msg.wa_status === "read") { ticks = "✓✓ "; read = true; }
    }
    return { ticks: ticks, read: read, text: text };
  }

  function fillPreview(el, pv) {
    if (!el) return;
    clear(el);
    if (pv.ticks) {
      var t = document.createElement("span");
      t.className = "ticks" + (pv.read ? " read" : "");
      t.title = pv.read ? "Leído" : (pv.ticks === "✓ " ? "Enviado" : "Entregado");
      t.textContent = pv.ticks;
      el.appendChild(t);
    }
    el.appendChild(document.createTextNode(pv.text));
  }

  function fillAvatar(el, c) {
    if (!el) return;
    el.textContent = (c && c.initials) || initialsOf(c && c.name);
    el.style.background = normalizeHex((c && c.avatar_color) || "#128C7E");
  }

  /* Insignias cortas de tarjeta y bandeja: estado del folio con su asesor, bot
     pausado, canal Twilio, ventana caducada, bloqueo y etiquetas del folio. */
  function fillBadges(el, c) {
    if (!el) return;
    clear(el);
    var chips = [];
    if (c.ticket) {
      var estado = ESTADOS_FOLIO[c.ticket.status] || String(c.ticket.status || "");
      chips.push({
        text: c.ticket.assigned_to ? estado + " · " + c.ticket.assigned_to : estado,
        cls: "chip-" + String(c.ticket.status || "").replace(/[^a-z_]/g, "")
      });
    }
    if (c.bot_pausado) chips.push({ text: "bot pausado", cls: "chip-bot" });
    if (c.provider === "twilio") chips.push({ text: "Twilio", cls: "chip-twilio" });
    if (c.ventana && !c.ventana.abierta && !c.ventana.nunca) chips.push({ text: "caducó", cls: "chip-caduco" });
    if (c.bloqueado) chips.push({ text: "bloqueado", cls: "chip-bloqueado" });
    (Array.isArray(c.labels) ? c.labels : []).forEach(function (label) {
      chips.push({ text: String(label), cls: "" });
    });
    chips.forEach(function (ch) {
      var s = document.createElement("span");
      s.className = "chip" + (ch.cls ? " " + ch.cls : "");
      s.textContent = ch.text;
      el.appendChild(s);
    });
  }

  function lastActivity(c) {
    return (c.last_message && c.last_message.created_at) || c.created_at || null;
  }

  function countFor(etapaId) {
    var n = Number(state.counts[String(etapaId)]);
    return isFinite(n) ? n : 0;
  }

  function bumpCount(etapaId, delta) {
    state.counts[String(etapaId)] = countFor(etapaId) + delta;
  }

  function etapaById(id) {
    var n = Number(id);
    for (var i = 0; i < state.etapas.length; i++) {
      if (state.etapas[i].id === n) return state.etapas[i];
    }
    return null;
  }

  function contactoByPhone(phone) {
    var p = String(phone);
    for (var i = 0; i < state.contactos.length; i++) {
      if (state.contactos[i].phone === p) return state.contactos[i];
    }
    return null;
  }

  /* ------------------------------------------------------------------ */
  /* Carga y render del tablero                                          */
  /* ------------------------------------------------------------------ */

  /* `silencioso`: es el sondeo. Sin cambios no repinta, y un fallo de red no
     molesta con un toast (el siguiente sondeo lo reintenta). */
  function load(opts) {
    opts = opts || {};
    var seq = ++loadSeq;
    /* El conjunto se fija al pedir y viaja con la respuesta: se aplica como lo que se
       pidió aunque la pestaña cambie mientras tanto (loadSeq descarta la vieja igual). */
    var cerradas = state.filter === CERRADAS;
    var params = [];
    if (state.search) params.push("q=" + encodeURIComponent(state.search));
    if (cerradas) params.push("cerradas=1");
    return api("GET", API + "/tablero" + (params.length ? "?" + params.join("&") : ""))
      .then(function (data) {
        if (seq !== loadSeq) return; // llegó una carga más nueva
        var firma = JSON.stringify(data);
        if (opts.silencioso && firma === ultimoTablero) return;
        /* El repintado destruye el nodo que se está editando (renombrar, alta,
           color, menú, arrastre). Una respuesta que llega a media edición —la
           recarga tras mover una tarjeta, o el sondeo— espera a que termine. */
        if (tableroOcupado()) {
          tableroPendiente = { data: data, firma: firma, cerradas: cerradas };
          programarPendiente();
          return;
        }
        aplicarTablero(data, firma, cerradas);
      })
      .catch(function (err) {
        if (seq !== loadSeq || opts.silencioso) return;
        toast("No se pudo cargar el tablero: " + mensajeDe(err, "error"));
        if (els.board && !$(".column", els.board)) {
          clear(els.board);
          var p = document.createElement("p");
          p.className = "board-loading";
          p.textContent = "No se pudo cargar el tablero. Recarga la página para intentarlo de nuevo.";
          els.board.appendChild(p);
        }
      });
  }

  function aplicarTablero(data, firma, cerradas) {
    tableroPendiente = null;
    ultimoTablero = firma;
    state.etapas = (data && Array.isArray(data.etapas)) ? data.etapas : [];
    state.contactos = (data && Array.isArray(data.contactos)) ? data.contactos : [];
    state.counts = (data && data.counts && typeof data.counts === "object") ? data.counts : {};
    state.total = (data && isFinite(Number(data.total))) ? Number(data.total) : state.contactos.length;
    state.miembros = (data && Array.isArray(data.miembros)) ? data.miembros : [];
    state.oficinas = (data && Array.isArray(data.oficinas)) ? data.oficinas : [];
    if (!cerradas) state.conteoAbiertas = { counts: state.counts, total: state.total };
    if (state.filter !== "all" && state.filter !== CERRADAS && !etapaById(state.filter)) state.filter = "all";
    if (state.miembro !== null && state.miembro !== SIN_ASESOR && !miembroPorNombre(state.miembro)) state.miembro = null;
    render();
  }

  function programarPendiente() {
    if (timerPendiente) return;
    timerPendiente = setTimeout(function () {
      timerPendiente = null;
      if (!tableroPendiente) return;
      if (tableroOcupado()) { programarPendiente(); return; }
      var p = tableroPendiente;
      aplicarTablero(p.data, p.firma, p.cerradas);
    }, 400);
  }

  function render() {
    renderMiembros();
    renderTabs();
    renderBoard();
    renderTray();
    marcarChatAbierto();
  }

  /* --- pestañas-filtro --- */

  /* Pinta en #crm-tabs-list, que se vacía entero en cada repintado. El botón
     «Tablero» vive en la misma fila (#crm-tabs) pero como hermano de la lista:
     si estuviera dentro, clear() lo borraría y abrirChat() mostraría un botón
     que ya no está en el documento. El teclado del tablist tampoco lo alcanza,
     porque el keydown se escucha en la lista. */
  function renderTabs() {
    var nav = els.tabs;
    if (!nav) return;
    clear(nav);
    var frag = document.createDocumentFragment();
    var visibles = contactosVisibles();
    var enCerradas = state.filter === CERRADAS;
    /* En «Folios cerrados» el tablero muestra otro conjunto: las pestañas de etapa siguen
       contando la cola ABIERTA, o «Todos 2» ahí diría algo falso. */
    var abiertas = enCerradas ? state.conteoAbiertas : null;
    var total = abiertas ? abiertas.total : (state.miembro === null ? state.total : visibles.length);
    frag.appendChild(makeTab("all", "Todos", total, "var(--wa-teal)"));
    state.etapas.forEach(function (etapa) {
      var n = abiertas ? (Number(abiertas.counts[String(etapa.id)]) || 0) : conteoEtapa(etapa.id, visibles);
      frag.appendChild(makeTab(String(etapa.id), etapa.nombre, n, etapa.color));
    });
    /* Sin conteo mientras no se abre: contarlas costaría una consulta más en cada sondeo. */
    frag.appendChild(makeTab(CERRADAS, "Folios cerrados", enCerradas ? visibles.length : null, "var(--wa-muted)"));
    nav.appendChild(frag);
  }

  function makeTab(filter, name, count, color) {
    var tab = tpl("crm-tpl-tab");
    tab.dataset.filter = filter;
    var dot = $(".tab-dot", tab);
    if (dot) dot.style.background = color;
    setText(tab, ".tab-name", name);
    var num = $(".tab-count", tab);
    if (num) {
      num.hidden = count === null;
      num.textContent = count === null ? "" : String(count);
    }
    var active = state.filter === filter;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.setAttribute("aria-controls", "crm-tray");
    tab.tabIndex = active ? 0 : -1; // roving tabindex: una sola parada de Tab, flechas para moverse
    tab.addEventListener("click", function () { setFilter(filter); });
    return tab;
  }

  /* Flechas, Inicio y Fin dentro de la lista de pestañas (patrón ARIA tablist
     con selección automática: la pestaña enfocada pasa a ser el filtro). */
  function onTabsKeydown(e) {
    var delta = { ArrowRight: 1, ArrowLeft: -1, Home: 0, End: 0 };
    if (!(e.key in delta) || !els.tabs) return;
    var tabs = $$(".tab", els.tabs);
    if (!tabs.length) return;
    var i = tabs.indexOf(document.activeElement);
    if (i < 0) {
      for (var k = 0; k < tabs.length; k++) if (tabs[k].classList.contains("is-active")) i = k;
      if (i < 0) i = 0;
    }
    var next = e.key === "Home" ? 0
      : e.key === "End" ? tabs.length - 1
      : (i + delta[e.key] + tabs.length) % tabs.length;
    e.preventDefault();
    tabs[next].focus();
    setFilter(tabs[next].dataset.filter);
  }

  /* Marca la pestaña activa en sitio: no se reconstruye el nav, así una
     referencia tomada antes del clic sigue siendo el botón vivo. */
  function updateTabs() {
    if (!els.tabs) return;
    $$(".tab", els.tabs).forEach(function (tab) {
      var active = tab.dataset.filter === state.filter;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.tabIndex = active ? 0 : -1;
    });
  }

  function setFilter(filter) {
    var cambiaConjunto = (filter === CERRADAS) !== (state.filter === CERRADAS);
    state.filter = filter;
    updateTabs();
    if (cambiaConjunto) {
      /* Entrar o salir de «Folios cerrados» cambia QUÉ conversaciones hay, no solo cuáles
         se ven: se vuelve a pedir el tablero y lo repinta aplicarTablero. */
      ultimoTablero = null;
      load();
      return;
    }
    renderTray();
    marcarChatAbierto();
    if (!els.board) return;
    var focused = null;
    $$(".column", els.board).forEach(function (section) {
      var on = filter !== "all" && section.dataset.columnId === filter;
      section.classList.toggle("is-focused", on);
      if (on) focused = section;
    });
    if (focused && !els.board.hidden && typeof focused.scrollIntoView === "function") {
      focused.scrollIntoView({ inline: "center", block: "nearest" });
    }
  }

  /* --- miembros de la oficina --- */

  function miembroPorNombre(username) {
    for (var i = 0; i < state.miembros.length; i++) {
      if (state.miembros[i].username === username) return state.miembros[i];
    }
    return null;
  }

  function atendidoPor(c) {
    return (c.ticket && c.ticket.assigned_to) ? c.ticket.assigned_to : null;
  }

  /* Filtro por miembro: sus clientes son los que tienen el folio vigente
     asignado a él; «Sin asesor», los que no tienen folio o lo tienen libre. */
  function contactosVisibles() {
    if (state.miembro === null) return state.contactos;
    return state.contactos.filter(function (c) {
      var asesor = atendidoPor(c);
      return state.miembro === SIN_ASESOR ? asesor === null : asesor === state.miembro;
    });
  }

  function conteoEtapa(etapaId, visibles) {
    if (state.miembro === null) return countFor(etapaId);
    var n = 0;
    (visibles || contactosVisibles()).forEach(function (c) {
      if (String(c.etapa_id) === String(etapaId)) n++;
    });
    return n;
  }

  function renderMiembros() {
    var nav = els.miembros;
    if (!nav) return;
    clear(nav);
    if (!state.miembros.length) return;
    var frag = document.createDocumentFragment();
    var varias = state.oficinas.length > 1;
    var oficinaActual = null;
    state.miembros.forEach(function (m) {
      if (varias && m.office_id !== oficinaActual) {
        oficinaActual = m.office_id;
        var etiqueta = document.createElement("span");
        etiqueta.className = "miembros-oficina";
        etiqueta.textContent = m.oficina;
        frag.appendChild(etiqueta);
      }
      frag.appendChild(makeMiembro(m));
    });
    var sinAsesor = 0;
    state.contactos.forEach(function (c) { if (atendidoPor(c) === null) sinAsesor++; });
    frag.appendChild(makeMiembro({
      username: SIN_ASESOR, nombre: "Sin asesor", initials: "–", avatar_color: "#54656F",
      estado: null, atiende: sinAsesor
    }));
    nav.appendChild(frag);
  }

  function makeMiembro(m) {
    var b = tpl("crm-tpl-miembro");
    var nombre = m.nombre || m.username;
    var activo = state.miembro === m.username;
    b.dataset.username = m.username;
    b.classList.toggle("is-active", activo);
    b.setAttribute("aria-pressed", activo ? "true" : "false");
    var avatar = $(".miembro-avatar", b);
    if (avatar) {
      avatar.textContent = m.initials || initialsOf(nombre);
      avatar.style.background = normalizeHex(m.avatar_color || "#128C7E");
    }
    setText(b, ".miembro-nombre", nombre);
    var estado = $(".miembro-estado", b);
    if (estado) {
      if (m.estado) {
        var texto = ESTADOS_MIEMBRO[m.estado] || m.estado;
        estado.classList.add(m.estado);
        estado.setAttribute("aria-label", texto);
        estado.title = texto;
      } else {
        quitar(estado);
      }
    }
    setText(b, ".miembro-count", String(m.atiende));
    b.title = nombre + (m.estado ? " · " + (ESTADOS_MIEMBRO[m.estado] || m.estado) : "")
      + " · atiende " + m.atiende + (m.atiende === 1 ? " chat" : " chats");
    b.setAttribute("aria-label", (activo ? "Quitar el filtro de " : "Ver solo los chats de ") + nombre);
    b.addEventListener("click", function () { setMiembro(activo ? null : m.username, m.username); });
    return b;
  }

  /* El repintado destruye el chip pulsado: el foco vuelve al equivalente nuevo. */
  function setMiembro(username, enfocar) {
    state.miembro = username;
    render();
    var chip = (enfocar && els.miembros) ? $('.miembro[data-username="' + enfocar + '"]', els.miembros) : null;
    if (chip) {
      try { chip.focus({ preventScroll: true }); } catch (_) { chip.focus(); }
    }
  }

  /* --- bandeja --- */

  function renderTray() {
    var list = els.trayList;
    if (!list) return;
    clear(list);
    var visibles = contactosVisibles();
    var items = state.filter === "all" || state.filter === CERRADAS
      ? visibles
      : visibles.filter(function (c) { return String(c.etapa_id) === state.filter; });
    if (els.trayCount) {
      els.trayCount.textContent = !items.length ? "" : (items.length === 1 ? "1 chat" : items.length + " chats");
    }
    if (!items.length) {
      var li = document.createElement("li");
      li.className = "tray-empty";
      if (state.filter === CERRADAS) {
        li.textContent = state.search ? "Sin folios cerrados que coincidan" : "Sin folios cerrados";
      } else if (!ES_ADMIN && !state.contactos.length && !state.search) {
        li.textContent = "Sin conversaciones todavía: aquí aparecen las de las solicitudes que tomas.";
      } else {
        li.textContent = (state.search || state.miembro !== null) ? "Sin contactos que coincidan" : "Sin contactos en esta etapa";
      }
      list.appendChild(li);
      return;
    }
    var now = Date.now();
    var frag = document.createDocumentFragment();
    items.forEach(function (c) { frag.appendChild(makeTrayItem(c, now)); });
    list.appendChild(frag);
  }

  /* Nombre accesible de una tarjeta o ítem de bandeja: qué abre y qué trae. */
  function accessibleName(c) {
    var extra = "";
    if (Number(c.unread) > 0) extra += ", " + c.unread + " sin leer";
    if (c.pinned) extra += ", fijado";
    return "Abrir chat con " + nombreDe(c) + (c.alias ? " (alias " + c.alias + ")" : "") + extra;
  }

  function makeTrayItem(c, now) {
    var li = tpl("crm-tpl-tray-item");
    li.dataset.contactId = c.phone;
    li.setAttribute("role", "button");
    li.setAttribute("aria-label", accessibleName(c));
    fillAvatar($(".avatar", li), c);
    setText(li, ".tray-name", nombreDe(c));
    fillAlias($(".alias-tag", li), c);
    setTiempo($(".tray-time", li), lastActivity(c), now);
    fillPreview($(".tray-preview", li), previewOf(c.last_message));
    fillBadges($(".tray-badges", li), c);
    var pin = $(".tray-pin", li);
    if (pin && !c.pinned) quitar(pin);
    var unread = $(".tray-unread", li);
    if (unread) {
      if (Number(c.unread) > 0) {
        unread.textContent = String(c.unread);
        unread.setAttribute("aria-label", c.unread + " sin leer");
      } else {
        quitar(unread);
      }
    }
    li.addEventListener("click", function () { openChat(c.phone); });
    li.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openChat(c.phone);
      }
    });
    return li;
  }

  /* El chat abierto se distingue en la bandeja y en su tarjeta. */
  function marcarChatAbierto() {
    if (!els.root) return;
    $$(".tray-item, .card", els.root).forEach(function (el) {
      var abierto = state.chat !== null && el.dataset.contactId === state.chat;
      el.classList.toggle("is-open", abierto);
      if (abierto) el.setAttribute("aria-current", "true");
      else el.removeAttribute("aria-current");
    });
  }

  /* --- tablero --- */

  /* El re-render destruye el nodo enfocado (el select «Mover a…», el input de
     renombrar, el formulario de alta) y el foco caería a <body>. Se guarda un
     descriptor y se devuelve al equivalente nuevo. */
  function captureBoardFocus() {
    var a = document.activeElement;
    if (!a || !els.board || !els.board.contains(a) || typeof a.closest !== "function") return null;
    var card = a.closest(".card");
    var column = a.closest(".column");
    return {
      contactId: card ? card.dataset.contactId : null,
      columnId: column ? column.dataset.columnId : null,
      inAddForm: !!a.closest(".add-contact-form"),
      selector: a.classList.length ? "." + a.classList[0] : a.tagName.toLowerCase()
    };
  }

  function restoreBoardFocus(f) {
    if (!f || !els.board) return;
    var root = null;
    if (f.contactId) root = $('.card[data-contact-id="' + f.contactId + '"]', els.board);
    if (!root && f.columnId) root = $('.column[data-column-id="' + f.columnId + '"]', els.board);
    if (!root) return;
    var target = null;
    if (f.inAddForm) target = $(".btn-add-contact", root);
    else if (f.selector) target = $(f.selector, root);
    if (!target || target.hidden) {
      target = root.classList.contains("column") ? ($(".column-menu-btn", root) || $(".btn-add-contact", root) || root) : root;
    }
    if (target && typeof target.focus === "function") {
      try { target.focus({ preventScroll: true }); } catch (_) { target.focus(); }
    }
  }

  function renderBoard() {
    var board = els.board;
    if (!board) return;
    var focus = captureBoardFocus();
    var scrollLeft = board.scrollLeft;
    var bodyScroll = {};
    $$(".column", board).forEach(function (section) {
      var body = $(".column-body", section);
      if (body) bodyScroll[section.dataset.columnId] = body.scrollTop;
    });
    clear(board);

    var byEtapa = {};
    contactosVisibles().forEach(function (c) {
      var key = String(c.etapa_id);
      (byEtapa[key] = byEtapa[key] || []).push(c);
    });

    var now = Date.now();
    var frag = document.createDocumentFragment();
    state.etapas.forEach(function (etapa) {
      frag.appendChild(makeColumn(etapa, byEtapa[String(etapa.id)] || [], now));
    });
    if (!state.etapas.length) {
      var p = document.createElement("p");
      p.className = "board-loading";
      p.textContent = "No hay etapas en el embudo.";
      frag.appendChild(p);
    }
    board.appendChild(frag);

    board.scrollLeft = scrollLeft;
    $$(".column", board).forEach(function (section) {
      var body = $(".column-body", section);
      var top = bodyScroll[section.dataset.columnId];
      if (body && top) body.scrollTop = top;
    });
    restoreBoardFocus(focus);
  }

  function makeColumn(etapa, contactos, now) {
    var section = tpl("crm-tpl-column");
    var color = normalizeHex(etapa.color);
    section.dataset.columnId = String(etapa.id);
    section.style.setProperty("--col-color", color);
    section.style.setProperty("--col-soft", hexToRgba(color, 0.14));
    section.classList.toggle("is-focused", state.filter === String(etapa.id));
    section.setAttribute("aria-label", "Etapa " + etapa.nombre);
    setText(section, ".column-name", etapa.nombre);
    setText(section, ".column-count", String(state.miembro === null ? countFor(etapa.id) : contactos.length));

    var body = $(".column-body", section);
    if (body) {
      if (!contactos.length) {
        var empty = document.createElement("p");
        empty.className = "column-empty";
        empty.textContent = "Arrastra aquí un contacto";
        body.appendChild(empty);
      } else {
        contactos.forEach(function (c) { body.appendChild(makeCard(c, etapa, color, now)); });
      }
    }

    /* Las etapas son de quien mira el tablero (app/tablero.py): el menú ⋮ y el
       arrastre de la cabecera son de todos, admins o no. */
    wireColumnMenu(section, etapa);
    wireColumnDrag(section, etapa);
    /* Dar de alta es de los admins: un colaborador solo ve las conversaciones
       de las solicitudes que toma, y un contacto sin folio nacería invisible
       para él. */
    if (ES_ADMIN) wireAddContact(section, etapa);
    else quitar($(".column-footer", section));
    wireColumnDnD(section);
    return section;
  }

  function wireColumnMenu(section, etapa) {
    var menuBtn = $(".column-menu-btn", section);
    var menu = $(".column-menu", section);
    if (!menuBtn || !menu) return;

    menuBtn.addEventListener("click", function (e) {
      e.stopPropagation();
      var wasHidden = menu.hidden;
      closeAllMenus();
      closeColorPickers();
      if (wasHidden) {
        menu.hidden = false;
        menuBtn.setAttribute("aria-expanded", "true");
        var first = $("button", menu);
        if (first) first.focus();
      }
    });
    menu.addEventListener("click", function (e) { e.stopPropagation(); });

    var rename = $(".act-rename", menu);
    if (rename) rename.addEventListener("click", function () { closeAllMenus(); startRename(section, etapa); });

    var colorBtn = $(".act-color", menu);
    if (colorBtn) colorBtn.addEventListener("click", function () { closeAllMenus(); openColorPicker(section, etapa); });

    /* La alternativa al arrastre para teclado y pantallas táctiles. En los
       extremos el botón existe pero no hace nada, y lo dice. */
    var posicion = posicionDe(etapa.id);
    [[".act-left", -1, posicion === 0, "Ya es la primera etapa"],
     [".act-right", 1, posicion === state.etapas.length - 1, "Ya es la última etapa"]].forEach(function (d) {
      var b = $(d[0], menu);
      if (!b) return;
      if (d[2]) {
        b.disabled = true;
        b.title = d[3];
      } else {
        b.addEventListener("click", function () { moverColumna(etapa, d[1]); });
      }
    });

    var del = $(".act-delete", menu);
    if (del) {
      if (state.etapas.length <= 1) {
        del.disabled = true;
        del.title = "No se puede eliminar la única etapa";
      } else {
        del.addEventListener("click", function () { closeAllMenus(); deleteColumn(etapa); });
      }
    }
  }

  function wireAddContact(section, etapa) {
    var addBtn = $(".btn-add-contact", section);
    var form = $(".add-contact-form", section);
    if (!addBtn || !form) return;
    var nameInput = $('input[name="name"]', form);
    var cancel = $(".btn-cancel", form);

    addBtn.addEventListener("click", function () {
      form.hidden = false;
      addBtn.hidden = true;
      if (nameInput) nameInput.focus();
    });
    if (cancel) {
      cancel.addEventListener("click", function () {
        form.reset();
        form.hidden = true;
        addBtn.hidden = false;
        addBtn.focus();
      });
    }
    form.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && cancel) { e.preventDefault(); e.stopPropagation(); cancel.click(); }
    });
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      submitContact(form, etapa);
    });
  }

  function submitContact(form, etapa) {
    var nameInput = $('input[name="name"]', form);
    var phoneInput = $('input[name="phone"]', form);
    var name = nameInput ? nameInput.value.trim() : "";
    var phone = phoneInput ? phoneInput.value.trim() : "";
    if (!name || !phone) {
      toast("Escribe nombre y teléfono");
      if (!name && nameInput) nameInput.focus();
      else if (phoneInput) phoneInput.focus();
      return;
    }
    var save = $('button[type="submit"]', form);
    if (save) save.disabled = true;
    api("POST", API + "/contactos", { name: name, phone: phone, etapa_id: etapa.id })
      .then(function () {
        toast("Contacto «" + name + "» añadido a «" + etapa.nombre + "»");
        /* Se cierra ANTES de recargar: con el formulario abierto el tablero
           cuenta como ocupado y el repintado se quedaría esperando. */
        form.reset();
        form.hidden = true;
        var addBtn = $(".btn-add-contact", form.parentNode || form);
        if (addBtn) {
          addBtn.hidden = false;
          addBtn.focus(); // el repintado devuelve el foco a este mismo botón
        }
        return load();
      })
      .catch(function (err) {
        toast(mensajeDe(err, "No se pudo guardar el contacto"));
        if (save) save.disabled = false;
      });
  }

  /* --- tarjetas --- */

  function makeCard(c, etapa, color, now) {
    var card = tpl("crm-tpl-card");
    card.dataset.contactId = c.phone;
    card.style.borderTopColor = color;
    card.setAttribute("role", "button");
    card.setAttribute("aria-label", accessibleName(c));
    fillAvatar($(".avatar", card), c);
    setText(card, ".card-name", nombreDe(c));
    fillAlias($(".alias-tag", card), c);
    setText(card, ".card-phone", c.name ? formatoTelefono(c.phone) : "");
    fillPreview($(".card-preview", card), previewOf(c.last_message));
    setTiempo($(".card-time", card), lastActivity(c), now);
    fillBadges($(".card-badges", card), c);

    var pin = $(".card-pin", card);
    if (pin && !c.pinned) quitar(pin);

    var unread = $(".card-unread", card);
    if (unread) {
      if (Number(c.unread) > 0) {
        unread.textContent = String(c.unread);
        unread.setAttribute("aria-label", c.unread + " sin leer");
      } else {
        quitar(unread);
      }
    }

    var sel = $(".card-move", card);
    if (sel) {
      var first = document.createElement("option");
      first.value = "";
      first.textContent = "Mover a…";
      sel.appendChild(first);
      state.etapas.forEach(function (other) {
        if (other.id === etapa.id) return;
        var opt = document.createElement("option");
        opt.value = String(other.id);
        opt.textContent = other.nombre;
        sel.appendChild(opt);
      });
      sel.value = "";
      sel.addEventListener("change", function () {
        var target = Number(sel.value);
        sel.value = "";
        if (target) moveContact(c.phone, target);
      });
      sel.addEventListener("click", function (e) { e.stopPropagation(); });
      sel.addEventListener("keydown", function (e) { e.stopPropagation(); });
      /* Mientras el select tiene el foco la tarjeta no se arrastra; así el
         control nativo funciona igual dentro de un elemento draggable. */
      sel.addEventListener("focus", function () { card.draggable = false; });
      sel.addEventListener("blur", function () { card.draggable = true; });
    }

    card.addEventListener("click", function (e) {
      var t = e.target;
      if (t && t.closest && t.closest(".card-move")) return;
      openChat(c.phone);
    });
    card.addEventListener("keydown", function (e) {
      if (e.target !== card) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openChat(c.phone);
      }
    });
    card.addEventListener("dragstart", onDragStart);
    card.addEventListener("dragend", onDragEnd);
    return card;
  }

  /* ------------------------------------------------------------------ */
  /* Drag & drop (HTML5 nativo)                                          */
  /* ------------------------------------------------------------------ */

  function onDragStart(e) {
    var card = e.currentTarget;
    var id = card.dataset.contactId;
    draggingId = id;
    if (e.dataTransfer) {
      try {
        e.dataTransfer.setData("text/plain", id);
        e.dataTransfer.effectAllowed = "move";
      } catch (_) { /* DataTransfer en modo protegido: queda el respaldo */ }
    }
    card.classList.add("is-dragging");
  }

  function onDragEnd(e) {
    e.currentTarget.classList.remove("is-dragging");
    clearDropTargets();
    draggingId = null;
  }

  function clearDropTargets() {
    if (!els.board) return;
    $$(".column.is-over", els.board).forEach(function (s) { s.classList.remove("is-over"); });
  }

  function wireColumnDnD(section) {
    function over(e) {
      /* Sin arrastre propio (texto seleccionado, un archivo, un enlace) ni se
         resalta ni se acepta: un archivo soltado aquí no mueve a nadie. */
      if (draggingId === null) return;
      e.preventDefault();
      if (e.dataTransfer) {
        try { e.dataTransfer.dropEffect = "move"; } catch (_) { /* solo lectura en algunos eventos */ }
      }
      section.classList.add("is-over");
    }
    section.addEventListener("dragenter", over);
    section.addEventListener("dragover", over);
    section.addEventListener("dragleave", function (e) {
      if (e.relatedTarget && section.contains(e.relatedTarget)) return;
      section.classList.remove("is-over");
    });
    section.addEventListener("drop", function (e) {
      if (draggingId === null) return; // arrastre ajeno: que el navegador haga lo suyo
      e.preventDefault();
      var id = draggingId;
      if (e.dataTransfer) {
        /* El DataTransfer debe traer el mismo teléfono; si trae otra cosa, manda el propio. */
        try { if (e.dataTransfer.getData("text/plain") !== String(id)) id = draggingId; } catch (_) { /* protegido */ }
      }
      clearDropTargets();
      if (els.board) {
        $$(".card.is-dragging", els.board).forEach(function (c) { c.classList.remove("is-dragging"); });
      }
      draggingId = null;
      if (!id) return;
      moveContact(String(id), Number(section.dataset.columnId));
    });
  }

  /* Mueve un contacto de etapa con actualización optimista; si el PUT falla,
     revierte. Lo usan el drop y el select.card-move. */
  function moveContact(phone, targetId) {
    var contacto = contactoByPhone(phone);
    var target = etapaById(targetId);
    if (!contacto || !target) return Promise.resolve();
    if (contacto.etapa_id === target.id) return Promise.resolve();

    var from = contacto.etapa_id;
    contacto.etapa_id = target.id;
    bumpCount(from, -1);
    bumpCount(target.id, 1);
    /* Una carga que salió antes de este movimiento trae el estado anterior; si
       llegara ahora —o estuviera esperando su turno—, la tarjeta rebotaría a su
       columna de origen. Se invalidan las dos. */
    loadSeq++;
    ultimoTablero = null;
    tableroPendiente = null;
    render();

    return api("PUT", API + "/contactos/" + encodeURIComponent(phone) + "/etapa", { etapa_id: target.id })
      .then(function () {
        toast("Movido a «" + target.nombre + "»");
        return load();
      })
      .catch(function (err) {
        var actual = contactoByPhone(phone);
        if (actual && actual.etapa_id === target.id) {
          actual.etapa_id = from;
          bumpCount(from, 1);
          bumpCount(target.id, -1);
          render();
        }
        toast(mensajeDe(err, "No se pudo mover") === "Sin conexión con el servidor" ? "No se pudo mover" : "No se pudo mover: " + mensajeDe(err, "error"));
      });
  }

  /* ------------------------------------------------------------------ */
  /* Reordenar etapas: arrastre de la cabecera y «Mover antes/después»   */
  /* ------------------------------------------------------------------ */

  /* Lo que se reordena es el tablero de quien lo mira, nunca a sus clientes:
     el servidor recibe la lista completa de ids (PUT /crm/api/etapas) y cada
     tarjeta se queda en su columna. Igual que al mover una tarjeta, se pinta
     antes de que conteste y se revierte si falla. */

  function posicionDe(id) {
    for (var i = 0; i < state.etapas.length; i++) {
      if (state.etapas[i].id === id) return i;
    }
    return -1;
  }

  function wireColumnDrag(section, etapa) {
    var header = $(".column-header", section);
    if (!header) return;
    header.draggable = true;
    header.addEventListener("dragstart", function (e) {
      draggingColumnId = etapa.id;
      closeAllMenus();
      closeColorPickers();
      if (e.dataTransfer) {
        try {
          e.dataTransfer.setData("text/plain", etapa.nombre);
          e.dataTransfer.effectAllowed = "move";
          /* Se arrastra la columna entera, no solo su cabecera. */
          if (typeof e.dataTransfer.setDragImage === "function") {
            var r = section.getBoundingClientRect();
            e.dataTransfer.setDragImage(section, e.clientX - r.left, e.clientY - r.top);
          }
        } catch (_) { /* DataTransfer en modo protegido */ }
      }
      section.classList.add("is-col-dragging");
    });
    header.addEventListener("dragend", function () {
      section.classList.remove("is-col-dragging");
      limpiarMarcaColumna();
      draggingColumnId = null;
    });
  }

  /* Un solo juego de escuchas en el tablero, no uno por columna: la columna
     cae tanto sobre otra como en el hueco entre dos o más allá de la última.
     Los de las tarjetas (wireColumnDnD) ni se enteran: miran draggingId. */
  function wireBoardColumnDnD() {
    var board = els.board;
    if (!board) return;
    board.addEventListener("dragenter", function (e) {
      if (draggingColumnId !== null) e.preventDefault();
    });
    board.addEventListener("dragover", function (e) {
      if (draggingColumnId === null) return;
      e.preventDefault();
      if (e.dataTransfer) {
        try { e.dataTransfer.dropEffect = "move"; } catch (_) { /* solo lectura en algunos eventos */ }
      }
      marcarColumnaDestino(indiceInsercion(e));
    });
    board.addEventListener("dragleave", function (e) {
      if (draggingColumnId === null) return;
      if (e.relatedTarget && board.contains(e.relatedTarget)) return;
      limpiarMarcaColumna();
    });
    board.addEventListener("drop", function (e) {
      if (draggingColumnId === null) return;
      e.preventDefault();
      var id = draggingColumnId;
      var indice = indiceInsercion(e);
      draggingColumnId = null;
      limpiarMarcaColumna();
      $$(".column.is-col-dragging", board).forEach(function (s) { s.classList.remove("is-col-dragging"); });
      reordenarColumna(id, indice);
    });
  }

  /* Las otras columnas, en el orden de la pantalla. */
  function otrasColumnas() {
    return $$(".column", els.board).filter(function (s) {
      return s.dataset.columnId !== String(draggingColumnId);
    });
  }

  /* Dónde caería: cuántas de las OTRAS columnas quedan antes del puntero,
     medido contra su punto medio en el eje del tablero. Horizontal, salvo por
     debajo de 900 px, donde crm.css las apila y el eje es el vertical. */
  function indiceInsercion(e) {
    var vertical = window.getComputedStyle(els.board).flexDirection === "column";
    var p = vertical ? e.clientY : e.clientX;
    var indice = 0;
    otrasColumnas().forEach(function (s) {
      var r = s.getBoundingClientRect();
      if (p > (vertical ? r.top + r.height / 2 : r.left + r.width / 2)) indice++;
    });
    return indice;
  }

  function ordenCon(id, indice) {
    var ids = [];
    state.etapas.forEach(function (e) { if (e.id !== id) ids.push(e.id); });
    ids.splice(indice, 0, id);
    return ids;
  }

  function mismoOrden(ids) {
    return ids.join(",") === state.etapas.map(function (e) { return e.id; }).join(",");
  }

  /* Una barra en el hueco donde caería. Soltarla en su propio lugar no marca
     nada: no hay nada que guardar. */
  function marcarColumnaDestino(indice) {
    limpiarMarcaColumna();
    if (draggingColumnId === null || mismoOrden(ordenCon(draggingColumnId, indice))) return;
    var otras = otrasColumnas();
    if (!otras.length) return;
    if (indice < otras.length) otras[indice].classList.add("is-drop-before");
    else otras[otras.length - 1].classList.add("is-drop-after");
  }

  function limpiarMarcaColumna() {
    if (!els.board) return;
    $$(".column.is-drop-before, .column.is-drop-after", els.board).forEach(function (s) {
      s.classList.remove("is-drop-before", "is-drop-after");
    });
  }

  function reordenarColumna(id, indice) {
    var ids = ordenCon(id, indice);
    if (mismoOrden(ids)) return Promise.resolve(false);
    return guardarOrden(ids, id);
  }

  function guardarOrden(ids, movidaId) {
    var antes = state.etapas.slice();
    var porId = {};
    antes.forEach(function (e) { porId[e.id] = e; });
    var etapa = porId[movidaId];
    state.etapas = ids.map(function (id) { return porId[id]; });
    /* Una carga que salió antes trae el orden viejo: si llegara ahora, la
       columna volvería a su lugar. Se invalidan las dos, como en moveContact. */
    loadSeq++;
    ultimoTablero = null;
    tableroPendiente = null;
    render();
    return api("PUT", API + "/etapas", { orden: ids })
      .then(function () {
        /* El aviso dice el lugar y no «a la derecha»: por debajo de 900 px las
           columnas se apilan y la derecha es abajo. Lo lee también el lector
           de pantalla (#crm-toast es role=status). */
        toast("«" + etapa.nombre + "» quedó en el lugar " + (ids.indexOf(movidaId) + 1) + " de " + ids.length);
        return load().then(function () { return true; });
      })
      .catch(function (err) {
        state.etapas = antes;
        /* Si en medio llegó un sondeo con la verdad del servidor, su firma quedó como la última
           pintada y el siguiente sondeo, idéntico, no repintaría: `antes` se quedaría en
           pantalla. Sin firma, el siguiente sondeo repinta lo que diga el servidor. */
        ultimoTablero = null;
        render();
        toast(mensajeDe(err, "No se pudo reordenar"));
        return false;
      });
  }

  /* «Mover antes/después» del menú ⋮. El menú se cierra con el foco adentro,
     así que el foco se lleva al ⋮ de la columna movida —ya en su lugar nuevo—
     y el repintado de la recarga lo conserva ahí. */
  function moverColumna(etapa, delta) {
    var i = posicionDe(etapa.id);
    var j = i + delta;
    if (i < 0 || j < 0 || j >= state.etapas.length) return;
    closeAllMenus();
    reordenarColumna(etapa.id, j);
    var btn = els.board ? $('.column[data-column-id="' + etapa.id + '"] .column-menu-btn', els.board) : null;
    if (btn) btn.focus();
  }

  /* ------------------------------------------------------------------ */
  /* Menú de columna: renombrar, color, eliminar (cada quien las suyas)  */
  /* ------------------------------------------------------------------ */

  function closeAllMenus() {
    if (!els.board) return;
    $$(".column-menu", els.board).forEach(function (m) { m.hidden = true; });
    $$(".column-menu-btn", els.board).forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
  }

  function closeColorPickers() {
    if (!els.board) return;
    $$(".column .color-picker", els.board).forEach(function (p) { quitar(p); });
  }

  function startRename(section, etapa) {
    var h2 = $(".column-name", section);
    if (!h2) return;
    /* Con la cabecera arrastrable, Firefox no deja poner el cursor ni seleccionar
       dentro del input: mientras se renombra, la cabecera no se arrastra. */
    var header = h2.parentNode;
    if (header) header.draggable = false;
    var input = document.createElement("input");
    input.type = "text";
    input.className = "column-rename";
    input.value = etapa.nombre;
    input.maxLength = 40;
    input.setAttribute("aria-label", "Nuevo nombre de la etapa");
    h2.parentNode.replaceChild(input, h2);
    input.focus();
    input.select();

    var done = false;
    function finish(save) {
      if (done) return;
      done = true;
      if (header) header.draggable = true;
      var value = input.value.trim();
      if (save && value && value !== etapa.nombre) {
        api("PUT", API + "/etapas/" + etapa.id, { nombre: value })
          .then(function () {
            toast("Etapa renombrada a «" + value + "»");
            /* El h2 vuelve con el nombre nuevo antes de recargar: mientras
               exista el input, el tablero está ocupado y no se repinta. */
            h2.textContent = value;
            if (input.parentNode) input.parentNode.replaceChild(h2, input);
            var menuBtn = $(".column-menu-btn", section);
            if (menuBtn) menuBtn.focus(); // y el repintado lo conserva ahí
            return load();
          })
          .catch(function (err) {
            toast(mensajeDe(err, "No se pudo renombrar la etapa"));
            if (input.parentNode) input.parentNode.replaceChild(h2, input);
          });
      } else if (input.parentNode) {
        input.parentNode.replaceChild(h2, input);
      }
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
    input.addEventListener("click", function (e) { e.stopPropagation(); });
  }

  /* Rellena un div.color-picker con los 8 swatches de la paleta delante de su
     input[type=color]. onPick(color) recibe el hex en mayúsculas. */
  function fillSwatches(picker, selected, onPick) {
    var input = $('input[type="color"]', picker);
    PALETTE.forEach(function (color) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "swatch";
      b.dataset.color = color;
      b.style.background = color;
      b.title = color;
      b.setAttribute("aria-label", "Color " + color);
      b.addEventListener("click", function () {
        markSwatch(picker, color);
        if (input) input.value = color.toLowerCase();
        onPick(color);
      });
      if (input) picker.insertBefore(b, input);
      else picker.appendChild(b);
    });
    markSwatch(picker, selected);
    if (input) {
      input.value = normalizeHex(selected).toLowerCase();
      input.addEventListener("change", function () {
        var color = normalizeHex(input.value);
        markSwatch(picker, color);
        onPick(color);
      });
    }
  }

  function markSwatch(picker, color) {
    var wanted = normalizeHex(color);
    $$(".swatch", picker).forEach(function (s) {
      s.classList.toggle("is-selected", s.dataset.color === wanted);
    });
  }

  function openColorPicker(section, etapa) {
    closeColorPickers();
    var picker = document.createElement("div");
    picker.className = "color-picker";
    picker.setAttribute("role", "group");
    picker.setAttribute("aria-label", "Color de la etapa " + etapa.nombre);

    var input = document.createElement("input");
    input.type = "color";
    input.className = "color-input";
    input.setAttribute("aria-label", "Color personalizado");
    picker.appendChild(input);

    var close = document.createElement("button");
    close.type = "button";
    close.className = "picker-close";
    close.setAttribute("aria-label", "Cerrar selector de color");
    close.textContent = "✕";
    close.addEventListener("click", function () { quitar(picker); });
    picker.appendChild(close);

    fillSwatches(picker, etapa.color, function (color) { applyColumnColor(etapa, color); });
    picker.addEventListener("click", function (e) { e.stopPropagation(); });

    var header = $(".column-header", section);
    if (header && header.parentNode) header.parentNode.insertBefore(picker, header.nextSibling);
    else section.insertBefore(picker, section.firstChild);
    var firstSwatch = $(".swatch", picker);
    if (firstSwatch) firstSwatch.focus();
  }

  /* El color se aplica en sitio (columna, tarjetas y punto de la pestaña): el
     selector sigue abierto por si se quiere probar otro, y con él abierto el
     tablero no se repinta. El siguiente sondeo trae la verdad del servidor. */
  function pintarColorEtapa(etapa, color) {
    etapa.color = color;
    var section = els.board ? $('.column[data-column-id="' + etapa.id + '"]', els.board) : null;
    if (section) {
      section.style.setProperty("--col-color", color);
      section.style.setProperty("--col-soft", hexToRgba(color, 0.14));
      $$(".card", section).forEach(function (card) { card.style.borderTopColor = color; });
    }
    var dot = els.tabs ? $('.tab[data-filter="' + etapa.id + '"] .tab-dot', els.tabs) : null;
    if (dot) dot.style.background = color;
    ultimoTablero = null;
  }

  function applyColumnColor(etapa, color) {
    if (normalizeHex(color) === normalizeHex(etapa.color)) return;
    api("PUT", API + "/etapas/" + etapa.id, { color: color })
      .then(function () {
        toast("Color de «" + etapa.nombre + "» actualizado");
        pintarColorEtapa(etapa, normalizeHex(color));
      })
      .catch(function (err) {
        toast(mensajeDe(err, "No se pudo cambiar el color"));
      });
  }

  function deleteColumn(etapa) {
    if (state.etapas.length <= 1) return;
    var remaining = state.etapas.filter(function (e) { return e.id !== etapa.id; });
    var target = remaining[0]; // state.etapas ya viene ordenada por ord, id
    var n = (typeof etapa.count === "number") ? etapa.count : countFor(etapa.id);
    var detalle = n === 0 ? "No tiene contactos."
      : n === 1 ? "Su único contacto pasará a «" + target.nombre + "»."
      : "Sus " + n + " contactos pasarán a «" + target.nombre + "».";
    var ok = window.confirm("¿Eliminar la etapa «" + etapa.nombre + "»? " + detalle
      + " Solo cambia tu tablero.");
    if (!ok) return;
    api("DELETE", API + "/etapas/" + etapa.id)
      .then(function () {
        if (state.filter === String(etapa.id)) state.filter = "all";
        toast("Etapa «" + etapa.nombre + "» eliminada");
        return load();
      })
      .catch(function (err) {
        toast(mensajeDe(err, "No se pudo eliminar la etapa"));
      });
  }

  /* ------------------------------------------------------------------ */
  /* Nueva etapa (dialog)                                                */
  /* ------------------------------------------------------------------ */

  function dialogSupported(dlg) {
    return !!dlg
      && typeof window.HTMLDialogElement === "function"
      && typeof dlg.showModal === "function";
  }

  function openNewColumnDialog() {
    if (dialogSupported(els.dlgColumn)) {
      var dlg = els.dlgColumn;
      if (els.formColumn) els.formColumn.reset();
      var picker = $(".color-picker", dlg);
      if (picker) markSwatch(picker, DEFAULT_COLOR);
      if (els.columnColor) els.columnColor.value = DEFAULT_COLOR.toLowerCase();
      if (!dlg.open) dlg.showModal();
      if (els.columnName) els.columnName.focus();
      return;
    }
    var name = window.prompt("Nombre de la nueva etapa:");
    if (name && name.trim()) createColumn(name.trim(), DEFAULT_COLOR);
  }

  function createColumn(name, color) {
    return api("POST", API + "/etapas", { nombre: name, color: color })
      .then(function (etapa) {
        toast("Etapa «" + ((etapa && etapa.nombre) || name) + "» creada");
        /* El diálogo se cierra antes de recargar: abierto, el tablero cuenta
           como ocupado y el repintado esperaría a que se cerrara. */
        if (els.dlgColumn && els.dlgColumn.open && typeof els.dlgColumn.close === "function") els.dlgColumn.close();
        return load().then(function () {
          if (els.board) els.board.scrollLeft = els.board.scrollWidth;
          return true;
        });
      })
      .catch(function (err) {
        toast(mensajeDe(err, "No se pudo crear la etapa"));
        return false;
      });
  }

  function wireDialog() {
    var dlg = els.dlgColumn;
    if (!dlg) return;
    var picker = $(".color-picker", dlg);
    if (picker) fillSwatches(picker, DEFAULT_COLOR, function () { /* solo marca la selección */ });
    if (els.btnColumnCancel) {
      els.btnColumnCancel.addEventListener("click", function () {
        if (typeof dlg.close === "function" && dlg.open) dlg.close();
      });
    }
    if (els.formColumn) {
      els.formColumn.addEventListener("submit", function (e) {
        e.preventDefault();
        var name = els.columnName ? els.columnName.value.trim() : "";
        var color = els.columnColor ? normalizeHex(els.columnColor.value) : DEFAULT_COLOR;
        if (!name) {
          toast("Escribe el nombre de la etapa");
          if (els.columnName) els.columnName.focus();
          return;
        }
        var submit = $('button[type="submit"]', els.formColumn);
        if (submit) submit.disabled = true;
        createColumn(name, color).then(function () {
          if (submit) submit.disabled = false;
        });
      });
    }
  }

  /* ------------------------------------------------------------------ */
  /* Mis respuestas (dialog)                                             */
  /* ------------------------------------------------------------------ */

  /* Las respuestas guardadas de quien tiene la sesión (app/replies.py). La lista
     se pide cada vez que se abre —otra pestaña pudo cambiarla— y el servidor es
     quien decide: nada se pinta antes de que conteste. */

  function abrirRespuestas(desdeChat) {
    var dlg = els.dlgRespuestas;
    if (!dlg || dlg.open) return;
    abridorRespuestas = document.activeElement;
    if (els.formRespuesta) els.formRespuesta.reset();
    avisoRespuesta(null);
    estadoRespuesta("");
    /* Desde el chat, lo escrito en el compositor llega ya como texto: guardar lo
       que se acaba de redactar es el uso más común. El compositor no se toca. */
    var borrador = desdeChat && els.body ? els.body.value.trim() : "";
    if (borrador && els.respTexto) els.respTexto.value = borrador.slice(0, LIMITES_RESPUESTAS.texto || borrador.length);
    if (dialogSupported(dlg)) dlg.showModal();
    else dlg.setAttribute("open", "");
    if (els.respTitulo) els.respTitulo.focus();
    cargarRespuestas();
  }

  function cerrarRespuestas() {
    var dlg = els.dlgRespuestas;
    if (!dlg) return;
    if (dialogSupported(dlg)) {
      if (dlg.open) dlg.close();
    } else {
      dlg.removeAttribute("open");
      alCerrarRespuestas();
    }
  }

  /* Al cerrarse —✕ o Escape—: el foco vuelve a quien lo abrió y se quita el
     ?respuestas=1 con que llega la ruta vieja, para que recargar no lo reabra. */
  function alCerrarRespuestas() {
    /* El evento close llega en una tarea aparte: si para entonces el diálogo ya se
       reabrió, es el cierre anterior y no debe invalidar la carga de la nueva
       apertura (el «Cargando…» se quedaba para siempre). Lo cazó el juez. */
    if (els.dlgRespuestas && els.dlgRespuestas.open) return;
    respuestasSeq++;
    try {
      var url = new URL(window.location.href);
      if (url.searchParams.has("respuestas")) {
        url.searchParams.delete("respuestas");
        window.history.replaceState(null, "", url);
      }
    } catch (_) { /* URL no manipulable: se sigue sin ella */ }
    var a = abridorRespuestas;
    abridorRespuestas = null;
    if (a && a !== document.body && typeof a.focus === "function" && document.contains(a) && a.getClientRects().length) {
      try { a.focus({ preventScroll: true }); } catch (_) { a.focus(); }
    }
  }

  function cargarRespuestas(conservarAviso) {
    var seq = ++respuestasSeq;
    if (els.respCargando) els.respCargando.hidden = false;
    return api("GET", API + "/respuestas")
      .then(function (data) {
        if (seq === respuestasSeq) aplicarRespuestas(data);
      }, function (err) {
        if (seq === respuestasSeq && !conservarAviso) avisoRespuesta(mensajeDe(err, "No se pudieron cargar tus respuestas"));
      })
      .then(function () {
        if (seq === respuestasSeq && els.respCargando) els.respCargando.hidden = true;
      });
  }

  /* Una respuesta del servidor trae la lista del diálogo Y la fila del compositor.
     Con `superar`, la lista que pidió abrirRespuestas y siga en vuelo ya no cuenta:
     llegaría con lo de antes de guardar o eliminar y lo pintaría encima. */
  function aplicarRespuestas(data, superar) {
    if (!data || !Array.isArray(data.mias)) return;
    if (superar) {
      respuestasSeq++;
      if (els.respCargando) els.respCargando.hidden = true;
    }
    pintarListaRespuestas(data.mias, data.max || topeRespuestas);
    if (Array.isArray(data.respuestas_rapidas)) {
      DATOS.respuestas_rapidas = data.respuestas_rapidas;
      pintarRespuestasRapidas(data.respuestas_rapidas);
    }
  }

  function pintarListaRespuestas(mias, max) {
    if (!els.respLista) return;
    clear(els.respLista);
    mias.forEach(function (r) {
      var li = tpl("crm-tpl-respuesta");
      if (!li || !r) return;
      li.dataset.replyId = String(r.id);
      setText(li, ".resp-titulo", r.title || "");
      setText(li, ".resp-texto", r.text || "");
      var b = $(".resp-borrar", li);
      if (b) {
        b.setAttribute("aria-label", "Eliminar «" + (r.title || "") + "»");
        b.addEventListener("click", function () { eliminarRespuesta(r, b); });
      }
      els.respLista.appendChild(li);
    });
    totalRespuestas = mias.length;
    if (max) topeRespuestas = max;
    if (els.respVacio) els.respVacio.hidden = mias.length > 0;
    if (els.respCuenta) els.respCuenta.textContent = max ? "(" + mias.length + " de " + max + ")" : "(" + mias.length + ")";
    var llenas = respuestasLlenas();
    if (els.respTope) els.respTope.hidden = !llenas;
    if (els.respGuardar) els.respGuardar.disabled = llenas || guardandoRespuesta;
  }

  function respuestasLlenas() {
    return topeRespuestas > 0 && totalRespuestas >= topeRespuestas;
  }

  function avisoRespuesta(texto) {
    if (!els.respError) return;
    els.respError.textContent = texto || "";
    els.respError.hidden = !texto;
  }

  function estadoRespuesta(texto) {
    if (els.respEstado) els.respEstado.textContent = texto;
  }

  /* Pide el chat abierto otra vez: invalida (chatSeq) un sondeo en vuelo que traería
     la fila de respuestas de antes del cambio y la repintaría encima de la nueva. */
  function refrescarChatAbierto() {
    if (state.chat !== null) cargarChat(state.chat, { silencioso: true });
  }

  function guardarRespuesta(e) {
    e.preventDefault();
    if (guardandoRespuesta || !els.respTitulo || !els.respTexto) return;
    var titulo = els.respTitulo.value.trim();
    var texto = els.respTexto.value.trim();
    avisoRespuesta(null);
    estadoRespuesta("");
    if (!titulo || !texto) {
      avisoRespuesta("Escribe el título y el texto de la respuesta.");
      (titulo ? els.respTexto : els.respTitulo).focus();
      return;
    }
    guardandoRespuesta = true;
    if (els.respGuardar) els.respGuardar.disabled = true;
    api("POST", API + "/respuestas", { title: titulo, text: texto })
      .then(function (data) {
        guardandoRespuesta = false;
        if (els.formRespuesta) els.formRespuesta.reset();
        aplicarRespuestas(data, true);
        estadoRespuesta("Respuesta «" + titulo + "» guardada.");
        refrescarChatAbierto();
        if (els.dlgRespuestas && els.dlgRespuestas.open) els.respTitulo.focus();
      }, function (err) {
        guardandoRespuesta = false;
        avisoRespuesta(mensajeDe(err, "No se pudo guardar la respuesta"));
        if (els.respGuardar) els.respGuardar.disabled = respuestasLlenas();
      });
  }

  function eliminarRespuesta(r, boton) {
    if (!window.confirm("¿Eliminar «" + (r.title || "") + "»?")) return;
    avisoRespuesta(null);
    estadoRespuesta("");
    boton.disabled = true;
    /* El botón pulsado desaparece y dentro de un modal el foco no puede quedar en el
       aire: pasa a la respuesta de al lado o, si no queda ninguna, al título. */
    var li = boton.closest ? boton.closest(".resp-item") : null;
    var vecino = li ? (li.nextElementSibling || li.previousElementSibling) : null;
    var vecinoId = vecino ? vecino.dataset.replyId : null;
    api("DELETE", API + "/respuestas/" + encodeURIComponent(r.id))
      .then(function (data) {
        aplicarRespuestas(data, true);
        estadoRespuesta("Respuesta «" + (r.title || "") + "» eliminada.");
        refrescarChatAbierto();
        var destino = vecinoId ? $('.resp-item[data-reply-id="' + vecinoId + '"] .resp-borrar', els.respLista) : null;
        destino = destino || els.respTitulo;
        if (destino && els.dlgRespuestas && els.dlgRespuestas.open) destino.focus();
      }, function (err) {
        boton.disabled = false;
        avisoRespuesta(mensajeDe(err, "No se pudo eliminar la respuesta"));
        /* Si ya no existía (otra pestaña la borró), la lista se pone al día sin
           tapar el aviso con otro. */
        cargarRespuestas(true);
      });
  }

  function wireRespuestas() {
    var dlg = els.dlgRespuestas;
    if (!dlg || !document.getElementById("crm-tpl-respuesta")) {
      /* Sin el diálogo no hay qué abrir: mejor sin botones que con botones muertos. */
      quitar(els.btnRespuestas);
      quitar(els.btnRespuestasChat);
      els.btnRespuestas = null;
      els.btnRespuestasChat = null;
      return;
    }
    if (els.btnRespuestas) els.btnRespuestas.addEventListener("click", function () { abrirRespuestas(false); });
    if (els.btnRespuestasChat) els.btnRespuestasChat.addEventListener("click", function () { abrirRespuestas(true); });
    if (els.respCerrar) els.respCerrar.addEventListener("click", cerrarRespuestas);
    if (els.formRespuesta) els.formRespuesta.addEventListener("submit", guardarRespuesta);
    dlg.addEventListener("close", alCerrarRespuestas);
  }

  /* ------------------------------------------------------------------ */
  /* Panel de chat                                                       */
  /* ------------------------------------------------------------------ */

  /* Gancho público: resuelve window.openChat en el momento del clic (otro
     módulo puede sobreescribirlo) y emite el evento crm:openchat. */
  function openChat(phone) {
    var fn = window.openChat;
    if (typeof fn === "function") fn(phone);
    else abrirChat(phone);
    document.dispatchEvent(new CustomEvent("crm:openchat", { detail: { phone: String(phone), contactId: String(phone) } }));
  }

  /* Aviso de cambio de conversación para el módulo de al lado (el cajón del
     folio, static/js/crm-panel.js). Sale de abrirChat/cerrarChat y no de
     openChat porque el chat también se abre solo, con ?chat= al cargar. */
  function emitirChat(phone, mismo) {
    try {
      document.dispatchEvent(new CustomEvent("crm:chat", {
        detail: { phone: phone, mismo: !!mismo }
      }));
    } catch (_) { /* sin CustomEvent el cajón simplemente no se abre */ }
  }

  function abrirChat(phone) {
    phone = String(phone || "").replace(/\D/g, "");
    if (!phone || !els.chat) return;
    var mismo = state.chat === phone;
    state.chat = phone;
    if (!mismo) {
      state.chatDatos = null;
      ultimoHilo = null;
      ultimaCabecera = null;
      ultimasRespuestas = null;
      ocultarErrorEnvio();
    }
    els.root.classList.add("chat-abierto");
    els.chat.hidden = false;
    els.board.hidden = true;
    if (els.btnBoard) els.btnBoard.hidden = false;
    marcarChatAbierto();
    emitirChat(phone, mismo);

    if (els.composerForm) els.composerForm.action = "/inbox/" + phone + "/send";
    var conocido = contactoByPhone(phone);
    if (conocido) pintarCabecera(conocido, null);
    else if (!mismo) pintarCabecera({ phone: phone, name: null, initials: phone.slice(-2), ventana: { abierta: false, nunca: true } }, null);
    if (!mismo) mostrarEstadoHilo("Cargando la conversación…", "chat-cargando");

    try {
      var url = new URL(window.location.href);
      url.searchParams.set("chat", phone);
      window.history.replaceState(null, "", url);
    } catch (_) { /* URL no manipulable: se sigue sin ella */ }

    cargarChat(phone, { completa: true }).then(function () {
      if (!mismo && state.chat === phone && els.chatName) {
        try { els.chatName.focus({ preventScroll: true }); } catch (_) { els.chatName.focus(); }
      }
    });
  }

  function cerrarChat() {
    if (state.chat === null) return;
    var phone = state.chat;
    chatSeq++;
    state.chat = null;
    state.chatDatos = null;
    ultimoHilo = null;
    ultimaCabecera = null;
    els.root.classList.remove("chat-abierto");
    els.chat.hidden = true;
    els.board.hidden = false;
    if (els.btnBoard) els.btnBoard.hidden = true;
    marcarChatAbierto();
    emitirChat(null, false);
    try {
      var url = new URL(window.location.href);
      url.searchParams.delete("chat");
      window.history.replaceState(null, "", url);
    } catch (_) { /* ídem */ }
    /* El foco vuelve a la tarjeta del contacto que se estaba viendo. */
    var card = $('.card[data-contact-id="' + phone + '"]', els.board) || $('.tray-item[data-contact-id="' + phone + '"]', els.trayList);
    if (card) {
      try { card.focus({ preventScroll: true }); } catch (_) { card.focus(); }
    }
  }

  function mostrarEstadoHilo(texto, clase) {
    if (!els.chatThread) return;
    clear(els.chatThread);
    var p = document.createElement("p");
    p.className = clase;
    p.textContent = texto;
    els.chatThread.appendChild(p);
  }

  function cargarChat(phone, opts) {
    opts = opts || {};
    var seq = ++chatSeq;
    return api("GET", API + "/chat/" + encodeURIComponent(phone))
      .then(function (data) {
        if (seq !== chatSeq || state.chat !== phone) return;
        state.chatDatos = data;
        pintarChat(data, opts);
      })
      .catch(function (err) {
        if (seq !== chatSeq || state.chat !== phone || opts.silencioso) return;
        ultimoHilo = null;
        mostrarEstadoHilo("No se pudo cargar la conversación: " + mensajeDe(err, "error"), "chat-error");
      });
  }

  /* Cabecera y compositor ANTES que el hilo: los dos cambian de alto (insignias,
     aviso de ventana, respuestas rápidas) y el hilo se acomoda entre ambos; si se
     bajara al fondo antes de que existan, el último mensaje quedaría tapado. */
  function pintarChat(data, opts) {
    var c = data.contacto || {};
    pintarCabecera(c, data);
    pintarCompositor(data);
    pintarHilo(Array.isArray(data.mensajes) ? data.mensajes : [], !!(opts.completa || opts.irAlFondo));
  }

  /* Cabecera: avatar, nombre, teléfono e insignias con sus acciones. Solo se
     repinta si algo cambió, para que el sondeo no parpadee. */
  function pintarCabecera(c, extra) {
    if (!c || !els.chatBadges) return;
    // Sin la respuesta del chat (se pinta con la tarjeta mientras carga) se deduce de la
    // tarjeta: el agente de su folio, y nadie más con el suyo apagado.
    var agenteActivo = (extra && typeof extra.agente_activo === "boolean")
      ? extra.agente_activo
      : (c.ticket ? c.ticket.agent_active !== false : true) && !(c.apagado_por && c.apagado_por.length);
    var asesor = (c.ticket && c.ticket.assigned_to) ? miembroPorNombre(c.ticket.assigned_to) : null;
    var estadoAsesor = (asesor && asesor.estado) ? (ESTADOS_MIEMBRO[asesor.estado] || asesor.estado) : null;
    // Quién más tiene el agente apagado en su folio: con varios dueños el bot puede estar
    // callado por el folio de un compañero, no por el de quien mira.
    var apagadoPorOtros = (c.apagado_por || []).filter(function (u) { return u !== USUARIO; });
    var firma = JSON.stringify([c.phone, c.name, c.alias, c.avatar_color, c.initials, c.bot_pausado, c.ventana,
      c.provider, c.ticket, c.bloqueado, agenteActivo, estadoAsesor, apagadoPorOtros]);
    if (firma === ultimaCabecera) return;
    ultimaCabecera = firma;

    fillAvatar(els.chatAvatar, c);
    if (els.chatName) els.chatName.textContent = c.name || "Sin nombre";
    fillAlias(els.chatAlias, c);
    if (els.chatPhone) els.chatPhone.textContent = formatoTelefono(c.phone);

    var b = els.chatBadges;
    clear(b);
    function chip(texto, cls) {
      var s = document.createElement("span");
      s.className = "chip" + (cls ? " " + cls : "");
      s.textContent = texto;
      b.appendChild(s);
      return s;
    }
    function boton(texto, cls, onClick) {
      var x = document.createElement("button");
      x.type = "button";
      x.className = cls;
      x.textContent = texto;
      x.addEventListener("click", onClick);
      b.appendChild(x);
      return x;
    }
    function enlace(texto, cls, href) {
      var a = document.createElement("a");
      a.className = "btn-nav " + cls;
      a.href = href;
      a.textContent = texto;
      b.appendChild(a);
      return a;
    }
    var volver = encodeURIComponent("/crm?chat=" + c.phone);

    if (c.bot_pausado) {
      // Si quien mira también lo tiene apagado en su folio, se dice: si no, reactivar el
      // suyo parecería no servir de nada.
      var yoTambien = (c.apagado_por || []).indexOf(USUARIO) !== -1;
      if (!agenteActivo && apagadoPorOtros.length) {
        chip("agente apagado — lo " + (yoTambien ? "atienden tú y " : "atiende ") + apagadoPorOtros.join(", "), "chip-bot");
      } else if (c.ticket && !agenteActivo) chip("agente apagado — el cliente solo te ve a ti", "chip-bot");
      else chip("escalado — el agente sigue activo; se apaga al enviar tu primer mensaje", "chip-bot");
      boton("Reactivar bot", "btn-sm chat-bot-on", reactivarBot);
    } else {
      chip("bot activo", "chip-cerrado");
    }

    var v = c.ventana || {};
    if (!v.abierta) chip("ventana de " + VENTANA_HORAS + " h cerrada", "chip-caduco");
    else chip("ventana" + (v.urgente ? " urgente" : "") + ": " + (v.texto || ""), v.urgente ? "chip-tomado" : "chip-cerrado");

    if (c.provider === "twilio") chip("responde por Twilio", "chip-twilio");
    else chip("responde por Meta", "");

    if (c.ticket) {
      enlace("🎫 Ver solicitud #" + c.ticket.id, "chat-ticket", "/tickets/" + c.ticket.id + "?volver=" + volver);
      if (c.ticket.assigned_to) chip("atiende: " + c.ticket.assigned_to + (estadoAsesor ? " · " + estadoAsesor : ""), "");
    }
    if (c.phone) enlace("👤 Ficha del cliente", "chat-client", "/clients/" + encodeURIComponent(c.phone) + "?volver=" + volver);
    if (c.bloqueado) chip("cliente bloqueado", "chip-bloqueado");
    if (ES_ADMIN && c.phone) {
      boton(c.bloqueado ? "Desbloquear cliente" : "Bloquear cliente",
        "btn-sm chat-block" + (c.bloqueado ? "" : " btn-danger"),
        function () { alternarBloqueo(!!c.bloqueado); });
    }
  }

  function estaAlFondo(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  }

  /* Sustituye el hilo solo si cambió. El scroll se queda donde estaba, salvo
     que se acabe de abrir, se acabe de enviar o el usuario estuviera al fondo. */
  function pintarHilo(mensajes, irAlFondo) {
    var hilo = els.chatThread;
    if (!hilo) return;
    var firma = mensajes.map(function (m) {
      return m.id + ":" + (m.wa_status || "") + ":" + (m.mio ? 1 : 0);
    }).join("|");
    if (firma === ultimoHilo) return;
    var primeraVez = ultimoHilo === null;
    var pegado = primeraVez || irAlFondo || estaAlFondo(hilo);
    var arriba = hilo.scrollTop;
    ultimoHilo = firma;
    clear(hilo);
    if (!mensajes.length) {
      var p = document.createElement("p");
      p.className = "chat-vacio";
      p.textContent = "Sin mensajes todavía.";
      hilo.appendChild(p);
      return;
    }
    var frag = document.createDocumentFragment();
    mensajes.forEach(function (m) { frag.appendChild(makeBubble(m)); });
    hilo.appendChild(frag);
    hilo.scrollTop = pegado ? hilo.scrollHeight : arriba;
    /* Una imagen adjunta crece al cargar y empuja el final fuera de vista: si
       el hilo estaba pegado al fondo, se vuelve a pegar cuando termine. */
    if (pegado) {
      $$("img", hilo).forEach(function (img) {
        if (img.complete) return;
        img.addEventListener("load", function () {
          if (estaAlFondo(hilo) || hilo.scrollTop === 0) hilo.scrollTop = hilo.scrollHeight;
        });
      });
    }
  }

  function tamanoLegible(n) {
    n = Number(n) || 0;
    if (!n) return "";
    return n < 1024 * 1024 ? Math.max(1, Math.round(n / 1024)) + " KB" : (n / 1024 / 1024).toFixed(1) + " MB";
  }

  /* Archivo que vive en Meta o Twilio: el VPS solo guarda la referencia y el
     visor (/crm/api/adjunto/{id}) lo trae al abrirlo. Uno vencido ya no se
     puede traer, y se dice con texto. */
  function adjuntoDe(adj) {
    var nombre = adj.nombre || (/^image\//.test(adj.mime || "") ? "Imagen" : "Documento");
    if (adj.vencido) {
      var v = document.createElement("span");
      v.className = "bubble-adjunto vencido";
      v.textContent = "📎 " + nombre + " · ya no está en WhatsApp";
      return v;
    }
    var a = document.createElement("a");
    a.className = "bubble-adjunto";
    a.href = adj.url;
    a.target = "_blank";
    a.rel = "noopener";
    if (/^image\//.test(adj.mime || "")) {
      var img = document.createElement("img");
      img.src = adj.url;
      img.alt = nombre;
      img.loading = "lazy";
      a.appendChild(img);
    } else {
      var tam = tamanoLegible(adj.bytes);
      a.textContent = "📄 " + nombre + (tam ? " · " + tam : "");
    }
    return a;
  }

  function makeBubble(m) {
    var b = tpl("crm-tpl-bubble");
    b.classList.add(m.direction === "out" ? "out" : "in");
    b.dataset.messageId = String(m.id);
    setText(b, ".bubble-text", m.body || "");
    var meta = $(".meta", b);
    if (m.adjunto) {
      b.insertBefore(adjuntoDe(m.adjunto), meta);
    } else if (m.media_path) {
      var a = document.createElement("a");
      a.className = "bubble-adjunto";
      a.href = m.media_path;
      a.target = "_blank";
      a.rel = "noopener";
      if (/\.pdf$/i.test(m.media_path)) {
        a.textContent = "📄 Ver documento";
      } else {
        var img = document.createElement("img");
        img.src = m.media_path;
        img.alt = "Imagen enviada";
        a.appendChild(img);
      }
      b.insertBefore(a, meta);
    }
    var partes = [m.direction === "in" ? "Cliente" : (m.sent_by || "Bot"), fechaHora(m.created_at)];
    if (m.direction === "out" && m.wa_status) partes.push(m.wa_status);
    if (meta) {
      meta.textContent = partes.filter(Boolean).join(" · ");
      if (m.mio) {
        meta.appendChild(document.createTextNode(" · "));
        var del = document.createElement("button");
        del.type = "button";
        del.className = "msg-delete";
        del.textContent = "eliminar";
        del.setAttribute("aria-label", "Ocultar este mensaje");
        del.addEventListener("click", function () { ocultarMensaje(m.id); });
        meta.appendChild(del);
      }
    }
    return b;
  }

  /* Compositor o aviso de ventana cerrada, según la ventana de 24 h. Nunca
     toca el textarea ni el adjunto: eso es del asesor. */
  function pintarCompositor(data) {
    var v = data.ventana || {};
    if (!els.composerAbierto || !els.bannerCerrada) return;
    if (v.abierta) {
      els.composerAbierto.hidden = false;
      els.bannerCerrada.hidden = true;
      if (els.windowNote) {
        els.windowNote.classList.toggle("urgente", !!v.urgente);
        $$(".si-urgente", els.windowNote).forEach(function (s) { s.hidden = !v.urgente; });
      }
      if (els.windowIco) els.windowIco.textContent = v.urgente ? "⚠️" : "⏳";
      if (els.windowLeft) els.windowLeft.textContent = v.texto || "";
      pintarRespuestasRapidas(Array.isArray(data.respuestas_rapidas) ? data.respuestas_rapidas : (DATOS.respuestas_rapidas || []));
    } else {
      els.composerAbierto.hidden = true;
      els.bannerCerrada.hidden = false;
      $$(".si-nunca", els.bannerCerrada).forEach(function (s) { s.hidden = !v.nunca; });
      $$(".si-cerrada", els.bannerCerrada).forEach(function (s) { s.hidden = !!v.nunca; });
    }
  }

  function pintarRespuestasRapidas(lista) {
    if (!els.qrBar) return;
    var firma = JSON.stringify(lista);
    if (firma === ultimasRespuestas) return;
    ultimasRespuestas = firma;
    /* Solo se cambian las respuestas: «⚡ Mis respuestas», marcado en la plantilla,
       se queda al final de la fila. */
    $$(".qr", els.qrBar).forEach(quitar);
    var fin = els.btnRespuestasChat && els.btnRespuestasChat.parentNode === els.qrBar ? els.btnRespuestasChat : null;
    lista.forEach(function (q) {
      if (!q || typeof q.text !== "string") return;
      var b = document.createElement("button");
      b.type = "button";
      b.className = "qr";
      b.dataset.text = q.text;
      b.textContent = q.title || q.text.slice(0, 24);
      if (String(q.title || "").indexOf("★") === 0) b.title = "Respuesta guardada personal";
      els.qrBar.insertBefore(b, fin);
    });
  }

  /* --- acciones del chat (endpoints de app/inbox.py) --- */

  function recargarTrasAccion(phone) {
    cargarChat(phone, { irAlFondo: true });
    ultimoTablero = null;
    load({ silencioso: true });
  }

  function reactivarBot() {
    var phone = state.chat;
    if (!phone) return;
    accionBandeja("/inbox/" + phone + "/bot-on").then(function (r) {
      if (!r.ok) { toast(textoError(r.err)); return; }
      toast("Bot reactivado");
      ultimaCabecera = null;
      recargarTrasAccion(phone);
    });
  }

  function alternarBloqueo(bloqueadoAhora) {
    var phone = state.chat;
    if (!phone) return;
    var ok = window.confirm(bloqueadoAhora ? "¿Desbloquear a este cliente?" : "¿Bloquear a este cliente?");
    if (!ok) return;
    accionBandeja("/inbox/" + phone + "/block").then(function (r) {
      if (!r.ok) { toast(textoError(r.err)); return; }
      toast(bloqueadoAhora ? "Cliente desbloqueado" : "Cliente bloqueado");
      ultimaCabecera = null;
      recargarTrasAccion(phone);
    });
  }

  function ocultarMensaje(id) {
    var phone = state.chat;
    if (!phone || !id) return;
    if (!window.confirm("¿Ocultar este mensaje?")) return;
    accionBandeja("/inbox/" + phone + "/messages/" + id + "/delete").then(function (r) {
      if (!r.ok) { toast(textoError(r.err)); return; }
      toast("Mensaje ocultado");
      recargarTrasAccion(phone);
    });
  }

  /* --- compositor (misma comodidad que static/js/inbox-composer.js) --- */

  function mostrarErrorEnvio(texto) {
    if (!els.sendError) return;
    els.sendError.textContent = texto;
    els.sendError.hidden = false;
  }

  function ocultarErrorEnvio() {
    if (!els.sendError) return;
    els.sendError.hidden = true;
    els.sendError.textContent = "";
  }

  // El textarea crece con el contenido y se detiene en max-height (CSS).
  function crecer() {
    var ta = els.body;
    if (!ta) return;
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
  }

  function peso(bytes) {
    return bytes < 1048576
      ? Math.max(1, Math.round(bytes / 1024)) + " KB"
      : (bytes / 1048576).toFixed(1) + " MB";
  }

  function pintarChip() {
    var chip = els.attachChip;
    var file = els.file;
    if (!chip || !file) return;
    chip.textContent = "";
    var f = file.files && file.files[0];
    if (!f) {
      chip.hidden = true;
      if (els.body) els.body.placeholder = "Escribe un mensaje…";
      return;
    }
    // Se revela ANTES de llenarlo: lo que cambia dentro de un subárbol
    // oculto no lo anuncia la región aria-live.
    chip.hidden = false;
    if (f.type.indexOf("image/") === 0) {
      // data: y no blob: — el CSP del panel es `img-src 'self' data:`.
      var img = document.createElement("img");
      img.alt = "";
      var lector = new FileReader();
      lector.onload = function () { img.src = lector.result; };
      lector.readAsDataURL(f);
      chip.appendChild(img);
    } else {
      var ico = document.createElement("span");
      ico.textContent = "📄";
      ico.setAttribute("aria-hidden", "true");
      chip.appendChild(ico);
    }
    var nombre = document.createElement("span");
    nombre.className = "name";
    nombre.textContent = f.name;
    var tam = document.createElement("span");
    tam.className = "peso";
    tam.textContent = peso(f.size);
    var quitarBtn = document.createElement("button");
    quitarBtn.type = "button";
    quitarBtn.className = "chip-x";
    quitarBtn.textContent = "✕ Quitar";
    quitarBtn.addEventListener("click", function () {
      file.value = "";
      pintarChip();
      file.focus();
    });
    chip.appendChild(nombre);
    chip.appendChild(tam);
    chip.appendChild(quitarBtn);
    if (els.body) els.body.placeholder = "Agrega un texto (opcional)…";
  }

  function limpiarChip() {
    if (els.file) els.file.value = "";
    pintarChip();
  }

  function enviarMensaje(e) {
    e.preventDefault();
    if (enviando || !state.chat || !els.composerForm) return;
    var texto = els.body ? els.body.value.trim() : "";
    var archivo = els.file && els.file.files && els.file.files.length;
    if (!texto && !archivo) {
      // Sin texto ni archivo no hay nada que enviar: se dice por qué.
      if (els.attachChip) {
        els.attachChip.textContent = "Escribe un mensaje o adjunta un archivo.";
        els.attachChip.hidden = false;
      }
      if (els.body) els.body.focus();
      return;
    }
    var phone = state.chat;
    enviando = true;
    if (els.sendBtn) els.sendBtn.disabled = true;
    ocultarErrorEnvio();
    var cuerpo = new FormData(els.composerForm);
    accionBandeja("/inbox/" + phone + "/send", cuerpo)
      .then(function (r) {
        if (!r.ok) {
          mostrarErrorEnvio(textoError(r.err));
          return;
        }
        if (state.chat === phone) {
          els.composerForm.reset();
          limpiarChip();
          crecer();
        }
        toast("Mensaje enviado");
        if (state.chat === phone) recargarTrasAccion(phone);
      })
      .then(function () {
        enviando = false;
        if (els.sendBtn) els.sendBtn.disabled = false;
      }, function () {
        enviando = false;
        if (els.sendBtn) els.sendBtn.disabled = false;
      });
  }

  function wireComposer() {
    var form = els.composerForm;
    var ta = els.body;
    if (!form || !ta) return;
    ta.addEventListener("input", crecer);
    // Enter envía, Shift+Enter salta de línea (como WhatsApp Web). Solo con
    // teclado físico: en un teclado táctil no hay Shift+Enter.
    var tecladoFisico = window.matchMedia && window.matchMedia("(pointer: fine)").matches;
    ta.addEventListener("keydown", function (e) {
      if (tecladoFisico && e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else enviarMensaje(e);
      }
    });
    if (els.file) els.file.addEventListener("change", pintarChip);
    form.addEventListener("submit", enviarMensaje);
    if (els.qrBar) {
      els.qrBar.addEventListener("click", function (e) {
        var b = e.target && e.target.closest ? e.target.closest(".qr") : null;
        if (!b) return;
        ta.value = b.dataset.text || "";
        ta.focus();
        crecer();
      });
    }
  }

  function wireChat() {
    if (els.chatClose) els.chatClose.addEventListener("click", cerrarChat);
    if (els.btnBoard) els.btnBoard.addEventListener("click", cerrarChat);
  }

  /* ------------------------------------------------------------------ */
  /* Sondeo sin recargar                                                 */
  /* ------------------------------------------------------------------ */

  /* El tablero no se repinta a media edición: un arrastre, el formulario de
     alta abierto, un renombrado, un selector de color, un menú o un campo
     con el foco dentro del tablero. */
  function tableroOcupado() {
    if (draggingId !== null || draggingColumnId !== null) return true;
    if (els.dlgColumn && els.dlgColumn.open) return true;
    if (!els.board) return false;
    if ($(".add-contact-form:not([hidden]), .column-rename, .column .color-picker, .column-menu:not([hidden])", els.board)) return true;
    var a = document.activeElement;
    return !!(a && els.board.contains(a) && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }

  function sondearTablero() {
    refrescarTiempos();
    if (document.visibilityState !== "visible" || tableroOcupado()) return;
    load({ silencioso: true });
  }

  function sondearChat() {
    if (state.chat === null || document.visibilityState !== "visible" || enviando) return;
    cargarChat(state.chat, { silencioso: true });
  }

  /* Avisos en vivo (SSE, app/crm.py::crm_eventos). Es ADITIVO: los dos sondeos de arriba siguen
     corriendo con sus mismos tiempos, porque por el stream solo viajan los mensajes — mover una
     tarjeta de etapa, tomar un folio o cerrarlo los sigue trayendo el reloj, igual que hoy. El
     aviso no trae datos: dispara las mismas funciones de siempre, con sus mismas guardas. */
  function conectarEventos() {
    if (!window.EventSource) return;
    /* Sin onerror: EventSource reconecta solo, y mientras no lo logre el sondeo cubre. */
    var fuente = new EventSource("/crm/api/eventos");
    fuente.addEventListener("mensajes", function () {
      if (timerAvisoChat === null) {
        timerAvisoChat = setTimeout(function () {
          timerAvisoChat = null;
          sondearChat();
        }, COALESCENCIA_CHAT_MS);
      }
      /* Con el chat abierto el tablero está oculto: lo mantiene al día el reloj y no hay por qué
         pagar su descarga entera por cada mensaje que entra. */
      if (timerAvisoTablero === null && els.board && !els.board.hidden) {
        timerAvisoTablero = setTimeout(function () {
          timerAvisoTablero = null;
          sondearTablero();
        }, COALESCENCIA_TABLERO_MS);
      }
    });
  }

  /* ------------------------------------------------------------------ */
  /* Arranque                                                            */
  /* ------------------------------------------------------------------ */

  function wireGlobal() {
    if (els.search) {
      els.search.addEventListener("input", function () {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(applySearch, SEARCH_DEBOUNCE_MS);
      });
      /* El evento "search" llega al borrar con la ✕ o al pulsar Enter. */
      els.search.addEventListener("search", function () {
        clearTimeout(searchTimer);
        applySearch();
      });
    }
    if (els.tabs) els.tabs.addEventListener("keydown", onTabsKeydown);
    if (els.btnNewColumn) els.btnNewColumn.addEventListener("click", openNewColumnDialog);
    if (els.btnToggleTray && els.tray) {
      els.btnToggleTray.addEventListener("click", function () {
        var collapsed = els.tray.classList.toggle("is-collapsed");
        els.btnToggleTray.setAttribute("aria-expanded", collapsed ? "false" : "true");
        els.btnToggleTray.textContent = collapsed ? "Mostrar bandeja" : "Ocultar bandeja";
      });
    }
    document.addEventListener("click", function () {
      closeAllMenus();
      closeColorPickers();
    });
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      /* Con un diálogo abierto (nueva etapa, mis respuestas) Escape es suyo: lo cierra
         el navegador y el chat de atrás se queda donde estaba. */
      if (els.root && $("dialog[open]", els.root)) return;
      var habiaMenu = !!(els.board && $(".column-menu:not([hidden]), .column .color-picker", els.board));
      closeAllMenus();
      closeColorPickers();
      if (habiaMenu || state.chat === null) return;
      /* Escape en el chat vuelve al tablero, salvo que se esté escribiendo. */
      var a = document.activeElement;
      if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName) && a !== els.search) return;
      cerrarChat();
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState !== "visible") return;
      sondearTablero();
      sondearChat();
    });
  }

  function applySearch() {
    var value = els.search ? els.search.value.trim() : "";
    if (value === state.search) return;
    state.search = value;
    ultimoTablero = null;
    load();
  }

  function init() {
    var chat = document.getElementById("crm-chat");
    var form = document.getElementById("crm-composer");
    els = {
      root: document.getElementById("crm"),
      search: document.getElementById("crm-search"),
      btnNewColumn: document.getElementById("crm-btn-new-column"),
      btnToggleTray: document.getElementById("crm-btn-toggle-tray"),
      btnBoard: document.getElementById("crm-btn-board"),
      tabs: document.getElementById("crm-tabs-list"), // el tablist, no la fila #crm-tabs
      miembros: document.getElementById("crm-miembros"),
      tray: document.getElementById("crm-tray"),
      trayList: document.getElementById("crm-tray-list"),
      trayCount: document.getElementById("crm-tray-count"),
      board: document.getElementById("crm-board"),
      chat: chat,
      chatAvatar: document.getElementById("crm-chat-avatar"),
      chatName: document.getElementById("crm-chat-name"),
      chatAlias: document.getElementById("crm-chat-alias"),
      chatPhone: document.getElementById("crm-chat-phone"),
      chatBadges: document.getElementById("crm-chat-badges"),
      chatThread: document.getElementById("crm-chat-thread"),
      chatClose: chat ? $(".chat-close", chat) : null,
      composerAbierto: document.getElementById("crm-composer-abierto"),
      windowNote: document.getElementById("crm-window-note"),
      windowIco: document.getElementById("crm-window-ico"),
      windowLeft: document.getElementById("crm-window-left"),
      qrBar: document.getElementById("crm-qr-bar"),
      composerForm: form,
      file: document.getElementById("crm-file"),
      body: document.getElementById("crm-body"),
      attachChip: document.getElementById("crm-attach-chip"),
      sendError: document.getElementById("crm-send-error"),
      sendBtn: form ? $(".btn-send", form) : null,
      bannerCerrada: document.getElementById("crm-banner-cerrada"),
      toast: document.getElementById("crm-toast"),
      dlgColumn: document.getElementById("crm-dlg-column"),
      formColumn: document.getElementById("crm-form-column"),
      columnName: document.getElementById("crm-column-name"),
      columnColor: document.getElementById("crm-column-color"),
      btnColumnCancel: document.getElementById("crm-btn-column-cancel"),
      btnRespuestas: document.getElementById("crm-btn-respuestas"),
      btnRespuestasChat: document.getElementById("crm-btn-respuestas-chat"),
      dlgRespuestas: document.getElementById("crm-dlg-respuestas"),
      formRespuesta: document.getElementById("crm-form-respuesta"),
      respTitulo: document.getElementById("crm-resp-titulo"),
      respTexto: document.getElementById("crm-resp-texto"),
      respError: document.getElementById("crm-resp-error"),
      respTope: document.getElementById("crm-resp-tope"),
      respGuardar: document.getElementById("crm-resp-guardar"),
      respEstado: document.getElementById("crm-resp-estado"),
      respCuenta: document.getElementById("crm-resp-cuenta"),
      respCargando: document.getElementById("crm-resp-cargando"),
      respLista: document.getElementById("crm-resp-lista"),
      respVacio: document.getElementById("crm-resp-vacio"),
      respCerrar: document.getElementById("crm-resp-cerrar")
    };
    var templates = ["crm-tpl-tab", "crm-tpl-miembro", "crm-tpl-tray-item", "crm-tpl-column", "crm-tpl-card", "crm-tpl-bubble"];
    var missing = templates.filter(function (id) { return !document.getElementById(id); });
    if (!els.root || !els.board || !els.tabs || !els.trayList || !els.chat || !els.chatThread || missing.length) {
      toast("La página no tiene la estructura esperada; no se puede iniciar el tablero.");
      return;
    }
    wireGlobal();
    wireBoardColumnDnD();
    wireDialog();
    wireChat();
    wireComposer();
    wireRespuestas();
    load();
    if (DATOS.chat_inicial) abrirChat(DATOS.chat_inicial);
    /* /crm?respuestas=1 es a donde reenvía la ruta vieja /replies. */
    if (DATOS.abrir_respuestas) abrirRespuestas(false);
    setInterval(sondearTablero, SONDEO_TABLERO_MS);
    setInterval(sondearChat, SONDEO_CHAT_MS);
    conectarEventos();
  }

  window.openChat = window.openChat || abrirChat;

  window.CRMKanban = {
    reload: function () { ultimoTablero = null; return load(); },
    getState: function () { return state; },
    openChat: abrirChat,
    closeChat: cerrarChat,
    toast: toast,
    usuario: USUARIO,
    version: VERSION
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
