# JiuwenSwarm 企业级部署工具使用手册

JiuwenSwarm 企业级部署工具是适配 Kubernetes 集群的一站式自动化部署运维工具，专注于 JiuwenSwarm 企业版服务的快速搭建与全生命周期运维管理，有效解决了传统部署模式流程繁琐、环境适配复杂、运维操作碎片化、多组件协同部署易出错、标准化程度低等行业痛点。

该工具深度适配企业级集群交付场景，内置 MinIO 对象存储、PostgreSQL/MySQL 数据库、Redis 缓存等全套基础依赖组件的一键标准化部署能力，同时支持灵活对接外置 OBS 存储、独立数据库与缓存服务，适配多样化企业存量架构。工具高度封装部署、卸载、重启等核心运维操作，无需用户手动编写复杂的 Kubernetes YAML 资源文件，大幅降低操作门槛。此外，工具支持基于命名空间的多实例隔离部署，可自定义服务端口与各类环境参数，兼顾部署规范性与场景灵活度，能够有效提升集群搭建效率、统一企业运维标准，全面适配大规模、高可用、多租户的生产级集群运行环境。

## 1. 环境准备

### 1.1 基础环境要求

**安装JiuwenSwarm前，请确保满足以下要求：**

- 操作系统：Linux（推荐 Unbuntu 20.04）
- CPU 架构：**集群内所有节点架构必须一致，统一为 AMD64 或统一为 ARM64，禁止混合架构部署**
- 硬件资源：至少2个计算节点，每个节点最低配置为16核CPU、32GB物理内存
- 运行环境：已预先搭建好 Kubernetes 集群，搭建流程可参考官方指导：https://kubernetes.io/zh-cn/docs/setup/


**执行以下命令逐一校验节点环境信息：**

```
# 查看操作系统
uname -s

# 查看 CPU 架构
uname -m

# 查看 CPU 核心数量
nproc

# 查看总内存大小（单位GB）
free -g | grep Mem | awk '{print $2}'

# 校验K8s集群节点就绪状态
kubectl get nodes
```

### 1.2 预装软件依赖

| 工具 | 版本要求 | 校验范围  |校验方法|核心用途|
|------|----------|----------|----------|----------|
| yq | mikefarah/yq v4+ Go 版本 | 仅部署节点 |运行`yq --version`检查其版本是否符合要求|解析、修改 YAML 配置文件|
| jq | 无强制版本限制 | 仅部署节点 |执行 `which jq` 命令，校验其是否已安装| 解析并筛选 JSON 数据，提取所需字段|

### 1.3 下载部署工具

在`部署节点`执行以下操作，下载并解压官方部署工具安装包，工具下载地址：

```
https://openjiuwen-ci.obs.cn-north-4.myhuaweicloud.com/jiuwenclaw/JiuwenClaw/product/JiuwenClaw_deployTool_<VERSION>_<ARCH>_product.zip
```

解压命令：

```
unzip ***.zip
```

后续所有部署、运维命令，均需进入解压后的工具目录执行。

### 1.4 部署工具目录结构说明
部署工具解压后完整目录结构及各文件/目录用途说明如下，业务配置统一在配置文件`.env.custom` 中调整，其他文件非必要不修改，：

```
JiuwenClaw_deployTool_<VERSION>_<ARCH>_product/
├── .env.example                          # 配置文件的参数说明书
├── .env.custom                           # 配置文件（需用户手动修改）
├── README.md                             # 部署工具本地说明文档
├── deploy.sh                             # 部署工具运行入口脚本
├── args_handler.sh                       # 命令行参数解析与校验脚本
├── check_handler.sh                      # 环境、依赖、集群状态校验脚本
├── cmd_handler.sh                        # 底层命令封装与执行脚本
├── common.sh                             # 公共工具函数库
├── envfile_handler.sh                    # 处理环境变量文件脚本
├── global_vars.sh                        # 全局常量、默认参数定义脚本
├── gateway_handler.sh                    # Gateway网关模块部署、运维处理脚本
├── runtime_handler.sh                    # AgentRuntime 运行时模块部署、运维处理脚本
├── k8s_handler.sh                        # Kubernetes 集群资源操作核心脚本
├── minio_handler.sh                      # MinIO 对象存储模块部署运维脚本
├── mysql_handler.sh                      # MySQL 数据库模块部署运维脚本
├── ports_handler.sh                      # 端口分配、校验、冲突检测管理脚本
├── postgresql_handler.sh                 # PostgreSQL 数据库模块部署运维脚本
├── redis_handler.sh                      # Redis 缓存模块部署运维脚本
├── nfs_handler.sh                        # NFS 模块部署运维脚本
├── template_handler.sh                   # Kubernetes 模板文件渲染、配置生成脚本
├── update_conf.sh                        # 配置更新、重载处理脚本
├── web_handler.sh                        # Web 前端模块部署、运维脚本
├── log_handler.sh                        # 日志管理模块部署、运维脚本
├── configmap_secret_handler.sh           # 处理存放密码的ConfigMap的脚本
└── templates/                            # 所有 Kubernetes 资源模板配置目录
    ├── gateway-config-jiuwen.template.yaml # 网关业务配置模板
    ├── gateway.template.env                # 网关环境变量配置模板
    ├── gateway.template.yaml               # 网关 Kubernetes 部署资源模板
    ├── agentserver.template.json           # AgentServer 服务模板
    ├── agentserver.template.env            # AgentServer 环境变量配置模板
    ├── runtime.template.yaml               # AgentRuntime 运行时 Kubernetes 资源模板
    ├── minio.template.yaml                 # MinIO 存储 Kubernetes 资源模板
    ├── mysql.template.yaml                 # MySQL 数据库 Kubernetes 资源模板
    ├── nfs.template.yaml                   # NFS 存储 Kubernetes 资源模板
    ├── nfs-sc.template.yaml                # NFS 存储供给 Kubernetes 资源模板
    ├── claw-pvc.template.yaml              # 业务内置持久卷PVC资源清单模板
    ├── postgresql.template.yaml            # PostgreSQL 数据库 Kubernetes 资源模板
    ├── redis.template.yaml                 # Redis 缓存 Kubernetes 资源模板
    ├── log.template.yaml                   # 日志模块 Kubernetes 资源模板
    ├── configmap-secret.template.yaml      # 专门存放密码的ConfigMap 资源模板
    └── web.template.yaml                   # Web 前端 Kubernetes 部署资源模板
```

