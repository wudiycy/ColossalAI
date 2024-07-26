from typing import Any, Callable, List, Optional, Tuple, Union, cast

import torch
import torch.nn.functional as F
import torch.distributed as dist


def cast_to_fp8(inp: torch.Tensor, fp8_format="e4m3") -> (torch.Tensor, torch.Tensor):
    r"""
    casting torch Tensor into specified fp8 tensor with per-channel scaling or per-tensor scaling.
    Args:
        inp: input torch Tensor, should be in torch.FloatTensor, torch.HalfTensor, torch.BFloat16Tensor.
        scale: scaling factor for fp8 casting. If it is None, then it is computed automatically. Per-channel scaling
        is applied if input tensor is 2 dimension, otherwise, per-tensor scaling is applied.
        fp8_format: e4m3 or e5m2

    Returns:
        Tuples: A tuple (fp8_tensor, scale)
    """

    if inp.dtype not in [torch.float32, torch.float16, torch.bfloat16]:
        raise TypeError("Only float16, bfloat16, and float32 are allowed.")

    fp8_type = torch.float8_e4m3fn if fp8_format == "e4m3" else torch.float8_e5m2
    fp8_max = torch.finfo(fp8_type).max

    if inp.dim() == 2:
        per_channel_max = inp.abs().max(dim=-1).values.float()
        per_channel_max = torch.where(per_channel_max > 0, per_channel_max, 1.0)
        scale = fp8_max / per_channel_max[:, None]
        scale_inv = per_channel_max / fp8_max
    else:
        per_tensor_max = inp.abs().max().float()
        per_tensor_max = torch.where(per_tensor_max > 0, per_tensor_max, 1.0)
        scale = fp8_max / per_tensor_max
        scale_inv = 1.0 / scale

    ret = (scale * inp.float()).to(fp8_type)
    return ret, scale_inv


def cast_from_fp8(inp: torch.Tensor, scale_inv: torch.Tensor, ret_type: torch.dtype) -> torch.Tensor:
    r"""

    Args:
        inp: should be a fp8 torch tensor in one of the types: [torch.float8_e4m3fn, torch.float8_e5m2].
        scale: scaling factor returned by cast_to_fp8 function.
        ret_type: the datatype of the returned tensor.

    Returns:
        torch.Tensor
    """
    if inp.dtype not in [torch.float8_e4m3fn, torch.float8_e5m2]:
        raise TypeError("Only float8_e4m3fn and float8_e5m2 are allowed.")

    if inp.dim() == 2:
        ret = scale_inv[:, None] * inp.float()
    else:
        ret = scale_inv * inp.float()
    return ret.to(ret_type)


def all_reduce_fp8(tensor: torch.Tensor, fp8_format="e4m3", group=None) -> None:
    r"""
    This is an in-place operation for compressed all_reduce using fp8.
    It works like dist.all_reduce but during communication the data is cast to fp8 format.

    Args:
        tensor: torch.Tensor in fp32, fp16, bf16 datatype.
        fp8_format: e4m3 or e5m2

    Returns:
        None
    """

    world_size = dist.get_world_size(group=group)
    input_type = tensor.dtype
    input_shape = tensor.shape
    input_device = tensor.device
    input_size = tensor.numel()
    flat_padded_x = tensor.flatten()

    if flat_padded_x.size(0) % world_size != 0:
        pad_size = world_size - flat_padded_x.size(0) % world_size
        flat_padded_x = F.pad(flat_padded_x, (0, pad_size))

    fp8_type = torch.float8_e4m3fn if fp8_format == "e4m3" else torch.float8_e5m2
    ret, scale = cast_to_fp8(flat_padded_x, fp8_format=fp8_format)

    inp = ret.view(torch.uint8)
    input_chunks = list(torch.chunk(inp, world_size, dim=0))
    output_chunks = list(torch.chunk(torch.empty_like(inp), world_size, dim=0))
    dist.all_to_all(output_chunks, input_chunks, group=group)
    scale_list = [torch.ones(1, dtype=scale.dtype, device=input_device) for _ in range(world_size)]
    dist.all_gather(scale_list, scale, group=group)
    summed_out = torch.zeros_like(output_chunks[0]).to(input_type)
    for scale, out in zip(scale_list, output_chunks):
        out = out.view(fp8_type)
        summed_out += cast_from_fp8(out, scale, input_type)
    summed_out.div_(world_size)

    summed_out_fp8, scale = cast_to_fp8(summed_out, fp8_format=fp8_format)
    dist.all_gather(scale_list, scale, group=group)

    tensor_list = [torch.empty_like(summed_out_fp8.view(torch.uint8)) for _ in range(world_size)]
    dist.all_gather(tensor_list, summed_out_fp8.view(torch.uint8), group=group)
    for i in range(world_size):
        tensor_list[i] = tensor_list[i].view(fp8_type).to(input_type) * scale_list[i]
    out = torch.cat(tensor_list, dim=0)
    tensor.copy_(out[:input_size].view(input_shape).to(input_type))



