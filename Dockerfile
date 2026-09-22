# Por digest y no solo por tag (auditoría 2026-09, INFRA-6): el tag se
# reescribe con cada parche del OS y cambia lo que corre en producción sin un
# commit de por medio. El precio es que los parches de la base llegan cuando
# alguien mueve este digest, a propósito y por git. Para moverlo:
#   docker buildx imagetools inspect python:3.12-slim   (o el header
#   Docker-Content-Digest del registro) y pegar el sha256 de la lista de manifiestos.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Sin USER, uvicorn corría como root: un RCE de la app era root en el contenedor
# (auditoría 2026-09, INFRA-5). uid fijo (10001) para que el chown del bind
# mount ./data en el VPS sea determinista: el host tiene que hacerlo ANTES del
# deploy que trae esta línea, o el contenedor no puede escribir /data, /health
# no contesta y el cron hace rollback (deploy/verificar-deploy.sh antes lo
# avisa). El cron nocturno `docker exec tramites-bot python -m app.jobs` hereda
# este usuario y escribe en /data, así que no cambia.
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --system --no-create-home app \
    && mkdir -p /data && chown app:app /data
COPY app/ app/
COPY templates/ templates/
COPY static/ static/
COPY landing/ landing/
USER app
EXPOSE 8000
# --timeout-graceful-shutdown: SIN esto uvicorn espera INDEFINIDAMENTE a que se cierren las
# conexiones abiertas cuando recibe SIGTERM, y el stream de avisos del CRM
# (/crm/api/eventos) no se cierra nunca por sí solo. Medido en este repo: con un solo panel
# abierto, `docker compose up -d` dejaba al contenedor viejo vivo hasta el SIGKILL de Docker
# (10 s), y ahí los `finally` ya no corren. Con el tope, para en ~3 s.
# El valor va por encima del peor caso del webhook (~8 s de cascada NLU) para no cortar un
# mensaje a medias, y por debajo del stop_grace_period del compose, que le da margen.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--timeout-graceful-shutdown", "10"]