### 1.5 修改配置文件`.env.custom`

修改配置前，请先查阅配置文件参数说明书 `.env.example`，明确各配置项含义与使用规则；再结合自身实际环境，按需调整配置文件`.env.custom`的参数，完成部署环境与业务场景适配。

以下为系统运行必需参数，需依据实际大模型服务凭证完整填写：
```
# ====================== 大模型接口配置 ======================
# 模型厂商标识（如：OpenAI等）
MODEL_PROVIDER=""

# 大模型名称
MODEL_NAME=""

# 大模型API基础地址
API_BASE=""

# 大模型鉴权密钥
API_KEY=""

```

## 2 部署工具命令行介绍

**命令格式:**
```
./deploy.sh [操作命令(必填)] [模块列表(选填)] [配置参数(选填)]
```

### 2.1 操作命令（必填）

**部署工具支持三种核心操作，用于管理服务生命周期：**
- **up**：部署并启动指定的业务模块
- **down**：停止并卸载指定的业务模块
- **restart**：重启指定的业务模块

**基础用法**（当未指定模块参数时，部署工具默认操作所有业务模块）：
```
./deploy.sh up       # 部署 Gateway、Web、AgentRuntime 模块（AgentServer 由 AgentRuntime 拉起）
./deploy.sh down     # 卸载 AgentRuntime、Web、Gateway 模块
./deploy.sh restart  # 重启 Gateway、Web、AgentRuntime 模块
```

### 2.2 模块列表（选填）

**部署工具支持对以下独立模块进行精细化管理：**
- **nfs**：NFS 存储服务模块
- **nfs-sc**：NFS 存储供给模块
- **mysql**：MySQL 存储服务模块
- **postgresql**：PostgreSQL 存储服务模块
- **minio**：Minio 存储服务模块
- **log**：日志管理模块
- **gateway**：Gateway 模块
- **web**：Web 前端页面服务模块
- **runtime**：AgentRuntime 运行时模块，负责按需创建与管理 AgentServer Pod

**单模块操作示例：**
```
./deploy.sh [操作命令] nfs          # 仅操作 NFS 存储模块
./deploy.sh [操作命令] nfs-sc       # 仅操作 NFS 存储供给模块
./deploy.sh [操作命令] mysql        # 仅操作 MySQL 模块
./deploy.sh [操作命令] postgresql   # 仅操作 PostgreSQL 模块
./deploy.sh [操作命令] minio        # 仅操作 MinIO 模块
./deploy.sh [操作命令] log          # 仅操作日志管理模块
./deploy.sh [操作命令] gateway      # 仅操作 Gateway 模块
./deploy.sh [操作命令] web          # 仅操作 Web 模块
./deploy.sh [操作命令] runtime      # 仅操作 AgentRuntime 模块
```

**重要约束：**

- **NFS / MySQL / PostgreSQL / MinIO / Log：** 以上基础依赖模块全局仅支持单次部署，固定运行于 default 命名空间，部署命令自动忽略自定义命名空间参数。
- **Redis：** 不作为独立模块部署，仅作为 Gateway、Runtime 的附属依赖。每个命名空间拥有独立的 Redis 实例（Deployment 名为 `jiuwenclaw-redis`），实现多业务实例间数据隔离。启动 Gateway 或 Runtime 等依赖 Redis 的业务模块时，部署工具会自动执行就绪检查：已配置外挂 Redis 则复用外部服务；否则复用同命名空间已有的内置 Redis；若同命名空间既无外挂 Redis 也无内置 Redis，则自动拉起一个内置 Redis 实例。
- **Web / Gateway / Runtime：** 业务服务模块需保持命名空间一致（为了环境隔离与日志运维，禁止使用default命令空间），否则服务间网络互通异常、功能不可用。

**使用示例：**

