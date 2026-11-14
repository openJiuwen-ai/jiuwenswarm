# Gateway/Web deployment

`deploy/yuanrong/` contains the Swarm application installer and Gateway/Web lifecycle.
`deploy/adx/deploy.sh` and `package.sh` forward here for existing callers.
AgentOS installs the files under `deploy/jiuwenswarm/`.

Prepare application `config.yaml` and `.env` under `<gateway.home>/.jiuwenswarm/config/`
before starting. In AgentOS, Enterprise generates these files, migrates retained user
workspaces and prepares new-user templates. Swarm reads the prepared configuration;
its runtime binds current node routing and credentials. The installed service's dotenv
is loaded by the application itself, including on SELinux hosts.

Copy `settings.yaml` to configure service home and listener ports. Standalone listeners
remain loopback-only. AgentOS supplies its own settings and Master/VIP startup hook.

```sh
export SWARM_PYTHON=/path/to/venv/bin/python
export SWARM_SETTINGS=/path/to/service-settings.yaml
export SWARM_PACKAGE_DIR=/path/to/offline/wheels
bash deploy/yuanrong/deploy.sh install
bash deploy/yuanrong/deploy.sh up
bash deploy/yuanrong/deploy.sh status
bash deploy/yuanrong/deploy.sh restart
bash deploy/yuanrong/deploy.sh down
```

Installation expects one Core wheel and one Swarm wheel; prepare their dependencies
first. An active service is preserved by `up`; use `restart` to apply listener changes.
`service.py` reads service identity and ports without modifying application or user
configuration. `down`, `status` and `uninstall` resolve only service identity, so missing
ADX startup credentials do not prevent cleanup. Callers may supply `SWARM_HOME` and
`SWARM_INSTANCE_NAME` explicitly; an empty instance name selects the default services.
Optional `SWARM_BINDINGS` expands deployment variables, while `SWARM_SERVICE_PREFLIGHT`
is the composer's executable startup check.

```sh
bash deploy/yuanrong/package.sh /absolute/output/deploy.tar.gz
```

The archive contains the lifecycle implementation and compatibility entrypoints.
Enterprise adds its configuration and migration helpers when assembling AgentOS.
