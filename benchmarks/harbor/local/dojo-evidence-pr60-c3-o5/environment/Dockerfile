FROM python:3.12-slim-bookworm@sha256:31c0807da611e2e377a2e9b566ad4eb038ac5a5838cbbbe6f2262259b5dc77a0

# Build context is environment/ only. Verifier and oracle are never image layers.
WORKDIR /app
COPY app/requirements.txt /tmp/dojo-requirements.txt
RUN python3 -m pip install --no-cache-dir -r /tmp/dojo-requirements.txt \
    && rm /tmp/dojo-requirements.txt
COPY app/ /app/
ENV PYTHONDONTWRITEBYTECODE=1
