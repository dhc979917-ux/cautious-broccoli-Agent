"""服务预览：把 Agent 在会话沙箱里起的 Web 服务，发布成用户可直接点开的临时地址。

整条链路（全部由 PREVIEW_ENABLED 总开关控制，关闭时所有入口立即返回，行为与改造前一致）：

  探测   app.agents.service_probe.detect_services()  -> 沙箱内 LISTEN 端口 + 启动命令 + cwd
  发布   起一个 sidecar 代理容器（挂到沙箱所在网络，宿主端口从 PREVIEW_HOST_PORT_RANGE 取）
  打包   把服务源码目录打成 zip，附平台生成的 HOW_TO_RUN.md，登记进文件中心（一条记录）
  回传   URL + 源码包 通过 WebSocket 事件 service_preview 推给前端，并落库到消息 metadata
  回收   沙箱消失/TTL 到期 -> 标记 expired；宽限期后摘掉 sidecar 释放宿主端口

设计取舍：
  * 预览是"临时版"。沙箱一回收，URL 就只剩一个说明页；用户要长期使用必须下载 zip 自己部署。
    这条语义在 HOW_TO_RUN.md、失效页、提示词、前端卡片四处都写明，避免用户误以为是托管。
  * 不做 HTTP 层鉴权。裸 TCP 转发对 WebSocket/SSE 透明，代价是没法校验 token；
    访问控制靠"不可枚举的高位端口 + TTL + 并发上限"，并只在总开关打开的环境启用。
  * 与其它模块的耦合面只有三个：service_probe（纯探测）、output_service.build_zip（纯打包）、
    FileService.register_output_file（纯登记）。本模块不 import chat/manager，
    反向由它们调用，方便单独关闭或替换实现。
"""

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.config import OUTPUT_DIR, settings
from app.models.service_preview import ServicePreview

logger = logging.getLogger(__name__)

# sidecar 容器统一标签：启动清孤儿、批量回收都按它筛，不依赖名字约定
LABEL_MANAGED = "taskpilot.preview"
LABEL_SESSION = "taskpilot.preview.session"
LABEL_PREVIEW_ID = "taskpilot.preview.id"
LABEL_USER = "taskpilot.preview.user"
LABEL_TARGET = "taskpilot.preview.target"
CONTAINER_PREFIX = "tp_preview_"

# 源码包在会话目录里的存放子目录（output_service 已把它排除在"新增产物"探测之外，
# 否则同一个 zip 会被登记两次）
BUNDLE_DIRNAME = "_previews"
HOW_TO_RUN_NAME = "HOW_TO_RUN.md"

# sidecar 容器内监听端口（宿主端口映射到它）
_PROXY_LISTEN_PORT = 8080
# 发布后确认服务真的通了的探测脚本（在 sidecar 容器内跑，走完整链路）
_CHECK_SCRIPT = (
    "import urllib.request, urllib.error\n"
    "url = 'http://127.0.0.1:%d/'\n"
    "try:\n"
    "    r = urllib.request.urlopen(url, timeout=8)\n"
    "    print('OK', r.status, r.headers.get('X-Taskpilot-Preview') or '-')\n"
    "except urllib.error.HTTPError as e:\n"
    "    print('HTTP', e.code, e.headers.get('X-Taskpilot-Preview') or '-')\n"
    "except Exception as e:\n"
    "    print('ERR', type(e).__name__, str(e)[:120])\n"
) % _PROXY_LISTEN_PORT

# 打包时永远跳过的目录：工作区框架目录 + 平台产物目录 + 任何点目录
_ALWAYS_SKIP_DIRS = {
    "outputs", "skills", "data", "sessions", "_attachments", BUNDLE_DIRNAME,
}
# 单个源码包里最多列进 HOW_TO_RUN.md 的文件条数
_README_MAX_ENTRIES = 60
# 发布后连通性确认的重试节奏（服务可能刚好在重启）
_CHECK_ATTEMPTS = 4
_CHECK_INTERVAL = 1.5
# 端口被抢占时最多换几个候选端口重试（并发发布/宿主端口紧张时才有意义）
_MAX_PORT_ATTEMPTS = 20

# 同一会话并发发布时串行化，避免同一端口起两个 sidecar
_session_locks: Dict[int, asyncio.Lock] = {}


def _lock_for(session_id: int) -> asyncio.Lock:
    lock = _session_locks.get(session_id)
    if lock is None:
        lock = asyncio.Lock()
        _session_locks[session_id] = lock
    return lock


# ==================== 开关与地址 ====================

def preview_enabled() -> bool:
    """总开关：只有 docker 沙箱模式下才有"沙箱里的服务"可发布。"""
    if not bool(getattr(settings, "PREVIEW_ENABLED", False)):
        return False
    return str(getattr(settings, "WORKSPACE_BACKEND", "local")) == "docker"


