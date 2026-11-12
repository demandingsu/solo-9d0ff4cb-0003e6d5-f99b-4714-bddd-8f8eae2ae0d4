"""禁入名单核验服务。

核验流程
--------
1. 从目录 URL 开始逐页抓取分页，解析每页的批次标识、决定入口与下一页链接；
2. 对每条决定抓取并解析详情页，提取决定编号、主体代码、禁入起止日、
   发布批次与来源链接；
3. 全程校验「列表页与详情页同批」：任一页批次缺失、与首批不一致，或
   分页循环、抓取/解析失败，本轮即判定为待核实（pending），**保留旧快照、
   不得用旧快照证明本轮未禁入**；
4. 仅当整批完整且同批时，原子替换该目录的最近一次完整批次快照，再基于
   本轮新数据给出禁入/未禁入结论；
5. 同一主体多条决定时，只要一条的 [开始日, 截止日] 覆盖核验日（起止日
   均含当日）即返回禁入并给出依据；完整名单均不覆盖才返回未禁入。
"""
from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import date, datetime
from typing import Optional

from . import blacklist_parser as bp
from . import config, fetcher, snapshot
from .fetcher import FetchError

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# 核验结论
BLOCKED = "blocked"          # 禁入：本轮完整批次中存在覆盖核验日的决定
NOT_BLOCKED = "not_blocked"  # 未禁入：本轮完整批次全部决定均不覆盖核验日
PENDING = "pending"          # 待核实：本轮未能取得完整同批名单


class PendingVerification(Exception):
    """本轮无法取得完整同批名单，结论只能是待核实。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _parse_verify_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"核验日期格式应为 YYYY-MM-DD：{raw!r}") from exc


def crawl_full_batch(index_url: str) -> tuple[str, int, list[bp.BlacklistDecision]]:
    """抓取目录全部分页及决定详情，返回（批次, 页数, 决定列表）。

    任何分页循环、抓取或解析失败、批次不一致都抛出 PendingVerification；
    本函数不写快照，由调用方决定如何处理旧快照。
    """
    visited_pages: set[str] = set()
    entries: list[bp.BlacklistEntry] = []
    batch: Optional[str] = None
    page_count = 0
    page_url: Optional[str] = index_url

    while page_url is not None:
        if page_url in visited_pages:
            raise PendingVerification(f"分页循环：{page_url} 被重复访问")
        if page_count >= config.BLACKLIST_MAX_PAGES:
            raise PendingVerification(
                f"分页数量超过上限 {config.BLACKLIST_MAX_PAGES}，疑似分页异常"
            )
        visited_pages.add(page_url)
        page_count += 1

        try:
            html = fetcher.fetch(page_url)
            index_page = bp.parse_index(html, page_url)
        except (FetchError, bp.BlacklistParseError) as exc:
            raise PendingVerification(f"目录页抓取/解析失败（{page_url}）：{exc}")

        if not index_page.batch:
            raise PendingVerification(f"目录页缺少发布批次标识（{page_url}）")
        if batch is None:
            batch = index_page.batch
        elif index_page.batch != batch:
            # 分页途中批次切换：本轮数据不可用
            raise PendingVerification(
                f"批次变化：首批为 {batch}，{page_url} 为 {index_page.batch}"
            )

        if not index_page.entries:
            raise PendingVerification(f"目录页未解析到任何决定入口（{page_url}）")
        entries.extend(index_page.entries)
        page_url = index_page.next_url

    # 目录入口去重（决定编号唯一），同时发现跨页重复编号
    deduped: dict[str, bp.BlacklistEntry] = {}
    for entry in entries:
        if entry.decision_no in deduped:
            continue
        deduped[entry.decision_no] = entry

    decisions: list[bp.BlacklistDecision] = []
    for entry in deduped.values():
        try:
            detail_html = fetcher.fetch(entry.detail_url)
            decision = bp.parse_decision(detail_html, entry.detail_url)
        except (FetchError, bp.BlacklistParseError) as exc:
            raise PendingVerification(
                f"决定详情抓取/解析失败（{entry.decision_no}）：{exc}"
            )
        # 列表页与详情须同批
        if decision.batch != batch:
            raise PendingVerification(
                f"批次变化：目录批次为 {batch}，决定 {entry.decision_no} "
                f"详情批次为 {decision.batch}"
            )
        # 详情主体代码须与目录入口一致，防止张冠李戴
        if decision.subject_code != entry.subject_code:
            raise PendingVerification(
                f"决定 {entry.decision_no} 详情主体代码 {decision.subject_code} "
                f"与目录入口 {entry.subject_code} 不一致"
            )
        decisions.append(decision)

    return batch, page_count, decisions


def _covers(decision: bp.BlacklistDecision, target: date) -> bool:
    """禁入区间 [start_date, end_date] 是否覆盖核验日，起止日含当日。"""
    start = date.fromisoformat(decision.start_date)
    end = date.fromisoformat(decision.end_date)
    return start <= target <= end


def verify(
    index_url: str, subject_code: str, verify_date: str
) -> dict:
    """执行一次禁入核验并返回响应字典（详见 models.BlacklistVerifyResponse）。"""
    target = _parse_verify_date(verify_date)
    subject_code = subject_code.strip().upper()
    old_snapshot = snapshot.load_snapshot(index_url)

    try:
        batch, page_count, decisions = crawl_full_batch(index_url)
    except PendingVerification as exc:
        logger.warning("禁入名单核验待核实：%s", exc.reason)
        return _pending_response(index_url, subject_code, verify_date, exc)

    # 完整同批数据到手后才替换快照；失败路径不会执行到这里，旧快照原样保留
    snapshot.save_snapshot(index_url, batch, page_count, decisions)

    matched = [d for d in decisions if d.subject_code == subject_code]
    covering = [d for d in matched if _covers(d, target)]

    if covering:
        basis = sorted(
            (asdict(d) for d in covering),
            key=lambda item: (item["start_date"], item["decision_no"]),
        )
        return {
            "index_url": index_url,
            "subject_code": subject_code,
            "verify_date": verify_date,
            "status": BLOCKED,
            "batch": batch,
            "basis": basis,
            "snapshot_batch": batch,
            "old_snapshot_batch": old_snapshot.get("batch") if old_snapshot else None,
            "reason": None,
            "checked_at": _now(),
        }

    return {
        "index_url": index_url,
        "subject_code": subject_code,
        "verify_date": verify_date,
        "status": NOT_BLOCKED,
        "batch": batch,
        "basis": [],
        "snapshot_batch": batch,
        "old_snapshot_batch": old_snapshot.get("batch") if old_snapshot else None,
        "reason": None,
        "checked_at": snapshot._now(),
    }


def _pending_response(
    index_url: str,
    subject_code: str,
    verify_date: str,
    exc: PendingVerification,
) -> dict:
    old_snapshot = snapshot.load_snapshot(index_url)
    return {
        "index_url": index_url,
        "subject_code": subject_code,
        "verify_date": verify_date,
        "status": PENDING,
        "batch": None,
        # 待核实不输出任何禁入依据，旧快照只做批次信息提示，不参与结论
        "basis": [],
        "snapshot_batch": old_snapshot.get("batch") if old_snapshot else None,
        "old_snapshot_batch": old_snapshot.get("batch") if old_snapshot else None,
        "reason": exc.reason,
        "checked_at": snapshot._now(),
    }
