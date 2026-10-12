#!/bin/bash
set -e

# ---------------------------------------------------------------------------
# Seed built-in skills into volume-mounted directories.
#
# skills/ (optional):
#   Per-skill seeding - new skills from image updates are added, existing ones
#   are left alone unless the seed carries a .seed_version marker and the
#   volume copy is missing or has a different version.  That keeps platform
#   managed skills recoverable after partial manual updates while still
#   preserving ordinary user-uploaded skills.
#   A skill an admin moved into a builtin root ("内置") is never seeded back
#   here, otherwise every restart would leave a duplicate copy behind and the
#   DB would see the same skill name in two roots.
#
# skills_dialog/ and skills_task/ (builtin):
#   Per-skill seeding with a .deleted_skills tracker.  New skills from image
#   updates are seeded automatically; skills previously hard-deleted by an
#   admin are recorded in .deleted_skills and never re-seeded.
#   The same tracker records skills an admin moved OUT of a builtin root
#   (取消内置), so demoted skills are not re-seeded either.
# ---------------------------------------------------------------------------

seed_optional() {
    local seed_dir="$1"
    local target_dir="$2"
    [ -d "$seed_dir" ] || return 0
    mkdir -p "$target_dir"
    local entry name seed_version target_version
    for entry in "$seed_dir"/*; do
        [ -e "$entry" ] || continue
        name=$(basename "$entry")

        if [ -f "$target_dir/.deleted_skills" ] && grep -qxF "$name" "$target_dir/.deleted_skills"; then
            echo "[entrypoint] Skipping deleted optional skill: $name"
            continue
        fi

        seed_version=""
        if [ -f "$entry/.seed_version" ]; then
            seed_version=$(cat "$entry/.seed_version" 2>/dev/null || true)
        fi
        target_version=""
        if [ -f "$target_dir/$name/.seed_version" ]; then
            target_version=$(cat "$target_dir/$name/.seed_version" 2>/dev/null || true)
        fi

        if [ -n "$seed_version" ] && [ "$seed_version" != "$target_version" ]; then
            # Platform-managed skill: replace the volume copy atomically enough
            # for startup, so mixed file sets cannot survive an image update.
            local tmp_target="$target_dir/.seed-$name.$$"
            rm -rf "$tmp_target"
            cp -r "$entry" "$tmp_target"
            rm -rf "$target_dir/$name"
            mv "$tmp_target" "$target_dir/$name"
            echo "[entrypoint] Synced managed skill: $name ($seed_version)"
            continue
        fi

        if [ ! -e "$target_dir/$name" ]; then
            # Promoted to a builtin root by an admin - leave it there
            if [ -e "/app/app/agents/skills_dialog/$name" ] || [ -e "/app/app/agents/skills_task/$name" ]; then
                echo "[entrypoint] Skipping optional seed (now builtin): $name"
                continue
            fi
            cp -r "$entry" "$target_dir/$name"
            echo "[entrypoint] Seeded optional skill: $name"
        fi
    done
}

seed_builtin() {
    local seed_dir="$1"
    local target_dir="$2"
    local label="$3"
    [ -d "$seed_dir" ] || return 0
    mkdir -p "$target_dir"
    local tracker="$target_dir/.deleted_skills"
    local entry name
    for entry in "$seed_dir"/*; do
        [ -e "$entry" ] || continue
        name=$(basename "$entry")
        # Already in volume - leave it alone
        [ -e "$target_dir/$name" ] && continue
        # Previously hard-deleted by admin - do not re-seed
        if [ -f "$tracker" ] && grep -qxF "$name" "$tracker"; then
            echo "[entrypoint] Skipping deleted builtin $label skill: $name"
            continue
        fi
        cp -r "$entry" "$target_dir/$name"
        echo "[entrypoint] Seeded builtin $label skill: $name"
    done
}

# Builtin roots first: on a fresh mount they are empty until seeded, and the
# optional pass has to see them to skip skills that now live there.
seed_builtin  "/app/app/agents/skills_dialog_seed"  "/app/app/agents/skills_dialog" "dialog"
seed_builtin  "/app/app/agents/skills_task_seed"    "/app/app/agents/skills_task"   "task"
seed_optional "/app/app/agents/skills_seed"        "/app/app/agents/skills"

if find /ms-playwright -maxdepth 4 -type f -name chrome-headless-shell -print -quit 2>/dev/null | grep -q .; then
    echo "playwright ready" > /app/.verify_render_status
fi

exec "$@"
