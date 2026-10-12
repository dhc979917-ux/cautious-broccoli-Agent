from pydantic_settings import BaseSettings
from functools import lru_cache
from pydantic_settings import SettingsConfigDict
from pathlib import Path
import os

# ===== 目录路径（集中定义，全项目统一从这里取，不要各自用 os.path.dirname 推算）=====
BASE_DIR = Path(__file__).resolve().parent.parent    # backend/
# 容器化部署时可用环境变量把数据目录重定向到宿主机挂载目录（同路径挂载，
# DockerWorkspace sibling 容器的 bind mount 源要求宿主机真实存在，见 deploy/）；
# 本地开发不设环境变量时保持原相对路径行为
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", str(BASE_DIR / "uploads")))    # 用户上传文件目录
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", str(BASE_DIR / "outputs")))    # 技能生成的输出文件目录
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", str(BASE_DIR / "memory")))     # 用户长期记忆目录


'''读取配置'''


class Settings(BaseSettings):
     # 应用配置
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    APP_ENV: str = "development"

     # 日志级别（DEBUG/INFO/WARNING/ERROR）；留空按 APP_ENV 自动选择：
     # development→DEBUG，production→INFO
    LOG_LEVEL: str = ""

     # 允许跨域的前端来源（逗号分隔）。部署环境通过环境变量配置为实际访问地址
    CORS_ORIGINS: str = "http://0.0.0.0:3000,http://0.0.0.0:5173,http://localhost:5173,http://127.0.0.1:5173"
    
     # 数据库配置
    DATABASE_URL: str = "sqlite:///./app.db"
    
     # JWT配置
    JWT_SECRET_KEY: str = "your-super-secret-key"
    JWT_SECRET_KEY_FILE: str = "/secrets/jwt_secret_key"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440  # 24小时
    # 窗口感知过期：基础有效期 + 强制重登窗口
    # 基础有效期到期后，若落在 [END, START) 工作时段则推迟到当天 START:00
    # 否则保持原样（落在 20:00-08:00 非工作时段即在此时段内过期）
    JWT_EXPIRY_BASE_HOURS: int = 48               # token 基础有效期（小时），默认 2 天
    FORCE_RELOGIN_WINDOW_START: int = 20          # 强制重登窗口起始（24h 制，20 = 20:00）
    FORCE_RELOGIN_WINDOW_END: int = 8             # 强制重登窗口结束（8 = 08:00）
     # 邮箱授权码等可逆存储的独立密钥；留空兼容历史数据（回退 JWT_SECRET_KEY）。
     # 生产迁移时可先填旧 JWT 密钥，完成专门的数据重加密后再轮换。
    SECRET_ENCRYPTION_KEY: str = ""
    SECRET_ENCRYPTION_KEY_FILE: str = "/secrets/data_encryption_key"
    LEGACY_SECRET_ENCRYPTION_KEY: str = ""
     # 生产环境显式开启后执行强校验；开发环境保持兼容，避免影响现有调试流程。
    MCP_LEGACY_ENV_READ: bool = False
    SKILL_EVOLUTION_ENABLED: bool = True
    SKILL_EVOLUTION_WORKER_ENABLED: bool = True
    SKILL_EVOLUTION_MAX_JOBS_PER_DAY: int = 20
    SKILL_HUB_ENABLED: bool = False
    SKILL_HUB_MAX_RESOURCES: int = 100
    SKILL_HUB_MAX_STORAGE_MB: int = 1024
    SKILL_HUB_TEST_CONCURRENCY: int = 2
    GOVERNANCE_RBAC_ENABLED: bool = False
    GOVERNANCE_RESOURCE_VERSIONING_ENABLED: bool = False
    SKILL_TEST_IMAGE: str = ""
    SKILL_TEST_HOST_KNOWLEDGE_DIR: str = ""
    GOVERNANCE_TOOL_POLICY_ENABLED: bool = False
    GOVERNANCE_MEMORY_CANDIDATES_ENABLED: bool = False
    GOVERNANCE_OPERATIONS_ENABLED: bool = False
    STRICT_PRODUCTION_CONFIG: bool = False

     # 自助注册新用户的默认角色（viewer/operator；出于安全考虑不允许注册为 admin）
    REGISTER_DEFAULT_ROLE: str = "viewer"
    
     # AI模型配置（OpenAI 兼容网关；本地开发可指向 DashScope 等公开服务）
     # 例：https://dashscope.aliyuncs.com/compatible-mode/v1
    AI_GATEWAY_BASE_URL: str = ""
    AI_GATEWAY_API_KEY: str = ""
    OCR_PROXY_URL: str = ""
    MODEL_NAME: str = "qwen-max"
     # 知识库文档改写模型（PDF 等转 Obsidian md 用；独立于聊天模型）
    KNOWLEDGE_REWRITE_MODEL: str = "qwen-plus"


     # 追踪配置
    TRACING_ENABLED: bool = True
     # trace 文件保留天数（按天轮转，超期自动清理）；单条 span 属性的最大字符数
     # （工具结果/工具定义可达数百 KB，截断后保留头尾，防止 trace 文件膨胀）
    TRACE_RETENTION_DAYS: int = 30
     # 单条 span 属性最大字符数。8000 会截断 gen_ai.tool.definitions（MCP schema
     # 快照），导致事后无法离线归因工具包袱构成；放宽到 20000（仍防超大块撑爆）
    TRACE_MAX_ATTR_CHARS: int = 20000

     # Token 预算配置（单次回复的最大加权 token 消耗，超出后 Agent 被要求立即收尾）
    TOKEN_BUDGET: float = 50000
    TOKEN_BUDGET_INPUT_WEIGHT: float = 1.0
    TOKEN_BUDGET_OUTPUT_WEIGHT: float = 2.0

    # 两个基座架构的单轮回复加权预算（口径同上：input×1 + output×2，按 reply 清零，
    # 不是会话级上限）。放进 settings 是为了线上可用同名环境变量覆盖，无需改代码重建镜像。
    # 工具助手（agent_type=dialog）
    TOKEN_BUDGET_DIALOG: float = 1000000
    # 数据分析师（agent_type=task）
    TOKEN_BUDGET_TASK: float = 2000000

     # ask_user 交互：等待用户选择方案的超时时间（秒），超时后由 Agent 自选最优方案继续
    ASK_USER_TIMEOUT_SECONDS: int = 600

    # ===== 质量闭环：先问再做（需求采集）/ 做完再看（产物验证）=====
    # 需求采集表单等待用户提交的超时（秒）。比方案选择更长：表单要填多项。
    # 超时后按模型为每题填写的 assume 值继续，不阻断任务。
    COLLECT_REQUIREMENTS_TIMEOUT_SECONDS: int = 900

    # 构建型任务（artifact 档）的单轮回复加权 token 预算。构建型要额外承担
    # 需求采集（约 0.7K）与验证+修复（约 2.2K）的固定开销，用默认 50K 会被
    # 70% 警告挤压；<=0 表示不分档，沿用 TOKEN_BUDGET。
    # 注意：生效值取 max(架构预算, 本值)。dialog/task 的架构预算已是 100 万/200 万，
    # 因此本配置对这两个架构不再起作用，只影响沿用 TOKEN_BUDGET 的架构。
    TOKEN_BUDGET_ARTIFACT: float = 150000

    # 产物验证（VerifyArtifact）。分层：Tier0 静态检查（纯 Python，必跑）
    # → Tier1 无头渲染（node + Playwright）→ Tier2 截图（默认关，见下）。
    ARTIFACT_VERIFY_ENABLED: bool = True
    # Tier1：关掉则只做静态检查（缺 node/Chromium 的环境可显式关闭以免每次探测）
    ARTIFACT_VERIFY_RENDER: bool = True
    # Tier2 截图：当前 AgentScope 版本的消息块没有图片类型，截图无法回传给模型，
    # 开了也只产生一个用户可见的文件、模型看不见，故默认关闭；框架支持图片块后再开。
    ARTIFACT_VERIFY_SCREENSHOT: bool = False
    # node 可执行文件名或绝对路径（Tier0 的脚本语法检查与 Tier1 都要用）
    ARTIFACT_VERIFY_NODE: str = "node"
    # 含 node_modules/playwright 的目录；留空则按 内置目录 → 后端根 → 仓库 test/ 顺序自动探测
    ARTIFACT_VERIFY_PLAYWRIGHT_DIR: str = ""
    # 单次渲染检查超时（秒），超时按"渲染层不可用"降级，不阻断交付
    ARTIFACT_VERIFY_TIMEOUT_SECONDS: int = 90
    # 交互探测最多点击多少个控件（点完看是否新增报错）
    ARTIFACT_VERIFY_MAX_CLICKS: int = 4
    # 回传给模型的结论文本上限（字符）。验证结论本身不能变成上下文负担。
    ARTIFACT_VERIFY_RESULT_MAX_CHARS: int = 2000

     # 事件循环保护（C6/A3/B4）：
     # 单轮回复硬时长上限（秒）：ReAct 循环超时即强制截止，防止单轮无限占用事件循环
    AGENT_TURN_TIMEOUT_SECONDS: int = 3600
     # 产出文件打包阈值：单轮生成文件数超过此值时打包为 zip，避免文件中心被大量文件淹没
    OUTPUT_ZIP_THRESHOLD: int = 3
     # headroom JSON 压缩单块超时（秒）：CPU 密集同步段超时即放弃该块压缩，不饿死循环
    HEADROOM_CRUSH_TIMEOUT_SECONDS: float = 20.0
     # Headroom 上下文压缩节流（缓存友好）：原地改写旧块会让该轮 prompt 前缀从改写点
     # 起整体失效（实测改写当轮缓存命中归零、纯追加则 99%），所以把压缩集中到少数轮次。
     # 每 N 轮推理最多原地压缩一次；<=1 = 每轮都压（旧行为）
    CONTEXT_COMPRESS_EVERY_N_ROUNDS: int = 5
     # 上下文估算 token 超过 context_size * 该比例时立即压缩，不等轮次；0 = 关闭压力门
    CONTEXT_COMPRESS_PRESSURE_RATIO: float = 0.35
     # 单次模型调用的输入侧硬上限（估算 token）。超过即先压缩上下文再推理。
     # 限制的是"每轮 prompt 大小"，不是轮数/预算；0 = 关闭。
    CONTEXT_INPUT_HARD_LIMIT_TOKENS: int = 45000

    # ===== Token 优化：MCP 工具 schema 包袱 / 历史驱逐 / 重复调用去重 =====
     # MCP 工具组按 server 拆分（每个 server 一个 reset_tools 组，模型按需激活）。
     # 关闭则退回旧的"全部 MCP 塞进一个 mcp 大组"行为（排障开关）。
    MCP_GROUP_PER_SERVER: bool = True
    MCP_BUILD_TIMEOUT_SECONDS: float = 20.0
    MCP_STDIO_ENV_PASSTHROUGH: str = ""
    MCP_STDIO_ALLOWED_COMMANDS: str = ""
    MCP_HTTP_ALLOWED_HOSTS: str = ""
    MCP_HTTP_REVIEWED_URLS: str = ""
    MCP_REQUIRE_CONFIRM_TOOLS: str = "send_email,reply_email"
     # MCP schema 精简：截断冗长的工具/参数描述（保留参数名/类型/必填项，只压散文）。
     # 冗长型 MCP server 的 description 普遍超长（实测某工单类 server 37 工具/53k 字符），
     # 精简后每轮可省上万 token。MCP_SCHEMA_SLIM_EXCLUDE_SERVERS 可按 server 名豁免。
    MCP_SCHEMA_SLIM_ENABLED: bool = True
    MCP_SCHEMA_SLIM_TOOL_DESC_CHARS: int = 300
    MCP_SCHEMA_SLIM_PARAM_DESC_CHARS: int = 120
    MCP_SCHEMA_SLIM_EXCLUDE_SERVERS: str = ""
     # 旧工具结果驱逐（microcompact）：最近 N 条消息之外的小工具结果替换为短占位符
     # （框架与 headroom 只处理大块结果，几百字符的小块永不清理、无限累积）。
     # 与 headroom 共用同一个节流门（只在工具组变化轮/压力轮/每 N 轮成批做），
     # 避免单独改写历史造成额外的缓存失效。
    TOOL_RESULT_EVICT_ENABLED: bool = True
    TOOL_RESULT_EVICT_KEEP_RECENT: int = 6
    TOOL_RESULT_EVICT_MIN_CHARS: int = 200
     # 驱逐豁免工具（结果必须原样保留：用户选择/需求采集/交付验证/写操作确认等）。
     # 逗号分隔，匹配精确名或 mcp__<server>__<tool> 的短名部分。
    TOOL_RESULT_EVICT_EXEMPT_TOOLS: str = (
        "AskUserChoice,CollectRequirements,VerifyArtifact,"
        "send_email,reply_email,create_schedule,delete_schedule"
    )
     # 同参重复只读调用去重：同一次回复内、白名单只读工具以相同参数再次调用时，
     # 直接重放首次结果并附提示，不再真实执行（实测一次重复调用白烧约 3 万 token）。
    TOOL_REPLAY_DEDUP_ENABLED: bool = True
    TOOL_REPLAY_DEDUP_TOOLS: str = (
        "mcp__email__lookup_user_email,mcp__email__check_emails,"
        "mcp__schedule__query_schedules,memory_search,SearchKnowledge"
    )
    TOOL_REPLAY_MAX_RESULT_CHARS: int = 4000
     # 工具助手（dialog）软轮次上限：超过后注入收尾提醒（不硬停，硬停仍归
     # max_react_iters / token 预算管）。0 = 关闭。
    DIALOG_SOFT_ROUND_LIMIT: int = 25
     # 工具助手（dialog）thinking 预算按交付档位降档：answer/document 档的思考量
     # 需求远低于构建档（output token 是最贵的部分，按 ×2 权重计费）。
     # <=0 = 不降档，沿用架构默认。
    DIALOG_THINKING_BUDGET_DEFAULT: int = 4096
    DIALOG_THINKING_BUDGET_SIMPLE: int = 1024

     # ReME 后台记忆提炼单任务超时（秒）：auto_memory 写回放后台执行，超时放弃本次提炼
    MEMORY_WRITEBACK_TIMEOUT_SECONDS: int = 120
     # ReME auto_memory 节流：本轮回复无工具调用且助手文本短于该值时跳过提炼
     # （纯寒暄/闲聊没有记忆价值）；0 = 不跳过，每条回复都提炼。
    MEMORY_WRITEBACK_MIN_REPLY_CHARS: int = 500
     # ReME _run_job / app.run_job 硬超时（秒）：所有 ReME 操作的底层入口，
     # 内部做 embedding/向量检索/BM25 等同步 IO，护卫版用 asyncio.to_thread
     # 丢进线程池 + wait_for 硬超时，防止阻塞事件循环（实测可达 300 秒）
    MEMORY_RUN_JOB_TIMEOUT: int = 60
     # 记忆生命周期 janitor（app/agents/memory.py，每日 03:00 由 main.py 定时器驱动）：
     # 启动后延迟补跑一次，覆盖"容器恰好在 03:00 前后重启"导致的整天漏跑；
     # 日提炼/周聚合/月聚合都是补偿式，补跑只会处理还没出卡的内容，天然幂等。
    MEMORY_JANITOR_STARTUP_CATCHUP: bool = True
    MEMORY_JANITOR_STARTUP_DELAY_SECONDS: int = 600
     # 月度聚合补偿窗口：最多回看多少个已结束月份（够覆盖长假/停机，又不至于
     # 首次上线时把陈年历史全部重算）
    MEMORY_MONTHLY_CATCHUP_MONTHS: int = 6
     # 独立线程健康探针端口（B4）：与业务事件循环解耦，Docker healthcheck 专用，
     # 业务循环被同步段短暂阻塞时探针仍能应答，避免误判 unhealthy
    HEALTH_PROBE_PORT: int = 8001

     # 知识库：向量库文件路径、文档存储目录、embedding 模型、单文件上传上限（MB）
    EMBEDDING_MODEL: str = "text-embedding-v3"
    KNOWLEDGE_DIR: str = str(BASE_DIR / "knowledge")
    VAULTS_DIR: str = str(BASE_DIR.parent / "vaults")
    VECTOR_DB_PATH: str = str(BASE_DIR / "vector.db")
    MAX_KNOWLEDGE_FILE_MB: int = 0  # 0 = 不限制单文件大小（个人库无配额）

     # 文件中心：每用户存储配额（MB），上传前校验；.env 可覆盖
    FILE_QUOTA_MB: int = 1024

     # Redis配置（部署时使用；留空则自动降级为无缓存，本地开发无需配置）
    REDIS_URL: str = ""

     # SMTP 邮件配置（忘记密码验证码发送）
     # SMTP_HOST 为空时不真实发信，验证码打印到后端日志（开发联调 mock 模式）
    SMTP_HOST: str = ""
    SMTP_PORT: int = 465
     # ssl / starttls / auto（auto：先明文连接，服务器支持 STARTTLS 时自动升级加密）
    SMTP_SECURITY: str = "auto"
    SMTP_USER: str = ""
    SMTP_PASS: str = ""
    SMTP_FROM: str = ""

     # IMAP 收信配置（Agent 邮件工具）：为空时复用 SMTP_HOST；凭据复用 SMTP_USER/SMTP_PASS
    IMAP_HOST: str = ""
    IMAP_PORT: int = 143

     # ===== 调度任务模块（定时作业） =====
     # 总开关：关闭后不启动调度引擎（API 仍可用，任务不会被触发）
    SCHEDULER_ENABLED: bool = True
     # 调度器全局时区（任务级时区另存于 scheduled_jobs.timezone）
    SCHEDULER_TIMEZONE: str = "Asia/Shanghai"
     # 误点宽限（秒）：服务短暂不可用后，宽限内的错过的触发会补跑一次
    SCHEDULER_MISFIRE_GRACE_SECONDS: int = 3600
     # 调度任务统一执行 Agent 的名称（创建任务时按名解析，不硬编码 id）
    SCHEDULER_TASK_AGENT_NAME: str = "任务助手"
     # 前端访问基址：结果通知邮件里拼接会话链接用；部署时按实际入口配置
    FRONTEND_BASE_URL: str = "http://localhost:5173"

     # 被 BaseSettings 内部自动读取，用来控制如何从外部加载配置
     # Workspace / Offloader：本地用 LocalWorkspace，服务器用 DockerWorkspace（WORKSPACE_BACKEND=docker）
    WORKSPACE_BACKEND: str = "local"  # "local" | "docker"
    WORKSPACE_DOCKER_IMAGE: str = "python:3.11-slim"
    WORKSPACE_DOCKER_EXTRA_PIP: str = ""  # 逗号分隔的额外 pip 包，注入会话容器
     # 允许透传进沙箱容器的后端环境变量名（逗号分隔），只透传名字在此列出且
     # 后端进程里确实存在的变量。默认空 = 沙箱拿不到任何后端凭据（保持现状）。
     # 技能需要额外服务时才透传最小权限凭据，不得透传网关主密钥。
     # 注意：沙箱内跑的是 Agent 生成的任意代码，透传即等于把该凭据暴露给会话，
     # 请只透传"技能确实需要、且泄露影响可控"的变量。
    WORKSPACE_DOCKER_ENV_PASSTHROUGH: str = ""
     # 后端容器内 outputs 目录对应的宿主机真实路径。DockerWorkspace 的兄弟沙箱
     # 容器由宿主 Docker daemon 创建，bind mount 源必须是宿主机路径；后端本身
     # 跑在容器里时（compose 部署）需显式给出映射。留空 = 直接用 OUTPUT_DIR
     # （后端直接跑在宿主机上时即正确值）。
    WORKSPACE_HOST_OUTPUT_DIR: str = ""
     # 沙箱容器兜底最长存活时间（小时）：回收器定期强制回收超龄且无运行中任务
     # 持有的 as_ws_* 容器，防止清理异常导致容器泄漏
    WORKSPACE_SANDBOX_MAX_AGE_HOURS: int = 2

    # ===== 服务预览（Agent 在沙箱里起的 Web 服务，发布成宿主端口供用户直接访问） =====
    # 总开关。关闭时整条链路（探测/发布/清理/前端卡片）全部短路，
    # 行为与改造前完全一致，因此可以按环境灰度。
    PREVIEW_ENABLED: bool = False
    # 对外展示用的基址（协议+主机，不含端口）。留空则由后端按请求 Host 推断，
    # 部署在反向代理后面时应显式配置，例如 http://your-server-host
    PREVIEW_PUBLIC_BASE_URL: str = ""
    # 宿主端口分配区间：预览代理容器在这个区间里取一个未被占用的端口发布。
    # 需要在宿主防火墙放行；区间耗尽时发布失败并给出明确提示，不会退化到随机端口。
    PREVIEW_HOST_PORT_RANGE: str = "31000-31099"
    # 额外保留、不参与分配的宿主端口（逗号分隔）：给非 Docker 的宿主服务让路。
    PREVIEW_RESERVED_HOST_PORTS: str = ""
    # 预览代理容器镜像。留空 = 复用沙箱镜像（WORKSPACE_DOCKER_IMAGE），
    # 代理脚本用 python3 -c 内联注入，不需要额外构建/分发镜像。
    PREVIEW_PROXY_IMAGE: str = ""
    # 单条预览最长存活时间（分钟）。到期由清理循环摘掉代理容器并标记 expired。
    # 不要超过 WORKSPACE_SANDBOX_MAX_AGE_HOURS：沙箱先被回收，预览自然就没了。
    PREVIEW_MAX_TTL_MINUTES: int = 120
    # 失效后的宽限期（分钟）：沙箱回收/TTL 到期后先保留代理容器，让访问者看到
    # "预览已结束"的说明页而不是浏览器报错；宽限期过了才摘容器、释放宿主端口。
    PREVIEW_EXPIRED_GRACE_MINUTES: int = 30
    # 全局并发预览上限，超出时拒绝新建并提示用户先停掉旧的。
    PREVIEW_MAX_CONCURRENT: int = 20
    # 每用户并发预览上限。
    PREVIEW_MAX_CONCURRENT_PER_USER: int = 3
    # 单轮对话最多发布几个预览：探测到多个监听端口时按"通配绑定优先、端口小者优先"
    # 取前 N 个，避免一次把端口区间占满。
    PREVIEW_MAX_PER_TURN: int = 3
    # 预览源码包（zip）体积上限 MB，超出则只打包到上限并写 PACKAGING_ERRORS.txt。
    PREVIEW_BUNDLE_MAX_MB: int = 50
    # 打包时跳过的目录名（依赖/缓存/版本库），逗号分隔。
    PREVIEW_BUNDLE_EXCLUDE_DIRS: str = (
        ".git,.venv,venv,env,node_modules,__pycache__,.pytest_cache,.mypy_cache,"
        ".next,dist,build,.cache,.idea,.vscode"
    )
    # 沙箱容器附加的 Docker 网络名（逗号分隔）。留空 = 沿用框架默认（bridge），
    # 保持既有行为；需要让沙箱与后端互通时才配置。
    WORKSPACE_DOCKER_NETWORK: str = ""
    model_config = SettingsConfigDict(
         # 默认读取项目根目录 .env；容器化部署由 ENV_FILE 指向挂载进容器的凭据文件（见 docker-compose.yml backend 节，凭据放宿主机不入仓库）
        env_file=os.environ.get("ENV_FILE", ".env"),
        case_sensitive=True,     # 从环境变量/.env 文件中匹配字段名时，是否区分大小写
         # .env 里允许出现本类未声明的变量（如第三方技能/MCP 自带的凭据），
         # 一律忽略，避免任何新增环境变量把配置加载直接打崩
        extra="ignore"
    )

