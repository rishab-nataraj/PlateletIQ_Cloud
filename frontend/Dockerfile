# PlateletIQ console — static build served by nginx (local / self-hosted).
# Production uses Vercel; this image is for docker-compose and parity testing.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
ARG VITE_API_BASE_URL=http://localhost:8000/api/v1
ARG VITE_SENTRY_DSN=
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL VITE_SENTRY_DSN=$VITE_SENTRY_DSN
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
