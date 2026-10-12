"""沙箱内服务探测：找出 Agent 在会话沙箱里起的监听端口。

为什么不用 ss/netstat：沙箱镜像（docker/Dockerfile.skill 派生）是精简 Python 镜像，
没有 iproute2/net-tools。这里用一段内联 Python 直接读 /proc —— /proc/net/tcp{,6}
给出监听套接字，/proc/<pid>/fd 的 socket:[inode] 反查出进程，再取 cmdline 与 cwd。
脚本以 `python3 -u -c` 的形式经 docker exec 注入，不依赖镜像里预装任何东西，
也不需要往沙箱里写临时文件。

本模块只做"探测"，不做发布/清理，preview_service 负责后者，两者通过
DetectedService 这个纯数据结构解耦。
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

# 探测超时：沙箱卡死/被 kill 时不能把整轮收尾拖住
_PROBE_TIMEOUT_SECONDS = 12.0
# 输出体积上限：防御性截断，避免异常输出把内存吃掉
_PROBE_OUTPUT_LIMIT = 256 * 1024

# 框架自身的进程（MCP 网关等）装在 /root/.agentscope 下，不是用户服务，
# 探测结果里要滤掉，否则会给用户推一个打不开的"预览"。
_INTERNAL_CMDLINE_MARKERS = ("/root/.agentscope/", "agentscope-mcp")

# 沙箱内脚本：读 /proc 列出所有 TCP LISTEN 套接字及其归属进程，输出一行 JSON。
# 只读不写，任何一步失败都退化为空结果，不让探测把沙箱搞出问题。
_PROBE_SCRIPT = r'''
import glob
import json
import os

LISTEN = "0A"


def _v4(hexaddr):
    try:
        b = bytes.fromhex(hexaddr)
    except ValueError:
        return hexaddr
    if len(b) != 4:
        return hexaddr
    return ".".join(str(x) for x in b[::-1])


def _v6(hexaddr):
    if len(hexaddr) != 32:
        return hexaddr
    if set(hexaddr) == {"0"}:
        return "::"
    if hexaddr == "00000000000000000000000001000000":
        return "::1"
    return hexaddr


def parse(path, family):
    out = []
    try:
        with open(path) as f:
            rows = f.read().splitlines()[1:]
    except OSError:
        return out
    for row in rows:
        p = row.split()
        if len(p) < 10 or p[3] != LISTEN:
            continue
        addr, _sep, port = p[1].rpartition(":")
        try:
            port = int(port, 16)
        except ValueError:
            continue
        out.append({
            "port": port,
            "addr": _v4(addr) if family == "tcp4" else _v6(addr),
            "inode": p[9],
            "family": family,
        })
    return out


def inode_to_pid():
    m = {}
    for fd_dir in glob.glob("/proc/[0-9]*/fd"):
        pid = fd_dir.split("/")[2]
        try:
            names = os.listdir(fd_dir)
        except OSError:
            continue
        for name in names:
            try:
                link = os.readlink(fd_dir + "/" + name)
            except OSError:
                continue
            if link.startswith("socket:[") and link.endswith("]"):
                m.setdefault(link[8:-1], pid)
    return m


def proc_info(pid):
    info = {"pid": int(pid), "command": "", "name": "", "cwd": ""}
    try:
        with open("/proc/%s/cmdline" % pid, "rb") as f:
            raw = f.read()
        info["command"] = " ".join(
            x.decode("utf-8", "replace") for x in raw.split(b"\x00") if x
        )
    except OSError:
        pass
    try:
        with open("/proc/%s/comm" % pid) as f:
            info["name"] = f.read().strip()
    except OSError:
        pass
    try:
        info["cwd"] = os.readlink("/proc/%s/cwd" % pid)
    except OSError:
        pass
    return info


sockets = parse("/proc/net/tcp", "tcp4") + parse("/proc/net/tcp6", "tcp6")
pmap = inode_to_pid() if sockets else {}
cache = {}
result = []
for s in sockets:
    pid = pmap.get(s["inode"])
    if pid and pid not in cache:
        cache[pid] = proc_info(pid)
    info = cache.get(pid) if pid else None
    result.append({
        "port": s["port"],
        "addr": s["addr"],
        "family": s["family"],
        "pid": int(pid) if pid else 0,
        "command": (info or {}).get("command", ""),
        "name": (info or {}).get("name", ""),
        "cwd": (info or {}).get("cwd", ""),
    })
result.sort(key=lambda r: r["port"])
print(json.dumps(result))
'''


@dataclass
class DetectedService:
    """沙箱内一个对外可服务的监听端口（同端口的多条记录已合并）。"""

    port: int
    bind_addr: str = ""          # 合并后最具代表性的绑定地址
    pid: int = 0
    command: str = ""            # 完整启动命令
    process_name: str = ""       # comm，如 python3 / node
    cwd: str = ""                # 进程工作目录（沙箱内路径）
    alive: bool = True           # 是否找到了归属进程（僵尸监听为 False）
    families: List[str] = field(default_factory=list)
    raw_addrs: List[str] = field(default_factory=list)

    @property
    def loopback_only(self) -> bool:
        """只绑了回环地址：代理容器（另一个容器）访问不到，无法对外发布。"""
        addrs = self.raw_addrs or [self.bind_addr]
        return all(a in ("127.0.0.1", "::1", "localhost") for a in addrs if a)

    @property
    def wildcard(self) -> bool:
        """绑了 0.0.0.0 / :: —— 任意网卡可达，是最理想的发布形态。"""
        return any(a in ("0.0.0.0", "::") for a in (self.raw_addrs or [self.bind_addr]))

    def to_dict(self) -> dict:
        return {
            "port": self.port,
            "bind_addr": self.bind_addr,
            "pid": self.pid,
            "command": self.command,
            "process_name": self.process_name,
            "cwd": self.cwd,
            "alive": self.alive,
            "loopback_only": self.loopback_only,
            "wildcard": self.wildcard,
        }


def _is_internal(command: str, cwd: str) -> bool:
    text = f"{command} {cwd}"
    return any(marker in text for marker in _INTERNAL_CMDLINE_MARKERS)


def _pick_bind_addr(addrs: List[str]) -> str:
    """同端口可能同时有 v4/v6、通配/具体地址多条记录，挑一个最能代表可达性的。"""
    for preferred in ("0.0.0.0", "::"):
        if preferred in addrs:
            return preferred
    for addr in addrs:
        if addr not in ("127.0.0.1", "::1"):
            return addr
    return addrs[0] if addrs else ""


def _merge(rows: List[dict]) -> List[DetectedService]:
    grouped: "dict[int, DetectedService]" = {}
    for row in rows:
        port = int(row.get("port") or 0)
        if port <= 0 or port > 65535:
            continue
        command = str(row.get("command") or "")
        cwd = str(row.get("cwd") or "")
        if _is_internal(command, cwd):
            continue
        svc = grouped.get(port)
        if svc is None:
            svc = DetectedService(
                port=port,
                pid=int(row.get("pid") or 0),
                command=command,
                process_name=str(row.get("name") or ""),
                cwd=cwd,
                alive=bool(int(row.get("pid") or 0)),
            )
            grouped[port] = svc
        addr = str(row.get("addr") or "")
        if addr and addr not in svc.raw_addrs:
            svc.raw_addrs.append(addr)
        family = str(row.get("family") or "")
        if family and family not in svc.families:
            svc.families.append(family)
        # 进程信息以"有 pid 的那条"为准（僵尸监听拿不到进程）
        if not svc.pid and row.get("pid"):
            svc.pid = int(row["pid"])
            svc.command = command or svc.command
            svc.cwd = cwd or svc.cwd
            svc.process_name = str(row.get("name") or "") or svc.process_name
            svc.alive = True
    services = list(grouped.values())
    for svc in services:
        svc.bind_addr = _pick_bind_addr(svc.raw_addrs)
    # 通配绑定优先，其次端口小的在前：让用户看到的大概率就是他要的那个服务
    services.sort(key=lambda s: (not s.wildcard, s.loopback_only, s.port))
    return services


def parse_probe_output(payload: str) -> List[DetectedService]:
    """把探测脚本的 JSON 输出解析成 DetectedService 列表（可单测，不碰 docker）。"""
    text = (payload or "").strip()
    if not text:
        return []
    # 脚本可能带出少量 stderr 噪声，只取最后一个 JSON 数组
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        rows = json.loads(text[start:end + 1])
    except (ValueError, TypeError) as e:
        logger.warning(f"[service-probe] 探测输出解析失败: {e}")
        return []
    if not isinstance(rows, list):
        return []
    return _merge([r for r in rows if isinstance(r, dict)])


async def detect_services(container_name: str) -> List[DetectedService]:
    """探测指定沙箱容器里的监听服务。

    容器不存在/已停止/exec 失败一律返回空列表并记 warning：探测是收尾增强项，
    绝不能因为它失败而让本轮对话报错。
    """
    try:
        import aiodocker
    except ImportError:
        logger.debug("[service-probe] aiodocker 不可用，跳过探测")
        return []

    client = None
    try:
        client = aiodocker.Docker()
        container = await client.containers.get(container_name)
        info = await container.show()
        state = (info.get("State") or {})
        if not state.get("Running"):
            logger.info(f"[service-probe] {container_name} 未在运行，跳过探测")
            return []
        # exec/读流的版本差异统一交给 docker_exec（aiodocker 0.27 的 Exec.start()
        # 是同步方法，直接 await 会抛 "object Stream can't be used in 'await' expression"）
        from app.agents.docker_exec import exec_capture
        payload = await exec_capture(
            container,
            ["python3", "-u", "-c", _PROBE_SCRIPT],
            timeout=_PROBE_TIMEOUT_SECONDS,
            limit=_PROBE_OUTPUT_LIMIT,
        )
        services = parse_probe_output(payload)
        logger.info(
            f"[service-probe] {container_name} 探测到 {len(services)} 个监听服务: "
            + ", ".join(f"{s.port}({s.bind_addr})" for s in services[:8])
        )
        return services
    except (Exception, asyncio.CancelledError) as e:
        logger.warning(f"[service-probe] 探测 {container_name} 失败（按无服务处理）: {e}")
        return []
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass


def pick_primary(services: List[DetectedService]) -> Optional[DetectedService]:
    """从探测结果里挑一个最该发布的服务。

    优先级：通配绑定(0.0.0.0/::) > 具体内网地址 > 回环（回环发不出去，但排最后
    是为了让上层能识别出"服务起了但绑错地址"这种情况并给出整改提示）。
    同档里取端口最小的，行为可预期。
    """
    if not services:
        return None
    return services[0]
