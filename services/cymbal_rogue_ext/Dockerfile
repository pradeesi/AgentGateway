# ==============================================================================
# Purpose: Production container image for the unregistered external webhook
#          service (`cymbal-rogue-ext`) deployed on Google Cloud Run.
# Architecture/Context: Deliberately excluded from Google Cloud Agent Registry
#          so `AGENT_TO_ANYWHERE` Agent Gateway blocks outbound calls to it.
# Dependencies/Side Effects: Runs as non-root UID `10001` listening on `$PORT`.
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

COPY common/__init__.py ./common/__init__.py
COPY common/logging_utils.py ./common/logging_utils.py
COPY common/models.py ./common/models.py
COPY common/seed_data.py ./common/seed_data.py
COPY common/ledger_store.py ./common/ledger_store.py
COPY services/__init__.py ./services/__init__.py
COPY services/cymbal_rogue_ext ./services/cymbal_rogue_ext

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
      "fastapi>=0.115.0,<1.0.0" \
      "uvicorn[standard]>=0.32.0,<1.0.0" \
      "pydantic>=2.9.0,<3.0.0"

USER appuser

EXPOSE 8080

CMD ["uvicorn", "services.cymbal_rogue_ext.app:app", "--host", "0.0.0.0", "--port", "8080"]
