import request from './request'

export interface HubMember { path: string; title: string; summary: string; skill_id: number; version_id: string }
export interface HubFile { path: string; bytes: number; sha256: string }
export interface HubDetails {
  title: string; summary: string; category: string; tags: string[]; description: string
  example: string; requirements: string; risks: string; license: string; icon?: string
  dependencies?: Record<string, unknown[]>
}
export interface HubVersion {
  id: string; number: number; state: string; test_status: string; test_report: Record<string, unknown>
  metadata: HubDetails; created_at: string
}
export interface HubResource {
  id: string; kind: 'skill' | 'package'; title: string; summary: string; author: string
  owner_id: number | null; origin: string; allow_fork: boolean; archived: boolean; blocked: boolean
  revision: number; published_version_id?: string; draft_version_id?: string; selected_version_id?: string
  source_title?: string; source_version_id?: string; source_resource_id?: string; created_at: string
  can_preview?: boolean; members?: HubMember[]
  provenance?: { title: string; version_id: string }[]; versions?: HubVersion[]; files?: HubFile[]; metadata?: HubDetails; share_count?: number
  listing_id?: string; listing_status?: string; update_available?: boolean; installed_from?: string; share_id?: string
}
export interface HubShare {
  id: string; resource_id: string; title: string; recipient: string; author: string; shared_to: number
  version_id: string; proposed_version_id?: string; allow_fork: boolean; expires_at?: string; active: boolean
}
export interface HubListing {
  id: string; resource_id: string; kind: string; author: string; author_id: number; status: string
  version_id: string; details: HubDetails; install_count: number; rating_avg: number; rating_count: number
  installed_resource_id?: string; published_at?: string; review_note?: string; files?: HubFile[]
  can_preview?: boolean; members?: HubMember[]
  history?: { version_id: string; details: HubDetails; reviewed_at: string }[]; test_report?: Record<string, unknown>; source_title?: string
}
export interface HubRequest {
  id: string; listing_id: string; resource_id: string; version_id: string; status: string
  author: string; author_id: number; kind: string; details: HubDetails; review_note?: string
  files: HubFile[]; test_report: Record<string, unknown>; current_version_id?: string; created_at: string
}
export interface HubReview { id: string; user_id: number; author: string; rating: number; comment: string; version_id: string; author_reply?: string }
export interface FilePreview { path: string; kind: string; content: string | null; editable: boolean; bytes: number }
export interface FileChange { path: string; action: string }
const hub = '/skill-hub'
export const hubSystem = () => request.get<HubResource[]>(`${hub}/system`).then(r => r.data)
export const hubMine = () => request.get<HubResource[]>(`${hub}/my`).then(r => r.data)
export const hubDetail = (id: string) => request.get<HubResource>(`${hub}/resources/${id}`).then(r => r.data)
export const hubCreate = (body: { title: string; summary: string; content: string }) => request.post<HubResource>(`${hub}/resources`, body).then(r => r.data)
export const hubFork = (id: string) => request.post<HubResource>(`${hub}/resources/${id}/fork`, {}).then(r => r.data)
export const hubEdit = (id: string, body: { revision: number; path: string; content?: string; operation?: string; new_path?: string }) => request.post<HubResource>(`${hub}/resources/${id}/files`, body).then(r => r.data)
export const hubFile = (id: string, version: string, path: string) => request.get<FilePreview>(`${hub}/resources/${id}/versions/${version}/file`, { params: { path } }).then(r => r.data)
export const hubDownload = (id: string, version: string, path: string) => request.get(`${hub}/resources/${id}/versions/${version}/download`, { params: { path }, responseType: 'blob' }).then(r => r.data as Blob)
export const hubMetadata = (id: string, revision: number, details: HubDetails) => request.put<HubResource>(`${hub}/resources/${id}/metadata`, { revision, details }).then(r => r.data)
export const hubTest = (id: string, script?: string, sample_input = '', scripts: Record<string, string> = {}) => request.post<HubVersion>(`${hub}/resources/${id}/test`, { script: script || null, sample_input, scripts }, { timeout: 300000 }).then(r => r.data)
export const hubPublish = (id: string, revision: number, version_id?: string) => request.post<HubResource>(`${hub}/resources/${id}/publish`, { revision, version_id }).then(r => r.data)
export const hubArchive = (id: string, value: boolean) => request.put(`${hub}/resources/${id}/archive`, { value })
export const hubShares = (incoming = false) => request.get<HubShare[]>(`${hub}/shares`, { params: { incoming } }).then(r => r.data)
export const hubShare = (id: string, body: { shared_to: number; allow_fork: boolean; expires_at?: string }) => request.post(`${hub}/resources/${id}/share`, body)
export const hubRevoke = (id: string) => request.delete(`${hub}/shares/${id}`)
export const hubAccept = (id: string) => request.post(`${hub}/shares/${id}/accept`)
export const hubUsers = (keyword = '') => request.get<{ id: number; name: string }[]>(`${hub}/users`, { params: { keyword } }).then(r => r.data)
export const hubPublications = () => request.get<HubRequest[]>(`${hub}/publications`).then(r => r.data)
export const hubSubmit = (id: string, details: HubDetails) => request.post(`${hub}/resources/${id}/submit`, details)
export const hubWithdraw = (id: string) => request.post(`${hub}/requests/${id}/withdraw`)
export const hubListings = (params: Record<string, string>) => request.get<HubListing[]>(`${hub}/marketplace`, { params }).then(r => r.data)
export const hubListing = (id: string) => request.get<HubListing>(`${hub}/marketplace/${id}`).then(r => r.data)
export const hubCommunityFile = (id: string, path: string, version_id?: string) => request.get<FilePreview>(`${hub}/marketplace/${id}/file`, { params: { path, version_id } }).then(r => r.data)
export const hubInstall = (id: string, another = false) => request.post<HubResource>(`${hub}/marketplace/${id}/install`, {}, { params: { another }, timeout: 300000 }).then(r => r.data)
export const hubUpgrade = (id: string) => request.post<{ resource: HubResource; mode: string }>(`${hub}/resources/${id}/upgrade`, {}, { timeout: 300000 }).then(r => r.data)
export const hubUninstall = (id: string) => request.post(`${hub}/resources/${id}/uninstall`)
export const hubReviews = (id: string) => request.get<HubReview[]>(`${hub}/marketplace/${id}/reviews`).then(r => r.data)
export const hubReview = (id: string, rating: number, comment: string) => request.post(`${hub}/marketplace/${id}/reviews`, { rating, comment })
export const hubDeleteReview = (id: string) => request.delete(`${hub}/reviews/${id}`)
export const hubReply = (id: string, note: string) => request.post(`${hub}/reviews/${id}/reply`, { note })
export const hubReport = (id: string, note: string) => request.post(`${hub}/marketplace/${id}/report`, { note })
export const hubImport = (file: File, title: string, summary: string) => {
  const form = new FormData(); form.append('file', file); form.append('title', title); form.append('summary', summary)
  return request.post<HubResource>(`${hub}/import`, form, { timeout: 300000 }).then(r => r.data)
}
export const hubUpload = (id: string, file: File, revision: number, confirm: boolean) => {
  const form = new FormData(); form.append('file', file); form.append('revision', String(revision)); form.append('confirm', String(confirm))
  return request.post<{ changes: FileChange[]; revision: number; confirmed: boolean }>(`${hub}/resources/${id}/upload`, form, { timeout: 300000 }).then(r => r.data)
}
export const hubDiff = (id: string, before: string, after: string) => request.get<FileChange[]>(`${hub}/resources/${id}/diff`, { params: { before, after } }).then(r => r.data)
const admin = '/admin/skill-hub'
export const hubRequests = (status = 'pending_review') => request.get<HubRequest[]>(`${admin}/requests`, { params: { status } }).then(r => r.data)
export const hubDecision = (id: string, approved: boolean, note: string) => request.post(`${admin}/requests/${id}/decision`, { approved, note })
export const hubRequestFile = (id: string, path: string) => request.get<FilePreview>(`${admin}/requests/${id}/file`, { params: { path } }).then(r => r.data)
export const hubRequestDiff = (id: string) => request.get<FileChange[]>(`${admin}/requests/${id}/diff`).then(r => r.data)
export const hubAdminListings = () => request.get<HubListing[]>(`${admin}/listings`).then(r => r.data)
export const hubOffline = (id: string, note: string) => request.post(`${admin}/listings/${id}/offline`, { note })
export const hubBlock = (id: string, note: string, version_id?: string, descendants = false) => request.post(`${admin}/resources/${id}/block`, { note, version_id, descendants })
export const hubAdminResources = () => request.get<HubResource[]>(`${admin}/resources`).then(r => r.data)
export const hubForkPolicy = (id: string, value: boolean) => request.put(`${admin}/resources/${id}/fork-policy`, { value })
export interface HubReportItem { id: string; listing_id: string; reason: string; status: string; resolution?: string }
export const hubReports = () => request.get<HubReportItem[]>(`${admin}/reports`).then(r => r.data)
export const hubResolve = (id: string, note: string) => request.post(`${admin}/reports/${id}/resolve`, { note })
export const hubHideReview = (id: string, value: boolean) => request.put(`${admin}/reviews/${id}/hidden`, { value })

export const hubVersionFiles = (id: string, version: string) => request.get<HubFile[]>(`${hub}/resources/${id}/versions/${version}/files`).then(r => r.data)
export const hubAuthorOffline = (id: string, note: string) => request.post(`${hub}/resources/${id}/offline`, { note })

export const hubImportFolder = (files: File[], title: string, summary: string) => {
  const body = new FormData()
  files.forEach(file => body.append('files', file))
  body.append('paths', JSON.stringify(files.map(f => f.webkitRelativePath || f.name)))
  body.append('title', title); body.append('summary', summary)
  return request.post<HubResource>(`${hub}/import-folder`, body, { timeout: 120000 }).then(r => r.data)
}
export const hubTrial = (id: string) => request.get<{ skills: string[]; version_id: string }>(`${hub}/resources/${id}/trial`).then(r => r.data)

export interface HubModerationReview { id: string; listing_id: string; rating: number; comment: string; hidden: boolean }
export const hubModerationReviews = () => request.get<HubModerationReview[]>('/admin/skill-hub/reviews').then(r => r.data)
