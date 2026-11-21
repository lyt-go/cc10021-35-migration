"""质量报告 v1 → v2 兼容链路测试。

覆盖任务要求：
1. 存量 v1 汇总日报迁移到带来源与结构版本的 v2 快照，报告编号可回查；
2. 同一日报两个版本可同时请求（v1 行 + v2 快照）；
3. 旧快照复核中、关联事件刚从待确认变成已关闭时，快照内容不混入新状态；
4. 已签报告逐项重做，找不到来源标记存量兼容缺项；
5. 部署中断后恢复 / 回滚，适配器不拼接不同时点的事件内容；
6. 确认结果同步到质量事件回查清单；
7. 事件侧从快照定位事件和当天处置归属，也能查到缺来源的旧日报；
8. 用已导出的 v1 日报跨版本读取：原编号、事件回查证据、新格式补齐一半时实际取得的报告。
"""
from __future__ import annotations

import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.seed import SEED_ROWS, V1_EXPORT_CSV, V1_SOURCE_ROWS
from app.services.report_snapshot import METRIC_NAMES, report_compat
from app.store import store


DAY = "2026-11-18"


def _seed_day_rows(include_check_run: bool = True) -> None:
    """把 2026-11-18 的来源行补进仓库（不重复插入）。

    ``include_check_run=False`` 用来模拟“当天巡检来源缺失”，
    迁移后通过/失败规则数应标记存量兼容缺项。
    """
    for module, rows in V1_SOURCE_ROWS.items():
        if module == "dq_check_run" and not include_check_run:
            continue
        target = store.rows(module)
        existing_ids = {int(row.get("id", 0)) for row in target}
        for row in rows:
            if int(row["id"]) not in existing_ids:
                store.append(module, dict(row))


def _new_incident(event_id: int, day: str, owner: str, status: str = "待确认") -> dict:
    row = {
        "id": event_id,
        "事件编号": f"DQ_I-{event_id:04d}",
        "关联规则": "DQ_R-daily-check",
        "触发时间": f"{day}T09:30:00+00:00",
        "偏离程度": "18%",
        "影响行数": "1200",
        "严重级别": "warning",
        "处理状态": "自动检测",
        "处理人": owner,
        "status": status,
        "pending": status in ("待确认", "调查中"),
        "abnormal": True,
    }
    store.append("dq_incident", row)
    return row


@pytest.fixture(autouse=True)
def clean_state():
    store.reset()
    report_compat.reset()
    yield
    store.reset()
    report_compat.reset()


class TestLegacyMigration:
    def test_migrate_v1_reports_to_v2_snapshots_with_sources(self):
        _seed_day_rows()
        detail = report_compat.start_migration()
        assert detail["状态"] == "已完成"
        assert detail["已提交快照数"] == len(SEED_ROWS["dq_report"])

        snapshots, total = report_compat.list_snapshots(report_no="DQ_R-0001")
        assert total == 1
        snapshot = snapshots[0]
        assert snapshot["结构版本"] == "v2.0"
        assert snapshot["来源版本"] == "v1-汇总"
        # v1 汇总列可以找到来源的指标可重做，找不到的必须标缺项。
        for metric in snapshot["指标"]:
            assert "来源" in metric
            assert metric["可重做"] is True or metric["是否缺项"] is True
            if metric["是否缺项"]:
                assert metric["重做状态"] == "存量兼容缺项"

    def test_report_number_keeps_both_versions(self):
        detail = report_compat.start_migration(["DQ_R-0001"])
        assert detail["状态"] == "已完成"
        versions = report_compat.versions_for_number("DQ_R-0001")
        assert versions["原编号核对"] is True
        structures = [item["结构版本"] for item in versions["历史"]]
        assert "v1" in structures and "v2.0" in structures
        # 同一编号两个版本同时可读，不互相覆盖。
        assert len(versions["历史"]) == 2

    def test_same_day_two_versions_served_together(self):
        _seed_day_rows()
        report_compat.start_migration(["DQ_R-0001"])
        # 再生成一份运行时 v2 报告，形成“同一日报两份 v2 + 一份 v1”
        from app.services.dq_report import DqReportService

        DqReportService().aggregate_quality_report("2026-11-18")
        daily = report_compat.daily_view("2026-09-01")
        assert len(daily["v1旧日报"]) >= 1
        assert len(daily["v2快照"]) >= 1
        numbers = {snapshot["报告编号"] for snapshot in daily["v2快照"]}
        assert "DQ_R-0001" in numbers
        # 同一日报两个版本可以同时被请求
        versions = report_compat.versions_for_number("DQ_R-0001")
        assert {item["结构版本"] for item in versions["历史"]} == {"v1", "v2.0"}

    def test_migration_is_idempotent(self):
        first = report_compat.start_migration(["DQ_R-0001"])
        second = report_compat.start_migration(["DQ_R-0001"])
        assert first["已提交快照数"] == 1
        # 已经迁移过的编号不会重复出快照，仍然按原编号可取得一份 v2
        assert second["已提交快照数"] == 0
        assert second["可读快照数"] == 1
        _, total = report_compat.list_snapshots(report_no="DQ_R-0001", size=1000)
        assert total == 1


