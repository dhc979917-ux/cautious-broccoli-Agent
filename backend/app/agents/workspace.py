"""AgentScope 工作区工厂：按部署模式返回 LocalWorkspace 或 DockerWorkspace。

本地用 LocalWorkspace（工具在主机执行）；服务器用 DockerWorkspace（每会话独立容器，
主机输出目录挂载到容器 /workspace）。工作区同时承担 Offloader 职责：持久化被压缩的
上下文与被截断的工具结果，供 Agent 用 Read/Grep 回查。
"""
import asyncio
import os
import sys
import logging
import shutil
import time
import json
import uuid
from typing import Optional

from agentscope.workspace import DockerWorkspace, LocalWorkspace, WorkspaceBase
from agentscope.workspace._docker._make_dockerfile import CONTAINER_WORKDIR

from app.config import OUTPUT_DIR, settings

logger = logging.getLogger(__name__)

# 沙箱容器 PATH：官方 DockerWorkspace 镜像模板把 MCP 网关 venv 的 bin 置于
# PATH 最前，而技能依赖装在镜像系统 Python 里（docker/Dockerfile.skill），
# 故把系统解释器目录提到最前恢复优先级，venv 仍留在末尾保证网关自身可用。
_SANDBOX_PATH = (
    "/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin"
    ":/root/.agentscope/.venv/bin"
)

# agentscope DockerWorkspace 的沙箱容器命名规则。服务预览要按会话反查沙箱容器、
# 回收器要按容器名反查工作区，两边共用这一处定义，避免名字口径各写一份。
SANDBOX_CONTAINER_PREFIX = "as_ws_"


def sandbox_container_name(workspace_id: str) -> str:
    """工作区 id -> 沙箱容器名。"""
    return f"{SANDBOX_CONTAINER_PREFIX}{workspace_id}"


class HardenedDockerWorkspace(DockerWorkspace):
    """官方 DockerWorkspace 的取消安全加固版。

    官方 _teardown_backend 的每个清理步骤用 except Exception 兜底，但
    CancelledError 是 BaseException，会直接穿透：清理等待期间请求任务被取消
    （客户端断开/超时）时，chown 之后的 kill/删除被跳过，as_ws_* 沙箱容器
    泄漏，异常还会沿调用链冒泡导致已生成回复的请求返回 500。
    这里把每个步骤改为独立容忍 CancelledError，保证容器回收流程必被执行。
    """

    async def _create_and_start_container(self) -> None:
        """按官方流程创建并启动沙箱容器，之后按需附加额外的 Docker 网络。

        官方实现刻意不给沙箱配任何网络/端口（工具调用全走 docker exec），
        这里也不去改它的创建参数——只在容器起来后按需 `network connect`，
        是纯附加动作：WORKSPACE_DOCKER_NETWORK 留空时整段短路，行为与改造前
        完全一致；框架日后改了内部创建逻辑也不会被这份覆写打断。

        典型用途是把沙箱接到后端所在的网络，让后端能直连沙箱里的服务。
        服务预览不依赖它：预览代理容器会自己挂到"沙箱当前所在的那张网"上。
        """
        await super()._create_and_start_container()
        raw = str(getattr(settings, "WORKSPACE_DOCKER_NETWORK", "") or "")
        names = [n.strip() for n in raw.split(",") if n.strip()]
        if not names or self._container is None or self._client is None:
            return
        for name in names:
            try:
                network = await self._client.networks.get(name)
                await network.connect({"Container": self._container.id})
                logger.info(f"[workspace] 沙箱容器已附加网络 {name}")
            except Exception as e:
                # 网络不存在 / 已经附加过，都不该让会话建不起来
                logger.warning(f"[workspace] 沙箱附加网络 {name} 失败（已忽略）: {e}")

    async def _teardown_backend(self) -> None:
        if self._container is not None:
            # Linux 原生 docker 下，容器内 root 进程写出的 bind-mount 文件属主在容器侧；
            # 归还属主便于宿主清理。失败/超时/被取消都容忍，绝不阻塞容器回收。
            if (
                self.host_workdir is not None
                and sys.platform == "linux"
                and self._backend is not None
            ):
                try:
                    await self._backend.exec_shell(
                        [
                            "chown",
                            "-R",
                            f"{os.getuid()}:{os.getgid()}",
                            CONTAINER_WORKDIR,
                        ],
                        timeout=10.0,
                    )
                except (Exception, asyncio.CancelledError):
                    pass
            try:
                await self._container.kill()
            except (Exception, asyncio.CancelledError):
                pass
            try:
                await self._container.delete(force=True)
            except (Exception, asyncio.CancelledError):
                pass
            self._container = None

        if self._client is not None:
            try:
                await self._client.close()
            except (Exception, asyncio.CancelledError):
                pass
            self._client = None


