"""一次性清理脚本：删掉没有文件中心记录的孤儿产物压缩包。

背景：产出打包此前用 zipfile.write() 直接写目标路径，遇到 mtime 早于 1980 的文件
（沙箱内 Write/Edit 工具落盘的常见情况）会在写到一半时抛异常，留下一个
outputs_*.zip 半成品；由于整包失败，上层回退单文件模式，这个半成品从来没有对应的
files 表记录，既不会被下载也不会被 _cleanup_session_files 清理，只会一直占盘。
打包逻辑已改为"先写 .part 再原子替换"，不再产生新的半成品，本脚本负责清历史存量。

判定为孤儿的条件（全部满足才删）：
  1. 路径形如 outputs/user_*/sessions/*/outputs_*.zip 或 .../_previews/*.zip；
  2. files 表里没有 kind=output 且 stored_name 与之对应的记录；
  3. 不是 *.zip.part（.part 一律删，正常流程不会留下它）。

用法（在 backend 目录下）：
    python migrate_cleanup_orphan_output_zips_v1.py            # 只列不删
    python migrate_cleanup_orphan_output_zips_v1.py --execute  # 真正删除
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _collect_candidates(output_root: str):
    """遍历 outputs/user_*/sessions/*/ 下的压缩包，产出 (绝对路径, stored_name)。"""
    if not os.path.isdir(output_root):
        return
    for user_name in sorted(os.listdir(output_root)):
        if not user_name.startswith("user_"):
            continue
        sessions_dir = os.path.join(output_root, user_name, "sessions")
        if not os.path.isdir(sessions_dir):
            continue
        for sid in sorted(os.listdir(sessions_dir)):
            sess_dir = os.path.join(sessions_dir, sid)
            if not os.path.isdir(sess_dir):
                continue
            for base, _dirs, files in os.walk(sess_dir):
                for fname in files:
                    if not fname.lower().endswith(".zip") and not fname.endswith(".zip.part"):
                        continue
                    if not (fname.startswith("outputs_") or fname.startswith("preview_")):
                        continue
                    full = os.path.join(base, fname)
                    rel = os.path.relpath(full, sess_dir).replace(os.sep, "/")
                    yield full, f"sessions/{sid}/{rel}"


def main():
    execute = "--execute" in sys.argv
    from app.config import OUTPUT_DIR
    from app.database import SessionLocal
    from app.models.file import FileRecord

    output_root = str(OUTPUT_DIR)
    db = SessionLocal()
    removed = kept = 0
    try:
        for full, stored_name in _collect_candidates(output_root):
            rec = db.query(FileRecord).filter(
                FileRecord.kind == "output",
                FileRecord.stored_name == stored_name,
            ).first()
            is_part = full.endswith(".part")
            if rec is not None and not is_part:
                kept += 1
                continue
            try:
                size = os.path.getsize(full)
            except OSError:
                size = -1
            reason = "半成品 .part" if is_part else "无 files 记录"
            print(f"[orphan-zip] {reason} {size:>12} B  {full}")
            if execute:
                try:
                    os.remove(full)
                    removed += 1
                except OSError as e:
                    print(f"[orphan-zip] 删除失败 {full}: {e}")
            else:
                removed += 1
    finally:
        db.close()
    verb = "已删除" if execute else "待删除（加 --execute 真正执行）"
    print(f"[orphan-zip] 孤儿压缩包 {removed} 个{verb}；有记录保留 {kept} 个")


if __name__ == "__main__":
    main()
