import json
from types import SimpleNamespace

import pytest

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


def test_llm_ambos_null_cae_a_capas_baratas(monkeypatch, tmp_path):
    # REGLA (paso 1 del plan RAG): "ambos null" del LLM ya no es veredicto
    # final — las capas de abajo ven sinónimos aprendidos que el prompt no
    # lleva, así que se intentan igual antes de rendirse en "ninguna".
    # Con el atajo exacto por delante, un nombre bien escrito ya ni llega al
    # LLM; el rescate que queda por probar aquí es el de la capa difusa, así
    # que la frase va con faltas de ortografía a propósito.
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": None, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    service, category, capa, _ = nlu.deduce_explained(CON_TYPOS)

    assert service is not None and service.id == "curp"
    assert category is None
    assert capa == "fuzzy"  # rescató una capa barata, no el LLM


def test_llm_ambos_null_sin_match_literal_sigue_siendo_ninguna(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": None, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    assert nlu.deduce_explained("zzz qqq no relacionado")[:2] == (None, None)


# --- Atajo determinista: lo que el catálogo resuelve no paga LLM ---


def _explota(*args, **kwargs):
    raise AssertionError("el matching exacto ya resolvió: ningún LLM debe correr")


def _sin_llms(monkeypatch):
    """Ambas capas LLM configuradas pero envenenadas: si alguna se llega a
    invocar, la prueba truena en vez de pasar por casualidad."""
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr("httpx.post", _explota)
    monkeypatch.setattr(nlu, "_client", SimpleNamespace(
        messages=SimpleNamespace(create=_explota)
    ))


def test_nombre_del_catalogo_no_paga_llm(monkeypatch):
    _sin_llms(monkeypatch)
    svc = _first_service()

    service, category, capa, confianza = nlu.deduce_explained(f"quiero {svc.name}")

    assert service is not None and service.id == svc.id
    assert (category, capa, confianza) == (None, "nombre", None)


def test_sinonimo_del_catalogo_no_paga_llm(monkeypatch, tmp_path):
    _sin_llms(monkeypatch)
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))

    service, _, capa, _ = nlu.deduce_explained("hola, ocupo mi clave unica por favor")

    assert service is not None and service.id == "curp"
    assert capa == "sinonimo"


def test_frase_sin_match_exacto_si_llega_al_llm(monkeypatch):
    # Cara B: el atajo no puede tragarse la cascada entera. Sin esta prueba,
    # un atajo demasiado goloso dejaría los LLM muertos y nadie se enteraría.
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    svc = _first_service()
    llamado = {}

    def _fake_post(url, **kw):
        llamado["si"] = True
        return _fake_ollama_response({"service_id": svc.id, "category_id": None})

    monkeypatch.setattr("httpx.post", _fake_post)

    service, _, capa, _ = nlu.deduce_explained("ese asunto pendiente que traigo")

    assert llamado.get("si") is True
    assert service is not None and service.id == svc.id
    assert capa == "ollama"


def test_ambiguedad_literal_no_la_resuelve_el_atajo(monkeypatch, tmp_path):
    # Dos servicios comparten el MISMO sinónimo: un empate es ambigüedad real,
    # no un acierto. El atajo debe abstenerse y dejar decidir al modelo, que
    # sí lee el contexto de la frase.
    from app.catalog import Catalog

    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"categories": [{"id": "c", "name": "Cat", "services": [
            {"id": "a", "name": "Tramite A", "price_info": "$1", "synonyms": ["el papel"]},
            {"id": "b", "name": "Tramite B", "price_info": "$2", "synonyms": ["el papel"]},
        ]}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(nlu, "catalog", Catalog(path))
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _fake_ollama_response({"service_id": "b", "category_id": None}),
    )

    service, _, capa, _ = nlu.deduce_explained("necesito el papel ese")

    assert service is not None and service.id == "b"
    assert capa == "ollama"


