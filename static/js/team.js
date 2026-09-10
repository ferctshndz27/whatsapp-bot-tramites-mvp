// Chat de equipo. Nada aquí recarga la página: el sondeo pide el hilo y solo
// toca el DOM si cambió, y nunca la caja de texto. Antes la página entera se
// recargaba cada 20 s y el hilo se repintaba completo cada 5 s — parpadeo,
// scroll al fondo a media lectura y el mensaje a medio escribir borrado.
let currentRoomId = null;
let pollInterval = null;
let roomsInterval = null;
let remindersInterval = null;
let ultimoHilo = null;   // HTML del hilo pintado, tal como lo mandó el servidor
let ultimaLista = null;  // ídem, la lista de salas
let adjunto = null;      // File que espera en la vista previa
let adjuntoUrl = null;   // object URL de su miniatura (se libera al quitarlo)

const SONDEO_HILO_MS = 5000;
const SONDEO_SALAS_MS = 20000;
// Mismos tipos y pesos (MB) que ALLOWED_MEDIA en app/media_store.py. Quien
// decide es el servidor; esto solo evita subir 16 MB para enterarse después.
const TIPOS_ADJUNTO = {
    'image/jpeg': 5,
    'image/png': 5,
    'image/webp': 5,
    'application/pdf': 16,
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 16,
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 16,
};

function parsear(html) {
    return new DOMParser().parseFromString(html, 'text/html');
}

function estaAlFondo(el) {
    return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}

// Sesión vencida: mejor ir al login que pintar la página de login dentro del chat.
function sesionVencida(res) {
    if (res.status === 403 || (res.redirected && new URL(res.url).pathname === '/login')) {
        location.href = '/login';
        return true;
    }
    return false;
}

function marcarActiva() {
    document.querySelectorAll('.room-item').forEach(el => {
        const activa = Number(el.dataset.roomId) === currentRoomId;
        el.classList.toggle('active', activa);
        if (activa) el.setAttribute('aria-current', 'true');
        else el.removeAttribute('aria-current');
    });
}

function loadRoom(roomId) {
    if (currentRoomId === roomId) return;
    currentRoomId = roomId;
    ultimoHilo = null;
    marcarActiva();

    // La sala abierta vive en la URL: un F5 (o volver a entrar) regresa a ella.
    const url = new URL(location.href);
    url.searchParams.set('room', roomId);
    history.replaceState(null, '', url);

    fetchRoomContent(true);

    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(() => fetchRoomContent(false), SONDEO_HILO_MS);
}

// `completa`: al abrir la sala se pinta todo (encabezado, hilo, caja de texto).
// En el sondeo solo el hilo y el encabezado, y solo si cambiaron.
function fetchRoomContent(completa) {
    const sala = currentRoomId;
    if (!sala || (!completa && document.hidden)) return;

    fetch(`/team/rooms/${sala}/messages`)
        .then(res => (sesionVencida(res) ? null : res.text()))
        .then(html => {
            // Cambió de sala mientras llegaba la respuesta: esta ya no aplica.
            if (html === null || sala !== currentRoomId) return;
            if (completa) pintarSala(html);
            else actualizarHilo(html, false);
        })
        .catch(() => {}); // Red caída: el siguiente sondeo lo reintenta.
}

function pintarSala(html) {
    limpiarAdjunto(); // la caja de texto nueva no trae el adjunto de la sala anterior
    const chatDiv = document.getElementById('main-chat');
    chatDiv.innerHTML = html;
    const hilo = document.getElementById('chat-messages');
    ultimoHilo = hilo ? parsear(html).getElementById('chat-messages').innerHTML : null;
    if (hilo) hilo.scrollTop = hilo.scrollHeight;
    attachForms();
}

// Sustituye SOLO el hilo (y el encabezado si cambió). El scroll se queda donde
// estaba, salvo que el usuario estuviera al fondo o acabe de enviar.
function actualizarHilo(html, irAlFondo) {
    const nuevo = parsear(html);
    const hiloNuevo = nuevo.getElementById('chat-messages');
    const hilo = document.getElementById('chat-messages');
    if (!hiloNuevo || !hilo) {
        // Perdió el acceso a la sala (el servidor manda el aviso) o aún no
        // había sala pintada: ahí sí se pinta completo.
        pintarSala(html);
        return;
    }

    const cabecera = document.querySelector('#main-chat .chat-header');
    const cabeceraNueva = nuevo.querySelector('.chat-header');
    if (cabecera && cabeceraNueva && cabecera.innerHTML !== cabeceraNueva.innerHTML) {
        cabecera.innerHTML = cabeceraNueva.innerHTML;
    }

    if (hiloNuevo.innerHTML !== ultimoHilo) {
        const pegado = irAlFondo || estaAlFondo(hilo);
        const arriba = hilo.scrollTop;
        hilo.innerHTML = hiloNuevo.innerHTML;
        ultimoHilo = hiloNuevo.innerHTML;
        hilo.scrollTop = pegado ? hilo.scrollHeight : arriba;
    }
    attachForms();
}