```
./deploy.sh up nfs        # 启动 NFS 存储模块（只需一次）
./deploy.sh up nfs-sc     # 启动 NFS 存储供给模块（只需一次）
./deploy.sh up mysql      # 启动 MySQL 存储模块（只需一次）
./deploy.sh up postgresql # 启动 PostgreSQL 存储模块（只需一次）
./deploy.sh up minio      # 启动 MinIO 存储模块（只需一次）
./deploy.sh up log        # 启动日志管理服务模块（只需一次）
./deploy.sh up gateway    # 启动 Gateway 服务模块
./deploy.sh up web        # 启动 Web 前端模块
./deploy.sh up runtime    # 启动 AgentRuntime 运行时模块（负责拉起 AgentServer）


./deploy.sh down web        # 卸载 Web 前端模块（按需卸载）
./deploy.sh down gateway    # 卸载 Gateway 服务模块（按需卸载）
./deploy.sh down runtime    # 卸载 AgentRuntime 运行时模块（按需卸载）
./deploy.sh down log        # 卸载日志管理服务模块（非必要不卸载）
./deploy.sh down minio      # 卸载 MinIO 存储模块（非必要不卸载
./deploy.sh down postgresql # 卸载 PostgreSQL 存储模块（非必要不卸载）
./deploy.sh down mysql      # 卸载 MySQL 存储模块（非必要不卸载）
./deploy.sh down nfs-sc     # 卸载 NFS 存储供给模块（非必要不卸载）
./deploy.sh down nfs        # 卸载 NFS 存储模块（非必要不卸载）

./deploy.sh restart nfs-sc      # 重启 NFS 存储供给模块（非必要不重启）
./deploy.sh restart nfs         # 重启 NFS 存储模块（非必要不重启）
./deploy.sh restart mysql       # 重启 MySQL 存储模块（非必要不重启）
./deploy.sh restart postgresql  # 重启 PostgreSQL 存储模块（非必要不重启）
./deploy.sh restart minio       # 重启 MinIO 存储模块（非必要不重启）
./deploy.sh restart log         # 重启日志管理模块（非必要不重启）
./deploy.sh restart gateway     # 重启 Gateway 服务模块（按需重启）
./deploy.sh restart web         # 重启 Web 前端模块（按需重启）
./deploy.sh restart runtime     # 重启 AgentRuntime 运行时模块（按需重启）
```

**重要说明：**
每当升级新版本服务时，对于**NFS、NFS-SC、MySQL、PostgreSQL、MinIO、Log** 等全局基础依赖组件应尽量保持不变，无需重复部署。**Redis** 已改为按命名空间独立部署、随业务实例隔离（不支持单独部署），升级业务模块时各命名空间下的 Redis 实例保持不变即可。仅需对业务服务**Gateway、Web、AgentRuntime**进行版本替换：在旧版本部署目录中，依次卸载 业务模块；随后切换至新版本部署工具目录，启动对应新版业务模块。

### 2.3 配置参数（选填）

**参数说明：**
- `-n`:  指定部署目标命名空间, 从而实现模块多实例隔离部署，不同命名空间的资源不冲突，默认值：`default`。需要注意的是：操作 NFS / MySQL / PostgreSQL / MinIO / Log 等基础依赖模块时，该参数强制失效，固定部署于 `default` 命名空间；**Redis** 随业务模块按 `-n` 指定的命名空间自动部署（不支持单独部署）。
- `--render-only`：只渲染模板输出文件至 conf 目录，不操作集群、不校验集群资源

**参数使用示例：**
```
./deploy.sh up -n test-ns                    # 部署核心模块至 test-ns 命名空间
./deploy.sh up web -n test-ns                # 部署 Web 模块至 test-ns 命名空间, 自动分配空闲端口
./deploy.sh up nfs -n test-ns                # -n 参数无效，NFS 仍部署于 default 空间
```

## 3 部署基础依赖服务

所有基础依赖服务仅需全局部署一次。

### 3.1 部署 NFS 服务

#### 3.1.1 工具内置部署 NFS 服务（开发环境可用）

本部署工具提供一键部署能力，可直接在集群内快速搭建 NFS 存储服务。部署之前，请在集群内所有节点执行以下命令逐一校验环境信息：
```
# 确认 NFS 服务端模块已加载
# lsmod | grep nfsd
nfsd                  647168  0
auth_rpcgss           139264  1 nfsd
nfs_acl                16384  2 nfsd,nfsv3
lockd                 110592  3 nfsd,nfsv3,nfs
grace                  16384  2 nfsd,lockd
sunrpc                585728  10 nfsd,auth_rpcgss,lockd,nfsv3,nfs_acl,nfs

# 确认 NFS 客户端挂载工具已安装
# which mount.nfs
/usr/sbin/mount.nfs
```

检查完成之后，请执行以下命令完成部署：
```
./deploy.sh up nfs          # 部署 NFS 存储模块（基础依赖，只需也只能一次）
```

**限制条件**：
- 本部署工具仅支持 AMD64 架构 环境下单机部署；
- ARM64 架构、高可用 / 分布式 NFS 场景，需用户自行搭建外部 NFS 服务。

#### 3.1.2 工具内置部署 NFS 存储供给组件（开发环境可用）

本部署工具提供一键部署 NFS 存储供给组件，组件底层基于 nfs-subdir-external-provisioner 实现，提供标准 K8s StorageClass，业务 Pod 可通过 PVC 动态申领独立 NFS 存储子目录，实现数据持久化挂载。
```
./deploy.sh up nfs-sc          # 部署 NF S存储供给组件（基础依赖，只需也只能一次）
```
部署完成后自动生成对应 StorageClass，搭配 CLAW_MOUNT_TYPE=pvc 模式使用，可自动创建隔离式 NFS 持久卷。