class TestTimePointIsolation:
    def test_snapshot_does_not_mix_later_incident_state(self):
        """旧快照复核中，关联事件从待确认变成已关闭，快照内容保持原时点。

        DQ_R-0003 是已完成的旧日报，迁移后进入“复核中”；当天事件先建好再迁移，
        快照固定事件当时的状态，之后再关闭事件也不影响快照。
        """
        incident = _new_incident(9101, "2026-09-03", "数据工程组", "待确认")
        snapshot = report_compat.start_migration(["DQ_R-0003"])["快照"][0]
        assert snapshot["签收状态"] == "复核中"
        evidence = next(
            item for item in snapshot["事件回查"] if item["事件ID"] == incident["id"])
        assert evidence["当时状态"] == "待确认"
        assert evidence["处置归属"] == "数据工程组"

        # 事件在快照之后才关闭
        store.find("dq_incident", incident["id"])["status"] = "已关闭"
        store.record("dq_incident", incident["id"], "update", {"status": "已关闭"})

        stored = report_compat.get_snapshot(snapshot["id"])
        stale = next(
            item for item in stored["事件回查"] if item["事件ID"] == incident["id"])
        assert stale["当时状态"] == "待确认"

        # 事件侧回查能同时看到“当时状态”和“当前状态”，并标记已变化
        incident_view = report_compat.incident_evidence(incident["id"])
        first = next(
            item for item in incident_view["快照证据"]
            if item["证据"]["事件ID"] == incident["id"])
        assert first["证据"]["当时状态"] == "待确认"
        assert first["当前状态"] == "已关闭"
        assert first["状态已变化"] is True

    def test_interrupted_then_resumed_uses_original_timepoint(self):
        interrupted = report_compat.start_migration(stop_after=1)
        assert interrupted["状态"] == "已中断"
        seq = interrupted["数据时点"]
        assert interrupted["已提交快照数"] == 1

        # 中断期间：补一条 2026-09-03 的当日事件；恢复后 DQ_R-0003 的快照不应包含它
        new_event = _new_incident(9003, "2026-09-03", "临时支援组", "待确认")

        detail, message = report_compat.resume_migration(interrupted["迁移编号"])
        assert detail["状态"] == "已完成", message
        snapshots = detail["快照"]
        assert len(snapshots) == 3
        assert all(snapshot["数据时点"] == seq for snapshot in snapshots)
        for snapshot in snapshots:
            assert all(item["事件ID"] != new_event["id"] for item in snapshot["事件回查"])
        # DQ_R-0003 当天的种子事件仍在，说明快照是按原时点重建而不是空证据
        third = next(snapshot for snapshot in snapshots if snapshot["报告编号"] == "DQ_R-0003")
        assert any(item["事件ID"] == 3 for item in third["事件回查"])

    def test_rollback_hides_staged_and_committed_snapshots(self):
        detail = report_compat.start_migration(["DQ_R-0001"])
        assert detail["状态"] == "已完成"
        rolled, message = report_compat.rollback_migration(detail["迁移编号"])
        assert rolled["状态"] == "已回滚", message
        assert rolled["回滚快照数"] == 1
        # 回滚后适配器回退 v1，v2 快照不可见
        effective = report_compat.effective_report("DQ_R-0001")
        assert effective["来源结构"] == "v1"
        _, total = report_compat.list_snapshots(report_no="DQ_R-0001", size=1000)
        assert total == 0

    def test_signed_migration_cannot_rollback(self):
        detail = report_compat.start_migration(["DQ_R-0003"])
        snapshot_id = detail["快照"][0]["id"]
        signed, _ = report_compat.sign_snapshot(snapshot_id, reviewer="复核人丁")
        assert signed["签收状态"] == "已签收"

        rolled, message = report_compat.rollback_migration(detail["迁移编号"])
        assert rolled["状态"] != "已回滚"
        assert "禁止回滚" in message
        assert report_compat.get_snapshot(snapshot_id) is not None


