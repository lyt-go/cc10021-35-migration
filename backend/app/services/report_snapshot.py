"""质量报告兼容层：把 v1 汇总列日报迁移为带来源与结构版本的 v2 快照。

设计要点（对应部署与回查约束）：

1. **存量兼容**：v1 日报只有汇总列，没有来源。迁移时逐项尝试按“日报日期”
   在评分卡 / 巡检任务 / 趋势记录里找回来源；找不回的指标保留原值并标记
   ``存量兼容缺项``，不会编造数字。
2. **同一编号多版本**：v2 快照沿用原 ``报告编号``，另带 ``结构版本``、``修订号``。
   v1 汇总行与 v2 快照可以同时被读取，报告编号始终可回查。
3. **时点隔离**：创建快照时固定 ``store.journal_seq()``，之后所有事件回查都通过
   ``store.rows_as_of(module, seq)`` 重建，绝不读取实时行。部署中断、恢复、
   回滚或事件状态刚变化，都不会把不同时点的事件内容拼进同一份报告。
4. **签收与回查清单**：签收结果按快照内的事件证据同步到质量事件回查清单。
5. **逐项重做**：已签快照不可变；重做会生成新的修订号快照，只更新指定指标，
   找不到来源的指标继续标记缺项。
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Iterable

from app.store import store

STRUCTURE_VERSION = "v2.0"
LEGACY_VERSION = "v1-汇总"
RUNTIME_VERSION = "运行时生成"

# v2 快照的指标顺序与 v1 汇总列一一对应，便于跨版本核对。
METRIC_NAMES = ["覆盖表数", "通过规则数", "失败规则数", "趋势分析"]

# 签收状态机：迁移/运行时生成 -> 待签收或复核中 -> 已签收 / 已驳回。
SIGN_STATUSES = ["待签收", "复核中", "已签收", "已驳回", "已作废"]
REVIEW_SIGNED = "已签收"
REVIEW_REJECTED = "已驳回"

_DATE_RE = re.compile(r"(\d{4})[-/.]?(\d{2})[-/.]?(\d{2})")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_day(*values: Any) -> str | None:
    """从报告周期、生成时间等字段里提取 YYYY-MM-DD。

    v1 的“报告周期”可能是 daily、中文描述或日期，取第一个可识别日期；
    都识别不出时返回 None，由调用方回退到生成时间。
    """
    for value in values:
        if value is None:
            continue
        match = _DATE_RE.search(str(value))
        if not match:
            continue
        year, month, day = match.groups()
        try:
            return datetime(int(year), int(month), int(day)).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def report_day(report: dict[str, Any]) -> str | None:
    return normalize_day(report.get("报告周期"), report.get("生成时间"), report.get("日期"))


def _row_day(row: dict[str, Any], fields: Iterable[str]) -> str | None:
    return normalize_day(*(row.get(field) for field in fields))


def _safe_int(value: Any) -> int | None:
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(float(text))
    except (TypeError, ValueError):
        return None


def _rows_on_day(module: str, day: str, seq: int, date_fields: Iterable[str]) -> list[dict[str, Any]]:
    """按快照时点取当日行；只使用 rows_as_of，避免混入实时状态。"""
    return [
        row for row in store.rows_as_of(module, seq)
        if _row_day(row, date_fields) == day
    ]


class ReportCompatService:
    """质量报告 v1 → v2 兼容、签收、回查与重做服务。"""

    def __init__(self) -> None:
        # 兼容层自己的三张附属表不放进 store 的模块表，避免污染 18 个业务模块
        # 的列表 / 概览；读写统一使用 store.lock，保证与业务行同一把锁。
        self._snapshots: list[dict[str, Any]] = []
        self._migrations: list[dict[str, Any]] = []
        self._review_items: list[dict[str, Any]] = []
        self._snapshot_seq = 0
        self._migration_seq = 0
        self._review_seq = 0

    # ------------------------------------------------------------------
    # 基础访问
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """测试或本地演示用：清空兼容层附属表，业务数据由 store.reset() 负责。"""
        with store.lock:
            self._snapshots.clear()
            self._migrations.clear()
            self._review_items.clear()
            self._snapshot_seq = self._migration_seq = self._review_seq = 0

    def _next_snapshot_id(self) -> int:
        self._snapshot_seq += 1
        return self._snapshot_seq

    def _next_migration_id(self) -> int:
        self._migration_seq += 1
        return self._migration_seq

    def _next_review_id(self) -> int:
        self._review_seq += 1
        return self._review_seq

    def _snapshot_by_id(self, snapshot_id: int) -> dict[str, Any] | None:
        for snapshot in self._snapshots:
            if int(snapshot["id"]) == int(snapshot_id):
                return snapshot
        return None

    @staticmethod
    def _visible(snapshot: dict[str, Any]) -> bool:
        return (
            snapshot.get("提交状态") == "已提交"
            and snapshot.get("签收状态") != "已作废"
        )

    def _snapshots_for_number(self, report_no: str, *, visible_only: bool = True) -> list[dict[str, Any]]:
        found = [
            snapshot for snapshot in self._snapshots
            if str(snapshot.get("报告编号")) == str(report_no)
            and (self._visible(snapshot) if visible_only else True)
        ]
        return sorted(found, key=lambda item: (int(item.get("修订号", 1)), int(item["id"])))

    def snapshots_for_entry(self, entry_id: int) -> list[dict[str, Any]]:
        """按报告行 id 找已提交快照（快照里保存的是报告编号，这里用编号反查）。"""
        with store.lock:
            report = store.find("dq_report", entry_id)
            if report is None:
                return []
            return [
                self._public_snapshot(snapshot)
                for snapshot in self._snapshots_for_number(str(report.get("报告编号")))
            ]

    # ------------------------------------------------------------------
    # 来源解析：v1 汇总列 -> v2 指标
    # ------------------------------------------------------------------
    def _resolve_legacy_metric(
        self, name: str, day: str | None, seq: int,
    ) -> tuple[str | None, dict[str, Any], bool]:
        """尝试为 v1 指标找回来源。

        返回 ``(重算值, 来源描述, 是否找到)``。找不到时来源仍写入检索范围，
        便于审计为什么是“存量兼容缺项”。
        """
        if day is None:
            return None, {"检索": "无法确定日报日期"}, False

        if name == "覆盖表数":
            rows = _rows_on_day("dq_scorecard", day, seq, ("评分时间", "日期"))
            if rows:
                ids = [row.get("记录编号") or row.get("id") for row in rows]
                return str(len(rows)), {
                    "模块": "dq_scorecard",
                    "编号": ids,
                    "字段": "评分记录数",
                    "时点": seq,
                    "日报日期": day,
                }, True
            return None, {"模块": "dq_scorecard", "检索日期": day, "时点": seq, "结果": "未找到"}, False

        if name in ("通过规则数", "失败规则数"):
            field = "通过数" if name == "通过规则数" else "失败数"
            rows = _rows_on_day(
                "dq_check_run", day, seq,
                ("执行时间", "生成时间", "日期"),
            )
            matched = [row for row in rows if _safe_int(row.get(field)) is not None]
            if matched:
                total = sum(_safe_int(row.get(field)) or 0 for row in matched)
                ids = [row.get("任务编号") or row.get("id") for row in matched]
                return str(total), {
                    "模块": "dq_check_run",
                    "编号": ids,
                    "字段": field,
                    "时点": seq,
                    "日报日期": day,
                }, True
            return None, {
                "模块": "dq_check_run",
                "检索日期": day,
                "时点": seq,
                "结果": "当日巡检记录缺少通过/失败计数",
            }, False

        if name == "趋势分析":
            rows = _rows_on_day("dq_trend", day, seq, ("日期",))
            if rows:
                latest = max(rows, key=lambda row: str(row.get("日期", "")))
                value = latest.get("趋势标记") or latest.get("status")
                ids = [row.get("记录编号") or row.get("id") for row in rows]
                return str(value or "未知"), {
                    "模块": "dq_trend",
                    "编号": ids,
                    "字段": "趋势标记",
                    "时点": seq,
                    "日报日期": day,
                }, True
            return None, {"模块": "dq_trend", "检索日期": day, "时点": seq, "结果": "未找到"}, False

        return None, {"结果": "不支持的指标"}, False

    def _build_metrics(
        self,
        report: dict[str, Any],
        day: str | None,
        seq: int,
        runtime_metrics: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        metrics: list[dict[str, Any]] = []
        for name in METRIC_NAMES:
            original = report.get(name)
            if runtime_metrics and name in runtime_metrics:
                spec = runtime_metrics[name]
                metrics.append({
                    "名称": name,
                    "原值": original,
                    "值": spec.get("值", original),
                    "重算值": spec.get("值", original),
                    "来源": spec.get("来源", {"类型": "生成时计算"}),
                    "可重做": bool(spec.get("可重做", True)),
                    "重做状态": spec.get("重做状态", "生成时计算"),
                    "是否缺项": False,
                })
                continue

            recomputed, source, resolved = self._resolve_legacy_metric(name, day, seq)
            metrics.append({
                "名称": name,
                "原值": original,
                "值": original,
                # 迁移阶段不覆盖历史值：已签数字要能逐项重做，而不是迁移时静默改写。
                "重算值": recomputed,
                "来源": source,
                "可重做": resolved,
                "重做状态": "待重做" if resolved else "存量兼容缺项",
                "是否缺项": not resolved,
                "同步记录": (
                    "v1 汇总列原值，迁移时已找到来源，可逐项重做"
                    if resolved else "v1 汇总列原值，迁移时未找到来源，标记存量兼容缺项"
                ),
            })
        return metrics

    def _evidence(self, day: str | None, seq: int) -> list[dict[str, Any]]:
        """按快照时点固定事件证据，记录当天的处置归属。"""
        if day is None:
            return []
        incidents = [
            row for row in store.rows_as_of("dq_incident", seq)
            if _row_day(row, ("触发时间", "日期")) == day
        ]
        evidence = []
        for row in incidents:
            evidence.append({
                "事件ID": row.get("id"),
                "事件编号": row.get("事件编号"),
                "触发时间": row.get("触发时间"),
                "严重级别": row.get("严重级别"),
                "当时状态": row.get("status"),
                "当时处理状态": row.get("处理状态"),
                "处置归属": row.get("处理人") or "未分配",
                "处置状态": row.get("处理状态") or row.get("status"),
                "关联规则": row.get("关联规则"),
                "来源时点": seq,
            })
        return evidence

    # ------------------------------------------------------------------
    # 快照创建
    # ------------------------------------------------------------------
    def _create_snapshot_locked(
        self,
        report: dict[str, Any],
        *,
        seq: int,
        source_version: str,
        migration_id: str | None,
        submit_status: str,
        sign_status: str,
        revision: int = 1,
        previous_snapshot: int | None = None,
        runtime_metrics: dict[str, dict[str, Any]] | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """在已持有 store.lock 的前提下创建快照，内部内容始终来自 seq 时点。"""
        snapshot_id = self._next_snapshot_id()
        day = report_day(report)
        metrics = self._build_metrics(report, day, seq, runtime_metrics)
        missing = [metric["名称"] for metric in metrics if metric["是否缺项"]]
        available = len(metrics) - len(missing)
        snapshot = {
            "id": snapshot_id,
            "快照编号": f"DQR-S-{snapshot_id:04d}",
            "报告编号": report.get("报告编号"),
            "日报日期": day,
            "结构版本": STRUCTURE_VERSION,
            "来源版本": source_version,
            "迁移编号": migration_id,
            "数据时点": seq,
            "快照时间": now_iso(),
            "提交状态": submit_status,
            "签收状态": sign_status,
            "修订号": revision,
            "前序快照": previous_snapshot,
            "指标": metrics,
            "缺项": missing,
            "缺项标记": "存量兼容缺项" if missing else "",
            "补齐率": round(available / len(metrics), 4) if metrics else 0.0,
            "完整": not missing,
            "事件回查": self._evidence(day, seq),
            "报告快照": {
                "报告编号": report.get("报告编号"),
                "日报日期": day,
                "结构版本": STRUCTURE_VERSION,
                **{metric["名称"]: metric["值"] for metric in metrics},
            },
            "签收结果": None,
            "签收人": "",
            "备注": remark,
        }
        self._snapshots.append(snapshot)
        return snapshot

    def _migration_sign_status(self, report: dict[str, Any]) -> str:
        """存量报告旧状态决定快照初始签收状态：

        - 旧日报是“已完成”时视为正在复核，继任评审要签字后才能归档；
        - 旧日报是“已归档”时视为存量已签收，迁移提交时同步回查清单；
        - 其余状态（草稿 / 生成中）保持待签收。
        """
        if report.get("status") == "已归档":
            return "已签收"
        if report.get("status") == "已完成":
            return "复核中"
        return "待签收"

    def _create_and_commit_locked(
        self,
        report: dict[str, Any],
        *,
        migration: dict[str, Any],
        remark: str,
    ) -> dict[str, Any]:
        """创建一份存量快照并按旧报告状态提交。

        逐份提交（而不是整批一个提交点）是部署兼容的关键：
        部署在任意一份之间中断，已经提交的日报可读 v2，
        没轮到的日报继续按原编号回退 v1，读取适配器不会看到半份快照。
        """
        snapshot = self._create_snapshot_locked(
            report,
            seq=int(migration["数据时点"]),
            source_version=LEGACY_VERSION,
            migration_id=migration["迁移编号"],
            submit_status="暂存",
            sign_status=self._migration_sign_status(report),
            remark=remark,
        )
        snapshot["提交状态"] = "已提交"
        if snapshot["签收状态"] == "已签收":
            self._sync_review_locked(
                snapshot,
                result="存量已签收",
                reviewer="迁移兼容",
                remark="迁移时按旧报告已归档状态视为已签收",
            )
        return snapshot

    def create_runtime_snapshot(
        self,
        report: dict[str, Any],
        *,
        seq: int,
        runtime_metrics: dict[str, dict[str, Any]] | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """新格式部署后由业务服务调用：报告落库时同步生成 v2 快照。

        指标值由生成方直接给出，并携带来源引用；事件证据仍按传入 seq 固定。
        """
        with store.lock:
            return self._create_snapshot_locked(
                report,
                seq=seq,
                source_version=RUNTIME_VERSION,
                migration_id=None,
                submit_status="已提交",
                sign_status="待签收",
                runtime_metrics=runtime_metrics,
                remark=remark,
            )

    # ------------------------------------------------------------------
    # 存量迁移 / 中断恢复 / 回滚
    # ------------------------------------------------------------------
    def _legacy_reports(self, report_numbers: list[str] | None = None) -> list[dict[str, Any]]:
        rows = store.rows("dq_report")
        targets = []
        for row in rows:
            if str(row.get("结构版本", "")).startswith("v2"):
                continue
            if report_numbers is not None and str(row.get("报告编号")) not in report_numbers:
                continue
            targets.append(row)
        return targets

    def _migration_by_id(self, migration_id: str) -> dict[str, Any] | None:
        for migration in self._migrations:
            if migration.get("迁移编号") == migration_id:
                return migration
        return None

    def _migration_detail(self, migration: dict[str, Any]) -> dict[str, Any]:
        staged = [
            self._public_snapshot(snapshot)
            for snapshot in self._snapshots
            if snapshot.get("迁移编号") == migration["迁移编号"]
        ]
        numbers = set(migration.get("报告编号") or [])
        readable = [
            self._public_snapshot(snapshot)
            for snapshot in self._snapshots
            if snapshot.get("报告编号") in numbers and self._visible(snapshot)
        ]
        detail = deepcopy(migration)
        detail["快照"] = staged
        detail["已提交快照数"] = sum(1 for item in staged if item["提交状态"] == "已提交")
        detail["暂存快照数"] = sum(1 for item in staged if item["提交状态"] == "暂存")
        detail["回滚快照数"] = sum(1 for item in staged if item["提交状态"] == "已回滚")
        # 目标编号当前实际可读的 v2 快照（包含之前迁移已提交的部分），
        # 用于判断“迁移完成后到底有哪些报告能取得 v2”。
        detail["可读快照数"] = len(readable)
        return detail

    def start_migration(
        self,
        report_numbers: list[str] | None = None,
        *,
        stop_after: int | None = None,
        timepoint: int | None = None,
    ) -> dict[str, Any]:
        """启动存量迁移；``stop_after`` 用于模拟部署中断。

        整个迁移使用同一个 ``数据时点``：即使恢复发生在很久以后，也不会
        把恢复时刻的事件状态拼进旧日报。暂存快照在提交前对读取适配器不可见。
        """
        with store.lock:
            targets = self._legacy_reports(report_numbers)
            seq = store.journal_seq() if timepoint is None else int(timepoint)
            migration_id = f"DQR-M-{self._next_migration_id():04d}"
            migration = {
                "id": self._migration_seq,
                "迁移编号": migration_id,
                "状态": "进行中",
                "数据时点": seq,
                "开始时间": now_iso(),
                "更新时间": now_iso(),
                "游标": 0,
                "目标数": len(targets),
                "stop_after": stop_after,
                "报告编号": [str(row.get("报告编号")) for row in targets],
                "回滚时间": None,
                "说明": "v1 汇总列迁移到 v2 报告快照",
            }
            self._migrations.append(migration)

            created: list[dict[str, Any]] = []
            for index, report in enumerate(targets):
                existing = [
                    snapshot for snapshot in self._snapshots_for_number(
                        str(report.get("报告编号")), visible_only=False)
                    if snapshot.get("来源版本") == LEGACY_VERSION
                    and snapshot.get("提交状态") != "已回滚"
                ]
                if existing:
                    migration["游标"] = index + 1
                    continue
                snapshot = self._create_and_commit_locked(
                    report,
                    migration=migration,
                    remark=f"由迁移 {migration_id} 从 {report.get('报告编号')} 的 v1 汇总列生成",
                )
                created.append(snapshot)
                migration["游标"] = index + 1
                migration["更新时间"] = now_iso()
                if stop_after is not None and len(created) >= stop_after:
                    migration["状态"] = "已中断"
                    migration["说明"] = (
                        f"部署中断：已提交 {len(created)}/{len(targets)} 份快照，"
                        "已提交部分可读 v2，其余继续按原报告编号回退 v1"
                    )
                    return self._migration_detail(migration)

            return self._finish_migration_locked(migration)

    def _finish_migration_locked(self, migration: dict[str, Any]) -> dict[str, Any]:
        migration["状态"] = "已完成"
        migration["更新时间"] = now_iso()
        migration["说明"] = "迁移完成，v2 快照逐份提交，可使用原报告编号回查"
        return self._migration_detail(migration)

    def resume_migration(self, migration_id: str) -> tuple[dict[str, Any] | None, str]:
        """恢复中断的迁移：沿用原数据时点，逐份提交剩余快照。"""
        with store.lock:
            migration = self._migration_by_id(migration_id)
            if migration is None:
                return None, f"迁移 {migration_id} 不存在"
            if migration["状态"] == "已完成":
                return self._migration_detail(migration), "迁移已完成，无需恢复"
            if migration["状态"] == "已回滚":
                return self._migration_detail(migration), "迁移已回滚，不能恢复，请重新发起"

            targets = self._legacy_reports(migration.get("报告编号"))
            for index, report in enumerate(targets):
                if index < int(migration["游标"]):
                    continue
                existing = [
                    snapshot for snapshot in self._snapshots_for_number(
                        str(report.get("报告编号")), visible_only=False)
                    if snapshot.get("迁移编号") == migration_id
                    and snapshot.get("提交状态") != "已回滚"
                ]
                if not existing:
                    self._create_and_commit_locked(
                        report,
                        migration=migration,
                        remark=f"恢复迁移 {migration_id}，沿用原数据时点 {migration['数据时点']}",
                    )
                migration["游标"] = index + 1
                migration["更新时间"] = now_iso()

            return self._finish_migration_locked(migration), "迁移已恢复并提交"

    def rollback_migration(self, migration_id: str) -> tuple[dict[str, Any] | None, str]:
        """回滚部署：作废本次迁移产生的所有快照，读取回退到 v1。"""
        with store.lock:
            migration = self._migration_by_id(migration_id)
            if migration is None:
                return None, f"迁移 {migration_id} 不存在"
            if migration["状态"] == "已回滚":
                return self._migration_detail(migration), "迁移已回滚"

            snapshots = [
                snapshot for snapshot in self._snapshots
                if snapshot.get("迁移编号") == migration_id
            ]
            if any(snapshot.get("签收状态") == "已签收" for snapshot in snapshots):
                return self._migration_detail(migration), "存在已签收快照，禁止回滚，请先走报告作废流程"

            for snapshot in snapshots:
                snapshot["提交状态"] = "已回滚"
                snapshot["签收状态"] = "已作废"
            # 回滚同步撤销由本次迁移写入的回查清单条目，避免清单指向已作废快照。
            self._review_items = [
                item for item in self._review_items
                if item.get("迁移编号") != migration_id
            ]
            migration["状态"] = "已回滚"
            migration["回滚时间"] = now_iso()
            migration["更新时间"] = now_iso()
            migration["说明"] = "部署回滚：v2 暂存/已提交快照已作废，读取适配器回退 v1 汇总"
            return self._migration_detail(migration), "迁移已回滚"

    def list_migrations(self) -> list[dict[str, Any]]:
        with store.lock:
            return [self._migration_detail(migration) for migration in self._migrations]

    # ------------------------------------------------------------------
    # 跨版本读取
    # ------------------------------------------------------------------
    def _public_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return deepcopy(snapshot)

    @staticmethod
    def _legacy_report(report_no: str) -> dict[str, Any] | None:
        matches = [
            row for row in store.rows("dq_report")
            if str(row.get("报告编号")) == str(report_no)
        ]
        if not matches:
            return None
        return matches[-1]

    def list_snapshots(
        self,
        *,
        report_no: str | None = None,
        day: str | None = None,
        structure_version: str | None = None,
        sign_status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = [
                self._public_snapshot(snapshot)
                for snapshot in self._snapshots
                if self._visible(snapshot)
            ]
        if report_no:
            rows = [row for row in rows if str(row.get("报告编号")) == report_no]
        if day:
            rows = [row for row in rows if row.get("日报日期") == day]
        if structure_version:
            rows = [row for row in rows if row.get("结构版本") == structure_version]
        if sign_status:
            rows = [row for row in rows if row.get("签收状态") == sign_status]
        rows.sort(key=lambda item: (str(item.get("日报日期")), str(item.get("报告编号")), int(item["id"])))
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        with store.lock:
            snapshot = self._snapshot_by_id(snapshot_id)
            if snapshot is None or snapshot.get("提交状态") == "已回滚":
                return None
            return self._public_snapshot(snapshot)

    def versions_for_number(self, report_no: str) -> dict[str, Any]:
        """按报告编号回查全部版本：v1 汇总行 + v2 快照，并给出实际可取得报告。"""
        with store.lock:
            legacy = self._legacy_report(report_no)
            snapshots = [
                self._public_snapshot(snapshot)
                for snapshot in self._snapshots_for_number(report_no)
            ]
            effective = self._effective_locked(report_no)
        history: list[dict[str, Any]] = []
        if legacy is not None:
            history.append({
                "结构版本": "v1",
                "类型": "存量汇总行",
                "报告编号": legacy.get("报告编号"),
                "日报日期": report_day(legacy),
                "报告": deepcopy(legacy),
                "可回查": True,
            })
        for snapshot in snapshots:
            history.append({
                "结构版本": snapshot["结构版本"],
                "类型": "报告快照",
                "快照编号": snapshot["快照编号"],
                "修订号": snapshot["修订号"],
                "报告编号": snapshot["报告编号"],
                "日报日期": snapshot["日报日期"],
                "签收状态": snapshot["签收状态"],
                "补齐率": snapshot["补齐率"],
                "缺项": snapshot["缺项"],
                "可回查": True,
            })
        return {
            "报告编号": report_no,
            "原编号核对": legacy is not None and str(legacy.get("报告编号")) == str(report_no),
            "版本数": len(history),
            "历史": history,
            "最新有效": effective,
            "说明": "同一报告编号的 v1 汇总行与 v2 快照可同时读取，编号不变",
        }

    def _effective_locked(self, report_no: str) -> dict[str, Any]:
        snapshots = self._snapshots_for_number(report_no)
        legacy = self._legacy_report(report_no)
        if not snapshots:
            if legacy is None:
                return {
                    "报告编号": report_no,
                    "来源结构": "不存在",
                    "可取得": False,
                    "说明": "该报告编号没有 v1 汇总行，也没有 v2 快照",
                    "缺项": METRIC_NAMES,
                    "补齐率": 0.0,
                }
            return {
                "报告编号": report_no,
                "来源结构": "v1",
                "可取得": True,
                "日报日期": report_day(legacy),
                "报告": {name: legacy.get(name) for name in METRIC_NAMES},
                "报告版本": legacy.get("报告版本") or "v1",
                "补齐率": 0.0,
                "缺项": METRIC_NAMES,
                "缺项标记": "存量兼容缺项",
                "说明": "尚无已提交的 v2 快照，按存量兼容返回 v1 汇总行",
                "事件回查": [],
            }

        latest = snapshots[-1]
        legacy_values = legacy or {}
        merged: dict[str, Any] = {}
        fields: dict[str, Any] = {}
        fallback: list[str] = []
        for metric in latest["指标"]:
            name = metric["名称"]
            if metric["是否缺项"]:
                merged[name] = legacy_values.get(name)
                fallback.append(name)
                fields[name] = {
                    "结构版本": "v1",
                    "回退": True,
                    "值": legacy_values.get(name),
                    "说明": "v2 快照该指标为存量兼容缺项，按原编号回退到 v1 汇总列",
                }
            else:
                merged[name] = metric["值"]
                fields[name] = {
                    "结构版本": STRUCTURE_VERSION,
                    "回退": False,
                    "值": metric["值"],
                    "重做状态": metric["重做状态"],
                }
        return {
            "报告编号": report_no,
            "来源结构": STRUCTURE_VERSION,
            "可取得": True,
            "日报日期": latest.get("日报日期"),
            "快照编号": latest["快照编号"],
            "修订号": latest["修订号"],
            "签收状态": latest["签收状态"],
            "报告": merged,
            "字段来源": fields,
            "报告版本": STRUCTURE_VERSION,
            "补齐率": latest["补齐率"],
            "缺项": latest["缺项"],
            "缺项标记": latest.get("缺项标记", ""),
            "回退项": fallback,
            "完整": not latest["缺项"],
            "事件回查": deepcopy(latest["事件回查"]),
            "说明": (
                "v2 快照可直接取得；缺项按原报告编号回退到 v1 汇总列，"
                "事件内容只来自快照固定时点，不使用实时事件状态"
            ),
        }

    def effective_report(self, report_no: str) -> dict[str, Any]:
        with store.lock:
            return self._effective_locked(report_no)

    def _legacy_missing_sources_locked(self, day: str) -> list[dict[str, Any]]:
        """找出当天“当时缺少来源记录”的旧日报。

        两种情况都算：完全没有 v2 快照（只能按 v1 取得），
        或者已有 v2 快照但有指标缺项（该指标回退到 v1 汇总列）。
        """
        missing: list[dict[str, Any]] = []
        for row in store.rows("dq_report"):
            if str(row.get("结构版本", "")).startswith("v2"):
                continue
            if report_day(row) != day:
                continue
            report_no = str(row.get("报告编号"))
            snapshots = self._snapshots_for_number(report_no)
            if not snapshots:
                missing.append({
                    "报告编号": report_no,
                    "日报日期": day,
                    "原因": "当时缺少来源记录，仅有 v1 汇总列",
                    "缺项": list(METRIC_NAMES),
                    "快照编号": None,
                    "缺项标记": "存量兼容缺项",
                })
                continue
            latest = snapshots[-1]
            if latest.get("缺项"):
                missing.append({
                    "报告编号": report_no,
                    "日报日期": day,
                    "原因": "v2 快照中部分指标未找到来源，按原编号回退 v1 汇总列",
                    "缺项": list(latest["缺项"]),
                    "快照编号": latest["快照编号"],
                    "缺项标记": "存量兼容缺项",
                })
        return missing

    def daily_view(self, day: str) -> dict[str, Any]:
        """事件侧按天查看：v2 快照、v1 旧日报、缺来源旧日报和当天处置归属。"""
        with store.lock:
            snapshots = [
                self._public_snapshot(snapshot)
                for snapshot in self._snapshots
                if self._visible(snapshot) and snapshot.get("日报日期") == day
            ]
            legacy_rows = [
                deepcopy(row) for row in store.rows("dq_report")
                if not str(row.get("结构版本", "")).startswith("v2")
                and report_day(row) == day
            ]
            missing_legacy = self._legacy_missing_sources_locked(day)
        attribution: dict[str, dict[str, Any]] = {}
        for snapshot in snapshots:
            for item in snapshot.get("事件回查", []):
                owner = item.get("处置归属") or "未分配"
                bucket = attribution.setdefault(owner, {
                    "处置归属": owner,
                    "事件数": 0,
                    "当时状态": {},
                    "快照": [],
                })
                bucket["事件数"] += 1
                status = str(item.get("当时状态"))
                bucket["当时状态"][status] = bucket["当时状态"].get(status, 0) + 1
                if snapshot["快照编号"] not in bucket["快照"]:
                    bucket["快照"].append(snapshot["快照编号"])
        return {
            "日期": day,
            "v2快照": snapshots,
            "v1旧日报": legacy_rows,
            "事件处置归属": list(attribution.values()),
            "缺少来源记录的旧日报": missing_legacy,
        }

    # ------------------------------------------------------------------
    # 逐项重做
    # ------------------------------------------------------------------
    def recompute_snapshot(
        self,
        snapshot_id: int,
        metrics: list[str] | None = None,
        *,
        reviewer: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """逐项重做：生成新修订快照，不修改原快照（含已签收快照）。"""
        with store.lock:
            original = self._snapshot_by_id(snapshot_id)
            if original is None or original.get("提交状态") != "已提交":
                return None, f"快照 {snapshot_id} 不存在或尚未提交，不能重做"
            target_names = metrics or [metric["名称"] for metric in original["指标"]]
            unknown = [name for name in target_names if name not in METRIC_NAMES]
            if unknown:
                return None, f"未知指标：{'、'.join(unknown)}"

            seq = int(original["数据时点"])
            day = original.get("日报日期")
            new_metrics: list[dict[str, Any]] = []
            redone: list[dict[str, Any]] = []
            for metric in deepcopy(original["指标"]):
                name = metric["名称"]
                if name not in target_names:
                    new_metrics.append(metric)
                    continue
                recomputed, source, resolved = self._resolve_legacy_metric(name, day, seq)
                if resolved:
                    metric["值"] = recomputed
                    metric["重算值"] = recomputed
                    metric["来源"] = source
                    metric["可重做"] = True
                    metric["重做状态"] = "已重做"
                    metric["是否缺项"] = False
                    metric["同步记录"] = "按快照固定时点重新计算"
                    redone.append({"指标": name, "结果": "已重做", "值": recomputed})
                else:
                    metric["可重做"] = False
                    metric["重做状态"] = "存量兼容缺项"
                    metric["是否缺项"] = True
                    metric["同步记录"] = "重做时未找到来源，保留原值并标记存量兼容缺项"
                    redone.append({"指标": name, "结果": "存量兼容缺项", "值": metric["值"]})
                new_metrics.append(metric)

            missing = [metric["名称"] for metric in new_metrics if metric["是否缺项"]]
            snapshot = {
                "id": self._next_snapshot_id(),
                "快照编号": f"DQR-S-{self._snapshot_seq:04d}",
                "报告编号": original["报告编号"],
                "日报日期": day,
                "结构版本": STRUCTURE_VERSION,
                "来源版本": original["来源版本"],
                "迁移编号": original.get("迁移编号"),
                "数据时点": seq,
                "快照时间": now_iso(),
                "提交状态": "已提交",
                "签收状态": "待签收",
                "修订号": int(original.get("修订号", 1)) + 1,
                "前序快照": original["id"],
                "指标": new_metrics,
                "缺项": missing,
                "缺项标记": "存量兼容缺项" if missing else "",
                "补齐率": round((len(new_metrics) - len(missing)) / len(new_metrics), 4),
                "完整": not missing,
                "事件回查": self._evidence(day, seq),
                "报告快照": {
                    "报告编号": original["报告编号"],
                    "日报日期": day,
                    "结构版本": STRUCTURE_VERSION,
                    **{metric["名称"]: metric["值"] for metric in new_metrics},
                },
                "签收结果": None,
                "签收人": "",
                "备注": f"由 {original['快照编号']} 第 {original.get('修订号', 1)} 版逐项重做",
                "重做明细": redone,
                "重做人": reviewer,
            }
            self._snapshots.append(snapshot)
            return self._public_snapshot(snapshot), f"已生成修订 {snapshot['修订号']}"

    # ------------------------------------------------------------------
    # 签收与事件回查清单
    # ------------------------------------------------------------------
    def _sync_review_locked(
        self,
        snapshot: dict[str, Any],
        *,
        result: str,
        reviewer: str,
        remark: str,
    ) -> list[dict[str, Any]]:
        synced: list[dict[str, Any]] = []
        for evidence in snapshot.get("事件回查", []):
            event_id = evidence.get("事件ID")
            existing = next((
                item for item in self._review_items
                if item.get("快照编号") == snapshot["快照编号"]
                and item.get("事件ID") == event_id
            ), None)
            live = store.find("dq_incident", int(event_id)) if event_id is not None else None
            current_status = live.get("status") if live else "事件不存在"
            payload = {
                "清单编号": existing["清单编号"] if existing else f"DQI-R-{self._next_review_id():04d}",
                "事件ID": event_id,
                "事件编号": evidence.get("事件编号"),
                "报告编号": snapshot["报告编号"],
                "快照编号": snapshot["快照编号"],
                "结构版本": snapshot["结构版本"],
                "日报日期": snapshot.get("日报日期"),
                "确认结果": result,
                "确认人": reviewer or "未指定",
                "确认时间": now_iso(),
                "当时状态": evidence.get("当时状态"),
                "当时处置归属": evidence.get("处置归属"),
                "当前状态": current_status,
                "状态已变化": current_status != evidence.get("当时状态"),
                "关联规则": evidence.get("关联规则"),
                "迁移编号": snapshot.get("迁移编号"),
                "备注": remark,
            }
            if existing:
                existing.update(payload)
                synced.append(deepcopy(existing))
            else:
                self._review_items.append(payload)
                synced.append(deepcopy(payload))
        return synced

    def sign_snapshot(
        self,
        snapshot_id: int,
        *,
        result: str = "通过",
        reviewer: str = "",
        remark: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """签收 / 驳回报告快照，并把确认结果同步到事件回查清单。"""
        with store.lock:
            snapshot = self._snapshot_by_id(snapshot_id)
            if snapshot is None or snapshot.get("提交状态") == "已回滚":
                return None, f"快照 {snapshot_id} 不存在或已作废"
            if snapshot.get("提交状态") != "已提交":
                return None, f"快照 {snapshot_id} 尚未提交，不能签收"
            if snapshot.get("签收状态") == "已签收":
                return None, f"快照 {snapshot_id} 已签收，如需调整请逐项重做后重新签收"
            if snapshot.get("签收状态") == "已作废":
                return None, f"快照 {snapshot_id} 已作废，不能签收"

            normalized = "通过" if result in ("通过", "已确认", "确认", "approve") else "驳回"
            snapshot["签收状态"] = REVIEW_SIGNED if normalized == "通过" else REVIEW_REJECTED
            snapshot["签收结果"] = {
                "结果": normalized,
                "签收人": reviewer or "未指定",
                "签收时间": now_iso(),
                "备注": remark,
            }
            snapshot["签收人"] = reviewer or "未指定"
            synced = self._sync_review_locked(
                snapshot,
                result="报告已签收" if normalized == "通过" else "报告已驳回",
                reviewer=reviewer or "未指定",
                remark=remark,
            )
            return self._public_snapshot(snapshot) | {"回查清单": synced}, (
                f"报告快照已{('签收' if normalized == '通过' else '驳回')}，"
                f"同步 {len(synced)} 条事件回查记录"
            )

    def review_items(
        self,
        *,
        incident_no: str | None = None,
        report_no: str | None = None,
        result: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        with store.lock:
            rows = [deepcopy(item) for item in self._review_items]
        if incident_no:
            rows = [row for row in rows if str(row.get("事件编号")) == incident_no]
        if report_no:
            rows = [row for row in rows if str(row.get("报告编号")) == report_no]
        if result:
            rows = [row for row in rows if row.get("确认结果") == result]
        rows.sort(key=lambda item: (str(item.get("日报日期") or ""), int(item.get("事件ID") or 0)))
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    # ------------------------------------------------------------------
    # 事件侧回查
    # ------------------------------------------------------------------
    def incident_evidence(self, incident_id: int) -> dict[str, Any]:
        """从 v2 快照定位事件及当天处置归属；无快照时返回缺来源的旧日报。"""
        with store.lock:
            live = store.find("dq_incident", incident_id)
            if live is None:
                return {
                    "事件ID": incident_id,
                    "可回查": False,
                    "说明": "质量事件不存在",
                    "快照证据": [],
                    "旧日报": [],
                }
            evidence: list[dict[str, Any]] = []
            for snapshot in self._snapshots:
                if not self._visible(snapshot):
                    continue
                for item in snapshot.get("事件回查", []):
                    if int(item.get("事件ID") or 0) == int(incident_id):
                        evidence.append({
                            "快照编号": snapshot["快照编号"],
                            "报告编号": snapshot["报告编号"],
                            "日报日期": snapshot.get("日报日期"),
                            "结构版本": snapshot["结构版本"],
                            "签收状态": snapshot.get("签收状态"),
                            "数据时点": snapshot.get("数据时点"),
                            "证据": deepcopy(item),
                            "当前状态": live.get("status"),
                            "状态已变化": item.get("当时状态") != live.get("status"),
                        })
            day = _row_day(live, ("触发时间", "日期"))
            covered_numbers = {item["报告编号"] for item in evidence}
            legacy_rows = [
                deepcopy(row) for row in store.rows("dq_report")
                if not str(row.get("结构版本", "")).startswith("v2")
                and report_day(row) == day
                and str(row.get("报告编号")) not in covered_numbers
            ]
            review, _ = self.review_items(incident_no=str(live.get("事件编号")), size=1000)
        return {
            "事件ID": incident_id,
            "事件编号": live.get("事件编号"),
            "触发日期": day,
            "当前状态": live.get("status"),
            "当前处置归属": live.get("处理人"),
            "快照证据": evidence,
            "回查清单": review,
            "旧日报": [
                {
                    "报告编号": row.get("报告编号"),
                    "日报日期": report_day(row),
                    "报告版本": row.get("报告版本") or "v1",
                    "原因": "当时缺少来源记录，只能按 v1 汇总列回查",
                    "缺项标记": "存量兼容缺项",
                }
                for row in legacy_rows
            ],
            "可回查": bool(evidence or legacy_rows),
            "说明": "快照证据固定为快照数据时点的事件状态，不与实时状态拼接",
        }

    # ------------------------------------------------------------------
    # v1 导出文件跨版本读取
    # ------------------------------------------------------------------
    @staticmethod
    def ensure_fixture_sources() -> int:
        """为演示用的 v1 导出补齐它当年依赖的来源行（幂等）。

        真实的存量库本来就保存着这些评分卡/巡检/趋势记录；本地演示库只有
        示例行，所以由样例端点调用这里补上，避免跨版本核对永远显示缺项。
        """
        from app.seed import V1_SOURCE_ROWS

        inserted = 0
        with store.lock:
            for module, rows in V1_SOURCE_ROWS.items():
                target = store.rows(module)
                existing = {int(row.get("id", 0)) for row in target}
                for row in rows:
                    if int(row["id"]) not in existing:
                        store.append(module, dict(row))
                        inserted += 1
        return inserted

    @staticmethod
    def parse_v1_export(csv_text: str) -> list[dict[str, Any]]:
        import csv
        import io

        if not csv_text or not csv_text.strip():
            raise ValueError("v1 导出内容为空")
        text = csv_text.lstrip("﻿")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            raise ValueError("v1 导出缺少表头")
        required = {"报告编号", "报告周期", "覆盖表数", "通过规则数", "失败规则数", "趋势分析", "生成时间", "报告版本"}
        missing = required - set(reader.fieldnames)
        if missing:
            raise ValueError(f"v1 导出缺少列：{'、'.join(sorted(missing))}")
        rows = []
        for raw in reader:
            if not str(raw.get("报告编号") or "").strip():
                continue
            rows.append({key: (value.strip() if isinstance(value, str) else value) for key, value in raw.items()})
        if not rows:
            raise ValueError("v1 导出没有有效数据行")
        return rows

    def import_v1_export(
        self,
        csv_text: str,
        *,
        stop_after: int | None = None,
    ) -> dict[str, Any]:
        """导入一份已经导出的 v1 日报，并按原编号做跨版本读取核对。

        ``stop_after`` 可模拟“新格式只补齐一半”：只有已提交的快照可见，
        未迁移的报告按原编号回退 v1。
        """
        parsed = self.parse_v1_export(csv_text)
        imported: list[dict[str, Any]] = []
        with store.lock:
            existing_numbers = {
                str(row.get("报告编号")) for row in store.rows("dq_report")
            }
            for row in parsed:
                report_no = str(row["报告编号"])
                if report_no in existing_numbers:
                    imported.append({
                        "报告编号": report_no,
                        "导入": False,
                        "原因": "报告编号已存在，按原记录回查，不重复登记",
                    })
                    continue
                rows = store.rows("dq_report")
                new_id = max((int(item.get("id", 0)) for item in rows), default=0) + 1
                legacy = {
                    "id": new_id,
                    "报告编号": report_no,
                    "报告周期": row.get("报告周期") or "",
                    "覆盖表数": row.get("覆盖表数") or "",
                    "通过规则数": row.get("通过规则数") or "",
                    "失败规则数": row.get("失败规则数") or "",
                    "趋势分析": row.get("趋势分析") or "",
                    "生成时间": row.get("生成时间") or "",
                    "报告版本": row.get("报告版本") or "v1.0",
                    "status": row.get("status") or "已完成",
                    "pending": False,
                    "abnormal": False,
                    "导入来源": "v1-导出",
                }
                store.append("dq_report", legacy)
                existing_numbers.add(report_no)
                imported.append({
                    "报告编号": report_no,
                    "导入": True,
                    "日报日期": report_day(legacy),
                    "记录": deepcopy(legacy),
                })

        migration = self.start_migration(
            [item["报告编号"] for item in imported],
            stop_after=stop_after,
            # 不显式传时点：start_migration 在持锁时取 journal_seq，
            # 此时刚导入的 v1 行已在流水里，后续事件不会被拼进来。
        )
        numbers = [item["报告编号"] for item in imported]
        cross_version = []
        for report_no in numbers:
            evidence = []
            snapshots, _ = self.list_snapshots(report_no=report_no, size=1000)
            for snapshot in snapshots:
                evidence.extend(snapshot.get("事件回查", []))
            cross_version.append({
                "报告编号": report_no,
                "v1原行": next(
                    (item.get("记录") for item in imported if item["报告编号"] == report_no),
                    None,
                ),
                "v2快照": [
                    {
                        "快照编号": snapshot["快照编号"],
                        "结构版本": snapshot["结构版本"],
                        "修订号": snapshot["修订号"],
                        "补齐率": snapshot["补齐率"],
                        "缺项": snapshot["缺项"],
                        "签收状态": snapshot["签收状态"],
                    }
                    for snapshot in snapshots
                ],
                "事件回查证据": evidence,
                "实际取得报告": self.effective_report(report_no),
            })
        return {
            "原编号": numbers,
            "导入": imported,
            "迁移": migration,
            "跨版本读取": cross_version,
            "说明": (
                "v1 导出按原报告编号跨版本读取；"
                "已提交 v2 快照直接取得，未迁移/缺项部分回退 v1 汇总列；"
                "事件证据只来自快照固定时点"
            ),
        }

    # ------------------------------------------------------------------
    # 能力说明
    # ------------------------------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        return {
            "结构版本": [STRUCTURE_VERSION],
            "兼容结构版本": ["v1"],
            "指标映射": {
                "覆盖表数": "dq_scorecard 当日评分记录数",
                "通过规则数": "dq_check_run 当日通过数之和",
                "失败规则数": "dq_check_run 当日失败数之和",
                "趋势分析": "dq_trend 当日最新趋势标记",
            },
            "缺项标记": "存量兼容缺项",
            "时点隔离": "快照创建时固定 store.journal_seq()，回查通过 rows_as_of 重建",
            "签收状态": SIGN_STATUSES,
            "端点": [
                "GET /api/report/capabilities",
                "POST /api/report/migrate",
                "GET /api/report/migrations",
                "POST /api/report/migrations/{迁移编号}/resume",
                "POST /api/report/migrations/{迁移编号}/rollback",
                "GET /api/report/snapshots",
                "GET /api/report/snapshots/{id}",
                "GET /api/report/numbers/{报告编号}",
                "GET /api/report/numbers/{报告编号}/effective",
                "POST /api/report/snapshots/{id}/recompute",
                "POST /api/report/snapshots/{id}/sign",
                "GET /api/report/incident-review",
                "GET /api/report/incidents/{id}/evidence",
                "GET /api/report/daily/{日期}",
                "GET /api/report/fixtures/v1-export",
                "POST /api/report/import/v1",
            ],
        }


report_compat = ReportCompatService()
