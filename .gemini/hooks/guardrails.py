#!/usr/bin/env python3
"""Barandales del operador para Gemini CLI — hook `BeforeTool`.

Espejo del `deny` de `.claude/settings.json`: el humano corre los `git push`,
`ssh` y deploys en su propia terminal, y el agente no lee `.env` ni escribe
datos de producción. En Claude Code esa política la sostiene la plataforma; en
Gemini CLI la sostiene este hook, y `tests/test_harness.py` exige que los dos
digan lo mismo.

Por qué un hook y no `tools.exclude` ni `.gemini/policies/` (verificado sobre
el código de Gemini CLI 0.58.0, no sobre la documentación):
- `tools.exclude` está deprecado —la CLI lo avisa al arrancar— y una entrada
  con paréntesis como `run_shell_command(git push)` se convierte en una regla
  cuyo `toolName` se compara por igualdad exacta contra `run_shell_command`:
  nunca casa, así que no bloquea nada.
- Las políticas de workspace (`.gemini/policies/`) vienen apagadas por código
  (`disableWorkspacePolicies = true`). El TOML de al lado existe para cuando
  se enciendan o para que el operador lo active con `policyPaths`.
- Los hooks de proyecto sí corren (en carpeta confiable), se concatenan con
  los del usuario y un `{"decision": "deny"}` en stdout bloquea la llamada.

Contrato con Gemini: recibe por stdin un JSON con `hook_event_name`,
`tool_name`, `tool_input` y `cwd`. Si la llamada está prohibida imprime
`{"decision": "deny", "reason": ...}` y sale con 0; si no, no imprime nada
(salida vacía + exit 0 = permitir). Un JSON ilegible se deja pasar: un hook
roto no debe dejar a Gemini inservible, y el caso lo cubre el test, no este
script. Solo biblioteca estándar: corre con el `python3` del sistema, fuera
del venv.
"""

from __future__ import annotations

import json
import os
import re
import sys

# --- La lista, una sola vez. Cada entrada tiene su gemela en el `deny` de ---
# --- .claude/settings.json; tests/test_harness.py lo comprueba.           ---

# Bash(<prefijo>:*) — se compara por prefijo de comando, como hace Gemini con
# sus propias reglas: el comando es igual al prefijo o empieza por él seguido
# de un espacio. Cada tramo de un comando encadenado (&&, ||, ;, |) se revisa
# por separado, para que `git status && git push` no se cuele.
PREFIJOS_SHELL = ("git push", "scripts/ship.sh", "ssh", "scp", "rm -rf")

# Read(./x) — rutas relativas a la raíz del repo. `.env.example` sigue
# legible a propósito: es plantilla, no secreto.
LECTURA_PROHIBIDA = (".env", ".env.old", ".env.bk")

# Edit(./x) / Write(./x) — un sufijo "/" prohíbe el directorio entero. Las dos
# últimas no están en .claude/settings.json porque allá las cubre la
# plataforma: el agente no puede reescribir su propia política.
ESCRITURA_PROHIBIDA = (
    ".env",
    "tramites.db",
    "data/",
    ".agents/",
    "marketing/",
    "HTTP",
    ".gemini/",
    ".claude/settings.json",
)

HERRAMIENTAS_SHELL = {"run_shell_command"}
HERRAMIENTAS_LECTURA = {"read_file", "read_many_files"}
HERRAMIENTAS_ESCRITURA = {"write_file", "replace"}

_SEPARADORES = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


def _tramos(comando: str) -> list[str]:
    """Cada comando de una cadena, con el espacio normalizado y sin `./`."""
    tramos = []
    for tramo in _SEPARADORES.split(comando):
        tramo = " ".join(tramo.split())
        if tramo.startswith("./"):
            tramo = tramo[2:]
        if tramo:
            tramos.append(tramo)
    return tramos


def _prefijo_prohibido(comando: str) -> str | None:
    for tramo in _tramos(comando):
        for prefijo in PREFIJOS_SHELL:
            if tramo == prefijo or tramo.startswith(prefijo + " "):
                return prefijo
    return None


def _rutas(entrada: dict) -> list[str]:
    """Toda ruta que la herramienta reciba, venga con el nombre que venga."""
    rutas: list[str] = []
    for clave in ("file_path", "absolute_path", "path", "dir_path"):
        valor = entrada.get(clave)
        if isinstance(valor, str):
            rutas.append(valor)
    for clave in ("paths", "include"):
        valor = entrada.get(clave)
        if isinstance(valor, list):
            rutas.extend(v for v in valor if isinstance(v, str))
    return rutas


def _relativa(ruta: str, raiz: str) -> str:
    absoluta = os.path.abspath(os.path.join(raiz, ruta))
    return os.path.relpath(absoluta, raiz).replace(os.sep, "/")


def _lectura_prohibida(rel: str) -> bool:
    return rel in LECTURA_PROHIBIDA


def _escritura_prohibida(rel: str) -> str | None:
    for patron in ESCRITURA_PROHIBIDA:
        if patron.endswith("/"):
            if (rel + "/").startswith(patron):
                return patron
        elif rel == patron:
            return patron
    return None


def evaluar(evento: dict) -> str | None:
    """La razón para negar la llamada, o None si pasa."""
    if evento.get("hook_event_name", "BeforeTool") != "BeforeTool":
        return None
    herramienta = evento.get("tool_name", "")
    entrada = evento.get("tool_input") or {}
    if not isinstance(entrada, dict):
        return None
    raiz = evento.get("cwd") or os.getcwd()

    if herramienta in HERRAMIENTAS_SHELL:
        prefijo = _prefijo_prohibido(str(entrada.get("command", "")))
        if prefijo:
            return (
                f"Barandal del operador: `{prefijo}` lo corre el humano en su propia "
                "terminal. Prepara el comando listo para pegar y díselo (AGENTS.md → Deploy)."
            )
        return None

    if herramienta in HERRAMIENTAS_LECTURA:
        for ruta in _rutas(entrada):
            if _lectura_prohibida(_relativa(ruta, raiz)):
                return (
                    f"Barandal del operador: `{_relativa(ruta, raiz)}` no se lee entero ni se "
                    "imprime. Si necesitas saber si una variable existe, cuenta con `grep -c`."
                )
        return None

    if herramienta in HERRAMIENTAS_ESCRITURA:
        for ruta in _rutas(entrada):
            rel = _relativa(ruta, raiz)
            patron = _escritura_prohibida(rel)
            if patron:
                return (
                    f"Barandal del operador: `{rel}` está bajo `{patron}`, que el agente no "
                    "escribe (datos de producción, secretos, directorios del operador o la "
                    "política del harness). Pídeselo al operador."
                )
        return None

    return None


def main() -> int:
    try:
        evento = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(evento, dict):
        return 0
    razon = evaluar(evento)
    if razon:
        print(json.dumps({"decision": "deny", "reason": razon}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
