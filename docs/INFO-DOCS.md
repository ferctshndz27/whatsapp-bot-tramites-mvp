# Documento informativo del agente (`INFO_DOCS_PATH`)

Con `RAG_ENABLED=1` el agente responde dudas libres del cliente ("¿atienden
sábados?", "¿en cuánto entregan?") usando **solo** el corpus público: los
requisitos del catálogo más este documento. Sin él, el agente únicamente
conoce los requisitos y contesta el aviso de siempre a todo lo demás.

Plantilla lista para llenar: [`info.ejemplo.md`](info.ejemplo.md).

## La regla dura

**Solo información que cualquier cliente pueda saber.** Nunca datos de
clientes, nunca información interna del negocio (márgenes, proveedores,
acuerdos), **nunca precios ni montos**: cotizar es del flujo determinista,
que cita el precio textual del catálogo. Si el modelo menciona dinero, un
CURP o un teléfono, la validación en código (`rag._respuesta_segura`) tumba
la respuesta y el cliente cae al flujo normal — el guardrail queda registrado
en `/unmatched` como `rag_descartado`.

## Cómo escribirlo

El archivo se parte en **párrafos separados por una línea en blanco**, y cada
párrafo es un documento independiente del corpus. De ahí las reglas de
redacción:

1. **Un tema por párrafo.** Horarios en uno, tiempos de entrega en otro.
2. **Autocontenido.** El párrafo puede llegarle al modelo solo, sin los
   demás: "Las actas se entregan en 3 días", no "Esas se entregan en 3 días".
3. **Con las palabras del cliente.** Si preguntan "¿cuánto tardan?", el
   párrafo debe decir "tardan"/"entregamos", no "SLA de entrega".
4. **Corto y afirmativo.** El agente responde en máximo 30 palabras; párrafos
   de 1-3 oraciones se resumen bien.
5. **Sin encabezados ni viñetas.** Markdown de títulos no aporta: el corpus
   es texto plano por párrafos.

Lo que no esté aquí, el agente lo responde con un "no sé" que cae al flujo de
siempre (menú o escalada a un asesor). Eso es correcto: es preferible a
inventar.

## Instalación

Copiar la plantilla, llenarla y subirla al VPS:

```bash
scp docs/info.ejemplo.md root@2.25.185.41:/opt/whatsapp-bot-tramites/data/info.md
```

Declarar la ruta **dentro del contenedor** (`./data` está montado en `/data`)
y recrear (`up -d`, no `restart`: no relee el `.env`):

```bash
ssh root@2.25.185.41 "echo 'INFO_DOCS_PATH=/data/info.md' >> /opt/whatsapp-bot-tramites/.env && cd /opt/whatsapp-bot-tramites && docker compose up -d"
```

## Mantenimiento

Actualizar el contenido es reemplazar el archivo — **no requiere deploy**. El
corpus se relee en frío; para que el cambio tome efecto de inmediato basta
recrear el contenedor.

Qué agregar con el tiempo: las preguntas reales que el agente no supo
responder. Viven en `/unmatched` (capa `ninguna`) y son la mejor fuente de
párrafos nuevos.

## Medir el agente

`scripts/eval_nlu.py` corre el corpus dorado contra la cascada real y reporta
precisión por capa y latencia p50/p90. Se ejecuta a mano (sale a labs, tarda
minutos, no es determinista) — nunca en CI:

```bash
python scripts/eval_nlu.py
```

Con `--sin-ollama` mide la línea base de las capas gratis; comparar ambas
corridas dice cuánto aporta realmente el modelo local y qué latencia cuesta.
La "resolución directa" que imprime es el número que vigila `MIN_ACCURACY` en
la suite de regresión.
