"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, scd_history

# 固定前缀的专用路由必须排在通用 /api/{module} 之前。
ROUTERS = [scd_history, resource]
