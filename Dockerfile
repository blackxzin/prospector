FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libnotify-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY prospector ./prospector
RUN pip install --no-cache-dir .

COPY templates.yaml listas.yaml ./
ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    TEMPLATES_PATH=/app/templates.yaml \
    LISTAS_PATH=/app/listas.yaml

ENTRYPOINT ["prospector"]
CMD ["status"]
