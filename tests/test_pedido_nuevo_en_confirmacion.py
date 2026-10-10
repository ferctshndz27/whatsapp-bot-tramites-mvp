"""Un pedido nuevo durante una confirmación no se trata como «quitar de la lista».

Prueba del operador por WhatsApp (2026-10-10, folio #70): el cliente tenía pendiente confirmar
«Descarga de CURP» y «NSS en PDF» y escribió «quiero acta y curp». El bot lo tomó como una
corrección del alcance («quédate solo con lo que nombré de la lista»): de la lista, solo la CURP
coincidía, así que contestó «Va, lo dejamos así 👍 ¿Confirmas 'Descarga de CURP'?» y el acta se
perdió sin aviso. Luego el cliente tuvo que pedir el acta otra vez desde el menú.

La regla de la corrección (`_handle_confirmar`) está para «no solo alta medica basica»: el cliente
nombra PARTE de lo que ya está en la lista. Si el mensaje nombra trámites que NO están en la lista,
es un pedido nuevo y se atiende como tal, igual que si lo hubiera escrito desde el menú.
"""

from app.conversation import handle_incoming
from app.models import Conversation

PHONE = "5215522220000"


def _estado(session) -> str:
    session.expire_all()
    return session.get(Conversation, PHONE).state


def _con_curp_y_nss_pendientes(session) -> None:
    texto = "\n".join(handle_incoming(session, PHONE, "necesito mi curp y mi nss"))
    assert "Confirmas estos 2 trámites" in texto and "NSS en PDF" in texto, f"la preparación cambió: {texto!r}"


def test_un_pedido_nuevo_en_la_confirmacion_no_pierde_lo_que_no_estaba_en_la_lista(session, catalogo_vivo):
    _con_curp_y_nss_pendientes(session)

    texto = "\n".join(handle_incoming(session, PHONE, "quiero acta y curp"))

    assert "lo dejamos así" not in texto, f"lo trató como quitar de la lista: {texto!r}"
    assert "Acta" in texto, f"se perdió el acta que pidió: {texto!r}"
    assert "Descarga de CURP" in texto
    assert "NSS en PDF" not in texto, "el pedido nuevo reemplaza al pendiente"


def test_el_pedido_nuevo_se_atiende_igual_que_desde_el_menu(session, catalogo_vivo):
    """Misma respuesta que daría el bot si el cliente lo hubiera escrito sin nada pendiente."""
    _con_curp_y_nss_pendientes(session)
    durante = handle_incoming(session, PHONE, "quiero acta y curp")
    estado_durante = _estado(session)

    otro = "5215522221111"
    handle_incoming(session, otro, "hola")
    desde_menu = handle_incoming(session, otro, "quiero acta y curp")

    assert durante[-1] == desde_menu[-1]
    session.expire_all()
    assert estado_durante == session.get(Conversation, otro).state


def test_quitar_de_la_lista_sigue_funcionando(session, catalogo_vivo):
    """La corrección del 2026-09-11 no se rompe: nombrar PARTE de la lista deja solo eso."""
    _con_curp_y_nss_pendientes(session)

    texto = "\n".join(handle_incoming(session, PHONE, "no, solo la curp"))

    assert "lo dejamos así" in texto, texto
    assert "Descarga de CURP" in texto and "NSS en PDF" not in texto


def test_corregir_a_otro_tramite_sigue_funcionando(session, catalogo_vivo):
    """Nombrar UN trámite fuera de la lista sigue siendo «me equivoqué, era este»."""
    _con_curp_y_nss_pendientes(session)

    texto = "\n".join(handle_incoming(session, PHONE, "mejor el acta de matrimonio"))

    assert "lo dejamos así" in texto, texto
    assert "Acta de matrimonio" in texto and "Descarga de CURP" not in texto
