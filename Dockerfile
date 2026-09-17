# Bookplate — self-hosted ebook library (linux/amd64 + linux/arm64)
FROM python:3.14-slim

# Z-Library CLI (static Go binary, official linux builds) so in-container
# search works. Bump: https://github.com/heartleo/zlib/releases — update
# version here; sha256 is verified against the release's checksums.txt.
ARG ZLIB_VERSION=0.0.8
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && case "$(dpkg --print-architecture)" in \
      amd64) A=x86_64 ;; \
      arm64) A=arm64 ;; \
      *) echo "unsupported arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSLO "https://github.com/heartleo/zlib/releases/download/v${ZLIB_VERSION}/zlib_${ZLIB_VERSION}_linux_${A}.tar.gz" \
 && curl -fsSLO "https://github.com/heartleo/zlib/releases/download/v${ZLIB_VERSION}/checksums.txt" \
 && grep "zlib_${ZLIB_VERSION}_linux_${A}.tar.gz" checksums.txt | sha256sum -c - \
 && tar xzf "zlib_${ZLIB_VERSION}_linux_${A}.tar.gz" -C /usr/local/bin zlib \
 && chmod +x /usr/local/bin/zlib \
 && rm -f zlib_*.tar.gz checksums.txt \
 && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN useradd -m app
# deps first so code changes don't bust this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ app/
COPY web/ web/
# DATA_DIR resolves to /app/data (app/db.py derives it from its own path) —
# mount a volume here; pre-create + chown so named volumes inherit ownership.
RUN mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8480
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8480/', timeout=5)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8480"]
