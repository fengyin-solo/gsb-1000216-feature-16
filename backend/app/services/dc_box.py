"""汇流箱管理业务规则：支路电流解析、电流不平衡判定、采集空态与重试都收在这里。

关键口径：
- 支路电流缺失（None / 空串 / 非数值 / 负数）属于「无数据」空态，绝不按 0 参与计算；
- 采集到的 0A 是真实读数（例如夜间），同样不参与不平衡判断，避免均值被拉到 0 造成全组串误报；
- 设备缺少输入支路配置时为「待补资料」，通讯读不到电流时为「采集中断」，两者都是独立空态；
- 每次成功重新采集都会产生新的批次号（batch_no），空数据期间保留旧批次旧结果，
  但会打上 stale（非当前批次）标记，不把旧结果当成新批次重新计算。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "dc_box"
REQUIRED_FIELDS = ["汇流箱编号", "所属组串"]
STATUS_ORDER = ["正常运行", "支路异常", "通讯中断", "检修中"]
ACTION_RULES = {"排查支路": "正常运行", "复位通讯": "正常运行", "安排检修": "检修中"}
NEGATIVE_ACTIONS = []

# 支路电流相对均值的偏差超过该阈值即判为不平衡
IMBALANCE_THRESHOLD = 0.2
# 至少需要多少条有效（有数据且非零）支路电流才能判断不平衡
MIN_VALID_CURRENTS = 2

COLLECT_STATUS = ["正常", "采集中断", "待补资料"]

# 模拟重新采集时补回的支路电流（真实项目里这里换成采集服务调用）
_MOCK_RECOVER_CURRENTS = [8.1, 8.0, 8.2]


def _next_batch_no(rows: list[dict[str, Any]]) -> str:
    """批次号在现有最大批次上递增；读取失败不推进批次，恢复采集后才产生新批次。"""
    seq = 0
    for row in rows:
        raw = str(row.get("批次号") or "")
        if raw.startswith("BATCH-"):
            try:
                seq = max(seq, int(raw.removeprefix("BATCH-")))
            except ValueError:
                continue
    return f"BATCH-{seq + 1:04d}"


def _normalize_branches(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """把支路明细规整为 [{支路, 电流, 状态}]，状态为「正常」或「无数据」。

    输入支路缺失时返回空列表，由上层判为「待补资料」。
    """
    raw = entry.get("支路明细")
    if not isinstance(raw, list):
        return []
    branches: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        name = str((item or {}).get("支路") or f"支路{index}")
        value = (item or {}).get("电流")
        current: float | None
        try:
            current = float(value)
        except (TypeError, ValueError):
            current = None
        if current is None or current < 0:
            branches.append({"支路": name, "电流": None, "状态": "无数据"})
        else:
            branches.append({"支路": name, "电流": round(current, 2), "状态": "正常"})
    return branches


def _evaluate_imbalance(branches: list[dict[str, Any]]) -> dict[str, Any]:
    """只依据有效且非零的支路电流判断不平衡；缺测不补零、零电流不参与。"""
    valid = [b["电流"] for b in branches if b["状态"] == "正常" and b["电流"] and b["电流"] > 0]
    missing = sum(1 for b in branches if b["状态"] == "无数据")
    if len(valid) < MIN_VALID_CURRENTS:
        reason = "有效电流不足，无法判断" if branches else ""
        return {"结果": "无法判断", "偏差": None, "有效支路数": len(valid), "缺失支路数": missing, "说明": reason}
    average = sum(valid) / len(valid)
    max_deviation = max(abs(current - average) for current in valid) / average
    result = "不平衡" if max_deviation > IMBALANCE_THRESHOLD else "平衡"
    return {
        "结果": result,
        "偏差": round(max_deviation, 4),
        "有效支路数": len(valid),
        "缺失支路数": missing,
        "说明": "",
    }


def _collect_status(entry: dict[str, Any], branches: list[dict[str, Any]]) -> str:
    if not branches:
        return "待补资料"
    # 通讯状态为空、离线或明确读取失败都算采集中断
    if str(entry.get("通讯状态") or "").strip() in ("", "离线", "读取失败"):
        return "采集中断"
    if all(b["状态"] == "无数据" for b in branches):
        return "采集中断"
    return "正常"


def _display_status(entry: dict[str, Any], collect_status: str, imbalance: dict[str, Any]) -> str:
    """检修中以人工动作为准；其余状态由当前批次采集结果推导，空态不沿用旧的异常结论。"""
    if entry.get("status") == STATUS_ORDER[-1]:
        return STATUS_ORDER[-1]
    if collect_status == "待补资料":
        return "通讯中断"
    if collect_status == "采集中断":
        return "通讯中断"
    if imbalance["结果"] == "不平衡":
        return "支路异常"
    return "正常运行"


def _build_view(entry: dict[str, Any]) -> dict[str, Any]:
    """组装列表/明细视图：推导状态并标注该行是否处于采集空窗（旧批次保留）。"""
    view = dict(entry)
    branches = _normalize_branches(entry)
    collect_status = _collect_status(entry, branches)
    imbalance = _evaluate_imbalance(branches)
    batch_no = str(entry.get("批次号") or "")
    # stale 仅指本行采集中断、只能沿用上一批次读数；正常设备各自批次独立，不标 stale
    stale = collect_status in ("采集中断", "待补资料") and bool(batch_no)

    if collect_status != "正常":
        # 空数据期间：保留旧批次记录，但旧结论不作为当前批次结果，也不重新计算
        imbalance = {**imbalance, "结果": "无法判断", "偏差": None,
                     "说明": "采集空窗，沿用上一批次记录，未重新计算"}
    status = _display_status(entry, collect_status, imbalance)

    view["支路明细"] = branches
    view["支路数"] = len(branches)
    view["采集状态"] = collect_status
    view["不平衡判断"] = imbalance
    view["批次号"] = batch_no
    view["stale"] = stale
    view["status"] = status
    view["abnormal"] = status == "支路异常"
    return view


class DcBoxService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("汇流箱编号", ""))]
        views = [_build_view(row) for row in rows]
        if status:
            views = [view for view in views if view.get("status") == status]
        total = len(views)
        start = max(page - 1, 0) * size
        return views[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return _build_view(entry)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 新登记设备还没有输入支路配置与采集数据：待补资料，等待重试采集
        entry["输入支路"] = values.get("输入支路")
        entry["箱体温度"] = None
        entry["通讯状态"] = "离线"
        entry["支路明细"] = []
        entry["批次号"] = ""
        entry["上次检修日"] = values.get("上次检修日")
        entry["status"] = "通讯中断"
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def retry_collection(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """重新采集单台汇流箱。

        - 缺少输入支路配置：模拟补齐配置后再读数，恢复后才计算；
        - 通讯离线/电流读取失败：恢复通讯并写入新批次读数，旧批次结论作废；
        - 检修中不自动重采，避免检修状态下产生误导性结论；
        - 当前已有完整读数时视为重复重试，原样返回当前批次。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"汇流箱 {entry_id} 不存在或已归档"
        if entry.get("status") == STATUS_ORDER[-1]:
            return None, "汇流箱检修中，暂不重新采集"

        branches = _normalize_branches(entry)
        collect_status = _collect_status(entry, branches)
        if collect_status == "正常":
            return self.get_entry(entry_id), "采集正常，已是最新批次"

        if not branches:
            entry["输入支路"] = len(_MOCK_RECOVER_CURRENTS)
        entry["通讯状态"] = "在线"
        entry["支路明细"] = [{"支路": f"支路{index}", "电流": current} for index, current in enumerate(_MOCK_RECOVER_CURRENTS, start=1)]
        entry["箱体温度"] = 35.0
        entry["批次号"] = _next_batch_no(store.rows(MODULE))
        return self.get_entry(entry_id), "采集已恢复，按新批次重新计算"

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"汇流箱 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于汇流箱管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        if action == "复位通讯":
            # 与重试采集同口径：恢复在线并写入新批次读数后才重新计算
            entry, retry_message = self.retry_collection(entry_id)
            if entry is None:
                return None, retry_message
        return self.get_entry(entry_id), f"汇流箱已{action}"
