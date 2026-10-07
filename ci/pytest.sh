#!/usr/bin/env bash
# Etapa «pytest» del Jenkinsfile: la suite completa sobre Python 3.12 con las dependencias de producción
# (ci/Dockerfile, etapa pytest). Es lo mismo que el job `test` de GitHub Actions. A mano:
#   docker build -f ci/Dockerfile --target pytest -t tramites-ci:pytest .
#   docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:$PWD" -w "$PWD" tramites-ci:pytest ci/pytest.sh
# Los argumentos van tal cual a pytest (Jenkins pasa --junitxml=reportes/pytest.xml).
set -euo pipefail
cd "$(dirname "$0")/.."
# El usuario del contenedor es el de Jenkins (uid 1000), sin HOME propio; git lo quiere escribible.
if [ ! -w "${HOME:-/nonexistent}" ]; then
  HOME="$(mktemp -d)"
  export HOME
fi
python --version
exec python -m pytest -q -p no:cacheprovider "$@"
