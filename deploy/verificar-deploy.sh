#!/bin/bash
# shellcheck disable=SC2015
# SC2015 («A && B || C no es if/else»): aquí cada `prueba && ok … || fallo …` es a propósito. ok, aviso
# y fallo solo imprimen y siempre devuelven 0, así que C nunca corre con A verdadera. (CI: ci/shellcheck.sh)
# Verificación de un deploy en el VPS, en tres fases, con un veredicto por paso.
# NO despliega: eso lo hace el cron con pull-deploy.sh. Esto mira antes, espera
# durante y comprueba después, y termina en 1 si algún paso quedó en ❌.
#
#   deploy/verificar-deploy.sh antes                 estado + respaldo de la base
#   deploy/verificar-deploy.sh durante <sha>         espera a que ese commit sirva
#   deploy/verificar-deploy.sh despues <sha>         que sea ese código el que sirve
#
# Cada paso corre en su propio «try»: un fallo se anota y se sigue con el
# siguiente, porque el valor está en ver TODO el cuadro, no en el primer error.
# Lo único que escribe es el respaldo (data/backups/) y una nota de estado
# (data/verificar-deploy.estado) que «despues» compara contra «antes».
#
# Se corre en la terminal del operador, dentro del VPS. Sin `set -e` a
# propósito: aquí el fallo de un paso es un dato, no una razón para parar.
set -uo pipefail

REPO_DIR=${REPO_DIR:-/opt/whatsapp-bot-tramites}
BRANCH=${BRANCH:-production}
CONTENEDOR=${CONTENEDOR:-tramites-bot}
SALUD=${SALUD:-http://127.0.0.1:8000/health}
LOGIN=${LOGIN:-http://127.0.0.1:8000/login}
ESPERA_MAX_SEGUNDOS=${ESPERA_MAX_SEGUNDOS:-600}   # «durante»: 2 min de cron + build en 2 vCPU
DISCO_MINIMO_GB=${DISCO_MINIMO_GB:-2}             # la build reinstala todas las dependencias
ESTADO="$REPO_DIR/data/verificar-deploy.estado"

FALLOS=0
AVISOS=0

ok()    { echo "✅ $1"; }
aviso() { echo "⚠️  $1"; AVISOS=$((AVISOS + 1)); }
fallo() { echo "❌ $1"; FALLOS=$((FALLOS + 1)); }

# «try»: corre el comando; si falla, anota el fallo con las últimas líneas de su
# salida y devuelve 1 para que el llamador decida si el resto tiene sentido.
intentar() {
  local descripcion="$1"; shift
  local salida
  if salida=$("$@" 2>&1); then
    ok "$descripcion"
    return 0
  fi
  fallo "$descripcion → falló"
  [ -n "$salida" ] && echo "$salida" | tail -5 | sed 's/^/     │ /'
  return 1
}

resumen() {
  echo
  if [ "$FALLOS" -gt 0 ]; then
    echo "❌ $FALLOS fallo(s), $AVISOS aviso(s). No sigas hasta entender cada ❌."
    exit 1
  fi
  echo "✅ Sin fallos, $AVISOS aviso(s)."
  exit 0
}

trap 'echo; echo "interrumpido"; exit 130' INT TERM

entrar_al_repo() {
  if ! cd "$REPO_DIR" 2>/dev/null; then
    fallo "no existe $REPO_DIR (¿REPO_DIR=… ?)"
    resumen
  fi
}

# --- utilidades ---------------------------------------------------------------

sha_desplegado() { awk '{print $1}' data/deploy-info 2>/dev/null; }
sha_completo()   { git rev-parse --verify --quiet "$1^{commit}" 2>/dev/null; }
arranque()       { docker inspect -f '{{.State.StartedAt}}' "$CONTENEDOR" 2>/dev/null; }
# Ollama es opcional (perfil `ollama` en .env): ausente es aviso, no fallo;
# presente y caído sí es fallo, porque `restart` debería haberlo levantado.
revisar_ollama() {
  local st; st=$(estado_de tramites-ollama)
  case "$st" in
    running) ok "contenedor tramites-ollama: running";;
    ausente) aviso "contenedor tramites-ollama: ausente (sin COMPOSE_PROFILES=ollama en .env)";;
    *)       fallo "contenedor tramites-ollama: $st";;
  esac
}
estado_de()      { docker inspect -f '{{.State.Status}}' "$1" 2>/dev/null || echo "ausente"; }
log_del_cron()   { crontab -l 2>/dev/null | grep pull-deploy | sed -n 's/.*>>[[:space:]]*\([^[:space:]]*\).*/\1/p' | head -1; }

