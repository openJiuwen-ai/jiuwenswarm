# CAM dispatch & combine Shmem算子
## 使用场景
提供在A3环境上运行的一对协同工作dispatch&&combine算子，基于Shmem实现后端通信，主要用于混合专家模型(Moe, Mixture of Experts)中用于专家并行（Expert Parallelism）带来的动态路由问题。在如下约束下可使用
1. 运行环境为昇腾A3环境，需要支持Shmem特性
2. 当前不支持TP
3. 当前Moe场景需要满足如下取值要求
 - 假设当前Moe通信域的rank数定义为ep_world_size，ep_world_size只支持如下取值：[8, 16, 32, 64, 128, 144, 256, 288]
 - 假设当前Moe通信域的专家数为num_experts，num_experts取值范围的取值范围是(0, 512]，由共享专家和moe专家组成共享专家数量为shared_expert_rank_num，moe专家为moe_expert_num，需要满足(moe_expert_num + shared_expert_rank_num) ≤ 512， moe_expert_num % (ep_world_size - shared_expert_rank_num) == 0 ，moe_expert_num / (ep_world_size - shared_expert_rank_num) ≤ MAX_EXPERT_PER_RANK, 当前该值设置为32， (ep_world_size + block_num -1) / block_num ≤ MULTI_RANK_SIZE，如果shared_expert_rank_num不为0，则ep_world_size需要为其整数倍，切ep_world_size ≠ shared_expert_rank_num，(batch_size * hidden_size * ep_world_size * expert_num_per_rank * 2)小于ext_info指向的地址空间大小
4. 当前Shmem算子使用时需要提前申请Shmem内存，申请Shmem内存时需要设置内存大小和ip端口参数，当前内存大小默认申请1024 ** 3即1GB，使用ip和端口为"tcp://127.0.0.1:8666"，注意这只是申请shmem用的参数，不要用到其他地方。
必须在算子执行完之后(如torch.npu.synchronize())之后释放shmem资源(aclshmem_free和aclshmem_finialize)

