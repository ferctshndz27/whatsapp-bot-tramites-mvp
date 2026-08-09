"""Reconocimiento de códigos y a qué requisito del trámite pertenece cada uno."""

import pytest

from app import codigos
from app.dashboard import cotizacion_estimada

# Los tres códigos de la prueba real en producción (2026-08-08): tienen forma
# de CURP pero no lo son (17 caracteres, sin el H/M del sexo). El agente debe
# reconocerlos igual — el cliente los mandó como su CURP.
CODIGOS_REALES = "CUSE234543KPLRT02 COUE237643KPLRT02 COSE234543KPLRT00"
CURP_VALIDA = "PEGF900101HDFRRN09"


def test_detecta_los_formatos_oficiales_por_separado():
    detectados = codigos.detectar(
        f"mi curp {CURP_VALIDA}, rfc PEGF900101AB3, nss 12345678901 y ana@correo.com"
    )
    assert detectados == {
        "CURP": [CURP_VALIDA],
        "RFC": ["PEGF900101AB3"],
        "NSS": ["12345678901"],
        "Email": ["ana@correo.com"],
    }


def test_el_prefijo_de_una_curp_no_se_confunde_con_un_rfc():
    assert codigos.detectar(CURP_VALIDA) == {"CURP": [CURP_VALIDA]}


@pytest.mark.parametrize(
    "texto",
    [
        "hola quiero informacion sobre mi pasaporte",
        "5215500000000",  # teléfono: sin letras, no es clave
        ".",
        "",
    ],
)
def test_no_inventa_codigos_donde_no_hay(texto):
    assert codigos.detectar(texto) == {}


def test_las_claves_mal_formadas_se_guardan_sin_clasificar():
    detectados = codigos.detectar(f"acta de nacimientos {CODIGOS_REALES}")
    assert detectados == {codigos.SUELTO: CODIGOS_REALES.split()}


@pytest.mark.parametrize(
    "requisito, tipo",
    [
        ("clave curp", "CURP"),
        ("CURP", "CURP"),
        ("Clave curp del cliente", "CURP"),
        ("CORREO", "Email"),
        ("nss", "NSS"),
        # Piden más de un dato: un solo código no los satisface y darlos por
        # recibidos escondería lo que falta.
        ("nss y curp", None),
        ("rfc o  clave curp", None),
        ("Nss, curp y nombre del cliente", None),
        # Sin formato que validar.
        ("nombre completo", None),
        ("numero de servicio", None),
    ],
)
def test_tipo_del_requisito(requisito, tipo):
    assert codigos.tipo_del_requisito(requisito) == tipo


def test_prellena_varios_codigos_del_mismo_tipo_en_un_solo_requisito():
    # El caso del screenshot: un trámite, tres códigos. NO se abre un folio por
    # código — quien los desdobla es el asesor.
    requisitos = [{"req": "clave curp", "valor": None, "pedido": False}]
    llenos = codigos.prellenar(requisitos, codigos.detectar(CODIGOS_REALES))

    assert llenos[0]["valor"] == CODIGOS_REALES.replace(" ", ", ")
    assert requisitos[0]["valor"] is None  # la lista de entrada no se toca


def test_no_pisa_un_valor_ya_capturado():
    requisitos = [{"req": "clave curp", "valor": "el que ya estaba", "pedido": True}]
    llenos = codigos.prellenar(requisitos, codigos.detectar(CURP_VALIDA))
    assert llenos[0]["valor"] == "el que ya estaba"


def test_un_codigo_sin_clasificar_no_se_reparte_entre_dos_claves():
    # "NSS" y "CURP" en líneas aparte: no hay forma de saber cuál es cuál, así
    # que se prefiere preguntar antes que adivinar.
    requisitos = [
        {"req": "NSS", "valor": None, "pedido": False},
        {"req": "CURP", "valor": None, "pedido": False},
    ]
    llenos = codigos.prellenar(requisitos, codigos.detectar(CODIGOS_REALES))
    assert [r["valor"] for r in llenos] == [None, None]


def test_un_codigo_exacto_si_llena_su_requisito_aunque_haya_otras_claves():
    requisitos = [
        {"req": "NSS", "valor": None, "pedido": False},
        {"req": "CURP", "valor": None, "pedido": False},
    ]
    llenos = codigos.prellenar(
        requisitos, codigos.detectar(f"{CURP_VALIDA} y mi nss 12345678901")
    )
    assert [r["valor"] for r in llenos] == ["12345678901", CURP_VALIDA]


