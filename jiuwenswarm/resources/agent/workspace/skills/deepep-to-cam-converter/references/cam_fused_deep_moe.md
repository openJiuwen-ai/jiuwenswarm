# CAM fused moe算子
## 使用场景
提供在A3环境上运行的用于MoE Decode阶段的通算大融合算子，通过融合[Dispatch + FFN(GMM1 + Swiglu + GMM2) + Combine]实现高效的模型推理和专家选择，在如下约束下可使用
1. 用于Moe Decode阶段，需要严格满足[Dispatch + FFN(GMM1 + Swiglu + GMM2) + Combine]的范式，其中FFN即专家部分，GMM1是使用分组矩阵乘法进行升维，GMM2是使用分组矩阵乘法降维，激活函数必须为Swiglu。
2. 当前接口只支持A3环境调用。
3. 当前接口不支持并发调用。极端情况下在单次forward中连续调用相同算子会产生未定义行为，这种场景需要在算子执行间添加torch.npu.synchronize()避免潜在的异步时序问题。
4. 当前接口图模式只支持AclGraph模式。
5. 不支持外置共享专家（即有的卡只放置共享专家）。
6. 参数范围要求：
 - 单个设备上在一次前向传播中处理的样本数量为BS，取值范围[0, 256]
 - 单个token的长度为token_length，取值范围[1024， 7168]且(token_length % 256) == 0
 - GMM1的权重矩阵为gmm1_weight，隐藏层的维度为gmm1_hiden_size，取值范围[1024， 6144]且(gmm1_hiden_size % 256) == 0
 - 共享专家MM1的权重矩阵为share_gmm1_weight，隐藏层维度为share_mm1_hidden_size，取值范围[1024， 6144]且(gmm1_hiden_size % 256) == 0
 - Moe会选择概率最高的K个专家，将token通过dispatch算子分发给对应的专家并通过combine算子收回，当前这套算子需要保证这个top_k取值范围为[0, 12]且应保证小于等于专家数
 - 所有卡的最大token总数为global_bs ≥ 0 且保证（global_bs % ep_rank_size） == 0
 - 需要满足: 路由专家卡需满足local_expert_num ≤ (aivnum / 2)，其中aivnum为硬件aiv核心数
 - 需要满足: gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale四个入参的模式必须统一，不能一部分耦合模式一部分分离模式
 - 需要满足: HCCL_BUFFERSIZE环境变量配置应不小于[(ep_rank_size * max_batch_size * moe_expert_num_per_rank * total_length * sizeof(x) * 2) / 1024 / 1024]向上取整
 - 需要满足: 若要进行内置共享专家计算，则共享专家所需的share_gmm1_weight、share_gmm1_weight_scale、share_gmm2_weight、share_gmm2_weight_scale需同时存在
- 需要满足: 若要进行smooth quant，需传入expert_smooth_scales，若同时进行内置共享专家计算则share_smooth_scales也必须存在

