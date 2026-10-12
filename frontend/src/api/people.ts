import request from './request'

export interface Department {
  id: number
  code: string
  name: string
  parent_id: number | null
  is_active: boolean
  revision: number
}

export interface RoleTemplate {
  id: number
  code: string
  name: string
  permissions: string[]
  is_active: boolean
  revision: number
}

export interface RoleAssignment {
  id: number
  template_id: number
  code: string
  name: string
  scope_type: string
  scope_json: string
}

export interface Person {
  id: number
  username: string
  account: string | null
  email: string | null
  phone: string | null
  role: 'admin' | 'operator' | 'viewer'
  is_active: boolean
  department_id: number | null
  department_name: string | null
  job_title: string | null
  external_id: string | null
  source: string | null
  employment_status: 'active' | 'departed' | null
  revision: number
  created_at: string | null
  updated_at: string | null
  last_login: string | null
  agent_ids: number[]
  skill_grants: { skill: number[]; package: number[] }
  roles: RoleAssignment[]
  usage: { sessions: number; messages: number }
  can_edit?: boolean
}

export interface PeoplePage {
  items: Person[]
  total: number
  page: number
  page_size: number
}

export interface PeopleFilters {
  page: number
  page_size: number
  search?: string
  department_id?: number
  role?: string
  template_id?: number
  status?: string
  sort_by?: string
  sort_order?: string
}

export type ScopeType = 'global' | 'department' | 'departments' | 'self' | 'resource'

export async function getOrgAccess(): Promise<{ can_view_people: boolean; can_manage: boolean }> {
  return (await request.get('/admin/access')).data
}

export async function getPeople(params: PeopleFilters): Promise<PeoplePage> {
  return (await request.get('/admin/people', { params })).data
}

export async function getPerson(personId: number): Promise<Person> {
  return (await request.get(`/admin/people/${personId}`)).data
}

export async function editPerson(personId: number, values: Record<string, unknown>): Promise<Person> {
  return (await request.put(`/admin/people/${personId}`, values)).data
}

export async function editResources(personId: number, values: {
  revision: number; agent_ids?: number[]; skill_ids?: number[]; package_ids?: number[]
}): Promise<Person> {
  return (await request.put(`/admin/people/${personId}/resources`, values)).data
}

export async function addRole(personId: number, values: {
  revision: number; template_id: number; scope_type: ScopeType; scope?: unknown
}): Promise<Person> {
  return (await request.post(`/admin/people/${personId}/roles`, values)).data
}

export async function removeRole(personId: number, assignmentId: number, revision: number): Promise<Person> {
  return (await request.delete(`/admin/people/${personId}/roles/${assignmentId}`, {
    data: { revision },
  })).data
}

export async function getDepartments(): Promise<Department[]> {
  return (await request.get('/admin/departments')).data
}

export async function addDepartment(values: { code: string; name: string; parent_id?: number | null }): Promise<Department> {
  return (await request.post('/admin/departments', values)).data
}

export async function editDepartment(id: number, values: {
  revision: number; name?: string; parent_id?: number | null; is_active?: boolean
}): Promise<Department> {
  return (await request.put(`/admin/departments/${id}`, values)).data
}

export async function getRoleTemplates(): Promise<RoleTemplate[]> {
  return (await request.get('/admin/role-templates')).data
}

export async function addRoleTemplate(values: {
  code: string; name: string; permissions: string[]
}): Promise<RoleTemplate> {
  return (await request.post('/admin/role-templates', values)).data
}

export async function editRoleTemplate(id: number, values: {
  revision: number; name?: string; permissions?: string[]; is_active?: boolean
}): Promise<RoleTemplate> {
  return (await request.put(`/admin/role-templates/${id}`, values)).data
}