# --- antes --------------------------------------------------------------------

fase_antes() {
  echo "== antes · $(date -Is) · $REPO_DIR =="

  local head desplegado
  head=$(git rev-parse HEAD 2>/dev/null || echo none)
  desplegado=$(sha_desplegado || true)
  if [ -z "$desplegado" ]; then
    aviso "data/deploy-info no existe: nunca se ha desplegado con este script, o alguien lo borró"
  elif [ "$head" = "$desplegado" ]; then
    ok "HEAD ($(git rev-parse --short HEAD)) es el commit que sirve (data/deploy-info)"
  else
    fallo "HEAD $(git rev-parse --short HEAD) ≠ desplegado ${desplegado:0:7}: hay un deploy fallido pendiente; no le encimes otro"
  fi

  # Solo lo modificado CON seguimiento: es lo único que `reset --hard` borra
  # (.env, data/ y media/ están ignorados y sobreviven; un archivo suelto sin
  # seguimiento también).
  if [ -n "$(git status --porcelain --untracked-files=no 2>/dev/null)" ]; then
    fallo "el árbol de $REPO_DIR tiene cambios locales con seguimiento; el próximo reset --hard los borra:"
    git status --porcelain --untracked-files=no | head -5 | sed 's/^/     │ /'
  else
    ok "árbol limpio (git status, archivos con seguimiento)"
  fi

  for c in "$CONTENEDOR" tramites-mssql; do
    local st; st=$(estado_de "$c")
    if [ "$st" = "running" ]; then ok "contenedor $c: running"; else fallo "contenedor $c: $st"; fi
  done
  revisar_ollama

  intentar "/health responde" timeout 10 curl -sf "$SALUD" -o /dev/null

  local libre
  libre=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9')
  if [ -z "$libre" ]; then
    aviso "no pude medir el disco libre"
  elif [ "$libre" -lt "$DISCO_MINIMO_GB" ]; then
    fallo "solo ${libre} GB libres en /; la build reinstala todo. Antes: docker image prune -f"
  else
    ok "${libre} GB libres en /"
  fi

  respaldar_base
  dueno_de_data

  if intentar "python:3.12-slim descargada (acorta la ventana de build)" timeout 300 docker pull -q python:3.12-slim; then :; else
    # No es un fallo del deploy: la build la baja sola. Se degrada a aviso.
    FALLOS=$((FALLOS - 1)); AVISOS=$((AVISOS + 1))
  fi

  # Nota de estado para «despues»: qué commit servía y cuándo arrancó el contenedor.
  {
    echo "HEAD=$head"
    echo "ARRANQUE=$(arranque)"
    echo "FECHA=$(date -Is)"
  } > "$ESTADO" 2>/dev/null && ok "estado guardado en $ESTADO" || aviso "no pude escribir $ESTADO"

  resumen
}

dueno_de_data() {
  # Desde INFRA-5 el contenedor corre con un uid fijo y data/ es un bind mount:
  # si el host no le dio data/ a ese uid, el contenedor no escribe la base y el
  # deploy hace rollback cada dos minutos. Se lee el uid del Dockerfile que va a
  # correr: el del checkout si ya trae USER, o el de origin/$BRANCH si el que
  # viene lo estrena.
  local uid
  uid=$(grep -hoE -- '--uid[[:space:]]+[0-9]+' Dockerfile 2>/dev/null | grep -oE '[0-9]+' | head -1)
  [ -z "$uid" ] && uid=$(git show "origin/$BRANCH:Dockerfile" 2>/dev/null | grep -oE -- '--uid[[:space:]]+[0-9]+' | grep -oE '[0-9]+' | head -1)
  [ -z "$uid" ] && return 0  # ni el actual ni el que viene fijan usuario
  local dueno
  dueno=$(stat -c '%u' data 2>/dev/null || echo "?")
  if [ "$dueno" = "$uid" ]; then
    ok "data/ pertenece al uid $uid del contenedor"
  else
    fallo "data/ es del uid $dueno y el contenedor corre como $uid: chown -R $uid:$uid data/ ANTES del deploy"
  fi
}