def _session_workdir(user_id: Optional[int], session_id: Optional[int]) -> str:
    if user_id and session_id:
        workdir = str(OUTPUT_DIR / f"user_{user_id}" / "sessions" / f"{session_id}")
    elif user_id:
        workdir = str(OUTPUT_DIR / f"user_{user_id}")
    else:
        workdir = str(OUTPUT_DIR)
    os.makedirs(workdir, exist_ok=True)
    # 预建产出目录：提示词与技能约定"结果写到 outputs/"，但沙箱里这个目录
    # 默认不存在，Agent 首次写文件会 FileNotFoundError 再自己 mkdir（实测踩过）。
    os.makedirs(os.path.join(workdir, "outputs"), exist_ok=True)
    return workdir


def agent_visible_path(path: str, output_dir: str) -> str:
    """把后端侧文件路径翻译成 Agent 实际能访问到的路径。

    docker 模式下后端跑在容器里，会话目录以 bind mount 形式挂到沙箱容器的
    /workspace；直接把后端侧路径（/app/outputs/user_1/sessions/236/...）注入
    提示词，Agent 在沙箱里按它找文件必然 FileNotFoundError（实测踩过：
    Agent 试错三轮后误判成"用户没有上传附件"）。这里统一翻译成容器内路径。

    local 模式（后端与工具同机）原样返回；路径不在本会话目录内也原样返回，
    避免把无关路径改坏。
    """
    if not path or not output_dir:
        return path
    if getattr(settings, "WORKSPACE_BACKEND", "local") != "docker":
        return path
    try:
        rel = os.path.relpath(os.path.normpath(path), os.path.normpath(output_dir))
    except ValueError:
        return path
    if rel == os.curdir or rel.startswith(".."):
        return path
    return "/".join([CONTAINER_WORKDIR.rstrip("/"), *rel.split(os.sep)])


def backend_path_of(path: str, output_dir: str) -> str:
    """``agent_visible_path`` 的逆向：把 Agent 看到的路径翻译回后端可读路径。

    为什么需要：Read/Write/Shell 走 backend（docker 模式下由框架代理进沙箱），
    直接用 ``/workspace/...`` 即可；但后端进程内自己打开文件的场景（例如产物验证
    要跑 node/无头浏览器）拿到的是沙箱内路径，在后端文件系统里不存在，必须映射回
    会话输出目录。两者是同一对挂载关系，正反必须同源，否则路径口径会漂。

    local 模式原样返回；映射结果越出 output_dir（``..`` 逃逸）时返回空串，
    由调用方拒绝，避免验证工具被诱导去读工作目录之外的文件。
    """
    if not path:
        return ""
    # 沙箱侧路径恒为 POSIX 风格，这里只做斜杠归一、不能用 os.path.normpath：
    # Windows 上 normpath 会把 /workspace 变成 \workspace，前缀比较随即失效
    # （实测踩过：docker 档下所有产物验证都被判成越界）。
    posix = str(path).replace("\\", "/")
    container_root = CONTAINER_WORKDIR.rstrip("/")
    if getattr(settings, "WORKSPACE_BACKEND", "local") == "docker" and (
        posix == container_root or posix.startswith(container_root + "/")
    ):
        rel = posix[len(container_root):].strip("/")
        if not rel:
            return os.path.normpath(output_dir)
        target = os.path.normpath(os.path.join(output_dir, *rel.split("/")))
    else:
        target = os.path.normpath(path)
    if not output_dir:
        return target
    try:
        rel_check = os.path.relpath(target, os.path.normpath(output_dir))
    except ValueError:
        return ""
    if rel_check == os.curdir or rel_check.startswith(".."):
        return ""
    return target