## 接口说明文档
当前提供算子已提供torch扩展包，需要import umdk_cam_op_lib，调用时使用torch.ops.umdk_cam_op_lib.xxx进行调用
### 2.1 fused_deep_moe ▶
#### 2.1.1 接口原型 
```python
fused_deep_moe(
    Tensor x, 
    Tensor expert_ids, 
    Tensor[] gmm1_weight, 
    Tensor[] gmm1_weight_scale, 
    Tensor[] gmm2_weight, 
    Tensor[] gmm2_weight_scale, 
    Tensor expert_scales, 
    Tensor? share_gmm1_weight, 
    Tensor? share_gmm1_weight_scale, 
    Tensor? share_gmm2_weight, 
    Tensor? share_gmm2_weight_scale, 
    Tensor? expert_smooth_scales,
    Tensor? share_smooth_scales,
    Tensor? x_active_mask, 
    str group_ep, 
    int ep_rank_size, 
    int ep_rank_id, 
    int moe_expert_num, 
    int quant_mode, 
    int global_bs) 
-> output: Tensor[]
```
#### 2.1.2 接口描述 
用于MoE Decode阶段的通算大融合算子，通过融合[Dispatch + FFN(GMM1 + Swiglu + GMM2) + Combine]实现高效的模型推理和专家选择，（可选）同时支持内置共享专家计算，适用于分布式推理场景。
#### 2.1.3 入参 
| **📌参数** | **🔧类型** | **✅是否必选** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|--------------|----------|
|x|Tensor|必选|形状:(batch_size, token_length), 支持bf16, float16类型|本卡dispatch阶段待处理的token|
|expert_ids|Tensor|必选|形状:(batch_size, topk)， 数据类型为int32, 取值范围[-1, num_experts)，-1用于占位使用，一个token不允许重复发给同一个专家|每个token的目标专家ID信息|
|gmm1_weight|Tensor[]|必选|耦合模式下，只有一个Tensor, 形状:(localExpertNum, token_length, gmm1_hidden_size); 分离模式下，包含localExpertNum个Tensor, 每个Tensor形状：（token_length, gmm1_hidden_size），数据类型为int8|GMM1的权重矩阵列表，支持耦合模式和分离模式|
|gmm1_weight_scale|Tensor[]|必选|耦合模式下，只有一个Tensor, 形状:(localExpertNum, gmm1_hidden_size); 分离模式下，包含localExpertNum个Tensor, 每个Tensor形状：（gmm1_hidden_size），数据类型为float32或与x数据类型一致|GMM1的权重矩阵量化时使用的缩放系数列表，支持耦合模式和分离模式|
|gmm2_weight|Tensor[]|必选|耦合模式下，只有一个Tensor, 形状:(localExpertNum, gmm1_hidden_size/2, token_length); 分离模式下，包含localExpertNum个Tensor, 每个Tensor形状：（gmm1_hidden_size/2, token_length），数据类型为int8|GMM2的权重矩阵列表，支持耦合模式和分离模式|
|gmm2_weight_scale|Tensor[]|必选|耦合模式下，只有一个Tensor, 形状:(localExpertNum, token_length); 分离模式下，包含localExpertNum个Tensor, 每个Tensor形状：（token_length），数据类型为float32或与x数据类型一致|GMM2的权重矩阵量化时使用的缩放系数列表，支持耦合模式和分离模式|
|expert_scales|Tensor|必选|形状：(batch_size, topk), 数据类型为float32|每个专家的权重，combine阶段使用|
|share_gmm1_weight|Tensor|可选|形状：（token_length, share_mm1_hidden_size），数据类型为int8|共享专家MM1的权重矩阵|
|share_gmm1_weight_scale|Tensor|可选|形状：（share_mm1_hidden_size），数据类型为与gmm1_weight_scale一致|共享专家MM1的权重矩阵量化时使用的缩放系数|
|share_gmm2_weight|Tensor|可选|形状：（share_mm1_hidden_size/2, token_length），数据类型为int8|共享专家MM2的权重矩阵|
|share_gmm2_weight_scale|Tensor|可选|形状：（token_length），数据类型为与gmm2_weight_scale一致|共享专家MM2的权重矩阵量化时使用的缩放系数|
|expert_smooth_scales|Tensor|可选|形状：(moe_expert_num，token_length)，数据类型为float32|各个路由专家的smooth quant平滑因子|
|share_smooth_scales|Tensor|可选|形状：(token_length)，数据类型为float32|共享专家的smooth quant平滑因子|
|x_active_mask|Tensor|可选|形状： (batch_size)，数据类型bool，取值范围[true, false]，true值一定要在false之前|dispatch分发token时的mask，true代表正常分发该token，false代表不分发|
|group_ep|str|必选|字符串长度范围：(0, 128), 且需要保证是有效的通信域名称|HCCL通信域名称|
|ep_rank_size|int|必选|需要满足：(ep_rank_size * MoeExpertNumPerRank) ≤ 512且ep_rank_size > 0|EP通信域大小|
|ep_rank_id|int|必选|[0, ep_rank_size)|本卡在通信域中的rankID|
|moe_expert_num|int|必选|需要满足：moe_expert_num % ep_rank_size == 0|MOE专家数量|
|quant_mode|int|必选|预留入参，当前只支持传0|量化模式|
|global_bs|int|必选|若所有卡的token数量一致，可以传入0或者batch_size * ep_rank_size; 若所有卡的token数量不一致，需要传入max_batch_size * ep_rank_size|所有卡的最大token总数|
#### 2.1.4 返回值 
函数返回值是一个Tensor列表，存放combine_x和expert_token_nums信息。
| **📌参数** | **🔧类型** | **📋取值说明** | **📝描述** |
|----------|----------|--------------|----------|
|combine_x|Tensor|形状：(batch_size, token_length)。数据类型与x一致|当前rank上token经各个专家处理后汇聚的结果|
|share_output|Tensor|形状：(batch_size, token_length)。数据类型与x一致|内置共享专家处理后的结果，即使不进行共享专家计算，也会返回该值占位|
|expert_token_nums|Tensor|形状：(local_expert_num)。数据类型为int64|本卡各个专家收到的token数量|
#### 2.1.5 约束和注意事项 ⚠️
1. 入参形状需严格满足上述入参描述中的形状定义。
2. 当前接口只支持A3环境调用。
3. 当前接口不支持并发调用。极端情况下在单次forward中连续调用相同算子会产生未定义行为，这种场景需要在算子执行间添加torch.npu.synchronize()避免潜在的异步时序问题。
4. 当前接口图模式只支持AclGraph模式。
5. 不支持外置共享专家（即有的卡只放置共享专家）。
6. Batch_size小于16时非目标场景，其性能相对于小算子拼接可能劣化，建议性能对比后决策使用。
7. 除满足上述形状约束外，其他参数取值要求：
 - 需要满足：BS取值范围[0, 256]
 - 需要满足: token_length取值范围[1024， 7168]且(token_length % 256) == 0
 - 需要满足: gmm1_hiden_size取值范围[1024， 6144]且(gmm1_hiden_size % 256) == 0
 - 需要满足: share_mm1_hidden_size取值范围[1024， 6144]且(gmm1_hiden_size % 256) == 0
 - 需要满足: topk取值范围[0, 12]且应保证小于等于专家数
 - 需要满足：global_bs ≥ 0 且保证（global_bs % ep_rank_size） == 0
 - 需要满足: 路由专家卡需满足local_expert_num ≤ (aivnum / 2)，其中aivnum为硬件aiv核心数
 - 需要满足: gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale四个入参的模式必须统一，不能一部分耦合模式一部分分离模式
 - 需要满足: HCCL_BUFFERSIZE环境变量配置应不小于[(ep_rank_size * max_batch_size * moe_expert_num_per_rank * total_length * sizeof(x) * 2) / 1024 / 1024]向上取整
 - 需要满足: 若要进行内置共享专家计算，则共享专家所需的share_gmm1_weight、share_gmm1_weight_scale、share_gmm2_weight、share_gmm2_weight_scale需同时存在
- 需要满足: 若要进行smooth quant，需传入expert_smooth_scales，若同时进行内置共享专家计算则share_smooth_scales也必须存在

