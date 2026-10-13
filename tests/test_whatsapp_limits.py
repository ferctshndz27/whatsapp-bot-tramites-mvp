"""Límites de la API de Meta y capa de presentación.

Un mensaje que excede los topes de WhatsApp se rechaza en producción y el
cliente se queda sin respuesta. Estas pruebas recorren el catálogo REAL —no un
ejemplo— y verifican que todo lo que sale de conversation._menu_rows sobrevive
intacto al armado del payload en whatsapp_client.
"""

import json
import math

import pytest
from sqlmodel import Session

import app.db as db
import app.nlu as nlu
from app import conversation, whatsapp_client
from app.catalog import PAGE_SIZE, catalog
from app.conversation import (
    CONFIRM_BUTTONS,
    LIST_BUTTON_LABEL,
    MENU_MARKER,
    handle_incoming,
    presentation,
)
from app.models import CatalogCategory, CatalogService, Conversation
from app.webhook import _deliver, _extract_text
from app.whatsapp_client import (
    BUTTON_TITLE_MAX,
    LIST_BODY_MAX,
    LIST_DESC_MAX,
    LIST_TITLE_MAX,
    MAX_BUTTONS,
    MAX_LIST_ROWS,
    _buttons_payload,
    _list_payload,
)

PHONE = "5215544443333"


def test_los_limites_de_meta_son_los_del_contrato_externo():
    # Ancla de VALORES. El resto del archivo assertea contra las constantes
    # importadas (`len(title) <= LIST_TITLE_MAX`), que es lo correcto para la
    # coherencia interna entre _menu_rows y whatsapp_client — pero se
    # autocancela: encoger LIST_TITLE_MAX a 12 mutila todos los títulos Y
    # relaja el assert en el mismo paso. Estos seis números NO son nuestros,
    # son el contrato de la API de Meta: se anclan al literal. Si Meta los
    # cambia, se actualiza ESTA línea a mano.
    assert (
        MAX_LIST_ROWS,
        LIST_TITLE_MAX,
        LIST_DESC_MAX,
        LIST_BODY_MAX,
        MAX_BUTTONS,
        BUTTON_TITLE_MAX,
    ) == (10, 24, 72, 1024, 3, 20)


# ---- Fixtures locales ----
# El autouse _isolated_db de conftest parcha app.db.engine con una base en
# memoria; el fixture "session" del conftest usa OTRO engine. Aquí se abre la
# sesión sobre app.db.engine para que el catálogo (que lee ese engine) y las
# aserciones miren la MISMA base, sin monkeypatch de por medio.


@pytest.fixture()
def db_session():
    with Session(db.engine) as s:
        yield s


@pytest.fixture(autouse=True)
def _sin_llm(monkeypatch):
    """Ninguna prueba puede llamar a la API real de Anthropic aunque el entorno
    tenga ANTHROPIC_API_KEY puesta: la cascada arranca en keywords."""
    monkeypatch.setattr(nlu.settings, "anthropic_api_key", "")


@pytest.fixture()
def catalogo_real():
    """Foto única de las categorías: catalog._refresh() corre en CADA acceso a
    la propiedad y devuelve objetos nuevos (nunca comparar por identidad)."""
    return list(catalog.categories)


# ---- Helpers ----


def _conversacion(session: Session, state: str, **context) -> Conversation:
    conv = session.get(Conversation, PHONE)
    if conv is None:
        conv = Conversation(phone=PHONE)
    conv.state = state
    conv.context_json = json.dumps(context)
    session.add(conv)
    session.commit()
    return conv


def _paginas(category) -> int:
    return max(1, math.ceil(len(category.services) / PAGE_SIZE))


def _filas(category_id: str, page: int) -> list[tuple[str, str]]:
    """Filas (id, título) tal como las emite la máquina de estados."""
    conv = Conversation(
        phone=PHONE,
        state="menu_servicio",
        context_json=json.dumps({"category_id": category_id, "page": page}),
    )
    return conversation._menu_rows(conv)


