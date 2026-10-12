"""Crash recovery for unreferenced personal snapshots; never touches active/system versions."""
import shutil
import time
from pathlib import Path
from uuid import UUID
from sqlalchemy import inspect
from app.config import settings
from app.models.skill import Skill
from app.models.skill_package import SkillPackage
from app.models.skill_hub import SkillHubResource
from app.models.skill_governance import SkillVersion
from app.models.skill_resource import PackageVersion
from app.services import skill_versions, skill_package_versions


def recover_orphans(db, minimum_age=86400):
    if not inspect(db.connection()).has_table(SkillHubResource.__tablename__):
        return 0
    # Include rolled-back creations, whose unique personal native name survives only on disk.
    removed = 0
    resources = db.query(SkillHubResource).filter(SkillHubResource.origin != 'system').all()
    ids = {r.skill_id for r in resources if r.skill_id}
    package_ids = {r.package_id for r in resources if r.package_id}
    if package_ids:
        ids.update(s.id for s in db.query(Skill).filter(Skill.package_id.in_(package_ids)))
    kinds = [(skill_versions.storage_root(), SkillVersion, ids),
             (skill_package_versions.storage_root(), PackageVersion, package_ids)]
    cutoff = time.time() - minimum_age
    for storage, model, managed in kinds:
        base = storage.resolve()
        if not base.exists():
            continue
        live = {str(v.id) for v in db.query(model)}
        native_model = Skill if model is SkillVersion else SkillPackage
        known = {r.id for r in db.query(native_model)}
        for parent in base.iterdir():
            if parent.is_symlink() or not parent.is_dir() or not parent.name.isdecimal():
                continue
            identifier = int(parent.name)
            if identifier in known and identifier not in managed:
                continue
            for candidate in parent.iterdir():
                name = candidate.name.removeprefix('.staging-')
                try:
                    UUID(name)
                except ValueError:
                    continue
                if name in live or candidate.is_symlink() or not candidate.is_dir() or candidate.stat().st_mtime > cutoff:
                    continue
                resolved = candidate.resolve()
                if resolved.is_relative_to(base) and resolved != base:
                    shutil.rmtree(resolved)
                    removed += 1
    return removed
