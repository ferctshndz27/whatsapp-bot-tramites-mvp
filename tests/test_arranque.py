"""El arranque de la app: que corra, y que corra ENTERO.

Hueco que este archivo cierra: `on_startup()` es lo que deja la base utilizable
—`init_db()` crea las tablas en un contenedor recién creado— y hasta ahora
ningún test comprobaba que llegara a ejecutarse. Los dos que lo mencionan
(`test_security.py`, `test_e2e_conversation.py`) lo hacen para EVITARLO, porque
tocaría MSSQL y el backfill.

Eso deja un modo de fallo caro y silencioso: un `lifespan` mal conectado no da
error ni rompe ninguna prueba — sencillamente no corre, y el primer síntoma es
un contenedor nuevo en el VPS con la base vacía. `deploy/pull-deploy.sh` haría
rollback por el health check, así que no hay caída permanente, pero el
diagnóstico empieza de cero cada vez.
"""

import app.main as main
from fastapi.testclient import TestClient


def test_el_arranque_se_ejecuta_al_levantar_la_app(monkeypatch):
    """REGLA: entrar al contexto de la app corre `on_startup()`.

    Se espía la función en vez de mirar su efecto (tablas creadas) a propósito:
    lo que se prueba es el CABLEADO del lifespan, y una base que ya tuviera las
    tablas por otra vía haría pasar la prueba con el cable suelto.
    """
    llamadas = []
    monkeypatch.setattr(main, "on_startup", lambda: llamadas.append("arrancó"))

    with TestClient(main.app):
        pass

    assert llamadas == ["arrancó"], (
        "el lifespan no llamó a on_startup(): un contenedor nuevo arrancaría con "
        "la base sin tablas y el fallo solo se vería en el VPS"
    )


def test_el_arranque_no_se_traga_sus_errores(monkeypatch):
    """REGLA: si el arranque falla, el proceso NO debe quedar atendiendo tráfico.

    `deploy/pull-deploy.sh` cuenta con que `/health` no responda para disparar el
    rollback; un `try/except` alrededor del arranque dejaría un contenedor vivo,
    sano a ojos del health check, y sirviendo con la base a medias.
    """
    def _revienta():
        raise RuntimeError("no such table: message")

    monkeypatch.setattr(main, "on_startup", _revienta)

    try:
        with TestClient(main.app):
            pass
    except RuntimeError as exc:
        assert "no such table" in str(exc)
    else:
        raise AssertionError(
            "el arranque falló y la app quedó atendiendo igual: el rollback de "
            "pull-deploy.sh nunca se dispararía"
        )


def test_on_startup_conserva_su_nombre():
    """El nombre es carga documental: `.claude/skills/devops-senior/SKILL.md` lo
    cita siete veces y el procedimiento de deploy razona sobre su presupuesto de
    60 s. Renombrarlo al migrar a `lifespan` habría dejado la skill apuntando a
    algo que ya no existe — y el harness solo verifica archivos citados, no
    símbolos."""
    assert callable(getattr(main, "on_startup", None)), (
        "on_startup() desapareció; devops-senior lo cita por nombre"
    )
