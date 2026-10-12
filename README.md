# 企业级 Agent 平台

基于 AgentScope 2.0 的企业级AI Agent管理平台

## 项目结构

``
program_code/
├── backend/                 # 后端服务（FastAPI + AgentScope 2.0）
│   ├── app/
│   │   ├── api/v1/         # API路由
│   │   ├── database_models/# 数据库模型
│   │   ├── schemas/        # 数据验证
│   │   ├── services/       # 业务逻辑
│   │   ├── core/           # 核心模块（安全、依赖注入）
│   │   ├── agents/         # AgentScope集成
│   │   ├── config.py       # 配置管理
│   │   ├── database.py     # 数据库连接
│   │   └── main.py         # 主入口
│   ├── init_db.py          # 数据库初始化脚本
│   ├── requirements.txt    # Python依赖
│   └── .env                # 环境变量配置
│
├── frontend/               # 前端应用（React + TypeScript）
│   ├── src/
│   │   ├── api/           # API调用
│   │   ├── pages/         # 页面组件
│   │   ├── store/         # 状态管理
│   │   ├── App.tsx        # 主应用
│   │   └── main.tsx       # 入口文件
│   ├── package.json       # 前端依赖
│   └── vite.config.ts     # Vite配置
│
└── README.md              # 本文件
``

## 快速开始

### 1. 配置后端

``bash
# 进入后端目录
cd backend

# 激活conda环境
conda activate agentscope

# 复制并编辑环境变量
# 在 .env 文件中填入你的 DASHSCOPE_API_KEY
# 申请地址: https://dashscope.console.aliyun.com/

# 初始化数据库（创建表和管理员账号）
python init_db.py

# 启动后端服务
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
``

后端启动后访问 http://localhost:8000/docs 查看API文档

### 2. 配置前端

``bash
# 进入前端目录
cd frontend

# 安装依赖（首次需要）
npm install

# 启动前端开发服务器
npm run dev
``

前端启动后访问 http://localhost:5173

### 3. 登录系统

- 默认管理员账号: ``admin``
- 初始密码: ``Initial@123456``（首次登录强制修改）

## 技术栈

### 后端
- **框架**: FastAPI 0.139.2
- **AI框架**: AgentScope 2.0.5（阿里开源）
- **数据库**: SQLAlchemy 2.0.36 + SQLite（开发）
- **认证**: python-jose (JWT) + passlib (bcrypt)
- **数据验证**: Pydantic 2.13.4

### 前端
- **框架**: React 18.3 + TypeScript 5.6
- **构建工具**: Vite 6.0
- **UI库**: Ant Design 5.22
- **路由**: React Router 6.28
- **状态管理**: Zustand 5.0
- **HTTP客户端**: Axios 1.7

### AI模型
- **通义千问 (Qwen)**: 通过 DashScope API 调用
- **模型名称**: qwen-max（可在 .env 中修改）

## 核心功能

✅ 用户认证（JWT登录 + RBAC权限控制）  
✅ Agent管理（创建、配置、删除Agent）  
✅ 实时对话（WebSocket流式输出）  
✅ 会话管理（多会话、历史记录）  
✅ 权限控制（不同用户访问不同Agent）  
✅ 管理后台（用户管理、Agent配置）

## 下一步

- [ ] 完善管理后台（用户管理、Agent配置界面）
- [ ] 添加Docker容器化部署配置
- [ ] 切换到PostgreSQL + Redis + MongoDB
- [ ] 添加更多Agent类型（RAG、任务执行等）
- [ ] 优化UI/UX体验

## 常见问题

**Q: 如何添加新的Agent？**  
A: 登录管理员账号，访问管理后台创建（功能开发中），或直接在数据库中插入agents表记录。

**Q: 如何修改AI模型？**  
A: 在 .env 文件中修改 MODEL_NAME，支持的值：qwen-turbo, qwen-plus, qwen-max 等。

**Q: WebSocket连接失败？**  
A: 确保后端和前端都在运行，且后端的CORS配置包含前端地址。

## 许可证

MIT License





## 启动命令
终端1（后端）：
conda activate agentscope
cd <项目根目录>/backend
python -m uvicorn app.main:app --reload --port 8000

终端2（前端）：

cd <项目根目录>/frontend
npm run dev

然后打开 http://localhost:5173