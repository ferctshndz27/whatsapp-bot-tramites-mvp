#!/usr/bin/env bash
# Etapa «Juez de navegador» del Jenkinsfile: lo único de este pipeline que GitHub Actions no corre. Tres
# jueces en navegadores reales sin cabeza, cada uno con su base temporal sembrada:
#  - scripts/qa_crm_navegador.mjs levanta uvicorn y recorre el CRM en Chromium (ver AGENTS.md, «CRM Kanban»).
#  - ext-cse/prueba/juez-chrome.mjs instala la extensión del panel lateral en Chromium: el panel muestra
#    el CRM con sesión, avisa si no hay CRM, red o permiso, y su regla no deja enmarcar el CRM fuera de él.
#  - ext-cse/prueba/juez-firefox.py hace lo mismo en Firefox, por geckodriver: el panel lateral navega
#    al CRM tras sus comprobaciones.
# En ci/Dockerfile (etapa navegador) QA_PYTHON, CHROME_BIN, FIREFOX_BIN y GECKODRIVER ya apuntan a lo
# instalado; fuera de esa imagen los jueces usan .venv/bin/python, google-chrome y el Firefox del PATH.
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
python3 ext-cse/prueba/juez-firefox.py
