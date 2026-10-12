// Chat page shared types

export interface Session {
  id: number
  title: string
  agent_id: number
  created_at: string
}

// ask_user 选项卡片的单个方案
export interface AskUserOption {
  key: string          // A/B/C...
  label: string        // 方案标题
  description?: string // 方案说明（利弊）
}

// 需求采集表单（CollectRequirements）的单个问题
export interface IntakeQuestion {
  id: string
  label: string
  type: 'choice' | 'multi' | 'text' | 'number'
  options?: string[]
  placeholder?: string
  unit?: string
 required?: boolean
 assume?: string      // 用户不作答时采用的默认值（后端会把它记为「假设，待确认」）
  allow_other?: boolean  // choice/multi 是否允许在选项外自定义作答
  multiline?: boolean    // text 是否多行输入
}

// 官方 Task 工具（TaskCreate/TaskUpdate）维护的任务项（计划面板渲染）
export interface OfficialTaskItem {
  id: string
  subject: string
  description?: string
  state: 'pending' | 'in_progress' | 'completed'
}

export interface MessageStep {
  // external_ask：官方外部执行卡片（AskUserChoice 决策 / CollectRequirements 需求表单）；
  // tasks：官方 Task 工具的任务列表快照（计划面板渲染）
  type: 'thinking' | 'tool_call' | 'external_ask' | 'tasks'
  content?: string
  name?: string
  tool_call_id?: string
  args?: string
  // 工具调用的一句话描述（后端解析参数时产出，显示在工具框头部）
  description?: string
  status?: 'calling' | 'done'
  state?: string
  output?: string
  // 所属模型调用轮次：计划面板按 round 把工具调用分组为"阶段 N"
  round?: number
  // ===== external_ask 专用字段（官方外部执行 / HITL）=====
  request_id?: string   // 官方 tool_call_id，external_reply 回传用
  reply_id?: string
  question?: string
  options?: AskUserOption[]
  // 交互形态：choice=方案单选卡（默认，兼容历史数据），intake=需求采集表单
  kind?: 'choice' | 'intake'
  // kind=intake 时的题目定义
  questions?: IntakeQuestion[]
  answer?: string
  resolved_by?: 'user' | 'timeout' | 'cancel' | 'interrupt'
  timeout_seconds?: number
  // ===== tasks 专用字段（官方任务列表快照）=====
  tasks?: OfficialTaskItem[]
}

export interface FileOutput {
  filename: string
  download_url: string
  size: number
}

// 服务预览：Agent 在会话沙箱里起的 Web 服务，由平台发布成可直接访问的临时地址。
// status: active=可访问 / expired=沙箱已回收或已到期 / blocked=服务只绑了回环地址
//         / error=发布失败（detail 里有原因）
export interface ServicePreview {
  id: number
  session_id: number
  status: 'active' | 'expired' | 'blocked' | 'error' | string
  // 对外访问地址；基址未配置或已失效时为空串
  url: string
  host_port: number | null
  target_port: number
  bind_addr?: string | null
  entry_command?: string
  process_name?: string
  project_dir?: string
  // 平台打包的源码压缩包（含 HOW_TO_RUN.md），结构与 FileOutput 一致
  bundle?: FileOutput | null
  detail?: string
  created_at?: string
  expires_at?: string
  seconds_left?: number
}

export interface TokenUsage {
  input_tokens: number
  output_tokens: number
  cache_input_tokens?: number
  cache_rate?: number
}

// 用户上传的附件
export interface Attachment {
  name: string
  stored_name: string
}

// 上传后待发送的文件（含展示与 Agent 所需信息）
export interface UploadedFile {
  file_path: string
  stored_name: string
  original_name: string
  size?: number
}

// 任务运行中排队（延时发送）的单项：输入文本 + 附件快照
export interface PendingInput {
  content: string
  files: UploadedFile[]
}

export interface ChatMessage {
  id?: number
  role: 'user' | 'agent'
  content: string
  created_at?: string
  steps?: MessageStep[]
  file_outputs?: FileOutput[]
  service_previews?: ServicePreview[]
  token_usage?: TokenUsage
  run_id?: string
  tool_call_count?: number
  attachments?: Attachment[]
  // 回复用时（秒）：流式结束时前端本地计时，刷新后来自后端落库的 metadata
  elapsed_seconds?: number
}