def cast_to_fp8_pipeline(inp: Any) -> None:
    """
    Cast the hidden_states tensor of inp object to fp8 format before p2p communication in pipeline.
    The activations tensor is indexed by 'hidden_states' in the inp dict.
    After FP8 casting, the resulting tensor is saved as float16 or bfloat16 format but the size becomes halved.
    Metadata such as fp8_scale is saved into inp dict for communication.
    """
    if inp is None:
        return
    # In pipeline parallelism, when inp is torch.Tensor, it only contains one element, thus can be omitted.
    if type(inp) == torch.Tensor:
        return

    assert 'hidden_states' in inp, 'required by pipeline parallelism.'
    assert inp["hidden_states"].size(-1) % 2 == 0, 'tensor size(-1) must be divisible by 2 to view Float8_e4m3fn as BFloat16 or Float16'
    inp_tensor = inp["hidden_states"]
    inp_dtype = inp_tensor.dtype

    min_val, max_val = inp_tensor.aminmax()
    amax = torch.maximum(min_val.abs(), max_val.abs())

    finfo = torch.finfo(torch.float8_e4m3fn)
    if amax > finfo.max:
        fp8_type = torch.float8_e5m2
        fp8_view_type = torch.float16
    else:
        fp8_type = torch.float8_e4m3fn
        fp8_view_type = torch.bfloat16

    finfo = torch.finfo(fp8_type)
    scale = torch.tensor(1.0).to(inp_tensor.device) if amax == 0.0 else finfo.max / amax.float()
    q_tensor = (inp_tensor.data.float() * scale)
    # Todo: Currently we use fp8_view_type <float16, bfloat16> to indicate which fp8 format is used. This is a temporary workaround due to 'Only support tensor for fast send'.
    #  inp_tensor needs to be a float datatype to avoid error during gradient placement.
    inp_tensor.data = q_tensor.to(fp8_type).view(fp8_view_type)

    inp["fp8_scale"] = scale.float().reciprocal()
    inp["dtype"] = torch.zeros_like(scale).to(inp_dtype)



def cast_from_fp8_pipeline(inp: Any, del_metadata=True) -> None:
    """
    Cast the FP8 encoded hidden_states tensor back to original dtype after p2p communication in pipeline.
    del_metadata = False is useful when this function is called before p2p communication.
    """
    if inp is None:
        return
    if type(inp) == torch.Tensor:
        return

    assert 'hidden_states' in inp, 'required by pipeline parallelism.'
    inp_tensor = inp["hidden_states"]
    scale = inp["fp8_scale"]

    fp8_view_type = inp_tensor.dtype
    if fp8_view_type == torch.float16:
        fp8_type = torch.float8_e5m2
    elif fp8_view_type == torch.bfloat16:
        fp8_type = torch.float8_e4m3fn
    else:
        raise TypeError("Only float16, bfloat16 are implemented.")

    inp_tensor.data = inp_tensor.data.view(fp8_type).to(inp["dtype"]) * scale

    if del_metadata:
        del inp["fp8_scale"]
        del inp["dtype"]


def reduce_scatter_fp8(output: torch.Tensor, input_list, group, fp8_format="e5m2") -> None:
    r"""
    This is an in-place operation for compressed reduce_scatter using fp8.
    It works like dist.reduce_scatter but during communication the data is cast to fp8 format.

    Args:
        tensor: torch.Tensor in fp32, fp16, bf16 datatype.
        fp8_format: e4m3 or e5m2

    Returns:
        None
    """

    input_type = output.dtype

    fp8_type = torch.float8_e4m3fn if fp8_format == "e4m3" else torch.float8_e5m2
    scale_list = []
    cast_input_list = []
    output_chunks = []
    output_scale_list = []
    for input in input_list:
        ret, scale = cast_to_fp8(input, fp8_format=fp8_format)
        scale_list.append(scale)
        ret = ret.view(torch.uint8)
        cast_input_list.append(ret)
        output_chunks.append(torch.empty_like(ret))
        output_scale_list.append(torch.empty_like(scale))
    dist.all_to_all(output_chunks, cast_input_list, group=group)
    dist.all_to_all(output_scale_list, scale_list, group=group)

    summed_out = torch.zeros_like(output_chunks[0]).to(input_type)
    for scale, out in zip(output_scale_list, output_chunks):
        out = out.view(fp8_type)
        summed_out += cast_from_fp8(out, scale, input_type)
    output.data = summed_out


