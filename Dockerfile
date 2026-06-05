FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Create the virtual environment inside the image.
RUN python -m venv "$VIRTUAL_ENV"

# Source needed for the editable install (setuptools must find the package).
COPY pyproject.toml ./
COPY erebus ./erebus
COPY config ./config
COPY tests ./tests

RUN pip install --upgrade pip && pip install -e ".[dev]"

# Default: run the test suite. The HTTP service entrypoint arrives in Phase 5
# (uvicorn erebus.supervisor.service:app --host 0.0.0.0 --port 8080).
CMD ["pytest", "-v"]
