#!/usr/bin/env python3
"""Arma la extensión ext-cse para Chrome y para Firefox, cada una en ext-cse/dist/<navegador>/.

    python3 ext-cse/construir.py --crm https://bot.esaumillan.cloud

Cada manifiesto es manifiesto/comun.json más la capa de su navegador (manifiesto/<navegador>.json)
y un solo host: el del CRM (--crm). Así lo que el navegador le enseña a quien la instala es todo lo
que la extensión puede tocar. Para Chrome genera además regla-marco.json, la regla que deja mostrar
el CRM dentro del panel: quita X-Frame-Options y pone la CSP del bot —leída de app/main.py, no
copiada— sin frame-ancestors; fondo-chrome.js se la devuelve con el id de la extensión al
instalarse, que antes no existe.

Primero arma todo en memoria y solo después escribe: un origen inválido, o una CSP que ya no está
donde se espera, no deja un paquete a medias. Solo usa la biblioteca estándar: la extensión no tiene
dependencias de npm ni empaquetador.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
from pathlib import Path
from urllib.parse import urlsplit

AQUI = Path(__file__).resolve().parent
MAIN = AQUI.parent / "app" / "main.py"
NAVEGADORES = ("chrome", "firefox")

# Lo que viaja en cada paquete, y nada más (tests/test_ext_cse_manifiesto.py sigue la cadena de
# referencias y falla si algo falta o sobra).
ARCHIVOS = {
    "chrome": ("fondo-chrome.js", "panel.html", "panel.css", "panel.js", "crm.js"),
    "firefox": ("fondo-firefox.js", "panel.html", "panel.css", "panel.js", "crm.js"),
}

# http solo en local, para probar contra un bot en la máquina: en cualquier otro sitio la cookie de
# sesión viajaría en claro.
LOCALES = {"localhost", "127.0.0.1"}


def origen_del_crm(texto: str) -> str:
    """El origen del CRM (esquema, dominio y puerto), sin barra final."""
    partes = urlsplit(texto.strip())
    host = partes.hostname or ""
    if partes.scheme not in ("https", "http") or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise ValueError(f"--crm debe ser una URL como https://bot.ejemplo.com, no {texto!r}")
    if partes.scheme == "http" and host not in LOCALES:
        raise ValueError(f"--crm debe ir por https (http solo en localhost): {texto!r}")
    if partes.username or partes.password or partes.query or partes.fragment or partes.path not in ("", "/"):
        raise ValueError(f"--crm es solo el origen, sin ruta, usuario ni parámetros: {texto!r}")
    puerto = f":{partes.port}" if partes.port else ""
    return f"{partes.scheme}://{host}{puerto}"


def csp_del_servidor(main: Path = MAIN) -> str:
    """La Content-Security-Policy del literal SECURITY_HEADERS de app/main.py, sin importar la app."""
    for nodo in ast.parse(main.read_text(encoding="utf-8")).body:
        if isinstance(nodo, ast.Assign):
            nombres, valor = nodo.targets, nodo.value
        elif isinstance(nodo, ast.AnnAssign) and nodo.value is not None:
            nombres, valor = [nodo.target], nodo.value
        else:
            continue
        if not any(isinstance(n, ast.Name) and n.id == "SECURITY_HEADERS" for n in nombres):
            continue
        try:
            cabeceras = ast.literal_eval(valor)
        except ValueError:
            raise ValueError(f"SECURITY_HEADERS de {main} ya no es un literal: no puedo leer su CSP") from None
        csp = cabeceras.get("Content-Security-Policy")
        if not csp:
            raise ValueError(f"SECURITY_HEADERS de {main} ya no trae Content-Security-Policy")
        return csp
    raise ValueError(f"no encontré SECURITY_HEADERS en {main}: la regla del marco necesita la CSP del bot")


def sin_frame_ancestors(csp: str) -> str:
    directivas = [d.strip() for d in csp.split(";") if d.strip()]
    return "; ".join(d for d in directivas if d.split()[0].lower() != "frame-ancestors")


def regla_marco(origen: str, csp: str) -> dict:
    host = urlsplit(origen).hostname
    return {
        "id": 1,
        "priority": 1,
        "action": {
            "type": "modifyHeaders",
            "responseHeaders": [
                {"header": "x-frame-options", "operation": "remove"},
                {"header": "content-security-policy", "operation": "set", "value": sin_frame_ancestors(csp)},
            ],
        },
        "condition": {
            # El ancla deja fuera subdominios y otros puertos, que requestDomains sí admitiría.
            "urlFilter": f"|{origen}/",
            "requestDomains": [host],
            "resourceTypes": ["sub_frame"],
            # fondo-chrome.js antepone el id de la extensión. El CRM va porque el login redirige
            # dentro del marco y ahí quien inicia la petición es el propio CRM (spike S1).
            "initiatorDomains": [host],
        },
    }


def _json(ruta: Path) -> dict:
    return json.loads(ruta.read_text(encoding="utf-8"))


def patron_del_sitio(navegador: str, origen: str) -> str:
    """El patrón del permiso de sitio. Firefox no admite puertos en los patrones (MDN, «Match
    patterns»): con puerto no coincide con nada y la consulta del panel a /health cae en CORS. Ahí
    va sin puerto, que vale para cualquiera de ese host; el origen exacto viaja en homepage_url."""
    if navegador == "firefox":
        partes = urlsplit(origen)
        return f"{partes.scheme}://{partes.hostname}/*"
    return f"{origen}/*"


def manifiesto(navegador: str, origen: str) -> dict:
    comun = _json(AQUI / "manifiesto" / "comun.json")
    capa = _json(AQUI / "manifiesto" / f"{navegador}.json")
    # Sin fusión profunda: una clave vive en un solo archivo, y el CRM solo lo pone --crm.
    repetidas = sorted(comun.keys() & capa.keys())
    if repetidas:
        raise ValueError(f"manifiesto/{navegador}.json repite claves de comun.json: {repetidas}")
    for clave in ("host_permissions", "homepage_url"):
        if clave in comun or clave in capa:
            raise ValueError(f"{clave} no se escribe a mano: lo pone --crm")
    # homepage_url es el origen exacto del CRM, del que crm.js saca sus direcciones.
    return {**comun, **capa, "homepage_url": f"{origen}/", "host_permissions": [patron_del_sitio(navegador, origen)]}


def paquete(navegador: str, origen: str) -> dict[str, bytes]:
    """Nombre → contenido de cada archivo del paquete de un navegador."""

    def a_json(dato: dict) -> bytes:
        return (json.dumps(dato, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    archivos = {nombre: (AQUI / "src" / nombre).read_bytes() for nombre in ARCHIVOS[navegador]}
    archivos["manifest.json"] = a_json(manifiesto(navegador, origen))
    if navegador == "chrome":
        archivos["regla-marco.json"] = a_json(regla_marco(origen, csp_del_servidor()))
    return archivos


def _es_borrable(destino: Path) -> bool:
    """Solo se borra una construcción anterior (para que no viaje un archivo viejo) o nada."""
    return not destino.exists() or (destino / "manifest.json").is_file() or not any(destino.iterdir())


def main(argv: list[str] | None = None) -> int:
    lector = argparse.ArgumentParser(description="Arma ext-cse para Chrome y Firefox.")
    lector.add_argument("--crm", required=True, help="origen del CRM, p. ej. https://bot.esaumillan.cloud")
    lector.add_argument("--navegador", choices=(*NAVEGADORES, "ambos"), default="ambos")
    lector.add_argument("--salida", type=Path, default=AQUI / "dist", help="carpeta de salida (def. ext-cse/dist)")
    args = lector.parse_args(argv)
    navegadores = NAVEGADORES if args.navegador == "ambos" else (args.navegador,)
    try:
        origen = origen_del_crm(args.crm)
        paquetes = {n: paquete(n, origen) for n in navegadores}
        ajenas = [args.salida / n for n in navegadores if not _es_borrable(args.salida / n)]
        if ajenas:
            raise ValueError(f"no borro {ajenas[0]}: no parece una construcción de ext-cse (no tiene manifest.json)")
    except ValueError as error:
        print(f"construir.py: {error}", file=sys.stderr)
        return 2
    for navegador, archivos in paquetes.items():
        destino = args.salida / navegador
        if destino.exists():
            shutil.rmtree(destino)
        destino.mkdir(parents=True)
        for nombre, contenido in archivos.items():
            (destino / nombre).write_bytes(contenido)
        print(f"{navegador}: {destino}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
