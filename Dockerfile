FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# collectstatic only needs *a* key; the real one comes from the environment at runtime.
RUN DJANGO_SECRET_KEY=build-only DJANGO_DEBUG=0 python manage.py collectstatic --noinput

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data && chown app:app /data
USER app

ENV DJANGO_DEBUG=0 \
    DJANGO_DB_PATH=/data/db.sqlite3

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

ENTRYPOINT ["./docker/entrypoint.sh"]
# One process keeps the locmem cache and station index shared; scale with threads.
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", \
     "--workers", "1", "--threads", "8", "--timeout", "60", "--access-logfile", "-"]
