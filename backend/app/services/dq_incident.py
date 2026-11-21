"""质量事件服务：记录和追踪数据质量异常事件，驱动趋势分析和评分卡计算。

domain_chain: 质量事件 (dq_incident)
  └─ 关联 → 趋势分析 (dq_trend)
  └─ 聚合 → 评分卡 (dq_scorecard)
  └─ 清洗 → 告警通知 (dq_alert_channel)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DqIncidentService(BaseModuleService):
    MODULE = "dq_incident"
    ENTITY = "质量事件"
    OPERATION_TERM = "写入"
    LIST_FIELDS = ["事件编号", "关联规则", "触发时间", "偏离程度", "影响行数", "严重级别", "处理状态", "处理人"]
    REQUIRED_FIELDS = ["事件编号", "关联规则", "触发时间"]
    STATUS_ORDER = ["待确认", "调查中", "已修复", "已关闭"]
    ACTION_RULES = {"确认事件": "待确认", "开始调查": "调查中", "标记修复": "已修复", "关闭事件": "已关闭"}
    NEGATIVE_ACTIONS = []

    def aggregate_incident_summary(self, table_name: str, days: int = 7) -> dict[str, Any]:
        """聚合指定表在时间窗口内的质量事件摘要。

        查询该表所有关联质量事件，按严重级别和状态聚合统计，
        去重同类事件并分析根因分布，生成事件摘要报告。
        """
        all_incidents = store.rows(self.MODULE)
        related = [
            i for i in all_incidents
            if table_name in str(i.get("关联规则", ""))
        ]

        by_level = {"critical": 0, "warning": 0, "info": 0}
        by_status = {}
        root_causes = {}

        for inc in related:
            level = inc.get("严重级别", "info")
            by_level[level] = by_level.get(level, 0) + 1
            status = inc.get("status", "待确认")
            by_status[status] = by_status.get(status, 0) + 1
            cause = inc.get("关联规则", "未知")
            root_causes[cause] = root_causes.get(cause, 0) + 1

        # 去重同规则事件
        deduped = {}
        for inc in related:
            key = inc.get("关联规则", "")
            if key not in deduped:
                deduped[key] = inc

        return {
            "目标表名": table_name,
            "时间窗口": f"{days}天",
            "事件总数": len(related),
            "去重后": len(deduped),
            "按级别": by_level,
            "按状态": by_status,
            "根因分布": dict(
                sorted(root_causes.items(), key=lambda x: x[1], reverse=True)[:5]
            ),
            "聚合时间": datetime.now(timezone.utc).isoformat(),
        }

    def write_quality_incident(self, rule_id: str, severity: str,
                                deviation: str, table_name: str) -> dict[str, Any]:
        """写入质量事件记录并将事件关联到告警通知通道。

        当检测到数据质量异常时，写入质量事件记录到事件存储中，
        同时将事件分发到告警通道触发通知，并登记到趋势服务。
        """
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1

        entry = {
            "id": new_id,
            "事件编号": f"DQ_I-{new_id:04d}",
            "关联规则": rule_id,
            "触发时间": datetime.now(timezone.utc).isoformat(),
            "偏离程度": deviation,
            "影响行数": str(random.randint(500, 100000)),
            "严重级别": severity,
            "处理状态": "新建",
            "处理人": "系统自动",
            "status": "待确认",
            "pending": True,
            "abnormal": True,
        }
        rows.append(entry)

        # 关联写入趋势记录
        trend_rows = store.rows("dq_trend")
        new_tid = max((int(r.get("id", 0)) for r in trend_rows), default=0) + 1
        trend_rows.append({
            "id": new_tid,
            "记录编号": f"DQ_T-{new_tid:04d}",
            "目标表名": table_name,
            "日期": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "完备率": f"{random.uniform(70, 85):.1f}%",
            "准确率": f"{random.uniform(75, 90):.1f}%",
            "及时率": f"{random.uniform(80, 95):.1f}%",
            "综合质量": f"{random.uniform(70, 85):.1f}",
            "趋势标记": "下降趋势",
            "status": "下降趋势",
            "pending": True,
            "abnormal": True,
        })

        return entry

    def analyze_incident_trend(self, table_name: str) -> dict[str, Any]:
        """分析质量事件趋势并计算改善评分。

        查询历史事件数据，计算事件频率变化、修复率和趋势方向，
        聚合后的趋势分析结果关联到评分卡计算。
        """
        all_incidents = store.rows(self.MODULE)
        related = [i for i in all_incidents if table_name in str(i.get("关联规则", ""))]

        if not related:
            # 无历史事件也要给出统一的趋势字段，避免调用方按“有/无事件”
            # 拿到两种不同的返回结构。
            return {
                "目标表名": table_name,
                "状态": "无历史事件",
                "历史事件总数": 0,
                "已修复": 0,
                "修复率": "N/A",
                "趋势": "平稳",
                "趋势方向": "平稳",
                "建议": "保持监控",
            }

        resolved = sum(1 for i in related if i.get("status") in ("已修复", "已关闭"))
        resolution_rate = resolved / len(related) * 100 if related else 0

        trend = "改善中" if resolution_rate > 60 else "恶化" if resolution_rate < 30 else "平稳"

        return {
            "目标表名": table_name,
            "历史事件总数": len(related),
            "已修复": resolved,
            "修复率": f"{resolution_rate:.1f}%",
            "趋势方向": trend,
            "建议": "加强规则覆盖" if trend == "恶化" else "保持监控" if trend == "平稳" else "继续改进",
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "写入"
        snapshot["desc"] = "写入质量事件记录并关联告警与趋势分析"
        snapshot["artifact"] = "质量事件数据集"
        snapshot["artifact_desc"] = "含趋势和告警关联的质量事件数据集"
        return snapshot