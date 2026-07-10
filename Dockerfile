# Service image for the API and the queue worker (rung-2 compose). The
# harness-runner images that run a real Claude Code / Hermes session under
# eval are separate, per-harness, version-pinned images (SPEC S6) built under
# services/.../images/ once the harness-runner build lands; this image is only
# the FastAPI service + worker.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY specs ./specs
COPY tests/fixtures ./tests/fixtures
RUN pip install --no-cache-dir -e ".[s3]"

EXPOSE 8080
CMD ["uvicorn", "wololo_evals.asgi:app", "--host", "0.0.0.0", "--port", "8080"]
