#!/usr/bin/env bash
# Instala, una vez por clon, el hook post-commit que avisa al Jenkins local de cada commit: el job
# «tramites» construye la rama en segundos en vez de esperar el escaneo de cada 2 min.
#   ci/instalar-hook.sh /ruta/a/01-TOOLS/jenkins/jenkinsctl
# Es local a propósito: git no versiona .git/hooks y la ruta de jenkinsctl es de este equipo. Vale para
# todos los worktrees del clon (comparten los hooks).
# No usa core.hooksPath: apagaría los hooks que ya viven en .git/hooks (el post-merge de rsc que limpia
# worktrees). Reinstalar es idempotente; un post-commit ajeno no se pisa.
set -euo pipefail
ctl="${1:?uso: ci/instalar-hook.sh /ruta/a/jenkinsctl}"
if [ ! -f "$ctl" ] || [ ! -x "$ctl" ]; then
  echo "instalar-hook: $ctl no existe o no es ejecutable" >&2
  exit 1
fi
ctl="$(cd "$(dirname "$ctl")" && pwd)/$(basename "$ctl")"
case "$ctl" in *"'"*) echo "instalar-hook: la ruta no puede llevar comillas simples" >&2; exit 1 ;; esac
if propio="$(git config --get core.hooksPath)"; then
  echo "instalar-hook: este clon usa core.hooksPath=$propio (quizá global); pon el aviso ahí a mano." >&2
  exit 1
fi
hook="$(git rev-parse --git-path hooks)/post-commit"
marca="# tramites: aviso a Jenkins (ci/instalar-hook.sh)"
if [ -e "$hook" ] && ! grep -qxF "$marca" "$hook"; then
  echo "instalar-hook: ya hay un $hook que no instaló este script; no lo toco." >&2
  exit 1
fi
mkdir -p "$(dirname "$hook")"
cat > "$hook" <<EOF
#!/bin/sh
$marca
# Avisa al Jenkins local para que construya la rama ya. En segundo plano y sin salida: nunca falla ni
# retrasa el commit. Con Jenkins apagado no pasa nada; al encenderlo, \`jenkinsctl scan tramites\`.
ctl='$ctl'
[ -x "\$ctl" ] && { "\$ctl" scan tramites --quiet >/dev/null 2>&1 & }
exit 0
EOF
chmod +x "$hook"
echo "Hook instalado: $hook → $ctl scan tramites"