# --- Capa Ollama (modelo abierto local) ---


def _fake_ollama_response(payload: dict):
    return SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"message": {"content": json.dumps(payload)}},
    )


def test_ollama_result_wins_with_capa_ollama(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    svc = _first_service()
    calls = {}

    def _fake_post(url, **kw):
        calls["url"] = url
        calls["json"] = kw["json"]
        return _fake_ollama_response({"service_id": svc.id, "category_id": None})

    monkeypatch.setattr("httpx.post", _fake_post)

    service, category, capa, _ = nlu.deduce_explained("quiero ese trámite que me dijeron")

    assert service is not None and service.id == svc.id
    assert category is None
    assert capa == "ollama"
    assert calls["url"] == "http://ollama.test/api/chat"
    assert calls["json"]["think"] is False  # sin razonamiento: latencia mínima


def test_ollama_sends_bearer_token(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(settings, "ollama_token", "tok-secreto")
    svc = _first_service()
    seen = {}

    def _fake_post(url, **kw):
        seen["headers"] = kw.get("headers", {})
        return _fake_ollama_response({"service_id": svc.id, "category_id": None})

    monkeypatch.setattr("httpx.post", _fake_post)

    nlu.deduce("cualquier texto largo")

    assert seen["headers"]["Authorization"] == "Bearer tok-secreto"


# Frase con faltas de ortografía: NINGÚN término del catálogo de prueba
# aparece literal, así que el atajo exacto no la resuelve y la cascada llega
# de verdad a las capas LLM. Con una frase bien escrita estas pruebas serían
# vacuas: el atajo contestaría antes de que el modelo se entere.
CON_TYPOS = "nesesito mi curp sertificada porfa"


def test_ollama_hallucinated_id_falls_to_keywords(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _fake_ollama_response(
            {"service_id": "no_existe_xyz", "category_id": None}
        ),
    )

    service, _, capa, _ = nlu.deduce_explained(CON_TYPOS)

    # El id inventado se descarta y el difuso rescata la respuesta.
    assert service is not None and service.id == "curp"
    assert capa == "fuzzy"


def test_ollama_null_answer_does_not_stop_the_chain(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _fake_ollama_response({"service_id": None, "category_id": None}),
    )

    service, _ = nlu.deduce(CON_TYPOS)

    assert service is not None and service.id == "curp"  # el "no sé" no apaga el resto


def test_ollama_failure_falls_to_keywords(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))
    llamado = {}

    def _boom(url, **kw):
        llamado["si"] = True
        raise RuntimeError("ollama caído simulado")

    monkeypatch.setattr("httpx.post", _boom)

    service, _ = nlu.deduce(CON_TYPOS)

    assert llamado.get("si") is True, "la frase no llegó a Ollama: prueba vacua"
    assert service is not None and service.id == "curp"


def test_ollama_failure_lets_claude_rescue(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")

    def _boom(url, **kw):
        raise RuntimeError("ollama caído simulado")

    monkeypatch.setattr("httpx.post", _boom)
    svc = _first_service()
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": svc.id, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    service, category, capa, _ = nlu.deduce_explained("quiero ese trámite que me dijeron")

    assert service is not None and service.id == svc.id
    assert capa == "llm"


def test_ollama_null_lets_claude_rescue(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _fake_ollama_response({"service_id": None, "category_id": None}),
    )
    svc = _first_service()
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": svc.id, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    service, _, capa, _ = nlu.deduce_explained("quiero ese trámite que me dijeron")

    assert service is not None and service.id == svc.id
    assert capa == "llm"  # el "no sé" del modelo local dejó decidir a Claude


def test_ollama_null_y_claude_null_dejan_rescatar_a_las_capas_baratas(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _fake_ollama_response({"service_id": None, "category_id": None}),
    )
    fake = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: _fake_response({"service_id": None, "category_id": None})
        )
    )
    monkeypatch.setattr(nlu, "_client", fake)

    # Con el catálogo de prueba el difuso SÍ rescata esta frase: main cambió
    # la semántica y un null de Claude ya no cierra la cadena, así que aquí
    # se fija el comportamiento NUEVO (rescate), no el viejo "ninguna".
    monkeypatch.setattr(nlu, "catalog", _catalog_with_synonyms(tmp_path))
    service, _, capa, _ = nlu.deduce_explained(CON_TYPOS)

    assert service is not None and service.id == "curp"
    assert capa == "fuzzy"


