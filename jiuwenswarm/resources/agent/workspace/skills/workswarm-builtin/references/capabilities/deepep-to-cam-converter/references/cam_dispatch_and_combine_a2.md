# CAM dispatch & combine A2算子
## 使用场景
提供在A2环境上运行的一对协同工作dispatch&&combine算子，主要用于混合专家模型(Moe, Mixture of Experts)中用于专家并行（Expert Parallelism）带来的动态路由问题。在如下约束下可使用
1. 运行环境为昇腾A2环境
2. 当前Moe场景中不存在共享专家
3. 当前Moe场景需要满足如下取值要求
 - Moe会选择概率最高的K个专家，将token通过dispatch算子分发给对应的专家并通过combine算子收回，当前这套算子需要保证这个top_k取值范围为(2， 16]
 - 假设当前Moe通信域的rank数定义为num_ranks，当前仅支持num_ranks为16
 - 假设当前Moe通信域的专家数为num_experts，num_experts取值范围的取值范围是(0, 256]，并且需要满足num_experts % num_ranks ==0
 - 假设当前本卡要发送的token形状为(batch_size, hidden_size), batch_size的取值范围需要满足[1, 4K], hidden_size的取值范围需要满足(0， 7168]且(hidden_size % 32) == 0
 - 在进行dispatch & combine过程中batch_size的取值范围需要满足[1, 4K]
 - 当前不支持开启量化


## 接口说明
当前提供算子已提供torch扩展包，需要import umdk_cam_op_lib，调用时使用torch.ops.umdk_cam_op_lib.xxx进行调用
### 2.1 get_dispatch_layout_a2 ▶
#### 2.1.1 接口原型 
```python
get_dispatch_layout_a2(
    Tensor topk_idx, 
    int num_experts, 
    int num_ranks)
-> output: tuple(Tensor, Tensor)
```
#### 2.1.2 接口描述
A2代际Prefill阶段Dispatch使用的前置接口，用以在当前rank将Token拓展TopK份(存在共享专家时，此处为(topK+1)份)之后按照专家粒度重排，方便后续的分发操作。该接口需配合moe_dispatch_prefill_a2和moe_combine_prefill_a2使用。
#### 2.1.3 入参 
| **📌参数** | **🔧类型** | **✅是否必选** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|--------------|----------|
|topk_idx|Tensor|必选|形状:(batch_size, topk)， int64类型，取值范围：[0, num_experts)|目标专家的ID信息|
|num_experts|int|必选|取值范围：(0, 256]|MOE专家数|
|num_ranks|int|必选|当前仅支持16|EP通信域rank数|
#### 2.1.4 返回值 
函数返回值是一个2个Tensor构成的Tuple，分别存放：number_tokens_per_expert和notify_send_data.
| **📌参数** | **🔧类型** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|----------|
|number_tokens_per_expert|Tensor|形状：（num_experts）,int类型|当前rank上发送给每个专家的token个数|
|notify_send_data|Tensor|形状：(num_experts * EXPERT_DATA_SIZE + server_num + max_bs * (1 + 2* server_num + num_experts)), 数据类型为int。当前EXPERT_DATA_SIZE=4097，max_bs=4096。七个部分的形状信息：<br> 1. num_tokens_per_expert, 形状：（num_experts）；<br> 2. num_token_per_server_uniq, 形状：（num_experts）；<br> 3. num_each_token_to_server, 形状：（max_bs * num_server）;<br> 4. each_token_to_num_server, 形状：（max_bs）;<br> 5. each_token_offset_to_server, 形状：（max_bs * num_server）；<br> 6. send_token_idx, 形状：（max_bs * num_experts）；<br> 7. expert_rank_token_idx, 形状：（num_experts， max_bs）；<br> |由七个部分组成的tensor,分别表示<br> 1. 每个expert从本卡收到的token数目；<br> 2. 每个server从本卡接收到的token数目（去重）；<br> 3. 本卡每个token发往每个server的个数；<br> 4. 本卡每个token发往的server个数；<br> 5. 本卡每个token发往每个server,token的顺序偏移。<br> 6. 本卡每个token按照专家维度分桶，在桶中的序号偏移。<br> 7. 每个专家收到的每个token，其对应的each_token_offset_to_server值|
#### 2.1.5 约束和注意事项 ⚠️
1. 入参形状需严格满足上述入参描述中的形状定义。
2. 当前接口只支持A2环境调用。
3. 当前接口不支持并发调用。
4. 当前接口不支持入图使用。
5. 当前接口不支持共享专家。
6. 除满足上述形状约束外，其他参数取值要求：
 - top_k取值范围：(2， 16]
 - 需要满足: num_experts % num_ranks == 0
 - 需要满足: num_ranks % 8 == 0
 - 需要配置：export HCCL_INTRA_PCIE_ENABLE = 1, export HCCL_INTRA_ROCE_ENABLE = 0

