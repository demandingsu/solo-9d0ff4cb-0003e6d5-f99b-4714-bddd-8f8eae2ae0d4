"""示例公共采购公告站（仅依赖 Python 标准库）。

用于在本地/容器内演示采集 API，覆盖以下场景：
  链 A：首次 + 更正（含截止时间继承）
  链 B：首次 + 更正 + 撤销（撤销使链失效）
  链 C：并行更正（同一公告有两个后继 -> 冲突）
  链 D：引用编号不存在（missing_reference -> 冲突 + 孤立）
  链 E：引用成环（reference_cycle -> 孤立）
  链 F：更正无引用编号（missing_reference -> 孤立）
  链 G：同一项目两个独立首次公告（保持两条独立链）
  公告 H：可通过 /fail-toggle 控制详情页返回 500，验证失败保留上次结果

禁入名单（/blacklist 起）覆盖：
  - 目录跨 3 个分页页，详情页独立抓取；
  - 同一主体多条禁入决定（含相邻区间与边界日核验）；
  - /blacklist-batch?name=2026-B2 切换发布批次（列表与详情同时换批，
    旧批次详情链接在新批次下 404，用于演示批次变化 -> 待核实）；
  - /blacklist-fail-toggle 注入列表页/详情页 500 故障。

控制接口：
  GET /fail-toggle?enabled=1|0          开关 H-001 公告详情页的 500 故障
  GET /fail-status                      查询公告故障开关
  GET /blacklist-batch?name=2026-B2     切换禁入名单发布批次
  GET /blacklist-fail-toggle?target=list|detail&enabled=1|0
  GET /blacklist-fail-status            查询禁入名单故障开关
"""
from __future__ import annotations

