"""Atomic personal candidate preparation and publication, reusing hub services."""
import copy
from pathlib import Path
from uuid import uuid4
from fastapi import HTTPException
from app.models.user import User
from app.schemas.skill_evolution import Proposal
from app.models.skill_hub import SkillHubResource, SkillHubVersion
from app.models.skill_evolution import SkillEvolutionBinding, SkillEvolutionJob
from app.services.skill_hub import access, content, sharing
from app.services.skill_resource_files import copy_tree, relative_path, safe_file
from app.services.skill_evolution import policy, runtime

TERMINAL = {'no_change', 'published', 'cancelled'}


def owned_job(db, actor, identifier, lock=False):
    if lock:
        db.query(User).filter_by(id=actor.id).with_for_update().one()
    query = db.query(SkillEvolutionJob).filter_by(id=identifier, user_id=actor.id)
    job = (query.with_for_update() if lock else query).first()
    if not job:
        raise HTTPException(404, '进化作业不存在')
    policy.actor_session(db, actor.id, job.session_id)
    return job


def describe(job):
    return {key: getattr(job, key) for key in ('id', 'session_id', 'message_id', 'status',
        'decision', 'reason', 'source_resource_id', 'source_version_id', 'target_resource_id',
        'base_version_id', 'candidate_version_id', 'report', 'attempts', 'created_at', 'updated_at')}


def texts(root):
    return {entry['path']: (root / entry['path']).read_text(encoding='utf-8', errors='replace')
            for entry in content.manifest(root)
            if Path(entry['path']).suffix.lower() in {'.md', '.txt', '.py', '.json', '.yml', '.yaml', '.js', '.ts', '.sh'}
            and entry['bytes'] < 100000}


def candidate_files(db, job):
    row = access.owner(db, job.target_resource_id, db.get(User, job.user_id))
    directory = content.version_directory(db, row, access.native(db, row, job.candidate_version_id))
    result = {k: policy.redact(v, 6000) for k, v in list(texts(directory).items())[:8]}
    db.commit()
    return result


def apply_operations(root, operations):
    for operation in operations:
        relative = relative_path(operation.path)
        # Copy tree is already symlink-free; root is exclusively owned by this staging context.
        path = root.joinpath(*relative.parts)
        if operation.action == 'delete':
            if path.name == 'SKILL.md':
                raise HTTPException(422, '自动进化不能删除技能入口')
            safe_file(root, operation.path).unlink()
        else:
            policy.check_text(operation.content if operation.action == 'write' else operation.new_string)
            if operation.action == 'patch':
                original = safe_file(root, operation.path).read_text(encoding='utf-8')
                if not operation.old_string or original.count(operation.old_string) != 1:
                    raise HTTPException(409, '补丁必须唯一匹配，请重新读取候选内容')
                value = original.replace(operation.old_string, operation.new_string, 1)
            else:
                value = operation.content
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding='utf-8')
    policy.validate_root(root)


def resolve_target(db, actor, job, proposal, found):
    if proposal.decision == 'create_personal':
        if job.selected_skills and not found:
            raise HTTPException(403, '本次来源未开放派生，不允许通过创建技能绕过许可')
        if proposal.resource_id or not proposal.title.strip():
            raise HTTPException(422, '新技能必须命名且不能伪装成来源派生')
        if db.query(SkillHubResource).filter_by(owner_id=actor.id, title=proposal.title.strip(), archived=False).first():
            job.status = 'conflict'
            job.reason = '已有同名个人技能，未创建副本；请先在问答选用该技能再修复'
            return None, None, None
        return None, None, None
    if proposal.resource_id not in found:
        raise HTTPException(403, '目标不是本次已选且允许进化的技能')
    source_row, source = found[proposal.resource_id]
    policy.permit_fork(db, source_row, actor, source.id)
    if source_row.owner_id == actor.id:
        if proposal.decision != 'repair_personal':
            raise HTTPException(422, '本人技能应直接修复，不重复派生')
        return source_row, source_row, source
    if proposal.decision != 'derive_source':
        raise HTTPException(403, '非本人技能只能在授权后派生')
    binding = db.query(SkillEvolutionBinding).filter_by(user_id=actor.id, source_resource_id=source_row.id).first()
    target = access.owner(db, binding.target_resource_id, actor) if binding else None
    if not target:
        copies = db.query(SkillHubResource).filter_by(owner_id=actor.id, source_resource_id=source_row.id, archived=False).all()
        if len(copies) > 1:
            job.status = 'awaiting_target'
            job.reason = '同来源有多个个人副本，请选择修复目标'
            job.report = {'choices': [{'id': r.id, 'title': r.title} for r in copies]}
            job.source_resource_id, job.source_version_id = source_row.id, source.id
            return source_row, None, source
        target = copies[0] if copies else None
    return source_row, target, source


