"""Durable bounded worker; optional adapter never blocks successful chat delivery."""
import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from fastapi import HTTPException
from app.config import settings
from app.database import SessionLocal, engine
from app.models.agent import Agent
from app.models.message import Message
from app.models.skill_evolution import SkillEvolutionJob
from app.services.skill_evolution import policy, lifecycle
from app.services.skill_evolution.provider import GatewayReviewer

logger = logging.getLogger(__name__)
_task = None
# Session-level lock is released on process/container failure, without a polling lease.
_LEADER_LOCK = 0x534B494C4C45564F
_local_leader = threading.Lock()


def enqueue(db, user_id, session_id, message_id, model, selected_skills, versions):
    if not policy.is_enabled(db, user_id):
        return None
    actor, session = policy.actor_session(db, user_id, session_id)
    agent = db.get(Agent, session.agent_id)
    if not agent or (agent.config or {}).get('_scheduled_skill_versions'):
        return None
    from app.models.user import User
    db.query(User).filter_by(id=actor.id).with_for_update().one()
    existing = db.query(SkillEvolutionJob).filter_by(message_id=message_id, user_id=user_id).first()
    if existing:
        return existing
    if db.query(SkillEvolutionJob).filter_by(user_id=user_id).filter(
        SkillEvolutionJob.created_at >= datetime.now(timezone.utc) - timedelta(days=1)).count() >= settings.SKILL_EVOLUTION_MAX_JOBS_PER_DAY:
        return None
    message = db.get(Message, message_id)
    if not message or message.session_id != session_id or message.role != 'agent':
        raise HTTPException(403, '只可复盘已完成的本人问答')
    job = SkillEvolutionJob(id=str(uuid4()), user_id=user_id, session_id=session_id,
        message_id=message_id, model=model, status='queued', selected_skills=list(selected_skills or []),
        runtime_versions=dict(versions or {}))
    try:
        db.add(job); db.commit(); db.refresh(job)
    except IntegrityError:
        db.rollback()
        return db.query(SkillEvolutionJob).filter_by(message_id=message_id, user_id=user_id).first()
    return job


def safe_enqueue(db, **kwargs):
    try:
        return enqueue(db, **kwargs)
    except Exception:
        db.rollback()
        logger.exception('Skill review enqueue failed; chat remains unaffected')
        return None


def process(identifier, reviewer=None):
    reviewer = reviewer or GatewayReviewer()
    with SessionLocal() as db:
        job = db.get(SkillEvolutionJob, identifier)
        if not job or job.status not in {'evaluating', 'validating', 'checking', 'queued'}:
            return
        try:
            actor, session = policy.actor_session(db, job.user_id, job.session_id)
            if not policy.is_enabled(db, actor.id):
                job.status = 'cancelled'; db.commit(); return
            if isinstance(reviewer, GatewayReviewer):
                from app.services.model_access import effective_model
                effective_model(db, actor, db.get(Agent, session.agent_id), override=job.model)
            snapshot = policy.context(db, job)
            db.commit()
            if job.status == 'checking':
                job.status = 'validating'; db.commit()
            if job.status != 'validating':
                job.status = 'evaluating'; db.commit()
                proposal = reviewer.propose(job.model, snapshot)
                lifecycle.prepare(db, job, proposal, snapshot)
            while job.status == 'validating':
                lifecycle.validate_candidate(db, job, reviewer)
                if job.status != 'failed' or job.attempts >= 2:
                    break
                job.attempts += 1
                db.commit()
                # Each repair is bounded; no unrelated source or new skill may be chosen.
                feedback = {'report': job.report, 'previous_proposal': job.proposal,
                            'candidate': job.candidate_version_id,
                            'candidate_files': lifecycle.candidate_files(db, job)}
                proposal = reviewer.propose(job.model, snapshot, feedback)
                if proposal.decision != job.decision or proposal.resource_id != job.proposal.get('resource_id'):
                    break
                lifecycle.prepare(db, job, proposal, snapshot)
            if job.status == 'ready':
                lifecycle.publish(db, actor, job)
        except Exception as error:
            db.rollback()
            job = db.get(SkillEvolutionJob, identifier)
            if job and job.status not in lifecycle.TERMINAL:
                job.status = 'conflict' if isinstance(error, HTTPException) and error.status_code == 409 else 'failed'
                job.reason = (str(error.detail) if isinstance(error, HTTPException) else '进化服务暂不可用，原技能未改变')[:2000]
                job.report = {**(job.report or {}), 'error_type': type(error).__name__}
                db.commit()
            logger.warning('Skill evolution job %s ended: %s', identifier, type(error).__name__)


def claim():
    with SessionLocal() as db:
        job = db.query(SkillEvolutionJob).filter(SkillEvolutionJob.status.in_(['queued', 'validating'])).order_by(
            SkillEvolutionJob.created_at).with_for_update(skip_locked=True).first()
        if not job:
            return None
        if job.status == 'queued':
            job.status = 'evaluating'
        else:
            job.status = 'checking'
        db.commit()
        return job.id


def recover():
    with SessionLocal() as db:
        # Candidate snapshots remain immutable; re-check evidence before any recovered publish.
        for job in db.query(SkillEvolutionJob).filter(SkillEvolutionJob.status.in_(
            ['evaluating', 'validating', 'checking', 'ready'])):
            job.status = 'validating' if job.candidate_version_id else 'queued'
        db.commit()


def tick():
    """Serialize recovery and model work across replicas, without locking chat tables.

    PostgreSQL owns the leader lock on a dedicated connection across job commits.
    SQLite is supported for single-process development only.
    """
    def run():
        recover()
        identifier = claim()
        if identifier:
            process(identifier)
        return bool(identifier)

    if engine.dialect.name != 'postgresql':
        if not _local_leader.acquire(blocking=False):
            return False
        try:
            return run()
        finally:
            _local_leader.release()
    with engine.connect() as connection:
        acquired = connection.execute(text('SELECT pg_try_advisory_lock(:key)'),
            {'key': _LEADER_LOCK}).scalar()
        connection.commit()
        if not acquired:
            return False
        try:
            return run()
        finally:
            # Do not return a connection holding a session lock to the pool.
            try:
                connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': _LEADER_LOCK})
                connection.commit()
            except Exception:
                connection.invalidate()
                raise


async def loop():
    while True:
        try:
            if not await asyncio.to_thread(tick):
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception('Skill evolution worker polling failed')
            await asyncio.sleep(5)


def start():
    global _task
    if settings.SKILL_HUB_ENABLED and settings.SKILL_EVOLUTION_ENABLED and settings.SKILL_EVOLUTION_WORKER_ENABLED:
        _task = asyncio.create_task(loop(), name='skill-evolution-worker')


async def stop():
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
