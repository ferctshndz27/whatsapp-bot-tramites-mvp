#!/usr/bin/env python3
"""Benchmark de la cascada NLU contra el corpus dorado y el Ollama REAL.

Responde con números lo que la suite de pytest no puede: cuánto acierta y
cuánto TARDA el modelo abierto en la vida real. pytest mide las capas offline
con el LLM apagado (determinista, sin costo, sin red); esto sale a labs y
mide la cascada completa tal como la vive un cliente de WhatsApp.

Corre a mano, NUNCA en CI: depende de un servicio externo, tarda minutos y no
es determinista.

    python scripts/eval_nlu.py                  # cascada completa (con Ollama)
    python scripts/eval_nlu.py --sin-ollama     # línea base: cuánto aporta la capa
    python scripts/eval_nlu.py --json out.json  # además guarda el detalle

El corpus es el MISMO de tests/test_nlu_regression.py (fuente única): las
frases reales con faltas de dedo y el service_id que el negocio espera.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from sqlmodel import SQLModel, create_engine  # noqa: E402

import app.db as db  # noqa: E402
from app import nlu  # noqa: E402
from app.catalog import SEED_PATH, catalog  # noqa: E402
from app.config import settings  # noqa: E402
from tests.test_nlu_regression import CORPUS, MIN_ACCURACY  # noqa: E402


def _catalogo_de_produccion() -> None:
    """Base en memoria sembrada desde el JSON semilla del repo — el mismo
    catálogo real que mide tests/test_nlu_regression.py.

    Así el benchmark corre en cualquier máquina sin depender de la base local
    del desarrollador y sin tocarla: Catalog importa el JSON en el primer
    acceso a .categories."""
    db.engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(db.engine)
    settings.catalog_path = str(SEED_PATH)


def _percentil(valores: list[float], p: float) -> float:
    """p50/p90 sin numpy: el corpus son decenas de casos, no millones."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    indice = min(int(round(p * (len(ordenados) - 1))), len(ordenados) - 1)
    return ordenados[indice]


def _medir(caso) -> dict:
    inicio = time.perf_counter()
    service, category, capa, confianza = nlu.deduce_explained(caso.texto)
    ms = (time.perf_counter() - inicio) * 1000

    obtenido = service.id if service else (category.id if category else None)
    sugerencias = [s.id for s in nlu.suggestions(caso.texto)]

    # Mismo criterio que test_corpus_caso_por_caso: lo que el NEGOCIO espera
    # de cada tipo de frase, no una igualdad ciega contra `espera`.
    if caso.tipo == "ruido":
        # Inventarle un trámite a "jajaja" le cotiza algo que nadie pidió.
        acierto = obtenido is None and not sugerencias
    elif caso.tipo == "vaga":
        # "quiero un acta": no debe elegir a ciegas — debe ofrecer el mini-menú
        # con la opción correcta dentro.
        acierto = obtenido is None and len(sugerencias) >= 2 and caso.espera in sugerencias
    elif caso.tipo == "categoria":
        acierto = obtenido == caso.espera and service is None
    elif caso.resuelve:
        acierto = obtenido == caso.espera
    elif obtenido not in (None, caso.espera):
        # Hueco conocido de catálogo/sinónimos: la regla dura es no inventar
        # OTRO servicio (eso cotiza el trámite equivocado).
        acierto = False
    elif obtenido == caso.espera:
        # Hueco conocido que HOY sí resuelve (un sinónimo aprobado, el modelo
        # local): es la mejora que el corpus promete, no un fallo.
        acierto = True
    else:
        # Sin deducción, el acierto es que el cliente vea su opción en el
        # mini-menú (o que no vea una lista sin su trámite).
        acierto = (
            caso.espera in sugerencias
            if caso.en_sugerencias
            else (not sugerencias or caso.espera in sugerencias)
        )

    return {
        "texto": caso.texto,
        "tipo": caso.tipo,
        "espera": caso.espera,
        "obtenido": obtenido,
        "capa": capa,
        "confianza": confianza,
        "sugerencias": sugerencias,
        "acierto": acierto,
        "ms": round(ms, 1),
    }


