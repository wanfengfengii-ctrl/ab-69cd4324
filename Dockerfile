FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080

WORKDIR /srv

# The service is pure standard library; no pip install is needed.
COPY app ./app
COPY tests ./tests
COPY verify.py ./verify.py
COPY docker/healthcheck.py ./healthcheck.py

RUN python -m compileall -q app && \
    adduser --system --no-create-home --uid 10001 appuser
USER 10001

EXPOSE 8080

HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=5 \
    CMD ["python", "/srv/healthcheck.py"]

CMD ["python", "-m", "app.main"]
