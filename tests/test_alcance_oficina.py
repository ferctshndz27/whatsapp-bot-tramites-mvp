"""Alcance por oficina en el panel — docs/SPEC-MULTI-OFICINA.md §5 y §8 (PR 5).

LA PRUEBA QUE CIERRA EL PR según §8: «cobro de prueba en la oficina 2 que no
aparece en el panel de la oficina 1».

Y el CANDADO DE COBERTURA al final, que es la otra mitad: con una sola oficina
operando, un filtro que falta no se nota en ninguna pantalla. La lista de
módulos todavía sin acotar se asevera aquí para que agregar el número 14 —o
acotar uno— rompa la prueba en vez de quedar en la memoria de nadie.
"""

import ast
from pathlib import Path

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, create_engine, select

import app.db as db
from app.auth import alcance
from app.config import settings
from app.main import app
from app.models import ChatMember, ChatRoom, Cobro, Office, Ticket, TicketStatus, User

CLAVE = "clave-de-prueba"
HASH = bcrypt.hashpw(CLAVE.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    monkeypatch.setattr(db, "engine", engine)
    db.init_db()
    monkeypatch.setattr(settings, "mssql_host", "")
    monkeypatch.setattr(settings, "geoip_enabled", False)
    with Session(engine) as s:
        s.add(Office(id=2, nombre="Puebla", slug="puebla", ciudad="Puebla"))
        # Dos admins, uno por oficina: §5 decidió que CUALQUIER admin administra
        # todo, así que ambos ven las dos carteras. La distinción la hace el
        # colaborador.
        s.add(User(username="jefa1", password_hash=HASH, is_admin=True, office_id=1))
        s.add(User(username="colab1", password_hash=HASH, is_admin=False, office_id=1))
        s.add(User(username="colab2", password_hash=HASH, is_admin=False, office_id=2))
        s.add(Cobro(phone="525500000001", concepto="acta de Tlaxcala", monto_centavos=15000,
                    moneda="mxn", estado="pendiente", office_id=1,
                    token_verificacion="tok-tlx"))
        s.add(Cobro(phone="525500000002", concepto="acta de Puebla", monto_centavos=25000,
                    moneda="mxn", estado="pendiente", office_id=2,
                    token_verificacion="tok-pue"))
        for oficina, quien in ((1, "folio de Tlaxcala"), (2, "folio de Puebla")):
            s.add(Ticket(phone=f"52550000000{oficina}", client_name=quien,
                         category="Actas", service_id="acta", service_name=quien,
                         price_info="N/A", status=TicketStatus.NUEVO, office_id=oficina))
        s.commit()
    yield


def _sesion(username: str) -> TestClient:
    cliente = TestClient(app, base_url="https://testserver")
    r = cliente.post("/login", data={"username": username, "password": CLAVE},
                     follow_redirects=False)
    assert r.status_code == 303, f"el login de {username} falló"
    return cliente


# --- El helper -------------------------------------------------------------

def test_el_alcance_sale_de_la_fila_del_usuario():
    assert alcance("colab1") == (1, False)
    assert alcance("colab2") == (2, False)
    assert alcance("jefa1").ve_todas is True


@pytest.mark.parametrize("quien", [None, "", "fantasma"])
def test_una_sesion_huerfana_cae_al_alcance_MAS_ESTRECHO(quien):
    """Sin usuario, o con uno borrado, lo seguro es la oficina 1 sin ver todas:
    un `ve_todas=True` por omisión convertiría una sesión rota en un pase libre."""
    assert alcance(quien) == (1, False)


# --- La prueba de §8 -------------------------------------------------------

def test_un_folio_de_la_oficina_2_no_aparece_para_un_colaborador_de_la_1():
    """LA PRUEBA QUE CIERRA EL PR 5.

    §8 la pedía sobre `/cobros`, pero esa sección es admin-only y §5 decidió que
    CUALQUIER admin ve todo: ahí el alcance no se puede observar ni siquiera en
    principio. La vista donde «un colaborador ve la suya» sí significa algo es
    «/», la lista de folios donde vive todo el día.
    """
    de_uno = _sesion("colab1").get("/").text
    assert "folio de Tlaxcala" in de_uno, "no ve ni sus propios folios"
    assert "folio de Puebla" not in de_uno, "ve los folios de otra oficina"

    de_dos = _sesion("colab2").get("/").text
    assert "folio de Puebla" in de_dos
    assert "folio de Tlaxcala" not in de_dos


def test_un_admin_ve_los_folios_de_todas():
    """Consecuencia asumida de §5 al descartar el rol de admin-de-oficina."""
    texto = _sesion("jefa1").get("/").text
    assert "folio de Tlaxcala" in texto and "folio de Puebla" in texto


def test_un_colaborador_no_abre_el_folio_de_otra_oficina_por_la_url():
    """Referencia directa: el id viene de la URL, así que acotar el LISTADO no
    cierra nada. Sin la comprobación en el detalle, teclear el id abre el folio
    entero —teléfono del cliente, historial, notas."""
    ajeno = _sesion("colab1").get("/tickets/2", follow_redirects=False)
    assert ajeno.status_code == 303 and ajeno.headers["location"] == "/"
    propio = _sesion("colab1").get("/tickets/1", follow_redirects=False)
    assert propio.status_code == 200, "el filtro se llevó también su propio folio"


def test_el_alcance_de_cobros_existe_pero_hoy_es_inerte():
    """El filtro de cobranza_panel está puesto y es correcto, pero HOY no se
    puede observar: /cobros es admin-only y todo admin ve todo. Queda escrito
    para que nadie lo lea como comportamiento probado.
    """
    assert _sesion("colab1").get("/cobros", follow_redirects=False).status_code == 303
    csv = _sesion("jefa1").get("/export/cobros.csv").text
    assert "acta de Tlaxcala" in csv and "acta de Puebla" in csv, (
        "un admin exporta todo: si esto cambia, §5 cambió y hay que revisarlo"
    )


# --- El candado de cobertura ----------------------------------------------

# Módulos del panel que TODAVÍA no acotan por oficina. Con una sola oficina
# operando ninguno de estos filtros se nota, así que la deuda se asevera en vez
# de recordarse. Acotar uno = quitarlo de aquí, en el mismo commit.
#
# `users.py` está en la lista pero es un CASO APARTE y deliberado: acotarlo
# contradiría a §5 —cualquier admin administra todo— y /oficinas/{id} ya lista
# los colaboradores por oficina. No se acota; se deja anotado para que nadie lo
# "arregle" sin leer esto.
SIN_ACOTAR = {
    "activity.py", "authlog.py", "catalog_admin.py", "clients.py",
    "kb.py", "replies.py", "synonym_miner.py", "system.py",
    "users.py",  # a propósito: ver arriba
    # /oficinas ES la vista de todas las oficinas: acotarla sería impedirle al
    # admin administrar la que no es la suya, que es su función entera.
    "offices.py",
}
# Los que NO llevan filtro nunca: el bot atiende a quien escribe, el cron barre
# todo y el espejo copia todo. Un filtro global los rompería en silencio, que es
# la razón por la que esto se aplica consulta por consulta.
SIN_SESION = {
    "archive.py", "asignacion.py", "agent_settings.py", "auth.py", "catalog.py",
    "cobranza.py", "cobranza_alertas.py", "cobranza_jobs.py", "conversation.py",
    "credenciales.py", "rag.py", "ruteo.py", "webhook.py", "stripe_webhook.py",
}


def _modulos_del_panel() -> set[str]:
    """Los que SIRVEN vistas leyendo la sesión: ahí y solo ahí aplica el alcance.

    Se exige que además declaren rutas (`@router.…`): `main.py` toca la sesión
    desde un middleware, no desde una vista, y listarlo como deuda pendiente
    habría sido una entrada que nadie podría saldar nunca.
    """
    raiz = Path(__file__).resolve().parent.parent / "app"
    encontrados = set()
    for ruta in sorted(raiz.glob("*.py")):
        src = ruta.read_text("utf-8")
        lee_sesion = any(m in src for m in ("request.session", "_admin_or_redirect",
                                            "_current_user"))
        sirve_vistas = any(
            isinstance(n, ast.FunctionDef)
            and any("router" in ast.unparse(d) for d in n.decorator_list)
            for n in ast.walk(ast.parse(src))
        )
        if lee_sesion and sirve_vistas:
            encontrados.add(ruta.name)
    return encontrados


def test_la_deuda_de_acotar_por_oficina_esta_enumerada():
    panel = _modulos_del_panel()
    acotados = {
        ruta.name
        for ruta in (Path(__file__).resolve().parent.parent / "app").glob("*.py")
        if "alcance(" in ruta.read_text("utf-8") and ruta.name != "auth.py"
    }
    faltan = panel - acotados - SIN_ACOTAR
    assert faltan == set(), (
        f"módulos del panel nuevos sin decidir su alcance: {sorted(faltan)}. "
        f"Acótalos con auth.alcance() o agrégalos a SIN_ACOTAR con el motivo."
    )
    sobran = SIN_ACOTAR & acotados
    assert sobran == set(), (
        f"{sorted(sobran)} ya acota(n) por oficina: quítalo(s) de SIN_ACOTAR"
    )


def test_ni_el_bot_ni_el_cron_ni_el_espejo_acotan_por_oficina():
    """Un filtro por sesión en estos módulos sería un apagón silencioso: el bot
    dejaría de atender, el cron de barrer y el espejo de copiar."""
    raiz = Path(__file__).resolve().parent.parent / "app"
    con_alcance = [
        nombre for nombre in SIN_SESION
        # auth.py queda fuera: es donde alcance() se DEFINE.
        if (raiz / nombre).exists() and nombre != "auth.py"
        and "alcance(" in (raiz / nombre).read_text("utf-8")
    ]
    assert con_alcance == [], f"{con_alcance} no tiene sesión de usuario que consultar"


# --- §6: el corpus del RAG por oficina -------------------------------------

def test_el_rag_solo_recupera_el_corpus_de_su_oficina():
    """§6 con sus propias palabras: «requisitos y precios varían por estado;
    mezclarlos es exactamente la respuesta equivocada a un cliente real».

    Es el único de los cuatro puntos de §6 que puede darle una respuesta
    equivocada a alguien HOY, y por eso es el que se cerró.
    """
    from app import rag
    from app.models import InfoDoc

    with Session(db.engine) as s:
        s.add(InfoDoc(texto="En Tlaxcala el acta cuesta 150 pesos.", position=1,
                      updated_by="qa", office_id=1))
        s.add(InfoDoc(texto="En Puebla el acta cuesta 320 pesos.", position=1,
                      updated_by="qa", office_id=2))
        s.commit()

    de_tlaxcala = rag._docs(1)
    de_puebla = rag._docs(2)
    assert any("150 pesos" in d for d in de_tlaxcala)
    assert not any("320 pesos" in d for d in de_tlaxcala), (
        "el corpus de Puebla viajaría a un cliente de Tlaxcala"
    )
    assert any("320 pesos" in d for d in de_puebla)
    assert not any("150 pesos" in d for d in de_puebla)


def test_el_corpus_que_escribe_el_estudio_nace_en_la_oficina_del_admin():
    """Sin esto el corpus de una oficina nueva nacería en la 1 por el
    server_default, y el filtro de arriba no lo encontraría nunca: el admin
    escribiría documentos que su propio bot no puede leer."""
    from app.models import InfoDoc

    with Session(db.engine) as s:
        s.add(User(username="jefapue", password_hash=HASH, is_admin=True, office_id=2))
        s.commit()

    cliente = _sesion("jefapue")
    r = cliente.post("/agente/corpus", json={"texto": "Puebla abre los sábados."})
    assert r.status_code in (200, 303), f"el alta del corpus contestó {r.status_code}"

    with Session(db.engine) as s:
        fila = s.exec(select(InfoDoc).where(InfoDoc.texto.contains("sábados"))).first()
    assert fila is not None, "no guardó el documento"
    assert fila.office_id == 2, f"el documento nació en la oficina {fila.office_id}"


# --- §6: lo que queda diferido, aseverado ---------------------------------

# Los tres puntos de §6 que NO se cerraron, con lo que costaría cada uno medido
# el 2026-09-03. Vive como prueba y no como nota porque ninguno es observable
# hasta que una segunda oficina opere: sin este candado, cerrar uno o añadir un
# cuarto no dejaría rastro en ninguna parte.
#
# El RAG (`InfoDoc`) SÍ se cerró: era el único que puede darle una respuesta
# equivocada a un cliente hoy, con las palabras de la propia §6 — «requisitos y
# precios varían por estado; mezclarlos es exactamente la respuesta equivocada
# a un cliente real».
SEIS_DIFERIDO = {
    # DECISIÓN VIGENTE, no deuda: hoy los trámites y sus requisitos son los
    # MISMOS en todas las oficinas, así que un catálogo compartido es la
    # respuesta correcta y no una simplificación. Se documenta aquí para que
    # nadie lo "arregle" sin el disparador.
    #
    # DISPARADOR para separarlo: el día que una oficina necesite un precio o un
    # requisito distinto. Entonces son 61 puntos de uso de la API del catálogo
    # más el botón «Copiar catálogo de…» — un PR entero — y detrás va el de los
    # sinónimos, que hoy está bloqueado por esto mismo.
    "catalog.py": "catálogo compartido (decisión vigente; separar = 61 puntos + panel)",
    # BLOQUEADO POR EL CATÁLOGO, no por tamaño: son 3 consultas, pero
    # `catalog.add_synonym` escribe `CatalogService.synonyms_json`, que es
    # GLOBAL. Filtrar solo la lista de propuestas construiría la ilusión que §6
    # quiere evitar, y peor: hoy todo admin ve todas las propuestas y sabe que
    # el efecto es global; con la lista acotada, el admin de Puebla aprobaría
    # algo que se ve suyo y cambiaría el NLU de Tlaxcala en silencio — un
    # trámite mal cotizado, que es la falla que la regla de oro existe para
    # impedir. El estado actual es más seguro que la media solución.
    "synonym_miner.py": "sinónimos por oficina — bloqueado por el catálogo global",
}


def test_lo_diferido_de_la_seccion_6_sigue_diferido_o_se_actualiza_la_lista():
    """Si alguien acota uno de estos, que lo saque de la lista en el mismo
    commit — y si nadie lo hace, que la lista siga diciendo qué falta."""
    raiz = Path(__file__).resolve().parent.parent / "app"
    ya_acotados = [
        nombre for nombre in SEIS_DIFERIDO
        if "office_id" in (raiz / nombre).read_text("utf-8")
    ]
    assert ya_acotados == [], (
        f"{ya_acotados} ya filtra(n) por oficina: quítalo(s) de SEIS_DIFERIDO y de "
        f"§11.3 de la spec, en este mismo commit"
    )


def test_el_corpus_del_rag_si_quedo_acotado():
    """El control positivo del candado de arriba: sin esto, un SEIS_DIFERIDO que
    creciera hasta incluirlo todo pasaría en verde sin que nadie lo note."""
    raiz = Path(__file__).resolve().parent.parent / "app"
    assert "InfoDoc.office_id" in (raiz / "rag.py").read_text("utf-8"), (
        "rag._docs() dejó de acotar el corpus: es el único punto de §6 que puede "
        "darle una respuesta equivocada a un cliente real"
    )


# --- §6: la medición del NLU, desglosable por oficina ----------------------

def test_el_benchmark_del_nlu_se_puede_desglosar_por_oficina():
    """§6: «el benchmark del NLU deja de ser un promedio de ciudades distintas».

    Lo que pide es poder DESGLOSAR, no restringir: quien mira estas métricas es
    siempre un admin, y un admin ve todas las oficinas (§5). Por eso el default
    de `office_id` es None = todas, y el desglose es explícito.
    """
    from app import alerts
    from app.agente_studio import _metricas_por_capa
    from app.models import NluEvent

    with Session(db.engine) as s:
        # Tlaxcala: 1 de 2 sin entender. Puebla: 0 de 2.
        for capa, oficina in (("ninguna", 1), ("literal", 1), ("literal", 2), ("literal", 2)):
            s.add(NluEvent(phone="525500000009", text="x", capa=capa, office_id=oficina))
        s.commit()

        assert alerts._no_entendidos_stats(s, office_id=1) == (2, 50.0)
        assert alerts._no_entendidos_stats(s, office_id=2) == (2, 0.0)
        # Sin desglosar: el promedio de las dos ciudades, que es justo lo que §6
        # señala como engañoso — 25% no describe a ninguna de las dos.
        assert alerts._no_entendidos_stats(s) == (4, 25.0)

        assert _metricas_por_capa(s, office_id=1).get("ninguna") == 1
        assert _metricas_por_capa(s, office_id=2).get("ninguna") is None
        assert _metricas_por_capa(s).get("literal") == 3


def test_la_alerta_de_no_entendidos_sigue_siendo_una_sola():
    """Partirla por oficina exige decidir a quién se le manda cada una, y ese
    ruteo no existe. Un porcentaje combinado que dispara de más es un aviso de
    sobra; uno por oficina que nadie recibe es un aviso perdido."""
    import inspect

    from app import alerts

    fuente = inspect.getsource(alerts.current_rules)
    assert "office_id" not in fuente, (
        "current_rules empezó a desglosar por oficina: hay que decidir a quién "
        "se le manda cada alerta antes de partirla"
    )


# --- /users: asignar oficina desde la ficha de la persona ------------------

def test_el_alta_de_usuario_nace_en_la_oficina_elegida():
    """El hueco de verdad no era cambiar la oficina, era CREAR sin ella: sin
    este campo, alguien de Puebla nacía en Tlaxcala por el server_default, veía
    los folios de Tlaxcala, y nadie se enteraba hasta que se quejaba."""
    _sesion("jefa1").post("/users", data={
        "username": "nueva_pue", "password": "contrasena-larga-123",
        "role": "colaborador", "office_id": 2})
    with Session(db.engine) as s:
        assert s.exec(select(User).where(User.username == "nueva_pue")).one().office_id == 2


def test_la_lista_de_usuarios_muestra_y_permite_cambiar_la_oficina():
    html = _sesion("jefa1").get("/users").text
    assert 'action="/users/colab1/oficina"' in html, "no se puede cambiar desde /users"
    assert "Oficina" in html, "la lista no muestra la oficina"


def test_mover_a_alguien_suelta_los_folios_que_deja_atras():
    """LA RAZÓN DE SER DE ESTE CAMBIO.

    Un folio vive en la oficina donde nació y su detalle rechaza a quien no es
    de ahí. Sin soltarlo, el folio sigue diciendo «asignado a colab1» mientras
    colab1 ya no puede abrirlo — el mismo trabajo congelado que describe
    dashboard.release_ticket, pero peor: allá la persona está deshabilitada y se
    nota; aquí sigue trabajando y nadie sospecha.
    """
    with Session(db.engine) as s:
        folio = s.exec(select(Ticket).where(Ticket.office_id == 1)).first()
        folio.assigned_to = "colab1"
        s.add(folio)
        s.commit()
        folio_id = folio.id

    r = _sesion("jefa1").post("/users/colab1/oficina", data={"office_id": 2})
    assert "soltaron 1 folio" in r.text, f"no avisó del folio suelto: {r.text[:200]}"

    with Session(db.engine) as s:
        assert s.get(Ticket, folio_id).assigned_to is None, (
            "el folio quedó asignado a alguien que ya no puede abrirlo"
        )
        assert s.exec(select(User).where(User.username == "colab1")).one().office_id == 2


def test_los_folios_de_la_oficina_destino_no_se_sueltan():
    """Allá sí los puede atender: soltarlos sería trabajo perdido sin motivo."""
    with Session(db.engine) as s:
        folio = s.exec(select(Ticket).where(Ticket.office_id == 2)).first()
        folio.assigned_to = "colab1"
        s.add(folio)
        s.commit()
        folio_id = folio.id

    _sesion("jefa1").post("/users/colab1/oficina", data={"office_id": 2})
    with Session(db.engine) as s:
        assert s.get(Ticket, folio_id).assigned_to == "colab1"


def test_no_se_asigna_a_una_oficina_dada_de_baja():
    """Su ruteo de entrada está cortado: la persona no vería nada y el síntoma
    («no me aparecen folios») no apunta a la causa."""
    with Session(db.engine) as s:
        oficina = s.get(Office, 2)
        oficina.habilitado = False
        s.add(oficina)
        s.commit()

    r = _sesion("jefa1").post("/users/colab1/oficina", data={"office_id": 2})
    assert "dada de baja" in r.text
    with Session(db.engine) as s:
        assert s.exec(select(User).where(User.username == "colab1")).one().office_id == 1


# --- /invitations: la oficina se decide al invitar -------------------------

def test_el_invitado_nace_en_la_oficina_de_su_invitacion():
    """La oficina se elige al INVITAR, no al registrarse: quien acepta el código
    no sabe —ni le toca decidir— de qué sede es. Sin esto, todo invitado nacía
    en la oficina 1 por el DEFAULT y había que moverlo después, arrastrando el
    rescate de folios de users.mover_de_oficina.
    """
    from app.models import Invitation

    jefa = _sesion("jefa1")
    jefa.post("/invitations", data={"office_id": 2})
    with Session(db.engine) as s:
        codigo = s.exec(select(Invitation).order_by(Invitation.id.desc())).first()
        assert codigo.office_id == 2, "el código no guardó la oficina elegida"
        token = codigo.token

    anon = TestClient(app, base_url="https://testserver")
    r = anon.post("/register", data={
        "username": "invitada", "password": "contrasena-larga-123",
        "phone": "5215544443333", "code": token}, follow_redirects=False)
    assert r.status_code == 303, f"el registro falló: {r.status_code}"

    with Session(db.engine) as s:
        assert s.exec(select(User).where(User.username == "invitada")).one().office_id == 2


def test_no_se_puede_invitar_a_una_oficina_dada_de_baja():
    """Entraría a una sede sin ruteo de entrada: no vería nada y el síntoma no
    apunta a la causa."""
    from app.models import Invitation

    with Session(db.engine) as s:
        oficina = s.get(Office, 2)
        oficina.habilitado = False
        s.add(oficina)
        s.commit()
        antes = len(s.exec(select(Invitation)).all())

    r = _sesion("jefa1").post("/invitations", data={"office_id": 2})
    assert "dada de baja" in r.text
    with Session(db.engine) as s:
        assert len(s.exec(select(Invitation)).all()) == antes, "generó el código igual"


def test_la_lista_de_invitaciones_muestra_su_oficina():
    jefa = _sesion("jefa1")
    jefa.post("/invitations", data={"office_id": 2})
    html = jefa.get("/invitations").text
    assert "Puebla" in html, "la lista no dice a qué oficina invita cada código"


# --- /team: el chat interno es por oficina (§2.3) --------------------------

def _salas_de(quien: str) -> set[int]:
    with Session(db.engine) as s:
        return set(s.exec(select(ChatMember.room_id).where(ChatMember.username == quien)).all())


def test_el_chat_de_equipo_solo_ofrece_gente_de_tu_oficina():
    """§2.3: «el chat interno es por oficina». La lista para abrir un DM o un
    grupo es por donde se cruza esa línea: si ofrece a todas, un colaborador de
    Tlaxcala le abre un chat a uno de Puebla."""
    html = _sesion("colab1").get("/team").text
    assert '<option value="jefa1">' in html, "ni a su propia oficina le ofrece"
    assert '<option value="colab2">' not in html, "le ofrece gente de otra oficina"


def test_un_admin_ve_a_la_gente_de_todas_las_oficinas_en_el_chat():
    """§5: un admin ve todas. Es además el control positivo del de arriba: un
    filtro fijo por oficina lo pasaría en verde y dejaría al admin sin poder
    escribirle a Puebla."""
    html = _sesion("jefa1").get("/team").text
    assert '<option value="colab1">' in html and '<option value="colab2">' in html


@pytest.mark.parametrize("integrantes", [["colab2"], ["jefa1", "colab2"], ["fantasma"]])
def test_un_post_armado_no_mete_a_nadie_ajeno_a_una_sala(integrantes):
    """Acotar la lista es la mitad: `members` lo manda el navegador y un POST
    hecho a mano no pasa por ella. Con uno solo que no sea elegible no se crea
    nada — la sala a medias tampoco es la que se pidió."""
    _sesion("colab1").post("/team/rooms", data={"room_type": "group", "name": "",
                                                "members": integrantes})
    with Session(db.engine) as s:
        salas = s.exec(select(ChatRoom).where(ChatRoom.room_type != "general")).all()
    assert salas == [], f"se creó una sala con {integrantes}"


@pytest.mark.parametrize("quien, con", [("colab1", "jefa1"), ("jefa1", "colab2")])
def test_con_alguien_elegible_la_sala_si_se_crea(quien, con):
    """Control positivo: una validación que lo rechazara TODO pasaría en verde
    el candado de arriba y nadie podría abrir un chat. La segunda fila es §5:
    un admin sí abre sala con otra oficina."""
    _sesion(quien).post("/team/rooms", data={"room_type": "group", "name": "",
                                             "members": [con]})
    assert _salas_de(quien) & _salas_de(con), f"{quien} no pudo abrir sala con {con}"
