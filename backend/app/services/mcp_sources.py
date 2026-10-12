"""Read only registered Python sources, without executing MCP server modules."""

import ast
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from app.models.tool_governance import McpToolCatalog


MAX_BYTES = 1_000_000
ROOTS = (Path(__file__).resolve().parents[1] / "agents" / "mcp_servers",
         Path(__file__).resolve().parents[2] / "mcp_sources")


def _local(path):
    candidate = Path(path).resolve()
    if candidate.suffix != ".py" or not any(candidate.is_relative_to(root.resolve()) for root in ROOTS):
        raise HTTPException(403, "源码不在受控的 Python 源码目录中")
    if not candidate.is_file():
        return None
    if candidate.stat().st_size > MAX_BYTES:
        raise HTTPException(413, "源码文件超过预览大小限制")
    return candidate.read_text(encoding="utf-8"), candidate.name


def _remote(url):
    try:
        reviewed = json.loads(os.environ.get("MCP_SOURCE_REVIEWED_URLS", "[]"))
    except ValueError:
        reviewed = []
    parsed = urlsplit(url)
    if url not in reviewed or parsed.scheme != "https" or parsed.username or parsed.password or (
            parsed.query or parsed.fragment or not parsed.path.endswith(".py")):
        raise HTTPException(403, "远程源码地址未经审核")
    with httpx.Client(timeout=10, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > MAX_BYTES:
                    raise HTTPException(413, "源码文件超过预览大小限制")
    return content.decode("utf-8"), Path(parsed.path).name


def _function_line(content, name):
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = [node.name]
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call):
                names.extend(keyword.value.value for keyword in decorator.keywords
                             if keyword.arg == "name" and isinstance(keyword.value, ast.Constant))
        if name in names:
            return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
    return None


def tool_source(db, server, name):
    if not db.query(McpToolCatalog).filter_by(server_id=server.id, name=name).first():
        raise HTTPException(404, "工具不存在")
    mapping = (server.source_files or {}).get(name) or (server.source_files or {}).get("*")
    if mapping is None and server.type == "http":
        manifest_path = ROOTS[1] / "manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = manifest.get(server.url)
            if entry:
                source_path = ROOTS[1] / entry["filename"]
                if source_path.is_file() and hashlib.sha256(source_path.read_bytes()).hexdigest() == entry["sha256"]:
                    mapping = {"path": str(source_path), "origin": entry["origin"]}
    candidates = []
    if isinstance(mapping, dict):
        candidates.append((mapping.get("path"), mapping.get("url"), mapping.get("origin")))
    if server.type == "stdio":
        try:
            args = json.loads(server.args_json or "[]")
        except ValueError:
            args = []
        candidates.extend((value, None, "本地 MCP 源文件") for value in args
                          if isinstance(value, str) and value.endswith(".py"))
    if not candidates:
        return {"available": False, "reason": "此 MCP 未提供可获取的 Python 源码，无法预览。"}
    for path, url, origin in candidates:
        try:
            result = _local(path) if path else _remote(url) if url else None
        except HTTPException as exc:
            if mapping:
                raise
            result = None
        except (httpx.HTTPError, UnicodeError, OSError):
            result = None
        if result:
            content, filename = result
            line = _function_line(content, name)
            if line is not None:
                return {"available": True, "filename": filename, "content": content,
                        "line": line, "origin": origin or "已审核的远程源码"}
    return {"available": False, "reason": "未取得可定位此工具的 Python 源码；不会生成或伪造源码。"}
