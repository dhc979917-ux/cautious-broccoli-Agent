"""产出打包回归测试：mtime 早于 1980 的文件不能让整包失败。

背景：沙箱内 Write/Edit 工具落盘的文件 mtime 可能是 0（框架用 tarfile 传文件时
没带 mtime），zipfile.write() 遇到就抛 "ZIP does not support timestamps before 1980"，
整包失败后上层回退单文件模式，几十个碎文件把文件中心淹掉，还会留下没有
FileRecord 的半个 zip（永远没人清理）。

运行（仓库根目录）：
    python test/_test_output_zip_mtime.py
"""
import io
import os
import shutil
import sys
import tempfile
import zipfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.services.output_service import (  # noqa: E402
    PACKAGING_ERRORS_NAME,
    build_output_zip,
    build_zip,
    find_new_files,
    snapshot_dir,
)

FAILED = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}{(' — ' + extra) if extra else ''}")
    if not cond:
        FAILED.append(name)


def main():
    # 不用 tempfile.mkdtemp：它按 0700 建目录，在受限沙箱里后续写文件会被拒；
    # 这里用固定前缀 + pid 自建目录，行为在各环境一致，跑完照样整目录删掉
    root = os.path.join(tempfile.gettempdir(), f"tp_zip_mtime_{os.getpid()}")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    try:
        # 1) 正常文件 + 一个 mtime=0 的文件（旧实现整包炸掉就是它）
        for i in range(5):
            with open(os.path.join(root, f"f{i}.txt"), "w", encoding="utf-8") as fh:
                fh.write(f"content-{i}\n" * 10)
        epoch_path = os.path.join(root, "epoch.txt")
        # newline="" 关掉 Windows 的 \r\n 翻译，保证跨平台断言一致
        with open(epoch_path, "w", encoding="utf-8", newline="") as fh:
            fh.write("mtime is zero\n")
        os.utime(epoch_path, (0, 0))
        check("前置：epoch.txt 的 mtime 确实早于 1980",
              os.path.getmtime(epoch_path) < 315532800,
              f"mtime={os.path.getmtime(epoch_path)}")

        files = [{"filename": f"f{i}.txt", "size": 10} for i in range(5)]
        files.append({"filename": "epoch.txt", "size": 15})
        item, errors = build_output_zip(root, files, "outputs_test.zip")

        check("整包成功（不回退单文件模式）", item is not None, f"errors={errors}")
        if item is None:
            return
        zip_path = os.path.join(root, "outputs_test.zip")
        check("zip 文件确实落盘且大小与返回值一致",
              os.path.isfile(zip_path) and os.path.getsize(zip_path) == item["size"])
        check("没有留下 .part 半成品", not os.path.exists(zip_path + ".part"))

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            check("6 个文件全部打进包里", len(names) == 6, f"names={names}")
            check("包内没有 PACKAGING_ERRORS.txt", PACKAGING_ERRORS_NAME not in names)
            check("epoch.txt 内容完好", zf.read("epoch.txt").decode() == "mtime is zero\n")
            infos = {i.filename: i for i in zf.infolist()}
            check("epoch.txt 的时间戳被夹到 1980 之后",
                  infos["epoch.txt"].date_time[0] >= 1980,
                  f"date_time={infos['epoch.txt'].date_time}")
            bad = zf.testzip()
            check("压缩包完整性校验通过", bad is None, f"bad={bad}")

        # 2) 单个文件读不到：只跳过它，其余照常打包，并写 PACKAGING_ERRORS.txt
        files2 = [{"filename": f"f{i}.txt", "size": 10} for i in range(5)]
        files2.append({"filename": "gone.txt", "size": 1})
        item2, errors2 = build_output_zip(root, files2, "outputs_partial.zip")
        check("部分失败仍出包", item2 is not None, f"errors={errors2}")
        with zipfile.ZipFile(os.path.join(root, "outputs_partial.zip")) as zf:
            names2 = zf.namelist()
            check("缺失文件被跳过", "gone.txt" not in names2)
            check("生成了 PACKAGING_ERRORS.txt", PACKAGING_ERRORS_NAME in names2)
            check("错误清单里点名了缺失文件", "gone.txt" in zf.read(PACKAGING_ERRORS_NAME).decode())

        # 3) 全部失败：返回 None，让调用方回退单文件模式，且不残留 .part
        item3, errors3 = build_output_zip(root, [{"filename": "nope.txt", "size": 1}], "outputs_none.zip")
        check("整包失败返回 None", item3 is None, f"errors={errors3}")
        check("整包失败不残留 zip", not os.path.exists(os.path.join(root, "outputs_none.zip")))
        check("整包失败不残留 .part", not os.path.exists(os.path.join(root, "outputs_none.zip.part")))

        # 4) 探测函数对非常规文件/悬空链接免疫
        #    Windows 建符号链接需要管理员特权，建不出来就跳过这一项（Linux/容器里必过）
        try:
            os.symlink(os.path.join(root, "missing-target"), os.path.join(root, "dangling.lnk"))
        except (OSError, NotImplementedError, AttributeError) as e:
            print(f"SKIP  悬空链接用例（当前系统建不了符号链接：{e}）")
        try:
            snap = snapshot_dir(root)
            new = find_new_files(root, {})
            check("snapshot_dir 不因悬空链接抛异常", isinstance(snap, dict))
            check("find_new_files 不因悬空链接抛异常", isinstance(new, list))
            if os.path.lexists(os.path.join(root, "dangling.lnk")):
                check("悬空链接不计入产物",
                      all(f["filename"] != "dangling.lnk" for f in new))
        except OSError as e:
            check("探测函数容忍悬空链接", False, str(e))

        # 5) 平台产物目录 _previews/ 不参与"新增文件"探测（避免源码包被登记两次）
        os.makedirs(os.path.join(root, "_previews"), exist_ok=True)
        with open(os.path.join(root, "_previews", "service_8000_1.zip"), "wb") as fh:
            fh.write(b"PK\x05\x06" + b"\x00" * 18)
        new2 = find_new_files(root, snap)
        check("_previews/ 下的源码包不被当作新增产物",
              all(not f["filename"].startswith("_previews/") for f in new2),
              f"new={[f['filename'] for f in new2]}")

        # 6) build_zip 的 extra_texts（HOW_TO_RUN.md 走的就是这条）
        out_zip = os.path.join(root, "with_text.zip")
        packed, errs = build_zip(
            [(os.path.join(root, "f0.txt"), "f0.txt")],
            out_zip,
            extra_texts={"HOW_TO_RUN.md": "# 说明\n中文内容\n"},
        )
        check("build_zip 支持内嵌文本成员", packed == 2 and not errs, f"packed={packed} errs={errs}")
        with zipfile.ZipFile(out_zip) as zf:
            check("内嵌文本内容正确",
                  zf.read("HOW_TO_RUN.md").decode("utf-8") == "# 说明\n中文内容\n")
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILED:
        print(f"共 {len(FAILED)} 项失败: {FAILED}")
        sys.exit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
