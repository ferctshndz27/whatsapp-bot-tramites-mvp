// Barra lateral de módulos, como la de MI CIERRO: una columna angosta de íconos y, al lado, la vista
// del módulo elegido. Las entradas son las del menú del ERP (templates/base.html), en su orden.
// `ruta`: la vista del ERP, relativa al CRM. `listo`: la vista ya se conectó y se revisó (plan
// ext-vistas, una por una); sin él, elegir el módulo solo lo marca y no pide nada al CRM.
import { ICONOS } from "./iconos.js";

export const MODULOS = [
  { id: "dia", ruta: "my", listo: true, nombre: "Mi día", grupo: "Operación" },
  { id: "cartera", ruta: "my/cobros", nombre: "Mi cartera", grupo: "Operación", listo: true },
  { id: "nuevocobro", ruta: "cobros/nuevo", nombre: "Nuevo cobro", grupo: "Operación", listo: true },
  { id: "solicitudes", ruta: "", listo: true, nombre: "Solicitudes", grupo: "Operación" },
  { id: "whatsapp", ruta: "crm", listo: true, nombre: "WhatsApp", grupo: "Operación" },
  { id: "clientes", ruta: "clients", listo: true, nombre: "Clientes", grupo: "Operación" },
  { id: "equipo", ruta: "team", nombre: "Equipo", grupo: "Operación" },
  { id: "actividad", ruta: "activity", listo: true, nombre: "Actividad", grupo: "Operación" },
  { id: "guia", ruta: "kb", listo: true, nombre: "Guía de trámites", grupo: "Operación" },
  { id: "catalogo", ruta: "catalog", listo: true, nombre: "Catálogo", grupo: "Operación" },
  { id: "invitaciones", ruta: "invitations", nombre: "Invitaciones", grupo: "Administración" },
  { id: "oficinas", ruta: "oficinas", nombre: "Oficinas", grupo: "Administración" },
  { id: "usuarios", ruta: "users", nombre: "Usuarios", grupo: "Administración" },
  { id: "cobros", ruta: "cobros", nombre: "Cobros", grupo: "Administración" },
  { id: "accesos", ruta: "logins", nombre: "Accesos", grupo: "Administración" },
  { id: "agente", ruta: "unmatched", nombre: "Agente WhatsApp", grupo: "Administración" },
  { id: "estudio", ruta: "agente", nombre: "Estudio del Agente", grupo: "Administración" },
  { id: "sistema", ruta: "system", nombre: "Sistema", grupo: "Administración" },
];

// Pinta la barra dentro de `nav` y llama a `alElegir(modulo)` con cada clic. Devuelve `elegir(id)`.
// `permitidos`: los id que el CRM dice que esta sesión puede abrir (GET /sesion); sin ellos no se pinta
// ningún módulo. Es cortesía: cada ruta del CRM conserva su propia guardia. Se vuelve a pintar entera
// cada vez, porque quien entra después puede ser otro usuario con otro rol.
export function pintarBarra(nav, alElegir, permitidos) {
  nav.querySelector("ul")?.remove();
  const lista = document.createElement("ul");
  const botones = new Map();
  let grupo;
  for (const modulo of MODULOS.filter((m) => permitidos?.includes(m.id))) {
    const item = document.createElement("li");
    if (grupo && grupo !== modulo.grupo) item.className = "separador";
    grupo = modulo.grupo;
    const boton = document.createElement("button");
    boton.type = "button";
    boton.dataset.id = modulo.id;
    boton.dataset.tip = modulo.nombre;
    boton.setAttribute("aria-label", modulo.nombre);
    boton.insertAdjacentHTML("beforeend", ICONOS[modulo.id]);
    boton.addEventListener("click", () => elegir(modulo.id));
    item.append(boton);
    lista.append(item);
    botones.set(modulo.id, boton);
  }
  nav.append(lista);

  function elegir(id) {
    for (const [clave, boton] of botones) {
      if (clave === id) boton.setAttribute("aria-current", "page");
      else boton.removeAttribute("aria-current");
    }
    alElegir(MODULOS.find((m) => m.id === id));
  }
  return elegir;
}

// Pone en el botón de un módulo cuántas cosas esperan atención: un globo con el número, el rótulo y la
// etiqueta para el lector de pantalla con la cuenta, y la clase `con-pendientes` (el globo es el
// estado; el número es el segundo canal, no solo el color). 0 lo deja como estaba. Pasa de 99 → «99+».
export function marcarPendientes(nav, id, cantidad) {
  const boton = nav.querySelector(`button[data-id="${id}"]`);
  if (!boton) return;
  const modulo = MODULOS.find((m) => m.id === id);
  const n = Number.isFinite(cantidad) && cantidad > 0 ? Math.floor(cantidad) : 0;
  boton.querySelector(".pendientes")?.remove();
  boton.classList.toggle("con-pendientes", n > 0);
  if (n === 0) {
    boton.dataset.tip = modulo.nombre;
    boton.setAttribute("aria-label", modulo.nombre);
    return;
  }
  const globo = document.createElement("span");
  globo.className = "pendientes";
  globo.setAttribute("aria-hidden", "true");
  globo.textContent = n > 99 ? "99+" : String(n);
  boton.append(globo);
  const texto = `${modulo.nombre}: ${n} ${n === 1 ? "mensaje sin atender" : "mensajes sin atender"}`;
  boton.dataset.tip = texto;
  boton.setAttribute("aria-label", texto);
}
