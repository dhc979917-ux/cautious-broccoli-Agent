#!/usr/bin/env bash
# ============================================================================
# 重建 Agent 沙箱镜像（基础镜像 + AgentScope 派生镜像）
#
# 为什么需要这个脚本：AgentScope 的 DockerWorkspace 不直接用
# WORKSPACE_DOCKER_IMAGE，而是以它为 base 再构建一层"派生镜像"
# （agentscope-workspace:<hash>，内含 MCP 网关 venv 等），并且**按 tag 缓存**。
# 那个 hash 只由"渲染后的 Dockerfile + 构建上下文文件"决定，**不含基础镜像内容**：
# 重建 taskpilot-skill:latest 之后 hash 不变，沙箱会继续命中旧派生镜像，
# 新装进基础镜像的依赖在沙箱里根本不存在，而且不报错、只是静默缺件。
#
# 所以基础镜像每次重建后，都必须清掉派生镜像缓存并预热重建 —— 本脚本一把做完。
# 不预热的话，重建会落在"第一个发起对话的用户"头上（阻塞其请求）。
#
# 另一个坑：派生镜像的 Dockerfile 用 `curl https://astral.sh/uv/install.sh | sh`
# 装 uv，而 releases.astral.sh 在内网只有约 100KB/s 且经常卡死（实测 20 分钟没下完）。
# 本脚本改用 `pip install uv`（走基础镜像里已配置的阿里云 PyPI 镜像），几十秒完成；
# 除这一行外，Dockerfile 与构建上下文完全由 AgentScope 自己生成，保证与运行时一致。
#
# 用法（在部署服务器上、仓库根目录执行）：
#   bash docker/rebuild_sandbox_image.sh              # 清派生缓存 + 预热重建
#   bash docker/rebuild_sandbox_image.sh --with-base  # 先重建 taskpilot-skill 基础镜像
# ============================================================================
set -euo pipefail

BASE_IMAGE="${WORKSPACE_DOCKER_IMAGE:-taskpilot-skill:latest}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-$(docker ps --format '{{.Names}}' | grep -E '^backend-' | head -1)}"
CTX_HOST_DIR="$(mktemp -d /tmp/ws_ctx.XXXXXX)"

if [ -z "$BACKEND_CONTAINER" ]; then
    echo "找不到 backend 容器（派生镜像的构建上下文要用容器内的 agentscope 生成）" >&2
    exit 1
fi

if [ "${1:-}" = "--with-base" ]; then
    echo "==> 重建基础镜像 $BASE_IMAGE"
    docker build -f docker/Dockerfile.skill -t "$BASE_IMAGE" .
fi

echo "==> 清理旧的派生镜像缓存（agentscope-workspace:*）"
# 只删没有被容器占用的；有沙箱在跑时 docker 会拒绝，属正常保护
# 注意 grep 无匹配时返回 1，配上 set -eo pipefail 会让脚本在这里静默退出，
# 所以整条管道用 || true 兜住（"没有旧缓存"是正常情况，不是错误）。
docker images --format '{{.Repository}}:{{.Tag}}' \
    | { grep -E '^agentscope-workspace:' || true; } \
    | while read -r img; do
        [ -n "$img" ] || continue
        docker rmi "$img" >/dev/null 2>&1 && echo "    removed $img" || echo "    skip（在用或已不存在） $img"
    done

echo "==> 用容器内的 agentscope 生成构建上下文（保证与运行时逐字节一致）"
TAG=$(docker exec -i -e PYTHONIOENCODING=utf-8 -e PYTHONPATH=/app -w /app "$BACKEND_CONTAINER" \
    python - "$BASE_IMAGE" <<'PY'
import shutil
import sys
from pathlib import Path

from agentscope.workspace._docker._make_dockerfile import (
    CONTAINER_WORKDIR,
    GATEWAY_HOME,
    prepare_build_context,
)
from app.config import settings

base_image = sys.argv[1]
extra_pip = [
    p.strip()
    for p in str(getattr(settings, "WORKSPACE_DOCKER_EXTRA_PIP", "") or "").split(",")
    if p.strip()
]

ctx_dir, tag, _copy_files = prepare_build_context(
    base_image=base_image,
    gateway_home=GATEWAY_HOME,
    container_workdir=CONTAINER_WORKDIR,
    node_version=None,
    extra_pip=extra_pip,
)

dockerfile = Path(ctx_dir) / "Dockerfile"
text = dockerfile.read_text(encoding="utf-8")
needle = "&& curl -LsSf https://astral.sh/uv/install.sh"
if needle in text:
    start = text.index(needle)
    tail_marker = "INSTALLER_NO_MODIFY_PATH=1 sh"
    end = text.index(tail_marker, start) + len(tail_marker)
    # 只换装 uv 的方式，其余一律不动；tag 仍用改动前算出的那个，保证运行时能命中缓存
    text = text[:start] + "&& pip install --no-cache-dir uv" + text[end:]
    dockerfile.write_text(text, encoding="utf-8")

out = Path("/tmp/ws_ctx")
shutil.rmtree(out, ignore_errors=True)
shutil.copytree(ctx_dir, out)
shutil.rmtree(ctx_dir, ignore_errors=True)
print(tag)
PY
)
# prepare_build_context 返回的是完整镜像引用（agentscope-workspace:<hash>），不是裸 hash
echo "    派生镜像 tag = $TAG"

docker cp "$BACKEND_CONTAINER":/tmp/ws_ctx/. "$CTX_HOST_DIR"/
docker exec "$BACKEND_CONTAINER" rm -rf /tmp/ws_ctx

echo "==> 构建派生镜像"
# 用传统构建器：Dockerfile 首行的 `# syntax=` 是给 BuildKit 的，走 BuildKit 会去
# 拉 dockerfile frontend 镜像（内网慢）；运行时 aiodocker 用的也是传统 build 端点。
DOCKER_BUILDKIT=0 docker build -t "$TAG" "$CTX_HOST_DIR"

rm -rf "$CTX_HOST_DIR"
echo "==> 完成。核对："
docker images | { grep -E "agentscope-workspace|$BASE_IMAGE" || true; }
echo "下一次会话起沙箱即命中该缓存，不会阻塞用户请求。"
