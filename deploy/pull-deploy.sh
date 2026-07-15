#!/bin/bash
# Deploy pull-based: corre en el VPS vía cron cada 2 minutos (con flock).
# Si la rama production avanzó, actualiza el código y reconstruye el contenedor.
# GitHub nunca tiene credenciales del VPS; este script solo necesita la
# deploy key de LECTURA que vive en ~/.ssh/tramites_deploy_key del VPS.
set -euo pipefail

REPO_DIR=/opt/whatsapp-bot-tramites
BRANCH=production

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
git reset --hard "origin/$BRANCH"
docker compose up -d --build

for _ in $(seq 1 12); do
  if curl -sf http://127.0.0.1:8000/health > /dev/null; then
    echo "$(date -Is) deploy OK: $REMOTE"
    exit 0
  fi
  sleep 5
done

echo "$(date -Is) ERROR: health check falló tras desplegar $REMOTE" >&2
docker compose logs --tail 30 bot >&2
exit 1
