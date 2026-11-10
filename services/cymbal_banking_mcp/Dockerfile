# ==============================================================================
# Purpose: Production container image for the Cymbal Banking MCP Server
#          (`cymbal-banking-mcp`) deployed on Google Cloud Run or GKE.
# Architecture/Context: Built from repository root context so both `common/` and
#          `services/cymbal_banking_mcp/` are packaged cleanly under an
#          unprivileged non-root UID (`10001`).
# Dependencies/Side Effects: Listens on `$PORT` (default `8080` on Cloud Run).
# Author: Pradeep Singh
# License: Apache-2.0
# ==============================================================================

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN groupadd --gid 10001 appgroup && \
    useradd --uid 10001 --gid appgroup --shell /sbin/nologin --create-home appuser

COPY pyproject.toml README.md ./
COPY common ./common
COPY services/__init__.py ./services/__init__.py
COPY services/cymbal_banking_mcp ./services/cymbal_banking_mcp

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
      "fastapi>=0.115.0,<1.0.0" \
      "uvicorn[standard]>=0.32.0,<1.0.0" \
      "pydantic>=2.9.0,<3.0.0" \
      "mcp>=1.6.0,<2.0.0" \
      "google-auth>=2.40.0,<=2.60.0" \
      "google-cloud-firestore>=2.19.0,<3.0.0"

USER appuser

EXPOSE 8080

CMD ["uvicorn", "services.cymbal_banking_mcp.server:app", "--host", "0.0.0.0", "--port", "8080"]
