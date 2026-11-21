"""质量报告服务：聚合巡检和质量事件数据，生成多维度质量分析报告。

domain_chain: 质量报告 (dq_report)
  └─ 聚合 → 质量趋势 (dq_trend)
  └─ 导出 → 告警通知 (dq_alert_channel)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.report_snapshot import report_compat
from app.store import store


class DqReportService(BaseModuleService):
    MODULE = "dq_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["报告编号", "报告周期", "覆盖表数", "通过规则数", "失败规则数", "趋势分析", "生成时间", "报告版本"]
    REQUIRED_FIELDS = ["报告编号", "报告周期", "覆盖表数"]
    STATUS_ORDER = ["草稿", "生成中", "已完成", "已归档"]
    ACTION_RULES = {"生成报告": "生成中", "确认完成": "已完成", "归档报告": "已归档"}
    NEGATIVE_ACTIONS = []

    def aggregate_quality_report(self, period: str = "daily") -> dict[str, Any]:
        """聚合全量巡检结果和质量事件，生成多维度质量分析报告。

        查询巡检执行记录、质量事件、评分卡和趋势数据，
        聚合各维度指标计算结果，写入质量报告记录并导出趋势快照。
        """
        # 查询各模块数据
        incident_rows = store.rows("dq_incident")
        check_rows = store.rows("dq_check_run")
        scorecard_rows = store.rows("dq_scorecard")
        trend_rows = store.rows("dq_trend")

        # 聚合巡检结果
        total_checks = len(check_rows)
        passed_checks = sum(1 for r in check_rows if not r.get("abnormal"))
        failed_checks = total_checks - passed_checks

        # 聚合质量事件
        total_incidents = len(incident_rows)
        open_incidents = sum(1 for r in incident_rows if r.get("status") in ("待确认", "调查中"))
        resolved_incidents = sum(1 for r in incident_rows if r.get("status") in ("已修复", "已关闭"))

        # 计算质量趋势
        quality_trends = [t for t in trend_rows]
        avg_quality = 0
        quality_values = []
        for t in quality_trends:
            try:
                quality_values.append(float(t.get("综合质量", "0")))
            except (ValueError, TypeError):
                pass
        if quality_values:
            avg_quality = sum(quality_values) / len(quality_values)

        # 写入质量报告记录。聚合与快照在同一把锁内完成：
        # 快照时点取写入之后的 journal_seq，生成这份报告的输入就是快照内容。
        with store.lock:
            rows = store.rows(self.MODULE)
            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            report = {
                "id": new_id,
                "报告编号": f"DQ_R-{new_id:04d}",
                "报告周期": period,
                "覆盖表数": str(len(scorecard_rows)),
                "通过规则数": str(passed_checks),
                "失败规则数": str(failed_checks),
                "趋势分析": "上升" if avg_quality >= 85 else "平稳" if avg_quality >= 70 else "下降",
                "生成时间": datetime.now(timezone.utc).isoformat(),
                "报告版本": "v2.0",
                "结构版本": "v2.0",
                "status": "已完成",
                "pending": False,
                "abnormal": failed_checks > passed_checks,
            }
            store.append(self.MODULE, report)
            snapshot = report_compat.create_runtime_snapshot(
                report,
                seq=store.journal_seq(),
                runtime_metrics={
                    "覆盖表数": {
                        "值": report["覆盖表数"],
                        "来源": {"类型": "聚合计算", "模块": "dq_scorecard", "字段": "评分记录数"},
                    },
                    "通过规则数": {
                        "值": report["通过规则数"],
                        "来源": {"类型": "聚合计算", "模块": "dq_check_run", "字段": "通过数之和"},
                    },
                    "失败规则数": {
                        "值": report["失败规则数"],
                        "来源": {"类型": "聚合计算", "模块": "dq_check_run", "字段": "失败数之和"},
                    },
                    "趋势分析": {
                        "值": report["趋势分析"],
                        "来源": {"类型": "聚合计算", "模块": "dq_trend", "字段": "综合质量均值"},
                    },
                },
                remark="新格式生成时同步产出 v2 快照",
            )
        report["快照编号"] = snapshot["快照编号"]

        # 计算质量维度得分汇总
        dim_scores = self._compute_dimension_scores(scorecard_rows)

        return {
            "报告编号": report["报告编号"],
            "快照编号": snapshot["快照编号"],
            "报告周期": period,
            "巡检总次数": total_checks,
            "巡检通过率": f"{passed_checks / total_checks * 100:.1f}%" if total_checks > 0 else "N/A",
            "质量事件总数": total_incidents,
            "未处理事件": open_incidents,
            "已修复事件": resolved_incidents,
            "修复率": f"{resolved_incidents / total_incidents * 100:.1f}%" if total_incidents > 0 else "N/A",
            "平均综合质量": round(avg_quality, 2),
            "维度得分": dim_scores,
            "生成时间": report["生成时间"],
            "趋势": report["趋势分析"],
        }

    def _compute_dimension_scores(self, scorecard_rows: list[dict]) -> dict[str, float]:
        """计算质量维度得分汇总：完整性、准确性、时效性、唯一性。"""
        dims = {"完整性": [], "准确性": [], "时效性": []}
        for s in scorecard_rows:
            for dim_key, field in [("完整性", "完整性得分"), ("准确性", "准确性得分"), ("时效性", "时效性得分")]:
                try:
                    val = float(s.get(field, "0"))
                    dims[dim_key].append(val)
                except (ValueError, TypeError):
                    pass
        return {
            k: round(sum(v) / len(v), 1) if v else 0.0
            for k, v in dims.items()
        }

    def export_report_csv(self, report_id: int) -> str | None:
        """导出质量报告为CSV格式，包含所有维度数据和趋势标记。"""
        report = store.find(self.MODULE, report_id)
        if not report:
            return None

        lines = [
            "报告编号,报告周期,覆盖表数,通过规则数,失败规则数,趋势,生成时间,版本",
            f"{report.get('报告编号')},{report.get('报告周期')},{report.get('覆盖表数')},{report.get('通过规则数')},{report.get('失败规则数')},{report.get('趋势分析')},{report.get('生成时间')},{report.get('报告版本')}",
        ]
        return "\n".join(lines)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """报告状态流转；“确认完成”同时把确认结果签收进事件回查清单。

        报告行本身仍是通用状态机，但 v2 快照需要独立签收记录，
        这样事件侧才能按快照定位“哪次确认覆盖了哪些事件”。
        """
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "确认完成":
            for snapshot in reversed(report_compat.snapshots_for_entry(entry_id)):
                if snapshot.get("签收状态") in ("已签收", "已作废"):
                    continue
                signed, _ = report_compat.sign_snapshot(
                    int(snapshot["id"]), result="通过", reviewer="报告确认")
                if signed is not None:
                    message = f"{message}，快照 {snapshot['快照编号']} 已签收并同步事件回查清单"
                    break
        return entry, message

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "聚合巡检和质量事件数据生成质量分析报告"
        snapshot["artifact"] = "质量报告数据集"
        snapshot["artifact_desc"] = "聚合多维度质量指标的质量报告数据集"
        return snapshot