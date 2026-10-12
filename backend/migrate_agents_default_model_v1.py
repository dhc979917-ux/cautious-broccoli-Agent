"""一次性迁移脚本：把基座 Agent 的默认模型对齐到平台配置（可安全重复执行）。

背景：
  init_db.py / migrate_agents_v2.py 早期把"工具助手""数据分析师"的 model_config_name
  硬编码为 qwen3.7-max，而平台默认模型（settings.MODEL_NAME，来自 .env / config.py）
  已经升级到新值。运行时优先级是 本次请求所选 > Agent 的 DB 值 > settings.MODEL_NAME
  （见 architectures/base.py 的 _model_name、api/v1/chat.py 的 actual_model），
  所以不带 model 的调用路径（如调度任务 job.model 为空时）仍会跑在旧模型上，
  管理后台"模型"列显示的也是这个旧值。

本脚本做什么：
  只改两个基座 Agent（工具助手 / 数据分析师），且只在它们的模型仍是"历史默认值"
  时才更新——管理员有意为某个 Agent 指定过其它模型的，一律不动。
  其它 Agent 需要批量对齐时显式加 --include-derived（它们通常继承基座 Agent 的旧值）。

用法（在 backend 目录下，容器内同样可用：python migrate_agents_default_model_v1.py）：
    python migrate_agents_default_model_v1.py            # 只对齐两个基座 Agent
    python migrate_agents_default_model_v1.py --include-derived   # 同时对齐继承了旧值的其它 Agent
    python migrate_agents_default_model_v1.py --dry-run  # 只打印将要发生的变更
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.config import settings
from app.database import SessionLocal
from app.models.agent import Agent

# 历史上被硬编码播种过的默认模型；命中这些值的行才认为是"未被人为指定过"
LEGACY_DEFAULT_MODELS = ("qwen-max", "qwen3.7-max")

# 基座 Agent 名称（与 agent_service.BASE_AGENT_NAME_BY_TYPE 保持一致）
BASE_AGENT_NAMES = ("工具助手", "数据分析师")


def migrate(include_derived: bool = False, dry_run: bool = False) -> int:
    target = settings.MODEL_NAME
    db = SessionLocal()
    changed = 0
    try:
        q = db.query(Agent)
        if not include_derived:
            q = q.filter(Agent.name.in_(BASE_AGENT_NAMES))
        rows = q.order_by(Agent.id.asc()).all()
        if not rows:
            print("[WARN] 未找到任何 Agent（新库请先运行 init_db.py）")
            return 0

        for agent in rows:
            current = agent.model_config_name
            base_hit = agent.name in BASE_AGENT_NAMES
            # 基座 Agent：空值或历史默认值都补齐；其它 Agent：只动历史默认值
            stale = current in LEGACY_DEFAULT_MODELS or (base_hit and not current)
            if not stale or current == target:
                print(f"[SKIP] id={agent.id} {agent.name} model={current or '(空)'}（非历史默认值，保持不变）")
                continue
            changed += 1
            print(f"[{'DRY' if dry_run else 'OK'}] id={agent.id} {agent.name}: {current or '(空)'} -> {target}")
            if not dry_run:
                agent.model_config_name = target
        if not dry_run:
            db.commit()
        print(f"[DONE] 平台默认模型 = {target}；{'预计' if dry_run else '实际'}更新 {changed} 个 Agent")
        return changed
    finally:
        db.close()


if __name__ == "__main__":
    migrate(
        include_derived="--include-derived" in sys.argv,
        dry_run="--dry-run" in sys.argv,
    )