#### 3.1.3 外部独立部署 NFS 服务（生产环境推荐）
生产环境下，推荐用户自行搭建高可用的外部 NFS 服务，搭建完成后需在自定义配置文件 `.env.custom` 中填写如下参数，完成外部 NFS 服务的对接工作：

```
# 设置产品组件的存储挂载模式为nfs
CLAW_MOUNT_TYPE="nfs"

# 外部 NFS 服务的连接地址
NFS_SERVER_ADDR=""

# 外部 NFS 服务的共享根目录路径
NFS_SHARE_PATH=""
```

#### 3.1.3 复用预创建 PVC 持久卷（生产环境推荐）

若客户有其他高可用企业级存储组件，并基于其组件的 StorageClass 预创建好了持久化 PVC 资源，业务组件可直接复用现有 PVC 完成存储挂载，配置如下：
```
# 设置产品组件的存储挂载模式为pvc
CLAW_MOUNT_TYPE="pvc"

# 外部 PVC 的名字，该 PVC 需存在，且与业务 Pod 处于同一 Namespace
CLAW_PVC=""

# 外部 PVC 绑定的 StorageClass 名称
NFS_SC_NAME=""
```


### 3.2 部署数据库服务

本产品支持 PostgreSQL、MySQL 两种数据库类型，可根据具体需求二选一配置即可。选定数据库类型后，在配置文件 `.env.custom` 中指定：
```
# 数据库类型，支持 mysql、postgresql，默认 mysql
DB_TYPE="mysql"
```

**说明：** SQLite 已不再支持（企业版多 Pod 共享场景下本地 sqlite 文件无法跨 Pod 共享）；个人版单机走内存/本地，不经此部署工具。

#### 3.2.1 工具内置部署（开发环境可用）

本部署工具提供一键部署能力，可在集群内快速拉起 MySQL 或 PostgreSQL 单实例服务：
```
./deploy.sh up mysql        # 部署 MySQL 存储模块（基础依赖，只需也只能一次）
./deploy.sh up postgresql   # 部署 PostgreSQL 存储模块（基础依赖，只需也只能一次）
```

若需为该服务挂载外部 NFS 服务实现数据持久化，需在配置文件`.env.custom` 中设置：
```
# nfs：Pod直接通过地址直连NFS服务挂载共享目录
CLAW_MOUNT_TYPE="nfs"

# 外部 NFS 服务的连接地址
NFS_SERVER_ADDR=""

# 外部 NFS 服务的共享根目录路径
# 前置约束：共享目录下必须预先创建 mysql / postgresql 对应子目录用于数据存储
NFS_SHARE_PATH=""
```

**注意：** 本部署工具仅支持单实例数据库服务部署，如需高可用数据库服务，请采用外部独立部署方式。

#### 3.2.2 外部独立部署（生产环境推荐）

生产环境下，推荐用户自行搭建高可用的外部数据库服务，搭建完成后需在自定义配置文件 `.env.custom` 中填写如下参数，完成外部数据库服务的对接工作。

```
# 外部数据库服务的连接地址
DB_HOST=""

# 外部数据库服务服务的连接端口
DB_PORT=

# 外部数据库服务服务的用户名跟密码，账号权限区分规则：
# 1. 分库独立账号配置：定义 GATEWAY_DB_USER / GATEWAY_DB_PASSWORD，实现业务库账号、密码独立
# 2. 全局统一账号配置：仅配置 DB_USER、DB_PASSWORD，Gateway 使用同一套数据库访问凭证
# 优先级规则：分库专属账号变量优先级 > 全局通用账号变量；若两类变量同时配置，以分库专属账号为准，全局账号配置自动失效
DB_USER=""
DB_PASSWORD=""
GATEWAY_DB_USER=""
GATEWAY_DB_PASSWORD=""

# (仅 PostgreSQL 有效) 各模块的专属 Schema 名称, 默认为 public
GATEWAY_PG_SCHEMA=""
IDENTITY_PG_SCHEMA=""
WEB_PG_SCHEMA=""
RUNTIME_PG_SCHEMA=""
```

#### 3.2.3 多实例数据库隔离

数据库服务（MySQL/PostgreSQL）为所有业务实例共享同一台 DB Server。在多实例（多命名空间）部署场景下，部署工具会自动为每个实例隔离各模块数据，实例间互不干扰，默认无需任何手动配置。

**默认行为（推荐）**：未显式指定各模块数据库名时，部署工具按实例（命名空间）自动为每个模块分配独立的数据库，天然保证各实例数据隔离：

- **MySQL**：每个实例的每个模块使用形如 `<模块库名>_<命名空间>` 的独立数据库（如实例 `test` 的 Gateway 库为 `gateway_test`）。
- **PostgreSQL**：在同一共享数据库内，为每个实例使用以命名空间命名的独立 schema 实现隔离（如实例 `test` 使用 schema `test`）。

各模块默认库名为：Gateway→`gateway`、Identity→`identity`、Runtime→`runtime`、Web→`web`。

**自定义数据库名**：如需指定，可在 `.env.custom` 中为各模块设置对应变量（MySQL 为 `*_DB_NAME`，PostgreSQL 为 `*_PG_SCHEMA`）。一旦显式设置，部署工具将直接采用该值并不再自动分配——**请务必保证同一套部署中每个实例的各模块数据库名（或 PostgreSQL schema 名）互不相同**，否则不同实例会读写同一数据库，导致数据串台。