def _host_workdir(workdir: str) -> str:
    """把后端侧 outputs 路径映射为宿主机真实路径（沙箱容器 bind mount 源）。

    后端本身跑在容器里时（compose 部署），容器内路径对宿主 Docker daemon 无意义，
    需用 WORKSPACE_HOST_OUTPUT_DIR 给出宿主侧对应目录；留空则原样返回
    （后端直接跑在宿主机上时即正确值）。
    """
    host_root = str(getattr(settings, "WORKSPACE_HOST_OUTPUT_DIR", "") or "").rstrip("/\\")
    if not host_root:
        return workdir
    out_root = str(OUTPUT_DIR)
    try:
        rel = os.path.relpath(workdir, out_root)
    except ValueError:
        return workdir
    if rel == ".":
        return host_root
    if rel.startswith(".."):
        return workdir
    return os.path.join(host_root, *rel.split(os.sep))


def _sandbox_env() -> dict:
    """沙箱容器环境变量：固定 PATH + 按需透传的凭据。

    透传名单由 settings.WORKSPACE_DOCKER_ENV_PASSTHROUGH 显式给出，默认空 ——
    即维持"沙箱拿不到任何后端凭据"的现状，不影响既有行为。技能需要云端
    多模态/OCR 能力时，运维按需把变量名加进名单即可，代码无需再改。
    """
    env = {"PATH": _SANDBOX_PATH}
    raw = str(getattr(settings, "WORKSPACE_DOCKER_ENV_PASSTHROUGH", "") or "")
    for name in (item.strip() for item in raw.split(",")):
        if not name:
            continue
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


def get_workspace(
    user_id: Optional[int],
    session_id: Optional[int],
    agent_type: Optional[str] = None,
    skill_paths: Optional[list] = None,
) -> WorkspaceBase:
    """按 settings.WORKSPACE_BACKEND 返回工作区实例（调用方负责 initialize/close）。

    skill_paths：本次构建装载的技能目录（宿主路径）。docker 模式下优先由
    _seed_skills_on_host 直接拷贝进会话工作目录（即沙箱容器 /workspace 的
    bind mount 源），官方逐技能 add_skill 播种只在拷贝全失败时兜底；
    local 模式技能目录就在主机上，无需播种。
    """
    workdir = _session_workdir(user_id, session_id)
    if getattr(settings, "WORKSPACE_BACKEND", "local") == "docker":
        extra_pip = [
            p.strip()
            for p in str(getattr(settings, "WORKSPACE_DOCKER_EXTRA_PIP", "") or "").split(",")
            if p.strip()
        ]
        # 技能播种（P0 延迟优化）：后端侧直接拷进会话工作目录。该目录经
        # bind mount 成为沙箱容器 /workspace，拷入即容器内可见，替代官方
        # 逐技能 tar 上传+容器内解包的 3 次往返（76 技能约 27s → 本地拷贝）。
        # 部分成功即信任宿主结果（官方 _setup_skills 见 skills/ 非空也跳过）；
        # 全部失败才回退官方逐技能播种（慢但保功能）。
        seed_paths = skill_paths
        if skill_paths is not None:
            ok, _total = _seed_skills_on_host(workdir, skill_paths)
            if ok > 0:
                seed_paths = None
        sandbox_env = _sandbox_env()
        if user_id and session_id and settings.OCR_PROXY_URL:
            from app.services.ocr_gateway import mint_ocr_token
            sandbox_env["OCR_PROXY_URL"] = settings.OCR_PROXY_URL
            sandbox_env["OCR_PROXY_TOKEN"] = mint_ocr_token(user_id, session_id)
        return HardenedDockerWorkspace(
            base_image=getattr(settings, "WORKSPACE_DOCKER_IMAGE", "python:3.11-slim"),
            host_workdir=_host_workdir(workdir),
            extra_pip=extra_pip,
            env=sandbox_env,
            skill_paths=seed_paths,
        )
    return LocalWorkspace(workdir=workdir)