def test_ollama_wins_over_claude_when_both_configured(monkeypatch):
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    svc = _first_service()
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _fake_ollama_response({"service_id": svc.id, "category_id": None}),
    )

    def _claude_no_debe_llamarse(**kw):
        raise AssertionError("Claude no debe llamarse si Ollama ya decidió")

    fake = SimpleNamespace(messages=SimpleNamespace(create=_claude_no_debe_llamarse))
    monkeypatch.setattr(nlu, "_client", fake)

    service, _, capa, _ = nlu.deduce_explained("quiero ese trámite que me dijeron")

    assert service is not None and service.id == svc.id
    assert capa == "ollama"


# --- Clasificación de códigos sueltos (Ollama) ---
# Los tres códigos de la prueba real en producción: tienen forma de CURP pero
# no cuadran con el patrón (17 caracteres, sin el H/M del sexo).
CODIGOS_RAROS = ["CUSE234543KPLRT02", "COUE237643KPLRT02", "COSE234543KPLRT00"]


def test_clasificar_codigos_devuelve_el_tipo_del_modelo(monkeypatch):
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    calls = {}

    def _fake_post(url, **kw):
        calls["url"] = url
        calls["mensaje"] = kw["json"]["messages"][-1]["content"]
        return _fake_ollama_response(
            {"codigos": [{"codigo": c, "tipo": "CURP"} for c in CODIGOS_RAROS]}
        )

    monkeypatch.setattr("httpx.post", _fake_post)

    assert nlu.clasificar_codigos(CODIGOS_RAROS) == dict.fromkeys(CODIGOS_RAROS, "CURP")
    assert calls["url"] == "http://ollama.test/api/chat"
    assert calls["mensaje"] == "\n".join(CODIGOS_RAROS)


def test_clasificar_codigos_descarta_lo_que_el_modelo_se_invento(monkeypatch):
    # Mismo criterio que _validated con los ids del catálogo: un código que no
    # se mandó, o un tipo que no existe, no entra.
    monkeypatch.setattr(settings, "ollama_url", "http://ollama.test")
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kw: _fake_ollama_response(
            {
                "codigos": [
                    {"codigo": CODIGOS_RAROS[0], "tipo": "CURP"},
                    {"codigo": CODIGOS_RAROS[1], "tipo": "PASAPORTE"},  # tipo inventado
                    {"codigo": "NUNCA-SE-MANDO", "tipo": "RFC"},        # código inventado
                    {"codigo": CODIGOS_RAROS[2], "tipo": None},         # "no sé"
                ]
            }
        ),
    )

    assert nlu.clasificar_codigos(CODIGOS_RAROS) == {CODIGOS_RAROS[0]: "CURP"}


@pytest.mark.parametrize(
    "url, revienta",
    [
        ("", False),                 # sin OLLAMA_URL no hay a quién preguntarle
        ("http://ollama.test", True),  # servidor caído o lento
    ],
)
def test_clasificar_codigos_nunca_revienta(monkeypatch, url, revienta):
    monkeypatch.setattr(settings, "ollama_url", url)
    if revienta:
        def _explota(*a, **kw):
            raise RuntimeError("servidor caído")

        monkeypatch.setattr("httpx.post", _explota)

    # {} = "no pude verificar": quien llama sigue adelante y crea el folio.
    assert nlu.clasificar_codigos(CODIGOS_RAROS) == {}
