import request from './request'

export interface ToolGrant {
  server_id: number
  tool_name: string
  schema_digest: string
}

export interface AgentSnapshot {
  name: string
  description: string | null
  agent_type: string
  system_prompt: string | null
  model_config_name: string
  config: {
    mcp_servers?: number[]
    knowledge_bases?: number[]
    builtin_skills?: string[]
    max_react_iters?: number
    _skill_versions?: Record<string, string>
  }
  tool_grants: ToolGrant[]
  memory_policy: string
}

export interface AgentRevision {
  id: string
  agent_id: number
  number: number
  state: 'draft' | 'published'
  snapshot: AgentSnapshot
  revision: number
  test_status: string
  test_summary: string | null
  created_at: string
}

export interface AgentHistory {
  published_version_id: string | null
  revision: number
  versioning_enabled: boolean
  versions: AgentRevision[]
  history: { id: number; action: string; version_id: string; created_at: string }[]
}

export interface SkillDependency {
  kind: 'skill' | 'tool' | 'model'
  skill_id?: number
  version_id?: string
  server_id?: number
  tool_name?: string
  schema_digest?: string
  model_id?: string
}

export interface SkillRevision {
  id: string
  skill_id: number
  status: 'draft' | 'published'
  content_hash: string
  manifest: { path: string; sha256: string; bytes: number }[]
  dependencies: SkillDependency[]
  category: string | null
  tags: string[]
  owner_id: number | null
  revision: number
  test_status: string
  test_summary: string | null
  created_at: string
}

export interface SkillHistory {
  published_version_id: string | null
  revision: number
  versioning_enabled: boolean
  dir_missing: boolean
  versions: SkillRevision[]
  history: { id: number; action: string; version_id: string; created_at: string }[]
}

export const agentVersioning = () => request.get<{ enabled: boolean }>('/admin/agents/versioning').then((res) => res.data.enabled)
export const skillVersioning = () => request.get<{ enabled: boolean }>('/admin/skills/versioning').then((res) => res.data.enabled)
export const agentHistory = (id: number) => request.get<AgentHistory>(`/admin/agents/${id}/versions`).then((res) => res.data)
export const createAgentDraft = (data: object) => request.post<AgentRevision>('/admin/agents/', data).then((res) => res.data)
export const cloneAgentVersion = (id: number, sourceId: string) => request.post<AgentRevision>(`/admin/agents/${id}/versions`, { source_id: sourceId }).then((res) => res.data)
export const saveAgentDraft = (id: number, versionId: string, revision: number, changes: object) =>
  request.put<AgentRevision>(`/admin/agents/${id}/versions/${versionId}`, { revision, changes }).then((res) => res.data)
export const agentVersionDiff = (id: number, versionId: string) => request.get<{ fields: Record<string, { before: unknown; after: unknown }> }>(
  `/admin/agents/${id}/versions/${versionId}/diff`).then((res) => res.data)
export const testAgentDraft = (id: number, versionId: string, prompt: string) =>
  request.post(`/admin/agents/${id}/versions/${versionId}/test`, { prompt }).then((res) => res.data)
export const releaseAgent = (id: number, versionId: string, revision: number, rollback = false) =>
  request.post(`/admin/agents/${id}/versions/${versionId}/${rollback ? 'rollback' : 'publish'}`, { revision }).then((res) => res.data)

export const skillHistory = (id: number) => request.get<SkillHistory>(`/admin/skills/${id}/versions`).then((res) => res.data)
export const createSkillCandidate = (id: number) => request.post<SkillRevision>(`/admin/skills/${id}/versions`).then((res) => res.data)
export const saveSkillDraft = (id: number, versionId: string, data: object) => request.put<SkillRevision>(
  `/admin/skills/${id}/versions/${versionId}`, data).then((res) => res.data)
export const skillVersionDiff = (id: number, versionId: string) => request.get<{
  files: { path: string; change: string }[]; dependencies: object
}>(`/admin/skills/${id}/versions/${versionId}/diff`).then((res) => res.data)
export const testSkillDraft = (id: number, versionId: string, script: string | null, sampleInput: string) =>
  request.post(`/admin/skills/${id}/versions/${versionId}/smoke-test`, { script, sample_input: sampleInput }).then((res) => res.data)
export const releaseSkill = (id: number, versionId: string, revision: number, rollback = false) =>
  request.post(`/admin/skills/${id}/versions/${versionId}/${rollback ? 'rollback' : 'publish'}`, { revision }).then((res) => res.data)

export const readableError = (error: unknown) => {
  const body = error as { response?: { data?: { detail?: unknown } } }
  const detail = body.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') return detail.message
  return '操作失败，请稍后重试'
}

export interface ReviewedTool {
  server_id: number
  name: string
  description: string | null
  schema_digest: string
  input_schema: object
  output_schema: object | null
  risk: 'unknown' | 'high' | 'low'
  available: boolean
  revision: number
  discovered_at: string | null
}

export const reviewedTools = (serverId: number) => request.get<ReviewedTool[]>(
  `/admin/mcp/servers/${serverId}/catalog`).then((res) => res.data)
export const refreshReviewedTools = (serverId: number) => request.post<{
  changes: { name: string; change: string }[]; tool_count: number
}>(`/admin/mcp/servers/${serverId}/catalog/refresh`).then((res) => res.data)
export const updateToolRisk = (serverId: number, toolName: string, revision: number, risk: 'low' | 'high') =>
  request.put(`/admin/mcp/servers/${serverId}/catalog/${encodeURIComponent(toolName)}/risk`, { revision, risk }).then((res) => res.data)
