# Bot de WhatsApp para trámites (MVP standalone)

Prototipo desacoplado del ERP de trámites planeado (`~/Descargas/plan-erp-tramites-v2.md`,
donde este bot es el Módulo 4.15). Este MVP existe para validar la idea con el
cliente real antes de esperar al roadmap completo — ver el plan en
`.claude/plans/quirky-greeting-alpaca.md` para el contexto completo.

El bot **nunca** fija precio final, valida documentos, confirma pagos ni cierra
un trámite por sí solo: solo identifica el servicio y crea una solicitud para
que un asesor humano la atienda.

## 1. Instalar

```bash
cd ~/Projects/whatsapp-bot-tramites-mvp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## 2. Crear la app de WhatsApp en Meta

1. Entra a https://developers.facebook.com/apps y crea una app tipo "Business".
2. Agrega el producto "WhatsApp".
3. En "API Setup" verás un **número de prueba** y un **token temporal** (dura
   24h; luego genera uno de "System User" permanente) — cópialos a
   `WHATSAPP_TOKEN` y `WHATSAPP_PHONE_NUMBER_ID` en tu `.env`.
4. En "App Settings > Basic" copia el **App Secret** a `WHATSAPP_APP_SECRET`.
5. Inventa un valor para `WHATSAPP_VERIFY_TOKEN` (cualquier string) — lo vas a
   usar también al configurar el webhook en el paso 4 más abajo.
6. En "API Setup" agrega hasta 5 números de teléfono de prueba (los tuyos o
   los del cliente) para poder probar sin esperar la verificación de negocio.
7. Inicia el trámite de **verificación de negocio** (Meta Business Manager)
   en paralelo — toma días y es necesario para pasar a un número real de
   producción con más de 5 destinatarios.

## 3. Correr el servidor y exponerlo con un túnel

```bash
uvicorn app.main:app --reload --port 8000
```

En otra terminal (necesitas HTTPS público para que Meta pueda llamar tu webhook):

```bash
ngrok http 8000
# o: cloudflared tunnel --url http://localhost:8000
```

## 4. Registrar el webhook en Meta

En "WhatsApp > Configuration > Webhook":
- Callback URL: `https://<tu-url-de-ngrok>/webhook`
- Verify token: el mismo valor que pusiste en `WHATSAPP_VERIFY_TOKEN`
- Suscríbete al campo `messages`.

## 5. Dashboard interno

Con `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` en `.env`, el usuario se crea
automáticamente al arrancar el servidor (si no existe ya). Entra en
`http://localhost:8000/login`.

## 6. Probar

Desde uno de los números de prueba autorizados, escribe cualquier mensaje al
número de WhatsApp de la app y sigue el flujo: nombre → categoría → servicio →
confirmar. La solicitud aparece en el dashboard con el transcript completo.

Escribir "quiero hablar con alguien" en cualquier momento marca la solicitud
como escalada y el bot deja de responder automáticamente (para no chocar con
un asesor humano que ya está atendiendo).

## Correr los tests

```bash
pytest
```

## Fuera de alcance a propósito

Carga de documentos, pagos, motor de precios dinámico, multi-sucursal,
roles/permisos finos, interpretación de lenguaje libre con IA. Ver la sección
"Alcance del MVP" del plan para el detalle completo y el camino de migración
hacia el ERP real.