class TestSignAndReview:
    def test_sign_syncs_to_incident_review_list(self):
        # DQ_R-0001 的日报日期是 2026-09-01，种子事件 DQ_I-0001 是当天的待确认事件。
        incident_id = 1
        snapshot = report_compat.start_migration(["DQ_R-0001"])["快照"][0]
        signed, message = report_compat.sign_snapshot(
            snapshot["id"], result="通过", reviewer="复核人甲", remark="数字逐项核对")
        assert signed["签收状态"] == "已签收", message
        assert len(signed["回查清单"]) >= 1

        items, total = report_compat.review_items(incident_no="DQ_I-0001")
        assert total == 1
        item = items[0]
        assert item["确认结果"] == "报告已签收"
        assert item["确认人"] == "复核人甲"
        assert item["当时状态"] == "待确认"
        assert item["报告编号"] == "DQ_R-0001"
        assert item["快照编号"] == snapshot["快照编号"]

        # 回查清单按事件编号定位到具体事件
        incident_view = report_compat.incident_evidence(incident_id)
        assert incident_view["回查清单"][0]["确认结果"] == "报告已签收"

    def test_signed_snapshot_is_immutable_and_recompute_makes_new_revision(self):
        _seed_day_rows()
        snapshot = report_compat.start_migration(["DQ_R-0001"])["快照"][0]
        report_compat.sign_snapshot(snapshot["id"], reviewer="复核人乙")

        revised, message = report_compat.recompute_snapshot(
            snapshot["id"], ["覆盖表数", "趋势分析"], reviewer="复核人乙")
        assert revised is not None, message
        assert revised["修订号"] == 2
        assert revised["前序快照"] == snapshot["id"]
        assert revised["签收状态"] == "待签收"

        # 原快照未被改写
        original = report_compat.get_snapshot(snapshot["id"])
        assert original["修订号"] == 1
        assert original["签收状态"] == "已签收"
        # 重做沿用同一数据时点
        assert revised["数据时点"] == original["数据时点"]

    def test_recompute_marks_missing_source(self):
        _seed_day_rows(include_check_run=False)
        # DQ_R-1002 当天的来源里没有巡检记录，通过/失败规则数必然缺项
        result = report_compat.import_v1_export(V1_EXPORT_CSV)
        snapshot = next(
            item for item in report_compat.list_snapshots(report_no="DQ_R-1002", size=10)[0]
        )
        revised, _ = report_compat.recompute_snapshot(snapshot["id"], reviewer="复核人丙")
        assert revised is not None
        metric_map = {metric["名称"]: metric for metric in revised["指标"]}
        assert metric_map["通过规则数"]["是否缺项"] is True
        assert metric_map["通过规则数"]["重做状态"] == "存量兼容缺项"
        assert metric_map["通过规则数"]["值"] == "20"
        assert metric_map["覆盖表数"]["是否缺项"] is False
        assert revised["补齐率"] == 0.5


