let currentRoomId = null;
let pollInterval = null;
let remindersInterval = null;

function loadRoom(roomId) {
    if (currentRoomId === roomId) return;
    currentRoomId = roomId;
    
    document.querySelectorAll('.room-item').forEach(el => el.classList.remove('active'));
    document.querySelector(`[data-room-id="${roomId}"]`).classList.add('active');
    
    fetchRoomContent();
    
    if (pollInterval) clearInterval(pollInterval);
    pollInterval = setInterval(fetchRoomContent, 5000);
}

function fetchRoomContent() {
    if (!currentRoomId) return;
    
    fetch(`/team/rooms/${currentRoomId}/messages`)
        .then(res => res.text())
        .then(html => {
            const chatDiv = document.getElementById('main-chat');
            chatDiv.innerHTML = html;
            const msgs = document.getElementById('chat-messages');
            if (msgs) msgs.scrollTop = msgs.scrollHeight;
            
            attachForms();
        });
}

function attachForms() {
    document.querySelectorAll('.send-msg-form, .react-form, .chat-header form').forEach(form => {
        form.onsubmit = e => {
            e.preventDefault();
            const formData = new FormData(form);
            fetch(form.action, {
                method: 'POST',
                headers: { 'hx-request': 'true' },
                body: formData
            }).then(res => res.text()).then(html => {
                const chatDiv = document.getElementById('main-chat');
                chatDiv.innerHTML = html;
                const msgs = document.getElementById('chat-messages');
                if (msgs) msgs.scrollTop = msgs.scrollHeight;
                attachForms();
            });
        };
    });
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
        });
}

document.addEventListener("DOMContentLoaded", () => {
    if (Notification.permission !== "granted" && Notification.permission !== "denied") {
        Notification.requestPermission();
    }
    
    if (window.TEAM_GENERAL_ROOM_ID) {
        loadRoom(window.TEAM_GENERAL_ROOM_ID);
    }
    
    remindersInterval = setInterval(checkReminders, 30000); // Check every 30s
});
