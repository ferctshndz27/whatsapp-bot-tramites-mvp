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
from app import messaging
from app.models import PROVIDER_META, PROVIDER_TWILIO, Conversation

PHONE = "525500000000"

# Módulos que SÍ pueden nombrar al adaptador concreto: el adaptador mismo y el
# puerto, que es quien elige entre adaptadores.
PUEDEN_USAR_EL_ADAPTADOR = frozenset({"whatsapp_client.py", "messaging.py"})


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


def test_el_canal_es_el_del_ultimo_entrante(sesion):
    # REGLA: es LA regla del módulo. Conversation.provider guarda por dónde
    # llegó el último mensaje y eso decide por dónde sale la respuesta.
    sesion.add(Conversation(phone=PHONE, provider=PROVIDER_TWILIO))
    sesion.commit()

    assert messaging.canal_de(PHONE) == PROVIDER_TWILIO


def test_enviar_a_un_cliente_de_twilio_falla_ruidosamente_por_ahora(sesion):
    # REGLA: en la Fase 2 el adaptador de Twilio no existe. Caer de vuelta a
    # Meta entregaría el mensaje desde un número que el cliente no reconoce —
    # peor que no enviarlo. Se revienta a propósito hasta la Fase 3.
    sesion.add(Conversation(phone=PHONE, provider=PROVIDER_TWILIO))
    sesion.commit()

    with pytest.raises(NotImplementedError):
        messaging.send_text(PHONE, "hola")


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
