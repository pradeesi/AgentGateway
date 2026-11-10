# ==============================================================================
# Purpose: Container image for the Tier-1 Support ADK Agent on Vertex AI Agent
#          Runtime, Cloud Run, or GKE.
# Architecture/Context: Installs optional `AGENT_GATEWAY_ROOT_CERTIFICATES` into
#          the Debian CA trust store so TLS decryption/inspection on Google Cloud
#          `AGENT_TO_ANYWHERE` Agent Gateway is trusted by Python `httpx`,
#          `requests`, OpenSSL, and gRPC.
# Dependencies/Side Effects: Runs as non-root UID `1000` serving `app.fast_api_app:app`.
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

RUN apt-get update && \
    apt-get install -y --no-install-recommends ca-certificates && \
    rm -rf /var/lib/apt/lists/*

ARG AGENT_GATEWAY_ROOT_CERTIFICATES=""
ARG AGENT_GATEWAY_ROOT_CERTS_B64=""
RUN mkdir -p /usr/local/share/ca-certificates && \
    if [ -n "$AGENT_GATEWAY_ROOT_CERTIFICATES" ]; then \
      printf "%b" "$AGENT_GATEWAY_ROOT_CERTIFICATES" | \
        awk 'BEGIN {c=0} /BEGIN CERTIFICATE/ {c++} c > 0 { print > "/usr/local/share/ca-certificates/agw-" c ".crt" }'; \
      update-ca-certificates; \
    elif [ -n "$AGENT_GATEWAY_ROOT_CERTS_B64" ]; then \
      printf "%s" "$AGENT_GATEWAY_ROOT_CERTS_B64" | base64 -d | \
        awk 'BEGIN {c=0} /BEGIN CERTIFICATE/ {c++} c > 0 { print > "/usr/local/share/ca-certificates/agw-" c ".crt" }'; \
      update-ca-certificates; \
    fi

ENV GRPC_DEFAULT_SSL_ROOTS_FILE_PATH=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV AGENT_GATEWAY_ROOT_CERT_302034098528=/etc/ssl/certs/ca-certificates.crt

RUN useradd --uid 1000 --create-home --shell /sbin/nologin agentuser

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

USER 1000
EXPOSE 8080

CMD ["uvicorn", "app.fast_api_app:app", "--host", "0.0.0.0", "--port", "8080"]
