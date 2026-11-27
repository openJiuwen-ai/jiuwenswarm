# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""User todos tool for JiuWenClaw - Managing todo items per channel."""

import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum
import contextvars as _contextvars
from openjiuwen.core.foundation.tool.tool import tool
from jiuwenclaw.utils import logger


_global_workspace_dir: str = "."
_ctx_channel_id: _contextvars.ContextVar[str] = _contextvars.ContextVar("user_todo_channel_id", default="default")
_ctx_created_by: _contextvars.ContextVar[str] = _contextvars.ContextVar("user_todo_created_by", default="")


class TodoStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TodoPriority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class UserTodosParams:
    action: str
    channel_id: Optional[str] = None
    todo_id: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    priority: Optional[str] = None
    due_at: Optional[str] = None
    remind_at: Optional[str] = None
    created_by: Optional[str] = None
    description: Optional[str] = None
    query: Optional[str] = None


@dataclass
class TodoItem:
    id: str
    title: str
    status: TodoStatus = TodoStatus.PENDING
    priority: TodoPriority = TodoPriority.MEDIUM
    due_at: Optional[str] = None
    remind_at: Optional[str] = None
    create_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    update_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_by: Optional[str] = None
    description: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "priority": self.priority.value,
            "due_at": self.due_at,
            "remind_at": self.remind_at,
            "create_at": self.create_at,
            "update_at": self.update_at,
            "created_by": self.created_by,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TodoItem":
        return cls(
            id=data.get("id", str(uuid.uuid4())[:8]),
            title=data.get("title", ""),
            status=TodoStatus(data.get("status", "pending")),
            priority=TodoPriority(data.get("priority", "medium")),
            due_at=data.get("due_at"),
            remind_at=data.get("remind_at"),
            create_at=data.get("create_at", datetime.now(timezone.utc).isoformat()),
            update_at=data.get("update_at", datetime.now(timezone.utc).isoformat()),
            created_by=data.get("created_by"),
            description=data.get("description", ""),
        )


def set_global_workspace_dir(workspace_dir: str):
    """Set global workspace directory."""
    global _global_workspace_dir
    _global_workspace_dir = workspace_dir


def set_global_channel_id(channel_id: str):
    """Set channel ID for user_todos tool (per-coroutine via ContextVar)."""
    _ctx_channel_id.set(channel_id)


def set_global_created_by(created_by: str):
    """Set created_by for user_todos tool (per-coroutine via ContextVar)."""
    _ctx_created_by.set(created_by)


def _get_todos_dir() -> str:
    """Get the todos directory path."""
    return os.path.join(_global_workspace_dir, "memory", "user_todos")


def _sanitize_channel_id(raw_id: str) -> str:
    """Remove path separators and special characters to prevent path traversal."""
    return re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_id)


def _get_todos_file_path(channel_id: Optional[str] = None) -> str:
    """Get the todos file path for a specific channel."""
    cid = channel_id or _ctx_channel_id.get()
    safe_cid = _sanitize_channel_id(cid)
    todos_dir = _get_todos_dir()
    return os.path.join(todos_dir, f"{safe_cid}.md")


def _parse_todos_file(file_path: str) -> List[TodoItem]:
    """Parse todos from markdown file with YAML frontmatter."""
    if not os.path.exists(file_path):
        return []
    
    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    todos = []
    todo_pattern = re.compile(
        r'^---\s*\n(.*?)\n---\s*\n(.*?)(?=^---\s*\n|\Z)',
        re.MULTILINE | re.DOTALL
    )
    
    for match in todo_pattern.finditer(content):
        frontmatter = match.group(1)
        body = match.group(2).strip()
        
        todo_data = {}
        for line in frontmatter.split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                todo_data[key.strip()] = value.strip()
        
        if body:
            todo_data['description'] = body
        
        if todo_data.get('id'):
            try:
                todos.append(TodoItem.from_dict(todo_data))
            except Exception as e:
                logger.warning(f"Failed to parse todo item: {e}")
    
    return todos


def _serialize_todo_item(todo: TodoItem) -> str:
    """Serialize a todo item to markdown with YAML frontmatter."""
    lines = ["---"]
    lines.append(f"id: {todo.id}")
    lines.append(f"title: {todo.title}")
    lines.append(f"status: {todo.status.value}")
    lines.append(f"priority: {todo.priority.value}")
    lines.append(f"due_at: {todo.due_at or ''}")
    lines.append(f"remind_at: {todo.remind_at or ''}")
    lines.append(f"create_at: {todo.create_at}")
    lines.append(f"update_at: {todo.update_at}")
    lines.append(f"created_by: {todo.created_by or ''}")
    lines.append("---")
    if todo.description:
        lines.append(todo.description)
    lines.append("")
    return "\n".join(lines)


def _write_todos_file(file_path: str, todos: List[TodoItem]):
    """Write todos to markdown file."""
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    
    content = "# User Todos\n\n"
    for todo in todos:
        content += _serialize_todo_item(todo)
    
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


