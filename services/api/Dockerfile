# Build context: repository root (the worker reuses the API package; the dashboard is built and served by the API).
FROM node:22.14.0-alpine AS dashboard
WORKDIR /build
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/dashboard/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/api
WORKDIR /srv/api
COPY services/api/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/api/ /srv/api/
COPY services/worker/ /srv/worker/
COPY --from=dashboard /build/dist /srv/api/app/static/dashboard
RUN sed -i 's/\r$//' /srv/api/entrypoint.sh && chmod +x /srv/api/entrypoint.sh \
    && useradd --system --uid 10001 rf
USER rf
CMD ["/srv/api/entrypoint.sh"]
