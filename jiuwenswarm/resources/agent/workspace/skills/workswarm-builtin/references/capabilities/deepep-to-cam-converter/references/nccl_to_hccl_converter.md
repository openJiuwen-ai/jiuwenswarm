# NCCL到HCCL通信域转换参考指南

## 概述
本文档提供了将NCCL通信域转换为昇腾NPU HCCL通信域的常见模式参考，帮助大模型在CAM算子替换过程中正确处理通信域的转换。

## 基本概念对比

### NCCL (NVIDIA Collective Communications Library)
- **平台**: NVIDIA GPU
- **通信后端**: NVLink, PCIe, InfiniBand
- **主要API**: `ncclComm_t`, `ncclGroupStart()`, `ncclGroupEnd()`
- **数据类型**: 基于CUDA的`cudaStream_t`

### HCCL (Huawei Collective Communications Library)
- **平台**: 昇腾NPU (Ascend)
- **通信后端**: PCIe, RoCE, HCCS
- **主要API**: `HcclComm`, `HcclGroupStart()`, `HcclGroupEnd()`
- **数据类型**: 基于NPU的`aclrtStream`

## 转换示例

### 模式1：通信域初始化

#### NCCL版本
```python
import os
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

def train_with_hccl():
    # 1. 初始化分布式环境
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])

    # 2. 初始化进程组，指定后端为 'hccl'
    dist.init_process_group(backend='hccl', init_method='env://')

    # 3. 设置当前进程绑定的 NPU 设备
    torch.npu.set_device(local_rank)
    device = torch.device('npu', local_rank)

    # 4. 定义一个简单的模型
    model = torch.nn.Linear(10, 10).to(device)
    
    # 5. 使用 DDP 包装模型
    # DDP 会自动使用 HCCL 后端进行梯度同步
    ddp_model = DDP(model, device_ids=[local_rank])

    # --- 训练循环 ---
    # 在训练循环中，ddp_model 的 backward() 会自动触发 HCCL AllReduce 操作
    # ----------------

    # 训练结束后，清理资源
    dist.destroy_process_group()

if __name__ == '__main__':
    # 同样，使用 multiprocessing 来模拟
    import torch.multiprocessing as mp
    # 假设单机有 8 张 NPU
    world_size = 8
    mp.spawn(train_with_hccl, args=(), nprocs=world_size, join=True)
```

#### HCCL版本
```python
import os
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP

def train_with_hccl():
    # 1. 初始化分布式环境
    rank = int(os.environ['RANK'])
    world_size = int(os.environ['WORLD_SIZE'])
    local_rank = int(os.environ['LOCAL_RANK'])
    
    # HCCL 通常需要 RANK_TABLE_FILE 环境变量来定位集群配置文件
    # os.environ['RANK_TABLE_FILE'] = '/path/to/hccl_rank_table.json'

    # 2. 初始化进程组，指定后端为 'hccl'
    dist.init_process_group(backend='hccl', init_method='env://')

    # 3. 设置当前进程绑定的 NPU 设备
    # 注意：这里使用 torch.npu 而非 torch.cuda
    torch.npu.set_device(local_rank)
    device = torch.device('npu', local_rank)

    # 4. 定义一个简单的模型
    model = torch.nn.Linear(10, 10).to(device)
    
    # 5. 使用 DDP 包装模型
    # DDP 会自动使用 HCCL 后端进行梯度同步
    ddp_model = DDP(model, device_ids=[local_rank])

    # --- 训练循环 ---
    # 在训练循环中，ddp_model 的 backward() 会自动触发 HCCL AllReduce 操作
    # ----------------

    # 训练结束后，清理资源
    dist.destroy_process_group()

if __name__ == '__main__':
    # 同样，使用 multiprocessing 来模拟
    import torch.multiprocessing as mp
    # 假设单机有 8 张 NPU
    world_size = 8
    mp.spawn(train_with_hccl, args=(), nprocs=world_size, join=True)
```

### 模式2：集体通信操作

#### 2.1 AllReduce操作

**NCCL版本**:
```python
# PyTorch NCCL AllReduce
dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
```

**HCCL版本**:
```python
# PyTorch NPU HCCL AllReduce
dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
# 注意：在NPU上需要确保tensor在NPU设备上
```

#### 2.2 AllGather操作

**NCCL版本**:
```python
# PyTorch NCCL AllGather
output_tensor_list = [torch.empty_like(tensor) for _ in range(world_size)]
dist.all_gather(output_tensor_list, tensor)
```

**HCCL版本**:
```python
# PyTorch NPU HCCL AllGather
output_tensor_list = [torch.empty_like(tensor) for _ in range(world_size)]
dist.all_gather(output_tensor_list, tensor)
```

#### 2.3 Broadcast操作

**NCCL版本**:
```python
# PyTorch NCCL Broadcast
dist.broadcast(tensor, src=rank)
```

**HCCL版本**:
```python
# PyTorch NPU HCCL Broadcast
dist.broadcast(tensor, src=rank)
```

### 模式3：点对点通信

#### NCCL版本
```python
# PyTorch NCCL点对点
dist.send(tensor, dst=dest_rank)
dist.recv(tensor, src=src_rank)
```