def _filas_construidas(body: str, rows: list[tuple[str, str]]) -> list[dict]:
    """Filas ya sometidas a los límites de Meta por whatsapp_client."""
    payload = _list_payload(PHONE, body, LIST_BUTTON_LABEL, rows)
    return payload["interactive"]["action"]["sections"][0]["rows"]


def _sembrar_categorias(session: Session, n: int) -> None:
    """Inserta n categorías ANTES del primer acceso al catálogo: Catalog._refresh
    solo importa la semilla JSON si la tabla está vacía."""
    for i in range(1, n + 1):
        session.add(CatalogCategory(id=f"cat-{i}", name=f"Categoría {i}", position=i))
        session.add(
            CatalogService(
                id=f"svc-{i}", category_id=f"cat-{i}", name=f"Servicio {i}",
                price_info="A cotizar", position=0,
            )
        )
    session.commit()


class _EspiaWhatsApp:
    """Doble de app.whatsapp_client: cuenta llamadas y guarda los argumentos.
    Devuelve un wamid NO vacío cuando simula éxito (un '' dispararía el
    fallback de _deliver y la prueba fallaría por la razón equivocada)."""

    def __init__(self, wamid: str | None = None):
        self.wamid = wamid
        self.llamadas: list[tuple] = []

    def __call__(self, *args):
        self.llamadas.append(args)
        return self.wamid

    @property
    def veces(self) -> int:
        return len(self.llamadas)


# ---- Filas de lista: topes de Meta sobre el catálogo real ----


def test_menu_rows_nunca_excede_el_tope_de_filas_de_meta(catalogo_real):
    # REGLA: ninguna pantalla puede pedirle a Meta más de MAX_LIST_ROWS filas;
    # si se pasa, whatsapp_client recorta en silencio y el cliente pierde
    # opciones (incluida la de "Volver", que va al final).
    for cat in catalogo_real:
        for page in range(_paginas(cat) + 1):  # +1: una página fuera de rango
            rows = _filas(cat.id, page)
            assert len(rows) <= MAX_LIST_ROWS, (
                f"{cat.id} página {page} pide {len(rows)} filas y el tope de Meta "
                f"es {MAX_LIST_ROWS}: las de más se descartan sin aviso."
            )


def test_ninguna_fila_del_catalogo_se_pierde_al_construir_el_payload_de_meta(catalogo_real):
    # REGLA: el recorte defensivo de whatsapp_client (rows[:MAX_LIST_ROWS] y
    # title[:24]) es una red de seguridad que JAMÁS debe activarse con el
    # catálogo real. Por eso se compara contra len(rows), no contra el tope:
    # "len(construidas) <= 10" pasaría siempre gracias al propio truncado.
    for cat in catalogo_real:
        for page in range(_paginas(cat)):
            body = catalog.format_service_menu(cat.id, page)
            rows = _filas(cat.id, page)
            payload = _list_payload(PHONE, body, LIST_BUTTON_LABEL, rows)
            built = payload["interactive"]["action"]["sections"][0]["rows"]

            assert len(built) == len(rows), f"{cat.id} p{page}: Meta descartaría filas"
            for row in built:
                assert row["id"] and len(row["id"]) <= 200
                assert 1 <= len(row["title"]) <= LIST_TITLE_MAX
                assert len(row.get("description", "")) <= LIST_DESC_MAX
            # La etiqueta del botón de la acción tampoco se recorta.
            assert payload["interactive"]["action"]["button"] == LIST_BUTTON_LABEL
            assert len(LIST_BUTTON_LABEL) <= BUTTON_TITLE_MAX