def _seed_skills_on_host(seed_root_workdir: str, skill_paths: list) -> tuple:
    """把技能目录直接拷贝进后端侧 <workdir>/skills/，返回 (成功数, 总数)。

    沙箱容器把会话工作目录 bind mount 到 /workspace，后端侧拷入的
    skills/ 容器内即时可见，替代官方"每技能 tar 上传 + 容器内解包"的
    3 次往返播种（76 技能约 27s），改为一次本地目录拷贝（约 1s）。
    已存在的目录跳过：工作目录在会话内跨轮次持久，播种天然复用。
    例外是带 .seed_version 的平台托管技能：镜像更新后版本对不上就替换会话副本，
    否则老会话会一直跑旧代码，技能修复对已有会话永远不生效。
    """
    skills_root = os.path.join(seed_root_workdir, "skills")
    os.makedirs(skills_root, exist_ok=True)
    manifest_path = os.path.join(seed_root_workdir, '.skill_seed_versions.json')
    try:
        with open(manifest_path, encoding='utf-8') as stream:
            previous = json.load(stream)
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    current = {}
    ok = 0
    t0 = time.monotonic()
    for path in skill_paths:
        name = os.path.basename(os.path.abspath(path))
        target = os.path.join(skills_root, name)
        identity = os.path.abspath(path) + ':' + _managed_skill_version(path)
        if os.path.islink(target):
            raise RuntimeError(f'技能副本路径不可用：{name}')
        if os.path.isdir(target) and previous.get(name) == identity:
            current[name] = identity
            ok += 1
            continue
        staging = os.path.join(skills_root, f'.seed-{uuid.uuid4().hex}')
        backup = os.path.join(skills_root, f'.backup-{uuid.uuid4().hex}')
        try:
            shutil.copytree(path, staging)
            if os.path.exists(target):
                os.replace(target, backup)
            os.replace(staging, target)
            if os.path.isdir(backup):
                shutil.rmtree(backup)
            current[name] = identity
            ok += 1
        except Exception:
            if os.path.isdir(staging):
                shutil.rmtree(staging)
            if os.path.isdir(backup) and not os.path.exists(target):
                os.replace(backup, target)
            raise RuntimeError(f'技能副本同步失败：{name}')
    for name in set(previous) - set(current):
        if name != os.path.basename(name) or name in ('.', '..'):
            continue
        target = os.path.join(skills_root, name)
        if os.path.isdir(target) and not os.path.islink(target):
            shutil.rmtree(target)
    temporary_manifest = manifest_path + '.' + uuid.uuid4().hex
    with open(temporary_manifest, 'w', encoding='utf-8') as stream:
        json.dump(current, stream, ensure_ascii=False)
    os.replace(temporary_manifest, manifest_path)
    logger.info(f"[workspace] 宿主侧技能播种 {ok}/{len(skill_paths)} 耗时 {time.monotonic() - t0:.1f}s")
    return ok, len(skill_paths)


def _managed_skill_version(path: str) -> str:
    """读技能目录里的 .seed_version（只有平台托管技能带），没有则返回空串。"""
    try:
        with open(os.path.join(path, ".seed_version"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _refresh_managed_skill(source: str, target: str) -> bool:
    """版本不一致时用镜像里的托管技能替换会话副本，返回是否替换过。

    只动带 .seed_version 的平台托管技能；用户上传的技能没有该标记，一律不碰。
    替换走"拷到临时目录 -> 旧目录改名让位 -> 新目录就位 -> 删旧目录"，
    任一步失败都回滚，不会留下半个技能目录。
    """
    version = _managed_skill_version(source)
    if not version or _managed_skill_version(target) == version:
        return False
    name = os.path.basename(target)
    staging = f"{target}.new.{os.getpid()}"
    backup = f"{target}.old.{os.getpid()}"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(source, staging)
        shutil.move(target, backup)
        shutil.move(staging, target)
    except Exception as e:
        logger.warning(f"[workspace] 会话内托管技能更新失败 {name}: {e}")
        shutil.rmtree(staging, ignore_errors=True)
        if not os.path.exists(target) and os.path.exists(backup):
            try:
                shutil.move(backup, target)
            except OSError:
                pass
        return False
    shutil.rmtree(backup, ignore_errors=True)
    return True
