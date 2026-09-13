/* Scroll al último mensaje extraído de templates/inbox_chat.html (antes en línea; la CSP ya no lo permite). */
(function () {
  var chat = document.getElementById('chat');
  if (chat) chat.scrollTop = chat.scrollHeight;
})();