> 提示：如无特殊需求，建议保持默认，由部署工具自动分配，既省心又能可靠保证隔离性。

### 3.3 部署redis服务

Redis 不支持单独部署，仅作为 Gateway、Runtime 的附属依赖，随业务模块启动时按命名空间自动就绪。每个命名空间拥有独立的 Redis 实例（Deployment 名为 `jiuwenclaw-redis`），实现多业务实例间的数据隔离。

部署 Gateway 或 Runtime 等依赖 Redis 的业务模块时，部署工具会自动执行 Redis 就绪检查（`ensure_redis_up`），其行为如下：

1. 若已在 `.env.custom` 中配置外挂 Redis（`REDIS_HOST`），则直接复用外部 Redis 服务，跳过内置部署；
2. 否则检测同命名空间下是否已存在内置 Redis 实例（Deployment `jiuwenclaw-redis`），存在则直接复用；
3. 若同命名空间下既未配置外挂 Redis、也不存在内置 Redis 实例，则自动拉起一个单节点内置 Redis 实例于该命名空间。

**注意：** 内置 Redis 为单节点实例，如需高可用 Redis 服务，请采用外部独立部署方式。

#### 3.3.1 外部独立部署（生产环境推荐）

生产环境下，推荐用户自行为每套实例搭建高可用的外部 Redis 服务，搭建完成后需在自定义配置文件 `.env.custom` 中填写如下参数，完成外部 Redis 服务的对接工作。

```
# 外部 Redis 服务的连接地址
REDIS_HOST=""

# 外部 Redis 服务的连接端口
REDIS_PORT=

# 外部 Redis 服务的密码
REDIS_PASSWORD=""

```

### 3.4 部署对象存储（OBS）服务

系统支持两种对象存储接入方案：通过部署工具内置部署的 MinIO 实例，或对接外部独立对象存储服务，可按需选择。

#### 3.4.1 工具内置部署 MinIO 服务（开发环境可用）

本部署工具提供一键部署能力，可在集群内快速拉起单节点 MinIO 对象存储实例：
```
./deploy.sh up minio        # 部署 MinIO 存储模块（基础依赖，只需也只能一次）
```

若需为该服务挂载外部 NFS 服务实现数据持久化，需在配置文件`.env.custom` 中设置：
```
# nfs：Pod直接通过地址直连NFS服务挂载共享目录
CLAW_MOUNT_TYPE="nfs"

# 外部 NFS 服务的连接地址
NFS_SERVER_ADDR=""

# 外部 NFS 服务的共享根目录路径
# 前置约束：共享目录下必须预先创建 minio 对应子目录用于数据存储
NFS_SHARE_PATH=""
```
**注意：** 本部署工具仅支持单实例 MinIO 服务部署，如需高可用 MinIO 服务，请采用外部独立部署方式。

#### 3.4.2 使用外部 OBS 服务（生产环境推荐）

生产环境下，推荐用户自行搭建或购买高可用的外部 OBS 服务，搭建完成后需在自定义配置文件 `.env.custom` 中填写如下参数，完成外部 OBS 服务的对接工作。
```
# 外部对象存储(S3/MinIO兼容)接入地址
# 为空时，自动使用本工具内置部署的MinIO实例
OBS_URL=""

# 业务存储桶名称
# 使用脚本内置部署的 MinIO 时无需配置；对接外部OBS/MinIO服务必须填写
OBS_BUCKET=""

# 对象存储公网预览访问域名
# 仅文件需要浏览器公网直读场景配置，内网环境可不填
OBS_PUBLIC_BASE_URL=""

# 对象存储访问的 AccessKey
# 本工具内置部署的 MinIO：对应 MinIO 的root用户账号；外部 OBS：填写云厂商/兼容S3服务的AccessKey
OBS_ACCESS_KEY=""

# 对象存储访问的 SecretKey
# 本工具内置部署的 MinIO：对应 MinIO 的root用户密码；外部 OBS：填写云厂商/兼容S3服务的SecretKey
OBS_SECRET_KEY=""

# 客户端连接是否启用HTTPS
# 本工具内置部署的 MinIO：无需配置；外部 OBS：根据服务端SSL开关选择 true/false
OBS_SECURE="false"

# 对象存储区域标识
# 本工具内置部署的 MinIO：无需配置；外部 OBS：按需填写
OBS_REGION=""
```

### 3.5 部署日志管理服务（可选部署）
为便于统一采集、查看与治理业务日志，本部署工具提供可自选部署的日志管理模块，基于 Fluent Bit + Vector 架构实现全链路日志采集与处理能力。

**架构分工说明：** 
- **Fluent Bit**： 以节点级组件部署于所有 K8s 集群节点，负责抓取业务容器原始日志，并将日志数据统一转发至 Vector 服务；
- **Vector**： 承接日志后续处理工作，完成日志数据脱敏清洗后，将合规日志持久化存储至部署工具运行节点，存储路径由环境变量 CLAW_LOG_DIR 统一指定。

#### 3.5.1 前置配置
部署日志管理服务前，需在自定义配置文件`.env.custom`中完成如下核心参数配置：
```
# 宿主机日志持久化根目录
# 默认路径：$HOME/claw_logs，生产环境建议手动显式配置，保证日志路径固定有读写权限
CLAW_LOG_DIR=""

# 部署工具运行所在K8s节点名称
# 脚本将尝试自动识别节点名，若识别失败，请手动填写值，取值参考 kubectl get nodes 输出第一列节点名称
CURRENT_NODE_NAME=
```