### 2.2 moe_dispatch_prefill_a2 ▶
#### 2.2.1 接口原型 
```python
moe_dispatch_prefill_a2(
    Tensor x, 
    Tensor topk_idx, 
    Tensor topk_weights, 
    Tensor num_tokens_per_expert,
 	Tensor notify_send_data, 
    str group_ep, 
    int rank, 
    int num_ranks, 
    bool use_quant) 
-> output: Tensor[]
```
#### 2.2.2 接口描述 
A2代际Prefill阶段Dispatch接口，将Token按照topk_idx的规则发送给对应专家。
#### 2.2.3 入参 
| **📌参数** | **🔧类型** | **✅是否必选** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|--------------|----------|
|x|Tensor|必选|形状:(batch_size, hidden_size), 支持bf16, float16类型|本卡发送的token|
|topk_idx|Tensor|必选|形状:(batch_size, topk)， 数据类型为int64，取值范围[0, num_experts)|每个token的目标专家ID信息|
|topk_weights|Tensor|必选|形状:(batch_size, topk)， 数据类型为float32|每个token的topk个目标专家的权重信息|
|number_tokens_per_expert|Tensor|必选|形状：（num_experts），数据类型为int|当前rank上发送给每个专家的token个数|
|notify_send_data|Tensor|必选|形状：(num_experts * EXPERT_DATA_SIZE + server_num + max_bs * (1 + 2* server_num + num_experts)), 数据类型为int|get_dispatch_layout_a2的输出，含义参考该部分的描述|
|group_ep|str|必选|--|HCCL通信域名称|
|rank|int|必选|[0, num_ranks)|本卡在通信域中的rankID|
|num_ranks|int|必选|当前只支持16|EP通信域rank数|
|use_quant|bool|必选|False: 不开启量化, 当前版本暂不支持量化|Dispatch量化指示符|
#### 2.2.4 返回值 
函数返回值是一个8个Tensor构成的List，分别存放：recv_x, dynamic_scales_out, expand_idx_out, ep_rank_token_cnt, offset_inner, offset_outer, count_outer, expand_scales.
| **📌参数** | **🔧类型** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|----------|
|recv_x|Tensor|形状：(recv_token_num, hidden_size), 其中recv_token_num为本卡收到的token个数。当use_quant为true时，数据类型为int8, false时数据类型与入参x一致。|当前rank上收到的token信息|
|dynamic_scales_out|Tensor|形状：(recv_token_num), 数据类型为float.当use_quant为false时该值没有意义。|当前rank上收到token的动态量化scale信息|
|expand_idx_out|Tensor|形状：(maxbs, num_experts), 数据类型为int|本卡发出的token在同一专家内的序号|
|ep_rank_token_cnt|Tensor|形状：(num_experts, num_ranks), 数据类型为int|每个专家从不同rank接收的token数量|
|offset_inner|Tensor|形状：(2, max_bs, num_experts), 数据类型为int|token给对应专家的偏移，仅存放当前卡对端server的同号卡信息|
|offset_outer|Tensor|形状：(max_bs, num_experts), 数据类型为int|token发送给对应server的token序号|
|count_outer|Tensor|形状：(max_bs), 数据类型为int|token发送到server的数量|
|expand_scales|Tensor|形状：(num_recv_tokens), 数据类型为float|接收token时对应到topk_weights中的权重|
#### 2.2.5 约束和注意事项 ⚠️
1. 入参形状需严格满足上述入参描述中的形状定义。
2. 当前接口只支持A2环境调用。
3. 当前接口不支持并发调用。
4. 当前接口不支持入图使用。
5. 当前接口不支持共享专家。
6. 除满足上述形状约束外，其他参数取值要求：
 - 需要满足：BS取值范围[1, 4K]
 - 需要满足: num_experts取值范围(0, 256]
 - 需要满足: topk取值范围(2, 16]
 - 需要满足: (num_experts % num_ranks) == 0
 - 需要满足: (num_ranks % 8) == 0
 - 需要满足: hidden_size取值范围(0， 7168]且(hidden_size % 32) == 0
 - 需要满足: 配置全局宏HCCL_BUFFERSIZE=4096
 - 需要配置：export HCCL_INTRA_PCIE_ENABLE = 1, export HCCL_INTRA_ROCE_ENABLE = 0