def test_titulo_recortado_conserva_el_nombre_completo_en_la_descripcion(catalogo_real):
    # REGLA: el nombre del trámite es lo que el cliente está comprando. Si el
    # título se recorta a 24, el nombre COMPLETO tiene que seguir visible en la
    # descripción; un título mutilado sin descripción hace que pida otra cosa.
    for cat in catalogo_real:
        for page in range(_paginas(cat)):
            body = catalog.format_service_menu(cat.id, page)
            rows = _filas(cat.id, page)
            built = _filas_construidas(body, rows)

            for (_, nombre), fila in zip(rows, built):
                if len(nombre) > LIST_TITLE_MAX:
                    # El día que alguien meta un nombre de más de 72 chars el
                    # nombre completo dejaría de caber en la descripción.
                    assert len(nombre) <= LIST_DESC_MAX, (
                        f"'{nombre}' ({len(nombre)}) no cabe completo en la "
                        f"descripción (tope {LIST_DESC_MAX})"
                    )
                    assert fila["description"] == nombre
                else:
                    # Contrato actual: título corto → sin descripción (sin ruido).
                    assert "description" not in fila


def test_titulos_que_colisionan_al_recortar_se_desambiguan_por_descripcion(catalogo_real):
    # REGLA: dos filas con el mismo título visible son indistinguibles para el
    # cliente. Si el recorte a 24 colapsa varios nombres, las descripciones
    # DEBEN diferir y traer el nombre completo.
    colisionan = set()
    for cat in catalogo_real:
        for page in range(_paginas(cat)):
            body = catalog.format_service_menu(cat.id, page)
            built = _filas_construidas(body, _filas(cat.id, page))

            grupos: dict[str, list[str | None]] = {}
            for fila in built:
                grupos.setdefault(fila["title"], []).append(fila.get("description"))
            for titulo, descripciones in grupos.items():
                if len(descripciones) > 1:
                    colisionan.add(cat.id)
                    assert all(d is not None for d in descripciones), (
                        f"{cat.id}: '{titulo}' se repite sin descripción que lo distinga"
                    )
                    assert len(set(descripciones)) == len(descripciones), (
                        f"{cat.id}: '{titulo}' se repite con descripciones iguales"
                    )

    # Hoy los 4 "Constancia de situación fiscal…" y las 2 "Alta para retiro por
    # desempleo…" se ven idénticas en negrita y solo la descripción las separa.
    # Antes esto se asserteaba (`{"servicios-sat","afore"} <= colisionan`), lo
    # que EXIGÍA que el defecto siguiera existiendo: renombrar los servicios
    # para distinguirlos dentro de 24 chars —una mejora de UX— ponía la prueba
    # en rojo. Queda como registro; el invariante que importa es el de arriba
    # (si colisionan, la descripción los distingue), no que colisionen.
    print(f"\ncategorías con títulos que colisionan al recortar: {sorted(colisionan)}")


@pytest.mark.parametrize(
    "nombre",
    [
        "Constancia de antecedentes no penales (estatal)",  # 47 chars
        "Amparo para liberar pensión en Mod. 40",  # 38 chars
        "Precalificación MejoraVit",  # 25 chars: el frontera 24+1
    ],
)
def test_nombres_largos_reales_llegan_completos_al_cliente(catalogo_real, nombre):
    # REGLA: los nombres más largos del catálogo son casos frontera reales del
    # tope de 24 de Meta y no pueden quedar mutilados sin recuperación.
    encontrado = False
    for cat in catalogo_real:
        for page in range(_paginas(cat)):
            body = catalog.format_service_menu(cat.id, page)
            rows = _filas(cat.id, page)
            built = _filas_construidas(body, rows)
            for (_, titulo), fila in zip(rows, built):
                if titulo != nombre:
                    continue
                encontrado = True
                assert fila["description"] == nombre  # completo en la descripción
                # Y también en el cuerpo: Meta no auto-abre la lista, así que
                # las opciones numeradas del body son la interfaz de facto.
                assert nombre in body

    assert encontrado, f"'{nombre}' ya no existe en el catálogo: actualiza la prueba"


