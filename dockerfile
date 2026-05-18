FROM python:3.14-slim
WORKDIR /usr/local/app

# Install system dependencies and Poetry
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir poetry

# Copy Poetry dependency files first (better Docker layer caching)
COPY pyproject.toml poetry.lock* ./

# Configure Poetry
RUN poetry config virtualenvs.create false

# Install dependencies
RUN poetry install --no-root --no-interaction --no-ansi

# Copy project files
COPY . .

# Create directories
RUN mkdir -p data/raw
RUN mkdir -p data/processed
RUN mkdir -p data/metadata
RUN mkdir -p models
    
    
# Expose Streamlit port
EXPOSE 8501

# Default command
CMD ["poetry", "run", "streamlit", "run", "src/dashboard.py"]