respaldar_base() {
  # API de backup de SQLite desde el propio contenedor: consistente aunque la
  # base esté en WAL y sin depender de que el VPS tenga `sqlite3`. Se verifica
  # la integridad del archivo resultante, no solo que exista.
  mkdir -p data/backups || { fallo "no pude crear data/backups"; return 1; }
  local salida
  if salida=$(timeout 120 docker exec -i "$CONTENEDOR" python - 2>&1 <<'PY'
import datetime, os, sqlite3, sys
dst = "/data/backups/tramites-%s.db" % datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
src = sqlite3.connect("/data/tramites.db")
out = sqlite3.connect(dst)
try:
    src.backup(out)
    veredicto = out.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    out.close(); src.close()
if veredicto != "ok":
    print("integrity_check:", veredicto); sys.exit(1)
print("%s %d bytes" % (dst, os.path.getsize(dst)))
PY
  ); then
    ok "respaldo íntegro: ${salida#/data/}"
  else
    fallo "respaldo de la base"
    echo "$salida" | tail -5 | sed 's/^/     │ /'
    return 1
  fi
}

# --- durante ------------------------------------------------------------------

fase_durante() {
  local esperado="$1" completo inicio log
  echo "== durante · esperando a que $esperado sirva (máx. ${ESPERA_MAX_SEGUNDOS}s) =="
  log=$(log_del_cron)
  [ -n "$log" ] && echo "   log del cron: $log"
  inicio=$(date +%s)
  while :; do
    git fetch origin "$BRANCH" --quiet 2>/dev/null || true
    completo=$(sha_completo "$esperado" || true)
    local head desplegado
    head=$(git rev-parse --short HEAD 2>/dev/null || echo none)
    desplegado=$(sha_desplegado || true)
    printf '   %s  HEAD=%s  sirve=%s  %s=%s\n' "$(date +%T)" "$head" "${desplegado:0:7}" "$CONTENEDOR" "$(estado_de "$CONTENEDOR")"
    if [ -n "$completo" ] && [ "$desplegado" = "$completo" ]; then
      ok "el commit $esperado está sirviendo"
      echo
      fase_despues "$esperado"
      # fase_despues hoy termina en exit (por eso shellcheck lo ve inalcanzable); si un día no, este
      # return es lo que corta el bucle.
      # shellcheck disable=SC2317
      return
    fi
    local rollbacks
    rollbacks=$(grep -c 'rollback a' "${log:-/dev/null}" 2>/dev/null | head -1)
    if [ "${rollbacks:-0}" -gt "${ROLLBACKS_PREVIOS:-0}" ]; then
      fallo "pull-deploy.sh hizo rollback; últimas líneas de $log:"
      tail -15 "$log" | sed 's/^/     │ /'
      resumen
    fi
    if [ $(( $(date +%s) - inicio )) -ge "$ESPERA_MAX_SEGUNDOS" ]; then
      fallo "pasaron ${ESPERA_MAX_SEGUNDOS}s y $esperado no sirve. ¿promote en verde? ¿origin/$BRANCH avanzó?"
      [ -n "$log" ] && tail -10 "$log" | sed 's/^/     │ /'
      resumen
    fi
    sleep 10
  done
}

# --- despues ------------------------------------------------------------------

fase_despues() {
  local esperado="$1" completo
  echo "== despues · $(date -Is) =="
  completo=$(sha_completo "$esperado" || true)
  if [ -z "$completo" ]; then
    fallo "el commit $esperado no existe en $REPO_DIR (¿git fetch? ¿sha correcto?)"
    resumen
  fi

  local head desplegado
  head=$(git rev-parse HEAD 2>/dev/null || echo none)
  desplegado=$(sha_desplegado || true)
  [ "$head" = "$completo" ]       && ok "HEAD es $esperado"                  || fallo "HEAD es ${head:0:7}, no $esperado"
  [ "$desplegado" = "$completo" ] && ok "data/deploy-info dice $esperado"    || fallo "data/deploy-info dice ${desplegado:0:7}: ese commit no pasó el health check"

  # Un deploy real reinicia el contenedor; deploy-info solo dice qué sirve.
  local antes_arranque="" ahora_arranque
  [ -f "$ESTADO" ] && antes_arranque=$(sed -n 's/^ARRANQUE=//p' "$ESTADO")
  ahora_arranque=$(arranque || true)
  if [ -z "$ahora_arranque" ]; then
    fallo "el contenedor $CONTENEDOR no existe"
  elif [ -z "$antes_arranque" ]; then
    aviso "sin nota de «antes»: no puedo probar que el contenedor se reinició (arrancó $ahora_arranque)"
  elif [[ "$ahora_arranque" > "$antes_arranque" ]]; then
    ok "el contenedor se reinició ($ahora_arranque > $antes_arranque)"
  else
    fallo "el contenedor NO se reinició desde «antes» ($ahora_arranque): sirve el código viejo"
  fi

  [ "$(estado_de "$CONTENEDOR")" = "running" ] && ok "contenedor running" || fallo "contenedor $(estado_de "$CONTENEDOR")"
  revisar_ollama
  intentar "/health responde" timeout 10 curl -sf "$SALUD" -o /dev/null

  pines_en_el_contenedor
  sondas_http
  errores_en_logs
  resumen
}

