import logging
import os

from dotenv import load_dotenv

load_dotenv()

# Interruptor único de la ruta Claude (API de Anthropic). En False la llave se
# ignora aunque esté en el .env: el agente corre 100% sobre Ollama. No es una
# variable de entorno a propósito — que no se encienda por accidente en el
# servidor; encenderla es un cambio de código, revisado y desplegado.
CLAUDE_HABILITADO = False


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
        # Decisión del negocio (2026-08-22): la capa Claude NO se usa — no hay
        # presupuesto de API y el agente corre entero sobre el Ollama de labs.
        # El apagador vive aquí, donde nace la llave, y no repartido por los
        # siete `if settings.anthropic_api_key` que ya existen: con la llave en
        # blanco todos esos gates quedan cerrados de una vez. El código de la
        # ruta Claude se conserva (y sus pruebas también): está inerte, y el día
        # que el negocio quiera pagar API se reactiva poniendo esto en True.
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if self.anthropic_api_key and not CLAUDE_HABILITADO:
            logging.getLogger(__name__).warning(
                "ANTHROPIC_API_KEY presente pero IGNORADA: la capa Claude está "
                "apagada en config.CLAUDE_HABILITADO. El agente usa Ollama."
            )
            self.anthropic_api_key = ""
        self.nlu_model = os.environ.get("NLU_MODEL", "claude-opus-4-8")
        # Capa informativa ("rag"): responde dudas de horarios/tiempos/requisitos
        # usando SOLO el corpus público (catálogo + INFO_DOCS_PATH). Apagada por
        # defecto; requiere ANTHROPIC_API_KEY. Ver app/rag.py.
        self.rag_enabled = os.environ.get("RAG_ENABLED", "0") == "1"
        # Nodo agéntico con herramientas de SOLO LECTURA (cotizar, folios).
        # Apagado por defecto; corre sobre OLLAMA_URL (o ANTHROPIC_API_KEY
        # como alternativa). Último recurso de _not_understood — nunca ruta
        # principal. Ver app/agent_tools.py.
        self.agent_tools_enabled = os.environ.get("AGENT_TOOLS_ENABLED", "0") == "1"
        # Encendido POR HERRAMIENTA, encima del permiso de agent_tools.PERMISOS
        # (que es el piso de seguridad y NO se toca desde el panel). Sirve para
        # "hoy no quiero que cotice, solo que consulte folios" sin apagar el
        # nodo entero. "cobrar" no tiene flag a propósito: es deny por
        # construcción y mover dinero jamás es del modelo.
        self.tool_cotizar_enabled = os.environ.get("TOOL_COTIZAR_ENABLED", "1") == "1"
        self.tool_folios_enabled = os.environ.get("TOOL_FOLIOS_ENABLED", "1") == "1"
        # Topes del bucle agéntico. Los defaults son espejo de
        # agent_tools.MAX_TURNOS / MAX_RESPONSE_WORDS (un test lo verifica);
        # aquí viven para poder ajustarlos en caliente desde /agente.
        self.agent_max_turnos = int(os.environ.get("AGENT_MAX_TURNOS", "3"))
        self.agent_max_words = int(os.environ.get("AGENT_MAX_WORDS", "80"))
        # Documento curado OPCIONAL con información pública del negocio
        # (horarios, tiempos de entrega, políticas). Regla dura: solo texto que
        # cualquier cliente puede saber — jamás datos de clientes ni internos.
        self.info_docs_path = os.environ.get("INFO_DOCS_PATH", "")
        # Paso vectorial OPCIONAL: con EMBEDDINGS_URL definida la capa
        # informativa recupera top-k documentos por similitud en vez de mandar
        # el corpus completo (que hoy cabe de sobra en el prompt).
        self.embeddings_url = os.environ.get("EMBEDDINGS_URL", "")
        self.embeddings_model = os.environ.get("EMBEDDINGS_MODEL", "nomic-embed-text")
        self.embeddings_token = os.environ.get("EMBEDDINGS_TOKEN", "")
        self.rag_top_k = max(1, int(os.environ.get("RAG_TOP_K", "8")))
        # Clasificador local (Ollama, p.ej. detrás de nginx en la instancia
        # labs). Sin OLLAMA_URL la capa no existe. Corre después del atajo
        # literal y antes de Claude: hace el mismo trabajo gratis, y si falla
        # o no decide, la cadena sigue. URL sin diagonal final y sin /api:
        # p.ej. https://labs.esaumillan.cloud/ollama
        self.ollama_url = os.environ.get("OLLAMA_URL", "").rstrip("/")
        self.ollama_token = os.environ.get("OLLAMA_TOKEN", "")
        # El default acepta "think": false (clasificar un id no necesita
        # cadena de razonamiento y sí castiga la latencia). Medido contra el
        # catálogo real en CPU ARM: 4b y 8b aciertan igual (14/14), pero el 8b
        # tiene mediana 7.1s contra 5.4s del 4b — por eso el chico es default.
        self.ollama_model = os.environ.get("OLLAMA_MODEL", "qwen3:4b")
        # Si el modelo local no contesta en este lapso, responde la siguiente
        # capa. En CPU el costo está en releer el catálogo del prompt, no en
        # generar: con p90 medido de 6.0s, un tope de 6s cortaría la mitad de
        # las respuestas buenas.
        self.ollama_timeout = float(os.environ.get("OLLAMA_TIMEOUT", "12"))
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
        # Plantilla twilio/quick-reply de la encuesta de cierre (ContentSid,
        # "HX..."). Twilio no manda botones sin plantilla; sin esto la encuesta
        # sale como texto con las opciones numeradas, que se sigue pudiendo
        # contestar tecleando.
        self.twilio_content_sid_feedback = os.environ.get("TWILIO_CONTENT_SID_FEEDBACK", "")
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

        # Tope de folios vivos por asesor para el reparto automático (fase 3b,
        # app/asignacion.py). Con un tope alcanzado el folio se queda SIN
        # asignar, que es la cola que el equipo ya sabe mirar.
        # ponytail: 15 es un número elegido, no medido: con ~1 día de datos de
        # tiempos cualquier calibración sería ruido. Lo que sí tiene historial
        # largo es TicketActivity — cuando haya semanas de folios cerrados,
        # calibrar contra la mediana de folios vivos por asesor. Barandal
        # 1-200: un 0 apagaría el reparto en silencio y un tope enorme lo
        # volvería un round-robin sin freno.
        self.carga_maxima_asesor = max(
            1, min(200, int(os.environ.get("CARGA_MAXIMA_ASESOR", "15")))
        )

        # Interruptor del reparto (fase 3b). APAGADO por default, aquí y en el
        # panel: repartir folios solos cambia a quién le aparece el trabajo en
        # /my, y eso lo enciende un admin desde el Estudio cuando el equipo
        # está listo — no un despliegue. Con esto en False el alta sigue
        # dejando los folios sin dueño, exactamente como antes de la fase.
        self.reparto_automatico = os.environ.get("REPARTO_AUTOMATICO", "0") == "1"


settings = Settings()
