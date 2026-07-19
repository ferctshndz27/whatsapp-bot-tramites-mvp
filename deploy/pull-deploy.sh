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
deploy() {
  git reset --hard "origin/$BRANCH"
  # Versión desplegada visible en el panel /system (data/ está montado en /data)
  mkdir -p data
  echo "$REMOTE $(date -Is)" > data/deploy-info
  docker compose up -d --build

  # Stack de correo (Poste.io) — independiente del bot, no bloquea su
  # deploy ni su health check si falla; solo se registra la advertencia.
  (cd mailserver && docker compose up -d) \
    || echo "$(date -Is) ADVERTENCIA: fallo al desplegar mailserver" >&2

  for _ in $(seq 1 12); do
    if curl -sf http://127.0.0.1:8000/health > /dev/null; then
      echo "$(date -Is) deploy OK: $REMOTE"
      return 0
    fi
    sleep 5
  done

  echo "$(date -Is) ERROR: health check falló tras desplegar $REMOTE" >&2
  docker compose logs --tail 30 bot >&2
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

echo "$(date -Is) desplegando $REMOTE (antes: $LOCAL)"
# .env, data/ y media/ están en .gitignore: reset --hard no los toca
deploy
