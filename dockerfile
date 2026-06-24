# syntax=docker/dockerfile:1
FROM python:3.13-slim-bookworm

# Install Poetry with a pinned version
RUN pip install poetry==2.4.1

# Install system dependencies required by the application
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Isolate the virtual environment from the system Python
ENV POETRY_VIRTUALENVS_CREATE=true
ENV POETRY_VIRTUALENVS_IN_PROJECT=true
ENV POETRY_CACHE_DIR=/tmp/poetry_cache

# Create a non-root user to run the application
RUN addgroup --system dev && adduser --system --ingroup dev dev

WORKDIR /app

COPY pyproject.toml poetry.lock* ./

# Install dependencies without dev dependencies or the project itself (not yet copied)
# BuildKit cache mount avoids re-downloading packages on subsequent builds
RUN --mount=type=cache,target=$POETRY_CACHE_DIR \
    poetry install --without dev --no-root --no-interaction --no-ansi

COPY src ./src

# Transfer ownership to the non-root user
RUN chown -R dev:dev /app

USER dev

# Expose Streamlit port
EXPOSE 8501

# Verify the container is healthy
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["/app/.venv/bin/streamlit"]
CMD ["run", "src/dashboard.py", "--server.address=0.0.0.0"]
