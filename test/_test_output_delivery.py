"""产物交付口径回归验证（离线：不连模型、不连库、不起服务）。

背景（2026-09-10 会话 276 实测）：本轮只产出 1 个 pptx，用户却同时收到原文件和
一个内容完全重复的 zip —— 不是平台打的（平台逻辑是 >OUTPUT_ZIP_THRESHOLD 才打包，
包名 outputs_<ts>.zip），是模型自己 `zip -q -j` 打的，动因来自长期记忆里的历史偏好。
本次改动：
  - 提示词里写死权威交付口径（阈值与 chat.py 共用 settings.OUTPUT_ZIP_THRESHOLD）；
  - output_service.drop_redundant_archives 做确定性兜底，只剔"完全重复本轮其它产物"
    的自打包；chat.py 在阈值打包之前调用它，计数才准。

用法（任意目录）：
    python test/_test_output_delivery.py

覆盖三组：
  1. 兜底过滤：重复 zip 被剔 / 只交 zip 时保留 / 含外部成员时保留 / 坏包保留 /
     不修改入参 / 多包只剔重复的那个 / 非登记格式不动。
  2. 提示词：阈值取自 settings 且随配置变化 / 明确禁止自打包 / 保留用户显式要包的例外。
  3. 端到端（真实临时目录 + 真实 snapshot/find_new_files）：复刻 chat.py 的产出登记
     流水线，<=阈值 只交原文件、>阈值 由平台打一个包、自打 zip 两种情况都不出现。
"""
import os
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.normpath(os.path.join(HERE, "..", "backend"))
os.chdir(BACKEND_DIR)
sys.path.insert(0, BACKEND_DIR)

from app.config import settings  # noqa: E402
from app.services.output_service import (  # noqa: E402
    drop_redundant_archives,
    find_new_files,
    snapshot_dir,
)

results = []


def check(name, ok, extra=""):
    results.append((name, bool(ok)))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {name}{(' -> ' + str(extra)) if extra else ''}")


