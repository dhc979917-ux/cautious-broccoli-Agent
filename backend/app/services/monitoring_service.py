"""监控与运维服务：聚合 messages.metadata_json（token/工具/耗时/模型）+ sessions，
按 用户 × Agent × 日期 × 模型 × 会话 维度提供统计。

数据全部来自现有落库数据（不引入新记录表）：
- agent 消息的 metadata_json: steps/token_usage/tool_call_count/elapsed_seconds/model
- sessions: user_id / agent_id / created_at
非 admin 只能看自己；admin 可传任意 user_id 或不传看全局。"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session as DBSession

from app.models.session import Session as SessionModel
from app.models.message import Message
from app.models.agent import Agent as AgentModel
from app.models.user import User
from app.services.task_manager import task_manager


def _parse_meta(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    try:
        m = json.loads(raw)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


class MonitoringService:
    def __init__(self, db: DBSession):
        self.db = db

    # ---- 基础查询 ----
    def _agent_messages(self, user_id: Optional[int], since: Optional[datetime]):
        """agent 回复消息（带会话关联），可选按用户与时间过滤"""
        q = (
            self.db.query(Message, SessionModel)
            .join(SessionModel, Message.session_id == SessionModel.id)
            .filter(Message.role == "agent")
        )
        if user_id is not None:
            q = q.filter(SessionModel.user_id == user_id)
        if since is not None:
            q = q.filter(Message.created_at >= since)
        return q.all()

    def _agent_name_map(self) -> dict:
        return {a.id: a.name for a in self.db.query(AgentModel).all()}

    # ---- 概览 ----
    def overview(self, user_id: Optional[int], days: Optional[int] = None) -> dict:
        since = None
        if days is not None and days > 0:
            since = datetime.now(timezone.utc) - timedelta(days=days)
        sess_q = self.db.query(SessionModel)
        if user_id is not None:
            sess_q = sess_q.filter(SessionModel.user_id == user_id)
        if since is not None:
            sess_q = sess_q.filter(SessionModel.created_at >= since)
        sessions = sess_q.count()

        rows = self._agent_messages(user_id, since=since)
        messages = len(rows)
        input_tokens = output_tokens = tool_calls = 0
        elapsed_sum = 0
        elapsed_cnt = 0
        errors = 0
        # 质量闭环（先问再做 / 做完再看）：与下面同一遍扫描聚合，不额外查库。
        # 数据源是消息 metadata.quality（由 manager 的 done 事件写入），
        # 老消息没有该字段，统计天然只覆盖改造后的数据。
        q_artifact = q_intake = q_verified = 0
        q_intake_questions = q_intake_assumed = q_verify_errors = 0
        q_tiers: dict = {}
        for msg, _sess in rows:
            meta = _parse_meta(msg.metadata_json)
            tu = meta.get("token_usage") or {}
            input_tokens += int(tu.get("input_tokens") or 0)
            output_tokens += int(tu.get("output_tokens") or 0)
            tool_calls += int(meta.get("tool_call_count") or 0)
            es = meta.get("elapsed_seconds")
            if es is not None:
                elapsed_sum += int(es)
                elapsed_cnt += 1
            if meta.get("partial"):
                errors += 1
            q = meta.get("quality") or {}
            if isinstance(q, dict) and q:
                if q.get("delivery_kind") == "artifact":
                    q_artifact += 1
                if int(q.get("intake_calls") or 0) > 0:
                    q_intake += 1
                    q_intake_questions += int(q.get("intake_questions") or 0)
                    q_intake_assumed += int(q.get("intake_assumed") or 0)
                if int(q.get("verify_calls") or 0) > 0:
                    q_verified += 1
                    q_verify_errors += int(q.get("verify_errors") or 0)
                    tier = q.get("verify_tier")
                    if tier is not None:
                        key = f"tier{tier}"
                        q_tiers[key] = int(q_tiers.get(key, 0)) + 1

        # 运行中任务（全局或该用户）
        running = 0
        try:
            for sid, info in task_manager._infos.items():
                if info.status != "running":
                    continue
                if user_id is None:
                    running += 1
                else:
                    s = self.db.query(SessionModel).filter(SessionModel.id == sid).first()
                    if s and s.user_id == user_id:
                        running += 1
        except Exception:
            pass

        return {
            "sessions": sessions,
            "messages": messages,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "tool_calls": tool_calls,
            "avg_elapsed_seconds": round(elapsed_sum / elapsed_cnt, 1) if elapsed_cnt else 0,
            "errors": errors,
            "running_tasks": running,
            # 两条核心曲线：采集触发率（"先问再做"是否生效）、
            # 平均每任务验证发现错误数（这套改造的 ROI；持续为 0 说明验证层可降档）
            "quality": {
                "artifact_replies": q_artifact,
                "intake_replies": q_intake,
                "intake_rate": round(q_intake / q_artifact * 100, 1) if q_artifact else 0,
                "intake_questions": q_intake_questions,
                "intake_assumed": q_intake_assumed,
                "assume_rate": (
                    round(q_intake_assumed / q_intake_questions * 100, 1)
                    if q_intake_questions else 0
                ),
                "verified_replies": q_verified,
                "verify_rate": round(q_verified / q_artifact * 100, 1) if q_artifact else 0,
                "verify_errors": q_verify_errors,
                "avg_verify_errors": round(q_verify_errors / q_verified, 2) if q_verified else 0,
                "verify_tiers": q_tiers,
            },
        }

    # ---- 趋势（柱状/日历数据源）----
    def trend(self, user_id: Optional[int], days: int, group_by: str) -> list:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        rows = self._agent_messages(user_id, since=since)
        agent_names = self._agent_name_map()
        agg = defaultdict(lambda: {"tokens": 0, "messages": 0})
        for msg, sess in rows:
            meta = _parse_meta(msg.metadata_json)
            tu = meta.get("token_usage") or {}
            tokens = int(tu.get("input_tokens") or 0) + int(tu.get("output_tokens") or 0)
            date_key = msg.created_at.date().isoformat() if msg.created_at else "unknown"
            if group_by == "agent":
                key = agent_names.get(sess.agent_id, f"Agent#{sess.agent_id}")
            else:  # model
                key = meta.get("model") or "默认模型"
            agg[(date_key, key)]["tokens"] += tokens
            agg[(date_key, key)]["messages"] += 1
        return [
            {"date": d, "key": k, "tokens": v["tokens"], "messages": v["messages"]}
            for (d, k), v in sorted(agg.items())
        ]

    # ---- 某天的会话列表（柱状点击下钻）----
    def conversations_on_day(self, user_id: Optional[int], date: str) -> list:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            return []
        since = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        until = since + timedelta(days=1)
        rows = self._agent_messages(user_id, since=since)
        agent_names = self._agent_name_map()
        conv = defaultdict(lambda: {
            "title": "", "agent_id": None, "agent_name": "",
            "messages": 0, "tokens": 0, "elapsed": 0, "models": set(), "first_ts": None,
        })
        for msg, sess in rows:
            if msg.created_at and msg.created_at >= until:
                continue
            meta = _parse_meta(msg.metadata_json)
            tu = meta.get("token_usage") or {}
            c = conv[sess.id]
            c["title"] = sess.title or f"会话{sess.id}"
            c["agent_id"] = sess.agent_id
            c["agent_name"] = agent_names.get(sess.agent_id, "")
            c["messages"] += 1
            c["tokens"] += int(tu.get("input_tokens") or 0) + int(tu.get("output_tokens") or 0)
            c["elapsed"] += int(meta.get("elapsed_seconds") or 0)
            if meta.get("model"):
                c["models"].add(meta["model"])
            if c["first_ts"] is None or (msg.created_at and msg.created_at < c["first_ts"]):
                c["first_ts"] = msg.created_at
        result = []
        for sid, c in conv.items():
            result.append({
                "session_id": sid,
                "title": c["title"],
                "agent_id": c["agent_id"],
                "agent_name": c["agent_name"],
                "messages": c["messages"],
                "tokens": c["tokens"],
                "elapsed_seconds": c["elapsed"],
                "models": sorted(c["models"]),
                "first_ts": c["first_ts"].isoformat() if c["first_ts"] else None,
            })
        return sorted(result, key=lambda x: x["first_ts"] or "", reverse=True)

    # ---- 单会话消息级明细 ----
    def conversation_detail(self, session_id: int) -> list:
        msgs = (
            self.db.query(Message)
            .filter(Message.session_id == session_id)
            .order_by(Message.created_at.asc())
            .all()
        )
        result = []
        for m in msgs:
            meta = _parse_meta(m.metadata_json)
            tu = meta.get("token_usage") or {}
            result.append({
                "id": m.id,
                "role": m.role,
                "ts": m.created_at.isoformat() if m.created_at else None,
                "model": meta.get("model"),
                "tokens": int(tu.get("input_tokens") or 0) + int(tu.get("output_tokens") or 0),
                "elapsed_seconds": meta.get("elapsed_seconds"),
                "tool_calls": meta.get("tool_call_count"),
                "preview": (m.content or "")[:80],
            })
        return result

    # ---- 任务列表（运行中/近期完成）----
    def tasks(self, user_id: Optional[int]) -> list:
        result = []
        try:
            items = sorted(task_manager._infos.values(), key=lambda i: i.started_at, reverse=True)
            for info in items[:50]:
                s = self.db.query(SessionModel).filter(SessionModel.id == info.session_id).first()
                if user_id is not None and (not s or s.user_id != user_id):
                    continue
                result.append({
                    "session_id": info.session_id,
                    "session_title": s.title if s else "",
                    "status": info.status,
                    "started_at": info.started_at,
                    "finished_at": info.finished_at,
                    "elapsed_seconds": round(
                        (info.finished_at or datetime.now().timestamp()) - info.started_at, 1
                    ) if info.started_at else None,
                })
        except Exception:
            pass
        return result

    # ---- admin 用户总览 ----
    def users_overview(self) -> list:
        users = self.db.query(User).all()
        # 本周 Token：本周一 00:00（本地时间）至今的消耗，一次查询按用户聚合
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        week_tokens = defaultdict(int)
        for msg, sess in self._agent_messages(user_id=None, since=week_start):
            tu = _parse_meta(msg.metadata_json).get("token_usage") or {}
            week_tokens[sess.user_id] += int(tu.get("input_tokens") or 0) + int(tu.get("output_tokens") or 0)
        result = []
        for u in users:
            ov = self.overview(u.id)
            last = (
                self.db.query(SessionModel)
                .filter(SessionModel.user_id == u.id)
                .order_by(SessionModel.updated_at.desc())
                .first()
            )
            result.append({
                "user_id": u.id,
                "username": u.username,
                "role": u.role,
                "is_active": u.is_active,
                "sessions": ov["sessions"],
                "messages": ov["messages"],
                "total_tokens": ov["total_tokens"],
                "week_tokens": week_tokens.get(u.id, 0),
                "last_active": u.last_active.isoformat() if u.last_active else (last.updated_at.isoformat() if last and last.updated_at else None),
            })
        return sorted(result, key=lambda x: x["total_tokens"], reverse=True)
