# 招投标公告采集 API

面向招投标专员的公共采购公告采集服务，提供两类能力：

1. **公告采集**：同步指定公告列表页与详情页，抽取公告编号、项目编号、
   公告类型、发布时间、投标截止时间、引用编号，沿引用链归并首次公告、
   更正公告与撤销公告，自动识别缺失引用、引用成环、并行更正等冲突，并输出
   每条链的状态、有效截止时间与来源链接。
2. **禁入名单核验**：输入名单目录 URL、统一社会信用代码与核验日期，抓取
   全部分页及决定详情，提取决定编号、主体代码、禁入起止日、发布批次和来源
   链接，按「起止日含当日」判定主体在核验日是否处于禁入期；分页/详情失败
   或批次变化时保留旧快照并返回**待核实**，绝不用旧快照证明本轮未禁入。

技术栈：FastAPI + BeautifulSoup4 + SQLite，Docker Compose 同时启动采集 API
与一个内置的示例公告站。

## 目录结构

```
.
├── app/                          # 采集 API
│   ├── config.py                 # 环境变量配置
│   ├── db.py                     # SQLite 连接与表结构
│   ├── fetcher.py                # HTTP 抓取（超时 + 有限重试）
│   ├── parser.py                 # 公告列表页/详情页解析（BeautifulSoup）
│   ├── blacklist_parser.py       # 禁入名单目录分页/决定详情解析
│   ├── chains.py                 # 引用链归并、冲突识别、有效截止时间计算
│   ├── service.py                # 公告同步编排与持久化（含失败保留上次结果）
│   ├── blacklist_service.py      # 禁入核验：分页抓取、同批校验、结论判定
│   ├── snapshot.py               # 按目录保存最近一次完整批次快照（原子写入）
│   ├── models.py                 # 请求/响应模型
│   └── main.py                   # FastAPI 路由
├── sample_site/                  # 示例公告站（仅 Python 标准库）
│   ├── server.py
│   └── Dockerfile
├── tests/                        # pytest 测试（36 个用例）
├── Dockerfile                    # API 镜像
├── docker-compose.yml            # api + sample-site 两个服务
├── run_tests.py                  # 本地测试入口（自动注入 .pylibs 路径）
├── run_server.py                 # 本地 API 启动入口（自动注入 .pylibs 路径）
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集 API：http://localhost:8000 （交互式文档 /docs）
- 示例公告站：http://localhost:8080/announcements

触发一次同步：

```bash
curl -X POST http://localhost:8000/api/v1/sync \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site:8080/announcements"}'
```

查询引用链与孤立异常公告：

```bash
curl http://localhost:8000/api/v1/chains
```

禁入名单核验（示例站禁入目录为 3 页、7 条决定）：

```bash
# 禁入：甲公司两条决定相邻，2026-06-30（截止日，含当日）仍在禁入期
curl -X POST http://localhost:8000/api/v1/blacklist/verify \
  -H 'Content-Type: application/json' \
  -d '{"index_url": "http://sample-site:8080/blacklist",
       "subject_code": "91110000ABCDEF001X",
       "verify_date": "2026-06-30"}'

# 未禁入：乙公司禁入期为 2025 全年，核验日不被任何决定覆盖
curl -X POST http://localhost:8000/api/v1/blacklist/verify \
  -H 'Content-Type: application/json' \
  -d '{"index_url": "http://sample-site:8080/blacklist",
       "subject_code": "91110000ABCDEF002Y",
       "verify_date": "2026-11-12"}'

# 查看该目录最近一次完整批次快照
curl "http://localhost:8000/api/v1/blacklist/snapshot?index_url=http://sample-site:8080/blacklist"
```

SQLite 数据文件与禁入名单快照存放在 Docker 卷 `api-data`
（容器内 `/data/procurement.db` 与 `/data/snapshots/`），容器重建后不丢失。

## 本地开发（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 终端 1：示例公告站
SAMPLE_PORT=8080 python -I sample_site/server.py

# 终端 2：API
DB_PATH=./data/procurement.db SNAPSHOT_DIR=./data/snapshots \
  python -I run_server.py 8000

# 测试（解释器默认路径不含依赖时用 run_tests.py 自动注入 .pylibs）
python -I run_tests.py -q
# 已把依赖安装进常规环境时也可直接：pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/v1/sync` | 同步指定列表页及其全部详情页，请求体 `{"list_url": "..."}` |
| GET | `/api/v1/chains` | 返回全部引用链 `chains` 与孤立异常公告 `orphans` |
| GET | `/api/v1/announcements/{notice_no}` | 查询单条公告（含抓取错误、链状态） |
| GET | `/api/v1/sync-runs?limit=20` | 同步执行记录 |
| POST | `/api/v1/blacklist/verify` | 禁入名单核验，请求体含 `index_url`、`subject_code`、`verify_date` |
| GET | `/api/v1/blacklist/snapshot?index_url=...` | 查询某目录最近一次完整批次快照 |

### 禁入核验响应

