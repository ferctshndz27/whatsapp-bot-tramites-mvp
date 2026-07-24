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
        self.mssql_host = os.environ.get("MSSQL_HOST", "")
        self.mssql_port = int(os.environ.get("MSSQL_PORT", "1433"))
        self.mssql_user = os.environ.get("MSSQL_USER", "sa")
        self.mssql_password = os.environ.get("MSSQL_SA_PASSWORD", "")
        self.mssql_db = os.environ.get("MSSQL_DB", "tramites_archive")


settings = Settings()
