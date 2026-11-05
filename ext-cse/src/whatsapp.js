// Content script de web.whatsapp.com: solo carga «chispa», los botones del pie del chat. Es un script clásico (los
// content scripts no son módulos), así que importa el módulo de la extensión con import().
import(chrome.runtime.getURL("whatsapp-chispa.js")).then((m) => m.montar()).catch((error) => console.error("ext-cse: no se pudo poner chispa", error));
