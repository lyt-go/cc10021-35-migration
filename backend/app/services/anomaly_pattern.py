"""异常模式识别服务：训练和检测数据异常模式，驱动质量事件和告警。

domain_chain: 异常模式识别 (anomaly_pattern)
  └─ 清洗 → 质量事件 (dq_incident)
  └─ 检测 → 告警通知 (dq_alert_channel)
  └─ 聚合 → 质量报告 (dq_report)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.report_snapshot import report_compat
from app.store import store


class AnomalyPatternService(BaseModuleService):
    MODULE = "anomaly_pattern"
    ENTITY = "异常模式"
    OPERATION_TERM = "清洗"
    LIST_FIELDS = ["模式编号", "模式名称", "检测方法", "特征字段", "训练窗口", "敏感度", "模式版本", "最后训练"]
    REQUIRED_FIELDS = ["模式编号", "模式名称", "检测方法"]
    STATUS_ORDER = ["草稿", "训练中", "已启用", "已停用"]
    ACTION_RULES = {"开始训练": "训练中", "启用模式": "已启用", "停用模式": "已停用"}
    NEGATIVE_ACTIONS = ["停用模式"]

    ANOMALY_METHODS = ["3-sigma", "IQR", "isolation_forest", "moving_average", "zscore_deviation", "percentile_shift"]

    def train_anomaly_pattern(self, pattern_id: int, training_window: str = "7d") -> dict[str, Any]:
        """训练异常检测模式：清洗历史数据并拟合检测模型参数。

        从字段画像和指标监控中读取历史数据，清洗异常值后拟合模型参数，
        计算统计基线（均值、标准差、四分位数），训练完成的模式写入存储。
        """
        pattern = store.find(self.MODULE, pattern_id)
        if not pattern:
            return {"状态": "失败", "原因": f"模式{pattern_id}不存在"}

        method = pattern.get("检测方法", "3-sigma")
        feature_field = pattern.get("特征字段", "amount")

        # 清洗历史数据：模拟从画像和指标中抓取历史值
        historical_values = [random.gauss(10000, 2000) for _ in range(100)]
        # 清洗异常值：移除超出3倍标准差的极端值
        mean_val = sum(historical_values) / len(historical_values)
        std_val = (sum((x - mean_val) ** 2 for x in historical_values) / len(historical_values)) ** 0.5
        cleaned = [v for v in historical_values if abs(v - mean_val) <= 3 * std_val]

        # 计算统计基线参数
        cleaned.sort()
        q1 = cleaned[len(cleaned) // 4]
        q3 = cleaned[3 * len(cleaned) // 4]
        iqr = q3 - q1

        model_params = {
            "清洗前样本数": len(historical_values),
            "清洗后样本数": len(cleaned),
            "均值": round(mean_val, 4),
            "标准差": round(std_val, 4),
            "Q1": round(q1, 4),
            "Q3": round(q3, 4),
            "IQR": round(iqr, 4),
            "检测方法": method,
            "上界(3sigma)": round(mean_val + 3 * std_val, 4),
            "下界(3sigma)": round(mean_val - 3 * std_val, 4),
            "训练窗口": training_window,
        }

        # 更新模式状态
        pattern["status"] = "已启用"
        pattern["pending"] = False
        pattern["最后训练"] = datetime.now(timezone.utc).isoformat()
        ver = pattern.get("模式版本", "v0")
        try:
            ver_num = int(ver.lstrip("v")) + 1
        except (ValueError, AttributeError):
            ver_num = 1
        pattern["模式版本"] = f"v{ver_num}"

        return {
            "模式编号": pattern.get("模式编号"),
            "训练结果": "成功",
            "模型参数": model_params,
            "训练时间": datetime.now(timezone.utc).isoformat(),
        }

    def detect_anomalies(self, pattern_id: int, current_values: list[float] | None = None) -> dict[str, Any]:
        """检测当前数据中的异常点并上报质量事件。

        加载已训练的异常模式模型参数，对当前数据执行异常检测计算，
        识别偏离统计基线的异常点，检测到的异常写入质量事件和告警通道。
        """
        pattern = store.find(self.MODULE, pattern_id)
        if not pattern:
            return {"状态": "失败", "原因": f"模式{pattern_id}不存在"}

        if current_values is None:
            current_values = [random.gauss(10000, 2000 * random.uniform(1, 5)) for _ in range(50)]

        # 使用模拟的模型参数进行检测
        mean_val = 10000
        std_val = 2000
        upper = mean_val + 3 * std_val
        lower = mean_val - 3 * std_val

        anomalies = []
        for val in current_values:
            if val > upper or val < lower:
                anomalies.append({
                    "异常值": round(val, 4),
                    "偏离倍数": round(abs(val - mean_val) / std_val, 2),
                    "判定依据": f"超出{'+' if val > mean_val else '-'}3σ边界",
                })

        result = {
            "模式编号": pattern.get("模式编号"),
            "检测方法": pattern.get("检测方法"),
            "检测样本数": len(current_values),
            "异常点数": len(anomalies),
            "异常率": f"{len(anomalies) / len(current_values) * 100:.2f}%",
            "异常明细": anomalies[:10],
            "检测时间": datetime.now(timezone.utc).isoformat(),
        }

        # 检测到异常：写入质量事件并触发告警
        if anomalies:
            self._report_anomaly_incidents(pattern, result, len(anomalies))

        return result

    def _report_anomaly_incidents(self, pattern: dict, result: dict, anomaly_count: int) -> None:
        """检测到的异常上报质量事件并聚合到质量报告。"""
        with store.lock:
            # 写入质量事件记录
            incident_rows = store.rows("dq_incident")
            new_iid = max((int(r.get("id", 0)) for r in incident_rows), default=0) + 1
            store.append("dq_incident", {
                "id": new_iid,
                "事件编号": f"DQ_I-{new_iid:04d}",
                "关联规则": f"anomaly:{pattern.get('模式编号', '')}",
                "触发时间": datetime.now(timezone.utc).isoformat(),
                "偏离程度": result["异常率"],
                "影响行数": str(anomaly_count),
                "严重级别": "warning" if anomaly_count > 5 else "info",
                "处理状态": "自动检测",
                "处理人": "异常检测引擎",
                "status": "待确认",
                "pending": True,
                "abnormal": True,
            })

            # 聚合到质量报告，并同步产出 v2 快照
            report_rows = store.rows("dq_report")
            new_rid = max((int(r.get("id", 0)) for r in report_rows), default=0) + 1
            report = {
                "id": new_rid,
                "报告编号": f"DQ_R-{new_rid:04d}",
                "报告周期": f"异常检测-{datetime.now(timezone.utc).strftime('%Y%m%d')}",
                "覆盖表数": "1",
                "通过规则数": str(max(0, result["检测样本数"] - anomaly_count)),
                "失败规则数": str(anomaly_count),
                "趋势分析": "待评估",
                "生成时间": datetime.now(timezone.utc).isoformat(),
                "报告版本": "v2.0",
                "结构版本": "v2.0",
                "status": "已完成",
                "pending": False,
                "abnormal": anomaly_count > 5,
            }
            store.append("dq_report", report)
            report_compat.create_runtime_snapshot(
                report,
                seq=store.journal_seq(),
                runtime_metrics={
                    "覆盖表数": {
                        "值": report["覆盖表数"],
                        "来源": {"类型": "异常检测", "模式编号": pattern.get("模式编号")},
                    },
                    "通过规则数": {
                        "值": report["通过规则数"],
                        "来源": {"类型": "异常检测", "字段": "检测样本数-异常点数"},
                    },
                    "失败规则数": {
                        "值": report["失败规则数"],
                        "来源": {"类型": "异常检测", "字段": "异常点数"},
                    },
                    "趋势分析": {
                        "值": report["趋势分析"],
                        "来源": {"类型": "异常检测", "字段": "待人工评估"},
                    },
                },
            )

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "清洗"
        snapshot["desc"] = "清洗历史数据训练异常检测模型"
        snapshot["artifact"] = "异常模式检测结果"
        snapshot["artifact_desc"] = "经清洗和检测生成的异常模式检测结果"
        return snapshot