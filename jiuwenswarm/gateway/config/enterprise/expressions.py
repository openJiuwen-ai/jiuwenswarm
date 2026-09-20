# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""实例 Agent 授权表达式：配置写入和请求准入共用解析、校验与求值。"""

from __future__ import annotations

import ast
import json
import re
from typing import Any

_ALLOWED_MATCH_NAMES = frozenset({"user_id", "group_id", "bot_id"})
# 对常见误写保留明确的错误提示；其余非空表达式统一通过 AST 校验。
_ALWAYS_MATCH_FORBIDDEN_CHECKS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\$\{"), "template_ref placeholders like ${...} are not allowed"),
    (re.compile(r"===|!=="), "=== / !== are not allowed; use == / !="),
    (
        re.compile(r">=|<=|(?<![!=])>(?!=)|(?<!<|>)<(?!=)"),
        "ordering operators (>, <, >=, <=) are not allowed; only == / != / in / not in are supported",
    ),
    (
        re.compile(r"\bservice_id\b|\bagent_id\b", re.IGNORECASE),
        "service_id / agent_id are not allowed; only user_id / group_id / bot_id",
    ),
)
_MATCH_EXPR_PREFIX = "invalid match_expr"


def _match_expr_error(reason: str) -> ValueError:
    return ValueError(f"{_MATCH_EXPR_PREFIX}: {reason}")


def _always_match_forbidden_reason(text: str) -> str | None:
    for pattern, reason in _ALWAYS_MATCH_FORBIDDEN_CHECKS:
        if pattern.search(text):
            return reason
    return None


def validate_match_expr(expr: Any) -> None:
    """校验表达式；空值全匹配，表达式列表为 OR，不合法时抛出 ValueError。"""
    _parse_match_expr(expr)


def _parse_match_expr(expr: Any) -> ast.AST | list | None:
    """返回已校验的 AST；写入和求值不各自解释表达式。"""
    if expr is None:
        return None
    if isinstance(expr, list):
        if not expr:
            return None
        nodes = []
        for index, item in enumerate(expr):
            try:
                if not isinstance(item, str) or not item.strip():
                    raise _match_expr_error("OR clauses must be non-empty expression strings")
                node = _parse_match_expr(item)
                if node is None or isinstance(node, list):
                    raise _match_expr_error("OR clauses must be non-empty expressions")
                nodes.append(node)
            except ValueError as exc:
                detail = str(exc)
                if detail.startswith(_MATCH_EXPR_PREFIX):
                    detail = detail[len(_MATCH_EXPR_PREFIX):].lstrip(": ").lstrip("：")
                raise _match_expr_error(f"item[{index}]: {detail}") from exc
        return nodes

    if not isinstance(expr, str):
        raise _match_expr_error("expected string, string array or null")
    text = expr.strip()
    if not text:
        return None

    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _match_expr_error(
                f"value starting with '[' must be a valid JSON array ({exc})"
            ) from exc
        if not isinstance(parsed, list):
            raise _match_expr_error("JSON value must be an array")
        return _parse_match_expr(parsed)

    if "==" not in text and "!=" not in text and not re.search(r"\bin\b", text):
        reason = _always_match_forbidden_reason(text)
        if reason:
            raise _match_expr_error(reason)

    # === / !== contain == / != as substrings; reject before ast.parse.
    if "===" in text or "!==" in text:
        raise _match_expr_error("=== / !== are not allowed; use == / !=")

    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError as exc:
        raise _match_expr_error(f"syntax error ({exc.msg})") from exc

    try:
        _validate_match_ast(tree.body)
    except ValueError as exc:
        raise _match_expr_error(str(exc)) from exc
    return tree.body


def _validate_match_ast(node: ast.AST) -> None:
    """校验比较/布尔 AST 是否落在 match_expr 安全子集内。"""
    if isinstance(node, ast.Compare):
        _validate_match_value(node.left)
        for op, comparator in zip(node.ops, node.comparators):
            if not isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)):
                raise ValueError(
                    "only ==, !=, in and not in are supported; "
                    "ordering operators (>, <, >=, <=) are not allowed"
                )
            _validate_match_value(comparator)
        return

    if isinstance(node, ast.BoolOp):
        if not isinstance(node.op, (ast.And, ast.Or)):
            raise ValueError("only and / or combinators are supported")
        for value in node.values:
            _validate_match_ast(value)
        return

    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return

    raise ValueError(
        "only comparison expressions combined with and/or are allowed "
        "(fields limited to user_id / group_id / bot_id)"
    )


def _validate_match_value(node: ast.AST) -> None:
    """校验比较左右操作数：字面量或允许的字段名。"""
    if isinstance(node, (ast.List, ast.Tuple)):
        if not all(isinstance(item, ast.Constant) for item in node.elts):
            raise ValueError("membership values must be literals")
        return
    if isinstance(node, ast.Constant):
        return
    if isinstance(node, ast.Name):
        if node.id not in _ALLOWED_MATCH_NAMES:
            raise ValueError(
                f"unknown name {node.id!r}; only user_id / group_id / bot_id are allowed"
            )
        return
    if isinstance(node, ast.Call):
        raise ValueError("function calls are not supported")
    if isinstance(node, ast.Attribute):
        raise ValueError("attribute access is not supported")
    if isinstance(node, (ast.BinOp, ast.UnaryOp)):
        raise ValueError("arithmetic and unary operators are not supported")
    raise ValueError("operands must be field names or literals")


def matches(expr: Any, identity: dict[str, str]) -> bool:
    """仅对已校验的语法求值；非法规则抛错，由准入层拒绝请求。"""
    return _evaluate_match_ast(_parse_match_expr(expr), identity)


def _match_value(node: ast.AST, identity: dict[str, str]) -> Any:
    if isinstance(node, ast.Name):
        return identity[node.id]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [item.value for item in node.elts]
    return node.value


def _evaluate_match_ast(node: ast.AST | list | None, identity: dict[str, str]) -> bool:
    if node is None:
        return True
    if isinstance(node, list):
        return any(_evaluate_match_ast(item, identity) for item in node)
    if isinstance(node, ast.BoolOp):
        results = (_evaluate_match_ast(item, identity) for item in node.values)
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.Constant):
        return node.value
    left = _match_value(node.left, identity)
    for op, comparator in zip(node.ops, node.comparators):
        right = _match_value(comparator, identity)
        if isinstance(op, (ast.In, ast.NotIn)):
            # 与 Manager 一致：单值 RHS 按单元素集合处理，不能做子串匹配。
            members = right if isinstance(right, list) else [right]
            matched = (left in members) == isinstance(op, ast.In)
        else:
            matched = (left == right) == isinstance(op, ast.Eq)
        if not matched:
            return False
        left = right
    return True


__all__ = ("validate_match_expr", "matches")
