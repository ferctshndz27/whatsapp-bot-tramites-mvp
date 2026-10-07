#!/usr/bin/env bash
# Etapa «Imagen de producción» del Jenkinsfile: construye el Dockerfile de la raíz como lo hacen el job
# `build` de GitHub Actions y el VPS (deploy/pull-deploy.sh), y la borra. Aquí no se publica ni se corre
# nada: solo avisa en el commit, y no en el deploy, de que la imagen dejó de construir.
# Corre en el controlador de Jenkins (agent any), que habla con el Docker del equipo.
set -euo pipefail
cd "$(dirname "$0")/.."
# El PID distingue dos builds del mismo commit en ramas distintas: ninguno borra la imagen del otro.
etiqueta="tramites-bot:ci-$(git rev-parse --short=12 HEAD)-$$"
borrar() { docker image rm "$etiqueta" >/dev/null 2>&1 || true; }
trap borrar EXIT
docker build -t "$etiqueta" .
docker image inspect --format "construida: {{.Id}} ({{.Size}} bytes); se borra al salir" "$etiqueta"
