"""禁入名单核验：跨页抓取、多决定覆盖、边界日、失败/批次变化待核实、快照保留。"""
from __future__ import annotations

import importlib

import pytest

from app.blacklist_parser import (
    BlacklistParseError,
    parse_decision,
    parse_index,
)

BL_INDEX = "/blacklist"

@pytest.fixture()
def bl_client(tmp_path, monkeypatch):
    """独立快照目录 + 独立数据库的 API 客户端，并重置示例站禁入名单状态。"""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("SNAPSHOT_DIR", str(tmp_path / "snapshots"))

    import app.config as config
    import app.db as db
    import app.main as main_mod
    import app.snapshot as snapshot_mod
    from sample_site import server as sample_server

    importlib.reload(config)
    importlib.reload(db)
    importlib.reload(snapshot_mod)
    importlib.reload(main_mod)

    # 每个用例从首批、无故障、无批次覆盖开始
    sample_server.BLACKLIST_STATE["batch"] = "2026-B1"
    sample_server.BLACKLIST_STATE["detail_batch_override"] = None
    sample_server.BLACKLIST_FAIL["list"] = False
    sample_server.BLACKLIST_FAIL["detail"] = False

    from fastapi.testclient import TestClient

    with TestClient(main_mod.app) as c:
        yield c, sample_server, config


def _verify(c, base_url, code, day):
    return c.post(
        "/api/v1/blacklist/verify",
        json={
            "index_url": f"{base_url}{BL_INDEX}",
            "subject_code": code,
            "verify_date": day,
        },
    )


# --- 解析器单元测试 ---------------------------------------------------------

def test_parse_index_pagination_and_entries(site_base_url):
    import httpx

    html = httpx.get(f"{site_base_url}{BL_INDEX}?page=1").text
    page = parse_index(html, f"{site_base_url}{BL_INDEX}?page=1")
    assert page.batch == "2026-B1"
    assert len(page.entries) == 3
    assert page.next_url.endswith("/blacklist?page=2")
    first = page.entries[0]
    assert first.decision_no == "BL-001"
    assert first.subject_code == "91110000ABCDEF001X"
    assert first.detail_url.endswith("/blacklist/decision/BL-001")

    last_html = httpx.get(f"{site_base_url}{BL_INDEX}?page=3").text
    last_page = parse_index(last_html, f"{site_base_url}{BL_INDEX}?page=3")
    assert last_page.next_url is None  # 末页无下一页


def test_parse_decision_extracts_all_fields(site_base_url):
    import httpx

    html = httpx.get(f"{site_base_url}/blacklist/decision/BL-001").text
    decision = parse_decision(html, f"{site_base_url}/blacklist/decision/BL-001")
    assert decision.decision_no == "BL-001"
    assert decision.subject_code == "91110000ABCDEF001X"
    assert decision.start_date == "2026-01-01"
    assert decision.end_date == "2026-06-30"
    assert decision.batch == "2026-B1"
    assert decision.source_url.endswith("/blacklist/decision/BL-001")


def test_parse_index_raises_on_missing_table():
    with pytest.raises(BlacklistParseError):
        parse_index("<html><body>无表格</body></html>", "http://x/blacklist")


def test_parse_decision_raises_on_missing_field():
    html = (
        '<table class="detail-table"><tr><th>决定编号</th><td>X-1</td></tr>'
        '<tr><th>统一社会信用代码</th><td>91110000ABCDEF001X</td></tr></table>'
    )
    with pytest.raises(BlacklistParseError):
        parse_decision(html, "http://x/d")


# --- 端到端核验 -------------------------------------------------------------

def test_blocked_with_multi_decisions_and_boundary_days(bl_client, site_base_url):
    c, _, _ = bl_client
    code = "91110000ABCDEF001X"  # BL-001 [01-01,06-30] + BL-002 [07-01,12-31]

    # 区间内部
    body = _verify(c, site_base_url, code, "2026-03-15").json()
    assert body["status"] == "blocked"
    assert body["batch"] == "2026-B1"
    assert len(body["basis"]) == 1
    assert body["basis"][0]["decision_no"] == "BL-001"
    assert body["basis"][0]["source_url"].endswith("/blacklist/decision/BL-001")

    # 起止日含当日：BL-001 截止日 2026-06-30 仍禁入
    boundary_end = _verify(c, site_base_url, code, "2026-06-30").json()
    assert boundary_end["status"] == "blocked"
    assert {d["decision_no"] for d in boundary_end["basis"]} == {"BL-001"}

    # 相邻决定：2026-07-01 是 BL-002 开始日，同样禁入（无空档）
    boundary_start = _verify(c, site_base_url, code, "2026-07-01").json()
    assert boundary_start["status"] == "blocked"
    assert {d["decision_no"] for d in boundary_start["basis"]} == {"BL-002"}