#### HCCL版本
```python
# PyTorch NPU HCCL点对点
dist.send(tensor, dst=dest_rank)
dist.recv(tensor, src=src_rank)
```

## 环境变量转换

### NCCL环境变量
```bash
# NCCL典型配置
export NCCL_DEBUG=INFO
export NCCL_SOCKET_IFNAME=eth0
export NCCL_IB_DISABLE=0
export NCCL_P2P_DISABLE=0
```

### HCCL环境变量
```bash
# HCCL对应配置
export HCCL_DEBUG=INFO
export HCCL_SOCKET_IFNAME=eth0
export HCCL_IB_DISABLE=0
export HCCL_P2P_DISABLE=0

# HCCL特有配置
export HCCL_BUFFERSIZE=4096  # A3环境需要
export HCCL_INTRA_PCIE_ENABLE=1  # A2环境需要
export HCCL_INTRA_ROCE_ENABLE=0  # A2环境需要
```

## 数据类型映射

| NCCL数据类型 | HCCL数据类型 | 说明 |
|-------------|-------------|------|
| `ncclInt8` | `HcclInt8` | 8位整数 |
| `ncclInt32` | `HcclInt32` | 32位整数 |
| `ncclFloat16` | `HcclFloat16` | 半精度浮点 |
| `ncclFloat32` | `HcclFloat32` | 单精度浮点 |
| `ncclBfloat16` | `HcclBfloat16` | Brain浮点16 |

## 操作类型映射

| NCCL操作 | HCCL操作 | 说明 |
|---------|---------|------|
| `ncclSum` | `HcclSum` | 求和 |
| `ncclProd` | `HcclProd` | 乘积 |
| `ncclMax` | `HcclMax` | 最大值 |
| `ncclMin` | `HcclMin` | 最小值 |
| `ncclAvg` | `HcclAvg` | 平均值 |

## 错误处理转换

### NCCL错误处理
```c
ncclResult_t result = ncclAllReduce(...);
if (result != ncclSuccess) {
    printf("NCCL error: %s\n", ncclGetErrorString(result));
    // 处理错误
}
```

### HCCL错误处理
```c
HcclResult result = HcclAllReduce(...);
if (result != HCCL_SUCCESS) {
    printf("HCCL error: %d\n", result);
    // 处理错误
}
```

## PyTorch分布式API转换

### 通用转换模式
```python
# NCCL版本
import torch.distributed as dist

# 初始化
dist.init_process_group(backend='nccl', ...)

# 通信操作
dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
dist.all_gather(output_list, tensor)
dist.broadcast(tensor, src=0)

# HCCL版本（仅需修改backend）
import torch.distributed as dist

# 初始化
dist.init_process_group(backend='hccl', ...)  # 仅此修改

# 通信操作（API保持不变）
dist.all_reduce(tensor, op=dist.ReduceOp.SUM)
dist.all_gather(output_list, tensor)
dist.broadcast(tensor, src=0)
```

## 性能优化建议

### 1. 缓冲区管理
- **NCCL**: 使用`cudaMalloc`分配GPU显存
- **HCCL**: 使用`aclrtMalloc`分配NPU内存

### 2. 流同步
- **NCCL**: 使用`cudaStreamSynchronize(stream)`
- **HCCL**: 使用`aclrtSynchronizeStream(stream)`

### 3. 通信重叠
- **NCCL**: 使用多个CUDA流重叠通信和计算
- **HCCL**: 使用多个ACL流重叠通信和计算

### 4. 拓扑感知
- **NCCL**: 使用`ncclTopoGetSystem`获取系统拓扑
- **HCCL**: 使用`HcclGetTopoInfo`获取NPU拓扑

## 常见问题与解决方案

### 问题1：通信域初始化失败
**NCCL原因**: `ncclCommInitRank`返回`ncclInvalidArgument`
**HCCL对应**: `HcclCommInitRank`返回`HCCL_INVALID_ARGUMENT`
**解决方案**: 检查`world_size`和`rank`参数是否有效

### 问题2：数据类型不匹配
**NCCL表现**: `ncclAllReduce`返回`ncclInvalidType`
**HCCL表现**: `HcclAllReduce`返回`HCCL_INVALID_TYPE`
**解决方案**: 确保发送和接收缓冲区数据类型一致

### 问题3：缓冲区大小不匹配
**NCCL表现**: `ncclAllGather`返回`ncclInvalidUsage`
**HCCL表现**: `HcclAllGather`返回`HCCL_INVALID_USAGE`
**解决方案**: 检查`sendcount`和`recvcount`参数

## 总结
将NCCL通信域转换为HCCL通信域主要涉及：
1. **后端修改**: `nccl` → `hccl`
2. **设备修改**: `.cuda()` → `.npu()`
3. **环境变量**: 更新为HCCL特定配置
4. **算子替换**: 将复杂NCCL通信替换为CAM融合算子
5. **性能优化**: 利用NPU特性和CAM算子优化

通过以上转换，可以充分利用昇腾NPU的硬件特性，实现更高效的分布式训练和推理。