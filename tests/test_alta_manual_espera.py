"""B4 de ext-cse — la espera del lote (`alta_manual_espera_s`).

Es el silencio que el agente deja pasar antes de preguntarle al colaborador por el número del
cliente. Abajo de 10 s le habla en medio de una ráfaga de reenvíos; arriba de 120 s el colaborador
cree que el agente no lo vio. El barandal vive en el arranque Y en el panel, con las mismas cotas:
un barandal que solo vive en la UI no lo es (el .env entra sin que nadie lo mire).
"""

import pytest

from app import agent_settings
from app.config import Settings


@pytest.fixture(autouse=True)
def _sin_env(monkeypatch):
    monkeypatch.delenv("ALTA_MANUAL_ESPERA_S", raising=False)


def test_nace_en_30_segundos():
    assert Settings().alta_manual_espera_s == 30


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [("0", 10), ("5", 10), ("9", 10), ("10", 10), ("30", 30), ("120", 120), ("121", 120), ("999", 120), ("-4", 10)],
)
def test_se_acota_al_arrancar(monkeypatch, crudo, esperado):
    monkeypatch.setenv("ALTA_MANUAL_ESPERA_S", crudo)
    assert Settings().alta_manual_espera_s == esperado


def test_un_valor_que_no_es_numero_no_tumba_el_arranque(monkeypatch):
    monkeypatch.setenv("ALTA_MANUAL_ESPERA_S", "veinte")
    assert Settings().alta_manual_espera_s == 30


def test_es_editable_con_las_mismas_cotas():
    p = agent_settings.CONFIG_EDITABLE["alta_manual_espera_s"]
    assert (p.tipo, p.minimo, p.maximo) == ("entero", 10, 120)
    assert p.unidad == " s"
    assert p.ayuda


@pytest.mark.parametrize("crudo", ["0", "5", "9", "121", "999"])
def test_el_panel_rechaza_fuera_de_rango(crudo):
    with pytest.raises(ValueError, match="10 a 120"):
        agent_settings._convertir(agent_settings.CONFIG_EDITABLE["alta_manual_espera_s"], crudo)


def test_el_panel_acepta_los_extremos():
    p = agent_settings.CONFIG_EDITABLE["alta_manual_espera_s"]
    assert agent_settings._convertir(p, "10") == 10
    assert agent_settings._convertir(p, "120") == 120
