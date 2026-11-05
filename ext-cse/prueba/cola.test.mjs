// Pruebas de la cola del agente (src/agente-cola.js) con un reloj falso: node --test ext-cse/prueba/cola.test.mjs
import assert from "node:assert/strict";
import test from "node:test";
import { AZAR_MS, MAX_COLA, PAUSA_MS, TOPE_HORA, VENCE_MS, crearCola } from "../src/agente-cola.js";

// Reloj y temporizadores falsos: `avanzar` corre lo que venza y deja que los envíos async terminen.
function mundo({ azar = () => 0, resultados = [] } = {}) {
  let t = 1_000_000;
  const tareas = [];
  const enviados = [];
  const descartados = [];
  const programar = (fn, ms) => {
    const tarea = { fn, en: t + ms, vivo: true };
    tareas.push(tarea);
    return tarea;
  };
  const cola = crearCola({
    enviar: async (d) => {
      enviados.push({ d, t });
      return resultados.shift() ?? "ok";
    },
    alDescartar: (item, motivo) => descartados.push([item.dato ?? item, motivo]),
    ahora: () => t,
    azar,
    programar,
    cancelar: (x) => {
      x.vivo = false;
    },
  });
  async function avanzar(ms) {
    const fin = t + ms;
    for (;;) {
      const sig = tareas.filter((x) => x.vivo).sort((a, b) => a.en - b.en)[0];
      if (!sig || sig.en > fin) break;
      sig.vivo = false;
      t = Math.max(t, sig.en);
      sig.fn();
      for (let i = 0; i < 5; i++) await Promise.resolve();
    }
    t = fin;
  }
  return { cola, avanzar, enviados, descartados, ahora: () => t };
}

test("un mensaje nuevo no sale antes de 20 s", async () => {
  const m = mundo();
  m.cola.encolar("a");
  await m.avanzar(PAUSA_MS - 1);
  assert.equal(m.enviados.length, 0);
  await m.avanzar(1);
  assert.equal(m.enviados.length, 1);
});

test("nunca dos mensajes con menos de 20 s entre sí, aunque se encolen de golpe", async () => {
  const m = mundo({ azar: Math.random });
  for (const x of "abcde") m.cola.encolar(x);
  await m.avanzar(10 * 60000);
  assert.equal(m.enviados.length, 5);
  for (let i = 1; i < m.enviados.length; i++) assert.ok(m.enviados[i].t - m.enviados[i - 1].t >= PAUSA_MS);
});

test("el azar solo suma: con azar máximo la pausa es 30 s, nunca menos de 20", async () => {
  const m = mundo({ azar: () => 0.99999999 });
  m.cola.encolar("a");
  m.cola.encolar("b");
  await m.avanzar(PAUSA_MS + AZAR_MS);
  assert.equal(m.enviados.length, 1);
  await m.avanzar(PAUSA_MS + AZAR_MS);
  assert.equal(m.enviados.length, 2);
  assert.ok(m.enviados[1].t - m.enviados[0].t >= PAUSA_MS + AZAR_MS);
});

test("reintentar no cuenta como envío y vence a los 2 minutos", async () => {
  const m = mundo({ resultados: Array(100).fill("reintentar") });
  m.cola.encolar("a");
  await m.avanzar(VENCE_MS + PAUSA_MS + AZAR_MS + 10000);
  assert.deepEqual(m.descartados.map((d) => d[1]), ["vencido"]);
  assert.equal(m.cola.pendientes, 0);
});

test("tras reintentar y poder enviar, la pausa corre desde el envío real", async () => {
  const m = mundo({ resultados: ["reintentar", "ok", "ok"] });
  m.cola.encolar("a");
  m.cola.encolar("b");
  await m.avanzar(5 * 60000);
  assert.equal(m.enviados.length, 3); // 1 intento fallido + a + b
  assert.ok(m.enviados[2].t - m.enviados[1].t >= PAUSA_MS);
});

test("la cola tiene tope y lo que sobra se descarta", async () => {
  const m = mundo();
  for (let i = 0; i < MAX_COLA + 2; i++) m.cola.encolar(i);
  assert.equal(m.cola.pendientes, MAX_COLA);
  assert.deepEqual(m.descartados.map((d) => d[1]), ["cola-llena", "cola-llena"]);
});

test("tope por hora: lo que pasa del tope se descarta, no se suelta de golpe", async () => {
  const m = mundo();
  for (let i = 0; i < TOPE_HORA + 3; i++) {
    m.cola.encolar(i);
    await m.avanzar(PAUSA_MS + AZAR_MS + 1);
  }
  assert.equal(m.enviados.length, TOPE_HORA);
  assert.equal(m.descartados.filter((d) => d[1] === "tope-por-hora").length, 3);
});

test("un envío que falla se descarta y el siguiente espera su pausa", async () => {
  const m = mundo({ resultados: ["descartar", "ok"] });
  m.cola.encolar("a");
  m.cola.encolar("b");
  await m.avanzar(3 * 60000);
  assert.deepEqual(m.descartados.map((d) => d[1]), ["no-se-pudo-enviar"]);
  assert.equal(m.enviados.length, 2);
});

test("quitar saca lo pendiente de un chat y parar vacía todo", async () => {
  const m = mundo();
  m.cola.encolar({ chat: 1 });
  m.cola.encolar({ chat: 2 });
  m.cola.quitar((d) => d.chat === 1);
  assert.equal(m.cola.pendientes, 1);
  m.cola.parar();
  await m.avanzar(60000);
  assert.equal(m.enviados.length, 0);
});