def test_cuerpo_de_todos_los_menus_cabe_en_el_limite_de_1024(catalogo_real):
    # REGLA: como Meta no auto-abre el desplegable, las opciones numeradas del
    # BODY son lo único que el cliente ve sin tocar nada. Si el body se recorta
    # a 1024 se pierden opciones sin ningún aviso.
    cuerpos = [catalog.format_category_menu()]
    for cat in catalogo_real:
        cuerpos += [catalog.format_service_menu(cat.id, p) for p in range(_paginas(cat))]

    for body in cuerpos:
        assert len(body) <= LIST_BODY_MAX
        payload = _list_payload(PHONE, body, LIST_BUTTON_LABEL, [("1", "x")])
        # Igualdad exacta, no longitud: detecta el truncado silencioso.
        assert payload["interactive"]["body"]["text"] == body


# ---- Menú de categorías: la puerta de entrada ----


def test_menu_de_categorias_no_pierde_ninguna_categoria_por_truncado_silencioso(
    db_session, catalogo_real
):
    # REGLA: _menu_rows corta el menú de categorías con [:10]. Una categoría por
    # encima del tope desaparece para SIEMPRE del bot: sin error, sin log y sin
    # que nadie se entere.
    conv = _conversacion(db_session, "menu_categoria")
    rows = conversation._menu_rows(conv)

    assert len(rows) == len(catalogo_real), "se cayó alguna categoría al armar el menú"
    assert [r[0] for r in rows] == [str(i) for i in range(1, len(catalogo_real) + 1)]
    assert [r[1] for r in rows] == [c.name for c in catalogo_real]
    assert len(catalogo_real) <= MAX_LIST_ROWS, (
        f"hay {len(catalogo_real)} categorías y el tope de Meta es {MAX_LIST_ROWS}: "
        "hay que paginar o agrupar categorías antes de seguir agregando."
    )


@pytest.mark.xfail(
    reason="LÍMITE: _menu_rows corta con [:10] sin paginar categorías",
    strict=True,
)
def test_una_undecima_categoria_deberia_seguir_siendo_alcanzable(db_session):
    # REGLA (deseada): el bot no debe tener un techo duro invisible de 10
    # categorías. Se auto-desactiva el día que se implemente la paginación.
    _sembrar_categorias(db_session, 11)
    conv = _conversacion(db_session, "menu_categoria")

    rows = conversation._menu_rows(conv)

    assert len(catalog.categories) == 11
    assert len(rows) == 11 or any(r[0] == "mas" for r in rows)
    assert "11" in [r[0] for r in rows], "la categoría 11 es inalcanzable desde la lista"


# ---- Paginación de servicios ----


def test_page_size_mas_filas_de_navegacion_cabe_en_el_tope_de_meta(catalogo_real):
    # REGLA: una página llena es PAGE_SIZE servicios + "Ver más" + "Volver". Esa
    # aritmética DEBE caber en MAX_LIST_ROWS o el cliente pierde la fila
    # "Volver" y se queda encerrado en el submenú.
    assert PAGE_SIZE + 2 <= MAX_LIST_ROWS, (
        f"PAGE_SIZE={PAGE_SIZE} + 'Ver más' + 'Volver' no cabe en {MAX_LIST_ROWS} filas"
    )

    paginadas = [c for c in catalogo_real if len(c.services) > PAGE_SIZE]
    assert paginadas, "ninguna categoría pagina: la prueba dejó de ejercitar el caso"
    for cat in paginadas:
        rows = _filas(cat.id, 0)
        # También detecta el número mágico '8' hardcodeado en _menu_rows: si
        # alguien sube PAGE_SIZE a 9, devuelve [] y toda página llena cae a
        # texto plano en silencio.
        assert len(rows) == PAGE_SIZE + 2, f"{cat.id} p0 debería tener la página llena"
        assert [r[0] for r in rows] == (
            [str(i) for i in range(1, PAGE_SIZE + 1)] + ["mas", "0"]
        )


