"""La ficha del folio muestra los archivos del cliente (operador, 2026-10-12).

El operador pidió que, si el cliente manda archivos o fotos, el agente procure a qué folio pertenecen
«para que [el asesor] sepa y los vea en la ficha del ticket». El bot ya pregunta y liga el archivo al
folio (`Message.ticket_id`), pero la ficha (`/tickets/{id}`) no mostraba NINGÚN archivo: solo el chat
del CRM los pintaba, con la etiqueta del folio. Esta tarjeta los lista ahí.

Los archivos no viven en el VPS (regla del 2026-09-28): la tarjeta lista la referencia y el enlace va
al visor `/crm/api/adjunto/{id}`, que decide con su propio permiso quién puede abrirlo.

Si una de estas pruebas se pone roja, el asesor no ve en la ficha los archivos de SU folio, ve los de
otro folio u otro cliente, abre un enlace que el visor le va a negar, o un nombre de archivo del
cliente se ejecuta como HTML en su sesión.
"""

from datetime import timedelta

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine

import app.db as db
from app.catalog import catalog
from app.config import settings
from app.main import app
from app.models import Client, Direction, Message, Ticket, TicketStatus, User, utcnow

CLAVE = "clave-de-prueba-1234"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
PHONE = "5215544445555"
OTRO = "5215500001111"
TARJETA = "Archivos del cliente"


@pytest.fixture(autouse=True)
def entorno(_isolated_db, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    for k, v in (("mssql_host", ""), ("anthropic_api_key", ""), ("geoip_enabled", False),
                 ("ollama_url", ""), ("rag_enabled", False)):
        monkeypatch.setattr(settings, k, v)
    with Session(db.engine) as s:
        s.add(User(username="admin", password_hash=HASH, is_admin=True))
        s.add(User(username="colab", password_hash=HASH, is_admin=False))
        s.commit()
    yield
    catalog._refresh()


def _sesion_de(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE}, follow_redirects=False)
    assert r.status_code == 303, "el login de la prueba falló"
    return cliente


def _folio(phone=PHONE, asignado="colab") -> int:
    with Session(db.engine) as s:
        # Como en producción: el cliente existe desde su primer mensaje. El visor de archivos
        # (`/crm/api/adjunto`) decide sobre él, y sin esa fila le negaría el archivo a todos.
        if s.get(Client, phone) is None:
            s.add(Client(phone=phone, name="Cliente", office_id=1))
        t = Ticket(phone=phone, category="(análisis)", service_id="analisis", service_name="Caso para análisis",
                   price_info="N/A", escalado=True, status=TicketStatus.NUEVO, assigned_to=asignado)
        s.add(t)
        s.commit()
        return t.id


def _archivo(ticket_id, phone=PHONE, nombre="constancia.pdf", mime="application/pdf", vence=None,
             bytes_=245_000, **extra) -> int:
    with Session(db.engine) as s:
        m = Message(phone=phone, direction=Direction.IN, body="[archivo adjunto]", ticket_id=ticket_id,
                    media_ref=f"ref-{nombre}", media_mime=mime, media_nombre=nombre, media_bytes=bytes_,
                    media_vence_at=vence, **extra)
        s.add(m)
        s.commit()
        return m.id


def _ficha(folio: int, usuario="admin") -> str:
    r = _sesion_de(usuario).get(f"/tickets/{folio}")
    assert r.status_code == 200, r.status_code
    return r.text


def test_la_ficha_lista_los_archivos_ligados_al_folio():
    """Lo que el operador pidió: el asesor ve en la ficha los archivos del folio, con nombre, tipo,
    tamaño y un enlace para abrirlos. Si se pone roja, hay que buscarlos burbuja por burbuja en el
    chat."""
    folio = _folio()
    pdf = _archivo(folio, nombre="constancia.pdf")
    foto = _archivo(folio, nombre="ine.jpg", mime="image/jpeg", bytes_=1_500_000)

    html = _ficha(folio)

    assert TARJETA in html
    assert "constancia.pdf" in html and "ine.jpg" in html
    assert f'href="/crm/api/adjunto/{pdf}"' in html and f'href="/crm/api/adjunto/{foto}"' in html
    # Se abre en otra pestaña (no saca al asesor de la ficha) sin darle acceso a ella a la página abierta.
    assert 'target="_blank" rel="noopener"' in html
    assert "PDF" in html and "Foto" in html and "1.4 MB" in html and "239 KB" in html


def test_un_archivo_de_otro_folio_o_sin_folio_no_aparece_en_esta_ficha():
    """Cada ficha muestra SUS archivos. Si se pone roja, el asesor del folio #1 ve la INE del #2."""
    este, otro = _folio(), _folio()
    _archivo(este, nombre="de-este.pdf")
    _archivo(otro, nombre="del-otro.pdf")
    _archivo(None, nombre="sin-folio.pdf")

    html = _ficha(este)

    assert "de-este.pdf" in html
    assert "del-otro.pdf" not in html and "sin-folio.pdf" not in html