// «Eliminar chat» no pasa por aquí: es un POST normal que regresa a /team.
function attachForms() {
    document.querySelectorAll('.send-msg-form, .react-form, .chat-header form:not(.form-eliminar-chat)').forEach(form => {
        form.onsubmit = e => {
            e.preventDefault();
            if (form.dataset.enviando) return; // doble Enter no manda dos veces
            const envio = form.classList.contains('send-msg-form');
            let destino = form.action;
            let cuerpo = new FormData(form);
            if (envio && adjunto) {
                // Con adjunto, el texto viaja como pie del archivo.
                destino = form.dataset.adjuntoAction;
                cuerpo = new FormData();
                cuerpo.append('file', adjunto, adjunto.name);
                cuerpo.append('caption', form.elements.body.value);
            }
            form.dataset.enviando = '1';
            fetch(destino, { method: 'POST', headers: { 'hx-request': 'true' }, body: cuerpo })
                .then(res => {
                    if (sesionVencida(res)) return null;
                    // 422: el servidor rechazó el archivo (tipo o peso) y dice por qué.
                    if (res.status === 422) return res.text().then(motivo => { alert(motivo); return null; });
                    return res.ok ? res.text() : null;
                })
                .then(html => {
                    if (html === null) return;
                    // La caja de texto no se repinta: se vacía y conserva el foco.
                    if (envio) { form.reset(); limpiarAdjunto(); }
                    actualizarHilo(html, envio);
                })
                .catch(() => alert('No se pudo enviar. Revisa tu conexión.'))
                .finally(() => { delete form.dataset.enviando; });
        };
    });
}

// --- Adjuntos: clip, pegar una captura (Ctrl+V) o arrastrar, como WhatsApp ---

function cajaDeTexto() {
    return document.querySelector('#main-chat .send-msg-form input[name="body"]');
}

// Una captura pegada llega como "image.png": se le pone un nombre que diga qué es.
function nombrarCaptura(file) {
    if (file.name && file.name !== 'image.png') return file;
    const sello = new Date().toISOString().slice(0, 19).replace(/[-:]/g, '').replace('T', '-');
    return new File([file], `captura-${sello}.png`, { type: file.type });
}

function elegirAdjunto(file) {
    const previo = document.querySelector('#main-chat .adjunto-previo');
    if (!file || !previo) return; // sin sala abierta no hay a dónde mandarlo
    const limiteMb = TIPOS_ADJUNTO[file.type];
    if (!limiteMb) {
        alert('Ese tipo de archivo no se puede enviar. Usa imagen (JPG, PNG o WEBP), PDF, Word o Excel.');
        return;
    }
    if (file.size > limiteMb * 1024 * 1024) {
        alert(`El archivo es demasiado grande: hasta ${limiteMb} MB.`);
        return;
    }
    limpiarAdjunto();
    adjunto = file;

    const esImagen = file.type.startsWith('image/');
    const miniatura = previo.querySelector('.adjunto-miniatura');
    if (esImagen) {
        adjuntoUrl = URL.createObjectURL(file);
        miniatura.src = adjuntoUrl;
    }
    miniatura.hidden = !esImagen;
    previo.querySelector('.adjunto-doc').hidden = esImagen;
    previo.querySelector('.adjunto-nombre').textContent = file.name;
    previo.hidden = false;

    // Con adjunto, el texto es opcional: pasa a ser el pie del archivo.
    const texto = cajaDeTexto();
    if (texto) {
        texto.dataset.placeholder = texto.dataset.placeholder || texto.placeholder;
        texto.required = false;
        texto.placeholder = 'Añade un comentario (opcional)';
        texto.focus();
    }
}

function limpiarAdjunto() {
    adjunto = null;
    if (adjuntoUrl) {
        URL.revokeObjectURL(adjuntoUrl);
        adjuntoUrl = null;
    }
    const previo = document.querySelector('#main-chat .adjunto-previo');
    if (previo) previo.hidden = true;
    const entrada = document.querySelector('#main-chat .adjunto-input');
    if (entrada) entrada.value = '';
    const texto = cajaDeTexto();
    if (texto) {
        texto.required = true;
        if (texto.dataset.placeholder) texto.placeholder = texto.dataset.placeholder;
    }
}

// La lista de salas también se actualiza sola (un DM que otro abrió contigo),
// sin recargar: se pide la misma página y solo se cambia la lista si cambió.
function refrescarSalas() {
    if (document.hidden) return;
    fetch(location.pathname + location.search)
        .then(res => (sesionVencida(res) || !res.ok ? null : res.text()))
        .then(html => {
            if (html === null) return;
            const nueva = parsear(html).querySelector('.room-list');
            const lista = document.querySelector('.room-list');
            if (!nueva || !lista || nueva.innerHTML === ultimaLista) return;
            lista.innerHTML = nueva.innerHTML;
            ultimaLista = nueva.innerHTML;
            marcarActiva();
        })
        .catch(() => {});
}