def test_ninguna_pagina_pierde_ni_duplica_servicios_del_catalogo(catalogo_real):
    # REGLA: la unión de todas las páginas de una categoría debe ser exactamente
    # sus servicios, sin huecos ni repetidos. Un servicio invisible es una venta
    # perdida que nadie reporta.
    for cat in catalogo_real:
        vistos: list[tuple[str, str]] = []
        for page in range(_paginas(cat)):
            vistos += [
                (rid, titulo)
                for rid, titulo in _filas(cat.id, page)
                if rid.isdigit() and rid != "0"
            ]

        indices = [int(rid) for rid, _ in vistos]
        assert indices == list(range(1, len(cat.services) + 1)), (
            f"{cat.id}: la numeración de las filas no cubre sus {len(cat.services)} servicios"
        )
        assert [t for _, t in vistos] == [s.name for s in cat.services]
        # El id de la fila y el índice del catálogo no pueden desincronizarse:
        # es el número que el cliente teclea y con el que se abre el ticket.
        for idx, (_, titulo) in zip(indices, vistos):
            assert catalog.service_by_index(cat.id, idx).name == titulo


def test_ultima_pagina_sin_ver_mas_y_toda_pagina_con_fila_volver(catalogo_real):
    # REGLA: "Ver más" solo debe existir si hay página siguiente (ofrecerlo al
    # final manda al cliente a un menú vacío), y "Volver" debe ir SIEMPRE al
    # final o el cliente se queda encerrado en el submenú.
    for cat in catalogo_real:
        for page in range(_paginas(cat)):
            ids = [r[0] for r in _filas(cat.id, page)]
            assert ids[-1] == "0", f"{cat.id} p{page} no ofrece volver"
            assert ids.count("0") == 1
            hay_siguiente = (page + 1) * PAGE_SIZE < len(cat.services)
            assert ("mas" in ids) is hay_siguiente, (
                f"{cat.id} p{page}: 'Ver más' {'falta' if hay_siguiente else 'sobra'}"
            )


def test_ids_de_ver_mas_y_volver_vuelven_a_entrar_en_la_maquina_de_estados(
    db_session, catalogo_real
):
    # REGLA (contrato central de la Fase 1): el id de cada fila ES el texto que
    # el cliente habría tecleado. Si alguien renombra 'mas' a 'ver_mas' la lista
    # se ve perfecta pero el toque no hace NADA, y ninguna prueba de payload lo
    # detectaría.
    indice_infonavit = [c.id for c in catalogo_real].index("infonavit") + 1
    handle_incoming(db_session, PHONE, "servicios")
    handle_incoming(db_session, PHONE, str(indice_infonavit))

    rows = dict(_filas("infonavit", 0))
    assert "mas" in rows and "0" in rows

    # El toque llega al webhook como interactive.list_reply.id.
    texto_mas, _ = _extract_text(
        {"type": "interactive", "interactive": {"list_reply": {"id": "mas", "title": rows["mas"]}}}
    )
    replies = handle_incoming(db_session, PHONE, texto_mas)

    db_session.expire_all()
    conv = db_session.get(Conversation, PHONE)
    assert conv.state == "menu_servicio"
    assert json.loads(conv.context_json)["page"] == 1
    servicios = catalog.category_by_id("infonavit").services
    for i, svc in enumerate(servicios[PAGE_SIZE:], start=PAGE_SIZE + 1):
        assert f"{i}. {svc.name}" in replies[-1]

    texto_volver, _ = _extract_text(
        {"type": "interactive", "interactive": {"list_reply": {"id": "0", "title": rows["0"]}}}
    )
    replies = handle_incoming(db_session, PHONE, texto_volver)

    db_session.expire_all()
    assert db_session.get(Conversation, PHONE).state == "menu_categoria"
    assert catalogo_real[0].name in replies[-1]


# ---- Botones y capa de presentación ----