## 接口说明文档
当前提供算子已提供torch扩展包，需要import umdk_cam_op_lib，调用时使用torch.ops.umdk_cam_op_lib.xxx进行调用
### 2.1 moe_dispatch_shmem ▶
#### 2.1.1 接口原型 
```python
moe_dispatch_shmem(
    Tensor x, 
    Tensor expert_ids, 
    Tensor scales, 
    Tensor x_active_mask, 
    int ep_world_size, 
    int ep_rank_id, 
    int moe_expert_num, 
    int tp_world_size, 
    int tp_rank_id, 
    int expert_shard_type, 
    int shared_expert_num, 
    int shared_expert_rank_num, 
    int quant_mode, 
    int global_bs, 
    int expert_token_nums_type, 
    int ext_info)
-> output: List[Tensor]
```
#### 2.1.2 接口描述 
基于SHMEM类内存的Dispatch接口，用以在EP通信阶段将token分发至不同的专家以供后续的操作。该接口需配合moe_combine_shmem配套使用。
#### 2.1.3 入参 
| **📌参数** | **🔧类型** | **✅是否必选** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|--------------|----------|
|x|Tensor|必选|形状:(batch_size, hidden_size)|输入Token|
|expert_ids|Tensor(int32)|必选|形状:(batch_size, top_k)|目的专家ID信息, 数据类型必须为int32|
|scales|Tensor|可选|非空时为float类型，存在共享专家时形状:(m+1,h), 不存在共享专家时形状(m, h),其中m为共享专家数|量化参数|
|x_active_mask|Tensor|可选|暂不支持，传入None|--|
|ep_world_size|int|必选|只支持如下取值：[8, 16, 32, 64, 128, 144, 256, 288]|EP通信域内的rank数|
|ep_rank_id|int|必选|[0, ep_world_size-1]|EP通信域内rank ID号|
|moe_expert_num|int|必选|[1, 512]|MoE专家数|
|tp_world_size|int|必选|暂不支持，传入1|--|
|tp_rank_id|int|必选|暂不支持，传入0|--|
|expert_shard_type|int|必选|暂不支持，传入0|--|
|shared_expert_num|int|必选|不支持非1的值，传入1|每张卡上设置的共享专家数量|
|shared_expert_rank_num|int|必选|[0, ep_world_size-1]|当前moe中共享专家数量，如果不存在共享专家设置为0|
|quant_mode|int|必选|非量化传0，量化传2|量化模式|
|global_bs|int|必选|根据实际情况传入，由实际内存大小约束|EP通信域全局BS大小|
|expert_token_nums_type|int|必选|传入0：输出每个专家处理的token数量；传入1：输出每个专家处理的token前缀和。|输出expert_token_nums_out的数据格式|
|ext_info|int|必选|--|SHMEM初始化后返回的基地址指针|
#### 2.1.4 返回值 
函数返回值是一个由Tensor构成的List，依次存放：expand_x, dynamic_scales, expand_idx, expert_token_nums, ep_send_count, tp_send_count和expand_scales.
| **📌参数** | **🔧类型** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|----------|
|expand_x|Tensor|当前rank是共享专家时，形状:(rank_size * batch_size / shared_expert_num, hidden_size);当前rank是路由专家时，形状：(expert_num_per_rank * rank_size * batch_size, hidden_size)|每个rank上所有专家的token|
|dynamic_scales|Tensor|形状同expand_x的第一维，即:当前rank是共享专家时，形状:(rank_size * batch_size / shared_expert_num);当前rank是路由专家时，形状：(expert_num_per_rank * rank_size * batch_size)|量化参数信息|
|expand_idx|Tensor|形状：(batch_size, top_k)|在目标专家内，仅排序当前rank的token时，当前rank发出的token各自的排序ID|
|expert_token_nums|Tensor|(expert_num_on_rank)|当前rank上每个专家收到的token数|
|ep_send_count|Tensor|形状：(expert_num_per_rank * ep_world_size)|每个专家从每个rank收到的token数|
|tp_send_count|Tensor|--|暂不支持，无意义|
|expand_scales|Tensor|--|暂不支持，无意义|
#### 2.1.5 约束和注意事项 ⚠️
1. 入参形状需严格满足上述入参描述中的形状定义。
2. 当量化模式开启时，expand_x的数据类型为int8类型，而不开启量化时其数据类型为bfloat16类型。
3. 当前接口不支持A2环境调用。
4. 当前接口不支持并发调用。
5. 当前接口在GE图模式下不支持动态图， 不支持fullgraph=true的选项。
6. 用户应保证ext_info地址合法性。
7. 除满足上述形状约束外，其他参数取值要求：
 - 需要满足：(moe_expert_num + shared_expert_rank_num) ≤ CAM_MAX_EXPERT_NUM, 当前最大专家数为512
 - 需要满足: moe_expert_num % (ep_world_size - shared_expert_rank_num) == 0
 - 需要满足：moe_expert_num / (ep_world_size - shared_expert_rank_num) ≤ MAX_EXPERT_PER_RANK, 当前该值设置为32
 - 需要满足： (ep_world_size + block_num -1) / block_num ≤ MULTI_RANK_SIZE
 - 需要满足： 如果shared_expert_rank_num不为0，则ep_world_size需要为其整数倍，切ep_world_size ≠ shared_expert_rank_num
 - 需要满足：(batch_size * hidden_size * ep_world_size * expert_num_per_rank * 2)小于ext_info指向的地址空间大小
- - 必须在算子执行完之后(如torch.npu.synchronize())之后释放shmem资源(aclshmem_free和aclshmem_finialize)

