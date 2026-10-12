# TaskPilot 前端镜像：node 构建 + nginx 托管
# 注意：构建上下文是仓库根目录（compose 中 context: .），以便同时取到 frontend/ 与 nginx/nginx.conf

# ---- 构建阶段 ----
FROM docker.m.daocloud.io/library/node:20-alpine AS build
WORKDIR /src
# 先装依赖，利用层缓存
COPY frontend/package.json frontend/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm ci --no-audit --no-fund --registry=$NPM_REGISTRY --fetch-retries=1 --fetch-timeout=30000
COPY frontend/ ./
RUN npm run build

# ---- 运行阶段 ----
# nginx.conf 是整套系统的唯一入口配置：静态资源 + /api 反代 + /api/v1/ws WebSocket
FROM docker.m.daocloud.io/library/nginx:alpine
ARG VCS_REF=unknown
ARG VERSION=dev
LABEL org.opencontainers.image.title="TaskPilot Frontend" \
      org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.version=$VERSION
COPY nginx/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /src/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/ || exit 1
