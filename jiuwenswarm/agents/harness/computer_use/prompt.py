"""Routing and the bundled engine's actual SDK contract."""

from pathlib import Path
import sys


def engine_root() -> Path:
    return Path(__file__).resolve().parents[3] / "cua_engine"


def build_system_prompt(language: str, *, repl_enabled: bool = True) -> str:
    if language == "cn":
        text = (
            "你是专用桌面子代理，负责通过真实应用界面完成用户的完整目标。"
            "简单观察或一次明确操作，可以使用现有 list_apps/list_windows/get_window_state、"
            "点击、输入、拖动和快捷键等工具。它们通过截图观察并使用真实前台输入。"
            "操作后读取界面验证，动作成功不等于任务完成。网页任务交回浏览器代理；"
            "Electron 桌面应用仍由你操作。不要触碰任务无关的窗口。\n"
        )
        workflow = (
            "当整体任务需要系统地完成多步工作流，例如打开文件、查找、填写、修改并保存，"
            "优先使用 cua_repl，以 Python 组织连续步骤、循环和条件判断。"
            "不要把每个细小步骤拆回单步工具。简单任务也可以使用 cua_repl，尤其是需要"
            "无障碍树定位时。macOS 单步工具也可用，截图不会主动激活窗口；写入按前台输入语义执行。\n"
        )
    else:
        text = (
            "You are a desktop subagent completing the user's full objective through real application UI. "
            "For a simple observation or one explicit action, the existing list_apps/list_windows/"
            "get_window_state and click, input, drag and key tools are appropriate. They use screenshots "
            "and real foreground input. Observe after acting; dispatch is not task completion. "
            "Return web tasks to the browser agent; Electron desktop apps are your responsibility. "
            "Do not touch unrelated windows.\n"
        )
        workflow = (
            "Prefer cua_repl for a systematic multi-step workflow: opening a file, searching, filling "
            "fields, editing and saving, or using loops and conditions. Keep the workflow in Python "
            "instead of switching back to individual tools for every small step. Simple tasks may also "
            "use cua_repl when accessibility targeting is useful or the old backend is unavailable. "
            "macOS also supports the individual tools; captures do not activate windows, while input uses foreground semantics.\n"
        )
    if not repl_enabled:
        return text + single_step_policy()
    platform_skill = {"darwin": "computer-use-macos", "win32": "computer-use-windows"}.get(sys.platform)
    skill = (engine_root() / "skills" / platform_skill / "SKILL.md" if platform_skill
             else engine_root() / "skills/computer-use/base.md")
    base = skill.read_text(encoding="utf-8")
    if base.startswith("---\n"):
        base = base.split("---", 2)[2].lstrip()
    before, separator, after = base.partition("## Session cleanup")
    if separator:
        _, api, remainder = after.partition("## API surface")
        if not api:
            raise ValueError("The bundled Computer Use Skill has no API surface")
        base = before + "## API surface" + remainder
    base = base.replace("computer_use({", "cua_repl({")
    return text + workflow + single_step_policy() + (
        "\nThe cua_repl language is Python, not JavaScript. Its namespace belongs to this "
        "delegated task and persists between its tool calls. The host closes it when the task returns; "
        "a resumed delegation must recreate variables and observe again. There is no model-facing "
        "reset tool. Ordinary Python runs with the host user's permissions, not in a security sandbox. "
        "Use the SDK for UI work, not shell/AppleScript/pyautogui or direct business-file edits.\n"
        "Never reuse old-tool element indices, window ids, or screenshot coordinates in the REPL, "
        "or vice versa. Re-observe with the chosen backend after switching. REPL input follows the "
        "host's shared/exclusive policy and does not automatically escalate or replay failed input. "
        "If a new image is needed to decide coordinates, return it, inspect it, then act in a later call. "
        "Use printImage for screenshots. Input actions do not automatically observe. "
        "Read explicitly after each meaningful action group. User-input pausing is checked at tool "
        "boundaries; do not ask the user to share keyboard/mouse control during a running script.\n\n"
    ) + base


def single_step_policy():
    return (
        "\nOn macOS, single-step tools use our Swift desktop backend. Its separate session defaults to "
        "exclusive foreground input; an explicit COMPUTER_USE_DESKTOP is honored. Shared mode allows "
        "observation but refuses this global-input route; use App/Element input when available. "
        "REPL policy is independent and is never changed by a single-step call. get_window_state/zoom "
        "capture the specified window without activation. Coordinates are Retina-normalized PNG pixels; "
        "get_screen_size describes the virtual screenshot canvas. Use cmd/super for Command.\n"
        if sys.platform == "darwin" else ""
    )
