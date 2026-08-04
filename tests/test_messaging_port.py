"""El puerto de mensajería: quien envía dice a QUIÉN, nunca POR DÓNDE.

REGLA DE NEGOCIO: son dos cuentas de WhatsApp independientes, cada una con su
número. La ventana de 24 h es POR NÚMERO, así que responderle a alguien por el
canal equivocado sale desde un remitente que no reconoce y, fuera de ventana, ni
siquiera se entrega. La regla "se contesta por donde entró" vive en
app/messaging.py y en ningún otro lado.

La prueba más valiosa del archivo es la última: un candado de arquitectura que
impide que un módulo nuevo —cobranza, por ejemplo— vuelva a llamar al adaptador
de Meta directo. Ese atajo no rompe nada hoy (solo existe Meta) y rompería en
silencio el día que haya un cliente en Twilio.
"""

import ast
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

import app.db as db
from app import messaging, twilio_client, whatsapp_client
from app.config import settings
from app.models import PROVIDER_META, PROVIDER_TWILIO, Conversation

PHONE = "525500000000"

# Módulos que SÍ pueden nombrar a un adaptador concreto: los adaptadores mismos
# y el puerto, que es quien elige entre ellos.
PUEDEN_USAR_EL_ADAPTADOR = frozenset({"whatsapp_client.py", "twilio_client.py", "messaging.py"})

# Excepción declarada, no escondida: webhook.py nombra a twilio_client para
# ENTRAR (verificar la firma y parsear el form), no para enviar. La entrada es
# irreduciblemente específica de cada proveedor — Meta manda JSON firmado con
# SHA256 y Twilio form-urlencoded firmado con SHA1 sobre la URL. Lo que el
# candado protege es la SALIDA, y ahí webhook.py sí pasa por el puerto.
ENTRADA_ESPECIFICA_DEL_PROVEEDOR = frozenset({"webhook.py"})