def test_not_blocked_when_no_decision_covers_date(bl_client, site_base_url):
    c, _, _ = bl_client
    # BL-003 区间为 2025 全年，核验日 2026-11-12 不被任何决定覆盖
    body = _verify(c, site_base_url, "91110000ABCDEF002Y", "2026-11-12").json()
    assert body["status"] == "not_blocked"
    assert body["basis"] == []
    assert body["batch"] == "2026-B1"


def test_gap_between_two_decisions_is_not_blocked(bl_client, site_base_url):
    c, _, _ = bl_client
    code = "91110000ABCDEF004W"  # BL-005 [02-01,03-31]、BL-006 [08-01,09-30]
    assert _verify(c, site_base_url, code, "2026-02-15").json()["status"] == "blocked"
    # 两条决定之间的空档
    gap = _verify(c, site_base_url, code, "2026-06-01").json()
    assert gap["status"] == "not_blocked"
    assert _verify(c, site_base_url, code, "2026-09-30").json()["status"] == "blocked"


def test_unknown_subject_is_not_blocked(bl_client, site_base_url):
    c, _, _ = bl_client
    body = _verify(c, site_base_url, "91999999UNKNOWN99X", "2026-11-12").json()
    assert body["status"] == "not_blocked"


def test_full_crawl_covers_three_pages(bl_client, site_base_url):
    c, _, _ = bl_client
    _verify(c, site_base_url, "91110000ABCDEF005V", "2026-11-12")
    snap = c.get(
        "/api/v1/blacklist/snapshot",
        params={"index_url": f"{site_base_url}{BL_INDEX}"},
    ).json()
    assert snap["exists"] is True
    assert snap["batch"] == "2026-B1"
    assert snap["page_count"] == 3
    assert snap["decision_count"] == 7


def test_snapshot_kept_and_pending_when_detail_fails(bl_client, site_base_url):
    c, sample_server, config = bl_client
    index_url = f"{site_base_url}{BL_INDEX}"

    # 首轮完整成功并落快照
    first = _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15").json()
    assert first["status"] == "blocked"
    assert first["snapshot_batch"] == "2026-B1"

    # 开启详情页故障后再次核验：待核实，旧快照保留
    sample_server.BLACKLIST_FAIL["detail"] = True
    pending = _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15").json()
    assert pending["status"] == "pending"
    assert pending["batch"] is None
    assert pending["basis"] == []
    assert "详情" in pending["reason"]
    # 旧快照未被破坏
    snap = c.get(
        "/api/v1/blacklist/snapshot", params={"index_url": index_url}
    ).json()
    assert snap["exists"] is True
    assert snap["batch"] == "2026-B1"
    assert snap["decision_count"] == 7
    assert pending["snapshot_batch"] == "2026-B1"

    # 关键安全约束：旧快照不能证明本轮未禁入 —— 换一个名单内不存在的主体，
    # 故障期间同样只能得到 pending，而不是 not_blocked
    unknown = _verify(c, site_base_url, "91999999UNKNOWN99X", "2026-11-12").json()
    assert unknown["status"] == "pending"

    # 故障恢复后可正常核验
    sample_server.BLACKLIST_FAIL["detail"] = False
    healed = _verify(c, site_base_url, "91110000ABCDEF002Y", "2026-11-12").json()
    assert healed["status"] == "not_blocked"


def test_pending_when_list_page_fails(bl_client, site_base_url):
    c, sample_server, _ = bl_client
    # 先成功一轮
    _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15")
    sample_server.BLACKLIST_FAIL["list"] = True
    pending = _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15").json()
    assert pending["status"] == "pending"
    assert "目录页" in pending["reason"]


def test_batch_mismatch_keeps_old_snapshot_and_returns_pending(
    bl_client, site_base_url
):
    """目录页已是新批、详情仍挂旧批（跨批混用）-> 待核实，旧快照保留。"""
    c, sample_server, _ = bl_client
    index_url = f"{site_base_url}{BL_INDEX}"

    first = _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15").json()
    assert first["status"] == "blocked"
    assert first["batch"] == "2026-B1"

    # 目录切到 B2，但详情页批次仍标注 B1 -> 列表页与详情不同批
    sample_server.BLACKLIST_STATE["batch"] = "2026-B2"
    sample_server.BLACKLIST_STATE["detail_batch_override"] = "2026-B1"
    pending = _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15").json()
    assert pending["status"] == "pending"
    assert "批次" in pending["reason"]
    # 旧快照（B1）原样保留，批次未被覆盖
    snap = c.get(
        "/api/v1/blacklist/snapshot", params={"index_url": index_url}
    ).json()
    assert snap["batch"] == "2026-B1"
    assert snap["decision_count"] == 7

    # 消除覆盖后 B2 自身完整同批，下一轮成功并原子替换为 B2 快照
    sample_server.BLACKLIST_STATE["detail_batch_override"] = None
    ok = _verify(c, site_base_url, "91110000ABCDEF006U", "2026-11-12").json()
    assert ok["status"] == "blocked"
    assert ok["batch"] == "2026-B2"
    assert ok["old_snapshot_batch"] == "2026-B1"
    new_snap = c.get(
        "/api/v1/blacklist/snapshot", params={"index_url": index_url}
    ).json()
    assert new_snap["batch"] == "2026-B2"
    assert new_snap["decision_count"] == 3


