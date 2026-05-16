FROM python:3.12-slim
WORKDIR /usr/local/app

# Speeds up rebuilds through caching if requirements are not modified
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip

RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Create directories
RUN mkdir -p data/raw
RUN mkdir -p data/processed
RUN mkdir -p models
    
    
# Expose Streamlit port
EXPOSE 8501

# Default command
CMD ["streamlit", "run", "app/streamlit_app.py"]