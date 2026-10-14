"""Small, self-contained Python modules. Validation, not a security sandbox.

Approved standard imports replace JavaScript's JSON/RegExp/Math/Date/Promise
globals; filesystem/network/process modules remain outside the package format.
"""
import ast
import builtins
import symtable
from ...protocol.primitives import utf16Length, trim

_standard = frozenset(("json", "re", "math", "datetime", "asyncio"))
_helpers = frozenset(("fileURLToPath", "pathToFileURL"))
_blocked = frozenset(("__import__", "__builtins__", "eval", "exec", "compile", "open", "globals", "process", "globalThis", "require", "fetch", "WebSocket"))
_unknown = object()


def checkModule(code, kind):
    if not isinstance(code, str) or not trim(code) or utf16Length(code) > 100000:
        raise ValueError(f"{kind}: expected 1–100000 characters of Python.")
    try: source = ast.parse(code, filename="computer-use-candidate.py")
    except SyntaxError as error: raise ValueError(f"{kind}: {error}") from error
    def fail(node, message):
        raise ValueError(f"{kind} line {getattr(node, 'lineno', 1)}:{getattr(node, 'col_offset', 0) + 1}: {message}")
    declared, imports, functions = set(), set(), set()
    postponed = any(isinstance(s, ast.ImportFrom) and s.module == "__future__" and all(a.name == "annotations" for a in s.names) for s in source.body)
    for statement in source.body:
        names = []
        if isinstance(statement, ast.Import):
            if any(item.name not in _standard for item in statement.names): fail(statement, "only json, re, math, datetime and asyncio standard imports are supported.")
            names = [item.asname or item.name for item in statement.names]; imports.update(names)
        elif isinstance(statement, ast.ImportFrom):
            if statement.level or not statement.names or any(item.name == "*" for item in statement.names): fail(statement, "only explicit standard named imports are supported.")
            if statement.module == "__future__" and all(a.name == "annotations" for a in statement.names): continue
            if not (statement.module == "computer_use.library" and all(a.name in _helpers for a in statement.names)) and statement.module not in _standard:
                fail(statement, "only named fileURLToPath/pathToFileURL imports from computer_use.library or supported standard imports are allowed.")
            names = [item.asname or item.name for item in statement.names]; imports.update(names)
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = [statement.name]; functions.add(statement.name)
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            if not all(isinstance(target, ast.Name) for target in targets): fail(statement, "top-level bindings must use simple names.")
            names = [target.id for target in targets]
        for name in names:
            if name in declared: fail(statement, f"duplicate top-level binding {name}; keep one declaration.")
            declared.add(name)
    constants = imports | functions; data = {}

    def dataValue(node):
        if isinstance(node, ast.Constant) and type(node.value) in (str, int, float, bool, type(None)): return node.value
        if isinstance(node, ast.Name): return data.get(node.id, _unknown)
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [dataValue(item) for item in node.elts]
            return _unknown if any(item is _unknown for item in values) else values
        if isinstance(node, ast.Dict):
            values = {}
            for key, child in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or type(key.value) not in (str, int, float, bool, type(None)) or key.value in values: return _unknown
                value = dataValue(child)
                if value is _unknown: return _unknown
                values[key.value] = value
            return values
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant):
            receiver, key = dataValue(node.value), node.slice.value
            if isinstance(receiver, dict) and type(key) in (str, int, float, bool, type(None)): return receiver.get(key, _unknown)
            if isinstance(receiver, list) and type(key) is int and 0 <= key < len(receiver): return receiver[key]
        if isinstance(node, ast.UnaryOp):
            value = dataValue(node.operand)
            if value is not _unknown and not isinstance(value, (dict, list)):
                try:
                    if isinstance(node.op, ast.Not): return not value
                    if isinstance(node.op, ast.UAdd): return +value
                    if isinstance(node.op, ast.USub): return -value
                    if isinstance(node.op, ast.Invert): return ~value
                except (TypeError, ValueError): return _unknown
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = dataValue(node.left), dataValue(node.right)
            if left is not _unknown and right is not _unknown and not isinstance(left, (dict, list)) and not isinstance(right, (dict, list)):
                try: return left + right
                except (TypeError, ValueError): return _unknown
        return _unknown

    def staticValue(node):
        if isinstance(node, ast.Lambda):
            return all(staticValue(default) for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)])
        if isinstance(node, ast.Name): return node.id in constants
        if dataValue(node) is not _unknown: return True
        if isinstance(node, (ast.List, ast.Tuple)): return all(staticValue(item) for item in node.elts)
        if isinstance(node, ast.Dict):
            return all(isinstance(key, ast.Constant) and type(key.value) in (str, int, float, bool, type(None)) and staticValue(value) for key, value in zip(node.keys, node.values))
        return False

    for statement in source.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)): continue
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str): continue
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if statement.decorator_list or getattr(statement, "type_params", []): fail(statement, "decorators and type-parameter initialization are not supported; no top-level execution.")
            if not all(staticValue(default) for default in [*statement.args.defaults, *(d for d in statement.args.kw_defaults if d is not None)]): fail(statement, "function defaults must be static data; no top-level execution.")
            if not postponed:
                annotations = [statement.returns, *(a.annotation for a in [*statement.args.posonlyargs, *statement.args.args, *statement.args.kwonlyargs]),
                               statement.args.vararg.annotation if statement.args.vararg else None, statement.args.kwarg.annotation if statement.args.kwarg else None]
                if any(a is not None and not (isinstance(a, ast.Constant) and isinstance(a.value, str)) for a in annotations):
                    fail(statement, "use from __future__ import annotations or quoted annotations to avoid top-level evaluation.")
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)) and statement.value is not None and staticValue(statement.value):
            if isinstance(statement, ast.AnnAssign) and not postponed: fail(statement, "use from __future__ import annotations for annotated constants.")
            value = dataValue(statement.value)
            for name in (statement.targets if isinstance(statement, ast.Assign) else [statement.target]):
                constants.add(name.id)
                if value is not _unknown: data[name.id] = value
            continue
        fail(statement, "unsupported static initializer; use function declarations, literal/functional constants, primitive + expressions, proved own data properties and approved standard imports only; no top-level execution.")
    required = "create" if kind == "package" else "cases"
    if required not in declared: raise ValueError(f"{kind}: missing {required}; declare {'def create(computer): ...' if kind == 'package' else 'cases = [...]' }.")
    for node in ast.walk(source):
        if isinstance(node, ast.Name) and node.id in _blocked: fail(node, f"{node.id} is outside this self-contained UI package format.")
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in source.body: fail(node, "dynamic imports are not supported; use approved top-level standard imports.")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("element", "window") and node.args and isinstance(node.args[0], ast.Constant) and type(node.args[0].value) in (str, int, float):
            fail(node, "do not save literal element/window references; resolve a unique target from a fresh observation.")
    try: symbols = symtable.symtable(code, "computer-use-candidate.py", "exec")
    except SyntaxError as error: raise ValueError(f"{kind}: {error}") from error
    globals = declared | set(dir(builtins)) | {"__name__", "__doc__"}
    def checkScope(table):
        for symbol in table.get_symbols():
            if symbol.is_referenced() and symbol.is_global() and symbol.get_name() not in globals:
                raise ValueError(f"{kind} line {table.get_lineno()}: {symbol.get_name()!r} is not declared in this module or the standard Python globals.")
        for child in table.get_children(): checkScope(child)
    checkScope(symbols)
