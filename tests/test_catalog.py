from app.catalog import catalog, parse_requirements, requisitos_del_ticket, requisitos_iniciales


def test_parse_requirements_una_linea_un_dato():
    # Una línea = un dato a pedir; las viñetas y la numeración se limpian.
    assert parse_requirements("") == []
    assert parse_requirements("   \n  ") == []
    assert parse_requirements("clave curp") == ["clave curp"]
    assert parse_requirements("- clave curp\n2) nss\n• acta") == ["clave curp", "nss", "acta"]
    # Una frase con comas NO se trocea: "original y copia" no son dos requisitos.
    assert parse_requirements("acta de nacimiento, original y copia") == [
        "acta de nacimiento, original y copia"
    ]
    assert len(parse_requirements("\n".join(f"dato {i}" for i in range(20)))) == 5


def test_requisitos_iniciales_y_lectura_tolerante():
    congelado = requisitos_iniciales("clave curp\nnss")
    assert requisitos_del_ticket(congelado) == [
        {"req": "clave curp", "valor": None, "pedido": False},
        {"req": "nss", "valor": None, "pedido": False},
    ]
    # Folio congelado antes de que existiera "pedido": se lee como no pedido.
    assert requisitos_del_ticket('[{"req": "nss", "valor": null}]') == [
        {"req": "nss", "valor": None, "pedido": False}
    ]
    # Sin requisitos documentados no se congela nada: el agente no pregunta.
    assert requisitos_iniciales("   ") is None
    # Folio viejo o JSON corrupto: sin requisitos que mostrar, sin reventar.
    assert requisitos_del_ticket(None) == []
    assert requisitos_del_ticket("{no es json") == []
    assert requisitos_del_ticket('[{"sin_req": 1}, "basura"]') == []


def test_loads_all_categories():
    names = [c.name for c in catalog.categories]
    assert "Servicios básicos" in names
    assert "Infonavit" in names
    assert len(catalog.categories) == 9


def test_category_by_index():
    first = catalog.category_by_index(1)
    assert first is not None
    assert first.name == "Servicios básicos"
    assert catalog.category_by_index(0) is None
    assert catalog.category_by_index(999) is None


def test_service_by_index():
    service = catalog.service_by_index("servicios-basicos", 1)
    assert service is not None
    assert service.name == "Acta de nacimiento"
    assert catalog.service_by_index("servicios-basicos", 999) is None
    assert catalog.service_by_index("no-existe", 1) is None


def test_search_by_keyword_single_match():
    matches = catalog.search_by_keyword("acta de divorcio")
    assert len(matches) == 1
    assert matches[0].id == "sb-acta-divorcio"


def test_search_by_keyword_short_text_returns_nothing():
    assert catalog.search_by_keyword("ab") == []


def test_infonavit_pagination_over_ten_items():
    category = catalog.category_by_id("infonavit")
    assert len(category.services) == 12

    # Páginas de 8: cada una cabe en la lista nativa de WhatsApp (8 + Ver más
    # + Volver = 10 filas, tope de Meta).
    page0 = catalog.format_service_menu("infonavit", page=0)
    assert "8." in page0
    assert "9." not in page0
    assert "más" in page0.lower()

    page1 = catalog.format_service_menu("infonavit", page=1)
    assert "9." in page1
    assert "12." in page1
