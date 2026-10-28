#!/usr/bin/env bash
# Etapa «Juez de navegador» del Jenkinsfile: lo único de este pipeline que GitHub Actions no corre. Dos
# jueces en el mismo Chromium headless, cada uno con su base temporal sembrada:
#  - scripts/qa_crm_navegador.mjs levanta uvicorn y recorre el CRM (ver AGENTS.md, «CRM Kanban»).
#  - ext-cse/prueba/juez-chrome.mjs instala la extensión del panel lateral y comprueba que muestra el
#    CRM con sesión y que su regla no deja enmarcar el CRM fuera del panel.
# En ci/Dockerfile (etapa navegador) QA_PYTHON y CHROME_BIN ya apuntan a lo instalado; fuera de esa
# imagen los jueces usan .venv/bin/python y google-chrome, como siempre.
# Los argumentos van tal cual al juez del CRM (Jenkins pasa --capturas=reportes/capturas).
set -euo pipefail
cd "$(dirname "$0")/.."
# Chromium escribe en HOME aunque tenga --user-data-dir; el usuario del contenedor no tiene uno propio.
if [ ! -w "${HOME:-/nonexistent}" ]; then
  HOME="$(mktemp -d)"
  export HOME
fi
echo "node $(node --version) · $("${CHROME_BIN:-/usr/bin/google-chrome}" --version)"
node scripts/qa_crm_navegador.mjs "$@"
node ext-cse/prueba/juez-chrome.mjs
