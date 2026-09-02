"""La imagen se construye desde requirements.txt (Dockerfile:3-4): lo que el venv
local tenga de más no cuenta. `cryptography` ya vivía ahí como huérfana
(Required-by vacío), así que `pytest -q` verde no probaba nada sobre la imagen.
Estas pruebas sí. Estilo tests/test_harness.py: leen archivos del repo como datos."""
import re
from importlib import metadata
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
REQUIREMENTS = RAIZ / "requirements.txt"
PIN_CRYPTOGRAPHY = "cryptography==45.0.7"  # docs/SPEC-MULTI-OFICINA.md §3.1


def test_cryptography_esta_pineada_en_requirements():
    lineas = [l.strip() for l in REQUIREMENTS.read_text(encoding="utf-8").splitlines()]
    assert lineas.count(PIN_CRYPTOGRAPHY) == 1, (
        f"requirements.txt debe traer exactamente una línea {PIN_CRYPTOGRAPHY!r}"
    )


def test_cryptography_del_entorno_coincide_con_el_pin():
    # En CI la versión solo puede venir del pin; en local obliga a que la
    # instalación huérfana sea la misma que llegará a la imagen.
    assert metadata.version("cryptography") == PIN_CRYPTOGRAPHY.split("==")[1]


def test_ningun_modulo_de_app_importa_cryptography_a_nivel_de_modulo():
    """PR 1: nada cifra todavía. Un import a nivel de módulo en app/ haría que un
    venv sin reinstalar tumbara TODA la suite (conftest importa app.db) y
    escondería el olvido del pin tras un ImportError ajeno. El PR 2 lo relaja a
    import perezoso dentro de _cifrar/_descifrar."""
    patron = re.compile(r"^(?:import|from)\s+cryptography\b", re.M)
    culpables = sorted(
        p.name for p in (RAIZ / "app").glob("*.py")
        if patron.search(p.read_text(encoding="utf-8"))
    )
    assert culpables == [], f"import de cryptography a nivel de módulo en: {culpables}"