### 示例1：[Dispatch + FFN(GMM1 + Swiglu + GMM2) + Combine]替换为fused deep moe算子
替换前：
```python
import torch
import numpy as np
import torch.distributed as dist
from collections import defaultdict
import gc
import os
import sys
import math
import socket
from flashinfer.cute_dsl import grouped_gemm_nt_masked
import torch.nn.functional as F
import deep_ep

def convert_tensor_into_parameter(tensor):
    if tensor is None:
        return None
    return torch.nn.Parameter(tensor, requires_grad=False)

def dequant_swiglu_quant_gpu(y1_int32, weight_scale, activation_scale, group_list):
    dequantized = y1_int32.to(torch.float32) * (activation_scale * weight_scale)
    dequantized = dequantized.to(torch.bfloat16)

    intermediate_dim = dequantized.shape[-1] // 2
    up_proj = dequantized[:, :intermediate_dim]
    gate_proj = dequantized[:, intermediate_dim:]

    swiglu_out = up_proj * F.silu(gate_proj.to(torch.float32)).to(torch.bfloat16)

    abs_max = torch.abs(swiglu_out).max(dim=-1, keepdim=True)[0]
    y1_scale = abs_max / 127.0

    safe_scale = torch.clamp(y1_scale, min=1e-8)
    y1_float = swiglu_out.to(torch.float32) / safe_scale
    y1 = torch.clamp(torch.round(y1_float), -128, 127).to(torch.int8)
    
    return y1, y1_scale.squeeze(-1)

class CustomOps(torch.nn.Module):

    def __init__(self,
                 ep_hcomm_info,
                 meta_info,
                 weight_datas,
                 share_weight_datas):
        super().__init__()
        self.ep_hcomm_info = ep_hcomm_info
        batch_size, ep_world_size, moe_expert_num, global_rank_id, dynamic_eplb = meta_info
        self.ep_world_size = ep_world_size
        self.moe_expert_num = moe_expert_num
        self.global_rank_id = global_rank_id
        self.dynamic_eplb = dynamic_eplb
        self.global_batch_size = batch_size * ep_world_size
        self.with_share = None
        self.with_smooth = None
        self._checkout_datas(weight_datas, share_weight_datas)
        self._process_share_weights_after_loading(share_weight_datas)
        self._process_weights_after_loading(weight_datas)

    def _checkout_datas(self, weight_datas, share_weight_datas):
        gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale, smooth_scales = weight_datas
        share_mm1_weight, share_mm1_weight_scale, share_mm2_weight, share_mm2_weight_scale, share_smooth_scales = share_weight_datas
        if share_mm1_weight is not None:
            assert share_mm1_weight_scale is not None, "share expert need share_mm1_weight_scale"
            assert share_mm2_weight is not None, "share expert need share_mm2_weight"
            assert share_mm2_weight_scale is not None, "share expert need share_mm2_weight_scale"
            if smooth_scales is not None:
                assert share_smooth_scales is not None, "share expert need share_smooth_scales"
                self.with_smooth = True
            else:
                self.with_smooth = False
            self.with_share = True
        else:
            self.with_share = False

    def _process_share_weights_after_loading(self, share_weight_datas):
        share_gmm1_weight, share_gmm1_weight_scale, share_gmm2_weight, share_gmm2_weight_scale, share_smooth_scales = share_weight_datas
        self.share_gmm1_weight = convert_tensor_into_parameter(share_gmm1_weight)
        self.share_gmm1_weight_scale = convert_tensor_into_parameter(share_gmm1_weight_scale)
        self.share_gmm2_weight = convert_tensor_into_parameter(share_gmm2_weight)
        self.share_gmm2_weight_scale = convert_tensor_into_parameter(share_gmm2_weight_scale)
        self.share_smooth_scales = convert_tensor_into_parameter(share_smooth_scales)

    def _process_weights_after_loading(self, weight_datas):
        gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale, smooth_scales = weight_datas
        self.gmm1_weight = convert_tensor_into_parameter(gmm1_weight)
        self.gmm1_weight_scale = convert_tensor_into_parameter(gmm1_weight_scale)
        self.gmm2_weight = convert_tensor_into_parameter(gmm2_weight)
        self.gmm2_weight_scale = convert_tensor_into_parameter(gmm2_weight_scale)
        self.smooth_scales = convert_tensor_into_parameter(smooth_scales)

    def _apply_ops(self, x, expert_ids, expert_scales, x_active_mask, buffer):
        raise NotImplementedError("To be implemented in subclass")

    def forward(self, x, expert_ids, expert_scales, x_active_mask, buffer):
        return self._apply_ops(x, expert_ids, expert_scales, x_active_mask, buffer)

class Ops(CustomOps):
    def __init__(self,
                 ep_hcomm_info,
                 meta_info,
                 weight_datas,
                 share_weight_datas):
        super().__init__(ep_hcomm_info, meta_info, weight_datas, share_weight_datas)
        self.shared_expert_rank_num = 0
        self.tp_hcomm_info = ""

    def _dynamic_quant(self, x):
        x_fp16 = x / self.share_smooth_scales if self.share_smooth_scales is not None else x
        scale = torch.abs(x_fp16).max(dim=-1, keepdim=True)[0] / 127.0
        scale = torch.clamp(scale, min=1e-8)
        x_int8 = torch.clamp(torch.round(x_fp16 / scale), -128, 127).to(torch.int8)
        return x_int8, scale.squeeze(-1)

    def _quant_matmul(self, x_int8, weight, weight_scale, pertoken_scale=None, output_dtype=None):
        if pertoken_scale is not None:
            scale = (pertoken_scale * weight_scale).to(output_dtype)
            result = (x_int8.to(output_dtype) @ weight.to(output_dtype)) * scale.unsqueeze(-1)
        else:
            result = (x_int8.to(torch.int32) @ weight.to(torch.int32)).to(output_dtype)
        return result

    def _swiglu_quant(self, x, weight_scale, activation_scale):
        # Dequant
        scale_factor = (activation_scale * weight_scale).to(torch.float16)
        x_fp16 = x.to(torch.float16) * scale_factor.unsqueeze(-1)
        
        # SwiGLU (activate_left=True, quant_mode=1)
        split = x_fp16.shape[-1] // 2
        swiglu_fp16 = x_fp16[..., :split] * torch.sigmoid(x_fp16[..., :split])
        
        # Quant
        scale = torch.abs(swiglu_fp16).max(dim=-1, keepdim=True)[0] / 127.0
        scale = torch.clamp(scale, min=1e-8)
        x_int8 = torch.clamp(torch.round(swiglu_fp16 / scale), -128, 127).to(torch.int8)
        
        return x_int8, scale.squeeze(-1)

    def share_compute(self, x):
        x1_int8, x1_scale = self._dynamic_quant(x)
        gmm1_result = self._quant_matmul(x1_int8, self.share_gmm1_weight, self.share_gmm1_weight_scale, pertoken_scale=None, output_dtype=torch.int32)
        x2_int8, x2_scale = self._swiglu_quant(gmm1_result, self.share_gmm1_weight_scale, x1_scale)
        gmm2_result = self._quant_matmul(x2_int8, self.share_gmm2_weight, self.share_gmm2_weight_scale, pertoken_scale=x2_scale, output_dtype=x.dtype)
        return gmm2_result

    def _apply_ops(self, x, expert_ids, expert_scales, x_active_mask, buffer):
        if self.with_share:
            share_output = self.share_compute(x)
        else:
            share_output = None

        num_tokens_per_rank, num_tokens_per_rdma_rank, num_tokens_per_expert, is_token_in_rank, _ = \
            buffer.get_dispatch_layout(expert_ids, self.moe_expert_num)

        num_sms = 24
        rdma_buffer_size, nvl_buffer_size = 128, 512
        config = deep_ep.Config(num_sms, 8, nvl_buffer_size, 16, rdma_buffer_size)

        dispatch_args = {
            'x': x,
            'num_tokens_per_rank': num_tokens_per_rank,
            'num_tokens_per_rdma_rank': num_tokens_per_rdma_rank,
            'is_token_in_rank': is_token_in_rank,
            'num_tokens_per_expert': num_tokens_per_expert,
            'config': config,
            'async_finish': False,
            'topk_idx': expert_ids,
            'topk_weights': expert_scales,
            'use_fp8' : True,
        }

        recv, _, handle, event = buffer.dispatch(**dispatch_args)
        recv_x = recv[0]
        recv_x_scales = recv[1]
        event.current_stream_wait()

        output_dtype = x.dtype

        y1_int32 = grouped_gemm_nt_masked(
            recv_x,
            self.gmm1_weight,
            num_tokens_per_expert,
        )
        y1, y1_scale = dequant_swiglu_quant_gpu(
            y1_int32,
            self.gmm1_weight_scale,
            recv_x_scales,
            num_tokens_per_expert,
        )
        y2 = grouped_gemm_nt_masked(
            y1,
            self.gmm2_weight,
            num_tokens_per_expert,
            scale=self.gmm2_weight_scale,
            per_token_scale=y1_scale,
            output_dtype=torch.bfloat16,
        )
        combine_args = {
            'x': recv_x,
            'bias': (torch.ones_like(recv_x), torch.zeros_like(recv_x)),
            'handle': handle,
            'config': config,
            'async_finish': False,
            'topk_weights': expert_scales
        }

        combine_output, event = buffer.combine(**combine_args)
        event.current_stream_wait()
        return (combine_output, share_output, num_tokens_per_expert)

def generate_datas(batch_size,
                   token_hidden_size,
                   moe_intermediate_size,
                   ep_world_size,
                   moe_expert_num,
                   global_rank_id,
                   top_k=8,
                   enable_dynamic_bs=False,
                   with_mc2_mask=False,
                   with_share=False,
                   with_smooth=False,
                   share_expert_intermediate_size=None):
    moe_expert_num_per_rank = moe_expert_num // ep_world_size
    actual_bs = int(
        np.random.randint(2 if with_mc2_mask else 1, batch_size)
        if enable_dynamic_bs else batch_size)
    local_expert_num = moe_expert_num_per_rank
    gmm1_input_dim = token_hidden_size
    gmm1_output_dim = moe_intermediate_size * 2
    gmm2_input_dim = moe_intermediate_size
    gmm2_output_dim = token_hidden_size
    x = np.random.rand(actual_bs, token_hidden_size).astype(np.float32) * 10 - 5
    expert_ids = np.arange(
        global_rank_id * batch_size * top_k,
        global_rank_id * batch_size * top_k + actual_bs * top_k,
        dtype=np.int32).reshape(actual_bs, top_k)
    expert_ids = expert_ids % moe_expert_num
    gmm1_weight = np.random.randint(
        -16, 16,
        [local_expert_num, gmm1_input_dim, gmm1_output_dim]).astype(np.int8)
    gmm2_weight = np.random.randint(
        -16, 16,
        [local_expert_num, gmm2_input_dim, gmm2_output_dim]).astype(np.int8)
    gmm1_weight_scale = (np.random.rand(local_expert_num, gmm1_output_dim
                                        ).astype(np.float32) * 0.003 + 0.0015)
    gmm2_weight_scale = (np.random.rand(local_expert_num, gmm2_output_dim
                                        ).astype(np.float32) * 0.003 + 0.0015)
    expert_scales = np.random.rand(actual_bs, top_k).astype(np.float32)
    # Generate shared expert weights
    share_mm1_weight = None
    share_mm1_weight_scale = None
    share_mm2_weight = None
    share_mm2_weight_scale = None
    if with_share:
        # Use share_expert_intermediate_size for shared expert gmm1HLen
        share_gmm2_input_dim = share_expert_intermediate_size if share_expert_intermediate_size is not None else moe_intermediate_size
        share_gmm1_output_dim = share_gmm2_input_dim * 2
        share_mm1_weight = np.ones([gmm1_input_dim, share_gmm1_output_dim]).astype(np.int8) * 4
        share_mm2_weight = np.ones([share_gmm2_input_dim, gmm2_output_dim]).astype(np.int8) * 4
        share_mm1_weight_scale = np.ones([share_gmm1_output_dim]) * 0.0015
        share_mm2_weight_scale = np.ones([gmm2_output_dim]) * 0.0015
        share_mm1_weight[:, ::2] = share_mm1_weight[:, ::2] * -1
        share_mm2_weight[:, ::2] = share_mm2_weight[:, ::2] * -1
    smooth_scales = None
    share_smooth_scales = None
    if with_smooth:
        smooth_scales = torch.rand([moe_expert_num, token_hidden_size])
        share_smooth_scales = torch.rand([token_hidden_size]).to(x.dtype)
    x_active_mask = None
    valid_token_num = actual_bs
    if with_mc2_mask:
        valid_token_num = int(np.random.randint(1, actual_bs))
        x_active_mask = np.concatenate(
            [np.ones(valid_token_num),
             np.zeros(actual_bs - valid_token_num)]).astype(bool)
    return (x, expert_ids, expert_scales, x_active_mask), \
            (gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale, smooth_scales), \
            (share_mm1_weight, share_mm1_weight_scale, share_mm2_weight, share_mm2_weight_scale, share_smooth_scales), \
            actual_bs, valid_token_num

CASE_4RANK = {
    "totalExpertNum": 16,
    "topk": 8,
    "batchSize": 16,
    "hiddenSize": 7168,
    "intermediateHiddenSize": 2048,
    "dynamicEPLB": False,
    "with_mc2_mask": False,
}

CASE_8RANK = {
    "totalExpertNum": 16,
    "topk": 8,
    "batchSize": 32,
    "hiddenSize": 7168,
    "intermediateHiddenSize": 2048,
    "dynamicEPLB": True,
    "with_mc2_mask": False,
}

def test_base_test():
    rank = int(os.environ.get("RANK", 0))
    worldSize = int(os.environ.get("WORLD_SIZE", 1))
    ip = os.getenv('MASTER_ADDR', '127.0.0.1')
    port = int(os.getenv('MASTER_PORT', '8361'))

    case = CASE_4RANK
    totalExpertNum = case["totalExpertNum"]
    topk = case["topk"]
    hiddenSize = case["hiddenSize"]
    intermediateHiddenSize = case["intermediateHiddenSize"]
    batchSize = case["batchSize"]
    dynamicEPLB = case["dynamicEPLB"]
    with_mc2_mask = case["with_mc2_mask"]
    test_bfloat16 = True

    torch.cuda.set_device(rank)
    device = torch.device(f"cuda:{rank}")
    dist.init_process_group(
        backend='nccl',
        device_id = device,
        rank=rank,
        world_size=worldSize
    )
    torch.set_default_dtype(torch.bfloat16)
    torch.set_default_device(device)

    ep_ranks_list = list(np.arange(0, worldSize))
    ep_group = dist.new_group(backend="nccl", ranks=ep_ranks_list)

    ep_hcomm_info = ep_group._get_backend(
        torch.device("cuda")).get_hccl_comm_name(rank)

    torch.cuda.synchronize()
    
    # 构造输入数据
    dynamicBS = False
    with_share = False
    with_smooth = False
    share_expert_intermediate_size = 0
    parameter = (batchSize, hiddenSize, intermediateHiddenSize,
                 worldSize, totalExpertNum, rank, topk, dynamicBS, with_mc2_mask,
                 with_share, with_smooth, share_expert_intermediate_size)
    input_datas, weight_datas, share_weight_datas, actual_bs, valid_token_num = generate_datas(*parameter)

    x_dtype = torch.bfloat16 if test_bfloat16 else torch.float16
    scale_dtype = torch.bfloat16 if test_bfloat16 else torch.float32
    x_np, expert_ids_np, expert_scales_np, x_active_mask_np = input_datas
    buffer = deep_ep.Buffer(
    num_ranks=worldSize,
    hidden_size=hiddenSize,
    use_fp8=True,
    round_scale=True,
    use_ue8m0=True,
    )
    input_datas = [
        torch.from_numpy(x_np).to(dtype=x_dtype).cuda(),
        torch.from_numpy(expert_ids_np).cuda(),
        torch.from_numpy(expert_scales_np).cuda(),
        torch.from_numpy(x_active_mask_np).cuda() if x_active_mask_np is not None else None,
        buffer,
    ]
    meta_info = (batchSize, worldSize, totalExpertNum, rank, dynamicEPLB)
    gmm1_w, gmm1_ws, gmm2_w, gmm2_ws, smooth_scales = weight_datas
    weight_datas = [
        torch.from_numpy(gmm1_w).cuda(),
        torch.from_numpy(gmm1_ws).float().cuda(),
        torch.from_numpy(gmm2_w).cuda(),
        torch.from_numpy(gmm2_ws).to(dtype=scale_dtype).cuda(),
        None if smooth_scales is None else torch.from_numpy(smooth_scales).float().cuda()
    ]
    share_mm1_w, share_mm1_ws, share_mm2_w, share_mm2_ws, share_smooth_scales = share_weight_datas
    share_weight_datas = [
        None if share_mm1_w is None else torch.from_numpy(share_mm1_w).cuda(),
        None if share_mm1_ws is None else torch.from_numpy(share_mm1_ws).float().cuda(),
        None if share_mm2_w is None else torch.from_numpy(share_mm2_w).cuda(),
        None if share_mm2_ws is None else torch.from_numpy(share_mm2_ws).to(dtype=scale_dtype).cuda(),
        None if share_smooth_scales is None else torch.from_numpy(share_smooth_scales).to(x_dtype).cuda()
    ]
    ops = Ops(ep_hcomm_info, meta_info, weight_datas, share_weight_datas).cuda()
    op_token_output, op_share_output, op_count_output = ops(*input_datas)
    torch.cuda.synchronize()

    if with_share:
        share_token_np = op_share_output.cpu().float().numpy()
if __name__ == "__main__":
    test_base_test()
```