#### 3.5.2 日志脱敏与输出策略配置
系统默认策略：业务日志默认输出至容器本地文件，且默认启用业务应用内置日志脱敏能力，日志管理服务（Vector）的脱敏功能默认关闭。
为优化业务应用运行性能，可关闭业务侧内置脱敏能力与容器本地日志文件输出，统一交由日志管理服务实现日志脱敏、采集、存储全流程管控，需在`.env.custom` 中新增如下配置：

```
# 日志采集服务（Vector）脱敏功能开关：开启统一日志脱敏处理
COLLECT_LOG_MASK_ENABLED=true

# 业务应用内置日志脱敏功能开关：关闭业务侧原生脱敏，避免重复处理、提升性能
LOG_MASK_ENABLED=false

# 业务应用容器本地日志文件输出开关：关闭容器本地文件落盘，减少容器IO开销
LOG_TO_FILE_ENABLED=false
```

#### 3.5.3 服务部署命令
完成上述配置后，执行以下命令一键部署日志管理模块，该模块为可选组件，且仅支持单次部署：
```
./deploy.sh up log          # 部署日志模块（可选部署，也只能部署一次）
```

部署完成后，日志采集服务将自动筛选集群内名称前缀为 `jiuwenclaw` 的 Pod 进行日志采集。采集完成后，日志文件按照 `命名空间/Pod名称/容器名称-日期.log` 层级目录结构持久化存储，目录组织示例如下：

```
└── myns
    ├── jiuwenclaw-agentserver-3nwipvx8cq-y7cu6
    │   ├── jiuwenbox-2026-07-14.log
    │   └── jiuwenclaw-agentserver-2026-07-14.log
    ├── jiuwenclaw-agentserver-n7iqunzg6h-3l6kf
    │   ├── jiuwenbox-2026-07-14.log
    │   └── jiuwenclaw-agentserver-2026-07-14.log
    ├── jiuwenclaw-gateway-668cccc968-66pxt
    │   ├── gateway-2026-07-14.log
    │   └── gateway-2026-07-15.log
    └── jiuwenclaw-web-545f77c477-drfcf
        └── web-2026-07-14.log
```
## 4 部署JiuwenSwarm企业级服务

JiuwenSwarm 企业级服务完整支持基于 Kubernetes 命名空间的多实例隔离部署，可在同一集群内通过不同命名空间部署多套独立运行的业务实例，实现环境隔离、多实例并行使用。

同一业务实例下的所有组件（Gateway、Web、AgentRuntime 及其拉起的 AgentServer、Redis）只有部署在同一个命名空间内才能服务调用和互通。其中 NFS、MySQL、MinIO、PostgreSQL 等基础依赖组件为所有业务实例的公共组件，固定部署于 default 命名空间，无需随业务实例重复部署；而 Redis 不作为独立模块部署，随业务实例按命名空间隔离，启动业务模块时由部署工具自动就绪。

**注意**：
- 业务组件请勿部署至 default 默认命名空间。
- 可参照对应章节分步单独部署各组件，也可执行以下命令一键部署所有业务组件（`Gateway`、`Web`、`AgentRuntime`）
- AgentServer 不是独立部署模块，由 AgentRuntime 负责按需拉起与管理
```
./deploy.sh up -n <你的命名空间>
```


### 4.1 部署 Gateway (必选部署)

Gateway 是 JiuwenSwarm 的多渠道接入网关与消息调度核心，负责多平台渠道接入、消息双向路由转发、会话关系映射等关键能力。作为客户端与 AgentServer 之间的中转枢纽，该组件为系统强制部署项，缺失 Gateway 将导致整体业务系统无法正常运行。执行以下命令完成网关部署：

```
./deploy.sh up gateway -n <你的命名空间>             # 部署 Gateway 核心网关模块
```

部署时会检查 `.env.custom` 中的 `JIUWENCLAW_ID`：未配置则自动生成并写回；已配置则沿用原值。

注意：Gateway 支持分布式多副本部署模式：多副本同时在线、连接 Redis 共享会话。所需 Redis 由部署工具按命名空间自动拉起（详见 3.3 节）。启动前可在配置文件 `.env.custom` 中确认如下参数：

```
# Gateway 副本实例数量，控制部署运行的 Pod 个数
GATEWAY_REPLICAS=1
```

### 4.2 部署 AgentRuntime（必选部署）

AgentRuntime 是 Agent 运行时管理模块，负责按需创建、管理与回收 AgentServer Pod。AgentServer 不由部署工具直接部署：部署工具将渲染好的 AgentServer 服务模板（`templates/agentserver.template.json`，含容器镜像、存储挂载、多容器编排等）与其环境变量 ConfigMap（由 `templates/agentserver.template.env` 渲染，名为 `jiuwenclaw-agentserver-env`）下发后，由 AgentRuntime 据此拉起并维护 AgentServer 实例。执行以下命令完成部署：

```
./deploy.sh up runtime -n <你的命名空间>     # 部署 AgentRuntime 运行时模块
```

