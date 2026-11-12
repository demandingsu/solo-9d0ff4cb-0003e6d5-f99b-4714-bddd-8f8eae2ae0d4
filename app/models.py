"""Pydantic 请求/响应模型。"""
from typing import Optional

from pydantic import BaseModel, Field


class SyncRequest(BaseModel):
    list_url: str = Field(..., description="公共采购网站公告列表页 URL")


class AnnouncementItem(BaseModel):
    notice_no: str
    project_no: Optional[str] = None
    notice_type: Optional[str] = None
    publish_time: Optional[str] = None
    deadline: Optional[str] = None
    ref_notice_no: Optional[str] = None
    source_url: Optional[str] = None
    conflict_reasons: list[str] = []
    effective_deadline: Optional[str] = None


class Chain(BaseModel):
    chain_id: str = Field(..., description="链标识，即首次公告编号")
    project_no: Optional[str] = None
    status: str = Field(..., description="ok / conflict / cancelled")
    effective_deadline: Optional[str] = Field(
        None, description="有效截止时间；冲突或已撤销时为 null"
    )
    announcements: list[AnnouncementItem] = []


class OrphanAnnouncement(BaseModel):
    notice_no: str
    project_no: Optional[str] = None
    notice_type: Optional[str] = None
    status: str
    conflict_reasons: list[str] = []
    effective_deadline: Optional[str] = None
    source_url: Optional[str] = None
    publish_time: Optional[str] = None
    deadline: Optional[str] = None
    ref_notice_no: Optional[str] = None


class QueryResponse(BaseModel):
    chains: list[Chain] = []
    orphans: list[OrphanAnnouncement] = []


class SyncResponse(BaseModel):
    run_id: str
    status: str
    total_listed: int
    fetched_ok: int
    failed_count: int
    kept_last: int
    message: Optional[str] = None


class BlacklistVerifyRequest(BaseModel):
    index_url: str = Field(..., description="禁入名单目录页 URL（支持分页）")
    subject_code: str = Field(
        ..., description="受核验主体的统一社会信用代码", min_length=1
    )
    verify_date: str = Field(..., description="核验日期，格式 YYYY-MM-DD")


class BlacklistDecisionItem(BaseModel):
    decision_no: str = Field(..., description="决定编号")
    subject_code: str = Field(..., description="主体代码（统一社会信用代码）")
    start_date: str = Field(..., description="禁入开始日 YYYY-MM-DD（含当日）")
    end_date: str = Field(..., description="禁入截止日 YYYY-MM-DD（含当日）")
    batch: str = Field(..., description="发布批次")
    source_url: str = Field(..., description="决定详情来源链接")
    subject_name: Optional[str] = None


class BlacklistVerifyResponse(BaseModel):
    index_url: str
    subject_code: str
    verify_date: str
    status: str = Field(
        ...,
        description="blocked=禁入；not_blocked=未禁入（基于本轮完整批次）；"
        "pending=待核实（抓取/解析失败、分页循环或批次变化，旧快照不参与结论）",
    )
    batch: Optional[str] = Field(
        None, description="本轮成功抓取的完整批次；待核实且无快照时为 null"
    )
    basis: list[BlacklistDecisionItem] = Field(
        default_factory=list,
        description="禁入依据：覆盖核验日的决定列表；待核实/未禁入时为空",
    )
    snapshot_batch: Optional[str] = Field(
        None, description="当前保存的最近一次完整批次快照的批次号"
    )
    old_snapshot_batch: Optional[str] = Field(
        None, description="本轮抓取前已有快照的批次号（无则 null）"
    )
    reason: Optional[str] = Field(
        None, description="待核实原因；blocked/not_blocked 时为 null"
    )
    checked_at: str


class BlacklistSnapshotResponse(BaseModel):
    index_url: str
    exists: bool
    batch: Optional[str] = None
    page_count: Optional[int] = None
    decision_count: Optional[int] = None
    captured_at: Optional[str] = None
    decisions: list[BlacklistDecisionItem] = []