def test_estado_confirmar_emite_exactamente_tres_botones_que_la_maquina_acepta(db_session):
    # REGLA: la confirmación es donde nace un ticket. Deben ser 2 botones con
    # títulos <= 20, y sus ids TIENEN que ser palabras que _handle_confirmar
    # reconozca: si no, tocar el botón cuenta como intento fallido y al segundo
    # el cliente termina escalado con un humano sin necesidad.
    _conversacion(db_session, "confirmar", pending_service_ids=["sb-curp"])

    out = presentation(db_session, PHONE, ["¿Confirmas este trámite?"])

    assert isinstance(out[-1], dict)
    assert [b[0] for b in out[-1]["buttons"]] == ["si", "no", "cancelar"]
    assert len(out[-1]["buttons"]) == 3 <= MAX_BUTTONS

    buttons = _buttons_payload(PHONE, out[-1]["text"], out[-1]["buttons"])[
        "interactive"
    ]["action"]["buttons"]
    assert [b["reply"]["id"] for b in buttons] == ["si", "no", "cancelar"]  # ninguno se descarta
    # len() cuenta code points y Meta cuenta caracteres: hoy coinciden porque
    # los emoji usados son de un solo code point (un emoji ZWJ los separaría).
    assert all(len(b["reply"]["title"]) <= BUTTON_TITLE_MAX for b in buttons)

    assert CONFIRM_BUTTONS[0][0] in conversation.YES_WORDS
    assert CONFIRM_BUTTONS[1][0] in conversation.NO_WORDS
    # «cancelar» lo reconoce _handle_confirmar: tocar el botón no cuenta como intento fallido.
    assert conversation._quiere_cancelar(CONFIRM_BUTTONS[2][0])


def test_presentation_solo_transforma_la_ultima_respuesta_y_no_muta_la_lista(db_session):
    # REGLA: presentation() solo decide CÓMO se ve el ÚLTIMO mensaje; los
    # anteriores viajan como texto plano. Y no debe mutar la lista que recibe:
    # esa misma lista ya se registró en la bitácora.
    _conversacion(db_session, "menu_servicio", category_id="infonavit", page=0)
    replies = ["aviso previo", catalog.format_service_menu("infonavit")]

    out = presentation(db_session, PHONE, replies)

    assert out is not replies
    assert out[0] == "aviso previo" and isinstance(out[0], str)
    assert isinstance(out[-1], dict) and "list" in out[-1]
    assert all(isinstance(r, str) for r in replies)  # la entrada quedó intacta
    assert replies[-1] == out[-1]["text"]


@pytest.mark.parametrize(
    "state", ["inicio", "esperando_nombre", "pidiendo_requisitos", "escalado"]
)
def test_estados_sin_menu_se_quedan_en_texto_plano(db_session, state):
    # REGLA: solo menu_categoria / menu_servicio / eligiendo_sugerencia producen
    # lista nativa. En cualquier otro estado el bot cae a texto: mandar una
    # lista vacía a Meta es un 400 y el cliente se queda sin respuesta.
    conv = _conversacion(db_session, state)
    texto = catalog.format_category_menu()
    assert MENU_MARKER in texto  # el marcador SÍ está: lo que salva es el estado

    assert conversation._menu_rows(conv) == []
    assert presentation(db_session, PHONE, [texto]) == [texto]


def test_presentation_sin_conversacion_ni_respuestas_no_inventa_nada(db_session):
    # REGLA: sin fila Conversation no hay estado que interpretar (texto tal cual)
    # y con replies vacío el silencio es total (agente en pausa).
    texto = catalog.format_category_menu()
    assert presentation(db_session, "5215500000001", [texto]) == [texto]

    _conversacion(db_session, "menu_categoria")
    assert presentation(db_session, PHONE, []) == []


# ---- Fallback de envío (webhook._deliver) ----


def test_fallback_a_texto_cuando_falla_el_envio_de_lista(monkeypatch):
    # REGLA: un interactivo rechazado por Meta no puede dejar al cliente sin
    # respuesta; _deliver reintenta como texto plano con el MISMO cuerpo (que ya
    # trae las opciones numeradas).
    lista = _EspiaWhatsApp(wamid=None)
    texto = _EspiaWhatsApp(wamid="wamid-txt")
    monkeypatch.setattr(whatsapp_client, "send_list", lista)
    monkeypatch.setattr(whatsapp_client, "send_text", texto)

    body = catalog.format_service_menu("infonavit")
    item = {"text": body, "list": _filas("infonavit", 0), "button": LIST_BUTTON_LABEL}

    assert _deliver(PHONE, item) == "wamid-txt"
    assert texto.veces == 1
    assert texto.llamadas[0] == (PHONE, body)  # cuerpo completo, no recortado


