import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    def __init__(self):
        self.whatsapp_token = os.environ.get("WHATSAPP_TOKEN", "")
        self.whatsapp_phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.verify_token = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
        self.app_secret = os.environ.get("WHATSAPP_APP_SECRET", "")
        self.db_path = os.environ.get("DB_PATH", "tramites.db")
        self.session_secret = os.environ.get("SESSION_SECRET", "")
        # Cookie de sesión con flag Secure (solo viaja por HTTPS). Default
        # activado para producción; en local sobre HTTP se apaga con
        # SECURE_COOKIES=0 para que el login funcione.
        self.secure_cookies = os.environ.get("SECURE_COOKIES", "1") != "0"
        self.dashboard_username = os.environ.get("DASHBOARD_USERNAME", "")
        self.dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "")
        self.registration_code = os.environ.get("REGISTRATION_CODE", "")
        self.media_dir = os.environ.get("MEDIA_DIR", "media")
        # En producción apunta al volumen persistente (/data/catalog.json):
        # el archivo del repo es solo la semilla inicial y se pisa en cada deploy.
        self.catalog_path = os.environ.get(
            "CATALOG_PATH", os.path.join(os.path.dirname(__file__), "catalog.json")
        )
        # Deducción de intención con Claude (opcional): sin API key el bot
        # usa el matching por palabras clave de siempre.
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.nlu_model = os.environ.get("NLU_MODEL", "claude-opus-4-8")
        # Umbral de la capa difusa (rapidfuzz) para aceptar un servicio con
        # faltas de ortografía. Barandal 70-95: bajarlo de más dispara falsos
        # positivos que cotizan el trámite equivocado a un cliente real.
        self.fuzzy_threshold = max(70, min(95, int(os.environ.get("FUZZY_THRESHOLD", "85"))))
        # Geolocalización de IPs en la bitácora de accesos (ipwho.is,
        # best-effort con cache local). GEOIP_ENABLED=0 la apaga.
        self.geoip_enabled = os.environ.get("GEOIP_ENABLED", "1") != "0"
        # Segunda cuenta de WhatsApp, vía Twilio. Es una cuenta INDEPENDIENTE
        # con su propio número que comparte este CRM; no hay failover con Meta.
        # Sin TWILIO_AUTH_TOKEN el webhook de Twilio rechaza todo: la firma no
        # se puede verificar, y aceptar sin verificar dejaría que cualquiera
        # inyecte mensajes al bot.
        self.twilio_account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.twilio_auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        # Remitente en formato Twilio: "whatsapp:+5215500000000".
        self.twilio_whatsapp_from = os.environ.get("TWILIO_WHATSAPP_FROM", "")
        # URL pública del servicio, SIN diagonal final. Twilio firma la URL
        # COMPLETA que llamó; detrás del proxy, request.url reporta el host
        # interno y http://, así que reconstruirla desde aquí no es un lujo:
        # sin esto fallan TODAS las firmas, no algunas.
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        self.mssql_host = os.environ.get("MSSQL_HOST", "")
        self.mssql_port = int(os.environ.get("MSSQL_PORT", "1433"))
        self.mssql_user = os.environ.get("MSSQL_USER", "sa")
        self.mssql_password = os.environ.get("MSSQL_SA_PASSWORD", "")
        self.mssql_db = os.environ.get("MSSQL_DB", "tramites_archive")
        # Cobranza con Stripe. Sin STRIPE_SECRET_KEY el módulo queda inerte:
        # el panel avisa "Stripe no configurado" y no se escribe ningún cobro.
        self.stripe_secret_key = os.environ.get("STRIPE_SECRET_KEY", "")
        # Distinto en modo prueba y en modo real. Sin él el webhook rechaza
        # TODO con 403 a propósito: un webhook sin verificar es dinero ajeno
        # entrando a nuestra base.
        self.stripe_webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        # Se pinea la versión para que un cambio de forma en la API de Stripe
        # no rompa el aplicador de eventos de un día para otro.
        self.stripe_api_version = os.environ.get("STRIPE_API_VERSION", "")
        # URL pública del panel: Stripe la exige para success_url/cancel_url.
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        # Tope defensivo: un dedazo del asesor no debe poder cobrar $1,000,000.
        self.cobro_monto_maximo_centavos = int(
            os.environ.get("COBRO_MONTO_MAXIMO_CENTAVOS", "10000000")
        )
        # Vigencia del voucher OXXO. Stripe acepta de 1 a 3 días.
        self.oxxo_dias_vencimiento = max(
            1, min(3, int(os.environ.get("OXXO_DIAS_VENCIMIENTO", "3")))
        )


settings = Settings()