`POST /api/v1/blacklist/verify` 的 `status` 有三种取值：

| status | 含义 |
| --- | --- |
| `blocked` | 禁入：本轮完整批次中存在覆盖核验日的决定，`basis` 给出全部命中决定及依据 |
| `not_blocked` | 未禁入：**基于本轮完整抓取的同批名单**，全部决定均不覆盖核验日 |
| `pending` | 待核实：分页循环、抓取/解析失败或批次变化；旧快照保留但不参与结论，`reason` 说明原因 |

响应示例（禁入，含完整依据字段）：

```json
{
  "index_url": "http://sample-site:8080/blacklist",
  "subject_code": "91110000ABCDEF001X",
  "verify_date": "2026-06-30",
  "status": "blocked",
  "batch": "2026-B1",
  "basis": [
    {
      "decision_no": "BL-001",
      "subject_code": "91110000ABCDEF001X",
      "start_date": "2026-01-01",
      "end_date": "2026-06-30",
      "batch": "2026-B1",
      "source_url": "http://sample-site:8080/blacklist/decision/BL-001",
      "subject_name": "甲建设工程有限公司"
    }
  ],
  "snapshot_batch": "2026-B1",
  "old_snapshot_batch": null,
  "reason": null,
  "checked_at": "2026-11-12 20:54:37"
}
```

`/api/v1/chains` 响应示例（节选）：

```json
{
  "chains": [
    {
      "chain_id": "A-001",
      "project_no": "PRJ-A",
      "status": "ok",
      "effective_deadline": "2026-11-25 15:00:00",
      "announcements": [
        {
          "notice_no": "A-001",
          "notice_type": "first",
          "deadline": "2026-11-20 09:30:00",
          "ref_notice_no": null,
          "source_url": "http://sample-site:8080/notice/A-001",
          "conflict_reasons": [],
          "effective_deadline": "2026-11-25 15:00:00"
        }
      ]
    }
  ],
  "orphans": [
    {
      "notice_no": "D-001",
      "status": "orphan",
      "conflict_reasons": ["missing_reference"],
      "ref_notice_no": "X-999",
      "effective_deadline": null,
      "source_url": "http://sample-site:8080/notice/D-001"
    }
  ]
}
```

## 业务规则

### 链归并

- 公告类型映射：含「首次」→ `first`，含「更正」→ `correction`，
  含「撤销」→ `revocation`。
- 更正/撤销通过「引用编号」指向上一条公告；沿引用反向追溯，链标识取链上
  首次公告的公告编号。
- **同一项目编号下的不同首次公告保持独立**，不做合并（示例中 PRJ-G 有两条链）。
- 链状态：
  - `ok`：结构正常，返回有效截止时间；
  - `cancelled`：链上存在撤销公告，整条链失效，有效截止时间为 `null`；
  - `conflict`：链上存在冲突，**不推测截止时间**，有效截止时间为 `null`。
- 有效截止时间：取链上明确给出的最新截止时间；更正未给出截止时间时沿用链上
  此前的截止时间（继承已有数据，而非推测）。
- 撤销公告之后又挂接的公告不再属于该链，作为孤立异常公告处理
  （原因 `entry_after_revocation`）。

### 冲突识别（冲突即不推测截止时间）

| 冲突原因 | 触发条件 |
| --- | --- |
| `missing_reference` | 引用编号在库中不存在；或更正/撤销公告未填引用编号 |
| `reference_cycle` | 引用关系成环（含自环） |
| `parallel_correction` | 同一公告被两个及以上公告引用（并行更正） |
| `first_notice_with_reference` | 首次公告却携带引用编号（结构异常） |

无法归入任何链的公告进入 `orphans`，每条都带状态、冲突原因、来源链接，
且有效截止时间为 `null`。

### 详情抓取失败的容错

- 列表页抓取失败：本次同步标记 `failed`，不动库内现有数据。
- 单条详情页抓取/解析失败：
  - 此前成功过 → **保留上次完整结果**，仅记录 `last_fetch_error` 与同步时间，
    同步状态为 `partial`，响应中 `kept_last` 计数；
  - 从未成功 → 写入占位记录并归入孤立异常，避免静默丢失。
- 每次同步后基于库内现有数据全量重算链状态。

### 禁入名单核验

- **抓取范围**：从目录 URL 起跟随「下一页」抓完全部分页，再逐条抓取决定
  详情页；每条决定提取决定编号、主体代码（统一社会信用代码）、禁入开始日、
  禁入截止日、发布批次与来源链接，日期统一为 `YYYY-MM-DD`。
- **同批校验（列表页与详情须同批）**：每张目录页必须带批次标识，且全程与
  首批一致；每条详情的发布批次必须与目录批次相同，主体代码必须与目录入口
  一致。任一不一致即判 **待核实（pending）**。
- **分页循环防护**：重复访问同一分页 URL、或页数超过
  `BLACKLIST_MAX_PAGES` 上限时判待核实，避免异常分页导致死循环。
