FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends clamav \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY .env.example ./
COPY README.md ./

RUN mkdir -p /app/data /app/logs

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