@tool(
    name="user_todos",
    description="管理用户的个人待办事项和日程安排（如会议、提醒、计划）。当用户提到未来的安排或计划时应主动调用。注意：\
        这是用户的个人待办工具，不要用于 agent 内部的任务规划"
        "（用户任务规划请用 todo_*；SKILL.md 步骤追踪请用 skill_step）。",
)
async def user_todos(params: UserTodosParams) -> Dict[str, Any]:
    """管理用户的待办事项列表。支持按 channel 隔离，每个 channel 有独立的待办列表。

    Args:
        params: 待办事项操作参数，包含以下字段：
            - action: 操作类型，可选值: list, get, create, update, delete, search
            - channel_id: Channel ID，用于隔离不同 channel 的待办事项（默认使用全局 channel_id）
            - todo_id: 待办事项 ID（用于 get, update, delete 操作）
            - title: 待办事项标题（用于 create, update 操作）
            - status: 状态 (pending, in_progress, completed, cancelled)
            - priority: 优先级 (high, medium, low)
            - due_at: 截止时间 (ISO 格式)
            - remind_at: 提醒时间 (ISO 格式)
            - created_by: 创建者
            - description: 详细描述
            - query: 搜索关键词（用于 search 操作）

    Returns:
        操作结果字典
    """
    if isinstance(params, dict):
        params = UserTodosParams(**{k: v for k, v in params.items() if k in UserTodosParams.__dataclass_fields__})
    return await _handle_user_todos(params)


async def _handle_user_todos(params: UserTodosParams) -> Dict[str, Any]:
    """处理用户待办事项的核心逻辑。

    Args:
        params: 用户待办事项参数封装

    Returns:
        操作结果字典
    """
    try:
        cid = params.channel_id or _ctx_channel_id.get()
        file_path = _get_todos_file_path(cid)
        
        if params.action == "list":
            todos = _parse_todos_file(file_path)
            return {
                "success": True,
                "channel_id": cid,
                "todos": [t.to_dict() for t in todos],
                "count": len(todos),
            }
        
        elif params.action == "get":
            if not params.todo_id:
                return {"success": False, "error": "todo_id is required for get action"}
            
            todos = _parse_todos_file(file_path)
            todo = next((t for t in todos if t.id == params.todo_id), None)
            
            if not todo:
                return {"success": False, "error": f"Todo not found: {params.todo_id}"}
            
            return {
                "success": True,
                "todo": todo.to_dict(),
            }
        
        elif params.action == "create":
            if not params.title:
                return {"success": False, "error": "title is required for create action"}
            
            todos = _parse_todos_file(file_path)
            
            final_remind_at = params.remind_at
            if params.due_at and not params.remind_at:
                try:
                    due_time = datetime.fromisoformat(params.due_at)
                    remind_time = due_time - timedelta(minutes=5)
                    final_remind_at = remind_time.isoformat()
                except Exception:
                    final_remind_at = ""
            
            new_todo = TodoItem(
                id=str(uuid.uuid4())[:8],
                title=params.title,
                status=TodoStatus(params.status) if params.status else TodoStatus.PENDING,
                priority=TodoPriority(params.priority) if params.priority else TodoPriority.MEDIUM,
                due_at=params.due_at,
                remind_at=final_remind_at,
                created_by=params.created_by or _ctx_created_by.get(),
                description=params.description or "",
            )
            
            todos.append(new_todo)
            _write_todos_file(file_path, todos)
            
            logger.info(f"Created todo: {new_todo.id} in channel {cid}")
            
            return {
                "success": True,
                "todo": new_todo.to_dict(),
                "message": f"待办事项已创建: {params.title}",
            }
        
        elif params.action == "update":
            if not params.todo_id:
                return {"success": False, "error": "todo_id is required for update action"}
            
            todos = _parse_todos_file(file_path)
            todo = next((t for t in todos if t.id == params.todo_id), None)
            
            if not todo:
                return {"success": False, "error": f"Todo not found: {params.todo_id}"}
            
            if params.title is not None:
                todo.title = params.title
            if params.status is not None:
                todo.status = TodoStatus(params.status)
            if params.priority is not None:
                todo.priority = TodoPriority(params.priority)
            if params.due_at is not None:
                todo.due_at = params.due_at
            if params.remind_at is not None:
                todo.remind_at = params.remind_at
            if params.created_by is not None:
                todo.created_by = params.created_by
            if params.description is not None:
                todo.description = params.description
            
            todo.update_at = datetime.now(timezone.utc).isoformat()
            
            _write_todos_file(file_path, todos)
            
            logger.info(f"Updated todo: {params.todo_id} in channel {cid}")
            
            return {
                "success": True,
                "todo": todo.to_dict(),
                "message": f"待办事项已更新: {todo.title}",
            }
        
        elif params.action == "delete":
            if not params.todo_id:
                return {"success": False, "error": "todo_id is required for delete action"}
            
            todos = _parse_todos_file(file_path)
            todo = next((t for t in todos if t.id == params.todo_id), None)
            
            if not todo:
                return {"success": False, "error": f"Todo not found: {params.todo_id}"}
            
            todos = [t for t in todos if t.id != params.todo_id]
            _write_todos_file(file_path, todos)
            
            logger.info(f"Deleted todo: {params.todo_id} from channel {cid}")
            
            return {
                "success": True,
                "message": f"待办事项已删除: {todo.title}",
            }
        
        elif params.action == "search":
            if not params.query:
                return {"success": False, "error": "query is required for search action"}
            
            todos = _parse_todos_file(file_path)
            query_lower = params.query.lower()
            
            matched = [
                t for t in todos
                if query_lower in t.title.lower() or query_lower in t.description.lower()
            ]
            
            return {
                "success": True,
                "channel_id": cid,
                "query": params.query,
                "todos": [t.to_dict() for t in matched],
                "count": len(matched),
            }
        
        else:
            return {
                "success": False,
                "error": f"Unknown action: {params.action}. Valid actions: list, get, create, update, delete, search",
            }
    
    except Exception as e:
        logger.error(f"user_todos failed: {e}")
        return {
            "success": False,
            "error": str(e),
        }


def get_decorated_tools() -> List:
    """获取使用 @tool 装饰器的工具列表"""
    return [user_todos]
