# TaskPilot 后端镜像（Python 3.11，与开发环境一致）
# 基础镜像走 daocloud 镜像源（服务器到 dockerhub 不通）
FROM docker.m.daocloud.io/library/python:3.11-slim
ARG VCS_REF=unknown
ARG VERSION=dev
LABEL org.opencontainers.image.title="TaskPilot Backend" \
      org.opencontainers.image.revision=$VCS_REF \
      org.opencontainers.image.version=$VERSION

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    TZ=Asia/Shanghai

# curl 供 healthcheck；git 供部分技能脚本；nodejs/npm 供 MCP stdio 工具（npx）
# 切换 apt 源到阿里云镜像（服务器到 deb.debian.org 慢，阿里云快 3~4 倍）
RUN sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g; s|http://security.debian.org|https://mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources || true

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 产物验证（VerifyArtifact 的 Tier1 无头渲染）需要 Playwright + headless Chromium。
# 放在 COPY 代码之前，让它独占一个稳定缓存层：改业务代码不会重新下载浏览器。
# 用 `|| true` 兜底 —— 装不上时验证自动降级为 Tier0 静态检查（结论里会写明降级原因），
# 绝不能因为可选能力缺失而让整个镜像构建失败、连带阻断部署。
# npm 走 npmmirror：与上面 apt/pip 换源同理，构建机到官方源不通或极慢。
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN timeout 240s sh -c 'npm config set registry https://registry.npmmirror.com \
    && npm install --no-audit --no-fund playwright@1.62.1 \
    && npx playwright install --with-deps chromium-headless-shell' \
    && echo "playwright ready" > /app/.verify_render_status \
    || echo "playwright unavailable - Tier1 render disabled" > /app/.verify_render_status

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install -r requirements.txt

# 再拷代码（技能包在 app/agents 下随包带入；运行时数据目录由 compose 卷挂载）
COPY app ./app
# 迁移脚本全部随镜像带上：容器内可直接 python migrate_xxx.py（新增脚本不必再改这里）
COPY init_db.py ./
COPY migrate_*.py ./
COPY _test*.py ./
COPY _fixture_admin_mcp.py ./
COPY mcp_servers_seed.json ./

# 保存内置技能种子副本：运行时 /app/app/agents/skills 被 compose 挂载为持久卷，
# 首次挂载时卷为空，entrypoint 用此种子把内置技能合并进去。
RUN cp -r /app/app/agents/skills /app/app/agents/skills_seed
RUN cp -r /app/app/agents/skills_dialog /app/app/agents/skills_dialog_seed
RUN cp -r /app/app/agents/skills_task /app/app/agents/skills_task_seed

# entrypoint：合并内置技能种子到持久卷后交还控制权
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# 运行时目录预创建（卷挂载后权限归属容器内 root，无问题）
RUN mkdir -p uploads outputs knowledge traces projects user_management memory data

# 8000 业务 API；8001 独立线程健康探针（B4：与业务事件循环解耦，
# 事件循环被长同步段阻塞时探针仍能应答，避免误判 unhealthy）
EXPOSE 8000 8001

HEALTHCHECK --interval=20s --timeout=30s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8001/health || exit 1

# agents_cache 是进程内状态，必须单 worker
COPY governance-entrypoint.sh /governance-entrypoint.sh
RUN chmod +x /governance-entrypoint.sh
ENTRYPOINT ["/governance-entrypoint.sh"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