@pytest.mark.parametrize(
    "requisitos, texto, cuantos",
    [
        # Tres claves para un trámite que pide una clave = tres unidades.
        (["clave curp"], CODIGOS_REALES, 3),
        (["clave curp"], CURP_VALIDA, 1),
        (["clave curp"], "quiero mi acta", 1),
        # Tres correos NO son tres actas: solo cuenta el dato que discrimina.
        (["clave curp"], "a@bc.com d@ef.com g@hi.com", 1),
        # El trámite pide dos claves: no hay una sola que cuente unidades.
        (["nss y curp"], CODIGOS_REALES, 1),
        (["NSS", "CURP"], CODIGOS_REALES, 1),
        # Sin dato con formato tampoco hay nada que contar.
        (["nombre completo"], CODIGOS_REALES, 1),
        # Tope: un pegote enorme no multiplica una cotización sin límite.
        (["clave curp"], " ".join(f"CUSE23454{i}KPLRT02" for i in range(15)), 10),
    ],
)
def test_cuantos_tramites(requisitos, texto, cuantos):
    assert codigos.cuantos_tramites(requisitos, codigos.detectar(texto)) == cuantos


def test_la_clasificacion_del_modelo_mueve_los_codigos_a_su_tipo():
    detectados = codigos.detectar(CODIGOS_REALES)
    assert detectados == {codigos.SUELTO: CODIGOS_REALES.split()}

    clasificados = codigos.aplicar_clasificacion(
        detectados, dict.fromkeys(CODIGOS_REALES.split(), "CURP")
    )

    assert clasificados == {"CURP": CODIGOS_REALES.split()}
    assert detectados == {codigos.SUELTO: CODIGOS_REALES.split()}  # entrada intacta
    # Ya clasificados, ahora SÍ llenan su requisito aunque el trámite pida dos
    # claves distintas — que es justo lo que el modelo local aporta.
    llenos = codigos.prellenar(
        [
            {"req": "NSS", "valor": None, "pedido": False},
            {"req": "CURP", "valor": None, "pedido": False},
        ],
        clasificados,
    )
    assert [r["valor"] for r in llenos] == [None, CODIGOS_REALES.replace(" ", ", ")]


def test_sin_veredicto_del_modelo_todo_queda_igual():
    # "Ollama no pudo verificar": el flujo sigue con lo que dieron los patrones.
    detectados = codigos.detectar(CODIGOS_REALES)
    assert codigos.aplicar_clasificacion(detectados, {}) == detectados


def test_lo_que_el_modelo_no_reconocio_se_queda_sin_tipo():
    detectados = codigos.detectar(CODIGOS_REALES)
    primero, segundo, tercero = CODIGOS_REALES.split()

    clasificados = codigos.aplicar_clasificacion(detectados, {primero: "CURP"})

    assert clasificados == {"CURP": [primero], codigos.SUELTO: [segundo, tercero]}


def _capturado(req: str, valor: str) -> list[dict]:
    return [{"req": req, "valor": valor, "pedido": False}]


def test_el_folio_muestra_el_estimado_derivado_de_lo_capturado():
    # Sin columna "cantidad": el número sale de los códigos que ya viven en el
    # folio, así que el espejo MSSQL no se entera de nada.
    cotizacion = cotizacion_estimada(
        "$25.00", _capturado("clave curp", CODIGOS_REALES.replace(" ", ", "))
    )
    assert cotizacion == {"cuantos": 3, "total": "$75.00 MXN"}


def test_el_folio_no_muestra_estimado_cuando_es_una_sola():
    assert cotizacion_estimada("$25.00", _capturado("clave curp", CURP_VALIDA)) is None
    assert cotizacion_estimada("$25.00", []) is None


def test_el_folio_cuenta_aunque_el_precio_no_sea_un_numero():
    cotizacion = cotizacion_estimada(
        "A cotizar", _capturado("clave curp", CODIGOS_REALES.replace(" ", ", "))
    )
    assert cotizacion == {"cuantos": 3, "total": None}


@pytest.mark.parametrize(
    "requisito, respuesta, trae",
    [
        ("clave curp", CURP_VALIDA, True),
        ("clave curp", "CUSE234543KPLRT02", True),  # mal formada, pero es una clave
        ("clave curp", ".", False),
        ("clave curp", "ya te la mande", False),
        # De un dato sin formato oficial no hay nada que validar.
        ("nombre completo", ".", True),
        ("nss y curp", ".", True),
    ],
)
def test_trae_el_dato(requisito, respuesta, trae):
    assert codigos.trae_el_dato(requisito, respuesta) is trae
