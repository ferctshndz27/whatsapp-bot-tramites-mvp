import json
from types import SimpleNamespace

import app.nlu as nlu
from app.catalog import catalog
from app.config import settings


def _fake_response(payload: dict, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
    )


def _first_service():
    return catalog.categories[0].services[0]


def test_keyword_fallback_finds_unique_service(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    service, category = nlu.deduce("necesito un acta de divorcio")

    assert service is not None
    assert "divorcio" in service.name.lower()
    assert category is None


def test_keyword_fallback_finds_category_by_name(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    cat_name = catalog.categories[0].name

    service, category = nlu.deduce(f"me interesa algo de {cat_name.lower()}")

    assert service is None
    assert category is not None
    assert category.id == catalog.categories[0].id


def test_ambiguous_text_returns_nothing(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")

    assert nlu.deduce("zzz qqq no relacionado") == (None, None)
    assert nlu.deduce("hi") == (None, None)  # muy corto


def test_llm_result_is_validated_against_catalog(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    svc = _first_service()
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": svc.id, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    service, category = nlu.deduce("quiero ese trámite que me dijeron")

    assert service is not None and service.id == svc.id
    assert category is None


def test_llm_hallucinated_id_is_discarded(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response(
                {"service_id": "no_existe_xyz", "category_id": None}
            )
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    assert nlu.deduce("texto cualquiera largo") == (None, None)


def test_llm_failure_falls_back_to_keywords(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    def _boom(**kw):
        raise RuntimeError("timeout simulado")

    fake = SimpleNamespace(messages=SimpleNamespace(create=_boom))
    monkeypatch.setattr(nlu, "_client", fake)

    service, _ = nlu.deduce("necesito un acta de divorcio")

    assert service is not None  # las keywords rescatan la respuesta
    assert "divorcio" in service.name.lower()


def _catalog_with_synonyms(tmp_path):
    from app.catalog import Catalog

    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps(
            {
                "categories": [
                    {
                        "id": "basicos",
                        "name": "Servicios básicos",
                        "services": [
                            {
                                "id": "curp",
                                "name": "CURP certificada",
                                "price_info": "$80",
                                "synonyms": ["clave única", "papel de la curp"],
                            },
                            {"id": "rfc", "name": "Constancia RFC", "price_info": "$100"},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return Catalog(path)


def test_synonym_inside_sentence_matches(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))

    service, _ = nlu.deduce("hola, ocupo mi clave unica por favor")  # sin acento, da igual

    assert service is not None and service.id == "curp"


def test_fuzzy_tolerates_typos(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))

    service, _ = nlu.deduce("nesesito mi curp sertificada porfa")

    assert service is not None and service.id == "curp"


def test_llm_refusal_returns_nothing(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(stop_reason="refusal", content=[])
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    assert nlu.deduce("texto cualquiera sin match") == (None, None)


def test_llm_ambos_null_cae_a_keywords(monkeypatch):
    # REGLA (paso 1 del plan RAG): "ambos null" del LLM ya no es veredicto
    # final — las capas literales ven sinónimos aprendidos que el prompt no
    # lleva, así que se intentan igual antes de rendirse en "ninguna".
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": None, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    service, category, capa, _ = nlu.deduce_explained("necesito un acta de divorcio")

    assert service is not None and "divorcio" in service.name.lower()
    assert category is None
    assert capa in {"nombre", "sinonimo"}  # rescató una capa literal, no el LLM


def test_llm_ambos_null_sin_match_literal_sigue_siendo_ninguna(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": None, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    assert nlu.deduce_explained("zzz qqq no relacionado")[:2] == (None, None)