替换后：
```python
import torch
import torch_npu
import numpy as np
import torch.distributed as dist
from collections import defaultdict
import gc
import os
import sys
import math
import socket
import umdk_cam_op_lib

torch_npu.npu.config.allow_internal_format = True

def convert_tensor_into_parameter(tensor, trans_nz=False):
    if tensor is None:
        return None
    if trans_nz:
        tensor = torch_npu.npu_format_cast(tensor, torch_npu.Format.FRACTAL_NZ)
    return torch.nn.Parameter(tensor, requires_grad=False)

class CustomOps(torch.nn.Module):

    def __init__(self,
                 ep_hcomm_info,
                 meta_info,
                 weight_datas,
                 share_weight_datas):
        super().__init__()
        self.ep_hcomm_info = ep_hcomm_info
        batch_size, ep_world_size, moe_expert_num, global_rank_id, dynamic_eplb = meta_info
        self.ep_world_size = ep_world_size
        self.moe_expert_num = moe_expert_num
        self.global_rank_id = global_rank_id
        self.dynamic_eplb = dynamic_eplb
        self.global_batch_size = batch_size * ep_world_size
        self.with_share = None
        self.with_smooth = None
        self._checkout_datas(weight_datas, share_weight_datas)
        self._process_share_weights_after_loading(share_weight_datas)
        self._process_weights_after_loading(weight_datas)

    def _checkout_datas(self, weight_datas, share_weight_datas):
        gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale, smooth_scales = weight_datas
        share_mm1_weight, share_mm1_weight_scale, share_mm2_weight, share_mm2_weight_scale, share_smooth_scales = share_weight_datas
        if share_mm1_weight is not None:
            assert share_mm1_weight_scale is not None, "share expert need share_mm1_weight_scale"
            assert share_mm2_weight is not None, "share expert need share_mm2_weight"
            assert share_mm2_weight_scale is not None, "share expert need share_mm2_weight_scale"
            if smooth_scales is not None:
                assert share_smooth_scales is not None, "share expert need share_smooth_scales"
                self.with_smooth = True
            else:
                self.with_smooth = False
            self.with_share = True
        else:
            self.with_share = False

    def _process_share_weights_after_loading(self, share_weight_datas):
        share_gmm1_weight, share_gmm1_weight_scale, share_gmm2_weight, share_gmm2_weight_scale, share_smooth_scales = share_weight_datas
        self.share_gmm1_weight = convert_tensor_into_parameter(share_gmm1_weight, trans_nz=True)
        self.share_gmm1_weight_scale = convert_tensor_into_parameter(share_gmm1_weight_scale)
        self.share_gmm2_weight = convert_tensor_into_parameter(share_gmm2_weight, trans_nz=True)
        self.share_gmm2_weight_scale = convert_tensor_into_parameter(share_gmm2_weight_scale)
        self.share_smooth_scales = convert_tensor_into_parameter(share_smooth_scales)

    def _process_weights_after_loading(self, weight_datas):
        gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale, smooth_scales = weight_datas
        self.gmm1_weight = convert_tensor_into_parameter(gmm1_weight, trans_nz=True)
        self.gmm1_weight_scale = convert_tensor_into_parameter(gmm1_weight_scale)
        self.gmm2_weight = convert_tensor_into_parameter(gmm2_weight, trans_nz=True)
        self.gmm2_weight_scale = convert_tensor_into_parameter(gmm2_weight_scale)
        self.smooth_scales = convert_tensor_into_parameter(smooth_scales)

    def _apply_ops(self, x, expert_ids, expert_scales, x_active_mask):
        raise NotImplementedError("To be implemented in subclass")

    def forward(self, x, expert_ids, expert_scales, x_active_mask):
        return self._apply_ops(x, expert_ids, expert_scales, x_active_mask)


class Ops(CustomOps):

    def __init__(self,
                 ep_hcomm_info,
                 meta_info,
                 weight_datas,
                 share_weight_datas):
        super().__init__(ep_hcomm_info, meta_info, weight_datas, share_weight_datas)

    def _apply_ops(self, x, expert_ids, expert_scales, x_active_mask):
        output, share_output, expert_token_nums = torch.ops.umdk_cam_op_lib.fused_deep_moe(
            x=x,
            expert_ids=expert_ids,
            gmm1_weight=self.gmm1_weight,
            gmm1_weight_scale=self.gmm1_weight_scale,
            gmm2_weight=self.gmm2_weight,
            gmm2_weight_scale=self.gmm2_weight_scale,
            expert_scales=expert_scales,
            share_gmm1_weight=self.share_gmm1_weight,
            share_gmm1_weight_scale=self.share_gmm1_weight_scale,
            share_gmm2_weight=self.share_gmm2_weight,
            share_gmm2_weight_scale=self.share_gmm2_weight_scale,
            expert_smooth_scales=self.smooth_scales,
            share_smooth_scales=self.share_smooth_scales_fp32,
            x_active_mask=x_active_mask,
            group_ep=self.ep_hcomm_info,
            ep_rank_size=self.ep_world_size,
            ep_rank_id=self.global_rank_id,
            moe_expert_num=self.moe_expert_num,
            quant_mode=0,
            global_bs=self.global_batch_size)
        return (output, share_output, expert_token_nums)

    def _process_share_weights_after_loading(self, share_weight_datas):
        super()._process_share_weights_after_loading(share_weight_datas)
        _, _, _, _, share_smooth_scales = share_weight_datas
        if self.with_share and self.with_smooth:
            self.share_smooth_scales_fp32 = convert_tensor_into_parameter(share_smooth_scales.float())
        else:
            self.share_smooth_scales_fp32 = None

    def _process_weights_after_loading(self, weight_datas):
        gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale, smooth_scales = weight_datas
        gmm1_weight = convert_tensor_into_parameter(gmm1_weight, trans_nz=True)
        gmm1_weight_scale = convert_tensor_into_parameter(gmm1_weight_scale)
        gmm2_weight = convert_tensor_into_parameter(gmm2_weight, trans_nz=True)
        gmm2_weight_scale = convert_tensor_into_parameter(gmm2_weight_scale)
        if self.dynamic_eplb:
            self.gmm1_weight = [
                weight.clone() for weight in gmm1_weight.unbind(dim=0)
            ]
            self.gmm1_weight_scale = [
                weight.clone() for weight in gmm1_weight_scale.unbind(dim=0)
            ]
            self.gmm2_weight = [
                weight.clone() for weight in gmm2_weight.unbind(dim=0)
            ]
            self.gmm2_weight_scale = [
                weight.clone() for weight in gmm2_weight_scale.unbind(dim=0)
            ]
        else:
            self.gmm1_weight = [gmm1_weight.clone()]
            self.gmm1_weight_scale = [gmm1_weight_scale.clone()]
            self.gmm2_weight = [gmm2_weight.clone()]
            self.gmm2_weight_scale = [gmm2_weight_scale.clone()]
        self.smooth_scales = convert_tensor_into_parameter(smooth_scales)

def generate_datas(batch_size,
                   token_hidden_size,
                   moe_intermediate_size,
                   ep_world_size,
                   moe_expert_num,
                   global_rank_id,
                   top_k=8,
                   enable_dynamic_bs=False,
                   with_mc2_mask=False,
                   with_share=False,
                   with_smooth=False,
                   share_expert_intermediate_size=None):
    moe_expert_num_per_rank = moe_expert_num // ep_world_size
    actual_bs = int(
        np.random.randint(2 if with_mc2_mask else 1, batch_size)
        if enable_dynamic_bs else batch_size)
    local_expert_num = moe_expert_num_per_rank
    gmm1_input_dim = token_hidden_size
    gmm1_output_dim = moe_intermediate_size * 2
    gmm2_input_dim = moe_intermediate_size
    gmm2_output_dim = token_hidden_size
    x = np.random.rand(actual_bs, token_hidden_size).astype(np.float32) * 10 - 5
    expert_ids = np.arange(
        global_rank_id * batch_size * top_k,
        global_rank_id * batch_size * top_k + actual_bs * top_k,
        dtype=np.int32).reshape(actual_bs, top_k)
    expert_ids = expert_ids % moe_expert_num
    gmm1_weight = np.random.randint(
        -16, 16,
        [local_expert_num, gmm1_input_dim, gmm1_output_dim]).astype(np.int8)
    gmm2_weight = np.random.randint(
        -16, 16,
        [local_expert_num, gmm2_input_dim, gmm2_output_dim]).astype(np.int8)
    gmm1_weight_scale = (np.random.rand(local_expert_num, gmm1_output_dim
                                        ).astype(np.float32) * 0.003 + 0.0015)
    gmm2_weight_scale = (np.random.rand(local_expert_num, gmm2_output_dim
                                        ).astype(np.float32) * 0.003 + 0.0015)
    expert_scales = np.random.rand(actual_bs, top_k).astype(np.float32)
    # Generate shared expert weights
    share_mm1_weight = None
    share_mm1_weight_scale = None
    share_mm2_weight = None
    share_mm2_weight_scale = None
    if with_share:
        # Use share_expert_intermediate_size for shared expert gmm1HLen
        share_gmm2_input_dim = share_expert_intermediate_size if share_expert_intermediate_size is not None else moe_intermediate_size
        share_gmm1_output_dim = share_gmm2_input_dim * 2
        share_mm1_weight = np.ones([gmm1_input_dim, share_gmm1_output_dim]).astype(np.int8) * 4
        share_mm2_weight = np.ones([share_gmm2_input_dim, gmm2_output_dim]).astype(np.int8) * 4
        share_mm1_weight_scale = np.ones([share_gmm1_output_dim]) * 0.0015
        share_mm2_weight_scale = np.ones([gmm2_output_dim]) * 0.0015
        share_mm1_weight[:, ::2] = share_mm1_weight[:, ::2] * -1
        share_mm2_weight[:, ::2] = share_mm2_weight[:, ::2] * -1
    smooth_scales = None
    share_smooth_scales = None
    if with_smooth:
        smooth_scales = torch.rand([moe_expert_num, token_hidden_size])
        share_smooth_scales = torch.rand([token_hidden_size]).to(x.dtype)
    x_active_mask = None
    valid_token_num = actual_bs
    if with_mc2_mask:
        valid_token_num = int(np.random.randint(1, actual_bs))
        x_active_mask = np.concatenate(
            [np.ones(valid_token_num),
             np.zeros(actual_bs - valid_token_num)]).astype(bool)
    return (x, expert_ids, expert_scales, x_active_mask), \
            (gmm1_weight, gmm1_weight_scale, gmm2_weight, gmm2_weight_scale, smooth_scales), \
            (share_mm1_weight, share_mm1_weight_scale, share_mm2_weight, share_mm2_weight_scale, share_smooth_scales), \
            actual_bs, valid_token_num

CASE_4RANK = {
    "totalExpertNum": 16,
    "topk": 8,
    "batchSize": 16,
    "hiddenSize": 7168,
    "intermediateHiddenSize": 2048,
    "dynamicEPLB": False,
    "with_mc2_mask": False,
}

CASE_8RANK = {
    "totalExpertNum": 16,
    "topk": 8,
    "batchSize": 32,
    "hiddenSize": 7168,
    "intermediateHiddenSize": 2048,
    "dynamicEPLB": True,
    "with_mc2_mask": False,
}

def test_base_test():

    rank = int(os.environ.get("RANK", 0))
    worldSize = int(os.environ.get("WORLD_SIZE", 1))
    ip = os.getenv('MASTER_ADDR', '127.0.0.1')
    port = int(os.getenv('MASTER_PORT', '8361'))

    case = CASE_4RANK
    totalExpertNum = case["totalExpertNum"]
    topk = case["topk"]
    hiddenSize = case["hiddenSize"]
    intermediateHiddenSize = case["intermediateHiddenSize"]
    batchSize = case["batchSize"]
    dynamicEPLB = case["dynamicEPLB"]
    with_mc2_mask = case["with_mc2_mask"]
    test_bfloat16 = True

    # 构造通信域
    torch.npu.set_device(rank)
    device = torch.device(f"npu:{rank}")
    dist.init_process_group(
        backend='hccl',
        device_id = device,
        rank=rank,
        world_size=worldSize
    )
    torch.set_default_dtype(torch.bfloat16)
    torch.set_default_device(device)

    ep_ranks_list = list(np.arange(0, worldSize))
    ep_group = dist.new_group(backend="hccl", ranks=ep_ranks_list)

    ep_hcomm_info = ep_group._get_backend(
        torch.device("npu")).get_hccl_comm_name(rank)
    torch_npu.npu.synchronize()
    
    # 构造输入数据
    dynamicBS = False
    with_share = False
    with_smooth = False
    share_expert_intermediate_size = 0
    parameter = (batchSize, hiddenSize, intermediateHiddenSize,
                 worldSize, totalExpertNum, rank, topk, dynamicBS, with_mc2_mask,
                 with_share, with_smooth, share_expert_intermediate_size)
    input_datas, weight_datas, share_weight_datas, actual_bs, valid_token_num = generate_datas(*parameter)

    x_dtype = torch.bfloat16 if test_bfloat16 else torch.float16
    scale_dtype = torch.bfloat16 if test_bfloat16 else torch.float32
    x_np, expert_ids_np, expert_scales_np, x_active_mask_np = input_datas
    input_datas = [
        torch.from_numpy(x_np).to(dtype=x_dtype).npu(),
        torch.from_numpy(expert_ids_np).npu(),
        torch.from_numpy(expert_scales_np).npu(),
        torch.from_numpy(x_active_mask_np).npu() if x_active_mask_np is not None else None,
    ]
    meta_info = (batchSize, worldSize, totalExpertNum, rank, dynamicEPLB)
    gmm1_w, gmm1_ws, gmm2_w, gmm2_ws, smooth_scales = weight_datas
    weight_datas = [
        torch.from_numpy(gmm1_w).npu(),
        torch.from_numpy(gmm1_ws).float().npu(),
        torch.from_numpy(gmm2_w).npu(),
        torch.from_numpy(gmm2_ws).to(dtype=scale_dtype).npu(),
        None if smooth_scales is None else torch.from_numpy(smooth_scales).float().npu()
    ]
    share_mm1_w, share_mm1_ws, share_mm2_w, share_mm2_ws, share_smooth_scales = share_weight_datas
    share_weight_datas = [
        None if share_mm1_w is None else torch.from_numpy(share_mm1_w).npu(),
        None if share_mm1_ws is None else torch.from_numpy(share_mm1_ws).float().npu(),
        None if share_mm2_w is None else torch.from_numpy(share_mm2_w).npu(),
        None if share_mm2_ws is None else torch.from_numpy(share_mm2_ws).to(dtype=scale_dtype).npu(),
        None if share_smooth_scales is None else torch.from_numpy(share_smooth_scales).to(x_dtype).npu()
    ]
    ops = Ops(ep_hcomm_info, meta_info, weight_datas, share_weight_datas).npu()
    op_token_output, op_share_output, op_count_output = ops(*input_datas)
    torch_npu.npu.synchronize()
    if with_share:
        share_token_np = op_share_output.cpu().float().numpy()

if __name__ == "__main__":
    test_base_test()
```