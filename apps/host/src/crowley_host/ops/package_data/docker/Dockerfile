FROM ghcr.io/astral-sh/uv:0.9.18-python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY *.whl /build/
RUN uv venv /app/.venv \
    && uv pip install --python /app/.venv/bin/python /build/*.whl \
    && rm /build/*.whl

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin crowley
RUN mkdir /data && chown -R crowley:crowley /app /data
USER crowley

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=5s --retries=6 CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
CMD ["/app/.venv/bin/crowley-server"]