### 2.2 moe_combine_shmem ▶
#### 2.2.1 接口原型 
```python
moe_combine_shmem(
    Tensor expand_x, 
    Tensor expert_ids, 
    Tensor expand_idx, 
    Tensor ep_send_counts, 
    Tensor expert_scales, 
    Tensor tp_send_counts, 
    Tensor x_active_mask, 
    Tensor activation_scale, 
    Tensor weight_scale, 
    Tensor group_list, 
    Tensor expand_scales, 
    int ep_world_size, 
    int ep_rank_id, 
    int moe_expert_num, 
    int tp_world_size, 
    int tp_rank_id, 
    int expert_shard_type, 
    int shared_expert_num, 
    int shared_expert_rank_num, 
    int global_bs, 
    int comm_quant_mode, 
    int ext_info, 
    int out_dtype, 
    int group_list_type)
-> output: Tensor
```
#### 2.2.2 接口描述 
基于SHMEM类内存的Combine接口，用以在EP通信阶段将分发至不同的专家的token回合以供后续的操作。该接口需配合moe_dispatch_shmem配套使用。
#### 2.2.3 入参 
| **📌参数** | **🔧类型** | **✅是否必选** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|--------------|----------|
|expand_x|Tensor|必选|形状同dispatch的出参expand_x|dispatch分发至各专家上的token|
|expert_ids|Tensor(int32)|必选|形状:(batch_size, top_k)|目的专家ID信息, 数据类型必须为int32|
|expand_idx|Tensor|必选|形状:(batch_size, top_k)|在目标专家内，仅排序当前rank的token时，按照rank发出的token各自的排序ID|
|ep_send_counts|Tensor|必选|形状:(expert_num_per_rank * ep_world_size)|每个专家从每个rank收到的token数|
|expert_scales|Tensor|必选|形状：（batch_size, top_k）|合并token时需要的权重|
|tp_send_count|Tensor|可选|暂不支持，传入int32类型的tensor[0]即可|--|
|x_active_mask|Tensor|可选|暂不支持，传入None|--|
|activation_scale|Tensor|可选|暂不支持，传入None|--|
|weight_scale|Tensor|可选|暂不支持，传入None|--|
|group_list|Tensor|可选|暂不支持，传入None|--|
|expand_scales|Tensor|可选|暂不支持，传入None|--|
|ep_world_size|int|必选|只支持如下取值：[8, 16, 32, 64, 128, 144, 256, 288]|EP通信域内的rank数|
|ep_rank_id|int|必选|[0, ep_world_size-1]|EP通信域内rank ID号|
|moe_expert_num|int|必选|[1, 512]|MoE专家数|
|tp_world_size|int|必选|暂不支持，传入1|--|
|tp_rank_id|int|必选|暂不支持，传入0|--|
|expert_shard_type|int|必选|暂不支持，传入0|--|
|shared_expert_num|int|必选|不支持非1的值，传入1|每张卡上设置的共享专家数量|
|shared_expert_rank_num|int|必选|[0, ep_world_size-1]|当前moe中共享专家数量，如果不存在共享专家设置为0|
|global_bs|int|必选|根据实际情况传入，由实际内存大小约束|EP通信域全局BS大小|
|out_dtype|int|必选|暂不支持，传入0|--|
|comm_quant_mode|int|必选|非量化传0，量化传2|量化模式|
|group_list_type|int|必选|暂不支持，传入0|--|
|ext_info|int|必选|--|SHMEM初始化后返回的基地址指针|
#### 2.2.4 返回值 
函数返回值是一个Tensor，存放expand_x信息。
| **📌参数** | **🔧类型** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|----------|
|expand_x|Tensor|形状:(batch_size, hidden_size)|合并后的token信息|
#### 2.2.5 约束和注意事项 ⚠️
1. 入参形状需严格满足上述入参描述中的形状定义。
2. 当前接口不支持A2环境调用。
3. 当前接口不支持并发调用。
4. 当前接口在GE图模式下不支持动态图， 不支持fullgraph=true的选项。
5. 当前不支持共享专家功能。
6. 用户应保证ext_info地址合法性。
7. 除满足上述形状约束外，其他参数取值要求：
 - 需要满足：(moe_expert_num + shared_expert_rank_num) ≤ CAM_MAX_EXPERT_NUM, 当前最大专家数为512
 - 需要满足: moe_expert_num % (ep_world_size - shared_expert_rank_num) == 0
 - 需要满足：moe_expert_num / (ep_world_size - shared_expert_rank_num) ≤ MAX_EXPERT_PER_RANK, 当前该值设置为32
 - 需要满足： (ep_world_size + block_num -1) / block_num ≤ MULTI_RANK_SIZE, 
 - 需要满足： 如果shared_expert_rank_num不为0，则ep_world_size需要为其整数倍，切ep_world_size ≠ shared_expert_rank_num
 - 需要满足：(batch_size * hidden_size * ep_world_size * expert_num_per_rank * 2)小于ext_info指向的地址空间大小