def public_base_url() -> str:
    """对外展示用的基址（协议+主机，不含端口、不含结尾斜杠）。

    优先级：PREVIEW_PUBLIC_BASE_URL > CORS_ORIGINS 的第一条 > FRONTEND_BASE_URL 的主机。
    后端跑在容器里，自己看到的 IP 是容器网段，所以基址必须来自配置而不是自省。
    回落 CORS_ORIGINS 是因为它本来就是"用户浏览器访问本平台的地址"，任何正确配置的
    部署都已经有它，这样新环境不额外配 PREVIEW_PUBLIC_BASE_URL 也能拼出可用地址。
    全都拿不到时返回空串，调用方降级成"只给端口"的提示，绝不编一个错地址。
    """
    raw = str(getattr(settings, "PREVIEW_PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if raw:
        return raw
    for origin in str(getattr(settings, "CORS_ORIGINS", "") or "").split(","):
        candidate = _scheme_host(origin)
        if candidate:
            return candidate
    return _scheme_host(str(getattr(settings, "FRONTEND_BASE_URL", "") or "")) or ""


def _scheme_host(value: str) -> str:
    """从一个 URL/origin 里取出 ``scheme://host``；localhost 之类本机地址不算数。"""
    text = (value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"http://{text}")
    host = (parsed.hostname or "").strip()
    if not host or host in ("localhost", "127.0.0.1", "::1"):
        return ""
    return f"{parsed.scheme or 'http'}://{host}"


def build_preview_url(host_port: Optional[int]) -> str:
    """预览地址。基址缺失时返回空串（前端据此显示"未配置对外地址"而不是死链）。"""
    if not host_port:
        return ""
    base = public_base_url()
    if not base:
        return ""
    return f"{base}:{host_port}/"


def _port_range() -> Tuple[int, int]:
    raw = str(getattr(settings, "PREVIEW_HOST_PORT_RANGE", "") or "").strip()
    try:
        low_s, _sep, high_s = raw.partition("-")
        low, high = int(low_s), int(high_s)
    except ValueError:
        low, high = 31000, 31099
    if low < 1024 or high > 65535 or low > high:
        low, high = 31000, 31099
    return low, high


def _reserved_ports() -> Set[int]:
    raw = str(getattr(settings, "PREVIEW_RESERVED_HOST_PORTS", "") or "")
    ports: Set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            ports.add(int(item))
        except ValueError:
            logger.warning(f"[preview] PREVIEW_RESERVED_HOST_PORTS 含非法端口，已忽略: {item}")
    return ports


# ==================== 源码包 ====================

def _bundle_exclude_dirs() -> Set[str]:
    raw = str(getattr(settings, "PREVIEW_BUNDLE_EXCLUDE_DIRS", "") or "")
    names = {item.strip() for item in raw.split(",") if item.strip()}
    return names | _ALWAYS_SKIP_DIRS


def _bundle_max_bytes() -> int:
    mb = int(getattr(settings, "PREVIEW_BUNDLE_MAX_MB", 50) or 0)
    return mb * 1024 * 1024 if mb > 0 else 0


def collect_bundle_entries(
    root: str,
    exclude_dirs: Optional[Set[str]] = None,
    max_bytes: int = 0,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """递归收集 (绝对路径, 包内相对路径)，返回 (条目, 跳过原因列表)。

    - 跳过依赖/缓存/版本库目录（可配），跳过点目录、符号链接与非常规文件；
    - 累计体积超过 max_bytes 就停止收录并记原因（0 = 不限）；
    - 目录顺序稳定（sorted），保证同样的源码两次打包内容一致，便于核对。
    """
    skip = exclude_dirs if exclude_dirs is not None else _bundle_exclude_dirs()
    limit = max_bytes if max_bytes > 0 else _bundle_max_bytes()
    entries: List[Tuple[str, str]] = []
    skipped: List[str] = []
    total = 0
    if not root or not os.path.isdir(root):
        return entries, [f"源码目录不存在: {os.path.basename(root or '(空)')}"]
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        kept_dirs = []
        for name in sorted(dirnames):
            if name.startswith(".") or name in skip:
                skipped.append(f"{(rel_dir + '/' + name).lstrip('/')}/: 依赖或缓存目录，未打包")
                continue
            kept_dirs.append(name)
        dirnames[:] = kept_dirs
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = f"{rel_dir}/{name}" if rel_dir else name
            try:
                if os.path.islink(full) or not os.path.isfile(full):
                    skipped.append(f"{rel}: 不是常规文件，未打包")
                    continue
                size = os.path.getsize(full)
            except OSError as e:
                skipped.append(f"{rel}: 读取失败 {e}")
                continue
            if limit and total + size > limit:
                skipped.append(f"{rel}: 超出压缩包体积上限（{limit // (1024 * 1024)} MB），未打包")
                continue
            entries.append((full, rel))
            total += size
    return entries, skipped


def render_how_to_run(
    *,
    entry_command: str,
    target_port: int,
    project_dir: str,
    file_names: List[str],
    preview_url: str,
    ttl_minutes: int,
    notes: Optional[List[str]] = None,
) -> str:
    """生成包内的 HOW_TO_RUN.md（平台生成，不是模型写的，内容与实际探测结果一致）。"""
    names = [n for n in file_names if n][:_README_MAX_ENTRIES]
    has_requirements = any(n == "requirements.txt" or n.endswith("/requirements.txt") for n in names)
    has_package_json = any(n == "package.json" or n.endswith("/package.json") for n in names)
    has_pyproject = any(n == "pyproject.toml" or n.endswith("/pyproject.toml") for n in names)
    entry = (entry_command or "").strip() or "(未探测到启动命令，请查看包内源码)"
    lines: List[str] = [
        "# 服务源码包 · 如何在自己环境里启动",
        "",
        f"> 打包时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 沙箱内工作目录：`{project_dir or '(未知)'}`",
        f"> 服务监听端口：`{target_port}`",
        f"> 探测到的启动命令：`{entry}`",
        "",
        "## 这是什么",
        "",
        "本压缩包是 AI 在会话沙箱里搭建并实际运行过的服务源码，由平台自动收集打包。",
        "会话页给出的预览地址是**临时**的：它随沙箱一起存活，沙箱被回收后立即失效。",
        "要长期使用，请按下述步骤在自己的机器或服务器上部署。",
        "",
        "## 启动步骤",
        "",
        "1. 解压本压缩包到任意目录，进入该目录。",
        "2. 准备运行环境：",
    ]
    if has_requirements:
        lines += [
            "   - Python 项目（包内有 `requirements.txt`）：",
            "     ```bash",
            "     python3 -m venv .venv",
            "     source .venv/bin/activate        # Windows: .venv\\Scripts\\activate",
            "     pip install -r requirements.txt",
            "     ```",
        ]
    elif has_pyproject:
        lines += [
            "   - Python 项目（包内有 `pyproject.toml`）：`pip install .` 或 `pip install -e .`",
        ]
    elif has_package_json:
        lines += [
            "   - Node 项目（包内有 `package.json`）：`npm install`",
        ]
    else:
        lines += [
            "   - 包内没有依赖清单。若启动时报缺模块，按报错提示 `pip install <包名>`，",
            "     并把用到的依赖补写进 `requirements.txt`，方便下次部署。",
        ]
    lines += [
        f"3. 启动服务：`{entry}`",
        f"4. 浏览器打开 `http://127.0.0.1:{target_port}/`（部署在服务器上时换成服务器 IP，"
        f"并确认防火墙放行 {target_port} 端口）。",
        "",
        "## 注意事项",
        "",
        f"- 服务必须监听 `0.0.0.0:{target_port}` 才能被外部访问；只监听 `127.0.0.1` 时"
        "只有本机打得开。",
        "- 长期运行建议交给进程管理器（systemd / supervisor / pm2 / docker），"
        "不要用 `nohup` 裸跑。",
    ]
    if preview_url:
        lines += [
            "",
            "## 会话内的临时预览",
            "",
            f"- 预览地址：`{preview_url}`",
            f"- 最长存活：{ttl_minutes} 分钟，且沙箱被回收即失效（两者取先到者）。",
            "- 预览只用于当场验收效果，不要把它当作正式部署地址写进文档或发给他人。",
        ]
    if notes:
        lines += ["", "## 打包说明", ""]
        lines += [f"- {n}" for n in notes[:20]]
    lines += [
        "",
        "## 包内文件",
        "",
    ]
    if names:
        lines += [f"- `{n}`" for n in names]
        if len(file_names) > len(names):
            lines += [f"- ……（另有 {len(file_names) - len(names)} 个文件未列出）"]
    else:
        lines += ["- （空）"]
    lines.append("")
    return "\n".join(lines)


def build_preview_bundle(
    *,
    user_id: int,
    session_id: int,
    agent_id: Optional[int],
    output_dir: str,
    project_dir_backend: str,
    project_dir_sandbox: str,
    entry_command: str,
    target_port: int,
    preview_url: str,
    ttl_minutes: int,
    db: Session,
) -> Optional[dict]:
    """把服务源码打成 zip（含 HOW_TO_RUN.md）并登记进文件中心。

    Returns:
        {"filename": stored_name, "download_url": str, "size": int} 或 None（打包/登记失败）。
        失败只记日志，不影响预览地址本身的发布。
    """
    from app.services.output_service import build_zip

    entries, skipped = collect_bundle_entries(project_dir_backend)
    if not entries:
        logger.info(f"[preview] 会话 {session_id} 源码目录为空，跳过打包: {project_dir_backend}")
        return None
    names = [rel for _full, rel in entries]
    readme = render_how_to_run(
        entry_command=entry_command,
        target_port=target_port,
        project_dir=project_dir_sandbox,
        file_names=names,
        preview_url=preview_url,
        ttl_minutes=ttl_minutes,
        notes=skipped,
    )
    zip_name = f"service_{target_port}_{int(time.time())}.zip"
    bundle_dir = os.path.join(output_dir, BUNDLE_DIRNAME)
    zip_path = os.path.join(bundle_dir, zip_name)
    packed, errors = build_zip(
        entries,
        zip_path,
        extra_texts={HOW_TO_RUN_NAME: readme},
    )
    if packed == 0:
        logger.warning(f"[preview] 会话 {session_id} 源码打包失败: {'; '.join(errors[:3])}")
        return None
    try:
        size = os.path.getsize(zip_path)
    except OSError as e:
        logger.warning(f"[preview] 源码包大小读取失败: {e}")
        return None
    stored_name = f"sessions/{session_id}/{BUNDLE_DIRNAME}/{zip_name}"
    try:
        from app.services.file_service import FileService
        FileService(db).register_output_file(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            rel_name=stored_name,
            abs_path=zip_path,
        )
    except Exception as e:
        logger.warning(f"[preview] 源码包登记失败（本地仍可下载）: {e}")
    logger.info(
        f"[preview] 会话 {session_id} 源码打包完成: {packed} 个文件 -> {zip_name} ({size} bytes)"
    )
    return {
        "filename": stored_name,
        "download_url": f"/api/v1/upload/download/{stored_name}",
        "size": size,
    }


# ==================== Docker 侧：网络 / 端口 / sidecar ====================

async def _sandbox_endpoint(client, container_name: str) -> Tuple[str, str]:
    """返回 (网络名, 沙箱 IP)。

    sidecar 必须和沙箱在同一张网桥上才能连通（跨网桥被 DOCKER-ISOLATION 直接 DROP）。
    优先选自定义网络（有内建 DNS、隔离性更好），否则退回默认 bridge；
    默认 bridge 上没有名字解析，所以一律用 IP 作为转发目标。
    """
    try:
        container = await client.containers.get(container_name)
        info = await container.show()
    except Exception as e:
        logger.warning(f"[preview] 读取沙箱容器 {container_name} 失败: {e}")
        return "", ""
    networks = (info.get("NetworkSettings") or {}).get("Networks") or {}
    fallback: Tuple[str, str] = ("", "")
    for name, conf in networks.items():
        ip = str((conf or {}).get("IPAddress") or "")
        if not ip:
            continue
        if name != "bridge":
            return name, ip
        fallback = (name, ip)
    return fallback


async def _used_host_ports(client) -> Set[int]:
    """当前宿主上已被 Docker 发布的端口 + 配置保留端口。"""
    used: Set[int] = set(_reserved_ports())
    try:
        containers = await client.containers.list(all=True)
    except Exception as e:
        logger.warning(f"[preview] 列举容器失败，端口分配可能冲突: {e}")
        return used
    for c in containers:
        info = getattr(c, "_container", None) or {}
        for item in info.get("Ports") or []:
            public = (item or {}).get("PublicPort")
            if public:
                used.add(int(public))
    return used


def _db_host_ports(db: Session) -> Set[int]:
    rows = db.query(ServicePreview.host_port).filter(
        ServicePreview.host_port.isnot(None),
        ServicePreview.status.in_(["active", "expired"]),
    ).all()
    return {int(r[0]) for r in rows if r[0]}


async def _allocate_host_port(db: Session, client) -> List[int]:
    """给出候选宿主端口（按序尝试，创建失败即换下一个，天然容忍并发竞争）。"""
    low, high = _port_range()
    taken = await _used_host_ports(client)
    taken |= _db_host_ports(db)
    return [p for p in range(low, high + 1) if p not in taken]


async def _remove_container(client, name: str) -> bool:
    if not name:
        return False
    try:
        container = await client.containers.get(name)
    except Exception:
        return False
    try:
        await container.kill()
    except (Exception, asyncio.CancelledError):
        pass
    try:
        await container.delete(force=True)
        return True
    except (Exception, asyncio.CancelledError) as e:
        logger.warning(f"[preview] 删除代理容器 {name} 失败: {e}")
        return False


async def _start_sidecar(
    client,
    *,
    name: str,
    image: str,
    network: str,
    target_host: str,
    target_port: int,
    host_port: int,
    labels: Dict[str, str],
) -> bool:
    """创建并启动代理容器。端口被抢占（并发）时返回 False，由调用方换端口重试。"""
    from app.agents.preview_proxy import PROXY_SCRIPT

    await _remove_container(client, name)
    port_key = f"{_PROXY_LISTEN_PORT}/tcp"
    config = {
        "Image": image,
        "Cmd": ["python3", "-u", "-c", PROXY_SCRIPT],
        "Env": [
            f"TP_TARGET_HOST={target_host}",
            f"TP_TARGET_PORT={target_port}",
            f"TP_LISTEN_PORT={_PROXY_LISTEN_PORT}",
            "TP_PAGE_TITLE=预览已结束",
            "TP_PAGE_TAG=PREVIEW UNAVAILABLE",
            "TP_PAGE_REASON=该预览对应的会话沙箱已被回收或服务已停止，地址不再可用。",
        ],
        "ExposedPorts": {port_key: {}},
        "Labels": labels,
        "HostConfig": {
            "NetworkMode": network or "bridge",
            "PortBindings": {port_key: [{"HostIp": "0.0.0.0", "HostPort": str(host_port)}]},
            "RestartPolicy": {"Name": "no"},
            "Memory": 128 * 1024 * 1024,
            "PidsLimit": 64,
            "SecurityOpt": ["no-new-privileges:true"],
        },
    }
    try:
        container = await client.containers.create(config, name=name)
    except Exception as e:
        logger.warning(f"[preview] 创建代理容器 {name} 失败（端口 {host_port}）: {e}")
        return False
    try:
        await container.start()
    except Exception as e:
        logger.warning(f"[preview] 启动代理容器 {name} 失败: {e}")
        await _remove_container(client, name)
        return False
    return True


async def _check_reachable(client, sidecar_name: str) -> Tuple[str, str]:
    """从 sidecar 内部访问一次自己，确认整条转发链路是通的。

    Returns:
        (verdict, detail)：verdict 取 "ok" / "down" / "unknown"
        - ok      服务返回了任意 HTTP 响应（含 4xx，说明应用活着）
        - down    代理回了 503 且带 unreachable 标记（沙箱内服务没在监听）
        - unknown 探测本身失败（镜像没有 python、exec 被拒等），不影响发布
    """
    try:
        container = await client.containers.get(sidecar_name)
        from app.agents.docker_exec import exec_capture
        text = (await exec_capture(
            container, ["python3", "-c", _CHECK_SCRIPT], timeout=15, limit=4096
        )).strip()
    except Exception as e:
        return "unknown", f"连通性探测未执行: {e}"
    if not text:
        return "unknown", "连通性探测无输出"
    parts = text.split()
    kind = parts[0] if parts else ""
    if kind == "OK":
        return "ok", text
    if kind == "HTTP":
        code = parts[1] if len(parts) > 1 else ""
        marker = parts[2] if len(parts) > 2 else "-"
        if marker == "unreachable":
            return "down", f"代理已就绪，但沙箱内 {code} 无服务应答"
        return "ok", text
    return "down", text


# ==================== 序列化 ====================

def _iso(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    return str(value)


def preview_to_dict(row: ServicePreview) -> dict:
    """DB 行 -> 前端事件/接口用的字典（不暴露容器名等内部信息）。"""
    expires_at = row.expires_at
    seconds_left = 0
    if expires_at and row.status == "active":
        if expires_at.tzinfo is not None:
            delta = expires_at - datetime.now(expires_at.tzinfo)
        else:
            delta = expires_at - datetime.now()
        seconds_left = max(0, int(delta.total_seconds()))
    bundle = None
    if row.bundle_path:
        bundle = {
            "filename": row.bundle_path,
            "download_url": f"/api/v1/upload/download/{row.bundle_path}",
            "size": int(row.bundle_size or 0),
        }
    return {
        "id": row.id,
        "session_id": row.session_id,
        "status": row.status,
        "url": build_preview_url(row.host_port) if row.status == "active" else "",
        "host_port": row.host_port,
        "target_port": row.target_port,
        "bind_addr": row.target_host,
        "entry_command": row.entry_command or "",
        "process_name": row.process_name or "",
        "project_dir": row.project_dir or "",
        "bundle": bundle,
        "detail": row.detail or "",
        "created_at": _iso(row.created_at),
        "expires_at": _iso(expires_at),
        "seconds_left": seconds_left,
    }


# ==================== 发布 ====================

def _proxy_image() -> str:
    return (
        str(getattr(settings, "PREVIEW_PROXY_IMAGE", "") or "").strip()
        or str(getattr(settings, "WORKSPACE_DOCKER_IMAGE", "") or "").strip()
    )


async def publish_preview(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    agent_id: Optional[int],
    container_name: str,
    workspace_id: str,
    service,
    output_dir: str,
) -> Optional[dict]:
    """把一个探测到的沙箱服务发布成可访问的预览，返回 preview_to_dict 结果或 None。

    任何失败都不抛异常：预览是收尾增强项，绝不能因为它让本轮对话报错。
    """
    if not preview_enabled():
        return None
    if service is None or not container_name:
        return None

    async with _lock_for(session_id):
        try:
            return await _publish_locked(
                db,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                container_name=container_name,
                workspace_id=workspace_id,
                service=service,
                output_dir=output_dir,
            )
        except (Exception, asyncio.CancelledError) as e:
            logger.warning(f"[preview] 会话 {session_id} 发布预览失败（不影响对话）: {e}")
            db.rollback()
            return None


async def _publish_locked(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    agent_id: Optional[int],
    container_name: str,
    workspace_id: str,
    service,
    output_dir: str,
) -> Optional[dict]:
    port = int(service.port)
    now = datetime.now()
    ttl = int(getattr(settings, "PREVIEW_MAX_TTL_MINUTES", 120) or 120)

    # 同会话同端口重复发布（Agent 重启了自己的服务）：复用既有 sidecar，只续期
    existing = db.query(ServicePreview).filter(
        ServicePreview.session_id == session_id,
        ServicePreview.target_port == port,
        ServicePreview.status.in_(["active", "expired"]),
    ).order_by(ServicePreview.id.desc()).first()
    if existing is not None and existing.host_port:
        if await _sidecar_alive(existing.proxy_container):
            existing.status = "active"
            existing.container_name = container_name
            existing.workspace_id = workspace_id or existing.workspace_id
            existing.entry_command = service.command or existing.entry_command
            existing.project_dir = service.cwd or existing.project_dir
            existing.process_name = service.process_name or existing.process_name
            existing.target_host = service.bind_addr or existing.target_host
            existing.bind_loopback = bool(service.loopback_only)
            existing.last_seen_at = now
            existing.expires_at = now + timedelta(minutes=ttl)
            existing.detail = ""
            db.commit()
            db.refresh(existing)
            return preview_to_dict(existing)
        # sidecar 没了（宿主重启/被手工删）：按新发布走，端口重新分配
        existing.status = "expired"
        existing.detail = "代理容器已不存在，预览地址失效"
        db.commit()

    if service.loopback_only:
        # 绑在 127.0.0.1 上，别的容器（包括代理）根本连不到；发布必然失败，
        # 这里直接落一条 blocked 记录，把整改话术带给用户，比给个死链强。
        row = _insert_row(
            db,
            user_id=user_id, session_id=session_id, agent_id=agent_id,
            container_name=container_name, workspace_id=workspace_id, service=service,
            status="blocked",
            detail=(
                f"服务只监听了 127.0.0.1:{port}，沙箱外无法访问。"
                f"请让它改为监听 0.0.0.0:{port} 后重试。"
            ),
            expires_at=now + timedelta(minutes=ttl),
        )
        return preview_to_dict(row) if row else None

    # 并发上限：先全局，再按用户
    max_total = int(getattr(settings, "PREVIEW_MAX_CONCURRENT", 20) or 0)
    if max_total > 0:
        active_total = db.query(ServicePreview).filter(ServicePreview.status == "active").count()
        if active_total >= max_total:
            logger.warning(f"[preview] 全局并发预览已达上限 {max_total}，拒绝会话 {session_id}")
            return None
    max_user = int(getattr(settings, "PREVIEW_MAX_CONCURRENT_PER_USER", 3) or 0)
    if max_user > 0:
        active_user = db.query(ServicePreview).filter(
            ServicePreview.user_id == user_id,
            ServicePreview.status == "active",
        ).count()
        if active_user >= max_user:
            logger.warning(f"[preview] 用户 {user_id} 并发预览已达上限 {max_user}")
            return None

    image = _proxy_image()
    if not image:
        logger.warning("[preview] 未配置代理镜像（PREVIEW_PROXY_IMAGE/WORKSPACE_DOCKER_IMAGE 均为空）")
        return None

    try:
        import aiodocker
    except ImportError:
        logger.warning("[preview] aiodocker 不可用，无法发布预览")
        return None

    client = None
    try:
        client = aiodocker.Docker()
        network, target_host = await _sandbox_endpoint(client, container_name)
        if not target_host:
            logger.warning(f"[preview] 拿不到沙箱 {container_name} 的网络地址，跳过发布")
            return None
        candidates = await _allocate_host_port(db, client)
        if not candidates:
            low, high = _port_range()
            logger.warning(f"[preview] 宿主端口区间 {low}-{high} 已耗尽，无法发布预览")
            return None

        # 先落库再起容器：sidecar 的标签要带上记录 id，启动清孤儿才能双向对账。
        # 端口被别的进程抢走时删掉这条记录换下一个端口重试。
        row = None
        for candidate in candidates[:_MAX_PORT_ATTEMPTS]:
            sidecar_name = f"{CONTAINER_PREFIX}s{session_id}p{candidate}"
            row = _insert_row(
                db,
                user_id=user_id, session_id=session_id, agent_id=agent_id,
                container_name=container_name, workspace_id=workspace_id, service=service,
                status="active",
                detail="",
                proxy_container=sidecar_name,
                host_port=candidate,
                target_host=target_host,
                expires_at=now + timedelta(minutes=ttl),
            )
            if row is None:
                return None
            ok = await _start_sidecar(
                client,
                name=sidecar_name,
                image=image,
                network=network,
                target_host=target_host,
                target_port=port,
                host_port=candidate,
                labels={
                    LABEL_MANAGED: "true",
                    LABEL_PREVIEW_ID: str(row.id),
                    LABEL_SESSION: str(session_id),
                    LABEL_USER: str(user_id),
                    LABEL_TARGET: f"{target_host}:{port}",
                },
            )
            if ok:
                break
            _delete_row(db, row)
            row = None
        if row is None:
            logger.warning(
                f"[preview] 会话 {session_id} 连续 {_MAX_PORT_ATTEMPTS} 个端口都发布失败"
            )
            return None
        sidecar_name = row.proxy_container
        host_port = row.host_port

        # 连通性确认：给服务一点反应时间，任一次通就算通
        verdict, detail = "unknown", ""
        for _attempt in range(_CHECK_ATTEMPTS):
            verdict, detail = await _check_reachable(client, sidecar_name)
            if verdict == "ok":
                break
            await asyncio.sleep(_CHECK_INTERVAL)
        url = build_preview_url(host_port)
        if verdict == "down":
            logger.info(f"[preview] 会话 {session_id} 端口 {port} 发布后无应答: {detail}")
        if verdict != "ok":
            row.detail = detail or None
        if not url:
            row.detail = ((row.detail or "") + " 未配置 PREVIEW_PUBLIC_BASE_URL，无法拼出对外访问地址。").strip()
        db.commit()
        db.refresh(row)

        # 源码包：放在 URL 之后，HOW_TO_RUN.md 里才能写上真实预览地址
        bundle = None
        try:
            from app.agents.workspace import backend_path_of
            project_backend = backend_path_of(service.cwd or "", output_dir) or output_dir
            bundle = build_preview_bundle(
                db=db,
                user_id=user_id,
                session_id=session_id,
                agent_id=agent_id,
                output_dir=output_dir,
                project_dir_backend=project_backend,
                project_dir_sandbox=service.cwd or "",
                entry_command=service.command or "",
                target_port=port,
                preview_url=url,
                ttl_minutes=ttl,
            )
        except Exception as e:
            logger.warning(f"[preview] 会话 {session_id} 源码打包异常（预览仍可用）: {e}")
        if bundle:
            row.bundle_path = bundle["filename"]
            row.bundle_size = bundle["size"]
            db.commit()
        db.refresh(row)
        return preview_to_dict(row)
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


def _insert_row(
    db: Session,
    *,
    user_id: int,
    session_id: int,
    agent_id: Optional[int],
    container_name: str,
    workspace_id: str,
    service,
    status: str,
    detail: str = "",
    proxy_container: Optional[str] = None,
    host_port: Optional[int] = None,
    target_host: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> Optional[ServicePreview]:
    try:
        row = ServicePreview(
            user_id=user_id,
            session_id=session_id,
            agent_id=agent_id,
            container_name=container_name,
            workspace_id=workspace_id or None,
            target_host=target_host or (service.bind_addr or None),
            target_port=int(service.port),
            bind_loopback=bool(service.loopback_only),
            proxy_container=proxy_container,
            host_port=host_port,
            project_dir=(service.cwd or None),
            entry_command=(service.command or None),
            process_name=(service.process_name or None),
            status=status,
            detail=detail or None,
            created_at=datetime.now(),
            last_seen_at=datetime.now(),
            expires_at=expires_at,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception as e:
        db.rollback()
        logger.warning(f"[preview] 预览记录落库失败: {e}")
        return None


def _delete_row(db: Session, row: ServicePreview) -> None:
    """端口发布失败时撤掉刚落的记录，避免留下没有容器的幽灵行。"""
    try:
        db.delete(row)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[preview] 撤销预览记录失败: {e}")


async def _sidecar_alive(name: Optional[str]) -> bool:
    if not name:
        return False
    try:
        import aiodocker
    except ImportError:
        return False
    client = None
    try:
        client = aiodocker.Docker()
        container = await client.containers.get(name)
        info = await container.show()
        return bool((info.get("State") or {}).get("Running"))
    except Exception:
        return False
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


# ==================== 查询 / 停止 / 刷新 ====================

def list_previews(db: Session, session_id: int, user_id: int) -> List[dict]:
    rows = db.query(ServicePreview).filter(
        ServicePreview.session_id == session_id,
        ServicePreview.user_id == user_id,
    ).order_by(ServicePreview.id.desc()).limit(20).all()
    return [preview_to_dict(r) for r in rows]


def _owned(db: Session, preview_id: int, user_id: int) -> Optional[ServicePreview]:
    return db.query(ServicePreview).filter(
        ServicePreview.id == preview_id,
        ServicePreview.user_id == user_id,
    ).first()


async def stop_preview(db: Session, preview_id: int, user_id: int) -> Optional[dict]:
    """用户主动停掉预览：立刻摘 sidecar 释放宿主端口。"""
    row = _owned(db, preview_id, user_id)
    if row is None:
        return None
    await _teardown(db, row, detail="用户已停止预览", status="expired")
    return preview_to_dict(row)


async def refresh_preview(db: Session, preview_id: int, user_id: int) -> Optional[dict]:
    """重新探测沙箱并校准状态：服务还活着就续期，死了就置 expired。"""
    row = _owned(db, preview_id, user_id)
    if row is None:
        return None
    if row.status != "active":
        return preview_to_dict(row)
    from app.agents.service_probe import detect_services

    services = await detect_services(row.container_name)
    alive = any(s.port == row.target_port and not s.loopback_only for s in services)
    sidecar_ok = await _sidecar_alive(row.proxy_container)
    ttl = int(getattr(settings, "PREVIEW_MAX_TTL_MINUTES", 120) or 120)
    if alive and sidecar_ok:
        row.last_seen_at = datetime.now()
        row.expires_at = datetime.now() + timedelta(minutes=ttl)
        row.detail = None
        db.commit()
        db.refresh(row)
        return preview_to_dict(row)
    reason = "沙箱内服务已停止" if not alive else "代理容器已不存在"
    await _teardown(db, row, detail=reason, status="expired", keep_sidecar=not sidecar_ok)
    return preview_to_dict(row)


async def _teardown(
    db: Session,
    row: ServicePreview,
    *,
    detail: str,
    status: str = "expired",
    keep_sidecar: bool = False,
) -> None:
    """摘掉 sidecar 并落状态。

    keep_sidecar=False 时容器被删除，宿主端口立即释放；此时 URL 直接连不上。
    沙箱被回收的场景走 keep_sidecar=True 的宽限路径（见 expire_by_container），
    让 sidecar 留着回一个"预览已结束"的说明页，比浏览器报错友好。
    """
    row.status = status
    row.detail = detail
    row.last_seen_at = datetime.now()
    if not keep_sidecar and row.proxy_container:
        try:
            import aiodocker
            client = aiodocker.Docker()
            try:
                await _remove_container(client, row.proxy_container)
            finally:
                await client.close()
        except Exception as e:
            logger.warning(f"[preview] 摘除代理容器失败: {e}")
        row.proxy_container = None
        row.host_port = None
    try:
        db.commit()
        db.refresh(row)
    except Exception as e:
        db.rollback()
        logger.warning(f"[preview] 预览状态落库失败: {e}")


# ==================== 生命周期清理 ====================

async def expire_by_container(container_name: str, reason: str = "会话沙箱已回收") -> int:
    """沙箱容器消失时调用：把它的预览全部置为 expired，保留 sidecar 回说明页。"""
    if not container_name:
        return 0
    from app.database import SessionLocal

    db = SessionLocal()
    count = 0
    try:
        rows = db.query(ServicePreview).filter(
            ServicePreview.container_name == container_name,
            ServicePreview.status == "active",
        ).all()
        for row in rows:
            row.status = "expired"
            row.detail = reason
            row.last_seen_at = datetime.now()
            count += 1
        if count:
            db.commit()
            logger.info(f"[preview] {container_name} 的 {count} 条预览已置为失效")
    except Exception as e:
        db.rollback()
        logger.warning(f"[preview] 按沙箱失效预览时出错: {e}")
    finally:
        db.close()
    return count


async def sweep_orphan_sidecars() -> int:
    """启动清孤儿：宿主上带预览标签、但库里已无对应 active 记录的 sidecar 一律删掉。

    进程重启后内存态清零，上一轮留下的代理容器没人认领，既占宿主端口又没人访问。
    """
    if not preview_enabled():
        return 0
    try:
        import aiodocker
    except ImportError:
        return 0
    from app.database import SessionLocal

    db = SessionLocal()
    removed = 0
    client = None
    try:
        live_ids = {
            str(r[0]) for r in db.query(ServicePreview.id).filter(
                ServicePreview.status == "active",
                ServicePreview.proxy_container.isnot(None),
            ).all()
        }
        # 库里还 active 但宿主上已经没了的：直接置失效（宿主重启过）
        client = aiodocker.Docker()
        found_names: Set[str] = set()
        containers = await client.containers.list(all=True)
        for c in containers:
            info = getattr(c, "_container", None) or {}
            labels = (info.get("Labels") or {})
            if str(labels.get(LABEL_MANAGED, "")).lower() != "true":
                continue
            names = info.get("Names") or []
            name = (names[0] if names else info.get("Name") or "").lstrip("/")
            found_names.add(name)
            if str(labels.get(LABEL_PREVIEW_ID, "")) not in live_ids:
                if await _remove_container(client, name):
                    removed += 1
                    logger.info(f"[preview] 启动清理：删除无主代理容器 {name}")
        # 反向对账：库里 active 但容器不在了
        stale = db.query(ServicePreview).filter(
            ServicePreview.status == "active",
            ServicePreview.proxy_container.isnot(None),
        ).all()
        for row in stale:
            if row.proxy_container not in found_names:
                row.status = "expired"
                row.detail = "代理容器已不存在（宿主或后端重启）"
                row.proxy_container = None
                row.host_port = None
                row.last_seen_at = datetime.now()
                removed += 1
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[preview] 启动清理代理容器失败: {e}")
    finally:
        db.close()
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    return removed


async def reap_expired() -> int:
    """周期清理：TTL 到期、或已失效且过了宽限期的预览，摘 sidecar 释放端口。"""
    if not preview_enabled():
        return 0
    from app.database import SessionLocal

    grace = int(getattr(settings, "PREVIEW_EXPIRED_GRACE_MINUTES", 30) or 0)
    ttl = int(getattr(settings, "PREVIEW_MAX_TTL_MINUTES", 120) or 120)
    now = datetime.now()
    db = SessionLocal()
    freed = 0
    client = None
    try:
        import aiodocker
        client = aiodocker.Docker()
        rows = db.query(ServicePreview).filter(
            ServicePreview.proxy_container.isnot(None),
        ).all()
        for row in rows:
            if row.status == "active":
                if not (row.expires_at and _naive(row.expires_at) < now):
                    continue  # 仍在有效期内，不动
                # TTL 到期：先进宽限期——保留代理容器，让访问者看到"预览已结束"说明页
                # 而不是浏览器的连接被拒；下一轮再摘容器、释放宿主端口
                row.status = "expired"
                row.detail = f"预览已到期（上限 {ttl} 分钟）"
                row.last_seen_at = now
                continue
            # 已失效：宽限期过了才真正摘掉代理容器
            if row.last_seen_at and _naive(row.last_seen_at) >= now - timedelta(minutes=grace):
                continue
            if await _remove_container(client, row.proxy_container):
                freed += 1
            row.proxy_container = None
            row.host_port = None
            row.last_seen_at = now
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"[preview] 周期清理失败: {e}")
    finally:
        db.close()
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    if freed:
        logger.info(f"[preview] 周期清理：释放 {freed} 个宿主端口")
    return freed


def _naive(value: datetime) -> datetime:
    """DB 里可能带时区（Postgres timestamptz），统一去掉 tzinfo 再和本地 now 比。"""
    if value.tzinfo is None:
        return value
    return value.astimezone().replace(tzinfo=None)


async def preview_reaper_loop(interval_seconds: int = 300) -> None:
    """常驻任务：周期回收到期预览，释放宿主端口。"""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await reap_expired()
        except (Exception, asyncio.CancelledError) as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"[preview] 清理循环异常: {e}")