class TestIncidentSideLookup:
    def test_incident_evidence_locates_snapshot_and_attribution(self):
        _seed_day_rows()
        # 事件先发生，再迁移，快照才能把它作为当日证据固定下来
        incident = _new_incident(9005, DAY, "质量平台组", "待确认")
        imported = report_compat.import_v1_export(V1_EXPORT_CSV)
        assert imported["原编号"] == ["DQ_R-1001", "DQ_R-1002", "DQ_R-1003"]

        evidence = report_compat.incident_evidence(incident["id"])
        assert evidence["可回查"] is True
        assert evidence["触发日期"] == DAY
        assert evidence["快照证据"], "应在 v2 快照里定位到该事件"
        first = evidence["快照证据"][0]
        assert first["报告编号"] == "DQ_R-1001"
        assert first["证据"]["处置归属"] == "质量平台组"
        assert first["证据"]["当时状态"] == "待确认"

    def test_daily_view_reports_legacy_reports_missing_sources(self):
        _seed_day_rows()
        imported = report_compat.import_v1_export(V1_EXPORT_CSV, stop_after=1)
        # 只补一半：DQ_R-1001 已提交可读，DQ_R-1002/1003 还没轮到，按原编号回退 v1
        cross = {item["报告编号"]: item for item in imported["跨版本读取"]}
        assert cross["DQ_R-1001"]["实际取得报告"]["来源结构"] == "v2.0"
        assert cross["DQ_R-1002"]["实际取得报告"]["来源结构"] == "v1"

        detail, _ = report_compat.resume_migration(imported["迁移"]["迁移编号"])
        assert detail["状态"] == "已完成"
        daily = report_compat.daily_view(DAY)
        # 两份 11-18 旧日报都已迁移出完整快照，不再列为“缺来源旧日报”
        assert daily["缺少来源记录的旧日报"] == []
        # 11-17 的 DQ_R-1003 没有当天来源：快照全缺项，按天视图把它列为缺来源旧日报
        snapshot = report_compat.list_snapshots(report_no="DQ_R-1003", size=10)[0][0]
        assert set(snapshot["缺项"]) == set(METRIC_NAMES)
        assert snapshot["补齐率"] == 0.0
        assert snapshot["缺项标记"] == "存量兼容缺项"
        daily_17 = report_compat.daily_view("2026-11-17")
        assert [item["报告编号"] for item in daily_17["v2快照"]] == ["DQ_R-1003"]
        missing = daily_17["缺少来源记录的旧日报"]
        assert [item["报告编号"] for item in missing] == ["DQ_R-1003"]
        assert set(missing[0]["缺项"]) == set(METRIC_NAMES)

    def test_incident_without_snapshot_falls_back_to_legacy_report(self):
        # 只登记一条旧日报，不做迁移：事件侧应能查到“当时缺少来源记录”的旧日报
        rows = store.rows("dq_report")
        legacy = {
            "id": max(int(row.get("id", 0)) for row in rows) + 1,
            "报告编号": "DQ_R-3001",
            "报告周期": DAY,
            "覆盖表数": "5",
            "通过规则数": "8",
            "失败规则数": "1",
            "趋势分析": "平稳",
            "生成时间": f"{DAY}T23:59:00+00:00",
            "报告版本": "v1.0",
            "status": "已完成",
            "pending": False,
            "abnormal": False,
        }
        store.append("dq_report", legacy)
        incident = _new_incident(9006, DAY, "数据工程组", "待确认")

        result = report_compat.incident_evidence(incident["id"])
        assert result["可回查"] is True
        assert result["快照证据"] == []
        assert [item["报告编号"] for item in result["旧日报"]] == ["DQ_R-3001"]
        assert result["旧日报"][0]["缺项标记"] == "存量兼容缺项"
        # 旧日报没有 v2 快照，按原编号仍然回退 v1 取得
        effective = report_compat.effective_report("DQ_R-3001")
        assert effective["来源结构"] == "v1"
        assert effective["报告"]["覆盖表数"] == "5"
        # 按天视图也能查到这份“当时缺少来源记录”的旧日报
        daily = report_compat.daily_view(DAY)
        assert [item["报告编号"] for item in daily["缺少来源记录的旧日报"]] == ["DQ_R-3001"]


