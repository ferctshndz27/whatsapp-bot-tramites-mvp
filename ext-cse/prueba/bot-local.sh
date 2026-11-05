#!/usr/bin/env bash
# Bot local con datos de prueba para ver la extensión a mano (nunca toca tramites.db ni .env).
#
#   ext-cse/prueba/bot-local.sh [puerto] [--mucho]   (puerto por omisión 8000)
#   --mucho: tablero lleno (30 clientes, 3 etapas extra, textos largos) para ver cómo se acomoda crm-layout.
#
# Siembra una base temporal con scripts/qa_crm_seed.py y levanta uvicorn sobre ella. Usuarios, todos
# con la clave clave-qa-123: jefa1 (admin global), jefe2 (admin de la oficina Puebla),
# colab1 y colab2 (colaboradores). Ctrl+C lo detiene y borra la base temporal.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PUERTO=8000; MUCHO=0
for a in "$@"; do case "$a" in --mucho) MUCHO=1 ;; *) PUERTO="$a" ;; esac; done
DIR="$(mktemp -d)"
trap 'rm -rf "$DIR"' EXIT
export DB_PATH="$DIR/qa.db" SECURE_COOKIES=0 SESSION_SECRET=qa-secreto-de-prueba-no-usar \
  MSSQL_HOST= MSSQL_SA_PASSWORD= OLLAMA_URL= ANTHROPIC_API_KEY= WHATSAPP_TOKEN= STRIPE_SECRET_KEY= \
  ALTA_MANUAL_ENABLED=0 GEOIP_ENABLED=0 DASHBOARD_USERNAME= DASHBOARD_PASSWORD= \
  CRM_QA_MUCHOS="$MUCHO" CRM_QA_REPO="$REPO" CRM_QA_CLAVE=clave-qa-123
cd "$REPO"
.venv/bin/python scripts/qa_crm_seed.py >/dev/null
echo "Bot de prueba en http://127.0.0.1:$PUERTO — usuario colab1 / clave-qa-123 (admin: jefa1)"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$PUERTO" --log-level warning
