import request from './request'

export type ResourceKind = 'skill' | 'package'
export interface ResourceVersion { id: string; number: number; label: string; created_at: string; current: boolean; files: number }
export interface ChangeJob { id: string | null; status: string; message: string; files: { path: string; change: string }[]; members: Record<string, string[]>; result?: { version_id?: string; unchanged: boolean } }
export interface ResourceHistory { resource_type: ResourceKind; resource_id: number; name: string; version_id: string; revision: number; editable: boolean; versions: ResourceVersion[]; jobs: ChangeJob[] }
export interface FileEntry { name: string; path: string; kind: 'directory' | 'file'; bytes: number | null }
export interface FileContent { path: string; bytes: number; sha256: string; kind: 'text' | 'image' | 'binary'; content: string | null; editable: boolean }
const root = (kind: ResourceKind, id: number) => `/admin/skill-resources/${kind}/${id}`
export const resourceHistory = (kind: ResourceKind, id: number) => request.get<ResourceHistory>(root(kind, id) + '/history').then((result) => result.data)
export const resourceEntries = (kind: ResourceKind, id: number, version: string, path: string) => request.get<FileEntry[]>(root(kind, id) + `/versions/${version}/entries`, { params: { path } }).then((result) => result.data)
export const resourceContent = (kind: ResourceKind, id: number, version: string, path: string) => request.get<FileContent>(root(kind, id) + `/versions/${version}/content`, { params: { path } }).then((result) => result.data)
export const resourceDownload = (kind: ResourceKind, id: number, version: string, path: string, inline = false) => request.get<Blob>(root(kind, id) + `/versions/${version}/download`, { params: { path, inline }, responseType: 'blob' }).then((result) => result.data)
export const resourceEdit = (kind: ResourceKind, id: number, values: { path: string; content: string; version_id: string; revision: number; sha256: string }) => request.post<ChangeJob>(root(kind, id) + '/edits', values).then((result) => result.data)
export const resourceUpload = (kind: ResourceKind, id: number, file: File) => {
  const body = new FormData(); body.append('file', file)
  return request.post<ChangeJob>(root(kind, id) + '/uploads', body, { timeout: 120000 }).then((result) => result.data)
}
export const resourceJob = (kind: ResourceKind, id: number, job: string) => request.get<ChangeJob>(root(kind, id) + '/jobs/' + job).then((result) => result.data)
export const resourceConfirm = (kind: ResourceKind, id: number, job: string) => request.post<ChangeJob>(root(kind, id) + '/jobs/' + job + '/confirm').then((result) => result.data)
export const resourceCancel = (kind: ResourceKind, id: number, job: string) => request.post<ChangeJob>(root(kind, id) + '/jobs/' + job + '/cancel').then((result) => result.data)
