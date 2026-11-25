# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Start the desktop Gateway with Swarm's local configuration defaults."""

import os


def main() -> None:
    # Resolve the instance before either workspace paths or Gateway config are loaded.
    from agent_gateway.dotenv_early import parse_dotenv_early

    parse_dotenv_early()

    from jiuwenswarm.common.utils import prepare_local_gateway_config

    env = os.environ.copy()
    prepare_local_gateway_config(env)
    os.environ.update(env)

    from agent_gateway.app_gateway import main as gateway_main

    gateway_main()


if __name__ == "__main__":
    main()
