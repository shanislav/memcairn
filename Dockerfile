FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[vector]" && useradd --system --uid 10001 memcairn \
    && mkdir /data && chown memcairn /data

USER memcairn
# The embedding model is downloaded on first start into the data volume.
ENV MEMCAIRN_DB=/data/memcairn.db \
    MEMCAIRN_MODEL_DIR=/data/models
VOLUME /data
EXPOSE 8765

# Set MEMCAIRN_PUBLIC_URL to the URL clients use (enables token auth and the Host check).
CMD ["memcairn", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