def test_fallback_a_texto_cuando_fallan_los_botones(monkeypatch):
    # REGLA: misma garantía en la rama de botones. Si Meta rechaza el
    # interactivo de confirmación, el cliente igual recibe la pregunta y puede
    # contestar "sí"/"no" tecleando.
    botones = _EspiaWhatsApp(wamid=None)
    texto = _EspiaWhatsApp(wamid="wamid-txt")
    lista = _EspiaWhatsApp(wamid="wamid-lista")
    monkeypatch.setattr(whatsapp_client, "send_buttons", botones)
    monkeypatch.setattr(whatsapp_client, "send_text", texto)
    monkeypatch.setattr(whatsapp_client, "send_list", lista)

    body = "¿Confirmas 'Descarga de CURP'? Responde sí o no."
    assert _deliver(PHONE, {"text": body, "buttons": list(CONFIRM_BUTTONS)}) == "wamid-txt"
    assert texto.llamadas == [(PHONE, body)]
    assert lista.veces == 0  # la rama se elige por "buttons" in item


def test_envio_interactivo_exitoso_no_duplica_el_mensaje(monkeypatch):
    # REGLA: si el interactivo se envió bien NO puede mandarse además el texto;
    # un doble envío duplica el mensaje y quema cuota de la ventana de 24h.
    lista = _EspiaWhatsApp(wamid="wamid-1")
    botones = _EspiaWhatsApp(wamid="wamid-2")
    texto = _EspiaWhatsApp(wamid="wamid-txt")
    monkeypatch.setattr(whatsapp_client, "send_list", lista)
    monkeypatch.setattr(whatsapp_client, "send_buttons", botones)
    monkeypatch.setattr(whatsapp_client, "send_text", texto)

    item_lista = {
        "text": catalog.format_service_menu("infonavit"),
        "list": _filas("infonavit", 0),
        "button": LIST_BUTTON_LABEL,
    }
    assert _deliver(PHONE, item_lista) == "wamid-1"
    assert _deliver(PHONE, {"text": "¿Confirmas?", "buttons": list(CONFIRM_BUTTONS)}) == "wamid-2"
    assert texto.veces == 0


# ---- Robustez ante contexto corrupto ----


@pytest.mark.parametrize(
    "context_json",
    [
        "no-es-json",
        "",
        "{}",
        '{"category_id": "no-existe", "page": 0}',
        '{"category_id": "infonavit", "page": 99}',
        '{"category_id": "infonavit", "page": -1}',
        '{"category_id": null, "page": 0}',
    ],
)
def test_menu_rows_cae_a_texto_ante_contexto_corrupto(db_session, context_json):
    # REGLA: un contexto corrupto debe degradar a texto plano, nunca romper. Una
    # excepción aquí sube hasta webhook._process_message, que la traga con
    # logger.exception, y el cliente se queda SIN NINGUNA respuesta.
    # Se prueba directo sobre _menu_rows/presentation: a nivel de webhook la
    # excepción quedaría oculta y la prueba pasaría en falso.
    conv = Conversation(phone=PHONE, state="menu_servicio", context_json=context_json)
    db_session.add(conv)
    db_session.commit()

    assert conversation._menu_rows(conv) == []
    texto = catalog.format_service_menu("infonavit")
    assert presentation(db_session, PHONE, [texto]) == [texto]


@pytest.mark.xfail(
    reason="BUG: _get_context no protege JSON válido no-objeto ni page no entero",
    strict=True,
)
@pytest.mark.parametrize(
    "context_json",
    ["null", "[]", '{"category_id": "infonavit", "page": "1"}'],
)
def test_menu_rows_sobrevive_a_un_contexto_json_valido_pero_no_objeto(db_session, context_json):
    # REGLA (deseada): _get_context solo captura JSONDecodeError/TypeError, así
    # que un JSON válido pero no-objeto revienta con AttributeError y un "page"
    # string con TypeError. El cliente queda mudo y en el log solo hay un
    # stacktrace.
    conv = Conversation(phone=PHONE, state="menu_servicio", context_json=context_json)

    assert conversation._menu_rows(conv) == []