class TestV1ExportCrossVersion:
    def test_parse_export_keeps_original_number(self):
        parsed = report_compat.parse_v1_export(V1_EXPORT_CSV)
        assert [row["报告编号"] for row in parsed] == ["DQ_R-1001", "DQ_R-1002", "DQ_R-1003"]
        for row in parsed:
            for column in ("报告周期", "覆盖表数", "通过规则数", "失败规则数", "趋势分析", "生成时间"):
                assert column in row

    def test_import_and_read_cross_version(self):
        _seed_day_rows()
        result = report_compat.import_v1_export(V1_EXPORT_CSV)
        assert result["原编号"] == ["DQ_R-1001", "DQ_R-1002", "DQ_R-1003"]
        assert result["迁移"]["状态"] == "已完成"
        cross = {item["报告编号"]: item for item in result["跨版本读取"]}

        first = cross["DQ_R-1001"]
        # 原编号核对
        assert first["v1原行"]["报告编号"] == "DQ_R-1001"
        assert first["实际取得报告"]["报告编号"] == "DQ_R-1001"
        # 来源可找回的指标补齐，缺项明确标记
        effective = first["实际取得报告"]
        assert effective["来源结构"] == "v2.0"
        snapshot = report_compat.list_snapshots(report_no="DQ_R-1001", size=10)[0][0]
        metric_map = {metric["名称"]: metric for metric in snapshot["指标"]}
        assert metric_map["覆盖表数"]["重算值"] == "12"
        assert metric_map["通过规则数"]["重算值"] == "30"
        assert metric_map["失败规则数"]["重算值"] == "2"
        assert metric_map["趋势分析"]["重算值"] == "平稳"
        # 迁移不改写历史值，原值仍是导出里的数字
        assert metric_map["覆盖表数"]["原值"] == "12"
        assert snapshot["缺项"] == []
        assert effective["完整"] is True

        # DQ_R-1002 导出值与当日实际来源不一致：迁移保留原值，重算值单独保留
        snapshot_2 = report_compat.list_snapshots(report_no="DQ_R-1002", size=10)[0][0]
        metric_map_2 = {metric["名称"]: metric for metric in snapshot_2["指标"]}
        assert metric_map_2["覆盖表数"]["原值"] == "9"
        assert metric_map_2["覆盖表数"]["重算值"] == "12"
        assert metric_map_2["通过规则数"]["重算值"] == "30"
        assert snapshot_2["缺项"] == []

    def test_half_migrated_actual_report(self):
        _seed_day_rows()
        result = report_compat.import_v1_export(V1_EXPORT_CSV, stop_after=1)
        assert result["迁移"]["状态"] == "已中断"
        assert result["迁移"]["已提交快照数"] == 1

        cross = {item["报告编号"]: item for item in result["跨版本读取"]}
        # 新格式只补齐一份：DQ_R-1001 可取得 v2，另外两份按原编号回退 v1
        assert cross["DQ_R-1001"]["实际取得报告"]["来源结构"] == "v2.0"
        for report_no in ("DQ_R-1002", "DQ_R-1003"):
            effective = cross[report_no]["实际取得报告"]
            assert effective["来源结构"] == "v1"
            assert effective["可取得"] is True
            assert effective["缺项标记"] == "存量兼容缺项"
            assert effective["报告编号"] == report_no

        # 恢复补齐剩余，仍沿用原数据时点
        detail, message = report_compat.resume_migration(result["迁移"]["迁移编号"])
        assert detail["状态"] == "已完成", message
        seq = result["迁移"]["数据时点"]
        assert all(snapshot["数据时点"] == seq for snapshot in detail["快照"])
        effective_1001 = report_compat.effective_report("DQ_R-1001")
        assert effective_1001["来源结构"] == "v2.0"
        assert effective_1001["补齐率"] > 0
        assert report_compat.effective_report("DQ_R-1002")["来源结构"] == "v2.0"

    def test_missing_metric_falls_back_to_v1_value(self):
        _seed_day_rows()
        # 只导入一份来源不足的旧日报：11-19 没有评分卡/巡检/趋势
        csv_text = "\n".join([
            "id,报告编号,报告周期,覆盖表数,通过规则数,失败规则数,趋势分析,生成时间,报告版本,status",
            "201,DQ_R-2001,2026-11-19,7,11,3,下降,2026-11-19T23:59:00+00:00,v1.0,已完成",
        ]) + "\n"
        result = report_compat.import_v1_export(csv_text)
        effective = result["跨版本读取"][0]["实际取得报告"]
        assert effective["来源结构"] == "v2.0"
        assert effective["完整"] is False
        assert set(effective["回退项"]) == set(METRIC_NAMES)
        # 缺项回退到 v1 原值，不能凭空变成 None
        assert effective["报告"]["覆盖表数"] == "7"
        assert effective["报告"]["通过规则数"] == "11"
        assert effective["字段来源"]["覆盖表数"]["回退"] is True

    def test_import_rejects_malformed_export(self):
        with pytest.raises(ValueError):
            report_compat.parse_v1_export("报告编号,报告周期\nDQ_R-1,daily\n")


