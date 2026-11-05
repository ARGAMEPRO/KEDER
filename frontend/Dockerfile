FROM node:20-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci
COPY . .
ARG VITE_API_URL=/api/v1
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build

FROM nginx:1.27-alpine
# Kept outside conf.d/ so nginx does not load it as a standalone server config;
# nginx.conf includes it explicitly from each location.
COPY security-headers.conf /etc/nginx/security-headers.conf
COPY nginx.conf /etc/nginx/conf.d/default.conf
# nginx runs as the unprivileged `nginx` user, so every file it reads at startup
# must be world-readable — do not rely on the source file mode (umask/checkout).
RUN chmod 0644 /etc/nginx/security-headers.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
# nginx:alpine is not configured to run as a non-root user out of the box; the
# writable paths below are what it needs.
RUN touch /var/run/nginx.pid \
    && chown -R nginx:nginx /usr/share/nginx/html /var/cache/nginx /var/run/nginx.pid
USER nginx
EXPOSE 80
