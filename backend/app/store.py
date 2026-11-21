"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。

除了按模块保存当前行，仓库还维护一份**事件流水（journal）**：
- 每次登记 / 状态流转都会追加一条带全局序号和时间的记录；
- `rows_as_of(module, seq)` 可以重建“某个序号时点”的模块数据。

报告兼容层用它来固定快照时点，避免部署中断、恢复或回滚后
把不同时点的质量事件内容拼进同一份报告。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = deepcopy(SEED_ROWS)
        # 初始快照用于按事件序号重建历史时点，后续所有变更都通过 journal 表达。
        self._baseline: dict[str, list[dict[str, Any]]] = deepcopy(SEED_ROWS)
        self._journal: list[dict[str, Any]] = []
        self._seq = 0
        # 快照创建与状态流转可能被并发请求触发，用可重入锁保证序号和行内容原子。
        self.lock = threading.RLock()

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def journal_seq(self) -> int:
        """返回当前全局事件序号；报告快照会把这个值固定下来。"""
        with self.lock:
            return self._seq

    def reset(self) -> None:
        """测试用：恢复到种子数据，清空流水和序号。"""
        with self.lock:
            self._tables = deepcopy(SEED_ROWS)
            self._baseline = deepcopy(SEED_ROWS)
            self._journal.clear()
            self._seq = 0

    def record(
        self,
        module: str,
        entry_id: int,
        operation: str,
        changes: dict[str, Any],
    ) -> dict[str, Any]:
        """追加一条事件流水，返回写入的事件。"""
        with self.lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "at": datetime.now(timezone.utc).isoformat(),
                "module": module,
                "id": int(entry_id),
                "operation": operation,
                "changes": deepcopy(changes),
            }
            self._journal.append(event)
            return event

    def record_create(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        return self.record(module, int(row["id"]), "create", {"row": deepcopy(row)})

    def append(self, module: str, row: dict[str, Any]) -> dict[str, Any]:
        """登记新行并写入流水；领域服务应优先使用这个方法。"""
        with self.lock:
            self.rows(module).append(row)
            self.record_create(module, row)
            return row

    def journal(self, limit: int | None = None) -> list[dict[str, Any]]:
        """返回事件流水的浅层副本，便于诊断和测试断言。"""
        with self.lock:
            events = deepcopy(self._journal)
        return events if limit is None else events[:limit]

    def rows_as_of(self, module: str, seq: int) -> list[dict[str, Any]]:
        """按事件序号重建模块数据：只应用 `seq` 之前（含）的登记和流转。

        报告适配器只通过这个入口读取历史内容，因此恢复 / 回滚后
        不会把快照之后才发生的事件状态混进来。
        """
        with self.lock:
            base = deepcopy(self._baseline.get(module, []))
            events = [
                deepcopy(event)
                for event in self._journal
                if event["seq"] <= seq and event["module"] == module
            ]
        rows = [dict(row) for row in base]
        index = {int(row.get("id", 0)): row for row in rows}
        for event in events:
            entry_id = int(event.get("id", 0))
            operation = event.get("operation")
            changes = event.get("changes") or {}
            if operation == "create":
                created = deepcopy(changes.get("row") or {})
                if created:
                    rows.append(created)
                    index[entry_id] = created
                continue
            target = index.get(entry_id)
            if target is None:
                continue
            if operation == "update":
                target.update(deepcopy(changes))
        return rows

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
