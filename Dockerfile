FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OPS_DASHBOARD_DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8091

CMD ["gunicorn", "--bind", "0.0.0.0:8091", "--workers", "1", "--threads", "4", "--timeout", "60", "app.app:app"]
