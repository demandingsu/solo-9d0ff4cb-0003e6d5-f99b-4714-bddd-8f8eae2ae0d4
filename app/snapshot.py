"""禁入名单完整批次快照：按目录 URL 分目录保存最近一次完整批次。

目录布局::

    SNAPSHOT_DIR/
      <index_url 的 sha256 前 16 位>/
        meta.json        # 目录 URL、批次、页数、决定数、抓取时间
        snapshot.json    # 最近一次完整批次的全部决定

写入采用「临时文件 + os.replace 原子替换」：本轮抓取/解析失败或批次变化
导致流程中断时，旧快照文件保持原样，不会被部分结果覆盖。任何时候都不得
用旧快照证明本轮“未禁入”，该约束在 blacklist_service 中强制执行。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config
from .blacklist_parser import BlacklistDecision

SNAPSHOT_FILE = "snapshot.json"
META_FILE = "meta.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def snapshot_dir_for(index_url: str) -> Path:
    key = hashlib.sha256(index_url.encode("utf-8")).hexdigest()[:16]
    return config.SNAPSHOT_DIR / key


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # delete=False + 手动 replace，保证同目录下的原子 rename
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        # 出错时清理临时文件，旧快照不受影响
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_snapshot(
    index_url: str,
    batch: str,
    page_count: int,
    decisions: list[BlacklistDecision],
) -> Path:
    """保存一次完整批次快照，原子替换旧文件并返回快照目录。"""
    target_dir = snapshot_dir_for(index_url)
    snapshot_payload = {
        "index_url": index_url,
        "batch": batch,
        "page_count": page_count,
        "decision_count": len(decisions),
        "captured_at": _now(),
        "decisions": [asdict(d) for d in decisions],
    }
    meta_payload = {
        "index_url": index_url,
        "batch": batch,
        "page_count": page_count,
        "decision_count": len(decisions),
        "captured_at": snapshot_payload["captured_at"],
        "snapshot_file": SNAPSHOT_FILE,
    }
    # 先写数据文件再写 meta；meta 只作可读摘要，判定一律以 snapshot.json 为准
    _atomic_write(target_dir / SNAPSHOT_FILE, snapshot_payload)
    _atomic_write(target_dir / META_FILE, meta_payload)
    return target_dir


def load_snapshot(index_url: str) -> Optional[dict]:
    """读取最近一次完整批次快照；不存在或损坏时返回 None。"""
    path = snapshot_dir_for(index_url) / SNAPSHOT_FILE
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(payload, dict) or not isinstance(
        payload.get("decisions"), list
    ):
        return None
    return payload
