import argparse

from sqlalchemy import text

from app.config import settings
from app.database import SessionLocal
from migrate_admin_stage0_v1 import migrate as stage0
from migrate_admin_stage1_v1 import migrate as stage1
from migrate_admin_stage2_v1 import migrate as stage2
from migrate_admin_stage3_v1 import migrate as stage3
from migrate_admin_stage4_v1 import migrate as stage4
from migrate_admin_stage5_v1 import migrate as stage5
from migrate_admin_stage5_v1 import backfill_sessions
from migrate_admin_stage6_v1 import migrate as stage6
from migrate_admin_stage7_v1 import migrate as stage7
from migrate_mcp_lifecycle_v1 import migrate as mcp_lifecycle
from migrate_model_gateway_v2 import migrate as model_gateway_v2
from migrate_mcp_governance_v2 import migrate as mcp_governance_v2
from migrate_mcp_display_v3 import migrate as mcp_display_v3
from migrate_skill_hub_v1 import migrate as skill_hub


def main(apply=False):
    for migration in (stage0, stage1, stage2, stage3, stage4, stage5, stage6, stage7, mcp_lifecycle, model_gateway_v2, mcp_governance_v2, mcp_display_v3, skill_hub):
        migration(apply)
    if not settings.GOVERNANCE_RESOURCE_VERSIONING_ENABLED:
        return
    if apply:
        backfill_sessions()
    checks = (
        'SELECT count(*) FROM skills s LEFT JOIN skill_publications p ON p.skill_id=s.id WHERE s.is_active=true AND NOT EXISTS (SELECT 1 FROM skill_hub_resources h WHERE h.owner_id IS NOT NULL AND (h.skill_id=s.id OR h.package_id=s.package_id)) AND p.skill_id IS NULL AND NOT EXISTS (SELECT 1 FROM skill_versions v WHERE v.skill_id=s.id)',
        'SELECT count(*) FROM agents a LEFT JOIN agent_publications p ON p.agent_id=a.id WHERE a.is_active=true AND p.agent_id IS NULL',
        'SELECT count(*) FROM sessions s LEFT JOIN session_agent_versions p ON p.session_id=s.id WHERE p.session_id IS NULL',
    )
    with SessionLocal() as db:
        if any(db.execute(text(query)).scalar() for query in checks):
            raise RuntimeError('Governance baseline incomplete; refusing versioned runtime startup')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    main(parser.parse_args().apply)
