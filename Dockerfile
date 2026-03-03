FROM python:3.12-slim

WORKDIR /app

# Install deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY app/ app/

# Create data directory (ixvolume mounts over this)
RUN mkdir -p /data /episodes

EXPOSE 8499

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8499"]
