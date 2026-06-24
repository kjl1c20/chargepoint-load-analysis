Use the following template when building the Dockerfile for a Python Streamlit application. Application dependencies will be managed by Poetry, so Poetry should be installed in an earlier layer.

Here are the principles to follow for a production-grade Dockerfile:
1. Follow Docker caching principles. Organise commands into logical layers to reduce build time.
2. Don't run commands as the root user. Create an environment user 'dev' to execute commands.
3. Define environment variables and arguments where necessary to make the code clear and readable.
4. Define WORKDIR to improve code readability.
5. Keep the Docker image minimal.
6. Pin the Poetry version. Poetry can contain breaking changes from one minor version to another.
7. Only COPY the data you need, and nothing else, based on my requirements.
8. Avoid installing development dependencies with `poetry install --without dev`, as you won't need linters and test suites in a production environment.
9. Create a .dockerignore to leave out notebooks or files that are not needed.
10. Make use of BuildKit to enhance performance.

Follow the template below to build the Dockerfile. Follow this order to maximise the benefits of Docker caching.
```
FROM python:3.{13}-slim-bookworm

# Install Poetry with a specific pinned version, e.g. 2.4.1
RUN pip install poetry=={specific_version}

# Install necessary system packages required by the application
RUN apt-get update && apt-get install -y --no-install-recommends \
    <package-1> \
    <package-2> \
    && rm -rf /var/lib/apt/lists/*

# Isolate the virtual environment from the system Python
ENV POETRY_VIRTUALENVS_CREATE=true
ENV POETRY_CACHE_DIR=/home/dev/.cache/pypoetry

# Create a non-root user to run the application
RUN addgroup --system dev && adduser --system --ingroup dev dev

WORKDIR /app

COPY pyproject.toml poetry.lock ./

# Install dependencies without dev dependencies or the project itself (not yet copied)
# BuildKit cache mount avoids re-downloading packages on subsequent builds
RUN --mount=type=cache,target=$POETRY_CACHE_DIR \
    poetry install --without dev --no-root --no-interaction --no-ansi

# Replace this with where the source code is located
COPY src ./src

# Transfer ownership to the non-root user
RUN chown -R dev:dev /app

USER dev

# Expose Streamlit port
EXPOSE 8501

# Verify the container is healthy
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl --fail http://localhost:8501/_stcore/health || exit 1

ENTRYPOINT ["poetry", "run", "streamlit"]
CMD ["run", "{streamlit_app}.py", "--server.address=0.0.0.0"]
```