### 2.3 moe_combine_prefill_a2 ▶
#### 2.3.1 接口原型 
```python
moe_combine_prefill_a2(
    Tensor x, 
    Tensor topk_idx, 
    Tensor topk_weights, 
    Tensor src_idx, 
    Tensor send_head, 
    Tensor expand_scales, 
    Tensor offset_inner, 
    Tensor offset_outer, 
    Tensor count_outer, 
    str group_ep, 
    int rank, 
    int num_ranks)
-> output: Tensor
```
#### 2.3.2 接口描述 
![moe_combine_prefill_a2示意图](figures/moe_combine_prefill_a2.png)
A2代际Prefill阶段Combine接口，将按照topk_idx的规则发送给对应专家的token，按照topk_weights指定的权重收回。
#### 2.3.3 入参 
| **📌参数** | **🔧类型** | **✅是否必选** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|--------------|----------|
|x|Tensor|必选|形状:(recv_token_num, hidden_size), 支持bf16, float16类型|本卡dispatch阶段收集到的token|
|topk_idx|Tensor|必选|形状:(batch_size, topk)， 数据类型为int64, 取值范围[0, num_experts)|每个token的目标专家ID信息|
|topk_weights|Tensor|必选|形状:(batch_size, topk)， 数据类型为float32|每个token的topk个目标专家的权重信息|
|src_idx|Tensor|必选|形状：(max_bs, num_experts), 数据类型为int|对应moe_dispatch_prefill_a2的出参expand_idx_out|
|send_head|Tensor|必选|形状：(num_experts), 数据类型为int|对应moe_dispatch_prefill_a2的出参ep_rank_token_cnt|
|expand_scales|Tensor|必选|形状：(num_recv_tokens), 数据类型为float|对应moe_dispatch_prefill_a2的出参expand_scales|
|offset_inner|Tensor|必选|形状：(2, max_bs, num_experts), 数据类型为int|对应moe_dispatch_prefill_a2的出参offset_inner|
|offset_outer|Tensor|必选|形状：(max_bs, num_experts), 数据类型为int|对应moe_dispatch_prefill_a2的出参offset_outer|
|count_outer|Tensor|形状：(max_bs), 数据类型为int|对应moe_dispatch_prefill_a2的出参count_outer|
|group_ep|str|必选|--|HCCL通信域名称|
|rank|int|必选|[0, num_ranks)|本卡在通信域中的rankID|
|num_ranks|int|必选|当前只支持16|EP通信域rank数|
#### 2.3.4 返回值 
函数返回值是一个Tensor，存放combine_x信息。
| **📌参数** | **🔧类型** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|----------|
|combine_x|Tensor|形状：(batch_size, hidden_size)。数据类型与x一致|当前rank上收到的token信息。|
#### 2.3.5 约束和注意事项 ⚠️
1. 入参形状需严格满足上述入参描述中的形状定义。
2. 当前接口只支持A2环境调用。
3. 当前接口不支持并发调用。
4. 当前接口不支持入图使用。
5. 当前接口不支持共享专家。
6. 除满足上述形状约束外，其他参数取值要求：
 - 需要满足：BS取值范围[1, 4K]
 - 需要满足: num_experts取值范围(0, 256]
 - 需要满足: topk取值范围[2, 16]
 - 需要满足: (num_experts % num_ranks) == 0
 - 需要满足: hidden_size取值范围(0， 7168]且(hidden_size % 32) == 0
 - 需要满足: 配置全局宏HCCL_BUFFERSIZE=4096
 - 需要配置：export HCCL_INTRA_PCIE_ENABLE = 1, export HCCL_INTRA_ROCE_ENABLE = 0

## 替换示例
### 示例1：deep ep dispatch & combine算子替换为 cam dispatch & combine a2算子
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
import torch.distributed as dist
import torch_npu
import umdk_cam_op_lib


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

    torch.npu.set_device(local_rank)

    dist.init_process_group(
        backend='hccl',
        init_method=f'tcp://{ip}:{port}',
        world_size=num_nodes * num_local_ranks,
        rank=node_rank * num_local_ranks + local_rank,
    )
    
    torch.set_default_dtype(torch.bfloat16)
    torch.set_default_device(f'npu:{local_rank}')
    
    return dist.get_rank(), dist.get_world_size(), dist.new_group(list(range(num_local_ranks * num_nodes)))


