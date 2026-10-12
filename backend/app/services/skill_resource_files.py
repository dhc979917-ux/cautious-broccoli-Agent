"""Bounded, immutable resource snapshots and safe file access."""

import hashlib
import os
import shutil
from pathlib import Path, PurePosixPath

from fastapi import HTTPException


TEXT_LIMIT = 2 * 1024 * 1024
TOTAL_LIMIT = 200 * 1024 * 1024
FILE_LIMIT = 2000
IMAGE_TYPES = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
               '.gif': 'image/gif', '.webp': 'image/webp'}


def relative_path(value, allow_root=False):
    if not value and allow_root:
        return PurePosixPath('.')
    if not value or '\\' in value or '\x00' in value or ':' in value:
        raise HTTPException(422, '文件路径无效')
    parts = value.split('/')
    if any(part in ('', '.', '..') for part in parts) or PurePosixPath(value).is_absolute():
        raise HTTPException(422, '文件路径超出技能目录')
    return PurePosixPath(value)


def safe_file(root, value):
    relative = relative_path(value)
    candidate = root.joinpath(*relative.parts)
    if any(part.is_symlink() for part in (candidate, *candidate.parents) if part.is_relative_to(root)):
        raise HTTPException(409, '技能文件不允许符号链接')
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
        raise HTTPException(404, '文件不存在')
    return resolved


def manifest(root):
    entries = []
    total = 0
    for path in sorted(root.rglob('*'), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise HTTPException(409, '技能内容包含符号链接')
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        relative_path(relative)
        before = path.stat()
        data = path.read_bytes()
        after = path.stat()
        if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
            raise HTTPException(409, '文件正在被其他操作修改，请重试')
        total += len(data)
        if total > TOTAL_LIMIT or len(entries) >= FILE_LIMIT:
            raise HTTPException(422, '技能内容超过200MiB或2000个文件限制')
        entries.append({'path': relative, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)})
    return entries


def copy_tree(source, destination):
    expected = manifest(source)
    shutil.copytree(source, destination, dirs_exist_ok=True)
    if manifest(destination) != expected:
        raise HTTPException(409, '文件复制时发生变化，原版本保持不变')
    return expected


def durable_tree(root):
    for path in root.rglob('*'):
        if path.is_file():
            with path.open('r+b') as stream:
                os.fsync(stream.fileno())
    for directory in sorted((item for item in root.rglob('*') if item.is_dir()), reverse=True):
        durable_directory(directory)
    durable_directory(root)


def durable_directory(directory):
    if os.name != 'posix':
        return
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def preview(root, value):
    path = safe_file(root, value)
    data = path.read_bytes()
    result = {'path': value, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
              'kind': 'binary', 'content': None, 'editable': False}
    if path.suffix.lower() in IMAGE_TYPES and len(data) <= 10 * 1024 * 1024:
        result['kind'] = 'image'
    elif len(data) <= TEXT_LIMIT and b'\x00' not in data:
        try:
            result.update(kind='text', content=data.decode('utf-8'), editable=True)
        except UnicodeError:
            pass
    return result


def download_type(path, inline=False):
    if inline:
        return IMAGE_TYPES.get(path.suffix.lower(), 'application/octet-stream')
    return 'application/octet-stream'