def mk_zip(path: str, members: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, body in members.items():
            zf.writestr(name, body)


def write(path: str, body: str = "x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def names(files) -> list:
    return sorted(f["filename"] for f in files)


def mk_root(name: str) -> str:
    """在仓库 test/ 下建一个干净的临时根目录（沙箱只放开工作区写入，系统 Temp 不可用）。"""
    root = os.path.join(HERE, name)
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    return root


# ==================== 1. 兜底过滤 ====================
def group_filter():
    print("\n[1] drop_redundant_archives")
    root = mk_root("_tmp_out")
    try:
        # 会话 276 的原始症状：1 个原文件 + 1 个内容重复的自打 zip
        write(os.path.join(root, "outputs", "deck.pptx"), "pptx")
        mk_zip(os.path.join(root, "outputs", "产物.zip"), {"deck.pptx": "pptx"})
        new_files = find_new_files(root, {})
        kept = drop_redundant_archives(root, new_files)
        check("重复自打 zip 被剔除，原文件保留",
              names(kept) == ["outputs/deck.pptx"], names(kept))
        check("不修改入参列表", len(new_files) == 2, names(new_files))

        # 只交了压缩包（用户明确要包）：不动
        root2 = os.path.join(root, "only_zip")
        mk_zip(os.path.join(root2, "deliver.zip"), {"a.txt": "a"})
        only = find_new_files(root2, {})
        check("本轮只交压缩包时保留（用户明确要包）",
              names(drop_redundant_archives(root2, only)) == ["deliver.zip"],
              names(drop_redundant_archives(root2, only)))

        # 压缩包里含本轮没有的成员（历史文件/外部素材）：不动
        root3 = os.path.join(root, "mixed")
        write(os.path.join(root3, "new.txt"), "n")
        mk_zip(os.path.join(root3, "bundle.zip"), {"new.txt": "n", "old_from_prev_round.txt": "o"})
        mixed = find_new_files(root3, {})
        check("压缩包含本轮之外成员时保留",
              names(drop_redundant_archives(root3, mixed)) == ["bundle.zip", "new.txt"],
              names(drop_redundant_archives(root3, mixed)))

        # 坏包（读不出成员）：不动，交给用户
        root4 = os.path.join(root, "broken")
        write(os.path.join(root4, "a.txt"), "a")
        write(os.path.join(root4, "broken.zip"), "not a zip at all")
        broken = find_new_files(root4, {})
        check("损坏的压缩包保留（不替用户做主）",
              names(drop_redundant_archives(root4, broken)) == ["a.txt", "broken.zip"],
              names(drop_redundant_archives(root4, broken)))

        # 多个包：只剔重复的那个
        root5 = os.path.join(root, "multi")
        write(os.path.join(root5, "a.txt"), "a")
        write(os.path.join(root5, "b.txt"), "b")
        mk_zip(os.path.join(root5, "dup.zip"), {"a.txt": "a", "b.txt": "b"})
        mk_zip(os.path.join(root5, "real.zip"), {"a.txt": "a", "external.dat": "e"})
        multi = find_new_files(root5, {})
        check("多个压缩包只剔完全重复的那个",
              names(drop_redundant_archives(root5, multi))
              == ["a.txt", "b.txt", "real.zip"],
              names(drop_redundant_archives(root5, multi)))

        # 嵌套目录成员：按 basename 比对（模型常用 zip -j 拍平）
        root6 = os.path.join(root, "nested")
        write(os.path.join(root6, "sub", "gen.py"), "p")
        mk_zip(os.path.join(root6, "flat.zip"), {"gen.py": "p"})
        nested = find_new_files(root6, {})
        check("zip -j 拍平后的成员按文件名也能识别为重复",
              names(drop_redundant_archives(root6, nested)) == ["sub/gen.py"],
              names(drop_redundant_archives(root6, nested)))

        # 非登记格式（tar.gz）不在识别范围内：保持原样，不做臆测
        root7 = os.path.join(root, "tarball")
        write(os.path.join(root7, "a.txt"), "a")
        write(os.path.join(root7, "a.tar.gz"), "tar")
        tar = find_new_files(root7, {})
        check("未登记的压缩格式不动（只在 _ARCHIVE_NAMELISTS 里扩展）",
              names(drop_redundant_archives(root7, tar)) == ["a.tar.gz", "a.txt"],
              names(drop_redundant_archives(root7, tar)))

        # 边界：空列表 / 无压缩包
        check("空产物列表原样返回", drop_redundant_archives(root, []) == [])
        plain = [{"filename": "a.txt", "size": 1}]
        check("无压缩包时原样返回", drop_redundant_archives(root, plain) == plain)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ==================== 2. 提示词口径 ====================
def group_prompt():
    print("\n[2] 提示词交付口径")
    from app.agents.architectures.base import AgentBuilderBase

    threshold = int(getattr(settings, "OUTPUT_ZIP_THRESHOLD", 3))
    hint = AgentBuilderBase.default_file_hint("/workspace/outputs")
    check("提示词含交付口径章节", "产物交付口径" in hint)
    check("提示词阈值与 settings.OUTPUT_ZIP_THRESHOLD 一致",
          f"≤ {threshold} 个" in hint and f"> {threshold} 个" in hint, threshold)
    check("提示词明确禁止自己打包",
          "不要**自己执行 zip/tar/Compress-Archive 打包" in hint)
    check("提示词说明超阈值由平台自动打包", "平台会自动打成一个压缩包" in hint)
    check("提示词保留用户显式要压缩包的例外",
          "唯一例外" in hint and "只交付压缩包" in hint)

    # 阈值改动后提示词跟着变（口径不会与 chat.py 漂移）
    original = settings.OUTPUT_ZIP_THRESHOLD
    try:
        settings.OUTPUT_ZIP_THRESHOLD = 5
        hint5 = AgentBuilderBase.default_file_hint("/workspace/outputs")
        check("阈值可配置：提示词随 OUTPUT_ZIP_THRESHOLD 变化",
              "≤ 5 个" in hint5 and "≤ 3 个" not in hint5)
    finally:
        settings.OUTPUT_ZIP_THRESHOLD = original
    check("阈值配置已还原", int(settings.OUTPUT_ZIP_THRESHOLD) == threshold)


# ==================== 3. 端到端：复刻 chat.py 的产出登记流水线 ====================
def deliver(output_dir: str, before: dict) -> list:
    """与 api/v1/chat.py:_detect_and_register_outputs 的取舍顺序一致（不含落库/上传）。"""
    new_files = find_new_files(output_dir, before)
    if not new_files:
        return []
    new_files = drop_redundant_archives(output_dir, new_files)
    if not new_files:
        return []
    threshold = getattr(settings, "OUTPUT_ZIP_THRESHOLD", 3)
    if len(new_files) > threshold:
        zip_name = "outputs_platform.zip"
        zip_path = os.path.join(output_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in new_files:
                src = os.path.join(output_dir, f["filename"])
                if os.path.isfile(src):
                    zf.write(src, f["filename"])
        new_files = [{"filename": zip_name, "size": os.path.getsize(zip_path)}]
    return names(new_files)


def group_pipeline():
    print("\n[3] 端到端流水线（真实临时目录）")
    root = mk_root("_tmp_pipe")
    try:
        threshold = int(getattr(settings, "OUTPUT_ZIP_THRESHOLD", 3))

        # 场景 A：1 个产物 + 模型自打 zip（会话 276）
        a = os.path.join(root, "a")
        os.makedirs(a, exist_ok=True)
        before = snapshot_dir(a)
        write(os.path.join(a, "deck.pptx"), "pptx-bytes")
        mk_zip(os.path.join(a, "产物打包.zip"), {"deck.pptx": "pptx-bytes"})
        check(f"A：{threshold} 个以内只交原文件，自打 zip 不出现",
              deliver(a, before) == ["deck.pptx"], deliver(a, before))

        # 场景 B：2 个产物 + 模型自打 zip（仍在阈值内）
        b = os.path.join(root, "b")
        os.makedirs(b, exist_ok=True)
        before_b = snapshot_dir(b)
        write(os.path.join(b, "report.docx"), "docx")
        write(os.path.join(b, "data.xlsx"), "xlsx")
        mk_zip(os.path.join(b, "all.zip"), {"report.docx": "docx", "data.xlsx": "xlsx"})
        check("B：阈值内交原文件（2 个），自打 zip 被剔",
              deliver(b, before_b) == ["data.xlsx", "report.docx"], deliver(b, before_b))

        # 场景 C：超过阈值 -> 平台打一个包，且模型自打的包不掺进来
        c = os.path.join(root, "c")
        os.makedirs(c, exist_ok=True)
        before_c = snapshot_dir(c)
        for i in range(threshold + 2):
            write(os.path.join(c, f"f{i}.txt"), f"body{i}")
        mk_zip(os.path.join(c, "model_made.zip"),
               {f"f{i}.txt": f"body{i}" for i in range(threshold + 2)})
        out_c = deliver(c, before_c)
        check("C：超阈值由平台打成单个 zip", out_c == ["outputs_platform.zip"], out_c)
        with zipfile.ZipFile(os.path.join(c, "outputs_platform.zip")) as zf:
            members = sorted(zf.namelist())
        check("C：平台包内是原文件，不含模型自打的 zip",
              members == sorted(f"f{i}.txt" for i in range(threshold + 2)), members)

        # 场景 D：用户明确只要压缩包（本轮没有别的产物）
        d = os.path.join(root, "d")
        os.makedirs(d, exist_ok=True)
        before_d = snapshot_dir(d)
        mk_zip(os.path.join(d, "deliver.zip"), {"a.txt": "a", "b.txt": "b"})
        check("D：只交压缩包时原样交付", deliver(d, before_d) == ["deliver.zip"],
              deliver(d, before_d))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main():
    group_filter()
    group_prompt()
    group_pipeline()

    failed = [n for n, ok in results if not ok]
    print(f"\n===== 合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)} =====")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
