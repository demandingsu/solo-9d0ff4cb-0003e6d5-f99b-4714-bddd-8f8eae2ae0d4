"""运行配置：全部通过环境变量注入，便于容器化部署。"""
import os
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# SQLite 数据库文件路径
DB_PATH = Path(os.getenv("DB_PATH", "/data/procurement.db"))

# HTTP 抓取超时（秒）与失败重试次数（仅对网络错误与 5xx 重试）
FETCH_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", "10"))
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "2"))

# 重试间隔基数（秒），实际等待 = base * 第几次重试
RETRY_BACKOFF = float(os.getenv("RETRY_BACKOFF", "0.5"))

# 详情页抓取失败时是否保留上次完整结果（需求要求保留，默认开启）
KEEP_LAST_ON_DETAIL_FAILURE = _env_bool("KEEP_LAST_ON_DETAIL_FAILURE", True)

# 禁入名单完整批次快照根目录（按目录 URL 分子目录保存最近一次完整批次）
SNAPSHOT_DIR = Path(os.getenv("SNAPSHOT_DIR", "/data/snapshots"))

# 禁入名单目录分页页数上限，防止异常分页导致死循环
BLACKLIST_MAX_PAGES = int(os.getenv("BLACKLIST_MAX_PAGES", "200"))
