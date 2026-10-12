import request from './request'

export interface KnowledgeBaseInfo {
  id: number
  name: string
  description?: string | null
  embedding_model?: string | null
  scope?: string
  kind?: 'normal' | 'memory'
  vault_dir?: string | null
  owner_id?: number | null
  owner_name?: string | null
  can_manage?: boolean
  created_at?: string
  doc_count?: number
  ready_count?: number
  failed_count?: number
  processing_count?: number
}

// 个人记忆库名称前缀（与后端 MEMORY_KB_PREFIX 一致）：记忆库自动挂载到每个 Agent，
// Agent 绑定知识库的选项里无需也不应出现它
export const MEMORY_KB_PREFIX = '我的记忆-'

export interface KnowledgeDocInfo {
  id: number
  kb_id: number
  filename: string
  size: number
  status: string
  chunk_count: number
  error?: string | null
  title?: string | null
  vault_path?: string | null
  tags?: string[]
  category?: string | null
  is_markdown?: boolean
  created_at?: string
}

export interface MemoryArchiveInfo {
  id: number
  source_rel: string
  absorbed_at: string
  expires_at: string
}

export interface GraphNode {
  id: number | string
  title?: string
  name?: string
  is_ghost?: boolean
  is_pdf?: boolean
  is_tag?: boolean
  mem_kind?: string
  tags?: string[]
  category?: string | null
  degree?: number
  vault_path?: string
  filename?: string
  community?: number
  community_name?: string
}
export interface GraphEdge { source: number | string; target: number | string; kind: string; anchor?: string; snippet?: string }
export interface GraphCommunity { cid: number; color: string; label: string; count: number }
export interface GraphData { nodes: GraphNode[]; edges: GraphEdge[]; stats: { nodes: number; edges: number; orphans: number; ghosts: number; tags: number }; communities?: GraphCommunity[] }
export interface TreeNode { name: string; children?: TreeNode[]; doc_id?: number; status?: string }
export interface Backlink { src_id: number; src_title?: string; src_vault_path?: string; kind: string; anchor?: string; snippet?: string }

export async function listKnowledgeBases(): Promise<KnowledgeBaseInfo[]> { return (await request.get('/knowledge/bases')).data }
export async function createKnowledgeBase(data: { name: string; description?: string; scope?: string }) { return (await request.post('/knowledge/bases', data)).data }
export async function updateKnowledgeBase(kbId: number, data: { name?: string; description?: string }) { return (await request.patch(`/knowledge/bases/${kbId}`, data)).data }
export async function deleteKnowledgeBase(kbId: number) { return (await request.delete(`/knowledge/bases/${kbId}`)).data }
export async function listDocuments(kbId: number): Promise<KnowledgeDocInfo[]> { return (await request.get(`/knowledge/bases/${kbId}/documents`)).data }
export async function listMemoryArchives(kbId: number): Promise<MemoryArchiveInfo[]> { return (await request.get(`/knowledge/bases/${kbId}/archives`)).data }
export async function restoreMemoryArchive(archiveId: number) { return (await request.post(`/knowledge/archives/${archiveId}/restore`)).data }
export async function uploadDocuments(kbId: number, files: File[]) {
  const form = new FormData(); files.forEach((f) => form.append('files', f))
  return (await request.post(`/knowledge/bases/${kbId}/documents`, form, { headers: { 'Content-Type': 'multipart/form-data' } })).data
}
export async function deleteDocument(docId: number) { return (await request.delete(`/knowledge/documents/${docId}`)).data }
export async function softDeleteDocument(docId: number) { return (await request.post(`/knowledge/docs/${docId}/soft-delete`)).data }
export async function hardDeleteDocument(docId: number) { return (await request.delete(`/knowledge/docs/${docId}/hard`)).data }
export async function restoreDocument(docId: number) { return (await request.post(`/knowledge/docs/${docId}/restore`)).data }
export async function retryDocument(docId: number) { return (await request.post(`/knowledge/docs/${docId}/retry`)).data }
export async function updateDocContent(docId: number, content: string): Promise<KnowledgeDocInfo & { content?: string | null }> { return (await request.put(`/knowledge/docs/${docId}/content`, { content })).data }
export async function searchKnowledge(query: string, kbIds?: number[], topK = 5) { return (await request.post('/knowledge/search', { query, kb_ids: kbIds, top_k: topK })).data }

// ---- Obsidian 模块 ----
export async function linkVault(kbId: number, vaultDir: string) { return (await request.post(`/knowledge/bases/${kbId}/link-vault`, { vault_dir: vaultDir })).data }
export async function importVault(kbId: number, file: File) {
  const form = new FormData(); form.append('file', file)
  return (await request.post(`/knowledge/bases/${kbId}/import-vault`, form, { headers: { 'Content-Type': 'multipart/form-data' } })).data
}
export async function reindexVault(kbId: number) { return (await request.post(`/knowledge/bases/${kbId}/reindex`)).data }
export async function getGraph(kbId: number, params: { tags?: string; folder?: string; depth?: number; focus?: number; include_orphans?: boolean; include_tags?: boolean } = {}): Promise<GraphData> {
  return (await request.get(`/knowledge/bases/${kbId}/graph`, { params })).data
}
export async function getVaultTree(kbId: number): Promise<TreeNode> { return (await request.get(`/knowledge/bases/${kbId}/tree`)).data }
export async function getVaultTags(kbId: number): Promise<{ tag: string; count: number }[]> { return (await request.get(`/knowledge/bases/${kbId}/tags`)).data }
export async function getDoc(docId: number): Promise<KnowledgeDocInfo & { content?: string | null }> { return (await request.get(`/knowledge/docs/${docId}`)).data }
export async function getBacklinks(docId: number): Promise<Backlink[]> { return (await request.get(`/knowledge/docs/${docId}/backlinks`)).data }
export async function classifySuggest(docId: number) { return (await request.post(`/knowledge/docs/${docId}/classify-suggest`)).data }
export async function setDocCategory(docId: number, category: string) { return (await request.post(`/knowledge/docs/${docId}/category`, { category })).data }
export async function createNote(docId: number, name: string, content?: string) { return (await request.post(`/knowledge/docs/${docId}/create`, { name, content })).data }
export async function writeFrontmatter(docId: number) { return (await request.post(`/knowledge/docs/${docId}/write-frontmatter`)).data }
export async function getChunk(docId: number, chunkIndex: number) { return (await request.get(`/knowledge/docs/${docId}/chunk/${chunkIndex}`)).data }
export async function getRawText(docId: number) { return (await request.get(`/knowledge/docs/${docId}/raw`)).data }
