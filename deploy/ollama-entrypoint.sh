#!/bin/sh
# Arranque del servicio `ollama` del docker-compose.yml (perfil `ollama`):
# levanta el servidor y, ya vivo, descarga el modelo de OLLAMA_MODEL si falta
# (idempotente: si ya está, `pull` termina en segundos). Un fallo de descarga
# es ADVERTENCIA en el log, nunca tumba el servicio ni el deploy. SIGTERM se
# reenvía al servidor para que `docker stop` sea limpio; si el servidor muere,
# el script termina y `restart: unless-stopped` lo vuelve a levantar.
# Vive en el repo (montado de solo lectura) y no dentro del YAML a propósito:
# aquí `$` es `$`, y tests/test_ollama_entrypoint.py lo corre con un ollama falso.
set -u
OLLAMA_BIN=${OLLAMA_BIN:-/bin/ollama}
ESPERA_SEGUNDOS=${OLLAMA_ESPERA_ARRANQUE:-60}

"$OLLAMA_BIN" serve &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null' TERM INT

i=0
while [ "$i" -lt "$ESPERA_SEGUNDOS" ]; do
  if "$OLLAMA_BIN" ls >/dev/null 2>&1; then break; fi
  i=$((i + 1))
  sleep 1
done

if [ -n "${OLLAMA_MODEL:-}" ]; then
  "$OLLAMA_BIN" pull "$OLLAMA_MODEL" \
    || echo "$(date -Is) ADVERTENCIA: no se pudo descargar $OLLAMA_MODEL; el servidor sigue arriba sin modelo" >&2
fi

wait "$pid"
wait "$pid" 2>/dev/null