def prepare(db, job, proposal, snapshot):
    actor, _ = policy.actor_session(db, job.user_id, job.session_id)
    if not policy.is_enabled(db, actor.id):
        job.status = 'cancelled'
        job.reason = '自动进化已关闭'
        db.commit()
        return
    db.refresh(job)
    if job.status == 'cancelled':
        return
    job.decision, job.reason = proposal.decision, policy.redact(proposal.reason, 2000)
    stored = proposal.model_dump()
    stored['reason'] = job.reason
    job.proposal = stored
    if proposal.decision == 'no_change':
        job.status = 'failed' if job.candidate_version_id else 'no_change'
        db.commit()
        return
    policy.check_text(proposal.title + '\n' + proposal.summary)
    evidence = policy.evidence_checked(proposal, snapshot)
    if not proposal.operations or not proposal.criteria or not proposal.sample_input.strip():
        raise HTTPException(422, '修改必须有操作、虚构样例和可检查的验收条件')
    policy.check_text(proposal.sample_input)
    for criterion in proposal.criteria:
        policy.check_text(criterion)
    db.query(User).filter_by(id=actor.id).with_for_update().one()
    found = policy.candidates(db, job, actor)
    source_row, target, source = resolve_target(db, actor, job, proposal, found)
    if job.target_resource_id and target is None and job.status not in {'conflict', 'awaiting_target'}:
        target = access.owner(db, job.target_resource_id, actor)
    if job.status in {'awaiting_target', 'conflict'}:
        db.commit()
        return
    if source_row:
        job.source_resource_id, job.source_version_id = source_row.id, source.id
    if target:
        target = access.owner(db, target.id, actor)
        if target.archived or access.is_blocked(db, target):
            raise HTTPException(409, '个人技能已归档或停用')
        if target.id in policy.preference(db, actor.id)['paused_resources']:
            job.status = 'cancelled'; job.reason = '此技能已暂停自动进化'; db.commit(); return
        # Serialize preparation against another job/manual edits on this resource.
        db.query(SkillHubResource).filter_by(id=target.id).with_for_update().one()
        if target.draft_version_id and target.draft_version_id != job.candidate_version_id:
            job.status = 'conflict'; job.reason = '已有人工或其他作业草稿，未覆盖'; db.commit(); return
        native = access.native(db, target, target.draft_version_id or target.published_version_id)
        baseline = access.native(db, target, target.published_version_id) if target.published_version_id else native
        job.base_version_id = job.base_version_id or target.published_version_id
        job.base_revision = target.revision if job.base_revision is None else job.base_revision
        directory = content.version_directory(db, target, native)
        original = texts(content.version_directory(db, target, baseline))
        metadata = copy.deepcopy(db.get(SkillHubVersion, native.id).metadata_json)
    elif source:
        directory = content.version_directory(db, source_row, source)
        original = texts(directory)
        metadata = None
    else:
        directory = None
        original = {}
        metadata = None
    with content.staging() as stage:
        if directory:
            copy_tree(directory, stage)
        apply_operations(stage, proposal.operations)
        if directory and content.manifest(stage) == content.manifest(directory):
            job.status = 'failed' if job.candidate_version_id else 'no_change'
            job.reason = '修复没有新增有效改动，原版保持不变' if job.candidate_version_id else '候选内容与基线一致'
            db.commit(); return
        if not target:
            if source:
                prior = db.get(SkillHubVersion, source.id)
                metadata = copy.deepcopy(prior.metadata_json) if prior else content.metadata_default(source_row.title, source_row.summary)
                target = content.make_resource(db, actor, source_row.title, source_row.summary,
                    source_row.kind, stage, origin='auto_forked', source_row=source_row,
                    source_version=source, metadata=metadata)
            else:
                target = content.make_resource(db, actor, proposal.title.strip(), proposal.summary,
                    'skill', stage, origin='auto_created')
            target.allow_fork = False
        else:
            content.freeze(db, target, stage, actor.id, metadata)
        job.target_resource_id = target.id
        job.candidate_version_id, job.candidate_revision = target.draft_version_id, target.revision
        job.base_revision = job.base_revision if job.base_revision is not None else target.revision - 1
        risks = policy.risk_of(proposal.operations, original)
        previous_risks = (job.report or {}).get('risks', [])
        job.report = {'evidence': evidence, 'risks': sorted(set(previous_risks + risks)),
                      'validation_mode': 'fictional_model_comparison', 'switched_session': False}
        job.status = 'validating'
        sharing.audit(db, actor, 'evolution_candidate', target.id, {'job_id': job.id, 'version_id': target.draft_version_id})
        db.commit()


