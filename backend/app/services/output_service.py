"""技能输出文件服务

职责：管理 outputs 目录的探测逻辑，让 API 层不直接关心文件系统细节。

每个用户有独立的输出子目录（outputs/user_{id}/），避免多用户之间
互相看到或下载对方生成的文件。
"""

import os
import logging
import shutil
import time
import zipfile
from typing import Dict, List, Optional, Tuple

from app.config import OUTPUT_DIR

logger = logging.getLogger(__name__)

# AgentScope 工作区（LocalWorkspace/DockerWorkspace）在自己的 workdir（即会话输出目录）里
# 维护的框架内部文件/目录，不属于用户产出，需在"新增文件"探测中排除：
#   .mcp / 任意点文件      —— 工作区持久化的 MCP/技能索引等元数据
#   data/                 —— 被卸载的多模态文件
#   skills/               —— 工作区内安装的技能子目录
#   sessions/             —— Offloader 持久化的上下文/工具结果
_WORKSPACE_INTERNAL_DIRS = {"data", "skills", "sessions"}

# 平台自己写进会话目录的产物目录（不是 Agent 交付物）：
#   _previews/  —— 服务预览打包（源码 zip + HOW_TO_RUN.md），由 preview_service
#                  单独登记为一条文件记录，不能再被"新增文件"探测重复收走一遍。
_PLATFORM_ARTIFACT_DIRS = {"_previews"}


def _is_workspace_internal(rel: str) -> bool:
    """判断（相对会话输出目录的）路径是否为工作区框架内部文件。

    命中任一条件即视为内部文件：
    - 路径中任一层级以 "." 开头（点文件/点目录，如 .mcp、skills/.skills）；
    - 顶层目录属于工作区保留目录（data/skills/sessions）。
    """
    parts = rel.split("/")
    if any(p.startswith(".") for p in parts):
        return True
    if parts and parts[0] in _WORKSPACE_INTERNAL_DIRS:
        return True
    if parts and parts[0] in _PLATFORM_ARTIFACT_DIRS:
        return True
    return False


def get_user_output_dir(user_id: int) -> str:
    """返回指定用户的输出目录（不存在则创建）"""
    path = OUTPUT_DIR / f"user_{user_id}"
    os.makedirs(path, exist_ok=True)
    return str(path)


def get_session_output_dir(user_id: int, session_id: int) -> str:
    """返回会话专属输出子目录（outputs/user_{id}/sessions/{session_id}/，不存在则创建）

    文件产出按会话物理隔离：任务结束时的目录 diff 只扫本会话目录，
    多会话并发运行也不会把别的会话新写的文件误归属到当前会话。
    """
    path = OUTPUT_DIR / f"user_{user_id}" / "sessions" / f"{session_id}"
    os.makedirs(path, exist_ok=True)
    return str(path)


def _stat_or_none(path: str) -> Optional[Tuple[float, int]]:
    """安全取 (mtime, size)。

    会话目录由沙箱容器内的 root 进程写，宿主侧可能遇到悬空符号链接、被并发删除的
    临时文件、socket/fifo 等非常规文件；os.walk 会把它们列进 files，直接 getmtime
    会抛 OSError 让整个探测（乃至整轮收尾）失败。取不到就当作"不是产物"跳过。
    """
    try:
        if not os.path.isfile(path):
            return None
        st = os.stat(path)
        return st.st_mtime, st.st_size
    except (OSError, ValueError):
        return None


def snapshot_dir(directory: str) -> Dict[str, float]:
    """对目录做一次递归快照：{相对路径: 修改时间}，用于后续对比找出新增/变化的文件。

    技能可能生成嵌套目录（如 projects/xxx/），因此递归扫描，
    键统一为 "/" 分隔的相对路径。
    """
    if not os.path.isdir(directory):
        return {}
    result = {}
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, directory).replace(os.sep, "/")
            if _is_workspace_internal(rel):
                continue
            stat = _stat_or_none(fpath)
            if stat is None:
                continue  # 符号链接/管道/socket/刚被删掉：不是可交付产物，跳过
            result[rel] = stat[0]
    return result


def find_new_files(directory: str, before: Dict[str, float]) -> List[dict]:
    """对比快照，找出本次运行期间新增或内容发生变化的文件

    Args:
        directory: 要扫描的目录
        before: snapshot_dir() 的先前快照

    Returns:
        [{"filename": str, "size": int}, ...]（filename 为 "/" 分隔的相对路径）
    """
    if not os.path.isdir(directory):
        return []
    new_files = []
    for root, _dirs, files in os.walk(directory):
        for fname in files:
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, directory).replace(os.sep, "/")
            if _is_workspace_internal(rel):
                continue
            stat = _stat_or_none(fpath)
            if stat is None:
                continue
            mtime, size = stat
            if rel not in before or mtime > before[rel]:
                new_files.append({"filename": rel, "size": size})
    return new_files


