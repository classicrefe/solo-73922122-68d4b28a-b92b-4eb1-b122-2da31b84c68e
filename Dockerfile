FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ACCOUNTING_DB=/data/accounting.db \
    PORT=5000

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app /data

USER appuser
VOLUME ["/data"]
EXPOSE 5000 8000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "30", "app:app"]
