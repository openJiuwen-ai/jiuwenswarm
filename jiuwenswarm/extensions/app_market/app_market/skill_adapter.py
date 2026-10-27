from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime

from .models import AppManifest, platform_key

logger = logging.getLogger(__name__)


class SkillAdapter:
    """WorkSwarm 应用 Skill 适配器 — 提供清单模板、脚本模板和状态协议模板。

    负责将应用目录和配置选项转换为标准化的 AppManifest，
    并为不同平台生成启停脚本和状态上报逻辑。
    """

    def __init__(self, logger=None):
        self._logger = logger or logging.getLogger(__name__)

    def adapt(self, app_dir: str, options: dict) -> AppManifest:
        """从选项生成应用清单。

        根据 run_type 生成不同类型的清单：
        - "static": 静态 Web 应用，page_entry_path 默认 "index.html"，interpreter 为 None
        - "managed": 受管本地服务，需要 start_script/stop_script

        Args:
            app_dir: 应用目录路径
            options: 应用配置选项，可包含 app_id, name, version, run_type,
                     default_port, status_mode, page_entry_path, interpreter,
                     args, work_dir, permissions, source_session

        Returns:
            AppManifest 对象
        """
        app_id = options.get("app_id", "")
        name = options.get("name", app_id)
        version = options.get("version", "1.0.0")
        run_type = options.get("run_type", "static")
        default_port = options.get("default_port", 8080)
        status_mode = options.get("status_mode", "pull")
        page_entry_path = options.get("page_entry_path")
        interpreter = options.get("interpreter")
        args = options.get("args", [])
        work_dir = options.get("work_dir", app_dir)
        permissions = options.get("permissions", [])
        source_session = options.get("source_session")

        # 根据 run_type 设置默认值
        if run_type == "static":
            # 静态 Web 应用
            if page_entry_path is None:
                page_entry_path = "index.html"
            interpreter = None
        elif run_type == "managed":
            # 受管本地服务
            if interpreter is None:
                interpreter = "python"

        # 生成启停脚本
        scripts = self.generate_scripts(app_dir, run_type, status_mode)

        # 构建平台映射
        pk = platform_key()
        start_script = {pk: scripts["start"]}
        stop_script = {pk: scripts["stop"]}

        manifest = AppManifest(
            app_id=app_id,
            name=name,
            version=version,
            description=options.get("description", ""),
            icon_url=options.get("icon_url", ""),
            run_type=run_type,
            page_entry_path=page_entry_path,
            start_script=start_script,
            stop_script=stop_script,
            interpreter=interpreter,
            args=args,
            work_dir=work_dir,
            default_port=default_port,
            status_mode=status_mode,
            permissions=permissions,
            owner_id=options.get("owner_id", ""),
            source_session=source_session,
            created_at=datetime.now().isoformat(),
        )

        self._logger.info(
            "已生成应用清单: app_id=%s, name=%s, run_type=%s", app_id, name, run_type
        )
        return manifest

    def generate_scripts(self, app_dir: str, run_type: str, status_mode: str) -> dict:
        """在 app_dir 下生成启停脚本。

        根据 platform_key() 生成对应平台的脚本：
        - Windows: start.ps1, stop.ps1 (PowerShell 脚本)
        - macOS/Linux: start.sh, stop.sh (Shell 脚本)

        脚本模板功能：
        - 读取环境变量 WORKSWARM_APP_PORT (兼容 PORT) 作为端口
        - 读取 WORKSWARM_INSTANCE_CONTEXT 作为实例上下文路径
        - push 模式下读取 WORKSWARM_STATUS_REPORT_URL 和 WORKSWARM_STATUS_TOKEN
        - managed 类型启动后上报 running 状态
        - static 类型仅启动 HTTP 服务器托管静态文件

        Args:
            app_dir: 应用目录路径
            run_type: 运行类型 ("static" | "managed")
            status_mode: 状态模式 ("push" | "pull")

        Returns:
            {"start": "scripts/start.ps1", "stop": "scripts/stop.ps1"}
            路径根据平台变化（.sh 或 .ps1）
        """
        scripts_dir = os.path.join(app_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)

        pk = platform_key()

        if pk == "windows":
            start_file = "start.ps1"
            stop_file = "stop.ps1"
            start_content = self._generate_windows_start(run_type, status_mode)
            stop_content = self._generate_windows_stop()
        else:
            start_file = "start.sh"
            stop_file = "stop.sh"
            start_content = self._generate_unix_start(run_type, status_mode)
            stop_content = self._generate_unix_stop()

        start_path = os.path.join(scripts_dir, start_file)
        stop_path = os.path.join(scripts_dir, stop_file)

        try:
            with open(start_path, "w", encoding="utf-8") as f:
                f.write(start_content)
            with open(stop_path, "w", encoding="utf-8") as f:
                f.write(stop_content)

            # Unix 脚本需要可执行权限
            if pk != "windows":
                os.chmod(start_path, 0o755)
                os.chmod(stop_path, 0o755)
        except OSError as e:
            self._logger.error("写入脚本文件失败: %s", e)
            raise

        self._logger.info("已生成启停脚本: %s, %s", start_file, stop_file)
        return {"start": f"scripts/{start_file}", "stop": f"scripts/{stop_file}"}

    def _generate_windows_start(self, run_type: str, status_mode: str) -> str:
        if run_type != "static":
            return self._legacy_windows_start(run_type, status_mode)
        return ('$ErrorActionPreference = "Stop"\n'
                '$appServer = Join-Path $PSScriptRoot \"app_server.py\"\n'
                'if (Test-Path $appServer) { & $env:WORKSWARM_PYTHON $appServer } else { & $env:WORKSWARM_PYTHON $env:WORKSWARM_STATIC_SERVER }\n'
                'exit $LASTEXITCODE\n')

    def _generate_unix_start(self, run_type: str, status_mode: str) -> str:
        if run_type != "static":
            return self._legacy_unix_start(run_type, status_mode)
        return '#!/bin/sh\nset -e\napp_server="$(dirname "$0")/app_server.py"\nif [ -f "$app_server" ]; then exec "$WORKSWARM_PYTHON" "$app_server"; fi\nexec "$WORKSWARM_PYTHON" "$WORKSWARM_STATIC_SERVER"\n'

    def upgrade_static_scripts(self, manifest: AppManifest) -> None:
        """Replace only the original generated static start template."""
        if manifest.run_type != "static":
            return
        from pathlib import Path
        pk = platform_key()
        relative = manifest.start_script.get(pk)
        if not relative:
            return
        path = Path(manifest.work_dir) / relative
        legacy = self._legacy_windows_start if pk == "windows" else self._legacy_unix_start
        try:
            original = path.read_text(encoding="utf-8") if path.is_file() else ""
        except UnicodeError:
            return
        if original == legacy("static", manifest.status_mode):
            content = self._generate_windows_start if pk == "windows" else self._generate_unix_start
            path.write_text(content("static", manifest.status_mode), encoding="utf-8")

    def _legacy_windows_start(self, run_type: str, status_mode: str) -> str:
        """生成 Windows PowerShell 启动脚本内容。"""
        lines = [
            "$ErrorActionPreference = \"Stop\"",
            "",
            "# 读取端口（兼容 PORT 环境变量）",
            "$port = $env:WORKSWARM_APP_PORT",
            "if (-not $port) { $port = $env:PORT }",
            "if (-not $port) { $port = \"8080\" }",
            "",
            "# 读取实例上下文路径",
            "$contextPath = $env:WORKSWARM_INSTANCE_CONTEXT",
            "",
        ]

        if run_type == "static":
            lines.extend(
                [
                    "# 静态应用：启动 HTTP 服务器托管静态文件",
                    "$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path",
                    "$webRoot = Split-Path -Parent $scriptDir",
                    "",
                    "Write-Host \"启动静态 HTTP 服务器，端口: $port，根目录: $webRoot\"",
                    "",
                    "# 使用 Python 内置 HTTP 服务器托管静态文件",
                    "python -m http.server $port --directory $webRoot",
                ]
            )
        else:
            # managed 类型
            lines.extend(
                [
                    "# 读取状态上报配置（push 模式）",
                    "$reportUrl = $env:WORKSWARM_STATUS_REPORT_URL",
                    "$statusToken = $env:WORKSWARM_STATUS_TOKEN",
                    "",
                    "# 启动应用服务",
                    "$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path",
                    "$appDir = Split-Path -Parent $scriptDir",
                    "",
                    "Write-Host \"启动受管应用服务，端口: $port，工作目录: $appDir\"",
                    "",
                    "# 启动应用（此处为占位，实际启动命令由应用配置指定）",
                    "# python \"$appDir/app.py\" --port $port",
                    "",
                ]
            )
            if status_mode == "push":
                lines.extend(
                    [
                        "# 上报 running 状态（push 模式）",
                        "if ($reportUrl -and $statusToken) {",
                        "    $body = @{ state = \"running\"; detail = \"应用已启动\" } | ConvertTo-Json",
                        "    try {",
                        "        Invoke-RestMethod -Uri $reportUrl -Method Post \\",
                        "            -Headers @{ Authorization = \"Bearer $statusToken\" } \\",
                        "            -Body $body -ContentType \"application/json\"",
                        "        Write-Host \"已上报 running 状态\"",
                        "    } catch {",
                        "        Write-Warning \"状态上报失败: $_\"",
                        "    }",
                        "}",
                    ]
                )

        return "\n".join(lines) + "\n"

    def _generate_windows_stop(self) -> str:
        """生成 Windows PowerShell 停止脚本内容。"""
        return (
            "$ErrorActionPreference = \"Stop\"\n"
            "\n"
            "# 读取实例上下文\n"
            "$contextPath = $env:WORKSWARM_INSTANCE_CONTEXT\n"
            "\n"
            "# 停止应用进程\n"
            "$port = $env:WORKSWARM_APP_PORT\n"
            "if (-not $port) { $port = $env:PORT }\n"
            "\n"
            "if ($port) {\n"
            "    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue\n"
            "    foreach ($conn in $connections) {\n"
            "        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue\n"
            "    }\n"
            "}\n"
            "\n"
            "# 清理临时资源\n"
            "if ($contextPath -and (Test-Path \"$contextPath\\tmp\")) {\n"
            "    Remove-Item -Path \"$contextPath\\tmp\\*\" -Recurse -Force -ErrorAction SilentlyContinue\n"
            "}\n"
            "\n"
            "Write-Host \"应用已停止\"\n"
        )

    def _legacy_unix_start(self, run_type: str, status_mode: str) -> str:
        """生成 Unix Shell 启动脚本内容。"""
        lines = [
            "#!/bin/bash",
            "set -e",
            "",
            "# 读取端口（兼容 PORT 环境变量）",
            'PORT="${WORKSWARM_APP_PORT:-${PORT:-8080}}"',
            "",
            "# 读取实例上下文路径",
            'CONTEXT_PATH="${WORKSWARM_INSTANCE_CONTEXT}"',
            "",
        ]

        if run_type == "static":
            lines.extend(
                [
                    "# 静态应用：启动 HTTP 服务器托管静态文件",
                    'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"',
                    'WEB_ROOT="$(dirname "$SCRIPT_DIR")"',
                    "",
                    'echo "启动静态 HTTP 服务器，端口: $PORT，根目录: $WEB_ROOT"',
                    "",
                    "# 使用 Python 内置 HTTP 服务器托管静态文件",
                    'python3 -m http.server "$PORT" --directory "$WEB_ROOT"',
                ]
            )
        else:
            # managed 类型
            lines.extend(
                [
                    "# 读取状态上报配置（push 模式）",
                    'REPORT_URL="${WORKSWARM_STATUS_REPORT_URL}"',
                    'STATUS_TOKEN="${WORKSWARM_STATUS_TOKEN}"',
                    "",
                    "# 启动应用服务",
                    'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"',
                    'APP_DIR="$(dirname "$SCRIPT_DIR")"',
                    "",
                    'echo "启动受管应用服务，端口: $PORT，工作目录: $APP_DIR"',
                    "",
                    "# 启动应用（此处为占位，实际启动命令由应用配置指定）",
                    '# python3 "$APP_DIR/app.py" --port "$PORT" &',
                    "",
                ]
            )
            if status_mode == "push":
                lines.extend(
                    [
                        "# 上报 running 状态（push 模式）",
                        'if [ -n "$REPORT_URL" ] && [ -n "$STATUS_TOKEN" ]; then',
                        '    curl -s -X POST "$REPORT_URL" \\',
                        '        -H "Authorization: Bearer $STATUS_TOKEN" \\',
                        '        -H "Content-Type: application/json" \\'
                        '        -d "{\\"state\\":\\"running\\",\\"detail\\":\\"应用已启动\\"}" || true',
                        '    echo "已上报 running 状态"',
                        "fi",
                    ]
                )

        return "\n".join(lines) + "\n"

    def _generate_unix_stop(self) -> str:
        """生成 Unix Shell 停止脚本内容。"""
        return (
            "#!/bin/bash\n"
            "\n"
            "# 读取实例上下文\n"
            'CONTEXT_PATH="${WORKSWARM_INSTANCE_CONTEXT}"\n'
            "\n"
            "# 停止应用进程\n"
            'PORT="${WORKSWARM_APP_PORT:-${PORT:-}}"\n'
            "\n"
            'if [ -n "$PORT" ]; then\n'
            '    PID=$(lsof -ti :"$PORT" 2>/dev/null || true)\n'
            '    if [ -n "$PID" ]; then\n'
            "        kill -9 $PID 2>/dev/null || true\n"
            "    fi\n"
            "fi\n"
            "\n"
            "# 清理临时资源\n"
            'if [ -n "$CONTEXT_PATH" ] && [ -d "$CONTEXT_PATH/tmp" ]; then\n'
            '    rm -rf "$CONTEXT_PATH/tmp/"* 2>/dev/null || true\n'
            "fi\n"
            "\n"
            'echo "应用已停止"\n'
        )

    def validate(self, manifest: AppManifest, app_dir: str) -> bool:
        """验证应用清单和脚本。

        检查项：
        - start_script 和 stop_script 文件是否存在
        - interpreter 是否可用（如果指定了）
        - default_port 是否在有效范围 (1024-65535)

        Args:
            manifest: 应用清单对象
            app_dir: 应用目录路径

        Returns:
            True 表示验证通过，False 表示验证失败
        """
        pk = platform_key()

        # 检查 start_script 文件是否存在
        start_rel = manifest.start_script.get(pk)
        if start_rel:
            start_path = os.path.join(app_dir, start_rel)
            if not os.path.isfile(start_path):
                self._logger.error("启动脚本不存在: %s", start_path)
                return False
        else:
            self._logger.error("当前平台 %s 无启动脚本配置", pk)
            return False

        # 检查 stop_script 文件是否存在
        stop_rel = manifest.stop_script.get(pk)
        if stop_rel:
            stop_path = os.path.join(app_dir, stop_rel)
            if not os.path.isfile(stop_path):
                self._logger.error("停止脚本不存在: %s", stop_path)
                return False
        else:
            self._logger.error("当前平台 %s 无停止脚本配置", pk)
            return False

        # 检查 interpreter 是否可用（如果指定了）
        if manifest.interpreter:
            if shutil.which(manifest.interpreter) is None:
                self._logger.error("解释器不可用: %s", manifest.interpreter)
                return False

        # 检查 default_port 是否在有效范围
        if not (1024 <= manifest.default_port <= 65535):
            self._logger.error(
                "默认端口不在有效范围 (1024-65535): %d", manifest.default_port
            )
            return False

        self._logger.info("应用清单验证通过: app_id=%s", manifest.app_id)
        return True
