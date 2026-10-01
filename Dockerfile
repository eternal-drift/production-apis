# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY proto ./proto
COPY wsdl ./wsdl
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
# Non-root, no shell-writable app dir, no package manager in the runtime image.
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/wsdl /app/wsdl
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 APP_ENV=production
USER 10001
EXPOSE 3001 3002 3003 3004 3005 3006
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:3001/healthz', timeout=2).status==200 else 1)"
CMD ["production-apis"]