@pytest.mark.xfail(
    reason="BUG: pedir 'mas' pasada la última página entrega un menú vacío y "
           "sigue avanzando page (catalog.format_service_menu devuelve un string "
           "truthy fuera de rango, así que la guarda 'if menu:' nunca dispara)",
    strict=True,
)
def test_ver_mas_pasada_la_ultima_pagina_no_entrega_un_menu_vacio(db_session, catalogo_real):
    # REGLA (deseada): las listas de WhatsApp siguen siendo tocables en el
    # historial del chat, así que re-tocar "Ver más opciones" de un mensaje
    # viejo reenvía "mas". Eso debe responder "No hay más opciones en esa
    # categoría", no un menú sin ninguna opción.
    indice_infonavit = [c.id for c in catalogo_real].index("infonavit") + 1
    handle_incoming(db_session, PHONE, "servicios")
    handle_incoming(db_session, PHONE, str(indice_infonavit))
    handle_incoming(db_session, PHONE, "mas")  # página 1: la última, correcta

    replies = handle_incoming(db_session, PHONE, "mas")  # una página de más

    db_session.expire_all()
    conv = db_session.get(Conversation, PHONE)
    hay_opciones = any(
        f"{i}." in replies[-1] for i in range(1, len(catalog.category_by_id("infonavit").services) + 1)
    )
    assert "No hay más opciones" in replies[-1] or hay_opciones
    assert json.loads(conv.context_json).get("page") != 2


# ---------------------------------------------------------------------------
# La FORMA de la respuesta de Meta también es parte del contrato
# ---------------------------------------------------------------------------


class _RespuestaFalsa:
    def __init__(self, cuerpo, revienta_json=False):
        self._cuerpo = cuerpo
        self._revienta = revienta_json

    def raise_for_status(self):
        return None

    def json(self):
        if self._revienta:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._cuerpo


@pytest.mark.parametrize(("nombre", "cuerpo", "esperado"), [
    ("la forma normal", {"messages": [{"id": "wamid.OK"}]}, "wamid.OK"),
    # Los cuatro que reventaban o podían reventar. `messages: []` era el caro:
    # `["messages"][0]` lanzaba IndexError, que NO está en los except de _send
    # —solo capturan errores de httpx— ni en el bucle de webhook.py que llama a
    # _deliver. O sea: 500 al webhook de Meta, Meta reintentando el mensaje
    # entrante, y el cliente recibiendo la conversación repetida.
    ("200 con messages vacío", {"messaging_product": "whatsapp", "messages": []}, None),
    ("200 sin la clave messages", {"messaging_product": "whatsapp"}, None),
    ("messages en null", {"messages": None}, None),
    ("un mensaje sin id", {"messages": [{}]}, None),
    ("el cuerpo es una lista", [1, 2, 3], None),
])
def test_una_respuesta_con_forma_rara_no_tumba_el_envio(nombre, cuerpo, esperado):
    """REGLA: el envío puede quedarse sin wamid; lo que no puede es reventar.

    Un wamid None ya está previsto aguas arriba —`webhook._tag_sent_message`
    solo corre `if wa_message_id`—, así que el mensaje sale igual y solo se
    pierde la correlación con su acuse. Una excepción, en cambio, sube hasta el
    webhook.
    """
    assert whatsapp_client._wamid(_RespuestaFalsa(cuerpo)) == esperado, nombre


def test_un_cuerpo_que_no_es_json_tampoco_tumba_el_envio():
    """Meta puede contestar 200 con HTML (una página de error de un proxy de por
    medio). `.json()` lanza ValueError y hasta aquí llega."""
    assert whatsapp_client._wamid(_RespuestaFalsa(None, revienta_json=True)) is None