pines_en_el_contenedor() {
  # La imagen se construyó desde el requirements.txt de ESTE commit: lo que
  # `pip freeze` diga adentro tiene que coincidir con cada `==` del archivo.
  local congelado
  if ! congelado=$(timeout 60 docker exec "$CONTENEDOR" pip freeze 2>/dev/null); then
    fallo "no pude leer pip freeze dentro de $CONTENEDOR"; return 1
  fi
  local diferencias=0 linea nombre version instalada
  while IFS= read -r linea; do
    linea=${linea%%#*}; linea=${linea// /}
    [[ "$linea" == *==* ]] || continue
    nombre=${linea%%==*}; nombre=${nombre%%[*}; version=${linea#*==}
    instalada=$(echo "$congelado" | grep -i "^${nombre}==" | head -1 | cut -d= -f3)
    if [ "${instalada,,}" != "${version,,}" ]; then
      fallo "pin $nombre==$version pero el contenedor tiene ${instalada:-nada}"
      diferencias=$((diferencias + 1))
    fi
  done < requirements.txt
  [ "$diferencias" -eq 0 ] && ok "los pines de requirements.txt son los que corren en el contenedor"
}

sondas_http() {
  # Por el camino real, no solo /health. Las tres son propiedades permanentes
  # de la app desde INFRA-2: el formulario contesta el formulario (no un 422
  # del framework) y el tope de campos de starlette aplica.
  local codigo
  codigo=$(timeout 10 curl -s -o /dev/null -w '%{http_code}' "$LOGIN" || echo "sin respuesta")
  [ "$codigo" = "200" ] && ok "GET /login → 200" || fallo "GET /login → $codigo"

  codigo=$(timeout 10 curl -s -o /dev/null -w '%{http_code}' -X POST -d 'username=x&password=' "$LOGIN" || echo "sin respuesta")
  [ "$codigo" = "200" ] && ok "POST /login con contraseña vacía → 200 (llega al handler)" \
                        || fallo "POST /login con contraseña vacía → $codigo (¿422 del framework?)"

  # El cuerpo va en un archivo temporal: como argumento, 200 KB rebasan ARG_MAX
  # y curl ni arranca; por tubería, si curl cierra stdin antes de leerlo todo,
  # `pipefail` cuenta el SIGPIPE de printf como fallo. Ambos los encontró el
  # harness de pruebas, no un deploy.
  local cuerpo
  cuerpo=$(mktemp) || { fallo "mktemp para la sonda de campos"; return 1; }
  printf 'a&%.0s' $(seq 1 100000) > "$cuerpo"
  codigo=$(timeout 30 curl -s -o /dev/null -w '%{http_code}' -X POST \
           -H 'content-type: application/x-www-form-urlencoded' --data-binary "@$cuerpo" "$LOGIN" || echo "sin respuesta")
  rm -f "$cuerpo"
  [ "$codigo" = "400" ] && ok "100 000 campos → 400 (tope de request.form())" \
                        || fallo "100 000 campos → $codigo (starlette < 1.3.1 los parsea)"
}

errores_en_logs() {
  local lineas
  lineas=$(docker compose logs --tail=200 bot 2>/dev/null | grep -iE "traceback|error" | grep -viE "mssql|pymssql|login fallido" | head -5)
  if [ -n "$lineas" ]; then
    aviso "hay errores en los últimos 200 renglones del bot (fuera del ruido de MSSQL):"
    echo "$lineas" | sed 's/^/     │ /'
  else
    ok "sin tracebacks en los últimos 200 renglones del bot"
  fi
}

# --- entrada ------------------------------------------------------------------

uso() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

[ $# -ge 1 ] || uso
FASE=$1; shift
entrar_al_repo
case "$FASE" in
  antes)   fase_antes ;;
  durante) [ $# -eq 1 ] || uso
           # Cuántos rollbacks trae ya el log: solo cuenta uno NUEVO. `grep -c` imprime
           # el 0 y además sale con 1, así que un `|| echo 0` aquí duplicaría el cero.
           ROLLBACKS_PREVIOS=$(grep -c 'rollback a' "$(log_del_cron)" 2>/dev/null | head -1)
           ROLLBACKS_PREVIOS=${ROLLBACKS_PREVIOS:-0}
           fase_durante "$1" ;;
  despues) [ $# -eq 1 ] || uso; fase_despues "$1" ;;
  *)       uso ;;
esac
