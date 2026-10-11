"""Siembra una base temporal para el QA en navegador del CRM (/crm): la corre scripts/qa_crm_navegador.mjs.

Se corre con las mismas variables de entorno que el servidor (DB_PATH apunta a la base temporal).
Misma familia de datos que tests/test_crm.py: dos oficinas, un admin global, un admin de oficina y
un colaborador por oficina, y clientes con ventanas de 24 h en los tres estados (abierta, urgente,
caducada, nunca abierta).
"""

import os
import sys
from datetime import timedelta

REPO = os.environ["CRM_QA_REPO"]
sys.path.insert(0, REPO)
os.chdir(REPO)

import bcrypt  # noqa: E402
from sqlmodel import Session, select  # noqa: E402

import app.db as db  # noqa: E402
from app.models import (  # noqa: E402
    Client,
    Cobro,
    Conversation,
    CrmEtapa,
    Direction,
    Message,
    Office,
    Ticket,
    TicketStatus,
    User,
    utcnow,
)

CLAVE = os.environ["CRM_QA_CLAVE"]
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()

ANA = "525500000011"
BETO = "525500000012"
DIEGO = "525500000013"
ELENA = "525500000014"
FELIPE = "525500000015"
CARLA = "522200000021"

db.init_db()
ahora = utcnow()
with Session(db.engine) as s:
    etapas = s.exec(select(CrmEtapa).order_by(CrmEtapa.ord)).all()
    ids = [e.id for e in etapas]
    s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
    for nombre, admin, solo, oficina, estado in (
        ("jefa1", True, False, 1, "disponible"),
        ("jefe2", True, True, 2, "disponible"),
        ("colab1", False, False, 1, "ocupado"),
        ("colab2", False, False, 2, "ausente"),
    ):
        s.add(User(username=nombre, password_hash=HASH, is_admin=admin,
                   solo_su_oficina=solo, office_id=oficina, estado=estado))

    s.add(Client(phone=ANA, name="Ana Lucía Ramírez", office_id=1,
                 created_at=ahora - timedelta(days=3)))
    s.add(Client(phone=BETO, name="Beto Nuñez", office_id=1,
                 created_at=ahora - timedelta(days=2)))
    s.add(Client(phone=DIEGO, name="Diego Solís", office_id=1, etapa_crm_id=ids[2],
                 etapa_crm_at=ahora - timedelta(days=1), etapa_crm_por="colab1",
                 created_at=ahora - timedelta(days=9)))
    s.add(Client(phone=ELENA, name="Elena Ortiz", office_id=1, etapa_crm_id=ids[1],
                 etapa_crm_at=ahora - timedelta(hours=5), etapa_crm_por="jefa1",
                 created_at=ahora - timedelta(days=4)))
    s.add(Client(phone=FELIPE, name=None, office_id=1, created_at=ahora - timedelta(hours=1)))
    s.add(Client(phone=CARLA, name="Carla Pérez", office_id=2))

    s.add(Conversation(phone=ANA, state="escalado", office_id=1))
    s.add(Conversation(phone=ELENA, state="inicio", office_id=1))
    folio_ana = Ticket(phone=ANA, client_name="Ana Lucía Ramírez", category="Actas",
                       service_id="acta", service_name="Acta de nacimiento", price_info="$150",
                       status=TicketStatus.TOMADO, assigned_to="colab1", office_id=1)
    s.add(folio_ana)
    s.add(Ticket(phone=DIEGO, client_name="Diego Solís", category="CURP",
                 service_id="curp", service_name="CURP certificada", price_info="$80",
                 status=TicketStatus.CERRADO, assigned_to="jefa1", office_id=1))
    # Elena: DOS trámites en curso en un mismo chat (criterio 9b): la cabecera los lista y el
    # compositor pregunta sobre cuál se contesta. El primero ya lo tomó jefa1; el segundo
    # sigue NUEVO y sin dueño.
    s.add(Ticket(phone=ELENA, client_name="Elena Ortiz", category="Actas",
                 service_id="acta", service_name="Acta de nacimiento", price_info="$150",
                 status=TicketStatus.TOMADO, assigned_to="jefa1", office_id=1,
                 created_at=ahora - timedelta(hours=23)))
    s.add(Ticket(phone=ELENA, client_name="Elena Ortiz", category="Pasaportes",
                 service_id="pasaporte", service_name="Cita de pasaporte", price_info="$300",
                 status=TicketStatus.NUEVO, office_id=1,
                 created_at=ahora - timedelta(hours=22)))

    # Ana: ventana abierta, dos entrantes sin contestar.
    s.add(Message(phone=ANA, direction=Direction.IN, body="Hola, ¿cuánto cuesta un acta?",
                  created_at=ahora - timedelta(hours=3)))
    s.add(Message(phone=ANA, direction=Direction.OUT, body="Hola Ana, son $150.",
                  sent_by="colab1", wa_status="read", created_at=ahora - timedelta(hours=2)))
    # Ligado al folio por el bot (casos-analisis, regla del folio): su burbuja lleva el folio.
    transferencia = Message(phone=ANA, direction=Direction.IN, body="¿Aceptan transferencia?",
                            created_at=ahora - timedelta(minutes=30))
    s.add(transferencia)
    s.add(Message(phone=ANA, direction=Direction.IN, body="Y ¿cuánto tarda?",
                  created_at=ahora - timedelta(minutes=5)))
    # Diego: la ventana caducó hace días; el último es un saliente entregado.
    s.add(Message(phone=DIEGO, direction=Direction.IN, body="¿Ya quedó mi CURP?",
                  created_at=ahora - timedelta(days=3)))
    s.add(Message(phone=DIEGO, direction=Direction.OUT, body="Sí, ya puedes pasar por ella.",
                  sent_by="jefa1", wa_status="delivered", created_at=ahora - timedelta(days=2)))
    # Elena: ventana urgente (escribió hace 22 h); último saliente solo «sent».
    s.add(Message(phone=ELENA, direction=Direction.IN, body="Buenas tardes, ¿tienen citas mañana?",
                  created_at=ahora - timedelta(hours=22)))
    s.add(Message(phone=ELENA, direction=Direction.OUT, body="Sí, a las 10:00.",
                  sent_by="jefa1", wa_status="sent", created_at=ahora - timedelta(hours=21)))
    # Felipe: sin nombre, un entrante con adjunto.
    s.add(Message(phone=FELIPE, direction=Direction.IN, body="[archivo adjunto]",
                  media_path="/media/qa-no-existe.jpg", created_at=ahora - timedelta(hours=1)))
    # Carla (Puebla): un entrante reciente.
    s.add(Message(phone=CARLA, direction=Direction.IN, body="Hola desde Puebla",
                  created_at=ahora - timedelta(minutes=50)))
    s.commit()
    # Un cobro pendiente en el folio de Ana: el cajón del chat resume su estado.
    s.refresh(folio_ana)
    transferencia.ticket_id = folio_ana.id
    s.add(transferencia)
    s.add(Cobro(ticket_id=folio_ana.id, phone=ANA,
                concepto=f"Acta de nacimiento (folio #{folio_ana.id})",
                monto_centavos=15000, estado="pendiente", creado_por="colab1",
                token_verificacion="qa-cobro-ana", url_pago="https://pagos.example/qa-ana"))
    s.commit()

print("siembra lista:", {"etapas": ids, "clientes": [ANA, BETO, DIEGO, ELENA, FELIPE, CARLA]})