# get_settings() 第一次调用时，会读取 .env、做类型校验、创建 Settings 对象
# @lru_cache() 缓存了这个返回值，后续再调用直接返回缓存，不会重复读取文件
@lru_cache()
def get_settings() -> Settings:
    loaded = Settings()
    if not loaded.LEGACY_SECRET_ENCRYPTION_KEY:
        loaded.LEGACY_SECRET_ENCRYPTION_KEY = loaded.SECRET_ENCRYPTION_KEY or loaded.JWT_SECRET_KEY
    for attribute, filename in (
        ("JWT_SECRET_KEY", loaded.JWT_SECRET_KEY_FILE),
        ("SECRET_ENCRYPTION_KEY", loaded.SECRET_ENCRYPTION_KEY_FILE),
    ):
        if filename and os.path.isfile(filename):
            file_stat = os.stat(filename)
            if file_stat.st_mode & 0o077:
                raise RuntimeError(f"Unsafe permissions for {attribute} file")
            with open(filename, encoding="utf-8") as secret_file:
                value = secret_file.read().strip()
            if not value:
                raise RuntimeError(f"Empty {attribute} file")
            setattr(loaded, attribute, value)
    return loaded

# 在模块加载时就调用一次，之后其他文件可以直接用settings中的参数
settings = get_settings()


def validate_runtime_settings() -> None:
    """在严格生产模式下阻止弱密钥、通配 CORS 等危险配置启动。"""
    if settings.APP_ENV.lower() != "production" or not settings.STRICT_PRODUCTION_CONFIG:
        return
    errors = []
    if len(settings.JWT_SECRET_KEY or "") < 32:
        errors.append("JWT_SECRET_KEY must be at least 32 characters")
    if len(settings.SECRET_ENCRYPTION_KEY or "") < 32:
        errors.append("SECRET_ENCRYPTION_KEY must be at least 32 characters")
    origins = [o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()]
    if not origins or "*" in origins:
        errors.append("CORS_ORIGINS must be an explicit production allow-list")
    if not settings.AI_GATEWAY_API_KEY:
        errors.append("AI_GATEWAY_API_KEY must be configured")
    if errors:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(errors))
