from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from app.config import settings


'''数据库连接'''


# 创建数据库引擎（维护着一个连接池，所有数据库操作都通过它进行）
engine = create_engine(
    settings.DATABASE_URL,
    #  SQLite 专属参数。SQLite 默认不允许多线程同时访问同一个连接，但 FastAPI 是异步框架，可能多个线程同时操作数据库。
    # 设置 check_same_thread=False 关闭这个限制。只在 SQLite 时加这个参数，换成 PostgreSQL 后就不需要了
    connect_args={"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}
)

# 创建会话工厂
# autocommit=False：不自动提交。你手动调用 db.commit() 才真正写入数据库。这样可以把多个操作打包成一个事务，要么全成功，要么全回滚
# autoflush=False：不自动刷写到数据库。调用 db.add(user) 后，数据只在内存中，不会立刻写入数据库文件，直到你 commit()
# bind=engine：这个会话工厂绑定到上面创建的引擎
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# 基础模型类
Base = declarative_base()

# 获取数据库会话的依赖注入函数
# 为每一个 HTTP 请求提供一个独立的数据库会话，并在请求结束后自动关闭
def get_db():
    db = SessionLocal()          # 创建数据库会话
    '''
    当 FastAPI 调用这个函数时，它会在 yield 处暂停，把 db 对象注入到对应的路由函数里。路由函数执行期间，这个会话一直存活
    无论请求处理成功还是抛出异常，finally 块都会执行，确保数据库连接被归还给连接池，不会泄漏
    '''
    try:
        yield db                 # 把会话交给请求处理函数
    finally:
        db.close()               # 请求结束后，关闭会话

# 初始化数据库（创建所有表）
def init_db():
    # 先 import 所有模型文件（只有 import 了，Base 才知道有哪些表）
    from app.models import (user, agent, session, message, skill, skill_package, skill_upload_job,
                            user_agent_access, user_skill_access, password_reset, email_record,
                            session_context, service_preview, audit_event, organization,
                            model_governance, model_access_policy, memory_governance, operations)
    # 让 SQLAlchemy 根据所有继承 Base 的子类，自动在数据库中创建对应的表
    Base.metadata.create_all(bind=engine)
    # 轻量迁移：create_all 不会给已存在的表补列，这里手动补齐后续新增的列
    _migrate_add_columns()
    from migrate_model_gateway_v2 import migrate
    migrate(apply=True)
    from migrate_mcp_governance_v2 import migrate as migrate_mcp
    migrate_mcp(apply=True)
    from migrate_mcp_display_v3 import migrate as migrate_mcp_display
    migrate_mcp_display(apply=True)
    from migrate_skill_hub_v1 import migrate as migrate_skill_hub
    migrate_skill_hub(apply=True)
    if settings.SKILL_HUB_ENABLED:
        from app.services.skill_hub.recovery import recover_orphans
        with SessionLocal() as db:
            recover_orphans(db)


def _migrate_add_columns():
    """给已存在的表补新增列（SQLite 用 ALTER TABLE ADD COLUMN；其他库同理）。
    逐个检测、幂等：列已存在则跳过。低耦合：只在这里集中处理，不散落各处。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    # (表名, 列名, 列定义 SQL)
    pending = [
        ("users", "permissions", "TEXT"),
        # 令牌版本号：管理员改权限后自增，旧 JWT 立刻失效（core/deps.py 校验）
        ("users", "token_version", "INTEGER DEFAULT 0 NOT NULL"),
        ("users", "account", "VARCHAR(50)"),
        ("users", "phone", "VARCHAR(20)"),
        ("users", "avatar", "VARCHAR(255)"),
        ("users", "agent_avatar", "VARCHAR(255)"),
        ("users", "mail_password", "VARCHAR(255)"),
        ("users", "must_change_password", "BOOLEAN DEFAULT false NOT NULL"),
        ("users", "password_initialized_at", "TIMESTAMP"),
        ("users", "password_changed_at", "TIMESTAMP"),
        ("users", "last_login", "TIMESTAMP"),
        ("files", "deleted_at", "TIMESTAMP"),
        # 产出文件的来源会话标题快照：会话被删除后文件中心仍能展示"来源"（置灰不可点）
        ("files", "session_title", "VARCHAR(200)"),
        ("knowledge_bases", "embedding_model", "VARCHAR(100)"),
        ("knowledge_bases", "scope", "VARCHAR(20) DEFAULT 'shared' NOT NULL"),
        ("knowledge_bases", "kind", "VARCHAR(20) DEFAULT 'normal' NOT NULL"),
        ("knowledge_bases", "vault_dir", "TEXT"),
        ("skills", "package_id", "INTEGER"),
        ("skills", "display_name_zh", "VARCHAR(200)"),
        ("skills", "description_zh", "VARCHAR(500)"),
        # 技能磁盘身份与目录缺失标记（v7）：同步/恢复/彻底删除都按 rel_path 定位目录
        ("skills", "rel_path", "VARCHAR(500)"),
        ("skills", "dir_missing", "BOOLEAN DEFAULT false NOT NULL"),
        # 归属锁定：管理员手动"内置/取消内置"过的技能，同步时不再按目录位置覆盖
        # builtin_dialog / builtin_task（见 services/skill_service.py 的 sync 与 change_scope）
        ("skills", "scope_locked", "BOOLEAN DEFAULT false NOT NULL"),
        # 技能广场可见性（管理员态）：hidden=True 对未授权非管理员隐藏
        ("skills", "is_hidden", "BOOLEAN DEFAULT false NOT NULL"),
        ("skill_packages", "required_role", "VARCHAR(20)"),
        ("skill_packages", "is_hidden", "BOOLEAN DEFAULT false NOT NULL"),
        ("skill_packages", "rel_path", "VARCHAR(500)"),
        ("skill_packages", "dir_missing", "BOOLEAN DEFAULT false NOT NULL"),
        ("email_records", "reply_to", "VARCHAR(255)"),
        ("knowledge_documents", "title", "VARCHAR(255)"),
        ("knowledge_documents", "vault_path", "TEXT"),
        ("knowledge_documents", "frontmatter_json", "TEXT"),
        ("knowledge_documents", "tags_json", "TEXT"),
        ("knowledge_documents", "category", "VARCHAR(100)"),
        ("knowledge_documents", "content_hash", "VARCHAR(64)"),
        ("knowledge_documents", "indexed_hash", "VARCHAR(64)"),
        ("knowledge_documents", "indexed_at", "TIMESTAMP"),
        ("knowledge_documents", "source_doc_id", "INTEGER"),
        ("knowledge_documents", "is_markdown", "BOOLEAN DEFAULT false"),
        ("knowledge_documents", "upload_batch_id", "VARCHAR(32)"),
    ]
    with engine.connect() as conn:
        for table, col, coltype in pending:
            if not insp.has_table(table):
                continue
            existing = {c["name"] for c in insp.get_columns(table)}
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                conn.commit()
        # users 表专属迁移：account 唯一索引 + 存量数据回填（幂等）
        if insp.has_table("users"):
            conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_account ON users(account)"))
            conn.execute(text("UPDATE users SET account = username WHERE account IS NULL OR account = ''"))
            conn.execute(text("UPDATE users SET phone = '18888888888' WHERE phone IS NULL OR phone = ''"))
            conn.commit()
        if insp.has_table("knowledge_bases"):
            conn.execute(text(
                "UPDATE knowledge_bases SET kind = 'memory' "
                "WHERE scope = 'personal' AND name LIKE '我的记忆-%' "
                "AND (kind IS NULL OR kind = 'normal')"
            ))
            conn.commit()
        # files 表专属迁移：从仍存在的会话回填标题快照（幂等，SQLite/PostgreSQL 同语法）
        if insp.has_table("files") and insp.has_table("sessions"):
            conn.execute(text(
                "UPDATE files SET session_title = "
                "(SELECT s.title FROM sessions s WHERE s.id = files.session_id) "
                "WHERE session_title IS NULL AND session_id IS NOT NULL"
            ))
            conn.commit()