@pytest.fixture()
def sesion(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    with Session(engine) as s:
        yield s


# ---------------------------------------------------------------------------
# Resolución del canal
# ---------------------------------------------------------------------------


def test_un_cliente_sin_conversacion_cae_en_meta(sesion):
    # REGLA: un cliente dado de alta a mano desde el panel todavía no ha
    # escrito, así que no tiene canal. Cae en Meta, la cuenta que existía
    # primero — nunca en "ninguno", que dejaría el mensaje sin enviar.
    assert messaging.canal_de(PHONE) == PROVIDER_META


def test_sin_credenciales_de_twilio_nadie_sale_por_ese_canal(sesion, monkeypatch):
    """REGLA: esta prueba es la que respalda mergear el módulo a main ANTES de
    activar la cuenta de Twilio. Con el entorno en blanco —que es el estado de
    producción mientras las credenciales no estén puestas— el código nuevo
    existe pero no se ejecuta nunca. Son tres condiciones y hacen falta las
    tres:

      1. `enabled()` es False, así que nada da el canal por disponible.
      2. La firma se rechaza siempre, así que NADIE puede crear desde fuera una
         Conversation con provider='twilio' — que es el único camino por el que
         un teléfono cambia de canal.
      3. Y sin esa fila, el puerto resuelve a Meta y devuelve su adaptador.

    Si alguna deja de cumplirse, mergear en estado inerte deja de ser seguro y
    esto tiene que fallar en CI antes de llegar al VPS.
    """
    monkeypatch.setattr(settings, "twilio_account_sid", "")
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    monkeypatch.setattr(settings, "twilio_whatsapp_from", "")

    assert twilio_client.enabled() is False

    # Ni siquiera una firma bien formada entra: sin token no hay con qué
    # compararla, y aceptar sin verificar dejaría inyectar mensajes ajenos.
    assert (
        twilio_client.verify_signature(
            "https://tramites.example.mx/webhook/twilio",
            {"From": f"whatsapp:+{PHONE}", "Body": "hola"},
            "kZk9c2FtcGxlIHNpZ25hdHVyZQ==",
        )
        is False
    )

    assert messaging.canal_de(PHONE) == PROVIDER_META
    assert messaging._adaptador(PROVIDER_META) is whatsapp_client


def test_el_canal_es_el_del_ultimo_entrante(sesion):
    # REGLA: es LA regla del módulo. Conversation.provider guarda por dónde
    # llegó el último mensaje y eso decide por dónde sale la respuesta.
    sesion.add(Conversation(phone=PHONE, provider=PROVIDER_TWILIO))
    sesion.commit()

    assert messaging.canal_de(PHONE) == PROVIDER_TWILIO


def test_un_cliente_de_twilio_se_atiende_por_twilio(sesion, monkeypatch):
    # REGLA: es el corazón del módulo. Si esto enrutara a Meta, el cliente
    # recibiría el mensaje desde un número que no reconoce y —fuera de la
    # ventana de 24 h de ESE número— no lo recibiría en absoluto.
    sesion.add(Conversation(phone=PHONE, provider=PROVIDER_TWILIO))
    sesion.commit()
    por_meta, por_twilio = [], []
    monkeypatch.setattr(messaging.whatsapp_client, "send_text", lambda p, b: por_meta.append(b))
    monkeypatch.setattr(
        messaging.twilio_client, "send_text", lambda p, b: por_twilio.append(b) or "SM1"
    )

    assert messaging.send_text(PHONE, "hola") == "SM1"
    assert por_twilio == ["hola"]
    assert por_meta == [], "el mensaje salió por la cuenta equivocada"


def test_el_canal_explicito_gana_sobre_el_de_la_conversacion(sesion, monkeypatch):
    # REGLA: el parámetro existe para quien YA tiene la conversación en la mano
    # y quiere ahorrarse la consulta. Si no ganara, sería decorativo.
    sesion.add(Conversation(phone=PHONE, provider=PROVIDER_TWILIO))
    sesion.commit()
    enviados = []
    monkeypatch.setattr(
        messaging.whatsapp_client, "send_text", lambda p, b: enviados.append((p, b)) or "wamid.X"
    )

    assert messaging.send_text(PHONE, "hola", provider=PROVIDER_META) == "wamid.X"
    assert enviados == [(PHONE, "hola")]


def test_el_puerto_devuelve_el_wamid_del_adaptador(sesion, monkeypatch):
    # REGLA: el wamid es lo que después empareja el estado de entrega con la
    # fila del mensaje (webhook._tag_sent_message). Un puerto que se lo comiera
    # dejaría todos los salientes sin taggear.
    monkeypatch.setattr(messaging.whatsapp_client, "send_text", lambda p, b: "wamid.ABC")

    assert messaging.send_text(PHONE, "hola") == "wamid.ABC"


# ---------------------------------------------------------------------------
# Candado de arquitectura
# ---------------------------------------------------------------------------


def _modulos_que_nombran(simbolo: str) -> list[str]:
    """Archivos de app/ que mencionan `simbolo` en su código (no en comentarios)."""
    encontrados = []
    for ruta in sorted(Path("app").glob("*.py")):
        if ruta.name in PUEDEN_USAR_EL_ADAPTADOR:
            continue
        arbol = ast.parse(ruta.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            usa = (
                (isinstance(nodo, ast.Name) and nodo.id == simbolo)
                or (isinstance(nodo, ast.Attribute) and nodo.attr == simbolo)
                or (isinstance(nodo, ast.ImportFrom) and any(a.name == simbolo for a in nodo.names))
                or (isinstance(nodo, ast.Import) and any(simbolo in a.name for a in nodo.names))
            )
            if usa:
                encontrados.append(ruta.name)
                break
    return encontrados


def test_ningun_modulo_esquiva_el_puerto():
    """REGLA: llamar a whatsapp_client directo manda SIEMPRE por Meta, ignorando
    el canal de la conversación. Hoy no rompe nada porque solo existe Meta; el
    día que haya un cliente en Twilio, ese módulo le escribiría desde el número
    equivocado y fuera de ventana. Falla en silencio, así que se vigila aquí.
    """
    infractores = _modulos_que_nombran("whatsapp_client")

    assert not infractores, (
        f"Módulos de app/ que usan el adaptador de Meta en vez del puerto: "
        f"{infractores}. Manda por app.messaging (send_text/send_list/"
        f"send_buttons/send_media): es quien sabe por cuál de las dos cuentas "
        f"contestarle a ese cliente."
    )


def test_solo_el_webhook_toca_el_adaptador_de_twilio():
    """REGLA: misma protección para el segundo adaptador, con una excepción
    declarada. webhook.py lo necesita para ENTRAR (firma y parseo del form, que
    no tienen equivalente genérico); cualquier otro módulo que lo nombre está
    enviando por fuera del puerto y le escribiría al cliente desde el número
    equivocado.
    """
    infractores = sorted(
        set(_modulos_que_nombran("twilio_client")) - ENTRADA_ESPECIFICA_DEL_PROVEEDOR
    )

    assert not infractores, (
        f"Módulos de app/ que tocan el adaptador de Twilio fuera del puerto: "
        f"{infractores}. Si es para ENVIAR, usa app.messaging. Si de verdad es "
        f"entrada específica del proveedor, decláralo en "
        f"ENTRADA_ESPECIFICA_DEL_PROVEEDOR con el motivo."
    )


def test_la_excepcion_declarada_sigue_existiendo():
    """REGLA: una lista blanca que apunta a un módulo fantasma deja de proteger
    y taparía un olvido real (mismo criterio que el espejo MSSQL)."""
    fantasma = sorted(
        nombre for nombre in ENTRADA_ESPECIFICA_DEL_PROVEEDOR if not Path("app", nombre).exists()
    )

    assert not fantasma, f"Excepciones declaradas que ya no existen en app/: {fantasma}"
