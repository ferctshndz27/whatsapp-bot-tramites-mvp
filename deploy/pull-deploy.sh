#!/bin/bash
# Deploy pull-based: corre en el VPS vía cron cada 2 minutos (con flock).
# Si la rama production avanzó, actualiza el código y reconstruye el contenedor.
# GitHub nunca tiene credenciales del VPS; este script solo necesita la
# deploy key de LECTURA que vive en ~/.ssh/tramites_deploy_key del VPS.
set -euo pipefail

REPO_DIR=/opt/whatsapp-bot-tramites
BRANCH=production

# ponytail: todo lo que corre DESPUÉS de "git reset --hard" vive dentro de
# esta función a propósito. bash lee y cachea el cuerpo completo de una
# función al parsear el script, antes de ejecutar nada — así el reset
# (que reescribe este mismo archivo en pleno vuelo) no corrompe la
# posición de lectura del intérprete a mitad de las líneas restantes.
# Si el deploy nuevo no levanta, regresar el código Y el contenedor al commit
# anterior. Sin esto, `git reset --hard` ya movió HEAD antes del `up`: en el
# siguiente tick LOCAL==REMOTE, el script sale 0 para siempre y el bot queda
# CAÍDO hasta que un humano entre por SSH. Con el rollback, HEAD se queda en
# el commit viejo y cada tick reintenta el deploy que falló.
rollback() {
  echo "$(date -Is) rollback a $LOCAL" >&2
  if [ "$LOCAL" != "none" ]; then
    git reset --hard "$LOCAL"
    docker compose up -d --build \
      || echo "$(date -Is) ERROR: el rollback tampoco levantó" >&2
  fi
}

deploy() {
  git reset --hard "origin/$BRANCH"
  mkdir -p data
  if ! docker compose up -d --build; then
    # Con `depends_on: service_healthy`, un MSSQL enfermo aborta el up y
    # DESTRUYE el contenedor viejo del bot antes de crear el nuevo.
    echo "$(date -Is) ERROR: compose up falló para $REMOTE" >&2
    rollback
    return 1
  fi

  # El stack de correo (mailserver/, Poste.io) se retiró del VPS el 2026-09-22:
  # no se usaba y sus 417 MiB y siete puertos publicados estorbaban. Este
  # script ya no lo levanta; mailserver/README.md dice cómo volver a activarlo.

  for _ in $(seq 1 12); do
    if curl -sf http://127.0.0.1:8000/health > /dev/null; then
      # Versión desplegada visible en /system (data/ está montado en /data). Se
      # escribe AQUÍ y no antes del build: hasta este punto nada garantizaba que
      # el commit llegara a servir una petición, y como rollback() no reescribe
      # el archivo, un deploy fallido dejaba a /system anunciando un SHA que
      # nunca atendió a nadie —y a quien lo mirara, buscando el bug en el código
      # equivocado—. Escrito después del health check, el archivo dice lo que
      # está corriendo: durante el arranque muestra el commit anterior, que es
      # justamente el que sigue sirviendo.
      echo "$REMOTE $(date -Is)" > data/deploy-info
      echo "$(date -Is) deploy OK: $REMOTE"
      return 0
    fi
    sleep 5
  done

  echo "$(date -Is) ERROR: health check falló tras desplegar $REMOTE" >&2
  # `|| true` NO es cosmético: bajo `set -e`, un `docker compose logs` que
  # devuelva != 0 mata el script AQUÍ y `rollback` nunca corre. Y falla más de lo
  # que parece — basta que el commit malo renombre el servicio `bot` en
  # docker-compose.yml ("no such service: bot"), o que el demonio de docker haya
  # muerto en esa ventana, que en un VPS de 2 vCPU con MSSQL y un `--build`
  # encima es justo el estado donde más falta hace el rollback.
  # Sin esto, HEAD se queda en el commit malo, el siguiente tick del cron ve
  # LOCAL == REMOTE, sale 0 para siempre, y el bot queda CAÍDO sin una sola
  # alerta hasta que un humano entre por SSH: exactamente la caída permanente
  # que rollback() fue escrito para evitar (ver su comentario arriba).
  # Los logs se vuelcan ANTES del rollback a propósito: rollback recrea los
  # contenedores y se llevaría la evidencia.
  docker compose logs --tail 30 bot >&2 || true
  rollback
  return 1
}

cd "$REPO_DIR"
export GIT_SSH_COMMAND="ssh -i ~/.ssh/tramites_deploy_key -o IdentitiesOnly=yes"

git fetch origin "$BRANCH" --quiet
LOCAL=$(git rev-parse HEAD 2>/dev/null || echo none)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
  exit 0
fi

# Solo se despliega lo que DESCIENDE de lo que ya sirve (auditoría 2026-09,
# INFRA-1). `promote` avanza en fast-forward, así que un origin/production que
# no contenga a HEAD solo puede venir de un force-push o de un push directo: un
# commit huérfano con un docker-compose.yml malicioso sería RCE en el VPS en
# menos de dos minutos. No se hace rollback porque no se movió nada; el cron
# vuelve a mirar en el siguiente tick y seguirá negándose hasta que alguien
# arregle la rama. Un rollback legítimo se hace hacia adelante (revertir el
# commit en main): reescribir production ya rompía `promote` de todos modos.
if [ "$LOCAL" != "none" ] && ! git merge-base --is-ancestor "$LOCAL" "$REMOTE"; then
  echo "$(date -Is) ERROR: origin/$BRANCH ($REMOTE) no desciende de HEAD ($LOCAL): la rama se reescribio; no se despliega" >&2
  exit 1
fi

echo "$(date -Is) desplegando $REMOTE (antes: $LOCAL)"
# .env, data/ y media/ están en .gitignore: reset --hard no los toca
deploy
