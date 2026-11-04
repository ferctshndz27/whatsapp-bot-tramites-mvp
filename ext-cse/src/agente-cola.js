// La cola de envíos del agente en WhatsApp Web. Es lo único que decide CUÁNDO se escribe un mensaje, y
// su regla es la que pidió el operador: mínimo 20 s entre un mensaje y el siguiente, nunca ráfagas.
//
//  - Un solo envío a la vez y un solo temporizador: no hay dos mensajes en vuelo.
//  - Entre dos mensajes, PAUSA_MS + un azar de 0 a AZAR_MS (una cadencia exacta se ve a máquina). El
//    azar solo suma: la pausa nunca baja de 20 s.
//  - Un mensaje nuevo tampoco sale antes de PAUSA_MS + azar desde que se encoló (el «leyendo»).
//  - Tope por hora (TOPE_HORA): pasado el tope se descarta, no se acumula para soltarlo de golpe.
//  - La cola no crece sin fin (MAX_COLA): si el cliente escribe mucho, lo que sobra se descarta.
//  - Si el envío dice «reintentar» (la persona cambió de chat o está escribiendo), se espera REINTENTO_MS y
//    se prueba otra vez, hasta VENCE_MS; luego se descarta. El reintento no cuenta como envío.
export const PAUSA_MS = 20000;
export const AZAR_MS = 10000;
export const TOPE_HORA = 20;
export const MAX_COLA = 5;
export const REINTENTO_MS = 5000;
export const VENCE_MS = 120000;
const HORA_MS = 3600000;

/**
 * @param {{enviar: (item: any) => Promise<"ok"|"reintentar"|"descartar">, alDescartar?: (item: any, motivo: string) => void,
 *   ahora?: () => number, azar?: () => number, programar?: typeof setTimeout, cancelar?: typeof clearTimeout}} o
 */
export function crearCola({ enviar, alDescartar = () => undefined, ahora = Date.now, azar = Math.random, programar = setTimeout, cancelar = clearTimeout }) {
  const items = [];
  let ultimo = -Infinity; // cuándo salió el último mensaje
  let hechos = []; // instantes de los envíos de la última hora
  let temporizador;
  let enVuelo = false;
  const espera = () => PAUSA_MS + Math.floor(azar() * (AZAR_MS + 1));

  function descartar(item, motivo) {
    alDescartar(item, motivo);
  }

  function agendar() {
    if (temporizador !== undefined || enVuelo || items.length === 0) return;
    const [item] = items;
    const cuando = Math.max(item.noAntes, ultimo + espera());
    temporizador = programar(turno, Math.max(0, cuando - ahora()));
  }

  async function turno() {
    temporizador = undefined;
    const [item] = items;
    if (!item) return;
    const t = ahora();
    if (t > item.vence) {
      items.shift();
      descartar(item, "vencido");
      return agendar();
    }
    hechos = hechos.filter((h) => t - h < HORA_MS);
    if (hechos.length >= TOPE_HORA) {
      items.shift();
      descartar(item, "tope-por-hora");
      return agendar();
    }
    enVuelo = true;
    let resultado;
    try {
      resultado = await enviar(item.dato);
    } catch {
      resultado = "descartar";
    }
    enVuelo = false;
    if (resultado === "reintentar") {
      temporizador = programar(turno, REINTENTO_MS);
      return;
    }
    items.shift();
    if (resultado === "ok") {
      ultimo = ahora();
      hechos.push(ultimo);
    } else {
      descartar(item, "no-se-pudo-enviar");
    }
    agendar();
  }

  return {
    encolar(dato) {
      if (items.length >= MAX_COLA) return descartar({ dato }, "cola-llena");
      const t = ahora();
      items.push({ dato, noAntes: t + espera(), vence: t + VENCE_MS + PAUSA_MS + AZAR_MS });
      agendar();
    },
    /** Quita lo pendiente de un chat (se apagó el agente ahí). */
    quitar(coincide) {
      for (let i = items.length - 1; i >= 0; i--) if (coincide(items[i].dato)) items.splice(i, 1);
    },
    get pendientes() {
      return items.length;
    },
    parar() {
      if (temporizador !== undefined) cancelar(temporizador);
      temporizador = undefined;
      items.length = 0;
    },
  };
}
