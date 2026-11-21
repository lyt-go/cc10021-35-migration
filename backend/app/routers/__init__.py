"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import report_compat, resource

# 固定路径的质量报告兼容路由必须排在 /api/{module} 之前。
ROUTERS = [report_compat, resource]
