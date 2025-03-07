import torch
import torch.nn as nn
from safetensors import safe_open

import turbomind as tm
from turbomind.utils import unpack_awq_gemm

torch.manual_seed(0)

# def dequantize(qweight, qzeros, scales, group_size: int = 128):
#     _qweight = unpack_awq_gemm(qweight)
#     _qzeros = unpack_awq_gemm(qzeros)
#     _qzeros = _qzeros.float()
#     _qweight = _qweight.float()
#     _scales = scales.float()
#     for i in range(qzeros.shape[0]):
#         start = i * group_size
#         end = start + group_size
#         _qweight[start:end] = (_qweight[start:end, :] -
#                                _qzeros[i:i + 1, :]) * _scales[i:i + 1, :]
#     return _qweight.half()

# def load_specified_linear_weights(path):
#     ckpt_path = path  # noqa
#     layer_id = 0
#     # prefix = f'model.layers.{layer_id}.self_attn.q_proj.'
#     prefix = f'model.layers.{layer_id}.mlp.down_proj.'
#     keys = ['qweight', 'qzeros', 'scales']
#     tensors = {}
#     with safe_open(ckpt_path, framework='pt', device='cuda:2') as f:
#         for key in keys:
#             tensors[key] = f.get_tensor(prefix + key)

#     return tensors['qweight'], tensors['qzeros'], tensors['scales']

def load_specified_linear_weights():
    ckpt_path = 'model-00001-of-000163.safetensors'  # noqa
    layer_id = 0
    # prefix = f'model.layers.{layer_id}.self_attn.q_proj.'
    prefix = f'model.layers.{layer_id}.mlp.down_proj.'
    keys = ['weight', 'weight_scale_inv']
    tensors = {}
    with safe_open(ckpt_path, framework='pt', device='cuda:2') as f:
        for key in keys:
            tensors[key] = f.get_tensor(prefix + key)

    return tensors['weight'], tensors['weight_scale_inv']

