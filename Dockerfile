FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY blast_radius ./blast_radius
COPY web ./web

# Render (and most platforms) pass the port in $PORT.
CMD ["sh", "-c", "uvicorn blast_radius.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
