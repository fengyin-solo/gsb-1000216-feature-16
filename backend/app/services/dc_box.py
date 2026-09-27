"""汇流箱管理业务规则：状态流转、字段校验、支路电流采集与不平衡判断。

约定：
- 支路电流按通道存储（branch_currents 列表），缺失通道用 None 表示，
  绝不把缺失/读取失败当成 0 电流参与不平衡判断。
- 每次成功采集生成新批次（batch_id 单调递增）；采集失败不推进批次，
  已有读数与判定结果原样保留，避免把旧结果当成新批次。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "dc_box"
REQUIRED_FIELDS = ["汇流箱编号", "所属组串", "输入支路"]
STATUS_ORDER = ["正常运行", "支路异常", "通讯中断", "检修中"]
ACTION_RULES = {"排查支路": "正常运行", "复位通讯": "正常运行", "安排检修": "检修中"}
NEGATIVE_ACTIONS = []

# 支路电流不平衡阈值：任一有效读数相对平均读数偏离 10% 即判支路异常
IMBALANCE_RATIO = 0.10
# 至少两路有效读数才做不平衡判断，否则只给空态不下结论
MIN_VALID_READINGS = 2

DATA_STATE_OK = "正常"
DATA_STATE_PENDING = "待补资料"
DATA_STATE_FAILED = "采集失败"


def _channel_count(entry: dict[str, Any]) -> int:
    """输入支路数：profile 缺失或不可解析时按 0 处理，走待补资料空态。"""
    raw = entry.get("输入支路")
    if isinstance(raw, int):
        return max(raw, 0)
    try:
        return max(int(str(raw).strip()), 0)
    except (TypeError, ValueError):
        return 0


def _valid_readings(entry: dict[str, Any]) -> list[float]:
    """提取真实采集到的正电流读数。

    None、空串（未采集/读取失败）以及 0（离线零电流）都不参与不平衡判断，
    因为缺失通道不代表该支路电流真的为零。
    """
    readings: list[float] = []
    for value in entry.get("branch_currents") or []:
        if value is None or value == "":
            continue
        try:
            current = float(value)
        except (TypeError, ValueError):
            continue
        if current > 0:
            readings.append(current)
    return readings


def evaluate_imbalance(entry: dict[str, Any]) -> dict[str, Any]:
    """基于有效读数重算不平衡，只依赖真实采集值。

    返回数据状态与判定信息；读数不足时明确给空态，绝不误报支路异常。
    """
    channels = _channel_count(entry)
    if channels == 0:
        entry["data_state"] = DATA_STATE_PENDING
        entry["abnormal"] = False
        return {"data_state": DATA_STATE_PENDING, "reason": "未配置输入支路，待补资料"}

    readings = _valid_readings(entry)
    if not readings:
        entry["data_state"] = DATA_STATE_FAILED
        entry["abnormal"] = False
        return {"data_state": DATA_STATE_FAILED, "reason": "支路电流缺失或读取失败，零电流不参与判断"}

    raw = list(entry.get("branch_currents") or [])
    failed = sum(1 for value in raw[:channels] if value is None or value == "")
    failed += max(channels - len(raw), 0)
    if failed > 0:
        entry["data_state"] = DATA_STATE_FAILED
        entry["abnormal"] = False
        return {
            "data_state": DATA_STATE_FAILED,
            "reason": f"{failed} 路支路电流读取失败，暂不参与不平衡判断",
        }

    if len(readings) < MIN_VALID_READINGS:
        entry["data_state"] = DATA_STATE_FAILED
        entry["abnormal"] = False
        return {"data_state": DATA_STATE_FAILED, "reason": "有效正电流读数不足，无法判断不平衡"}

    average = sum(readings) / len(readings)
    deviation = max(abs(value - average) for value in readings) / average if average else 0.0
    imbalanced = deviation > IMBALANCE_RATIO
    entry["data_state"] = DATA_STATE_OK
    entry["abnormal"] = imbalanced
    return {
        "data_state": DATA_STATE_OK,
        "reason": f"平均电流 {average:.2f}A，最大偏差 {deviation:.1%}",
        "average": round(average, 3),
        "deviation": round(deviation, 4),
        "imbalanced": imbalanced,
    }


# 模拟采集源：真实项目里这层会换成采集服务/网关。
# ok=False        -> 通讯失败，电流为空（读取失败）
# currents=None   -> 通讯正常但通道电流全部读不到
# currents 含 None -> 单路电流读取失败
# channels=0      -> 设备未登记输入支路，只能待补资料
_POLL_SCENARIOS: dict[int, dict[str, Any]] = {
    1: {"ok": True, "currents": [8.12, 8.05, 8.18, 8.09], "temperature": 42.6},
    2: {"ok": True, "currents": [8.20, 5.40, 8.15, 8.08], "temperature": 44.1},
    3: {"ok": False, "error": "通讯超时，无响应"},
    4: {"ok": True, "currents": [7.95, None, 8.02, 8.10], "temperature": 41.8},
    5: {"ok": True, "currents": None, "temperature": None},
}


class DcBoxService:
    def __init__(self) -> None:
        # 最近一次采集批次；空数据（整批失败）时保持上一批信息不变
        self.last_batch: dict[str, Any] | None = None
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """首次使用时跑一轮采集，把档案数据补成带电流与判定的记录。"""
        if not self._initialized:
            self._initialized = True
            self.refresh()

    def _poll_device(self, entry: dict[str, Any]) -> dict[str, Any]:
        scenario = _POLL_SCENARIOS.get(int(entry.get("id", 0)), {"ok": False, "error": "未知设备"})
        return dict(scenario)

    def _apply_poll(self, entry: dict[str, Any], poll: dict[str, Any]) -> bool:
        """把单次采集结果落到设备记录上；返回是否拿到有效数据。

        失败路径不推进批次（批次号在 refresh 里统一处理），并清空本轮电流，
        避免把上一批旧读数误当成当前值；data_state 置为待补资料/采集失败。
        """
        channels = _channel_count(entry)
        entry["通讯状态"] = "在线" if poll.get("ok") else "中断"

        if not poll.get("ok"):
            entry["data_state"] = DATA_STATE_PENDING if channels == 0 else DATA_STATE_FAILED
            entry["采集说明"] = str(poll.get("error") or "采集失败")
            entry["status"] = "通讯中断"
            entry["pending"] = True
            # 失败本轮不提供读数：清空旧电流避免误当成当前值；
            # batch_id/collected_at 保留，标明它属于哪一旧批次
            entry["branch_currents"] = []
            entry["箱体温度"] = None
            return False

        if channels == 0:
            # 没有输入支路的设备：采集再多次也算不出电流，只提示待补资料
            entry["data_state"] = DATA_STATE_PENDING
            entry["采集说明"] = "设备未登记输入支路，待补资料后重试"
            entry["status"] = "通讯中断"
            entry["pending"] = True
            entry["branch_currents"] = []
            entry["箱体温度"] = None
            return False

        currents = poll.get("currents")
        if currents is None:
            entry["data_state"] = DATA_STATE_FAILED
            entry["采集说明"] = "通讯正常但支路电流为空，读取失败"
            entry["status"] = "通讯中断"
            entry["pending"] = True
            entry["branch_currents"] = []
            entry["箱体温度"] = None
            return False

        # 采集成功（允许个别通道读取失败）：用新读数覆盖，重算不平衡，
        # 旧批次结果在调用方统一换号
        readings = [currents[i] if i < len(currents) else None for i in range(channels)]
        entry["branch_currents"] = readings
        entry["箱体温度"] = poll.get("temperature")
        result = evaluate_imbalance(entry)
        entry["采集说明"] = result["reason"]
        # 通讯在线、电流部分可读时不算通讯中断；只在确认不平衡时转支路异常
        entry["status"] = "支路异常" if result.get("imbalanced") else "正常运行"
        entry["pending"] = result["data_state"] != DATA_STATE_OK
        return True

    def refresh(self, entry_id: int | None = None) -> dict[str, Any]:
        """触发一轮采集（entry_id 为空=整批，否则单台重试）。

        仅当至少一台设备采到有效数据才推进批次；整批失败时保留旧批次、
        旧读数与旧判定，前端继续展示已有记录与当前筛选。
        """
        targets = [store.find(MODULE, entry_id)] if entry_id is not None else store.rows(MODULE)
        targets = [row for row in targets if row is not None]

        failures: list[dict[str, str]] = []
        refreshed = 0
        for entry in targets:
            poll = self._poll_device(entry)
            if self._apply_poll(entry, poll):
                refreshed += 1
            else:
                failures.append({"汇流箱编号": str(entry.get("汇流箱编号", "")), "原因": str(entry.get("采集说明", "采集失败"))})

        if refreshed == 0:
            # 整批空数据：不生成新批次，旧结果不能被当成新批次
            return {
                "batch": self.last_batch,
                "refreshed": 0,
                "failed": len(failures),
                "failures": failures,
                "advanced": False,
            }

        batch_id = (self.last_batch or {}).get("batch_id", 0) + 1
        batch = {
            "batch_id": batch_id,
            "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "refreshed": refreshed,
            "failed": len(failures),
        }
        self.last_batch = batch
        for entry in targets:
            if entry.get("data_state") == DATA_STATE_OK:
                entry["batch_id"] = batch_id
                entry["collected_at"] = batch["collected_at"]
        return {**batch, "failures": failures, "advanced": True}

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        汇流箱编号: str | None = None,
        所属组串: str | None = None,
        输入支路: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        self._ensure_initialized()
        rows = store.rows(MODULE)

        def match(value: Any, needle: str | None) -> bool:
            return not needle or needle in str(value or "")

        rows = [
            row
            for row in rows
            if match(row.get("汇流箱编号"), keyword or 汇流箱编号)
            and match(row.get("所属组串"), 所属组串)
            and match(row.get("输入支路"), 输入支路)
            and (not status or row.get("status") == status)
        ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._serialize(row) for row in rows[start:start + size]], total

    def _serialize(self, entry: dict[str, Any]) -> dict[str, Any]:
        """给列表/导出用的扁平结构：电流与采集状态各自独立，缺失不显示成 0。"""
        channels = _channel_count(entry)
        currents = entry.get("branch_currents") or []
        data_state = entry.get("data_state") or DATA_STATE_FAILED

        if channels == 0:
            current_display = "待补资料"
        elif not currents:
            current_display = "读取失败"
        else:
            shown = [("读取失败" if value is None else f"{float(value):.2f}A") for value in currents]
            missing = channels - len(shown)
            shown.extend(["读取失败"] * max(missing, 0))
            current_display = "、".join(shown)

        return {
            "id": entry.get("id"),
            "汇流箱编号": entry.get("汇流箱编号"),
            "所属组串": entry.get("所属组串"),
            "输入支路": channels or entry.get("输入支路"),
            "支路电流": current_display,
            "箱体温度": "读取失败" if entry.get("箱体温度") is None else entry.get("箱体温度"),
            "通讯状态": entry.get("通讯状态", "未知"),
            "上次检修日": entry.get("上次检修日"),
            "箱体状态": entry.get("status"),
            "采集状态": data_state,
            "采集说明": entry.get("采集说明", ""),
            "数据批次": entry.get("batch_id"),
            "采集时间": entry.get("collected_at"),
            "status": entry.get("status"),
            "abnormal": bool(entry.get("abnormal")),
        }

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        self._ensure_initialized()
        entry = store.find(MODULE, entry_id)
        return self._serialize(entry) if entry is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        entry["通讯状态"] = "未知"
        entry["data_state"] = DATA_STATE_PENDING if _channel_count(entry) == 0 else DATA_STATE_FAILED
        entry["branch_currents"] = []
        entry["采集说明"] = "新建后尚未采集"
        rows.append(entry)
        return entry, []

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
        # abnormal 表示实测支路不平衡，只能由采集重算驱动；人工动作不清空它
        if action in NEGATIVE_ACTIONS:
            entry["abnormal"] = True
        return entry, f"汇流箱已{action}"
