# Stage 0: audited governance foundation on 52

This release is additive. Existing Agent, Skill, model, and personal knowledge-base APIs keep their request and response contracts except the MCP admin `env` response, which must never expose a secret. Do not apply this procedure to any other server.

## Isolation and recovery gate

1. Verify the actual running backend/frontend images, deployed Gitea revision, and Docker mounts. Work from that immutable revision in a separate checkout; do not copy local dirty files into the release.
2. Make a restricted backup of PostgreSQL and the persistent vault, Memory, Skill, knowledge/index, encryption-key, and upload directories. Verify SHA-256, `pg_restore --list`, archive extraction, and a complete PostgreSQL restore in an isolated container with no network or exposed port. Never write into the live mounted directories.
3. Confirm a persistent encryption key exists without displaying it. Record the previous image IDs and keep the extra schema column/table on application rollback. If an index or file must be restored, halt affected writes and restore database and files from the same backup, then reconcile indices.

## Migration and rollout

Run `python migrate_admin_stage0_v1.py` in check mode first. After the backup gate, run `python migrate_admin_stage0_v1.py --apply` before starting the new backend. Check `python migrate_mcp_env_v1.py` against a restored copy; it reports counts, never secret values. If any legacy values exist, temporarily deploy the dual-read backend with `MCP_LEGACY_ENV_READ=true` while preserving Server IDs and Agent bindings. Verify that values are decryptable and old MCP connections still work, then run `python migrate_mcp_env_v1.py --apply` and check again for zero legacy values. On 52, the restored baseline currently reports zero legacy values, so keep `MCP_LEGACY_ENV_READ=false` (the safe default) unless a fresh preflight proves otherwise. Restart only the affected backend; check MCP connections again. Stop and restore the old image if any check fails. Do not enable versioning or cross-user memory reads as a workaround.

Commit/push an immutable revision to the authorized 52 Gitea repository and let 52 Coolify build it. A push does not prove deployment; confirm new image IDs and source hashes, backend/frontend readiness, PostgreSQL/Redis health, and unchanged persistent host mounts after restarting the affected service. Never patch a running container filesystem.

## Contracts and gates

- `GOVERNANCE_RBAC_ENABLED`, `GOVERNANCE_RESOURCE_VERSIONING_ENABLED`, `GOVERNANCE_TOOL_POLICY_ENABLED`, and `GOVERNANCE_MEMORY_CANDIDATES_ENABLED` default to false. They stage new capabilities; they never disable existing authorization, auditing, or secret protection.
- Future resource versions use immutable `resource_id` and `version_id`, a stable creator ID, an update timestamp, and an optimistic `revision`. Concurrent writes and missing dependencies return 409; denied access returns 403; unavailable upstream services return 503. No version-enabled write is allowed to silently hot-update a published object.
- Admin mutations, failed or denied attempts, and sensitive reads reserve an audit event in a separate transaction before execution. Sensitive reads fail closed if audit completion cannot be recorded. Ordinary audit metadata contains only whitelisted state differences or field names, never credentials, full prompts, or memory text.
- MCP `env` values in admin responses are metadata only (`configured`, `masked`, `last_rotated_at`). Omitted keys are preserved; explicit null or empty strings remove a key; a nonempty string rotates it. Connections use the same decryption path as connection tests. Old plaintext is readable only when the temporary `MCP_LEGACY_ENV_READ=true` override is deliberately configured.
- Do not advance to stage 1 until recovery, migration, permissions, unauthorized/failed audit, secret redaction, legacy MCP connectivity, frontend build, and existing chat/Skill/knowledge regressions pass. Separate decisions on memory audit retention, approval owner, and candidate retention are needed before cross-user memory management.
