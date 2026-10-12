import request from './request'

export interface AvailableModelsResponse {
  models: string[]
  default: string
  display_names: Record<string, string>
}

export interface ModelPolicy {
  scope: 'role' | 'user'
  principal: string
  allowed_models: string[]
}

export interface ModelPoliciesResponse {
  models: string[]
  display_names: Record<string, string>
  policies: ModelPolicy[]
}

export async function getAvailableModels(): Promise<AvailableModelsResponse> {
  const { data } = await request.get<AvailableModelsResponse>('/models/')
  return data
}

export async function getModelPolicies(): Promise<ModelPoliciesResponse> {
  const { data } = await request.get<ModelPoliciesResponse>('/models/policies')
  return data
}

export async function setModelPolicy(policy: ModelPolicy | Omit<ModelPolicy, 'allowed_models'> & { allowed_models: null }): Promise<void> {
  await request.put('/models/policies', policy)
}

export interface ModelCatalogItem {
  model_id: string
  provider: string | null
  display_name: string | null
  purpose: 'chat' | 'vision' | 'embedding' | 'other' | null
  capabilities: string[] | null
  context_length: number | null
  enabled: boolean
  in_gateway: boolean | null
  last_seen_at: string | null
  input_price_per_million: string | null
  output_price_per_million: string | null
  cached_input_price_per_million: string | null
  minimum_role: 'viewer' | 'operator' | 'admin' | 'custom'
  roles: string[]
  price_source: string | null
  price_updated_at: string | null
  revision: number
  authorized_scopes: Array<{ scope: string; principal: string }>
  last_test: { tested_at: string; outcome: string; error_code: string | null; duration_ms: number | null } | null
}

export interface ModelCatalogResponse {
  items: ModelCatalogItem[]
  total: number
  page: number
  page_size: number
  fetched_at: string | null
  stale: boolean
  gateway_reachable: boolean | null
}

export type ModelMetadataInput = Partial<Pick<ModelCatalogItem,
  'display_name' | 'purpose' | 'capabilities' | 'context_length' | 'enabled' |
  'input_price_per_million' | 'output_price_per_million' | 'cached_input_price_per_million' | 'price_source'>> & {
  model_id: string
  revision: number
}

export async function getModelCatalog(page: number, refresh = false): Promise<ModelCatalogResponse> {
  const { data } = await request.get<ModelCatalogResponse>('/admin/models/catalog', {
    params: { page, page_size: 8, refresh },
  })
  return data
}

export async function saveModelMetadata(payload: ModelMetadataInput): Promise<number> {
  const { data } = await request.put<{ revision: number }>('/admin/models/catalog/metadata', payload)
  return data.revision
}

export interface ModelTestResult {
  output: string
  duration_ms: number
  input_tokens: number | null
  output_tokens: number | null
  estimated_cost_usd: string | null
  trace_id: string
}

export interface ModelAuthorization {
  roles: Array<{ role: string; allowed: boolean; default_allowed: boolean; overridden: boolean }>
  is_chat: boolean
  user: { id: number; role_allowed: boolean; extra_allowed: boolean; allowed: boolean } | null
}

export async function getModelAuthorization(modelId: string, userId?: number): Promise<ModelAuthorization> {
  return (await request.get('/models/authorization', { params: { model_id: modelId, user_id: userId } })).data
}

export async function changeModelAuthorization(modelId: string, scope: 'role' | 'user' | 'minimum_role', principal: string, allowed: boolean | null): Promise<void> {
  await request.put('/models/authorization', { model_id: modelId, scope, principal, allowed })
}

export interface ModelExtraGrants {
  models: string[]
  catalog: string[]
  role_models: string[]
  disabled: string[]
  display_names: Record<string, string>
  revision: number
}

export async function getModelExtraGrants(userId: number): Promise<ModelExtraGrants> {
  return (await request.get(`/models/users/${userId}/extra-grants`)).data
}

export async function saveModelExtraGrants(userId: number, models: string[], revision: number): Promise<void> {
  await request.put(`/models/users/${userId}/extra-grants`, { models, revision })
}

export async function runModelTest(modelId: string, prompt: string, maxTokens: number): Promise<ModelTestResult> {
  const { data } = await request.post<ModelTestResult>('/admin/models/tests', {
    model_id: modelId, prompt, max_tokens: maxTokens,
  }, { timeout: 20000 })
  return data
}