部署内容包括：
- ServiceAccount / Role / RoleBinding（授予 AgentServer Pod 创建权限）
- AgentRuntime Deployment（默认名 `jiuwenclaw-agent-runtime`）
- AgentServer 环境变量 ConfigMap（`jiuwenclaw-agentserver-env`）
- 部署就绪后，通过 AgentRuntime 的 `config_sync` 接口下发 AgentServer 服务模板

启动前可在配置文件 `.env.custom` 中确认如下参数：

```
# Agent Runtime 服务镜像地址
AGENT_RUNTIME_IMAGE=""

# Agent Runtime 副本实例数量，控制部署运行的 Pod 个数
AGENT_RUNTIME_REPLICAS=1
```

说明：卸载 AgentRuntime 时会先优雅停止其 Pod（保留 ServiceAccount，确保退出过程中能正常清理所创建的 AgentServer Pod），再兜底删除遗留的 AgentServer Pod，最后清理 ServiceAccount、Role 等周边资源。



若需为业务服务挂载外部 NFS 服务实现数据持久化，需在配置文件`.env.custom` 中设置：
```
# 设置产品组件的存储挂载模式为nfs
CLAW_MOUNT_TYPE="nfs"

# 外部 NFS 服务的连接地址
NFS_SERVER_ADDR=""

# 外部 NFS 服务的共享根目录路径
# 前置约束：共享目录下必须预先创建 jiuwenclaw 对应子目录用于数据存储
NFS_SHARE_PATH=""
```

若需为业务服务挂载外部 PVC 服务实现数据持久化，需在配置文件`.env.custom` 中设置：
```
# 设置产品组件的存储挂载模式为pvc
CLAW_MOUNT_TYPE="pvc"

# 外部 PVC 的名字，该 PVC 需存在，且与业务 Pod 处于同一 Namespace
CLAW_PVC=""

# 外部 PVC 绑定的 StorageClass 名称
NFS_SC_NAME=""
```

### 4.3 部署 Web（可选部署）

Web 为 JiuwenSwarm 企业版面向终端用户的对话可视化前端，用于用户直接和大模型机器人在线对话功能。该模块为可选组件，若业务仅通过飞书渠道与机器人交互、无需网页端对话入口，则可不部署。

```
./deploy.sh up web -n <你的命名空间>          # 部署 Web 前端模块（可选部署）

```

## 5 服务异常排查

系统服务运行异常时，可按以下步骤逐层定位故障根因：

### 5.1 查看 Pod 状态
查看集群全部 Pod，识别 Pending、CrashLoopBackOff、ImagePullBackOff 等异常运行状态：
```
kubectl get pods -A
```
### 5.2 查看 Pod 事件（快速定位启动故障）

适用于排查 Pod 处于 Pending 等无法正常启动场景，可定位镜像拉取失败、资源配额不足、调度异常、健康探针校验失败等问题：
```
kubectl describe pod <pod-name> -n <namespace>
```

若需查询节点 kubelet 底层详细报错，执行如下命令实时输出日志：

```
journalctl -u kubelet -f
```

### 5.3 查看容器业务日志

本节适用于 Pod 状态为 Running 时，排查业务运行报错、接口异常、服务启动失败等问题。

**方式一：通过 kubectl 拉取标准输出日志**
日志输出内容较多时，建议将日志重定向至本地文件检索关键字 Error 定位异常：

```
kubectl logs -f <pod-name> -n <namespace> 2>&1 | tee <file-name>
```

**方式二：登录节点读取容器原始日志文件**

默认配置下，容器标准输出日志软链接统一存放于节点 /var/log/containers 目录下，仅对当前节点正在运行的 Pod 生成链接，

```
# ls /var/log/containers
jiuwenclaw-agentserver-gcivg9a6xk-aumi6_chenhui_jiuwenbox-f0c50f60193e14524f25f6f949d8c5e8333421812c339cfbaf37fc07a8f30f0f.log
jiuwenclaw-agentserver-gcivg9a6xk-aumi6_chenhui_jiuwenclaw-agentserver-e5b5a0f61ffd5cf817bf382734d6a50b6af15c81d2ea387c8106548e40005258.log
jiuwenclaw-gateway-6498fbc8d-nfhcl_chenhui_gateway-38a2134e8932dc4107c4e9cb7ce28e65531eac9f928c802d3cc49840ad363096.log
jiuwenclaw-web-fd64b644f-p42hf_chenhui_web-aeb66347df8980dea7f4697fe7f6b37eb1529c755efe2fbc778731646f70e7da.log
```
**注意**

1. kubelet 可自定义日志根路径 podLogsDir 参数，变更容器日志存储根目录。可通过如下命令查询当前节点生效日志根目录：
```
cat /var/lib/kubelet/config.yaml | grep podLogsDir
```
- 若命令有输出值：代表集群已自定义日志存储根目录，不再使用默认路径 /var/log/pods。
- 若命令无任何输出：代表未做自定义配置，使用 kubelet 默认底层日志目录 /var/log/pods

2. /var/log/containers 仅保留当前节点正常运行 Pod 的日志软链接；Pod 删除、漂移、容器完全退出后，软链接会被 kubelet 自动回收消失；



### 5.4 进入容器内部检查
若 Pod 处于 Running 状态，也可进入容器检查是否正常：
```
kubectl exec -it <pod-name> -n <namespace> -c <container-name> bash
```

### 5.5 启动日志管理服务收集所有业务日志