def dequantize_weights_torch(quantized_weights: torch.Tensor, scale_matrix: torch.Tensor) -> torch.Tensor:
    # 确认尺寸匹配
    H, W = quantized_weights.shape
    assert H % 128 == 0 and W % 128 == 0, "权重矩阵尺寸必须是 128 的倍数"
    assert scale_matrix.shape == (H // 128, W // 128), "scale 矩阵尺寸不匹配"
    scale_expanded = torch.kron(
        scale_matrix,
        torch.ones((128, 128), dtype=scale_matrix.dtype, device=scale_matrix.device)
    )

    dequantized = quantized_weights.float() * scale_expanded.float()
    return dequantized.half()

weight, weight_scale = load_specified_linear_weights()
# print(weight, weight_scale)
dequant_weight = dequantize_weights_torch(weight, weight_scale)
# print(dequant_weight, dequant_weight.shape)


# group_size_awq = 64
batch_size = 16384

# qweight_ref, qzeros_ref, scales_ref = load_specified_linear_weights('/root/turbomind/model-00001-of-00074.safetensors')
# 18432
in_features = dequant_weight.shape[1]
# 7168
out_features = dequant_weight.shape[0]

x = torch.randn((batch_size, in_features),
                device=dequant_weight.device,
                dtype=torch.float16) * 0.1

# weight_awq = dequantize(qweight_ref, qzeros_ref, scales_ref, group_size_awq)
print(f'-- dequantization: weight_awq.shape={dequant_weight.shape}, weight_awq: \n{dequant_weight}')
awq_linear = nn.Linear(in_features, out_features, bias=False, device='cuda:2')
with torch.no_grad():
    awq_linear.weight = nn.Parameter(dequant_weight)
    awq_res = awq_linear(x)
    print(awq_linear.weight.shape)
    print(f'nn.linear.awq_res: {awq_res}, {awq_res.shape}')


def load_weight(path):
    file_path = path
    layer_id = 0
    prefix = f'model.layers.{layer_id}.mlp.down_proj.'
    keys = ['input_scale', 
            'weight', 
            'weight_scale', 
            'weight_scale_2']
    tensors = {}
    # 使用 safe_open 打开文件
    with safe_open(file_path, framework="pt", device="cuda:2") as f:
        # 获取所有键
        # keys = f.keys()
        # print("Keys in the safetensors file:", keys)
        for key in keys:
            tensor = f.get_tensor(prefix + key)
            tensors[key] = tensor
            # print(f"Tensor '{key}': shape={tensor.shape}, dtype={tensor.dtype}")
    return tensors

def unpack_uint8_to_fp4(x: torch.Tensor) -> torch.Tensor:
    assert x.dtype == torch.uint8, "Input tensor must be of type torch.uint8"
    
    low = x & 0x0F
    high = (x >> 4) & 0x0F
    unpacked = torch.stack([low, high], dim=-1).view(*x.shape[:-1], -1)
    
    original_shape = unpacked.shape
    unpacked_flat = unpacked.view(-1)
    
    s = (unpacked_flat >> 3) & 0x01
    e_code = (unpacked_flat >> 1) & 0x03
    m = unpacked_flat & 0x01
    
    s = s.float()
    e_code = e_code.int()
    m = m.float()
    
    val_subnormal = m * 0.5  # 0.5（尾数） * 2^-1（指数）
    exponent = (e_code - 1).float()
    val_normal = (1.0 + m * 0.5) * torch.pow(2.0, exponent)
    
    e_code_zero = (e_code == 0)
    val = torch.where(e_code_zero, val_subnormal, val_normal)
    
    val = val * torch.where(s > 0.5, -1.0, 1.0)
    
    val_fp8 = val.to(torch.float8_e4m3fn)
    val_fp8 = val_fp8.view(original_shape)
    
    return val_fp8


def dequantize_w(w, w_block_scale, w_global_scale, group_size):
    # weight = tensors['weight']
    # weight_scale = tensors['weight_scale']
    # weight_scale_2 = tensors['weight_scale_2']

    w = unpack_uint8_to_fp4(w)
    w = w.float()
    # print(w)
    w_block_scale = w_block_scale.float()

    for i in range(w_block_scale.shape[-1]):
        start = i * group_size
        end = start + group_size
        w[:, start:end] = (w[:, start:end] * w_block_scale[:, i:i + 1])
    
    w = w * w_global_scale

    return w.half()

def dequantize_x(x, x_block_scale, x_global_scale, group_size):
    # weight = tensors['weight']
    # weight_scale = tensors['weight_scale']
    # weight_scale_2 = tensors['weight_scale_2']

    # x = unpack_uint8_to_fp4(x)
    # x = x.float()
    x_block_scale = x_block_scale.float()

    for i in range(x_block_scale.shape[-1]):
        start = i * group_size
        end = start + group_size
        x[:, start:end] = (x[:, start:end] * x_block_scale[:, i:i + 1])
    
    x = x * x_global_scale

    return x.half()


sorted_candidates = torch.tensor([0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0], dtype=torch.float32).to("cuda:2")

def quantize_to_fp4_e2m1(x : torch.Tensor, x_global_scale, group_size):
    org_shape = x.shape
    # [n_group, group_size]
    x = x.reshape(-1, group_size)
    # [n_group, 1]
    max_val = x.abs().amax(dim=1, keepdim=True)
    max_val = max_val.clamp(min=1e-5)

    x_block_scale = max_val / 6.0
    x_fp4 = x / x_block_scale
    # 计算绝对值
    abs_x = torch.abs(x_fp4)
    # 找到插入位置
    indices = torch.bucketize(abs_x, sorted_candidates)
    # 确保索引不越界
    indices = torch.clamp(indices, 0, len(sorted_candidates) - 1)
    # 左鄰居索引
    left_idx = torch.clamp(indices - 1, 0)
    # 获取左右候选值
    left_val = sorted_candidates[left_idx]
    right_val = sorted_candidates[indices]
    # 计算差异
    left_diff = torch.abs(abs_x - left_val)
    right_diff = torch.abs(abs_x - right_val)
    # 选择更接近的候选值
    mask = left_diff < right_diff
    selected_val = torch.where(mask, left_val, right_val)
    # 处理超出最大值的情况
    max_val = sorted_candidates[-1]
    selected_val = torch.where(abs_x > max_val, max_val, selected_val)
    # 应用符号
    x_fp4 = selected_val * torch.sign(x_fp4)
    # use global_scale to quantize block_scale
    x_block_scale = torch.clamp((x_block_scale / x_global_scale), -448.0, 448.0).to(torch.float8_e4m3fn)
    # [ci, co/group_size]
    x_block_scale = x_block_scale.view(org_shape[0], -1)

    x_fp4 = x_fp4.reshape(org_shape)
    return x_fp4, x_block_scale



tensors = load_weight('/root/turbomind/model-00001-of-00080.safetensors')
input_global_scale = tensors['input_scale']
weight_fp4 = tensors['weight']
weight_block_scale = tensors['weight_scale']
weight_global_scale = tensors['weight_scale_2']

group_size = 16
batch_size = 16384
# 7186
in_features = tensors['weight'].shape[0]

# 18432
out_features = tensors['weight'].shape[1]*2

input = x
# print(input)
input_fp4, input_block_scale = quantize_to_fp4_e2m1(input, input_global_scale, group_size)
# print(input_fp4, input_block_scale)
input = dequantize_x(input_fp4, input_block_scale, input_global_scale, group_size)
# print(input)

weight = dequantize_w(weight_fp4, weight_block_scale, weight_global_scale, group_size)
print(f'-- dequantization: weight.shape={weight.shape}, weight: \n{weight}')

fp4_linear = nn.Linear(out_features, in_features, bias=False, device='cuda:2')
with torch.no_grad():
    # print(fp4_linear.weight.shape)
    fp4_linear.weight = nn.Parameter(weight)
    # print(fp4_linear.weight.shape)
    fp4_res = fp4_linear(input)
    print(f'nn.linear.res: {fp4_res}, {fp4_res.shape}')


abs_diff = torch.abs(fp4_res - awq_res).float()
rel_diff = abs_diff / torch.max(torch.abs(awq_res), torch.abs(fp4_res))
rtol = 0.01
atol = 0.0001
outliers = abs_diff > atol + rtol * torch.abs(awq_res)
abs_diff = torch.sum(abs_diff) / abs_diff.numel()
rel_diff = torch.sum(rel_diff) / rel_diff.numel()
outliers = torch.sum(outliers) / outliers.shape[0]
print(f'abs_diff {abs_diff:4f}, '
      f'rel_diff {rel_diff:4f}, '
      f'outliers {outliers:4f}')
