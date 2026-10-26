# Linux 跨节点 NFS 使用说明

这两个脚本用于在 Linux 节点之间共享 `jiuwenswarm` 团队共享工作空间。

适用场景：

- 一个中心节点作为 NFS server
- 一个或多个节点作为 NFS client
- 多个节点共享一个或多个工作空间目录
- 分布式 Team 场景下，只共享 `team.workspace.root_path` 指向的团队共享目录，不要共享 `.agent_teams`

默认共享目录：

```text
${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}
```

建议：

- 优先使用内网 IP
- 所有节点都先完成一次 `jiuwenswarm` 初始化
- 尽量使用同一个用户运行
- 如果要使用自定义挂载点，在所有节点设置相同的 `JIUWEN_TEAM_WORKSPACE_ROOT`

## 1. 服务端执行

在中心节点执行（可重复传入多个 `--client-ip`）：

```bash
sudo bash scripts/nfs/setup_nfs_server.sh \
  --client-ip <客户端1内网IP> \
  --client-ip <客户端2内网IP>
```



如果要自定义路径：

```bash
sudo bash scripts/nfs/setup_nfs_server.sh \
  --client-ip <客户端1内网IP> \
  --client-ip <客户端2内网IP> \
  --export-dir /mnt/jiuwenswarm/shared_workspace/jiuwen_team \
  --mount-point /mnt/jiuwenswarm/shared_workspace/jiuwen_team
```

如果只有一个客户端，保留单个 `--client-ip` 也可以。

如果要共享多个目录（每组 `--export-dir` 必须对应一组 `--mount-point`）：

```bash
sudo bash scripts/nfs/setup_nfs_server.sh \
  --client-ip <客户端1内网IP> \
  --client-ip <客户端2内网IP> \
  --export-dir /mnt/jiuwenswarm/shared_workspace/jiuwen_team \
  --mount-point /mnt/jiuwenswarm/shared_workspace/jiuwen_team \
  --export-dir /mnt/jiuwenswarm/shared_artifacts \
  --mount-point /mnt/jiuwenswarm/shared_artifacts
```

## 2. 客户端执行

在每个客户端节点执行：

```bash
sudo bash scripts/nfs/setup_nfs_client.sh --server-ip <服务端内网IP>
```



如果要自定义路径：

```bash
sudo bash scripts/nfs/setup_nfs_client.sh \
  --server-ip <服务端内网IP> \
  --export-dir /mnt/jiuwenswarm/shared_workspace/jiuwen_team \
  --mount-point /mnt/jiuwenswarm/shared_workspace/jiuwen_team
```

如果要挂载多个目录（参数成对出现）：

```bash
sudo bash scripts/nfs/setup_nfs_client.sh \
  --server-ip <服务端内网IP> \
  --export-dir /mnt/jiuwenswarm/shared_workspace/jiuwen_team \
  --mount-point /mnt/jiuwenswarm/shared_workspace/jiuwen_team \
  --export-dir /mnt/jiuwenswarm/shared_artifacts \
  --mount-point /mnt/jiuwenswarm/shared_artifacts
```

## 3. 连通性检查

在客户端执行：

```bash
rpcinfo -p <服务端内网IP>
showmount -e <服务端内网IP>
```

如果两条命令都能正常返回，就说明 NFS 服务已经可达。

## 4. 挂载后检查

在客户端执行：

```bash
mount | grep jiuwen_team
df -h | grep jiuwen_team
```

## 5. 同步验证

在服务端执行：

```bash
echo hello > "${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}/hello.txt"
```

在客户端执行：

```bash
cat "${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}/hello.txt"
```

再在客户端追加：

```bash
echo world >> "${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}/hello.txt"
```

回到服务端查看：

```bash
cat "${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}/hello.txt"
```

如果两边都能看到相同内容，就说明同步成功。

## 6. 说明

- 客户端脚本会在挂载前备份已有本地目录
- 如果有多个客户端，每个客户端都执行一次客户端脚本即可
- 支持多客户端和多目录；多目录时 `--export-dir` 与 `--mount-point` 数量必须一致
- 这套方案共享的是文件系统，不是多节点分布式运行时
- `.agent_teams` 保存 team.db、成员 workspace、symlink 等本地运行状态，不应通过 NFS 在多个 teammate 之间共享

## 7. 取消挂载与回滚

服务端回滚（删除脚本导出并重载 export）：

```bash
sudo bash scripts/nfs/teardown_nfs_server.sh
```

如果还要同时停止并禁用 NFS 服务：

```bash
sudo bash scripts/nfs/teardown_nfs_server.sh --stop-service --disable-service
```

客户端取消挂载（按挂载点）：

```bash
sudo bash scripts/nfs/teardown_nfs_client.sh \
  --server-ip <服务端内网IP> \
  --mount-point "${JIUWEN_TEAM_WORKSPACE_ROOT:-/tmp/jiuwenswarm/shared_workspace/jiuwen_team}"
```

如果要清理该服务端在 `/etc/fstab` 的全部 nfs4 记录：

```bash
sudo bash scripts/nfs/teardown_nfs_client.sh \
  --server-ip <服务端内网IP> \
  --clean-all-server-entries
```
