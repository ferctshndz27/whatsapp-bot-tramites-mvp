# Imagen de los agentes de CI del Jenkins local (ver Jenkinsfile). Dos etapas:
#   pytest     Python y dependencias de producción, más git (tests/test_deploy_rollback.py y otros lo usan).
#   navegador  pytest + Node 22 + Chromium, para el juez del CRM (scripts/qa_crm_navegador.mjs).
#
# Lo de arriba de la primera capa propia (hasta el `pip install`) es, a la letra, el Dockerfile de
# producción, y tests/test_ci_jenkins.py lo vigila. Por dos razones:
#   1. pytest corre sobre la misma base (mismo digest) y las mismas dependencias que corren en el VPS.
#   2. El Docker de Jenkins no tiene buildx: con el constructor clásico, borrar la imagen de producción
#      (ci/imagen.sh) borraría también su capa de pip. Como esta imagen la comparte y no se borra, el
#      build de producción la reutiliza y tarda segundos en vez de reinstalar todo.
# Al mover el digest del Dockerfile, muévelo aquí también: el test falla si no.
FROM python:3.12-slim@sha256:2f17fc044b579bab302c2e8054d3a686e2cb9a83de48e70534b94cd8ebbe06a9 AS pytest
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM pytest AS navegador
# El juez no usa npm: basta el binario de Node de la imagen oficial (la misma versión que usa Coin).
COPY --from=node:22.23.3-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c \
     /usr/local/bin/node /usr/local/bin/node
# Chromium de Debian en lugar de google-chrome (sin repositorio de Google ni llaves). Las fuentes son para
# que el texto mida algo parecido a lo de un escritorio: sin ninguna, Chromium dibuja cajas.
RUN apt-get update \
 && apt-get install -y --no-install-recommends chromium fonts-dejavu-core fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*
# El juez lee estas dos (scripts/qa_crm_navegador.mjs); fuera de esta imagen usa .venv y google-chrome.
ENV CHROME_BIN=/usr/bin/chromium QA_PYTHON=/usr/local/bin/python