def test_main(args, local_rank, num_local_ranks, num_ranks, num_nodes, rank, group):
    # 配置参数
    num_tokens, hidden = args.num_tokens, args.hidden
    num_topk_groups, num_topk, num_experts = args.num_topk_groups, args.num_topk, args.num_experts
    
    # 准备随机数据
    x = torch.randn((num_tokens, hidden), dtype=torch.bfloat16, device='npu')
    scores = torch.randn((num_tokens, num_experts), dtype=torch.float32, device='npu').abs() + 1
    
    # 计算 top-k 索引
    group_scores = scores.view(num_tokens, num_nodes, -1).amax(dim=-1)
    group_idx = torch.topk(group_scores, k=num_topk_groups, dim=-1, sorted=False).indices
    masked_scores = create_grouped_scores(scores, group_idx, num_nodes)
    topk_idx = torch.topk(masked_scores, num_topk, dim=-1, largest=True, sorted=False)[1].to(torch.int64)
    topk_weights = torch.ones((num_tokens, num_topk), dtype=torch.float32, device='npu')
    # 计算 rank 索引
    rank_idx = (topk_idx // (num_experts // num_ranks)).to(torch.int64)
    rank_idx.masked_fill_(topk_idx == -1, -1)
    inplace_unique(rank_idx, num_ranks)
    
    # 使用新的 get_dispatch_layout_a2 算子
    num_tokens_per_expert, notify_send_data = torch.ops.umdk_cam_op_lib.get_dispatch_layout_a2(
        topk_idx, num_experts, num_ranks
    )
    
    if local_rank == 0:
        print(f"[layout] Verified get_dispatch_layout_a2")

    # 使用新的 moe_dispatch_prefill_a2 算子
    use_quant = False  # 当前不支持量化
    ep_hcomm_info = group._get_backend(torch.device('npu')).get_hccl_comm_name(rank)
    ep_hcomm_info = ep_hcomm_info.encode('utf-8')
    
    dispatch_args = {
        'x': x,
        'topk_idx': topk_idx,
        'topk_weights': topk_weights,
        'num_tokens_per_expert': num_tokens_per_expert,
        'notify_send_data': notify_send_data,
        'group_ep': ep_hcomm_info,
        'rank': rank,
        'num_ranks': num_ranks,
        'use_quant': use_quant,
    }
    (
        recv_x,
        dynamic_scales_out,
        expand_idx_out,
        recv_count,
        offset_inner,
        offset_outer,
        count_outer,
        expand_scales,
    ) = torch.ops.umdk_cam_op_lib.moe_dispatch_prefill_a2(**dispatch_args)
    
    dist.barrier()
    
    if local_rank == 0:
        print(f"[dispatch] Completed using moe_dispatch_prefill_a2")

    combine_args = {
        'x': recv_x,
        'topk_idx': topk_idx,
        'topk_weights': topk_weights,
        'src_idx': expand_idx_out,
        'send_head': recv_count,
        'expand_scales': expand_scales,
        'offset_inner': offset_inner,
        'offset_outer': offset_outer,
        'count_outer': count_outer,
        'group_ep': ep_hcomm_info,
        'rank': rank,
        'num_ranks': num_ranks,
    }
    
    combined_x = torch.ops.umdk_cam_op_lib.moe_combine_prefill_a2(**combine_args)
    
    dist.barrier()
    
    if local_rank == 0:
        print(f"[combine] Completed using moe_combine_prefill_a2")


def test_loop(local_rank, num_local_ranks, args):
    num_nodes = int(os.getenv('WORLD_SIZE', 1))
    rank, num_ranks, group = init_dist(local_rank, num_local_ranks)
    
    test_main(args, local_rank, num_local_ranks, num_ranks, num_nodes, rank, group)
    
    dist.barrier()
    dist.destroy_process_group()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--num-processes', type=int, default=16, help="Must be 16 for A2 environment")
    parser.add_argument('--num-tokens', type=int, default=4096, help="Batch size, must be in [1, 4096]")
    parser.add_argument('--hidden', type=int, default=7168, help="Hidden size, must be divisible by 32 and <= 7168")
    parser.add_argument('--num-topk-groups', type=int, default=None)
    parser.add_argument('--num-topk', type=int, default=8, help="TopK value, must be in (2, 16]")
    parser.add_argument('--num-experts', type=int, default=256, help="Number of experts, must be <= 256 and divisible by num_ranks")
    args = parser.parse_args()
    
    if args.num_topk_groups is None:
        num_nodes = int(os.getenv('WORLD_SIZE', 1))
        args.num_topk_groups = min(num_nodes, 4)
    
    torch.multiprocessing.spawn(test_loop, args=(args.num_processes, args), nprocs=args.num_processes)
```