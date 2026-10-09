FROM node:24.21.0-alpine3.24 AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY index.html vite.config.js nginx.conf.template ./
COPY src ./src
COPY public ./public
COPY tests ./tests
RUN npm test && npm run build

FROM nginx:1.30.5-alpine3.24
ENV DASHBOARD_PORT=8088 \
    API_ORIGIN=http://127.0.0.1:8000
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf.template /etc/dashboard/nginx.conf.template
COPY start.sh /usr/local/bin/moodle-dashboard
RUN chmod 0755 /usr/local/bin/moodle-dashboard
USER 101:101
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -T 3 -O /dev/null "http://127.0.0.1:${DASHBOARD_PORT}/healthz" || exit 1
ENTRYPOINT ["/usr/local/bin/moodle-dashboard"]