def test_un_mensaje_de_otro_telefono_con_el_id_de_este_folio_no_aparece():
    """Defensa en profundidad: aunque una fila de otro cliente trajera el ticket_id de este folio
    (un dato dañado o una escritura equivocada), la ficha no la muestra. Si se pone roja, el
    archivo de un cliente se ve en la solicitud de otro."""
    folio = _folio()
    _archivo(folio, nombre="mio.pdf")
    _archivo(folio, phone=OTRO, nombre="ajeno.pdf")

    html = _ficha(folio)

    assert "mio.pdf" in html and "ajeno.pdf" not in html


def test_un_archivo_ocultado_desde_el_panel_no_aparece_en_la_ficha():
    """El asesor puede ocultar un mensaje del chat (borrado suave). El visor ya lo trata como
    inexistente; la ficha tampoco lo lista. Si se pone roja, un archivo que el equipo retiró sigue a
    la vista en la solicitud."""
    folio = _folio()
    _archivo(folio, nombre="visible.pdf")
    _archivo(folio, nombre="retirado.pdf", habilitado=False)

    html = _ficha(folio)

    assert "visible.pdf" in html and "retirado.pdf" not in html


def test_un_mensaje_de_texto_ligado_no_es_un_archivo():
    """Se liga también el texto que el cliente suma al folio; la tarjeta es de ARCHIVOS."""
    folio = _folio()
    with Session(db.engine) as s:
        s.add(Message(phone=PHONE, direction=Direction.IN, body="aquí va mi CURP", ticket_id=folio))
        s.commit()

    assert TARJETA not in _ficha(folio)


def test_sin_archivos_no_hay_tarjeta():
    assert TARJETA not in _ficha(_folio())


def test_un_archivo_vencido_se_lista_sin_enlace():
    """Meta conserva el archivo 7–30 días. Uno vencido ya no se puede traer: se dice, no se ofrece
    un enlace que va a fallar. Si se pone roja, el asesor da clic y recibe un error."""
    folio = _folio()
    viejo = _archivo(folio, nombre="viejo.pdf", vence=utcnow() - timedelta(days=1))
    vigente = _archivo(folio, nombre="vigente.pdf", vence=utcnow() + timedelta(days=20))

    html = _ficha(folio)

    assert "viejo.pdf" in html and "Vencido" in html
    assert f"/crm/api/adjunto/{viejo}" not in html
    assert f'href="/crm/api/adjunto/{vigente}"' in html


def test_el_nombre_del_archivo_del_cliente_se_escapa():
    """El nombre lo escribe el cliente. Si se pone roja, un archivo llamado `<script>…` corre en la
    sesión del asesor."""
    folio = _folio()
    _archivo(folio, nombre="<script>alert(1)</script>.pdf")

    html = _ficha(folio)

    assert "<script>alert(1)" not in html
    assert "&lt;script&gt;alert(1)" in html


def test_quien_no_puede_abrir_la_conversacion_ve_la_lista_sin_enlaces():
    """El visor niega el archivo a un colaborador que no atiende ese cliente (404). La ficha no le
    ofrece enlaces muertos: lista los archivos y le dice qué falta. Si se pone roja, el colaborador
    da clic y recibe «No existe»."""
    folio = _folio(asignado="otro-colaborador")
    archivo = _archivo(folio, nombre="constancia.pdf")

    html = _ficha(folio, usuario="colab")

    assert "constancia.pdf" in html
    assert f"/crm/api/adjunto/{archivo}" not in html
    assert "Para abrirlos hay que atender esta solicitud" in html


def test_el_colaborador_que_atiende_el_folio_si_recibe_los_enlaces():
    folio = _folio(asignado="colab")
    archivo = _archivo(folio)

    html = _ficha(folio, usuario="colab")

    assert f'href="/crm/api/adjunto/{archivo}"' in html
    assert "Para abrirlos hay que atender esta solicitud" not in html


def test_con_mas_de_cincuenta_archivos_muestra_los_mas_recientes_y_avisa():
    """Sin tope de casos ni de archivos por cliente, la ficha no puede crecer sin límite. Si se pone
    roja, un folio con cientos de archivos hace una página enorme."""
    folio = _folio()
    for i in range(55):
        _archivo(folio, nombre=f"doc-{i:02d}.pdf")

    html = _ficha(folio)

    assert "doc-54.pdf" in html and "doc-05.pdf" in html
    assert "doc-04.pdf" not in html and "doc-00.pdf" not in html
    assert "50 más recientes de 55" in html