def test_full_new_batch_replaces_snapshot(bl_client, site_base_url):
    """正常切换到完整的新批次：新快照替换旧快照，结论基于新批次。"""
    c, sample_server, _ = bl_client
    index_url = f"{site_base_url}{BL_INDEX}"

    _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15")
    sample_server.BLACKLIST_STATE["batch"] = "2026-B2"

    # B2 中主体 004W 的禁入区间为 11 月整月
    blocked = _verify(c, site_base_url, "91110000ABCDEF004W", "2026-11-12").json()
    assert blocked["status"] == "blocked"
    assert blocked["batch"] == "2026-B2"
    assert blocked["basis"][0]["decision_no"] == "BL-103"

    snap = c.get(
        "/api/v1/blacklist/snapshot", params={"index_url": index_url}
    ).json()
    assert snap["batch"] == "2026-B2"
    assert snap["page_count"] == 1  # B2 只有 3 条，单页
    assert {d["decision_no"] for d in snap["decisions"]} == {
        "BL-101", "BL-102", "BL-103"
    }


def test_snapshot_isolated_per_index_url(bl_client, site_base_url, tmp_path):
    c, _, config = bl_client
    _verify(c, site_base_url, "91110000ABCDEF001X", "2026-03-15")
    # 不同目录 URL 对应不同快照子目录
    other = c.get(
        "/api/v1/blacklist/snapshot",
        params={"index_url": f"{site_base_url}/other-blacklist"},
    ).json()
    assert other["exists"] is False
    current = c.get(
        "/api/v1/blacklist/snapshot",
        params={"index_url": f"{site_base_url}{BL_INDEX}"},
    ).json()
    assert current["exists"] is True
    # 快照根目录下按哈希分了一个子目录
    assert len(list(config.SNAPSHOT_DIR.iterdir())) == 1


def test_verify_bad_requests(bl_client):
    c, _, _ = bl_client
    bad_scheme = c.post(
        "/api/v1/blacklist/verify",
        json={
            "index_url": "ftp://x/blacklist",
            "subject_code": "91110000ABCDEF001X",
            "verify_date": "2026-11-12",
        },
    )
    assert bad_scheme.status_code == 400

    bad_date = c.post(
        "/api/v1/blacklist/verify",
        json={
            "index_url": "http://x/blacklist",
            "subject_code": "91110000ABCDEF001X",
            "verify_date": "2026/11/12",
        },
    )
    assert bad_date.status_code == 400


def test_unreachable_index_returns_pending(bl_client):
    c, _, _ = bl_client
    body = c.post(
        "/api/v1/blacklist/verify",
        json={
            "index_url": "http://127.0.0.1:1/blacklist",
            "subject_code": "91110000ABCDEF001X",
            "verify_date": "2026-11-12",
        },
    ).json()
    assert body["status"] == "pending"
    assert "目录页" in body["reason"]


def test_pagination_loop_detected(bl_client, monkeypatch):
    """构造互相指向的分页链接，验证分页循环 -> 待核实。"""
    from app import blacklist_service as service
    from app.blacklist_parser import BlacklistIndexPage, BlacklistEntry

    pages = {
        "http://x/blacklist": BlacklistIndexPage(
            batch="B1",
            entries=[BlacklistEntry("D-1", "91110000ABCDEF001X",
                                    "http://x/d/D-1")],
            next_url="http://x/blacklist?page=2",
        ),
        "http://x/blacklist?page=2": BlacklistIndexPage(
            batch="B1",
            entries=[BlacklistEntry("D-2", "91110000ABCDEF002Y",
                                    "http://x/d/D-2")],
            # 指回第一页 -> 循环
            next_url="http://x/blacklist",
        ),
    }
    monkeypatch.setattr(
        "app.fetcher.fetch", lambda url: "<html></html>"
    )
    monkeypatch.setattr(
        "app.blacklist_parser.parse_index", lambda html, url: pages[url]
    )

    with pytest.raises(service.PendingVerification, match="分页循环"):
        service.crawl_full_batch("http://x/blacklist")