import os
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 每条公告：编号、项目编号、类型、发布时间、截止时间、引用编号
NOTICES: list[dict] = [
    # 链 A：正常链，COR-A-02 不带截止时间（应继承首次的截止时间）
    {"no": "A-001", "project": "PRJ-A", "type": "首次公告",
     "publish": "2026-10-20 09:00:00", "deadline": "2026-11-20 09:30:00",
     "ref": ""},
    {"no": "A-002", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-10-25 14:00:00", "deadline": "",
     "ref": "A-001"},
    {"no": "A-003", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-11-01 10:00:00", "deadline": "2026-11-25 15:00:00",
     "ref": "A-002"},

    # 链 B：撤销使整条链失效
    {"no": "B-001", "project": "PRJ-B", "type": "首次公告",
     "publish": "2026-10-21 09:00:00", "deadline": "2026-11-21 09:30:00",
     "ref": ""},
    {"no": "B-002", "project": "PRJ-B", "type": "更正公告",
     "publish": "2026-10-26 14:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": "B-001"},
    {"no": "B-003", "project": "PRJ-B", "type": "撤销公告",
     "publish": "2026-10-28 16:00:00", "deadline": "",
     "ref": "B-002"},

    # 链 C：并行更正（C-002 与 C-003 同时引用 C-001）
    {"no": "C-001", "project": "PRJ-C", "type": "首次公告",
     "publish": "2026-10-22 09:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": ""},
    {"no": "C-002", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 10:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "C-001"},
    {"no": "C-003", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 15:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "C-001"},

    # 链 D：引用了不存在的公告 X-999
    {"no": "D-001", "project": "PRJ-D", "type": "更正公告",
     "publish": "2026-10-23 09:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "X-999"},

    # 链 E：成环 E-001 -> E-002 -> E-001
    {"no": "E-001", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 09:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "E-002"},
    {"no": "E-002", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 10:00:00", "deadline": "2026-11-25 09:30:00",
     "ref": "E-001"},

    # 链 F：更正公告没有引用编号
    {"no": "F-001", "project": "PRJ-F", "type": "更正公告",
     "publish": "2026-10-25 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},

    # 链 G：同一项目两个独立首次公告
    {"no": "G-001", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-26 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},
    {"no": "G-101", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-27 09:00:00", "deadline": "2026-11-27 09:30:00",
     "ref": ""},
    {"no": "G-102", "project": "PRJ-G", "type": "更正公告",
     "publish": "2026-10-29 09:00:00", "deadline": "2026-11-28 09:30:00",
     "ref": "G-101"},

    # H：详情页故障开关目标（默认正常）
    {"no": "H-001", "project": "PRJ-H", "type": "首次公告",
     "publish": "2026-10-30 09:00:00", "deadline": "2026-11-30 09:30:00",
     "ref": ""},
]

NOTICE_BY_NO = {n["no"]: n for n in NOTICES}

# 运行期故障开关（内存态，重启容器即恢复）
FAIL_ENABLED = {"H-001": False}

# ---------------------------------------------------------------------------
# 禁入名单：按发布批次组织。每个批次是一组禁入决定，目录页按 PAGE_SIZE 分页。
# 每条决定：决定编号、主体名称、统一社会信用代码、禁入开始日、禁入截止日、批次。
# 场景设计：
#   BL-001 / BL-002 同一主体 91110000ABCDEF001X 的两条决定，相邻区间（核验
#       边界日 2026-06-30 与 2026-07-01 均应判禁入）；
#   BL-003 另一主体，区间已过 -> 对核验日 2026-11-12 未禁入；
#   BL-004 长期禁入主体；
#   BL-005 / BL-006 第三个多决定主体，区间之间有空档（空档日未禁入）；
#   BL-007 跨分页边界，确保第三页也有数据。
# 第二批 2026-B2：决定编号整体变化，模拟发布批次更新（旧详情链接随即 404）。
# ---------------------------------------------------------------------------
BLACKLIST_BATCHES: dict[str, list[dict]] = {
    "2026-B1": [
        {"no": "BL-001", "name": "甲建设工程有限公司",
         "code": "91110000ABCDEF001X",
         "start": "2026-01-01", "end": "2026-06-30", "batch": "2026-B1"},
        {"no": "BL-002", "name": "甲建设工程有限公司",
         "code": "91110000ABCDEF001X",
         "start": "2026-07-01", "end": "2026-12-31", "batch": "2026-B1"},
        {"no": "BL-003", "name": "乙物资供应有限公司",
         "code": "91110000ABCDEF002Y",
         "start": "2025-01-01", "end": "2025-12-31", "batch": "2026-B1"},
        {"no": "BL-004", "name": "丙监理咨询有限公司",
         "code": "91110000ABCDEF003Z",
         "start": "2024-01-01", "end": "2029-12-31", "batch": "2026-B1"},
        {"no": "BL-005", "name": "丁信息技术有限公司",
         "code": "91110000ABCDEF004W",
         "start": "2026-02-01", "end": "2026-03-31", "batch": "2026-B1"},
        {"no": "BL-006", "name": "丁信息技术有限公司",
         "code": "91110000ABCDEF004W",
         "start": "2026-08-01", "end": "2026-09-30", "batch": "2026-B1"},
        {"no": "BL-007", "name": "戊园林绿化有限公司",
         "code": "91110000ABCDEF005V",
         "start": "2026-05-01", "end": "2027-04-30", "batch": "2026-B1"},
    ],
    "2026-B2": [
        {"no": "BL-101", "name": "甲建设工程有限公司",
         "code": "91110000ABCDEF001X",
         "start": "2026-07-01", "end": "2027-06-30", "batch": "2026-B2"},
        {"no": "BL-102", "name": "己检测技术有限公司",
         "code": "91110000ABCDEF006U",
         "start": "2026-10-01", "end": "2028-09-30", "batch": "2026-B2"},
        {"no": "BL-103", "name": "丁信息技术有限公司",
         "code": "91110000ABCDEF004W",
         "start": "2026-11-01", "end": "2026-11-30", "batch": "2026-B2"},
    ],
}

# 当前发布批次与每页条数（运行期可切换）
BLACKLIST_STATE = {
    "batch": "2026-B1",
    "page_size": 3,
    # 详情批次覆盖：置为非 None 时，所有详情页标注该批次，模拟发布过程中
    # 列表页已换新批、详情仍挂旧批的跨批混用（核验应判待核实）。
    "detail_batch_override": None,
}

# 禁入名单故障注入：list=目录页 500；detail=所有决定详情 500
BLACKLIST_FAIL = {"list": False, "detail": False}

PAGE_CSS = """
body { font-family: sans-serif; margin: 2em; color: #222; }
h1 { font-size: 1.4em; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 6px 10px; text-align: left; }
th { background: #f0f3f7; }
a { color: #1a56c4; }
.meta { color: #666; margin-bottom: 1em; }
"""


def render_list() -> str:
    rows = "\n".join(
        '<tr><td><a href="/notice/{no}">{no}</a></td>'
        '<td>{project}</td><td>{type}</td><td>{publish}</td></tr>'.format(
            no=escape(n["no"]), project=escape(n["project"]),
            type=escape(n["type"]), publish=escape(n["publish"]),
        )
        for n in NOTICES
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公共采购公告列表</title><style>{PAGE_CSS}</style></head>
<body>
<h1>公共采购公告列表（示例站）</h1>
<p class="meta">共 {len(NOTICES)} 条公告 · 仅用于演示采集 API
　|　<a href="/blacklist">市场禁入名单目录 →</a></p>
<table class="announcement-list">
<thead><tr><th>公告编号</th><th>项目编号</th><th>类型</th><th>发布时间</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""


def _row(label: str, value: str) -> str:
    return (
        f"<tr><th>{escape(label)}</th>"
        f"<td>{escape(value) if value else '—'}</td></tr>"
    )


def render_detail(no: str) -> str:
    n = NOTICE_BY_NO[no]
    rows = "".join([
        _row("公告编号", n["no"]),
        _row("项目编号", n["project"]),
        _row("公告类型", n["type"]),
        _row("发布时间", n["publish"]),
        _row("投标截止时间", n["deadline"]),
        _row("引用编号", n["ref"]),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公告 {escape(n['no'])}</title><style>{PAGE_CSS}</style></head>
<body>
<h1>公告详情 {escape(n['no'])}</h1>
<p><a href="/announcements">← 返回列表</a></p>
<table class="detail-table"><tbody>{rows}</tbody></table>
</body></html>"""


def _blacklist_decisions() -> list[dict]:
    return BLACKLIST_BATCHES[BLACKLIST_STATE["batch"]]


def render_blacklist_index(page: int) -> str:
    """渲染禁入名单目录页（分页），page 从 1 开始。"""
    decisions = _blacklist_decisions()
    batch = BLACKLIST_STATE["batch"]
    size = BLACKLIST_STATE["page_size"]
    total_pages = max(1, (len(decisions) + size - 1) // size)
    page = max(1, min(page, total_pages))
    chunk = decisions[(page - 1) * size: page * size]

    rows = "\n".join(
        '<tr><td><a href="/blacklist/decision/{no}">{no}</a></td>'
        '<td>{name}</td><td>{code}</td>'
        '<td>{start}</td><td>{end}</td></tr>'.format(
            no=escape(d["no"]), name=escape(d["name"]),
            code=escape(d["code"]), start=escape(d["start"]),
            end=escape(d["end"]),
        )
        for d in chunk
    )

    pager_parts = [
        f'<span>第 {page} / {total_pages} 页</span>',
        f'<span>共 {len(decisions)} 条决定 · 每页 {size} 条</span>',
    ]
    if page > 1:
        pager_parts.append(
            f'<a href="/blacklist?page={page - 1}">上一页</a>'
        )
    if page < total_pages:
        pager_parts.append(
            f'<a rel="next" href="/blacklist?page={page + 1}">下一页</a>'
        )
    pager = "　".join(pager_parts)

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>禁入名单目录（{escape(batch)}）</title><style>{PAGE_CSS}</style></head>
<body>
<h1 data-batch="{escape(batch)}">市场禁入名单目录 · 发布批次 {escape(batch)}</h1>
<p class="meta">本页及全部详情页均属于同一发布批次 {escape(batch)}</p>
<table class="blacklist-list">
<thead><tr><th>决定编号</th><th>主体名称</th><th>统一社会信用代码</th>
<th>禁入开始日</th><th>禁入截止日</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<p class="pager">{pager}</p>
<p><a href="/announcements">← 返回公告列表</a></p>
</body></html>"""


def render_blacklist_detail(no: str) -> str:
    decisions = {d["no"]: d for d in _blacklist_decisions()}
    d = dict(decisions[no])
    # 模拟跨批混用：详情页批次被单独覆盖（与目录页批次不一致）
    if BLACKLIST_STATE.get("detail_batch_override"):
        d["batch"] = BLACKLIST_STATE["detail_batch_override"]
    rows = "".join([
        _row("决定编号", d["no"]),
        _row("主体名称", d["name"]),
        _row("统一社会信用代码", d["code"]),
        _row("禁入开始日", d["start"]),
        _row("禁入截止日", d["end"]),
        _row("发布批次", d["batch"]),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>禁入决定 {escape(d['no'])}</title><style>{PAGE_CSS}</style></head>
<body>
<h1 data-batch="{escape(d['batch'])}">禁入决定详情 {escape(d['no'])}</h1>
<p><a href="/blacklist">← 返回禁入名单目录</a></p>
<table class="detail-table"><tbody>{rows}</tbody></table>
<p class="meta">来源：示例站禁入名单 · 批次 {escape(d['batch'])}</p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: str, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/", "/announcements"):
            self._send(render_list())
            return

        # --- 禁入名单 ---
        if path == "/blacklist":
            if BLACKLIST_FAIL["list"]:
                self._send(
                    "<h1>500 禁入名单目录故障（模拟）</h1>", status=500
                )
                return
            try:
                page = int(query.get("page", ["1"])[0])
            except ValueError:
                page = 1
            self._send(render_blacklist_index(page))
            return

        if path == "/blacklist-batch":
            name = query.get("name", [BLACKLIST_STATE["batch"]])[0]
            if name not in BLACKLIST_BATCHES:
                self._send(
                    f"<h1>400 未知批次 {escape(name)}</h1>", status=400
                )
                return
            BLACKLIST_STATE["batch"] = name
            # 正常批次切换同时清掉详情批次覆盖
            BLACKLIST_STATE["detail_batch_override"] = None
            self._send(
                f'{{"blacklist_batch": "{escape(name)}"}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/blacklist-detail-batch":
            # 详情批次覆盖开关：?batch=2026-B1 强制详情标注旧批；?batch= 清除
            name = query.get("batch", [""])[0]
            BLACKLIST_STATE["detail_batch_override"] = name or None
            self._send(
                '{"detail_batch_override": "%s"}'
                % escape(BLACKLIST_STATE["detail_batch_override"] or ""),
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/blacklist-fail-toggle":
            target = query.get("target", ["list"])[0]
            if target not in ("list", "detail"):
                self._send("<h1>400 target 须为 list 或 detail</h1>", status=400)
                return
            enabled = (
                query.get("enabled", ["0"])[0] in {"1", "true", "yes"}
            )
            BLACKLIST_FAIL[target] = enabled
            self._send(
                f'{{"target": "{target}", "fail_enabled": '
                f'{str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/blacklist-fail-status":
            self._send(
                '{"list_fail": %s, "detail_fail": %s}'
                % (
                    str(BLACKLIST_FAIL["list"]).lower(),
                    str(BLACKLIST_FAIL["detail"]).lower(),
                ),
                content_type="application/json; charset=utf-8",
            )
            return

        if path.startswith("/blacklist/decision/"):
            no = path.rsplit("/", 1)[-1]
            decisions = {d["no"]: d for d in _blacklist_decisions()}
            if no not in decisions:
                # 批次切换后旧决定编号不再存在 -> 404，模拟来源失效
                self._send("<h1>404 禁入决定不存在</h1>", status=404)
                return
            if BLACKLIST_FAIL["detail"]:
                self._send(
                    "<h1>500 禁入决定详情故障（模拟）</h1>", status=500
                )
                return
            self._send(render_blacklist_detail(no))
            return

        if path == "/fail-toggle":
            enabled = (query.get("enabled", ["0"])[0] in {"1", "true", "yes"})
            FAIL_ENABLED["H-001"] = enabled
            self._send(
                f'{{"H-001_fail_enabled": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/fail-status":
            enabled = FAIL_ENABLED["H-001"]
            self._send(
                f'{{"H-001_fail_enabled": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path.startswith("/notice/"):
            no = path.rsplit("/", 1)[-1]
            notice = NOTICE_BY_NO.get(no)
            if notice is None:
                self._send("<h1>404 公告不存在</h1>", status=404)
                return
            if FAIL_ENABLED.get(no):
                self._send("<h1>500 服务器内部错误（模拟故障）</h1>", status=500)
                return
            self._send(render_detail(no))
            return

        self._send("<h1>404</h1>", status=404)

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"[sample-site] {self.address_string()} {fmt % args}")


def main() -> None:
    port = int(os.getenv("SAMPLE_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[sample-site] 示例公告站监听 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
