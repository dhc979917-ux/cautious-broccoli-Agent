"""统一日志配置。

所有 app 模块通过 logging.getLogger(__name__) 获取 logger（层级挂在 "app" 下），
启动时调用 setup_logging() 一次性安装 handler。幂等，重复调用不叠加 handler。
"""
import logging
import sys

_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_VALID = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_configured = False


def setup_logging(level: str = "", app_env: str = "production") -> None:
    """初始化 "app" 顶层 logger。

    level 显式给定（LOG_LEVEL 配置）时优先；否则按运行环境：
    development → DEBUG，production → INFO。
    """
    global _configured
    if _configured:
        return
    lvl = (level or "").upper()
    if lvl not in _VALID:
        lvl = "DEBUG" if app_env == "development" else "INFO"
    root = logging.getLogger("app")
    root.setLevel(lvl)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_FMT, _DATEFMT))
    root.addHandler(handler)
    root.propagate = False  # 避免 uvicorn root logger 重复输出
    _configured = True
