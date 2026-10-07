#!/usr/bin/env bash
# Etapa «Juez de navegador» del Jenkinsfile: scripts/qa_crm_navegador.mjs levanta uvicorn con una base
# temporal sembrada y recorre el CRM en Chromium headless (ver AGENTS.md, «CRM Kanban»). Es lo único de
# este pipeline que GitHub Actions no corre.
# En ci/Dockerfile (etapa navegador) QA_PYTHON y CHROME_BIN ya apuntan a lo instalado; fuera de esa
# imagen el juez usa .venv/bin/python y google-chrome, como siempre.
# Los argumentos van tal cual al juez (Jenkins pasa --capturas=reportes/capturas).
set -euo pipefail
cd "$(dirname "$0")/.."
# Chromium escribe en HOME aunque tenga --user-data-dir; el usuario del contenedor no tiene uno propio.
if [ ! -w "${HOME:-/nonexistent}" ]; then
  HOME="$(mktemp -d)"
  export HOME
fi
echo "node $(node --version) · $("${CHROME_BIN:-/usr/bin/google-chrome}" --version)"
exec node scripts/qa_crm_navegador.mjs "$@"
