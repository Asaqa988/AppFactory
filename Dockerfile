# syntax=docker/dockerfile:1
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    CREWAI_DISABLE_TELEMETRY=true \
    CREWAI_DISABLE_TRACKING=true \
    OTEL_SDK_DISABLED=true

WORKDIR /srv/app

# requirements.txt states intent (ranges); constraints.txt pins the exact versions this project was tested with.
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

RUN useradd --create-home --uid 10001 appuser && chown appuser:appuser /srv/app

# ---- test image: `docker build --target test` then `docker run --rm <image>` ----
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt -c constraints.txt
COPY app ./app
COPY tests ./tests
COPY pytest.ini .
USER appuser
CMD ["pytest", "-ra"]

# ---- runtime image (default; used by both the api and worker services) ----
FROM base AS runtime
COPY app ./app
COPY scripts ./scripts
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
