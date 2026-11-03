FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --system --uid 10001 engrammar \
    && mkdir /data && chown engrammar /data

USER engrammar
ENV ENGRAMMAR_DB=/data/engrammar.db
VOLUME /data
EXPOSE 8765

# Set ENGRAMMAR_PUBLIC_URL to the URL clients use (enables token auth and the Host check).
CMD ["engrammar", "serve", "--transport", "http", "--host", "0.0.0.0", "--port", "8765"]
