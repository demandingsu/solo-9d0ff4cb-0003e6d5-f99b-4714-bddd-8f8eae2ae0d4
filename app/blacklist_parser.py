"""禁入名单目录页与决定详情页解析（BeautifulSoup）。

页面约定（与 sample_site 的禁入名单示例对应，并对字段标签做别名兼容）：

目录页（可分页）
  - <table class="blacklist-list"> 内每行一条决定入口，链接指向详情页；
  - 行内可带决定编号、主体代码等列；
  - 批次标识放在带 data-batch 属性的元素或「发布批次」字段中；
  - 下一页链接：<a rel="next"> 或文本为「下一页」的链接。

详情页
  - table.detail-table / dl.detail-list 字段表，标签别名见 FIELD_ALIASES；
  - 必须包含决定编号、主体代码、禁入开始日、禁入截止日、发布批次。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

# 详情页字段标签别名（去掉冒号/空白后匹配）
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "decision_no": ("决定编号", "处罚决定编号", "文号"),
    "subject_code": ("统一社会信用代码", "主体代码", "信用代码"),
    "start_date": ("禁入开始日", "禁入起始日", "开始日期"),
    "end_date": ("禁入截止日", "禁入结束日", "截止日期"),
    "batch": ("发布批次", "批次", "批次编号"),
    "subject_name": ("主体名称", "单位名称", "企业名称"),
}

EMPTY_MARKERS = {"—", "–", "-", "/", "无", "空", "暂无"}

_DATE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")


class BlacklistParseError(ValueError):
    """禁入名单页面结构缺失或字段无法解析。"""


@dataclass
class BlacklistEntry:
    """目录页上的一条禁入决定入口。"""
    decision_no: str
    subject_code: str
    detail_url: str


@dataclass
class BlacklistIndexPage:
    """单张目录页的解析结果。"""
    batch: Optional[str]
    entries: list[BlacklistEntry] = field(default_factory=list)
    next_url: Optional[str] = None


@dataclass
class BlacklistDecision:
    """禁入决定详情解析结果。"""
    decision_no: str
    subject_code: str
    start_date: str  # 统一为 YYYY-MM-DD
    end_date: str    # 统一为 YYYY-MM-DD
    batch: str
    source_url: str
    subject_name: Optional[str] = None


def _clean(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    value = text.replace("\xa0", " ").strip()
    if not value or value in EMPTY_MARKERS:
        return None
    return value


def normalize_date(raw: Optional[str]) -> Optional[str]:
    """把常见日期写法统一为 YYYY-MM-DD；无法识别返回 None。"""
    value = _clean(raw)
    if not value:
        return None
    match = _DATE_RE.search(value)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _extract_batch(soup: BeautifulSoup) -> Optional[str]:
    # 优先读取显式 data-batch 标记
    marked = soup.select_one("[data-batch]")
    if marked is not None:
        value = _clean(marked.get("data-batch"))
        if value:
            return value
    # 兼容字段表中的「发布批次」
    fields = _field_map(soup)
    for alias in FIELD_ALIASES["batch"]:
        if alias in fields and _clean(fields[alias]):
            return _clean(fields[alias])
    return None


def parse_index(html: str, page_url: str) -> BlacklistIndexPage:
    """解析一张目录页：批次、决定入口列表、下一页绝对 URL。"""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.blacklist-list")
    if table is None:
        raise BlacklistParseError("目录页缺少 table.blacklist-list 名单表格")

    batch = _extract_batch(soup)
    entries: list[BlacklistEntry] = []
    seen: set[str] = set()
    for row in table.select("tr"):
        link = row.select_one("a[href]")
        if link is None:
            continue
        href = link.get("href")
        decision_no = _clean(link.get_text())
        if not href or not decision_no:
            continue
        cells = [_clean(td.get_text()) for td in row.find_all(["td", "th"])]
        cells = [c for c in cells if c]
        # 主体代码：优先取行内形似统一社会信用代码（18 位大写字母数字）的
        # 单元格；否则按目录约定取第三个单元格（决定编号、主体名称、主体代码）。
        subject_code = None
        for cell in cells:
            if re.fullmatch(r"[0-9A-Z]{18}", cell):
                subject_code = cell
                break
        if subject_code is None and len(cells) >= 3:
            subject_code = cells[2]
        if not subject_code:
            raise BlacklistParseError(
                f"目录行缺少主体代码：{decision_no}"
            )
        if decision_no in seen:
            continue
        seen.add(decision_no)
        entries.append(
            BlacklistEntry(
                decision_no=decision_no,
                subject_code=subject_code,
                detail_url=urljoin(page_url, href),
            )
        )

    next_url = None
    next_link = soup.select_one("a[rel~='next']")
    if next_link is None:
        for link in soup.find_all("a", href=True):
            if _clean(link.get_text()) == "下一页":
                next_link = link
                break
    if next_link is not None and _clean(next_link.get("href")):
        next_url = urljoin(page_url, next_link["href"])

    return BlacklistIndexPage(batch=batch, entries=entries, next_url=next_url)


def _field_map(soup: BeautifulSoup) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tr in soup.select("table.detail-table tr"):
        cells = tr.find_all(["th", "td"])
        if len(cells) >= 2:
            label = _clean(cells[0].get_text()) or ""
            value = _clean(cells[1].get_text())
            if label and value is not None:
                fields[label.rstrip("：:").strip()] = value
    for dt in soup.select("dl.detail-list dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None:
            label = _clean(dt.get_text()) or ""
            fields[label.rstrip("：:").strip()] = _clean(dd.get_text()) or ""
    return fields


def _pick(fields: dict[str, str], key: str) -> Optional[str]:
    for alias in FIELD_ALIASES[key]:
        if alias in fields:
            return _clean(fields[alias])
    return None


def parse_decision(html: str, source_url: str) -> BlacklistDecision:
    """解析禁入决定详情页，日期统一为 YYYY-MM-DD。"""
    soup = BeautifulSoup(html, "html.parser")
    fields = _field_map(soup)

    decision_no = _pick(fields, "decision_no")
    subject_code = _pick(fields, "subject_code")
    batch = _pick(fields, "batch")
    start_raw = _pick(fields, "start_date")
    end_raw = _pick(fields, "end_date")

    missing = [
        name for name, value in (
            ("决定编号", decision_no),
            ("统一社会信用代码", subject_code),
            ("发布批次", batch),
            ("禁入开始日", start_raw),
            ("禁入截止日", end_raw),
        )
        if not value
    ]
    if missing:
        raise BlacklistParseError(f"详情页缺少字段：{'、'.join(missing)}")

    start_date = normalize_date(start_raw)
    end_date = normalize_date(end_raw)
    if not start_date or not end_date:
        raise BlacklistParseError(
            f"详情页禁入起止日无法解析：{start_raw} ~ {end_raw}"
        )
    if end_date < start_date:
        raise BlacklistParseError(
            f"详情页禁入截止日早于开始日：{start_date} ~ {end_date}"
        )

    return BlacklistDecision(
        decision_no=decision_no,
        subject_code=subject_code,
        start_date=start_date,
        end_date=end_date,
        batch=batch,
        source_url=source_url,
        subject_name=_pick(fields, "subject_name"),
    )