class TestRuntimeSnapshot:
    def test_new_format_generation_creates_v2_snapshot(self):
        from app.services.dq_report import DqReportService

        result = DqReportService().aggregate_quality_report("daily")
        snapshot = report_compat.get_snapshot(
            next(
                item["id"] for item in report_compat.list_snapshots(report_no=result["报告编号"], size=10)[0]
            )
        )
        assert snapshot["结构版本"] == "v2.0"
        assert snapshot["来源版本"] == "运行时生成"
        assert snapshot["签收状态"] == "待签收"
        assert all(not metric["是否缺项"] for metric in snapshot["指标"])


class TestWorkflowConfirm:
    """报告确认动作要同步到质量事件回查清单。"""

    def test_confirm_action_signs_snapshot_and_syncs_review(self):
        from app.services.dq_report import DqReportService

        svc = DqReportService()
        generated = svc.aggregate_quality_report("2026-09-01")
        report_row = next(
            row for row in store.rows("dq_report")
            if row.get("报告编号") == generated["报告编号"]
        )
        entry, message = svc.run_action(int(report_row["id"]), "确认完成")
        assert entry is not None
        assert entry["status"] == "已完成"
        assert "同步事件回查清单" in message

        snapshots, _ = report_compat.list_snapshots(report_no=generated["报告编号"], size=10)
        assert snapshots[0]["签收状态"] == "已签收"
        items, total = report_compat.review_items(report_no=generated["报告编号"])
        assert total == len(snapshots[0]["事件回查"])


class TestConcurrencySafety:
    """同一日报两个版本可能被同时请求：读取必须拿到完整快照，不能看到半份。"""

    def test_concurrent_reads_never_see_partial_snapshot(self):
        import threading

        result: dict[str, int] = {"v2": 0, "v1": 0, "missing": 0}
        lock = threading.Lock()

        def read() -> None:
            report = report_compat.effective_report("DQ_R-0001")
            with lock:
                result[report["来源结构"]] = result.get(report["来源结构"], 0) + 1
                if report["来源结构"] == "v2.0":
                    assert set(report["报告"]) == set(METRIC_NAMES)

        threads = [threading.Thread(target=read) for _ in range(20)]
        for thread in threads:
            thread.start()
        report_compat.start_migration(["DQ_R-0001"])
        for thread in threads:
            thread.join()
        assert result.get("v2.0", 0) + result.get("v1", 0) == 20
