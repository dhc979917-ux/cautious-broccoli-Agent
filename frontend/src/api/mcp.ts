import request from './request'

// MCP 工具管理 API（写操作仅 admin）

export interface McpServerInfo {
  id: number
  name: string
  type: 'stdio' | 'http'
  command?: string | null
  args?: string[]
  env?: Record<string, { configured: boolean; masked: string; last_rotated_at?: string | null }>
  env_configured?: boolean
  url?: string | null
  description?: string | null
  display_description?: string | null
  enabled: boolean
  is_deleted?: boolean
  created_at?: string
  connection?: string
  visibility?: McpVisibility
}

export interface McpVisibility { hidden: boolean; roles: string[]; user_ids: number[]; revision: number }
export interface McpSource { available: boolean; reason?: string; filename?: string; content?: string; line?: number; origin?: string }
export const getMcpVisibility = (id: number): Promise<McpVisibility> => request.get(`/mcp/servers/${id}/visibility`).then((res) => res.data)
export const saveMcpVisibility = (id: number, data: McpVisibility): Promise<McpVisibility> => request.put(`/mcp/servers/${id}/visibility`, data).then((res) => res.data)
export const getMcpSource = (id: number, name: string): Promise<McpSource> => request.get(`/mcp/servers/${id}/source/${encodeURIComponent(name)}`).then((res) => res.data)

export type McpServerInput = Omit<Partial<McpServerInfo>, 'env'> & { env?: Record<string, string | null> }

export interface McpTestResult {
  ok: boolean
  tool_count: number
  tools: { name: string; description: string }[]
  error?: string | null
}

export async function listMcpServers(includeDeleted = false): Promise<McpServerInfo[]> {
  const res = await request.get('/mcp/servers', { params: { include_deleted: includeDeleted } })
  return res.data
}

export async function createMcpServer(data: McpServerInput) {
  const res = await request.post('/mcp/servers', data)
  return res.data
}

export async function updateMcpServer(id: number, data: McpServerInput) {
  const res = await request.put(`/mcp/servers/${id}`, data)
  return res.data
}

export async function deleteMcpServer(id: number) {
  const res = await request.delete(`/mcp/servers/${id}`)
  return res.data
}

export async function toggleMcpServer(id: number) {
  const res = await request.post(`/mcp/servers/${id}/toggle`)
  return res.data
}

export async function restoreMcpServer(id: number) {
  return (await request.put(`/mcp/servers/${id}/restore`)).data
}

export async function hardDeleteMcpServer(id: number, username: string, password: string) {
  return (await request.delete(`/mcp/servers/${id}/hard`, { data: { username, password } })).data
}

export async function testMcpConnection(data: McpServerInput, id?: number): Promise<McpTestResult> {
  const res = await request.post(id ? `/mcp/servers/${id}/test` : '/mcp/test', data)
  return res.data
}

export async function listMcpTools(id: number): Promise<McpTestResult> {
  const res = await request.get(`/mcp/servers/${id}/tools`)
  return res.data
}
