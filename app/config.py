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
        self.dashboard_username = os.environ.get("DASHBOARD_USERNAME", "")
        self.dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "")
        self.registration_code = os.environ.get("REGISTRATION_CODE", "")
        self.media_dir = os.environ.get("MEDIA_DIR", "media")
        self.mssql_host = os.environ.get("MSSQL_HOST", "")
        self.mssql_port = int(os.environ.get("MSSQL_PORT", "1433"))
        self.mssql_user = os.environ.get("MSSQL_USER", "sa")
        self.mssql_password = os.environ.get("MSSQL_SA_PASSWORD", "")
        self.mssql_db = os.environ.get("MSSQL_DB", "tramites_archive")


settings = Settings()
