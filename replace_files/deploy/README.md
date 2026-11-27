# JiuwenClaw企业级部署方案

## 简介

openYuanrong是一个Serverless分布式计算引擎，旨在为分布式应用提供高性能运行和集群资源的高效利用。基于此引擎，我们打造了JiuwenClaw企业级部署方案，通过其高性能分布式调度能力，全面满足企业对高并发、高稳定性的场景需求。

## 入门

### 前置要求

安装元戎系统和jiuwenclaw前，请确保满足以下要求：

- 操作系统：Linux（推荐Unbuntu 20.04）
- 系统架构：amd64或arm64
- 硬件资源：至少2个计算节点，每个节点最低配置为16核CPU及32GB内存
- 运行环境：已预先搭建好Kubernetes集群，搭建流程可参考官方指导：https://kubernetes.io/zh-cn/docs/setup/

您可以通过以下命令检查：

```
# 操作系统
uname -s
# 系统架构
uname -m
# CPU核心数
nproc
# 检查内存大小 (GB)
free -g | grep Mem | awk '{print $2}'
# 集群节点状态
kubectl get nodes
```

配置 Master 节点至所有 Worker 节点的免密 SSH 登录，在 Master 节点执行以下命令：

```
ssh-copy-id <Worker节点IP>
```

### 部署jiuwenclaw

- 下载openjiuwen官网提供的企业级安装包：

```

https://openjiuwen-ci.obs.cn-north-4.myhuaweicloud.com/jiuwenclaw/JiuwenClawXopenYuanrong/JiuwenClawXopenYuanrong_deployTool_<VERSION>_<ARCH>.zip
```

- 解压缩：

```
unzip JiuwenClawXopenYuanrong_deployTool_<VERSION>_<ARCH>.zip
```

- 配置选项

参考部署目录下 [.env.example](.env.example) 配置模板，按需修改环境变量、挂载路径、运行模式等自定义参数，完成业务与环境适配。

- 一键部署

```
./deploy.sh up              # 部署所有模块
./deploy.sh up all          # 部署所有模块
./deploy.sh up nfs          # 仅部署 NFS 模块
./deploy.sh up yr_claw      # 仅部署 OpenYuanRong-CLAW 模块
./deploy.sh up gateway      # 仅部署 Gateway 模块
```

- 一键卸载

```
./deploy.sh down            # 卸载所有模块
./deploy.sh down all        # 卸载所有模块
./deploy.sh down gateway    # 仅卸载 Gateway 模块
./deploy.sh down yr_claw    # 仅卸载 OpenYuanRong-CLAW 模块
./deploy.sh down nfs        # 仅卸载 NFS 模块
```
