FROM python:3.12-slim AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY pyproject.toml ./
COPY memu ./memu
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home memu
ENTRYPOINT ["python", "-m", "memu"]

FROM base AS core
RUN pip install --no-cache-dir '.[postgres,acquisition]' \
    && apt-get update && apt-get install -y --no-install-recommends ffmpeg poppler-utils \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data/memory && chown -R 10001:10001 /data
USER 10001:10001
EXPOSE 5072
CMD ["core"]

FROM core AS acquisition
USER root
RUN mkdir -p /data/acquisition && chown 10001:10001 /data/acquisition
USER 10001:10001
EXPOSE 5073
CMD ["acquisition"]

# Optional: only core needs native model libraries. Model weights are mounted, never baked in.
FROM core AS core-local-embedding
USER root
COPY ops/requirements-embedding.txt /app/requirements-embedding.txt
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 \
    && pip install --no-cache-dir -r /app/requirements-embedding.txt
USER 10001:10001

# A plain docker build defaults to the lightweight frontend, not model libraries.
FROM base AS mcp
USER 10001:10001
EXPOSE 5071
CMD ["mcp"]