- 必须在算子执行完之后(如torch.npu.synchronize())之后释放shmem资源(aclshmem_free和aclshmem_finialize)

### 示例1：deep ep dispatch & combine算子替换为 cam dispatch & combine shmem算子
替换前：
```python
import argparse
import os
import torch
import torch.distributed as dist
import deep_ep
import inspect

def inplace_unique(x: torch.Tensor, num_slots: int):
    assert x.dim() == 2
    mask = x < 0
    x_padded = x.masked_fill(mask, num_slots)
    bin_count = torch.zeros((x.size(0), num_slots + 1), dtype=x.dtype, device=x.device)
    bin_count.scatter_add_(1, x_padded, torch.ones_like(x_padded))
    bin_count = bin_count[:, :num_slots]
    sorted_bin_count, sorted_bin_idx = torch.sort(bin_count, dim=-1, descending=True)
    sorted_bin_idx.masked_fill_(sorted_bin_count == 0, -1)
    sorted_bin_idx = torch.sort(sorted_bin_idx, descending=True, dim=-1).values
    x[:, :].fill_(-1)
    valid_len = min(num_slots, x.size(1))
    x[:, :valid_len] = sorted_bin_idx[:, :valid_len]

def create_grouped_scores(scores: torch.Tensor, group_idx: torch.Tensor, num_groups: int):
    num_tokens, num_experts = scores.shape
    scores = scores.view(num_tokens, num_groups, -1)
    mask = torch.zeros((num_tokens, num_groups), dtype=torch.bool, device=scores.device)
    mask = mask.scatter_(1, group_idx, True).unsqueeze(-1).expand_as(scores)
    return (scores * mask).view(num_tokens, num_experts)

def init_dist(local_rank: int, num_local_ranks: int):
    ip = os.getenv('MASTER_ADDR', '127.0.0.1')
    port = int(os.getenv('MASTER_PORT', '8361'))
    num_nodes = int(os.getenv('WORLD_SIZE', 1))
    node_rank = int(os.getenv('RANK', 0))

    sig = inspect.signature(dist.init_process_group)
    params = {
        'backend': 'nccl',
        'init_method': f'tcp://{ip}:{port}',
        'world_size': num_nodes * num_local_ranks,
        'rank': node_rank * num_local_ranks + local_rank,
    }
    if 'device_id' in sig.parameters:
        params['device_id'] = torch.device(f'cuda:{local_rank}')
    dist.init_process_group(**params)
    torch.set_default_dtype(torch.bfloat16)
    torch.set_default_device('cuda')
    torch.cuda.set_device(local_rank)

    return dist.get_rank(), dist.get_world_size(), dist.new_group(list(range(num_local_ranks * num_nodes)))

def test_main(args, num_sms, local_rank, num_local_ranks, num_ranks, num_nodes, rank, buffer, group):
    # 配置参数
    num_tokens, hidden = args.num_tokens, args.hidden
    num_topk_groups, num_topk, num_experts = args.num_topk_groups, args.num_topk, args.num_experts

    # 准备随机数据
    x = torch.randn((num_tokens, hidden), dtype=torch.bfloat16, device='cuda')
    scores = torch.randn((num_tokens, num_experts), dtype=torch.float32, device='cuda').abs() + 1
    
    # 计算 top-k 索引
    group_scores = scores.view(num_tokens, num_nodes, -1).amax(dim=-1)
    group_idx = torch.topk(group_scores, k=num_topk_groups, dim=-1, sorted=False).indices
    masked_scores = create_grouped_scores(scores, group_idx, num_nodes)
    topk_idx = torch.topk(masked_scores, num_topk, dim=-1, largest=True, sorted=False)[1].to(deep_ep.topk_idx_t)
    topk_weights = torch.ones((num_tokens, num_topk), dtype=torch.bfloat16, device='cuda')

    # 计算 rank 索引
    rank_idx = (topk_idx // (num_experts // num_ranks)).to(torch.int64)
    rank_idx.masked_fill_(topk_idx == -1, -1)
    inplace_unique(rank_idx, num_ranks)
    
    rdma_rank_idx = (rank_idx // num_local_ranks).to(torch.int64)
    rdma_rank_idx.masked_fill_(rank_idx == -1, -1)
    inplace_unique(rdma_rank_idx, num_nodes)

    num_tokens_per_rank, num_tokens_per_rdma_rank, num_tokens_per_expert, is_token_in_rank, _ = \
        buffer.get_dispatch_layout(topk_idx, num_experts)
    
    if local_rank == 0:
        print(f"[layout] Verified get_dispatch_layout")

    # 配置
    rdma_buffer_size, nvl_buffer_size = 128, 512
    config = deep_ep.Config(num_sms, 8, nvl_buffer_size, 16, rdma_buffer_size)

    # 测试 dispatch
    dispatch_args = {
        'x': x,
        'num_tokens_per_rank': num_tokens_per_rank,
        'num_tokens_per_rdma_rank': num_tokens_per_rdma_rank,
        'is_token_in_rank': is_token_in_rank,
        'num_tokens_per_expert': num_tokens_per_expert,
        'config': config,
        'async_finish': False,
        'topk_idx': topk_idx,
        'topk_weights': topk_weights
    }
    
    recv_x, _, handle, event = buffer.dispatch(**dispatch_args)
    event.current_stream_wait()
    
    if local_rank == 0:
        print(f"[dispatch] Completed, received {recv_x.size(0)} tokens")

    # 测试 combine
    combine_args = {
        'x': recv_x,
        'bias': (torch.ones_like(recv_x), torch.zeros_like(recv_x)),
        'handle': handle,
        'config': config,
        'async_finish': False,
        'topk_weights': topk_weights
    }
    
    combined_x, event = buffer.combine(**combine_args)
    event.current_stream_wait()
    
    if local_rank == 0:
        print(f"[combine] Completed, output shape: {combined_x.shape}")
        print("[test] All tests passed!")


def test_loop(local_rank, num_local_ranks, args):
    num_nodes = int(os.getenv('WORLD_SIZE', 1))
    rank, num_ranks, group = init_dist(local_rank, num_local_ranks)
    
    num_sms = 24
    buffer = deep_ep.Buffer(group, int(2e9), int(1e9), explicitly_destroy=True)
    
    test_main(args, num_sms, local_rank, num_local_ranks, num_ranks, num_nodes, rank, buffer, group)
    
    buffer.destroy()
    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--num-processes', type=int, default=8)
    parser.add_argument('--num-tokens', type=int, default=4096)
    parser.add_argument('--hidden', type=int, default=7168)
    parser.add_argument('--num-topk-groups', type=int, default=None)
    parser.add_argument('--num-topk', type=int, default=8)
    parser.add_argument('--num-experts', type=int, default=256)
    args = parser.parse_args()
    
    if args.num_topk_groups is None:
        num_nodes = int(os.getenv('WORLD_SIZE', 1))
        args.num_topk_groups = min(num_nodes, 4)

    torch.multiprocessing.spawn(test_loop, args=(args.num_processes, args), nprocs=args.num_processes)
```

