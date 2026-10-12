import type { ReactNode } from 'react'
import {
  MessageOutlined,
  RobotOutlined,
  AppstoreOutlined,
  ApiOutlined,
  ScheduleOutlined,
  UnorderedListOutlined,
  DatabaseOutlined,
  ApartmentOutlined,
  FileTextOutlined,
  SearchOutlined,
  FolderOutlined,
  SyncOutlined,
  DeleteOutlined,
  EyeOutlined,
  DashboardOutlined,
  CalendarOutlined,
  TeamOutlined,
  SettingOutlined,
  BulbOutlined,
  ThunderboltOutlined,
  UserOutlined,
  BarChartOutlined,
  MailOutlined,
  HomeOutlined,
} from '@ant-design/icons'

export interface FeatureCard {
  icon: ReactNode
  title: string
  description: string
  route?: string
  screenshot?: string
}

export interface FeatureCategory {
  key: string
  tab: string
  icon: ReactNode
  adminOnly?: boolean
  cards: FeatureCard[]
}

export const FEATURE_CATEGORIES: FeatureCategory[] = [
  {
    key: 'core-chat',
    tab: '核心对话',
    icon: <MessageOutlined />,
    cards: [
      {
        icon: <MessageOutlined />,
        title: '开始问诊',
        description: '与 AI Agent 进行多轮对话的核心入口。支持流式输出实时显示、文件附件上传、会话历史浏览与切换、草稿自动保存。对话界面采用三栏布局：左侧会话列表、中间消息区、右侧扩展面板（技能 / MCP / 计划 / 消息导航），可根据需要展开或收起。',
        route: '/chat',
        screenshot: 'chat-main.png',
      },
      {
        icon: <RobotOutlined />,
       title: 'Agent 选择',
       description: '从预置 Agent（工具助手、数据分析师）或管理员自定义 Agent 中选择对话角色。不同 Agent 具备不同的系统提示词、模型配置、技能绑定和知识库绑定。首页和对话页均可切换 Agent，切换后立即生效。',
        route: '/?open_agent=1',
     },
     {
       icon: <AppstoreOutlined />,
       title: '技能面板（Skill Panel）',
       description: '对话过程中挂载技能以增强 Agent 能力。技能分为三种作用域：可选 Skill（用户自主勾选）、工具助手内置 Skill（自动加载）、数据分析师内置 Skill（自动加载）。面板支持搜索筛选和一键勾选，勾选状态在会话内持久化。',
        route: '/chat?panel=skill',
     },
     {
       icon: <ApiOutlined />,
       title: 'MCP 工具调用',
       description: '对话中选择 MCP 服务器，Agent 可自动调用外部工具（如网络搜索、数据库查询、文件操作等）。工具调用过程和返回结果在消息流中完整展示，支持多轮工具链式调用。MCP 选择面板列出了所有已启用服务器及其工具列表。',
        route: '/chat?panel=mcp',
     },
     {
       icon: <ScheduleOutlined />,
       title: '任务计划（Plan Panel）',
       description: '当 Agent 执行复杂任务时，Plan 面板可视化展示分步执行计划：每个步骤的状态（待执行 / 执行中 / 完成 / 失败）、步骤描述和执行结果。用户可中途审批或取消计划，实现人机协作的任务管控。',
        route: '/chat?panel=plan',
     },
     {
       icon: <UnorderedListOutlined />,
       title: '消息导航面板',
       description: '右侧消息导航面板按时间线列出当前会话的所有消息，支持快速定位到任意一条消息。配合缓存指示器（Cache Indicator），标记哪些消息来自缓存、哪些来自实时推理，帮助用户理解响应来源。',
        route: '/chat?panel=nav',
     },
    ],
  },
  {
    key: 'skills-knowledge',
    tab: '技能与知识',
    icon: <AppstoreOutlined />,
    cards: [
      {
        icon: <AppstoreOutlined />,
        title: 'Skill 广场',
        description: '浏览全部已安装技能的统一入口。卡片式展示每个技能的名称、描述、角色要求（观察者 / 操作员 / 管理员）和状态。支持按技能包展开查看成员技能列表，点击即可进入对话并自动挂载该技能。技能包支持上传部署，进度实时展示。',
        route: '/skills',
        screenshot: 'skills-square.png',
      },
      {
        icon: <DatabaseOutlined />,
        title: '知识库管理',
        description: '创建、编辑、删除知识库。上传文档（PDF / Word / Excel / PPT / TXT / Markdown 等），系统自动进行文档解析、分块、向量化与索引。支持文档软删除与恢复、重试索引失败文档。知识库可绑定到 Agent，在对话中自动检索相关知识。',
        route: '/knowledge',
        screenshot: 'knowledge-main.png',
      },
      {
        icon: <ApartmentOutlined />,
       title: '知识图谱',
       description: '可视化展示文档之间的关联关系（Graph View），以力导向图呈现文档节点和引用边。支持反向链接（Backlinks）查看：选中任一文档，即可看到所有引用它的其他文档。Vault 目录树以文件管理器形式浏览全部文档，支持搜索过滤。',
        route: '/knowledge?view=graph',
     },
      {
        icon: <FileTextOutlined />,
        title: '文档阅读器',
        description: '内置 Doc Reader 组件，支持在知识库页面直接阅读文档全文。提供文档分类标注功能，支持 AI 分类建议（自动推荐文档所属类别）。可查看文档原始文本、分块详情，并对索引失败的文档一键重试。',
        route: '/knowledge',
      },
      {
        icon: <SearchOutlined />,
        title: '语义检索',
        description: '跨知识库进行语义搜索，基于向量相似度返回最相关的文档片段（chunk）。搜索结果展示匹配文本片段、来源文档、相似度评分。对话中 Agent 也会自动检索知识库，将相关上下文注入到回答中，实现 RAG（检索增强生成）。',
        route: '/knowledge',
      },
    ],
  },
  {
    key: 'files-tools',
    tab: '文件与工具',
    icon: <FolderOutlined />,
    cards: [
      {
        icon: <FolderOutlined />,
        title: '文件中心',
        description: '统一管理用户上传文件和 Agent 产出文件。提供「全部」和「回收站」两个视图，按文件类型（Excel / Word / PDF / PPT / 压缩包 / 图片 / 文本）自动分类着色。支持文件搜索、大小统计、上传时间排序。有权限的用户可查看全部用户文件。',
        route: '/files',
        screenshot: 'files-main.png',
      },
      {
        icon: <SyncOutlined />,
        title: '磁盘对账',
        description: '对比本地磁盘文件与数据库记录，检测并修复不一致情况：找出数据库有记录但磁盘丢失的文件、磁盘存在但数据库无记录的孤儿文件。一键同步修复，确保数据一致性。',
        route: '/files',
      },
      {
        icon: <DeleteOutlined />,
       title: '文件管理操作',
       description: '支持文件重命名、软删除（移入回收站）、从回收站恢复、彻底删除四种操作。有权限的用户可管理他人文件（按细粒度权限码控制：重命名 / 软删除 / 恢复 / 彻底删除）。批量下载支持多选文件打包下载。',
        route: '/files?tab=trash',
     },
      {
        icon: <ApiOutlined />,
        title: 'MCP 工具管理',
        description: '添加、编辑、删除 MCP 服务器。支持 stdio（本地进程）和 http（远程服务）两种连接类型。配置启动命令、参数、环境变量。提供连接测试功能，实时返回测试结果。可查看每个服务器的可用工具列表和工具参数定义。支持启用 / 停用切换。',
        route: '/mcp',
        screenshot: 'mcp-main.png',
      },
      {
        icon: <EyeOutlined />,
        title: '会话预览',
        description: '从文件管理页面可直接预览与该文件关联的对话会话内容，无需切换到对话页面。通过 SessionPreviewModal 弹窗展示会话消息列表，快速了解文件的产生上下文。',
        route: '/files',
      },
    ],
  },
  {
    key: 'automation-monitoring',
    tab: '自动化与监控',
    icon: <ScheduleOutlined />,
    cards: [
      {
        icon: <ScheduleOutlined />,
        title: '调度任务',
        description: '创建和管理定时任务，支持四种触发类型：定时（Cron 表达式）、一次性（指定时间执行一次）、手动触发、失败重试。频率快捷设置：每天 / 每周 / 工作日 / 每月。支持任务统计（总数 / 活跃 / 暂停）、执行历史列表（运行中 / 成功 / 失败 / 跳过 / 超时）、立即执行、编辑、删除与恢复。',
        route: '/scheduled-tasks',
        screenshot: 'scheduled-tasks.png',
      },
      {
        icon: <DashboardOutlined />,
        title: '监控总览',
        description: '全局运营数据看板：对话总数、Token 消耗量、活跃用户数等核心指标。趋势图支持按模型或 Agent 分组对比，可切换 7 / 30 / 90 天时间范围。90 天日历热力图直观展示每日活跃度，颜色深浅代表对话量。',
        route: '/monitoring',
        screenshot: 'monitoring-main.png',
      },
      {
        icon: <CalendarOutlined />,
        title: '逐日下钻分析',
        description: '点击日历热力图中的某一天，展开当日对话列表。每条记录显示会话标题、Agent、模型、消息数、Token 消耗。进一步点击可进入会话明细抽屉，查看完整的消息级对话内容，实现从宏观到微观的逐层下钻分析。',
        route: '/monitoring',
      },
      {
        icon: <TeamOutlined />,
        title: '用户级监控',
        description: '拥有监控管理权限的用户可查看任意用户的使用明细：进入用户监控页面，查看该用户的个人总览、趋势图和对话记录。支持在用户列表中快速跳转到任意用户的监控页面，实现精细化运营管理。',
        route: '/monitoring',
      },
    ],
  },
  {
    key: 'admin',
    tab: '管理后台',
    icon: <SettingOutlined />,
    adminOnly: true,
    cards: [
      {
        icon: <RobotOutlined />,
        title: 'Agent 管理',
        description: '新建、编辑、删除 Agent。配置项包括：名称、描述、Agent 类型、模型配置（从可用模型列表选择）、系统提示词（System Prompt）、技能绑定（多选）、知识库绑定、MCP 服务器绑定。两个基础 Agent（工具助手、数据分析师）不可删除，仅可编辑名称和描述。绑定面板支持为 Agent 配置专属知识库和 MCP 服务器。',
        route: '/admin',
        screenshot: 'admin-agents.png',
      },
      {
        icon: <AppstoreOutlined />,
        title: 'Skill 管理与技能包',
        description: '新建、编辑、删除技能，设置技能作用域（可选 / 工具助手内置 / 数据分析师内置）。技能包管理支持上传、更新、软删除、恢复、彻底删除。提供刷新技能缓存功能，手动触发后端重新扫描技能目录。技能管理表格以包 -> 技能的树形结构展示，支持展开 / 折叠。',
        route: '/admin',
      },
      {
        icon: <TeamOutlined />,
        title: '人员信息与权限',
        description: '用户账号管理入口。查看全部用户列表，分配角色（观察者 / 操作员 / 管理员）。细粒度权限配置采用树形权限码体系，覆盖六大模块：MCP 部署管理、知识库管理、文件中心、Agent 管理、Skill 管理、监控与运维管理。每个模块下可进一步配置子权限（如文件中心下的下载、管理、同步、彻底删除等），实现精确到操作的权限控制。',
        route: '/users',
        screenshot: 'users-admin.png',
      },
    ],
  },
  {
    key: 'personal',
    tab: '个人中心',
    icon: <UserOutlined />,
    cards: [
      {
        icon: <UserOutlined />,
        title: '个人信息',
        description: '查看和编辑个人资料：用户名、邮箱、手机号。头像上传支持图片裁剪（256x256 PNG 输出）。可设置 Agent 头像（对话中 Agent 的展示头像）。密码修改遵循安全策略（长度、复杂度校验）。页面同时展示当前角色标签（观察者 / 操作员 / 管理员）。',
        route: '/profile',
        screenshot: 'profile-main.png',
      },
      {
        icon: <BarChartOutlined />,
        title: '用量统计',
        description: '查看个人资源使用情况：Token 消耗量（按模型统计）、对话总数、文件存储占用。配额进度条直观展示使用比例，帮助用户了解自身资源消耗趋势。',
        route: '/profile',
      },
      {
        icon: <MailOutlined />,
        title: '邮箱密码',
        description: '设置邮箱密码，用于后端邮件通知功能（如调度任务执行结果通知、系统告警等）。密码加密存储，保障安全性。',
        route: '/profile',
      },
      {
        icon: <BulbOutlined />,
        title: '深色 / 浅色模式',
        description: '一键切换深色模式与浅色模式，全站即时生效。主题偏好持久化到 localStorage，下次登录自动恢复。深色模式采用 warm-ink 配色（#16181B 背景），浅色模式采用 warm-paper 配色（#FFFFFF 背景），文字对比度均满足 WCAG 4.5:1 标准。',
      },
      {
        icon: <ThunderboltOutlined />,
        title: '动态效果',
        description: '开关页面入场动画效果。开启后，页面元素以 anime.js 驱动的 stagger（逐个入场）和 grid-wave（网格波浪）动画依次出现，提升视觉体验。关闭后所有动画即时完成，适合追求效率或低性能设备。',
      },
      {
        icon: <HomeOutlined />,
       title: '首页总览',
       description: '首页展示平台欢迎信息和核心功能入口卡片（开始问诊、Skill 广场、知识库、监控与运维），一键跳转到对应功能。Agent 选择器在首页提供快速切换入口，选择后直接开始对话。',
       route: '/',
     },
    ],
  },
]
