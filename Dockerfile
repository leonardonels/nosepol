FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

RUN useradd -u 1000 -m nosepol
COPY --chown=nosepol:nosepol server/ server/
COPY --chown=nosepol:nosepol web/ web/
COPY --chown=nosepol:nosepol data/ data/
USER nosepol

EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/lines', timeout=4)"

CMD ["python", "server/app.py", "--port", "8080"]