- **覆盖判定（起止日均含当日）**：决定区间为闭区间
  `[start_date, end_date]`，`start_date <= 核验日 <= end_date` 即覆盖。
  同一主体有多条决定时，**只要一条覆盖核验日就返回禁入**，并在 `basis`
  中给出全部覆盖决定；完整名单中没有任何决定覆盖时才返回未禁入。
- **快照与待核实纪律**：
  - 仅当整批完整抓取且同批校验通过时，才通过「临时文件 + 原子 rename」
    替换该目录的最近一次完整批次快照；
  - 分页循环、任一页读取或解析失败、批次变化时**保留旧快照不动**，并返回
    `pending` 与具体原因；
  - **任何不完整轮次都不得用旧快照证明本轮未禁入**：故障期间即使主体不在
    旧快照中，结论也只能是 `pending`；
  - 快照按目录 URL 的哈希分目录保存（`SNAPSHOT_DIR/<hash>/snapshot.json`
    与 `meta.json`），不同目录互不影响。

## 示例公告站内置场景

### 公告采集场景

| 编号 | 场景 | 预期结果 |
| --- | --- | --- |
| A-001 ~ A-003 | 首次 + 两条更正（中间一条无截止时间） | `ok`，有效截止取最新，中间项继承 |
| B-001 ~ B-003 | 首次 + 更正 + 撤销 | `cancelled`，有效截止为 null |
| C-001 ~ C-003 | 两条更正并行引用同一首次公告 | `conflict`（parallel_correction） |
| D-001 | 引用不存在的 X-999 | 孤立，missing_reference |
| E-001 / E-002 | 互相引用成环 | 孤立，reference_cycle |
| F-001 | 更正公告无引用编号 | 孤立，missing_reference |
| G-001 / G-101（+G-102） | 同项目两个独立首次公告 | 两条独立链 |
| H-001 | 可切换 500 故障 | 验证失败保留上次结果 |

故障开关（仅示例站）：

```bash
curl "http://localhost:8080/fail-toggle?enabled=1"   # 开启 H-001 详情 500
curl "http://localhost:8080/fail-toggle?enabled=0"   # 恢复
curl "http://localhost:8080/fail-status"
```

### 禁入名单场景（`/blacklist`，首批 2026-B1 共 3 页 7 条决定）

| 主体代码 | 决定 | 场景 | 预期 |
| --- | --- | --- | --- |
| 91110000ABCDEF001X | BL-001 `[01-01,06-30]`、BL-002 `[07-01,12-31]` | 同一主体两条相邻决定，边界日含当日 | 06-30 命中 BL-001、07-01 命中 BL-002，均禁入 |
| 91110000ABCDEF002Y | BL-003 `[2025-01-01,2025-12-31]` | 禁入期已过 | 2026-11-12 未禁入 |
| 91110000ABCDEF003Z | BL-004 长期禁入 | 跨年度区间 | 区间内任意核验日禁入 |
| 91110000ABCDEF004W | BL-005、BL-006 两条决定间有空档 | 多决定但不覆盖空档日 | 空档 2026-06-01 未禁入，区间内禁入 |
| 91110000ABCDEF005V | BL-007（位于第 3 页） | 验证跨分页完整抓取 | 快照 `page_count=3`、共 7 条 |
| （不存在的代码） | — | 完整名单中无该主体 | 未禁入；故障期间则为待核实 |

批次与故障控制（仅示例站）：

```bash
# 切换发布批次（B2 为 3 条决定的新批次，切换后旧决定编号 404）
curl "http://localhost:8080/blacklist-batch?name=2026-B2"
# 制造跨批混用：目录为当前批次、详情强制标注旧批 -> 核验判待核实
curl "http://localhost:8080/blacklist-detail-batch?batch=2026-B1"
curl "http://localhost:8080/blacklist-detail-batch?batch="      # 清除覆盖
# 目录页 / 详情页 500 故障注入
curl "http://localhost:8080/blacklist-fail-toggle?target=list&enabled=1"
curl "http://localhost:8080/blacklist-fail-toggle?target=detail&enabled=1"
curl "http://localhost:8080/blacklist-fail-status"
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/procurement.db` | SQLite 文件路径 |
| `SNAPSHOT_DIR` | `/data/snapshots` | 禁入名单完整批次快照根目录（按目录 URL 哈希分子目录） |
| `BLACKLIST_MAX_PAGES` | `200` | 禁入名单分页页数上限，超过即判待核实（防分页循环） |
| `FETCH_TIMEOUT` | `10` | 单次 HTTP 超时秒数 |
| `FETCH_RETRIES` | `2` | 网络错误/5xx 的重试次数（4xx 不重试） |
| `RETRY_BACKOFF` | `0.5` | 重试退避基数秒 |
| `KEEP_LAST_ON_DETAIL_FAILURE` | `true` | 公告详情失败时保留上次完整结果 |
| `SAMPLE_PORT`（仅示例站） | `8080` | 示例站监听端口 |