# ==================== 产物打包去重 ====================
# 交付口径（用户明确要求，提示词里也写了权威版本）：本轮产物 <= OUTPUT_ZIP_THRESHOLD
# 个就直接给原文件，超过阈值由**平台**统一打成一个 zip。模型不该自己打包，但实测
# （会话 276）它会被长期记忆里的历史偏好带跑，用 `zip -q -j` 自己打一份，于是用户
# 同时收到 1 个原文件 + 1 个内容完全重复的压缩包。
# 这里做确定性兜底：只丢"内容全部来自本轮其它产物"的自打包，其余一律不动。

def _zip_namelist(path: str) -> List[str]:
    """读取压缩包成员名（只读中央目录，不解压）。"""
    import zipfile

    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


# 支持的自打包格式 -> 成员名读取函数。新增格式（tar/7z）在此登记即可，调用方无需改动。
_ARCHIVE_NAMELISTS = {
    ".zip": _zip_namelist,
}


def _archive_suffix(filename: str) -> str:
    """返回可识别的压缩包扩展名（小写），不是压缩包返回空串。"""
    lower = filename.lower()
    for ext in _ARCHIVE_NAMELISTS:
        if lower.endswith(ext):
            return ext
    return ""


def drop_redundant_archives(directory: str, new_files: List[dict]) -> List[dict]:
    """剔除"完全重复本轮其它产物"的自打包压缩包。

    判定条件（全部满足才丢，任何一条不满足就原样保留）：
      1. 本轮产物里除压缩包外还有别的文件；
      2. 压缩包能正常打开并列出成员；
      3. 每个成员的文件名都能在本轮其它产物里找到同名文件（目录成员忽略）。

    这样：用户明确要压缩包、模型只交了压缩包（条件 1 不满足）→ 保留；压缩包里含
    历史文件/外部素材（条件 3 不满足）→ 保留；读不出成员（条件 2 不满足）→ 保留。
    丢掉的只是"原文件 + 同内容压缩包"这种重复交付。

    Args:
        directory: 产物根目录（与 find_new_files 的入参一致）
        new_files: find_new_files() 的结果

    Returns:
        过滤后的新列表（不修改入参）；无需过滤时内容与入参一致。
    """
    if not new_files:
        return new_files
    archives = [f for f in new_files if _archive_suffix(f["filename"])]
    if not archives:
        return new_files
    others = [f for f in new_files if not _archive_suffix(f["filename"])]
    if not others:
        return new_files  # 本轮只交了压缩包：那就是用户要的产物，不动

    other_names = {os.path.basename(f["filename"]) for f in others}
    dropped = []
    for arc in archives:
        ext = _archive_suffix(arc["filename"])
        path = os.path.join(directory, *arc["filename"].split("/"))
        try:
            members = _ARCHIVE_NAMELISTS[ext](path)
        except Exception:
            continue  # 打不开/不是合法压缩包：交给用户，别替它做主
        names = [m for m in members if not m.endswith("/")]
        if not names:
            continue
        if all(os.path.basename(m) in other_names for m in names):
            dropped.append(arc["filename"])
    if not dropped:
        return new_files
    logger.info(
        "[产出打包] 丢弃模型自打的重复压缩包 %d 个: %s（原文件已在本轮产物中）",
        len(dropped), ", ".join(dropped),
    )
    return [f for f in new_files if f["filename"] not in set(dropped)]


# ==================== 平台统一打包 ====================
# ZIP 格式的时间戳下限是 1980-01-01。沙箱里由 Write/Edit 工具落盘的文件，mtime
# 可能是 0（框架用 tarfile 传文件时没带 mtime），zipfile.write() 遇到就抛
# "ValueError: ZIP does not support timestamps before 1980"，整包失败 ->
# 上层回退单文件模式，几十上百个碎文件把文件中心淹掉（实测会话 364 登记了 54 条）。
# 因此这里统一：ZipInfo + 流式写入 + 时间戳夹到 1980 以后 + 单文件容错。

PACKAGING_ERRORS_NAME = "PACKAGING_ERRORS.txt"
_MIN_ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_COPY_CHUNK = 1024 * 1024


