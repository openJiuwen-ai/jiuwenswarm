"""CLI：将运行时数据初始化到用户数据根目录（与 ``get_user_workspace_dir()`` 一致）。

默认根目录为 ``~/.jiuwenswarm``；若进程环境中已设置 ``JIUWENSWARM_DATA_DIR``（须为可用绝对路径，
且应在启动本脚本前注入，见 ``jiuwenswarm.utils`` 中的 ``JIUWENSWARM_DATA_DIR``），则初始化到该路径下。

无论是通过 pip/whl 安装，还是在源码目录里直接运行：
- 运行本脚本会先询问语言偏好（zh/en），写入 config 的 preferred_language；
- 同时复制 config.yaml、builtin_rules.yaml、将 ``.env.template`` 复制为 ``<用户数据根>/config/.env``、agent 模板等到 ``<用户数据根>``；
- 根据语言偏好复制多语言文件（AGENT.md、HEARTBEAT.md、IDENTITY.md、SOUL.md 等），
  源文件使用 _ZH/_EN 后缀，目标文件不带后缀。

使用方式:
- jiuwenswarm-init -f: 强制清理，删除整个用户数据根目录后重新初始化
- jiuwenswarm-init: 保留原有数据，执行迁移合并
- jiuwenswarm-init --name alice: 创建命名实例 alice
- jiuwenswarm-init -f --name alice: 强制重建命名实例 alice
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from jiuwenswarm.common.utils import get_user_home, init_user_workspace, get_user_workspace_dir
from jiuwenswarm.instance_manager import (
    create_bootstrap_env,
    get_default_instance_status,
    get_instance_config,
    get_instance_status,
    get_instance_workspace_path,
    get_instance_index,
    calculate_instance_ports,
    find_available_ports,
    is_port_available,
    collect_all_ports,
    update_instances_yaml,
    validate_instance_name,
    InstanceConfig,
)


def run_init(force: bool = False, name: Optional[str] = None) -> int:
    """Run workspace initialization.

    Args:
        force: Force clean initialization, delete entire workspace before init
        name: Named instance name (e.g., alice, bob)
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # 1. Validate instance name if provided
    if name:
        validation_error = validate_instance_name(name)
        if validation_error:
            logging.info(f"[jiuwenswarm-init] ERROR: {validation_error}")
            return 1

    # 2. Determine target workspace path and set env var
    if name:
        workspace_path = get_instance_workspace_path(name)
        logging.info(f"[jiuwenswarm-init] Creating instance: {name}")
        logging.info(f"[jiuwenswarm-init] Workspace: {workspace_path}")
    else:
        workspace_path = get_user_home() / ".jiuwenswarm"
        logging.info(f"[jiuwenswarm-init] Initializing default workspace")
        logging.info(f"[jiuwenswarm-init] Workspace: {workspace_path}")

    # 3. Check if instance is running (for named instances, always check)
    if name:
        # For named instance, check if it's running
        config = get_instance_config(name)
        if config is None:
            # Instance not in instances.yaml yet, use default config
            workspace_path = get_instance_workspace_path(name)
            ports = calculate_instance_ports(1)  # Will be recalculated when added to yaml
            config = InstanceConfig(name=name, workspace=workspace_path, ports=ports)

        status = get_instance_status(config)
        if status.running:
            logging.info(f"[jiuwenswarm-init] ERROR: Instance '{name}' is running (PID={status.pid}).")
            logging.info(f"[jiuwenswarm-init] Stop it first with: jiuwenswarm-start --stop {name}")
            return 1
    elif force:
        # For default instance, use get_default_instance_status which includes port detection
        status = get_default_instance_status()
        if status.running:
            logging.info(f"[jiuwenswarm-init] ERROR: Default instance is running (PID={status.pid or '-'}).")
            logging.info(f"[jiuwenswarm-init] Stop it first with: jiuwenswarm-start --stop default")
            return 1

    # 4. Call init_user_workspace with workspace path
    #    (deletion and confirmation handled by init_user_workspace)
    target = init_user_workspace(overwrite=force, workspace_dir=workspace_path)

    # 5. Post-init: create bootstrap .env and update instances.yaml for named instance
    if name and target != "cancelled":
        # Calculate ports (using same index as update_instances_yaml will use)
        index = get_instance_index(name)
        ports = calculate_instance_ports(index)

        # Proactively scan for a conflict-free port group so the instance is
        # launchable immediately (otherwise the first jiuwenswarm-start --name
        # would hit a port clash and have to fall back at start time). If the
        # index's own group is fully available, keep it; otherwise scan upward.
        conflict_ports = [
            p for p in ports.values()
            if not is_port_available("127.0.0.1", p)
        ]
        if conflict_ports:
            # Exclude ports already claimed by other configured instances so
            # the scan never picks a group that collides with a sibling.
            exclude_ports = collect_all_ports(exclude_name=name)
            found = find_available_ports(
                base_index=index,
                host="127.0.0.1",
                scan_range=20,
                exclude_ports=exclude_ports,
            )
            if found is None:
                logging.info(
                    f"[jiuwenswarm-init] WARNING: No available port group within "
                    f"scan_range=20 (from index {index}). Falling back to index "
                    f"{index} ports; 'jiuwenswarm-start --name {name}' will retry."
                )
            else:
                ports, index = found
                logging.info(
                    f"[jiuwenswarm-init] Reserved conflict-free port group "
                    f"(index {index}) for instance '{name}': {ports}"
                )

        # Update YAML with full configuration (workspace + ports)
        update_instances_yaml(name, workspace_path, ports)

        # Create bootstrap .env with the same ports
        config = InstanceConfig(name=name, workspace=workspace_path, ports=ports)
        create_bootstrap_env(config)
        logging.info(f"[jiuwenswarm-init] Instance '{name}' initialized successfully.")
        return 0

    if target == "cancelled":
        return 1

    logging.info(f"[jiuwenswarm-init] initialized: {target}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize jiuwenswarm workspace directory (~/.jiuwenswarm)"
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force clean initialization: delete entire workspace before init",
    )
    parser.add_argument(
        "--name",
        type=str,
        default=None,
        help="Create a named instance workspace (e.g., alice, bob)",
    )
    # Use parse_known_args so that calling main() under pytest (which leaves
    # test paths in sys.argv) does not fail with SystemExit on unknown args.
    args, _ = parser.parse_known_args()
    return run_init(force=args.force, name=args.name)


if __name__ == "__main__":
    sys.exit(main())
