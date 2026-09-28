FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps for PyMuPDF, OpenCV (used by RapidOCR), libgomp (used by onnxruntime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libgl1 \
    libglib2.0-0 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY . .

# Create the data directory so the app can write sessions / job uploads.
# In production this path is volume-mounted, so contents survive restarts.
RUN mkdir -p /app/data

EXPOSE 8080

# Run migrations before starting the app
ENTRYPOINT ["sh", "-c", "python -c 'from app.core.database import get_resolved_database_url, init_database; from app.main import _run_migrations; _run_migrations()' && python main.py"]
