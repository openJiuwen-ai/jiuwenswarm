import json
import os
import shutil
import sys
import logging
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


# Exit codes
# 0 = config updated successfully
# 1 = no changes needed
# 2 = error occurred
def main():
    # Get the daemon.json path from command line argument
    if len(sys.argv) < 2:
        logging.error("Error: Please provide the path to daemon.json")
        sys.exit(2)

    daemon_file = sys.argv[1]
    daemon_dir = os.path.dirname(daemon_file) or "."

    # Ensure the directory exists
    os.makedirs(daemon_dir, exist_ok=True)

    # Insecure registries to be configured
    required_registries = [
        "swr.cn-southwest-2.myhuaweicloud.com",
        "swr.cn-north-4.myhuaweicloud.com"
    ]

    # Load existing configuration
    config = {}
    if os.path.isfile(daemon_file) and os.path.getsize(daemon_file) > 0:
        try:
            with open(daemon_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}

    # Check if modification is needed
    need_modify = False

    if "insecure-registries" not in config:
        config["insecure-registries"] = []
        need_modify = True

    # Add missing registries
    for reg in required_registries:
        if reg not in config["insecure-registries"]:
            config["insecure-registries"].append(reg)
            need_modify = True

    # If no changes needed, exit with code 0
    if not need_modify:
        logging.info("All required insecure registries already exist. No changes needed.")
        sys.exit(1)

    # Backup original file before modification
    if os.path.isfile(daemon_file):
        timestamp = datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_file = f"{daemon_file}.bak.{timestamp}"
        shutil.copyfile(daemon_file, backup_file)
        logging.info(f"Backed up original file to {backup_file}")

    # Write updated configuration
    try:
        with open(daemon_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logging.error(f"Failed to write daemon.json: {e}", file=sys.stderr)
        sys.exit(2)

    # Validate JSON format
    try:
        with open(daemon_file, "r", encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON generated: {e}", file=sys.stderr)
        sys.exit(2)

    # Changes applied, exit with code 0 (trigger Docker restart)
    logging.info("Insecure registries updated successfully.")
    sys.exit(0)

if __name__ == "__main__":
    main()
