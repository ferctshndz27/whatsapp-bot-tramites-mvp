FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY templates/ templates/
COPY static/ static/
COPY landing/ landing/
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
