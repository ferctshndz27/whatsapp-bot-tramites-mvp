"""Panel /oficinas — docs/SPEC-MULTI-OFICINA.md §5 (PR 3).

LA PRUEBA QUE CIERRA EL PR, según §8: dar de alta una oficina, poner y quitar
una llave, y **verificar que el valor nunca aparece en el HTML**. Aquí se
extiende a los otros dos lugares por donde un secreto puede escaparse sin que
nadie lo note: el `target` de AdminAudit —que SÍ se espeja a MSSQL, así que un
valor ahí viaja en claro a otra base y anula el cifrado de §3.1— y el
re-render de un formulario con error, que es donde un `value=` de conveniencia
devuelve al HTML justo lo que se acaba de escribir.
"""

import bcrypt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app import credenciales
from app.config import settings
from app.main import app
from app.models import AdminAudit, Office, OfficeCredential, User

CLAVE = "clave-de-prueba"
HASH_CLAVE = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()
SECRETO = "sk_test_ELVALORQUENUNCADEBESALIR9999"


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    """StaticPool: el hilo del TestClient tiene que ver la misma base (patrón de
    tests/test_navegacion.py:35-45)."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())
    # El .env de la prueba: lo que se hereda cuando no hay fila.
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_test_DEL_ENV_1111")
    with Session(engine) as s:
        s.add(User(username="jefa", password_hash=HASH_CLAVE, is_admin=True))
        s.add(User(username="colab", password_hash=HASH_CLAVE, is_admin=False))
        s.commit()
    yield


@pytest.fixture()
def admin():
    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.post("/login", data={"username": "jefa", "password": CLAVE},
                         follow_redirects=False)
        assert r.status_code == 303, "el login de la prueba dejó de funcionar"
        yield cliente


# Los badges COMPLETOS, no la palabra suelta: el botón de la misma fila dice
# «Poner propia», así que buscar "propia" a secas daba ese estado siempre — un
# helper que confirmaba lo que quisiera creer.
BADGES = {
    '<span class="badge asignada">propia</span>': "propia",
    '<span class="badge">heredada del global</span>': "heredada del global",
    '<span class="badge caducada">ilegible</span>': "ilegible",
    '<span class="badge">sin configurar</span>': "sin configurar",
}


def _estado_de(html: str, clave: str) -> str:
    """El estado que la fila de `clave` pinta, leído del HTML como lo ve el admin."""
    fila = html.split(f"<code>{clave}</code>", 1)[1].split("</tr>", 1)[0]
    hallados = [nombre for badge, nombre in BADGES.items() if badge in fila]
    assert len(hallados) == 1, f"la fila de {clave} pinta {hallados or 'ningún estado'}"
    return hallados[0]


# --- Puerta ----------------------------------------------------------------

def test_un_colaborador_no_entra():
    with TestClient(app, base_url="https://testserver") as cliente:
        cliente.post("/login", data={"username": "colab", "password": CLAVE},
                     follow_redirects=False)
        r = cliente.get("/oficinas", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_sin_sesion_va_al_login():
    with TestClient(app, base_url="https://testserver") as cliente:
        r = cliente.get("/oficinas", follow_redirects=False)
    assert r.headers["location"] == "/login"


# --- Alta ------------------------------------------------------------------

def test_alta_de_oficina(admin):
    r = admin.post("/oficinas", data={"nombre": "Puebla", "slug": "puebla",
                                      "ciudad": "Puebla", "zona_horaria": "America/Mexico_City"})
    assert r.status_code == 200 and "Puebla" in r.text
    with Session(db.engine) as s:
        assert s.exec(select(Office).where(Office.slug == "puebla")).first() is not None


@pytest.mark.parametrize("slug", ["con espacio", "acentué", "con/barra", "cón-guion"])
def test_un_slug_que_romperia_la_url_del_webhook_se_rechaza(admin, slug):
    # El slug es /webhooks/stripe/{slug} en el PR 5: lo que se acepte hoy es el
    # endpoint de mañana.
    r = admin.post("/oficinas", data={"nombre": "X", "slug": slug, "ciudad": "X"})
    assert "slug" in r.text.lower()
    with Session(db.engine) as s:
        assert s.exec(select(Office).where(Office.nombre == "X")).first() is None


def test_un_slug_en_mayusculas_se_normaliza(admin):
    # Normalizar es amable con quien teclea «Puebla»; lo que no se puede es
    # GUARDAR la mayúscula, porque la URL del webhook sí distingue.
    admin.post("/oficinas", data={"nombre": "Puebla", "slug": "PUEBLA", "ciudad": "Puebla"})
    with Session(db.engine) as s:
        assert s.exec(select(Office).where(Office.nombre == "Puebla")).one().slug == "puebla"


def test_una_zona_horaria_inventada_se_rechaza(admin):
    r = admin.post("/oficinas", data={"nombre": "Y", "slug": "y", "ciudad": "Y",
                                      "zona_horaria": "America/Nolandia"})
    assert "Zona horaria desconocida" in r.text
    with Session(db.engine) as s:
        assert s.exec(select(Office).where(Office.slug == "y")).first() is None


def test_un_nombre_repetido_no_es_un_500(admin):
    # Office.nombre y Office.slug son UNIQUE: sin el except IntegrityError esto
    # sería una traza en la cara del operador.
    admin.post("/oficinas", data={"nombre": "Puebla", "slug": "puebla", "ciudad": "Puebla"})
    r = admin.post("/oficinas", data={"nombre": "Puebla", "slug": "otro", "ciudad": "Puebla"})
    assert r.status_code == 200 and "Ya existe" in r.text


# --- El ciclo de vida de una llave -----------------------------------------

def test_heredada_propia_baja_heredada(admin):
    """Las tres transiciones que el operador hace de verdad, y el estado en cada paso."""
    detalle = "/oficinas/1"

    assert _estado_de(admin.get(detalle).text, "STRIPE_SECRET_KEY") == "heredada del global"

    r = admin.post(f"{detalle}/llaves", data={"clave": "STRIPE_SECRET_KEY", "valor": SECRETO})
    assert _estado_de(r.text, "STRIPE_SECRET_KEY") == "propia"
    with Session(db.engine) as s:
        fila = s.exec(select(OfficeCredential).where(
            OfficeCredential.habilitado == True)).one()  # noqa: E712
        assert fila.provider == "stripe", "el provider tiene que salir del inventario"
        assert fila.valor_cif != SECRETO.encode(), "guardado sin cifrar"
        assert fila.pista == SECRETO[-4:]

    r = admin.post(f"{detalle}/llaves/STRIPE_SECRET_KEY/baja")
    assert _estado_de(r.text, "STRIPE_SECRET_KEY") == "heredada del global"
    with Session(db.engine) as s:
        # Soft delete: la fila queda para el rastro de rotación.
        assert s.exec(select(OfficeCredential)).one().habilitado is False


def test_rotar_deja_una_sola_viva_y_conserva_la_anterior(admin):
    for valor in ("sk_test_PRIMERA_0001", "sk_test_SEGUNDA_0002"):
        admin.post("/oficinas/1/llaves", data={"clave": "STRIPE_SECRET_KEY", "valor": valor})
    with Session(db.engine) as s:
        filas = s.exec(select(OfficeCredential).order_by(OfficeCredential.id)).all()
    assert [f.habilitado for f in filas] == [False, True], "el índice parcial exige una sola viva"
    assert filas[-1].pista == "0002"


def test_la_llave_es_de_esa_oficina_y_no_de_las_demas(admin):
    admin.post("/oficinas", data={"nombre": "Puebla", "slug": "puebla", "ciudad": "Puebla"})
    admin.post("/oficinas/2/llaves", data={"clave": "STRIPE_SECRET_KEY", "valor": SECRETO})
    assert _estado_de(admin.get("/oficinas/2").text, "STRIPE_SECRET_KEY") == "propia"
    assert _estado_de(admin.get("/oficinas/1").text, "STRIPE_SECRET_KEY") == "heredada del global"


def test_una_llave_sin_valor_ni_en_el_env_sale_sin_configurar(admin, monkeypatch):
    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    assert _estado_de(admin.get("/oficinas/1").text, "STRIPE_WEBHOOK_SECRET") == "sin configurar"


# --- El cuarto estado ------------------------------------------------------

def test_una_credentials_key_cambiada_pinta_ilegible_y_no_tumba_la_pagina(admin, monkeypatch):
    """§5: el cuarto estado no puede colapsarse en «ausente» — son dos acciones
    distintas del operador (pegar la llave vs. restaurar CREDENTIALS_KEY). Y la
    página que se abre PARA diagnosticar eso no puede ser la que contesta 500.
    """
    admin.post("/oficinas/1/llaves", data={"clave": "STRIPE_SECRET_KEY", "valor": SECRETO})
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())

    r = admin.get("/oficinas/1")
    assert r.status_code == 200
    assert _estado_de(r.text, "STRIPE_SECRET_KEY") == "ilegible"
    assert admin.get("/oficinas").status_code == 200, "la lista también resuelve estados"


def test_sin_credentials_key_no_se_guarda_en_claro(admin, monkeypatch):
    monkeypatch.setattr(settings, "credentials_key", "")
    r = admin.post("/oficinas/1/llaves", data={"clave": "STRIPE_SECRET_KEY", "valor": SECRETO})
    assert r.status_code == 200 and "CREDENTIALS_KEY" in r.text
    with Session(db.engine) as s:
        assert s.exec(select(OfficeCredential)).all() == [], "guardó sin poder cifrar"


# --- Lo que nunca puede salir ----------------------------------------------

def test_el_valor_no_aparece_en_ninguna_pagina_ni_en_la_auditoria(admin):
    admin.post("/oficinas/1/llaves", data={"clave": "STRIPE_SECRET_KEY", "valor": SECRETO})

    for ruta in ("/oficinas", "/oficinas/1", "/system"):
        assert SECRETO not in admin.get(ruta).text, f"el secreto se filtró en {ruta}"

    with Session(db.engine) as s:
        filas = s.exec(select(AdminAudit).where(AdminAudit.action == "credencial:guardada")).all()
        assert filas, "la acción no quedó auditada"
        for fila in filas:
            # adminaudit se espeja a MSSQL: un valor aquí viaja en claro a otra base.
            assert SECRETO not in (fila.target or "")
            assert SECRETO[-4:] not in (fila.target or ""), "ni siquiera la pista"


def test_el_error_no_devuelve_el_valor_escrito_al_formulario(admin, monkeypatch):
    """El re-render con error es donde un `value=` de conveniencia reimprime en
    el HTML justo lo que se acaba de teclear. users.py no lo hace con la
    contraseña; aquí tampoco."""
    monkeypatch.setattr(settings, "credentials_key", "")
    r = admin.post("/oficinas/1/llaves", data={"clave": "STRIPE_SECRET_KEY", "valor": SECRETO})
    assert SECRETO not in r.text


def test_una_clave_fuera_del_inventario_se_rechaza(admin):
    r = admin.post("/oficinas/1/llaves", data={"clave": "INVENTADA", "valor": "x"})
    assert r.status_code == 200 and "no existe" in r.text
    with Session(db.engine) as s:
        assert s.exec(select(OfficeCredential)).all() == []


# --- Colaboradores ---------------------------------------------------------

def test_mover_un_colaborador_de_oficina(admin):
    admin.post("/oficinas", data={"nombre": "Puebla", "slug": "puebla", "ciudad": "Puebla"})
    r = admin.post("/oficinas/2/usuarios", data={"username": "colab"})
    assert r.status_code == 200
    with Session(db.engine) as s:
        assert s.exec(select(User).where(User.username == "colab")).one().office_id == 2


def test_system_no_revienta_con_la_llave_cambiada_y_nombra_la_credencial(admin, monkeypatch):
    """Cierra el `ponytail` de system.py: esa página contestaba 500 con una fila
    viva y una CREDENTIALS_KEY cambiada — justo el escenario para el que el
    operador la abre. Ahora dice cuál llave y qué revisar.
    """
    # Por la función y no por el panel: WHATSAPP_TOKEN es compartida y el panel
    # ya no la deja crear (§5). Así es como llegará la fila real en el PR 4.
    credenciales.guardar("WHATSAPP_TOKEN", office_id=1, valor="EAAG_token_1234", updated_by="jefa")
    monkeypatch.setattr(settings, "credentials_key", Fernet.generate_key().decode())

    r = admin.get("/system")
    assert r.status_code == 200
    assert "WHATSAPP_TOKEN" in r.text and "CREDENTIALS_KEY" in r.text
    assert "sin credenciales" in r.text, "ilegible no es «configurada»: el bot no puede enviar"


def test_una_llave_compartida_no_se_vuelve_propia_por_un_post_directo(admin):
    """El template ya no pinta su formulario, pero ocultar un botón no es una
    regla: sin la comprobación en el endpoint, un POST a mano crea la fila igual
    y esa oficina se queda con una llave que las demás creen compartir (§7, paso 4).
    """
    r = admin.post("/oficinas/1/llaves", data={"clave": "WHATSAPP_TOKEN", "valor": "EAAG_9999"})
    assert r.status_code == 200 and "compartida" in r.text
    with Session(db.engine) as s:
        assert s.exec(select(OfficeCredential)).all() == []