function setReminder(messageId) {
    const mins = prompt("¿En cuántos minutos deseas que te recuerde este mensaje?", "10");
    if (mins && !isNaN(mins)) {
        const fd = new FormData();
        fd.append('remind_in_minutes', mins);
        fetch(`/team/messages/${messageId}/remind`, {
            method: 'POST',
            body: fd
        }).then(res => alert(res.ok ? "Recordatorio guardado." : "No se pudo guardar el recordatorio."));
    }
}

// Notificaciones Push para Recordatorios
function checkReminders() {
    fetch('/team/reminders/active')
        .then(res => res.json())
        .then(reminders => {
            reminders.forEach(r => {
                if (Notification.permission === "granted") {
                    new Notification("Recordatorio de Chat", {
                        body: r.author + ": " + r.body
                    });
                } else {
                    alert("Recordatorio de Chat\n" + r.author + ": " + r.body);
                }
            });
        })
        .catch(() => {});
}

// Todo lo de dentro de #main-chat se repinta, así que sus eventos se escuchan
// delegados en #main-chat, que no cambia.
function conectarPanelDelChat(chat) {
    chat.addEventListener('click', e => {
        const boton = e.target.closest('[data-accion]');
        if (!boton) return;
        if (boton.dataset.accion === 'recordar') setReminder(Number(boton.dataset.mensaje));
        else if (boton.dataset.accion === 'quitar-adjunto') limpiarAdjunto();
    });

    chat.addEventListener('change', e => {
        if (e.target.matches('.adjunto-input')) elegirAdjunto(e.target.files[0]);
    });

    chat.addEventListener('submit', e => {
        if (e.target.matches('.form-eliminar-chat') && !confirm(
            '¿Eliminar este chat? Desaparece solo para ti; los demás lo conservan. ' +
            'Si alguien vuelve a escribir, reaparecerá solo con lo nuevo.')) {
            e.preventDefault();
        }
    });

    // Arrastrar y soltar un archivo sobre la conversación.
    chat.addEventListener('dragover', e => {
        if (!e.dataTransfer || !e.dataTransfer.types.includes('Files')) return;
        e.preventDefault();
        chat.classList.add('arrastrando');
    });
    chat.addEventListener('dragleave', e => {
        if (!chat.contains(e.relatedTarget)) chat.classList.remove('arrastrando');
    });
    chat.addEventListener('drop', e => {
        chat.classList.remove('arrastrando');
        const file = e.dataTransfer && e.dataTransfer.files[0];
        if (!file) return;
        e.preventDefault();
        elegirAdjunto(file);
    });
}

document.addEventListener("DOMContentLoaded", () => {
    if (Notification.permission !== "granted" && Notification.permission !== "denied") {
        Notification.requestPermission();
    }

    // Línea base ANTES de marcar la activa: se compara contra lo que manda el servidor.
    const lista = document.querySelector('.room-list');
    if (lista) ultimaLista = parsear(document.documentElement.outerHTML)
        .querySelector('.room-list').innerHTML;

    // Clic en una sala: delegado en la lista, así sigue funcionando cuando
    // refrescarSalas() le reemplaza el contenido.
    if (lista) lista.addEventListener('click', e => {
        const item = e.target.closest('.room-item');
        if (item) loadRoom(Number(item.dataset.roomId));
    });

    // Modal "Crear Sala": se muestra y oculta con el atributo `hidden`.
    const modal = document.getElementById('new-room-modal');
    document.querySelectorAll('[data-accion="abrir-nueva-sala"]')
        .forEach(b => b.addEventListener('click', () => { modal.hidden = false; }));
    document.querySelectorAll('[data-accion="cerrar-nueva-sala"]')
        .forEach(b => b.addEventListener('click', () => { modal.hidden = true; }));

    const chat = document.getElementById('main-chat');
    if (chat) conectarPanelDelChat(chat);

    // Pegar una captura de pantalla (Ctrl+V) la deja lista para enviar. El
    // texto se sigue pegando como siempre: solo se intercepta si trae archivo.
    document.addEventListener('paste', e => {
        const archivos = e.clipboardData ? e.clipboardData.files : [];
        if (!archivos.length || !document.querySelector('#main-chat .send-msg-form')) return;
        e.preventDefault();
        elegirAdjunto(nombrarCaptura(archivos[0]));
    });

    const inicial = window.TEAM_SALA_INICIAL || window.TEAM_GENERAL_ROOM_ID;
    if (inicial) {
        loadRoom(inicial);
    }

    roomsInterval = setInterval(refrescarSalas, SONDEO_SALAS_MS);
    remindersInterval = setInterval(checkReminders, 30000); // Check every 30s

    // Al volver a la pestaña se pone al día de inmediato, sin esperar el sondeo.
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) {
            fetchRoomContent(false);
            refrescarSalas();
        }
    });
});
