from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from sqlalchemy.sql import func
from datetime import datetime
from app.database import Base


'''服务预览记录表

Agent 在会话沙箱（as_ws_* 容器）里起了一个 Web 服务时，平台会：
  1) 探测沙箱内监听端口 -> 2) 起一个宿主端口发布的代理容器 -> 3) 把源码打成 zip
     连同 HOW_TO_RUN.md 登记进文件中心 -> 4) 把可访问 URL 回给前端。
预览是"临时版"：沙箱被回收（LRU 淘汰 / 超龄清扫 / 进程重启）后 URL 立即失效，
用户要长期使用必须下载 zip 自行部署。本表就是这条链路的状态机与清理依据。

status 取值：
  active  代理容器在跑，URL 可访问
  expired 沙箱或代理已消失，URL 返回"预览已结束"页（保留记录供前端展示历史）
  error   发布过程出错（端口耗尽/镜像缺失等），detail 里有原因
'''


class ServicePreview(Base):
    __tablename__ = "service_previews"

    id = Column(Integer, primary_key=True, index=True)
    # 归属：权限隔离与清理都按这两个字段走
    user_id = Column(Integer, nullable=False, index=True)
    session_id = Column(Integer, nullable=False, index=True)
    agent_id = Column(Integer, nullable=True)

    # 沙箱容器名（as_ws_{workspace_id}）与工作区 id：清理时反查用
    container_name = Column(String(128), nullable=False, index=True)
    workspace_id = Column(String(64), nullable=True, index=True)

    # 服务在沙箱内监听的地址与端口；bind_loopback=True 表示只绑了 127.0.0.1，
    # 代理容器无法访问，此时不发布，只在 detail 里给出整改提示
    target_host = Column(String(64), nullable=True)
    target_port = Column(Integer, nullable=False)
    bind_loopback = Column(Boolean, default=False, nullable=False)

    # 代理容器：宿主发布端口 + 容器名
    proxy_container = Column(String(128), nullable=True)
    host_port = Column(Integer, nullable=True, index=True)

    # 服务信息快照（探测结果），前端卡片与 HOW_TO_RUN.md 都从这里取
    project_dir = Column(String(512), nullable=True)   # 沙箱内工作目录，如 /workspace/app
    entry_command = Column(Text, nullable=True)        # 启动命令，如 python3 app.py
    process_name = Column(String(128), nullable=True)  # 进程名，如 python3 / node

    # 源码包：登记进文件中心的那条记录的相对路径（sessions/{id}/_previews/xxx.zip）
    bundle_path = Column(String(512), nullable=True)
    bundle_size = Column(Integer, default=0)

    status = Column(String(16), nullable=False, default="active", index=True)
    # 失效/失败原因，直接展示给用户（不暴露内部路径与容器名）
    detail = Column(Text, nullable=True)

    # 统一北京时间：与 files 表口径一致（SQLite 的 func.now() 是 UTC，会差 8 小时）
    created_at = Column(DateTime(timezone=True), default=datetime.now, server_default=func.now())
    last_seen_at = Column(DateTime(timezone=True), default=datetime.now)
    # 到期时间：清理循环据此摘代理容器并置 expired
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
