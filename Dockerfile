FROM python:3.13-slim

# Dependencies come from uv.lock, so every build of a commit installs the same versions.
COPY --from=ghcr.io/astral-sh/uv:0.11.0 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-cache --no-install-project --extra vector
COPY src ./src
RUN uv sync --frozen --no-cache --no-editable --extra vector \
    && useradd --system --create-home --uid 10001 memcairn \
    && mkdir /data && chown memcairn /data

USER memcairn
# The embedding model is downloaded on first start into the data volume; the Hugging Face
# client also needs a writable cache/log directory, kept there too.
ENV MEMCAIRN_DB=/data/memcairn.db \
    MEMCAIRN_MODEL_DIR=/data/models \
    HF_HOME=/data/models/hf
VOLUME /data
EXPOSE 8765

# Set MEMCAIRN_PUBLIC_URL to the URL clients use (enables token auth and the Host check).
CMD ["memcairn", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
