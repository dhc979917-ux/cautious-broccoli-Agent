"""Preserve referenced offloads between turns and remove them on session deletion."""

import logging
import shutil
from pathlib import Path

from app.config import OUTPUT_DIR

logger = logging.getLogger(__name__)


def remove_session_offloads(user_id: int, session_id: int) -> None:
    if user_id <= 0 or session_id <= 0:
        return
    output_root = Path(OUTPUT_DIR).resolve()
    session_dir = output_root / f"user_{user_id}" / "sessions" / str(session_id)
    target = session_dir / "sessions" / str(session_id)
    if session_dir.is_symlink() or target.is_symlink():
        return
    if not session_dir.resolve().is_relative_to(output_root):
        return
    if not target.resolve().is_relative_to(session_dir.resolve()):
        return
    if target.is_dir():
        try:
            shutil.rmtree(target)
        except OSError as exc:
            logger.warning("无法清理会话 %s 的 offload: %s", session_id, exc)