def _zip_date_time(path: Optional[str] = None) -> tuple:
    """返回合法的 ZIP 时间戳（早于 1980 一律夹到 1980-01-01）。"""
    stamp = None
    if path:
        try:
            stamp = time.localtime(os.path.getmtime(path))
        except (OSError, ValueError, OverflowError):
            stamp = None
    if stamp is None:
        stamp = time.localtime()
    candidate = (stamp.tm_year, stamp.tm_mon, stamp.tm_mday,
                 stamp.tm_hour, stamp.tm_min, stamp.tm_sec)
    return candidate if candidate >= _MIN_ZIP_DATE_TIME else _MIN_ZIP_DATE_TIME


def _zip_info(arcname: str, date_time: tuple) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(arcname, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    # 沙箱内 root 写出的文件常是 0600，解压到用户机器后不好处理，统一按 0644 落
    info.external_attr = 0o644 << 16
    return info


def build_zip(
    entries: List[Tuple[str, str]],
    zip_path: str,
    extra_texts: Optional[Dict[str, str]] = None,
) -> Tuple[int, List[str]]:
    """把 (磁盘路径, 包内相对路径) 列表打成一个 zip，返回 (成功数, 错误列表)。

    - 原子落盘：先写 ``*.zip.part``，全部成功后 ``os.replace`` 覆盖目标，
      任何一步失败都不会在会话目录里留下半个压缩包（历史上留下的孤儿
      outputs_*.zip 没有对应 FileRecord，永远不会被清理）。
    - 单文件容错：某个文件读不到/写不进，只记一条错误继续下一个；
      错误清单以 PACKAGING_ERRORS.txt 一并放进包里，用户能看到少了什么。
    - 整包失败（一个都没打进去）时删掉 .part 并返回 (0, errors)，由调用方决定回退。
    """
    errors: List[str] = []
    packed = 0
    part_path = f"{zip_path}.part"
    parent = os.path.dirname(zip_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    try:
        with zipfile.ZipFile(part_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for src, arcname in entries:
                try:
                    if not src or not os.path.isfile(src):
                        errors.append(f"{arcname}: 文件不存在或不是常规文件")
                        continue
                    info = _zip_info(arcname, _zip_date_time(src))
                    with zf.open(info, "w") as dst, open(src, "rb") as fh:
                        shutil.copyfileobj(fh, dst, _COPY_CHUNK)
                    packed += 1
                except (OSError, ValueError, zipfile.BadZipFile) as e:
                    errors.append(f"{arcname}: 打包失败 {e}")
            for arcname, text in (extra_texts or {}).items():
                try:
                    zf.writestr(
                        _zip_info(arcname, _zip_date_time()),
                        (text or "").encode("utf-8"),
                    )
                    packed += 1
                except (OSError, ValueError, zipfile.BadZipFile) as e:
                    errors.append(f"{arcname}: 写入失败 {e}")
            if errors:
                zf.writestr(
                    _zip_info(PACKAGING_ERRORS_NAME, _zip_date_time()),
                    ("以下文件未能打进压缩包：\n" + "\n".join(errors) + "\n").encode("utf-8"),
                )
        if packed == 0:
            raise RuntimeError("没有任何文件打包成功")
        os.replace(part_path, zip_path)
    except Exception as e:  # 整包失败：清掉半成品，交给调用方回退
        errors.append(f"<zip>: 整包失败 {e}")
        try:
            os.remove(part_path)
        except OSError:
            pass
        return 0, errors
    if errors:
        logger.warning("[产出打包] %d 个文件跳过（详见包内 %s）: %s",
                       len(errors), PACKAGING_ERRORS_NAME, "; ".join(errors[:5]))
    return packed, errors


def build_output_zip(
    output_dir: str,
    files: List[dict],
    zip_name: str,
) -> Tuple[Optional[dict], List[str]]:
    """把本轮产物打成单个 zip，返回 (zip_item 或 None, 错误列表)。

    zip_item = {"filename": zip_name, "size": bytes}，与 find_new_files 的元素同构，
    调用方可直接替换 new_files。返回 None 表示整包失败，调用方应回退单文件模式。
    """
    entries: List[Tuple[str, str]] = []
    for item in files or []:
        rel = str(item.get("filename") or "").replace("\\", "/").lstrip("/")
        if not rel:
            continue
        entries.append((os.path.join(output_dir, *rel.split("/")), rel))
    if not entries:
        return None, ["<zip>: 没有可打包的文件"]
    zip_path = os.path.join(output_dir, zip_name)
    packed, errors = build_zip(entries, zip_path)
    if packed == 0:
        return None, errors
    try:
        size = os.path.getsize(zip_path)
    except OSError as e:
        return None, errors + [f"<zip>: 读取产物大小失败 {e}"]
    return {"filename": zip_name, "size": size}, errors
