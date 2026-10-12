#!/bin/sh
set -eu
cd /app
python migrate_admin_release_v1.py --apply
python migrate_skill_evolution_v1.py --apply
exec /docker-entrypoint.sh "$@"