详情请见["部署日志管理服务"](#35-部署日志管理服务可选部署)


## 6 CCE 云集群 JiuwenSwarm 企业级部署

**方案：部署脚本 `--render-only` 渲染 YAML 离线部署（推荐，标准化、低门槛）**

该方案依托配套部署脚本自动渲染全套 K8s 资源清单，仅生成 YAML 不直接操作集群，适配本地运维机与 CCE 集群分离场景，无需手动编写 / 修改资源模板。

**步骤 1：修改 `.env.custom` 全局配置文件** 

1. 参考 [「业务必选配置修改」](#15-修改配置文件envcustom)，完成大模型接入凭证、飞书机器人对接参数等核心业务参数；

2. 参考 [「基础服务配置」](#3-部署基础依赖服务)，按需配置自身基础服务。

3. 追加离线部署专属配置：

```
# 本地执行机器≠CCE集群节点，关闭本地宿主机端口占用检测，避免端口状态误判阻断渲染
NO_CHECK_PORTS=true

# 当启用日志模块、NFS模块，或运行模式MODE=dev时必填；值为CCE集群内某个目标节点名称，用于将模块调度到该节点运行
CURRENT_NODE_NAME=

# 选取CCE节点的空闲端口（端口区间30000-32767）
GATEWAY_NODE_PORT=
WEB_NODE_PORT=
```

**步骤 2：执行脚本渲染全套 K8s 资源 YAML**

1. 登录一台可正常执行`kubectl`的运维机器；
2. 进入部署脚本根目录，执行仅渲染命令，替换`<命名空间>`为业务目标命名空间：
```
./deploy.sh up -n <命名空间> --render-only
```

>注意：脚本仅基于`.env.custom`变量渲染资源模板，不会发起任何创建 / 更新请求，所有产出 YAML 文件统一输出至 `./conf/` 目录。

**步骤 3：登录CCE 集群云管理平台，按如下文件顺序（资源依赖顺序）创建资源**
- configmap-secret.yaml
- gateway-env.configmap.yaml
- gateway-config.configmap.yaml
- gateway.yaml
- web.yaml
- runtime.yaml
- agentserver-env.configmap.yaml


# FAQ 

## 如何在线调试业务代码

    在开发调试环境中，如果需要对 Gateway、AgentRuntime、AgentServer、Web 业务组件进行代码在线修改以及调试配置，可通过配置变量控制 Pod 启动时挂载宿主机本地源码，Pod 不再使用镜像内置代码，直接加载本地修改后的源码，无需重新构建推送镜像，即可实时调试功能。

**使用前注意事项：**
- 提前在部署节点的宿主机上拉取对应模块完整源码，并切换至指定开发分支
- 具备部署节点的宿主机上的本地源码目录可读权限；
- 不调试某个模块时，对应路径变量必须留空，否则会覆盖镜像内置代码。
- 开发模式下 Pod 会固定调度到 `CURRENT_NODE_NAME` 指定的节点（hostPath 为节点本地路径），并以 root 身份运行，保证挂载源码在容器内可读写。

**使用方法：**
    请在启动服务前，修改配置文件 `.env.custom` 如下参数：

```
# 设置运行模式为开发模式，开启本地源码挂载调试逻辑
MODE=dev

# ===================== jiuwenclaw 模块调试 =====================
# CLAW源码宿主机绝对路径，仅调试claw组件时填写；不调试直接留空
# 源码仓库：https://gitcode.com/openJiuwen/jiuwenswarm.git
# 代码分支：dev-stable
CLAW_CODE_PATH=""

# ===================== agent-runtime 模块调试 =====================
# Runtime源码宿主机绝对路径，仅调试runtime组件时填写；不调试直接留空
# 源码仓库：https://gitcode.com/openJiuwen/agent-runtime.git
# 代码分支：develop
RUNTIME_CODE_PATH=""

# ===================== agent-core 模块调试 =====================
# agent-core项目在宿主机本地代码路径，仅调试core组件时填写；不调试直接留空
# 源码仓库：https://gitcode.com/openJiuwen/agent-core.git
# 代码分支：dev-stable
CORE_CODE_PATH=""

# 是否要给 Web 模块mount代码
# 注意：Web源代码代码需要npm install之后，才能mount进容器。
# cd jiuwenswarm/channels/web/frontend
# npm install

IS_MOUNT_WEB_CODE="false"
```

**源码路径变量与组件的对应关系：**

| 变量 | Gateway | AgentRuntime | AgentServer | Web |
|------|---------|--------------|-------------|-----|
| `CLAW_CODE_PATH` | 挂载 jiuwenswarm 源码 | 不涉及 | 挂载 jiuwenswarm 源码 | 挂载（需 `IS_MOUNT_WEB_CODE=true`） |
| `RUNTIME_CODE_PATH` | 挂载 foundation/management 包 | 挂载 agent-runtime 源码 | 挂载 foundation/management 包 | 挂载（需 `IS_MOUNT_WEB_CODE=true`） |
| `CORE_CODE_PATH` | 挂载 openjiuwen 核心包 | 不涉及 | 挂载 openjiuwen 核心包 | 挂载（需 `IS_MOUNT_WEB_CODE=true`） |

说明：AgentServer 为多容器 Pod（jiuwenclaw-agentserver + jiuwenbox），其源码挂载随服务模板一并生效；任一源码路径变量留空时，AgentServer 服务模板中对应的 hostPath 挂载会被自动剔除，继续使用镜像内置代码。
