# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""PlanCodeValidator -- 规划代码 AST 静态安全校验。"""

from __future__ import annotations

import ast

from dataclasses import dataclass


class PlanCodeValidationError(Exception):
    """规划代码静态校验失败。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(f"规划代码校验失败: {errors}")


@dataclass(frozen=True)
class CodeValidationPolicy:
    """代码静态校验策略。"""

    name: str = "plan_code"
    allow_import: bool = False
    allow_import_from: bool = True
    allowed_import_exact: tuple[str, ...] = ()
    allowed_import_prefixes: tuple[str, ...] = ()
    denied_import_exact: tuple[str, ...] = ()
    denied_import_prefixes: tuple[str, ...] = ()
    import_error_hint: str = "仅允许从 skill_codes 导入"
    deny_relative_import: bool = True
    deny_dunder_attribute: bool = True
    allowed_dunder_attributes: tuple[str, ...] = ()
    denied_call_names: tuple[str, ...] = ()
    denied_attribute_call_names: tuple[str, ...] = ()
    deny_global: bool = False
    deny_nonlocal: bool = False
    deny_with: bool = False
    deny_try: bool = False
    deny_del_attribute: bool = False
    deny_del_subscript: bool = False
    deny_type_three_args: bool = False
    # 安全护栏：要求所有捕获 Exception/BaseException 的 except 子句必须重抛 AbortError，
    # 否则 PermissionInterruptRail 抛出的 AbortError 会被吞掉，导致 HITL 中断失效。
    require_abort_reraise: bool = False

    @classmethod
    def plan_code(
        cls, allowed_import_prefixes: list[str] | tuple[str, ...] | None = None
    ) -> "CodeValidationPolicy":
        return cls(
            name="plan_code",
            allow_import=False,
            allow_import_from=True,
            allowed_import_prefixes=tuple(allowed_import_prefixes or ()),
            import_error_hint="仅允许从 skill_codes 导入",
            require_abort_reraise=True,
        )

    @classmethod
    def generated_skill_code(cls) -> "CodeValidationPolicy":
        return cls(
            name="generated_skill_code",
            allow_import=False,
            allow_import_from=True,
            allowed_import_exact=(
                "jiuwenswarm.server.runtime.skill_turbo.plan_node",
            ),
            import_error_hint="动态生成 skill_code 仅允许 import PlanNode",
            denied_call_names=(
                "open",
                "exec",
                "eval",
                "compile",
                "__import__",
                "getattr",
                "setattr",
                "delattr",
                "globals",
                "locals",
                "vars",
                "dir",
                "input",
                "help",
                "breakpoint",
                "exit",
                "quit",
                "memoryview",
                "type",
            ),
            denied_attribute_call_names=(
                "read_text",
                "read_bytes",
                "write_text",
                "write_bytes",
                "open",
                "unlink",
                "rmdir",
                "rename",
                "mkdir",
                "glob",
                "rglob",
                "create_subprocess_exec",
                "create_subprocess_shell",
                "to_thread",
                "basicConfig",
            ),
            deny_global=True,
            deny_nonlocal=True,
            deny_with=True,
            deny_try=True,
            deny_del_attribute=True,
            deny_del_subscript=True,
            deny_type_three_args=True,
        )

    @classmethod
    def builtin_skill_code(
        cls, allowed_import_prefixes: list[str] | tuple[str, ...] | None = None
    ) -> "CodeValidationPolicy":
        return cls(
            name="builtin_skill_code",
            allow_import=True,
            allow_import_from=True,
            allowed_import_exact=(
                "__future__",
                "asyncio",
                "collections.abc",
                "contextlib",
                "dataclasses",
                "datetime",
                "json",
                "logging",
                "pathlib",
                "re",
                "typing",
                # AbortError / FallbackContractError 经 plan_node 统一 re-export；
                # skill_code 禁止直连 openjiuwen / fallback_handler
                "jiuwenswarm.server.runtime.skill_turbo.plan_node",
            ),
            allowed_import_prefixes=tuple(allowed_import_prefixes or ()),
            denied_import_exact=(
                "builtins",
                "ctypes",
                "importlib",
                "inspect",
                "marshal",
                "multiprocessing",
                "os",
                "pickle",
                "shutil",
                "socket",
                "subprocess",
                "sys",
                "tempfile",
                "threading",
            ),
            denied_import_prefixes=("http.", "urllib.", "requests."),
            import_error_hint="仅允许安全标准库与 skill_turbo 内部模块",
            allowed_dunder_attributes=("__init__", "__name__"),
            denied_call_names=(
                "open",
                "exec",
                "eval",
                "compile",
                "__import__",
                "getattr",
                "setattr",
                "delattr",
                "globals",
                "locals",
                "vars",
                "dir",
                "input",
                "help",
                "breakpoint",
                "exit",
                "quit",
                "memoryview",
            ),
            denied_attribute_call_names=(
                "read_text",
                "read_bytes",
                "write_text",
                "write_bytes",
                "open",
                "unlink",
                "rmdir",
                "rename",
                "mkdir",
                "glob",
                "rglob",
                "create_subprocess_exec",
                "create_subprocess_shell",
                "to_thread",
                "basicConfig",
            ),
            deny_del_attribute=True,
            deny_type_three_args=True,
            require_abort_reraise=True,
        )


class PlanCodeValidator:
    """代码安全校验器：按 profile 拦截 import、dunder 与危险语法。"""

    def __init__(
        self,
        allowed_import_prefixes: list[str] | None = None,
        policy: CodeValidationPolicy | None = None,
    ):
        self._policy = policy or CodeValidationPolicy.plan_code(
            allowed_import_prefixes
        )

    @classmethod
    def for_generated_skill_code(cls) -> "PlanCodeValidator":
        return cls(policy=CodeValidationPolicy.generated_skill_code())

    @classmethod
    def for_builtin_skill_code(
        cls, allowed_import_prefixes: list[str] | None = None
    ) -> "PlanCodeValidator":
        return cls(
            policy=CodeValidationPolicy.builtin_skill_code(allowed_import_prefixes)
        )

    def validate(self, code: str) -> list[str]:
        """校验代码，返回错误列表。空列表表示通过。"""
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [f"语法错误: {e}"]

        errors: list[str] = []
        for node in ast.walk(tree):
            self._check_node(node, errors)
        return errors

    def validate_or_raise(self, code: str) -> None:
        """校验代码，失败时抛出 PlanCodeValidationError。"""
        errors = self.validate(code)
        if errors:
            raise PlanCodeValidationError(errors)

    def _check_node(self, node: ast.AST, errors: list[str]) -> None:
        if isinstance(node, ast.Import):
            self._check_import(node, errors)
            return

        if isinstance(node, ast.ImportFrom):
            self._check_import_from(node, errors)
            return

        if isinstance(node, ast.Attribute) and self._policy.deny_dunder_attribute:
            if (
                node.attr.startswith("__")
                and node.attr not in self._policy.allowed_dunder_attributes
            ):
                errors.append(
                    f"禁止访问 dunder 属性: {node.attr} (行 {node.lineno})"
                )
            return

        if isinstance(node, ast.Call):
            self._check_call(node, errors)
            return

        if isinstance(node, ast.Global) and self._policy.deny_global:
            errors.append(f"禁止 global 语句 (行 {node.lineno})")
            return

        if isinstance(node, ast.Nonlocal) and self._policy.deny_nonlocal:
            errors.append(f"禁止 nonlocal 语句 (行 {node.lineno})")
            return

        if isinstance(node, (ast.With, ast.AsyncWith)) and self._policy.deny_with:
            errors.append(f"禁止 with 语句 (行 {node.lineno})")
            return

        if isinstance(node, ast.Try) and self._policy.deny_try:
            errors.append(f"禁止 try/except 语句 (行 {node.lineno})")
            return

        if isinstance(node, ast.Try) and self._policy.require_abort_reraise:
            self._check_try_reraise(node, errors)
            return

        if isinstance(node, ast.Delete):
            self._check_delete(node, errors)
            return

    def _check_import(self, node: ast.Import, errors: list[str]) -> None:
        if not self._policy.allow_import:
            errors.append(f"禁止 import 语句 (行 {node.lineno})")
            return

        for alias in node.names:
            module = alias.name
            if not self._is_import_allowed(module):
                errors.append(self._format_import_error(module, node.lineno))

    def _check_import_from(self, node: ast.ImportFrom, errors: list[str]) -> None:
        if node.level and node.level > 0 and self._policy.deny_relative_import:
            errors.append(f"禁止相对 import (行 {node.lineno})")
            return

        module = node.module or ""
        if not self._policy.allow_import_from:
            errors.append(self._format_import_error(module, node.lineno))
            return

        if not self._is_import_allowed(module):
            errors.append(self._format_import_error(module, node.lineno))

    def _check_call(self, node: ast.Call, errors: list[str]) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in self._policy.denied_call_names:
                errors.append(f"禁止调用危险函数: {func.id} (行 {node.lineno})")
                return
            if (
                self._policy.deny_type_three_args
                and func.id == "type"
                and len(node.args) >= 3
            ):
                errors.append(
                    f"禁止动态类型创建: type() 三参数调用 (行 {node.lineno})"
                )
            return

        if isinstance(func, ast.Attribute):
            if func.attr in self._policy.denied_attribute_call_names:
                errors.append(
                    f"禁止调用危险属性: {func.attr} (行 {node.lineno})"
                )
            return

    def _check_delete(self, node: ast.Delete, errors: list[str]) -> None:
        for target in node.targets:
            if isinstance(target, ast.Attribute) and self._policy.deny_del_attribute:
                errors.append(
                    f"禁止删除对象属性: del {target.attr} (行 {node.lineno})"
                )
            if (
                isinstance(target, ast.Subscript)
                and self._policy.deny_del_subscript
            ):
                errors.append(
                    f"禁止删除下标项: del obj[key] (行 {node.lineno})"
                )

    def _check_try_reraise(self, node: ast.Try, errors: list[str]) -> None:
        """要求 except Exception/BaseException 子句中显式重抛 AbortError。

        识别：宽泛 except 子句（无类型、捕获 Exception、捕获 BaseException），
        必须存在以下守护语句之一：
            if isinstance(<name>, AbortError): raise
            if isinstance(<name>, AbortError): raise <name>
        没有则报错。具体异常类（如 except ValueError）不受此约束。
        """
        for handler in node.handlers:
            if not self._is_broad_handler(handler):
                continue
            if self._handler_reraises_abort(handler):
                continue
            errors.append(
                "宽泛 except 必须显式重抛 AbortError："
                "请在 except 块开头加 `if isinstance(<name>, AbortError): raise`"
                f" (行 {handler.lineno})"
            )

    @staticmethod
    def _is_broad_handler(handler: ast.ExceptHandler) -> bool:
        exc_type = handler.type
        if exc_type is None:
            return True  # bare except
        if isinstance(exc_type, ast.Name) and exc_type.id in (
            "Exception",
            "BaseException",
        ):
            return True
        return False

    @staticmethod
    def _handler_reraises_abort(handler: ast.ExceptHandler) -> bool:
        """检查 except 子句体内是否包含 AbortError 重抛守护。"""
        for stmt in handler.body:
            if not isinstance(stmt, ast.If):
                continue
            test = stmt.test
            # 形如 isinstance(x, AbortError)
            if not (
                isinstance(test, ast.Call)
                and isinstance(test.func, ast.Name)
                and test.func.id == "isinstance"
                and len(test.args) == 2
            ):
                continue
            target = test.args[1]
            if isinstance(target, ast.Name) and target.id == "AbortError":
                pass
            elif (
                isinstance(target, ast.Attribute) and target.attr == "AbortError"
            ):
                pass
            else:
                continue
            for inner in stmt.body:
                if isinstance(inner, ast.Raise):
                    return True
        return False

    def _is_import_allowed(self, module: str) -> bool:
        if self._matches(module, self._policy.denied_import_exact, ()):
            return False
        if self._matches(module, (), self._policy.denied_import_prefixes):
            return False
        return self._matches(
            module,
            self._policy.allowed_import_exact,
            self._policy.allowed_import_prefixes,
        )

    @staticmethod
    def _matches(
        module: str,
        exact_modules: tuple[str, ...],
        prefixes: tuple[str, ...],
    ) -> bool:
        return module in exact_modules or any(module.startswith(p) for p in prefixes)

    def _format_import_error(self, module: str, lineno: int) -> str:
        return (
            f"禁止 import: {module}，{self._policy.import_error_hint} "
            f"(行 {lineno})"
        )
