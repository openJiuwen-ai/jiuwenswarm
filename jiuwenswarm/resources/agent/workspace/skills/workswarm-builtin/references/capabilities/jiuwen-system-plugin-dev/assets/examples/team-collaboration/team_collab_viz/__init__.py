"""团队协作可视化（team-collab-viz）扩展包。

聊天右侧两个页签（团队信息 / 任务关系）的实现：
* ``extension.py``  —— 宿主入口：``ui.ready`` 渲染 Iframe、``team.monitor_event`` 重读快照
* ``snapshot.py``   —— 快照组装与全部派生（进度、依赖反转、层级、状态判定、中文标签）
* ``sessionmap.py`` —— 会话→团队映射（事件学习 + ``ops.state`` 持久化 + 设防兜底）
* ``ui_state.py``   —— 视图状态存储（白名单/TTL/LRU/大小限制）
* ``http_server.py``—— 本机 HTTP 服务（静态页、只读快照、视图状态、健康检查）
* ``lifecycle.py``  —— 端口生命周期（weakref 看门狗 + finalize 兜底）

本模块只做包标记，不导入宿主代码，便于隔离测试时按需导入子模块。
"""

from __future__ import annotations

__all__ = ["PACKAGE_ID", "PACKAGE_VERSION"]

PACKAGE_ID = "webui.team-collab-viz"
PACKAGE_VERSION = "1.0.0"
