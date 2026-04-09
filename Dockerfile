FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

COPY requirements-irving-mvp.txt ./
RUN pip install --no-cache-dir -r requirements-irving-mvp.txt

COPY . .

CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-8080} --timeout 300 --graceful-timeout 30 --keep-alive 75 -k uvicorn.workers.UvicornWorker irving_mvp_server:app"]
