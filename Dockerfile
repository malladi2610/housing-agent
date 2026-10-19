# syntax=docker/dockerfile:1
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
ENV PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock ./
COPY flat_agent ./flat_agent
# Optional corporate/session CA for the build only. Never disable TLS checks.
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --frozen --no-dev --no-editable && \
    chmod -R a+rX /app/flat_agent && \
    useradd --uid 10001 --create-home flatagent && \
    mkdir -p /app/data && chown -R flatagent:flatagent /app/data
ENV PATH="/app/.venv/bin:$PATH" FLAT_DATA_DIR=/app/data
USER flatagent
EXPOSE 8000
CMD ["flat-agent","web"]