替换后：
```python
import argparse
import os
import torch
import torch_npu
import torch.distributed as dist
import umdk_cam_op_lib
import shmem as shm
import inspect
import numpy as np
import random

# 关闭tls认证
shm.set_conf_store_tls(False, "")

def inplace_unique(x: torch.Tensor, num_slots: int):
    assert x.dim() == 2
    mask = x < 0
    x_padded = x.masked_fill(mask, num_slots)
    bin_count = torch.zeros((x.size(0), num_slots + 1), dtype=x.dtype, device=x.device)
    bin_count.scatter_add_(1, x_padded, torch.ones_like(x_padded))
    bin_count = bin_count[:, :num_slots]
    sorted_bin_count, sorted_bin_idx = torch.sort(bin_count, dim=-1, descending=True)
    sorted_bin_idx.masked_fill_(sorted_bin_count == 0, -1)
    sorted_bin_idx = torch.sort(sorted_bin_idx, descending=True, dim=-1).values
    x[:, :].fill_(-1)
    valid_len = min(num_slots, x.size(1))
    x[:, :valid_len] = sorted_bin_idx[:, :valid_len]

def create_grouped_scores(scores: torch.Tensor, group_idx: torch.Tensor, num_groups: int):
    num_tokens, num_experts = scores.shape
    scores = scores.view(num_tokens, num_groups, -1)
    mask = torch.zeros((num_tokens, num_groups), dtype=torch.bool, device=scores.device)
    mask = mask.scatter_(1, group_idx, True).unsqueeze(-1).expand_as(scores)
    return (scores * mask).view(num_tokens, num_experts)

def init_dist(local_rank: int, num_local_ranks: int):
    ip = os.getenv('MASTER_ADDR', '127.0.0.1')
    port = int(os.getenv('MASTER_PORT', '8361'))
    num_nodes = int(os.getenv('WORLD_SIZE', 1))
    node_rank = int(os.getenv('RANK', 0))

    sig = inspect.signature(dist.init_process_group)
    params = {
        'backend': 'hccl',
        'init_method': f'tcp://{ip}:{port}',
        'world_size': num_nodes * num_local_ranks,
        'rank': node_rank * num_local_ranks + local_rank,
    }
    if 'device_id' in sig.parameters:
        params['device_id'] = torch.device(f'npu:{local_rank}')
    dist.init_process_group(**params)
    torch.set_default_dtype(torch.bfloat16)
    torch.set_default_device('npu')
    torch.npu.set_device(local_rank)

    return dist.get_rank(), dist.get_world_size(), dist.new_group(list(range(num_local_ranks * num_nodes)))

def test_main(args, local_rank, num_local_ranks, num_ranks, num_nodes, rank, group):
    # 配置参数
    num_tokens, hidden = args.num_tokens, args.hidden
    num_topk_groups, num_topk, num_experts = args.num_topk_groups, args.num_topk, args.num_experts
    
    # EP通信域配置
    ep_world_size = num_ranks
    ep_rank_id = rank
    
    shared_expert_num = 1
    shared_expert_rank_num = 0
    
    # 计算moe专家数
    moe_expert_num = num_experts - shared_expert_rank_num
    
    # 准备随机数据
    x = torch.randn((num_tokens, hidden), dtype=torch.bfloat16, device='npu')
    scores = torch.randn((num_tokens, num_experts), dtype=torch.float32, device='npu').abs() + 1
    
    # 计算 top-k 索引（保持原有逻辑）
    group_scores = scores.view(num_tokens, num_nodes, -1).amax(dim=-1)
    group_idx = torch.topk(group_scores, k=num_topk_groups, dim=-1, sorted=False).indices
    masked_scores = create_grouped_scores(scores, group_idx, num_nodes)
    topk_idx = torch.topk(masked_scores, num_topk, dim=-1, largest=True, sorted=False)[1]
    
    # 生成 expert_ids 和 scales
    expert_ids = topk_idx.to(torch.int32)
    scales = torch.gather(masked_scores, 1, topk_idx)  # 使用分数作为权重
    
    if local_rank == 0:
        print(f"[Rank {rank}] Configuration: ep_world_size={ep_world_size}, "
              f"moe_expert_num={moe_expert_num}, "
              f"shared_expert_num={shared_expert_num}, "
              f"shared_expert_rank_num={shared_expert_rank_num}")
        print(f"[Rank {rank}] x shape: {x.shape}, expert_ids shape: {expert_ids.shape}")
    
    # SHMEM初始化
    ipPort = "tcp://127.0.0.1:8666"
    localMemSize = 1024 ** 3  # 1GB
    
    init_attrs = shm.InitAttr()
    init_attrs.my_rank = rank
    init_attrs.n_ranks = ep_world_size
    init_attrs.local_mem_size = localMemSize
    init_attrs.ip_port = ipPort
    
    shm_ret = shm.aclshmem_init(init_attrs)
    if shm_ret != 0:
        raise ValueError(f'[ERROR] shmem_init failed on rank {rank}')
    
    # 分配共享内存
    shmem_ptr = shm.aclshmem_malloc(localMemSize)
    
    if local_rank == 0:
        print(f"[SHMEM] Initialized, ptr: {shmem_ptr}")
    
    # 调用 moe_dispatch_shmem
    if local_rank == 0:
        print(f"[Dispatch] Calling moe_dispatch_shmem...")
    
    dispatch_output = torch.ops.umdk_cam_op_lib.moe_dispatch_shmem(
        x=x,
        expert_ids=expert_ids,
        scales=None,
        x_active_mask=None,
        ep_world_size=ep_world_size,
        ep_rank_id=ep_rank_id,
        moe_expert_num=moe_expert_num,
        tp_world_size=1,
        tp_rank_id=0,
        expert_shard_type=0,
        shared_expert_num=shared_expert_num,
        shared_expert_rank_num=shared_expert_rank_num,
        quant_mode=0,
        global_bs=0,
        expert_token_nums_type=0,
        ext_info=shmem_ptr
    )
    
    # 解析返回值
    expand_x = dispatch_output[0]
    dynamic_scales = dispatch_output[1]
    expand_idx = dispatch_output[2]
    expert_token_nums = dispatch_output[3]
    ep_send_count = dispatch_output[4]
    tp_send_count = dispatch_output[5]
    
    dist.barrier()

    # 准备combine参数
    x_active_mask = None
    activation_scale = None
    weight_scale = None
    group_list = None
    expand_scales = None
    out_dtype = 0
    comm_quant_mode = 0
    group_list_type = 0
    
    combined_x = torch.ops.umdk_cam_op_lib.moe_combine_shmem(
        expand_x=expand_x,
        expert_ids=expert_ids,
        expand_idx=expand_idx,
        ep_send_counts=ep_send_count,
        expert_scales=scales,
        tp_send_counts=tp_send_count,
        x_active_mask=x_active_mask,
        activation_scale=activation_scale,
        weight_scale=weight_scale,
        group_list=group_list,
        expand_scales=expand_scales,
        ep_world_size=ep_world_size,
        ep_rank_id=ep_rank_id,
        moe_expert_num=moe_expert_num,
        tp_world_size=1,
        tp_rank_id=0,
        expert_shard_type=0,
        shared_expert_num=shared_expert_num,
        shared_expert_rank_num=shared_expert_rank_num,
        global_bs=0,
        comm_quant_mode=comm_quant_mode,
        ext_info=shmem_ptr,
        out_dtype=out_dtype,
        group_list_type=group_list_type
    )
    
    torch.npu.synchronize()
    
    # 清理SHMEM
    shm.aclshmem_free(shmem_ptr)
    shm.aclshmem_finialize()


def test_loop(local_rank, num_local_ranks, args):
    num_nodes = int(os.getenv('WORLD_SIZE', 1))
    rank, num_ranks, group = init_dist(local_rank, num_local_ranks)
    
    test_main(args, local_rank, num_local_ranks, num_ranks, num_nodes, rank, group)
    
    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--num-processes', type=int, default=8)
    parser.add_argument('--num-tokens', type=int, default=32)
    parser.add_argument('--hidden', type=int, default=7168)
    parser.add_argument('--num-topk-groups', type=int, default=None)
    parser.add_argument('--num-topk', type=int, default=4)
    parser.add_argument('--num-experts', type=int, default=8)
    args = parser.parse_args()
    
    if args.num_topk_groups is None:
        num_nodes = int(os.getenv('WORLD_SIZE', 1))
        args.num_topk_groups = min(num_nodes, 4)
    
    torch.multiprocessing.spawn(test_loop, args=(args.num_processes, args), nprocs=args.num_processes)
```