def _tabla_por_capa(filas: list[dict]) -> list[tuple]:
    capas = sorted({f["capa"] for f in filas})
    tabla = []
    for capa in capas:
        propias = [f for f in filas if f["capa"] == capa]
        tiempos = [f["ms"] for f in propias]
        aciertos = sum(1 for f in propias if f["acierto"])
        tabla.append(
            (
                capa,
                len(propias),
                round(aciertos / len(propias) * 100, 1),
                round(_percentil(tiempos, 0.5)),
                round(_percentil(tiempos, 0.9)),
            )
        )
    return tabla


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sin-ollama",
        action="store_true",
        help="apaga la capa local para medir la línea base de las capas gratis",
    )
    parser.add_argument("--json", metavar="RUTA", help="guarda el detalle caso por caso")
    args = parser.parse_args()

    _catalogo_de_produccion()
    if not catalog.categories:
        print(
            f"ERROR: el catálogo semilla ({SEED_PATH}) no sembró ningún servicio.",
            file=sys.stderr,
        )
        return 1

    if args.sin_ollama:
        settings.ollama_url = ""
    if not args.sin_ollama and not settings.ollama_url:
        print(
            "ERROR: OLLAMA_URL no está configurada en el .env — no hay capa local\n"
            "que medir. Usa --sin-ollama para la línea base.",
            file=sys.stderr,
        )
        return 1

    servicios = sum(len(c.services) for c in catalog.categories)
    print(f"Catálogo: {servicios} servicios en {len(catalog.categories)} categorías")
    print(f"Modelo local: {settings.ollama_model if settings.ollama_url else '(apagado)'}")
    print(f"Claude: {'sí' if settings.anthropic_api_key else 'no'}")
    print(f"Umbral difuso: {settings.fuzzy_threshold}")
    print(f"Casos: {len(CORPUS)}\n")

    filas = []
    for i, caso in enumerate(CORPUS, start=1):
        fila = _medir(caso)
        filas.append(fila)
        marca = "ok   " if fila["acierto"] else "FALLA"
        print(
            f"[{i:3}/{len(CORPUS)}] {marca} {fila['ms']:7.0f}ms "
            f"{fila['capa']:<10} {caso.texto[:52]}"
        )

    print()
    servicio = [f for f in filas if f["tipo"] == "servicio"]
    aciertos = sum(1 for f in servicio if f["acierto"])
    precision = round(aciertos / len(servicio) * 100, 2) if servicio else 0.0
    # Resolución DIRECTA: la métrica que vigila MIN_ACCURACY en la suite (el
    # caso cae al mini-menú no cuenta). Es la comparable con el piso histórico.
    directos = sum(1 for f in servicio if f["obtenido"] == f["espera"])
    directa = round(directos / len(servicio) * 100, 2) if servicio else 0.0
    tiempos = [f["ms"] for f in filas]

    print(f"RESOLUCIÓN DIRECTA (piso de la suite {MIN_ACCURACY}%): "
          f"{directos}/{len(servicio)} = {directa}%")
    print(f"ACIERTO DE NEGOCIO (incluye mini-menú correcto): "
          f"{aciertos}/{len(servicio)} = {precision}%")
    print(
        f"Latencia global: p50 {round(_percentil(tiempos, 0.5))}ms  "
        f"p90 {round(_percentil(tiempos, 0.9))}ms  "
        f"media {round(statistics.mean(tiempos))}ms"
    )
    print(f"Tiempo total: {round(sum(tiempos) / 1000, 1)}s\n")

    print(f"{'capa':<12} {'casos':>6} {'acierto':>8} {'p50 ms':>8} {'p90 ms':>8}")
    for capa, n, pct, p50, p90 in _tabla_por_capa(filas):
        print(f"{capa:<12} {n:>6} {pct:>7}% {p50:>8} {p90:>8}")

    fallos = [f for f in filas if not f["acierto"]]
    if fallos:
        print(f"\nFALLOS ({len(fallos)}):")
        for f in fallos:
            print(f"  «{f['texto']}» esperaba {f['espera']} — dio {f['obtenido']} ({f['capa']})")

    if args.json:
        # Crear el directorio si falta y NUNCA perder una corrida por un error
        # de escritura: el benchmark tarda minutos contra el modelo real y su
        # resultado ya se imprimió arriba.
        destino = Path(args.json)
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text(
                json.dumps(
                    {
                        "modelo": settings.ollama_model if settings.ollama_url else None,
                        "umbral_difuso": settings.fuzzy_threshold,
                        "resolucion_directa": directa,
                        "acierto_negocio": precision,
                        "p50_ms": round(_percentil(tiempos, 0.5)),
                        "p90_ms": round(_percentil(tiempos, 0.9)),
                        "casos": filas,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nDetalle guardado en {destino}")
        except OSError as exc:
            print(f"\nNo se pudo guardar el detalle en {destino}: {exc}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
