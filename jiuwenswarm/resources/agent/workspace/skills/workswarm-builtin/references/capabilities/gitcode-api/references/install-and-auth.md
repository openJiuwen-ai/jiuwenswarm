# 安装与认证

## 版本要求

本技能只覆盖 `gitcode-api` CLI `1.2.16` 及以上版本。使用前先检查：

```bash
gitcode-api --version
```

若命令不存在或版本过低，提示用户安装或升级。

## 安装

使用 pip：

```bash
pip install -U gitcode-api
```

使用 uv 安装到当前环境：

```bash
uv pip install -U gitcode-api
```

不想改动当前环境时，可以用 `uvx` 临时运行：

```bash
uvx gitcode-api --version
```

## 认证

推荐用户在 `~/.jiuwenclaw/config/.env` 中配置 `GITCODE_ACCESS_TOKEN` 环境变量

若用户坚持临时设置环境变量（Windows环境时）：

```bat
set GITCODE_ACCESS_TOKEN=<your-token>
```

若用户坚持临时设置环境变量（MacOS / Linux）：

```bash
export GITCODE_ACCESS_TOKEN=<your-token>
```

也可以在单次命令中传入：

```bash
gitcode-api users me --api-key "$GITCODE_ACCESS_TOKEN"
```

不要在回答中暴露用户 token。需要用户提供 token 时，提醒其使用环境变量或宿主平台的 secret 输入能力。

## CLI 限制

CLI 不暴露 Python SDK 的 `decrypt` 或自定义 `http_client` 能力。如果用户需要密文 token 解密、自定义 CA、代理或特殊 httpx client，应改用 Python SDK，而不是继续用 CLI 硬绕。
