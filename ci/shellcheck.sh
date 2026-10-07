#!/bin/sh
# Etapa «shellcheck» del Jenkinsfile. Corre en koalaman/shellcheck-alpine, que no trae bash: este script
# es POSIX a propósito.
#
# deploy/pull-deploy.sh lo ejecuta el cron del VPS cada 2 min bajo `set -euo pipefail`: un error de shell
# ahí deja el bot sin deploy o sin rollback. Revisa también ci/*.sh. Lo que shellcheck NO ve —que la ruta
# de fallo siga llamando a rollback— lo cubre tests/test_deploy_rollback.py (skill devops-senior).
set -eu
cd "$(dirname "$0")/.."
shellcheck --version | sed -n 2p
set -- deploy/*.sh ci/*.sh
# Umbral «info»: incluye SC2086 (variable sin comillas), la clase de error que más muerde en bash; deja
# fuera «style» (sugerencias de gusto). Una excepción deliberada va como directiva junto al código.
shellcheck --severity=info "$@"
echo "shellcheck: sin hallazgos (severidad info o mayor) en $# scripts"