def fp8_compress_ddp_grad_comm_hook_async(
    process_group: dist.ProcessGroup,
    bucket: dist.GradBucket,
    fp8_format: str = "e5m2",
) -> torch.futures.Future[torch.Tensor]:
    """
    Compress by casting ``GradBucket`` to FP8 floating-point format divided by process group size.

    This DDP communication hook implements a simple gradient compression approach that casts ``GradBucket`` tensor
    to FP8 floating-point format (``torch.float8_e5m2`` or ``torch.bfloat16_e4m3``), and then divides it
    by the process group size.
    Once compressed gradient tensors are allreduced, the chained callback ``decompress`` casts it back
    to the input data type (such as ``float32``).

    Example::
        >>> ddp_model.register_comm_hook(process_group, fp8_compress_ddp_grad_comm_hook_async)
    """
    group_to_use = process_group if process_group is not None else dist.group.WORLD

    input_tensor = bucket.buffer()
    world_size = dist.get_world_size()
    input_type = input_tensor.dtype
    input_device = input_tensor.device
    flat_padded_x = input_tensor.flatten()

    if flat_padded_x.size(0) % world_size != 0:
        pad_size = world_size - flat_padded_x.size(0) % world_size
        flat_padded_x = F.pad(flat_padded_x, (0, pad_size))

    fp8_type = torch.float8_e4m3fn if fp8_format == "e4m3" else torch.float8_e5m2
    ret, scale = cast_to_fp8(flat_padded_x, fp8_format=fp8_format)

    inp = ret.view(torch.uint8)
    output_chunks_single = torch.empty_like(inp)
    split_sizes = [inp.numel() // world_size for _ in range(world_size)]
    fut0 = dist.all_to_all_single(output_chunks_single, inp,
                                  output_split_sizes=split_sizes,
                                  input_split_sizes=split_sizes,
                                  group=group_to_use,
                                  async_op=True).get_future()

    scale_list = [torch.ones(1, dtype=scale.dtype, device=input_device) for _ in range(world_size)]
    fut1 = dist.all_gather_into_tensor(torch.cat(scale_list, dim=0), scale,
                                       group=group_to_use,
                                       async_op=True).get_future()
    all_to_all_fut = torch.futures.collect_all([fut0, fut1])

    def sum_and_allgather(fut):
        output_chunks_single = fut.value()[0].wait()[0]
        scale_list_single = fut.value()[1].wait()[0]

        output_chunks = list(torch.chunk(output_chunks_single, world_size, dim=0))
        scale_list = scale_list_single.chunk(world_size, dim=0)

        summed_out = torch.zeros_like(output_chunks[0]).to(input_type)
        for scale, out in zip(scale_list, output_chunks):
            out = out.view(fp8_type)
            summed_out += cast_from_fp8(out, scale, input_type)
        summed_out.div_(world_size)

        summed_out_fp8, scale = cast_to_fp8(summed_out, fp8_format=fp8_format)

        tensor_list_single = torch.empty(summed_out_fp8.size(0) * world_size, device=input_device, dtype=torch.uint8)
        fut2 = dist.all_gather_into_tensor(tensor_list_single, summed_out_fp8.view(torch.uint8), group=group_to_use,
                                           async_op=True).get_future()

        scale_list = [torch.ones(1, dtype=scale.dtype, device=input_device) for _ in range(world_size)]
        fut3 = dist.all_gather_into_tensor(torch.cat(scale_list, dim=0), scale, group=group_to_use,
                                           async_op=True).get_future()
        fut_combined2 = torch.futures.collect_all([fut2, fut3])
        return fut_combined2
    def decompress(fut):
        tensor_list_single = fut.value().wait()[0].value()[0]
        scale_list_single = fut.value().wait()[1].value()[0]

        tensor_list = list(torch.chunk(tensor_list_single, world_size, dim=0))
        scale_list = scale_list_single.chunk(world_size, dim=0)

        for i in range(world_size):
            tensor_list[i] = tensor_list[i].view(fp8_type).to(input_type) * scale_list[i]
        out = torch.cat(tensor_list, dim=0)

        input_tensor_size = input_tensor.numel()
        input_shape = input_tensor.shape
        out = out[:input_tensor_size]

        input_tensor.copy_(out.view(input_shape).to(input_type))
        return input_tensor

    return all_to_all_fut.then(sum_and_allgather).then(decompress)




def fp8_compress_ddp_grad_comm_hook_sync(
    process_group: dist.ProcessGroup,
    bucket: dist.GradBucket,
    fp8_format="e5m2",
) -> torch.futures.Future[torch.Tensor]:
    """
    Return a future that wraps the input, after the input is allreduced. However, the allreduce commnunication is synchronized.
    This breaks the overlapping between allreduce communication and backward compuation.

    This hook should **only** be used for debugging purposes, instead of the normal gradient synchronization.
    For asynchronized implementation, use fp8_compress_ddp_grad_comm_hook_async instead.

    Example::
        >>> # xdoctest: +SKIP
        >>> ddp_model.register_comm_hook(None, fp8_compress_ddp_grad_comm_hook_sync)
    """

    buffer = bucket.buffer()
    all_reduce_fp8(buffer, fp8_format=fp8_format)

    fut: torch.futures.Future[torch.Tensor] = torch.futures.Future()
    fut.set_result(bucket.buffer())

    return fut


def fp8_compress_fsdp_grad_comm_hook(state: object, unsharded_gradient_flattened: torch.Tensor,
                                     sharded_gradient: torch.Tensor, group=None, fp8_format="e5m2") -> None:
    """
    This communication hook implements a simple gradient compression approach that casts unsharded_gradient_flattened tensor
    to FP8 floating-point format (``torch.float8_e5m2`` or ``torch.bfloat16_e4m3``), and then perform scatter_allreduce logic
    by using all_to_all and all_gather among the process group.

    Example::
        >>> fsdp_model.register_comm_hook(None, fp8_compress_fsdp_grad_comm_hook)
    """
    grad = unsharded_gradient_flattened
    fp8_type = torch.float8_e4m3fn if fp8_format == "e4m3" else torch.float8_e5m2
    input_type = grad.dtype
    input_device = grad.device
    world_size = dist.get_world_size(group=group)

    grad_fp8, scale = cast_to_fp8(grad, fp8_format=fp8_format)
    uint8_buffer = torch.empty_like(grad_fp8).view(torch.uint8)
    dist.all_to_all_single(uint8_buffer, grad_fp8.view(torch.uint8), group=group)

    scale_list = [torch.ones(1, dtype=scale.dtype, device=input_device) for _ in range(world_size)]
    dist.all_gather(scale_list, scale, group=group)

    buffer_list = list(torch.chunk(uint8_buffer.view(fp8_type), world_size, dim=0))
    sharded_gradient.zero_()
    for tensor, scale in zip(buffer_list, scale_list):
        sharded_gradient += cast_from_fp8(tensor, scale, input_type)


def fp8_compress_fsdp_params_comm_hook(state: object, padded_unsharded_flat_param: torch.Tensor,
                                       sharded_flat_param: torch.Tensor, group=None, fp8_format="e5m2") -> None:
    """
        This hook is pending the official support for parameters communication hook in FSDP, e.g. register_params_comm_hook.

    Example::
        >>> fsdp_model.register_params_comm_hook(None, fp8_compress_fsdp_params_comm_hook)
    """

    fp8_type = torch.float8_e4m3fn if fp8_format == "e4m3" else torch.float8_e5m2
    fp8_max = torch.finfo(fp8_type).max
    inp = sharded_flat_param
    out = padded_unsharded_flat_param

    per_tensor_max = inp.abs().max().float()
    per_tensor_max = torch.where(per_tensor_max > 0, per_tensor_max, 1.0)
    dist.all_reduce(per_tensor_max, op=torch.distributed.ReduceOp.MAX, group=group)

    scale = fp8_max / per_tensor_max
    fp8_sharded_flat_param = (scale * inp.float()).to(fp8_type).view(torch.uint8)

    fp8_out = torch.empty(out.shape, dtype=torch.uint8, device=out.device)
    dist.all_gather_into_tensor(
        fp8_out,
        fp8_sharded_flat_param,
        group=group,
    )
    padded_unsharded_flat_param.copy_((fp8_out.view(fp8_type).float() / scale).to(out.dtype))