def validate_candidate(db, job, reviewer):
    actor, _ = policy.actor_session(db, job.user_id, job.session_id)
    target = access.owner(db, job.target_resource_id, actor)
    if target.draft_version_id != job.candidate_version_id or target.revision != job.candidate_revision:
        job.status = 'conflict'; job.reason = '候选版本已变化，未覆盖'; db.commit(); return
    proposal = Proposal.model_validate(job.proposal)
    if not policy.is_enabled(db, actor.id):
        job.status = 'cancelled'; db.commit(); return
    checked = content.test(db, actor, target.id, proposal.script, proposal.sample_input)
    report = dict(job.report)
    report['isolation'] = checked.test_report
    report['isolation_passed'] = checked.test_status == 'passed'
    native = access.native(db, target, job.candidate_version_id)
    after = texts(content.version_directory(db, target, native))
    if job.base_version_id:
        baseline = access.native(db, target, job.base_version_id)
        before = texts(content.version_directory(db, target, baseline))
    elif job.source_resource_id:
        row = access.resource(db, job.source_resource_id)
        before = texts(content.version_directory(db, row, access.native(db, row, job.source_version_id)))
    else:
        before = {}
    # Send only bounded, scrubbed instruction documents to the validator.
    scrub = lambda values: {k: policy.redact(v, 7000) for k, v in list(values.items())[:8]}
    db.commit()
    if checked.test_status == 'passed':
        report['business'] = reviewer.validate(job.model, proposal.sample_input, proposal.criteria,
                                               scrub(before), scrub(after))
    else:
        report['business'] = {'passed': False, 'warning': '隔离检查失败，未进行业务验证'}
    db.refresh(job)
    db.refresh(target)
    if job.status != 'validating':
        return
    if target.revision != job.candidate_revision or target.draft_version_id != job.candidate_version_id:
        job.status = 'conflict'; job.reason = '验证期间候选发生变化，未发布'; db.commit(); return
    job.report = report
    if not report['isolation_passed'] or not report['business'].get('passed'):
        job.status = 'failed'; job.reason = '验证未通过，原发布版本保持不变'
    elif report.get('risks'):
        job.status = 'awaiting_confirmation'; job.reason = '验证通过，但高风险变更需要确认'
    else:
        job.status = 'ready'
    db.commit()


def publish(db, actor, job, acknowledge_risk=False):
    target = access.owner(db, job.target_resource_id, actor)
    db.refresh(job)
    if job.status not in {'ready', 'awaiting_confirmation'}:
        raise HTTPException(409, '此作业尚未达到发布条件')
    if job.report.get('risks') and not acknowledge_risk:
        raise HTTPException(409, '请确认高风险变更后再发布')
    if not job.report.get('isolation_passed') or not job.report.get('business', {}).get('passed'):
        raise HTTPException(409, '验证证据不足，不能发布')
    if target.revision != job.candidate_revision or target.draft_version_id != job.candidate_version_id or target.published_version_id != job.base_version_id:
        # For a fresh fork there was never a personal published baseline.
        job.status = 'conflict'; job.reason = '发布基线或候选已变化'; db.commit(); return job
    if not policy.is_enabled(db, actor.id) or target.id in policy.preference(db, actor.id)['paused_resources']:
        raise HTTPException(409, '自动进化已关闭或此技能已暂停')
    content.publish(db, actor, target.id, job.candidate_revision)
    source = None
    policy.actor_session(db, actor.id, job.session_id)
    if job.source_resource_id:
        row = access.resource(db, job.source_resource_id)
        policy.permit_fork(db, row, actor, job.source_version_id)
        source = access.native(db, row, job.source_version_id)
        if row.id != target.id:
            binding = db.query(SkillEvolutionBinding).filter_by(user_id=actor.id, source_resource_id=row.id).first()
            if binding:
                binding.target_resource_id = target.id
            else:
                db.add(SkillEvolutionBinding(id=str(uuid4()), user_id=actor.id,
                    source_resource_id=row.id, target_resource_id=target.id))
    changed = runtime.bind(db, job, source, access.native(db, target, job.candidate_version_id))
    job.report = {**job.report, 'switched_session': changed}
    job.status, job.reason = 'published', '个人技能已验证并发布，历史版本保留'
    sharing.audit(db, actor, 'evolution_publish', target.id, {'job_id': job.id, 'version_id': job.candidate_version_id})
    db.commit()
    # Version fingerprint already invalidates agents; do not clear active-turn read guards.
    try:
        sharing.notify(db, actor.id, '个人技能进化完成', target.title + '：' + job.reason)
        db.commit()
    except Exception:
        db.rollback()  # Notification cannot reverse an already committed publication.
    return job


def discard(db, actor, job):
    if job.status == 'published':
        raise HTTPException(409, '已发布版本请通过版本管理回滚')
    if job.target_resource_id:
        target = access.owner(db, job.target_resource_id, actor)
        if target.draft_version_id == job.candidate_version_id and target.revision == job.candidate_revision:
            target.draft_version_id = None
            target.revision += 1
            if target.origin in {'auto_forked', 'auto_created'} and not target.published_version_id:
                target.archived = True
    job.status, job.reason = 'cancelled', '候选已拒绝或作业已取消，原版本未改变'
    db.commit()
    return job
