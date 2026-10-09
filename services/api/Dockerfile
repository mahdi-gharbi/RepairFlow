# Build context: services/ (the worker reuses the API package for models and dispatch logic).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/api
WORKDIR /srv/api
COPY api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY api/ /srv/api/
COPY worker/ /srv/worker/
RUN sed -i 's/\r$//' /srv/api/entrypoint.sh && chmod +x /srv/api/entrypoint.sh \
    && useradd --system --uid 10001 rf
USER rf
CMD ["/srv/api/entrypoint.sh"]
