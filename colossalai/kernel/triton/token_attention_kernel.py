# Adapted from ModelTC https://github.com/ModelTC/lightllm


import torch

try:
    import triton
    import triton.language as tl

    HAS_TRITON = True
except ImportError:
    HAS_TRITON = False
    print("please install triton from https://github.com/openai/triton")

try:
    from lightllm.models.bloom.triton_kernel.token_attention_nopad_att1 import (
        _fwd_kernel_token_att1 as _token_attn_1_alibi_kernel,
    )
    from lightllm.models.llama2.triton_kernel.token_attention_nopad_att1 import (
        token_att_fwd as lightllm_llama2_token_att_fwd,
    )
    from lightllm.models.llama2.triton_kernel.token_attention_nopad_reduceV import (
        token_att_fwd2 as lightllm_llama2_token_att_fwd2,
    )
    from lightllm.models.llama2.triton_kernel.token_attention_nopad_softmax import (
        token_softmax_fwd as lightllm_llama2_token_softmax_fwd,
    )
    from lightllm.models.llama.triron_kernel.token_attention_nopad_att1 import (
        _fwd_kernel_token_att1 as _token_attn_1_kernel,
    )

    HAS_TRITON_TOKEN_ATTENTION = True
except ImportError:
    print("unable to import lightllm kernels")
    HAS_TRITON_TOKEN_ATTENTION = False

if HAS_TRITON:

    @torch.no_grad()
    def token_attn_fwd_1(
        q, k, attn_out, kv_cache_loc, kv_cache_start_loc, kv_cache_seqlen, max_kv_cache_len, alibi=None
    ):
        BLOCK = 32
        # shape constraints
        q_head_dim, k_head_dim = q.shape[-1], k.shape[-1]
        assert q_head_dim == k_head_dim
        assert k_head_dim in {16, 32, 64, 128}
        sm_scale = 1.0 / (k_head_dim**0.5)

        batch, head_num = kv_cache_loc.shape[0], q.shape[1]

        grid = (batch, head_num, triton.cdiv(max_kv_cache_len, BLOCK))

        num_warps = 4 if k_head_dim <= 64 else 8
        num_warps = 2

        if alibi is not None:
            _token_attn_1_alibi_kernel[grid](
                q,
                k,
                sm_scale,
                alibi,
                kv_cache_loc,
                kv_cache_start_loc,
                kv_cache_seqlen,
                max_kv_cache_len,
                attn_out,
                kv_cache_loc.stride(0),
                kv_cache_loc.stride(1),
                q.stride(0),
                q.stride(1),
                q.stride(2),
                k.stride(0),
                k.stride(1),
                k.stride(2),
                attn_out.stride(0),
                attn_out.stride(1),
                HEAD_DIM=k_head_dim,
                BLOCK_N=BLOCK,
                num_warps=num_warps,
                num_stages=1,
            )
        else:
            num_warps = 4  # modified from lightllm: lightllm/lightllm/models/llama/triton_kernel/token_attention_nopad_att1.py/#L64
            _token_attn_1_kernel[grid](
                q,
                k,
                sm_scale,
                kv_cache_loc,
                kv_cache_start_loc,
                kv_cache_seqlen,
                max_kv_cache_len,
                attn_out,
                kv_cache_loc.stride(0),
                kv_cache_loc.stride(1),
                q.stride(0),
                q.stride(1),
                q.stride(2),
                k.stride(0),
                k.stride(1),
                k.stride(2),
                attn_out.stride(0),
                attn_out.stride(1),
                HEAD_DIM=k_head_dim,
                BLOCK_N=BLOCK,
                num_warps=num_warps,
                num_stages=1,
            )
        return

    # this function is modified from https://github.com/ModelTC/lightllm/blob/5c559dd7981ed67679a08a1e09a88fb4c1550b3a/lightllm/models/llama/triton_kernel/token_attention_nopad_softmax.py#L8
    @triton.jit
    def _token_attn_softmax_fwd(
        softmax_logics,
        kv_cache_start_loc,
        kv_cache_seqlen,
        softmax_prob_out,
        logics_head_dim_stride,
        logics_batch_stride,
        prob_head_dim_stride,
        prob_batch_stride,
        BLOCK_SIZE: tl.constexpr,
    ):
        current_batch = tl.program_id(0)
        current_head = tl.program_id(1)

        col_offsets = tl.arange(0, BLOCK_SIZE)
        current_batch_seq_len = tl.load(kv_cache_seqlen + current_batch)
        current_batch_in_all_start_index = tl.load(kv_cache_start_loc + current_batch)

        row = tl.load(
            softmax_logics
            + current_head * logics_head_dim_stride
            + (current_batch_in_all_start_index + col_offsets) * logics_batch_stride,
            mask=col_offsets < current_batch_seq_len,
            other=-float("inf"),
        ).to(tl.float32)

        row_minus_max = row - tl.max(row, axis=0)
        numerator = tl.exp(row_minus_max)
        denominator = tl.sum(numerator, axis=0)
        softmax_output = numerator / denominator

        tl.store(
            softmax_prob_out
            + current_head * prob_head_dim_stride
            + (current_batch_in_all_start_index + col_offsets) * prob_batch_stride,
            softmax_output,
            mask=col_offsets < current_batch_seq_len,
        )
        return

    # this function is modified from https://github.com/ModelTC/lightllm/blob/5c559dd7981ed67679a08a1e09a88fb4c1550b3a/lightllm/models/llama/triton_kernel/token_attention_nopad_softmax.py#L36
    @torch.no_grad()
    def token_attn_softmax_fwd(softmax_logics, kv_cache_start_loc, kv_cache_seqlen, softmax_prob_out, max_kv_cache_len):
        BLOCK_SIZE = triton.next_power_of_2(max_kv_cache_len)
        batch, head_num = kv_cache_start_loc.shape[0], softmax_logics.shape[0]

        num_warps = 4
        if BLOCK_SIZE >= 2048:
            num_warps = 8
        if BLOCK_SIZE >= 4096:
            num_warps = 16

        _token_attn_softmax_fwd[(batch, head_num)](
            softmax_logics,
            kv_cache_start_loc,
            kv_cache_seqlen,
            softmax_prob_out,
            softmax_logics.stride(0),
            softmax_logics.stride(1),
            softmax_prob_out.stride(0),
            softmax_prob_out.stride(1),
            num_warps=num_warps,
            BLOCK_SIZE=BLOCK_SIZE,
        )
        return

    # this function is modified from https://github.com/ModelTC/lightllm/blob/5c559dd7981ed67679a08a1e09a88fb4c1550b3a/lightllm/models/llama/triton_kernel/token_attention_nopad_reduceV.py#L8
    @triton.jit
    def _token_attn_2_kernel(
        Prob,
        V,
        attn_out,
        kv_cache_loc,
        kv_cache_start_loc,
        kv_cache_seqlen,
        max_kv_cache_len,
        kv_cache_loc_b_stride,
        kv_cache_loc_s_stride,
        prob_head_dim_stride,
        prob_batch_stride,
        v_batch_stride,
        v_head_stride,
        v_head_dim_stride,
        attn_out_batch_stride,
        attn_out_head_stride,
        attn_out_head_dim_stride,
        HEAD_DIM: tl.constexpr,
        BLOCK_N: tl.constexpr,
    ):
        current_batch = tl.program_id(0)
        current_head = tl.program_id(1)

        offs_n = tl.arange(0, BLOCK_N)
        offs_d = tl.arange(0, HEAD_DIM)
        current_batch_seq_len = tl.load(kv_cache_seqlen + current_batch)
        current_batch_start_index = max_kv_cache_len - current_batch_seq_len
        current_batch_in_all_start_index = tl.load(kv_cache_start_loc + current_batch)

        v_loc_off = current_batch * kv_cache_loc_b_stride + (current_batch_start_index + offs_n) * kv_cache_loc_s_stride
        p_offs = current_head * prob_head_dim_stride + (current_batch_in_all_start_index + offs_n) * prob_batch_stride
        v_offs = current_head * v_head_stride + offs_d[None, :] * v_head_dim_stride

        acc = tl.zeros([HEAD_DIM], dtype=tl.float32)
        for start_n in range(0, current_batch_seq_len, BLOCK_N):
            start_n = tl.multiple_of(start_n, BLOCK_N)
            p_value = tl.load(
                Prob + p_offs + start_n * kv_cache_loc_s_stride,
                mask=(start_n + offs_n) < current_batch_seq_len,
                other=0.0,
            )
            v_loc = tl.load(
                kv_cache_loc + v_loc_off + start_n * kv_cache_loc_s_stride,
                mask=(start_n + offs_n) < current_batch_seq_len,
                other=0.0,
            )
            v_value = tl.load(
                V + v_offs + v_loc[:, None] * v_batch_stride,
                mask=(start_n + offs_n[:, None]) < current_batch_seq_len,
                other=0.0,
            )
            acc += tl.sum(p_value[:, None] * v_value, 0)

        acc = acc.to(tl.float16)
        off_o = (
            current_batch * attn_out_batch_stride
            + current_head * attn_out_head_stride
            + offs_d * attn_out_head_dim_stride
        )
        out_ptrs = attn_out + off_o
        tl.store(out_ptrs, acc)
        return

    # this function is modifed from https://github.com/ModelTC/lightllm/blob/5c559dd7981ed67679a08a1e09a88fb4c1550b3a/lightllm/models/llama/triton_kernel/token_attention_nopad_reduceV.py#L47
    @torch.no_grad()
    def token_attn_fwd_2(prob, v, attn_out, kv_cache_loc, kv_cache_start_loc, kv_cache_seqlen, max_kv_cache_len):
        if triton.__version__ >= "2.1.0":
            BLOCK = 128
        else:
            BLOCK = 64
        batch, head = kv_cache_loc.shape[0], v.shape[1]
        grid = (batch, head)
        num_warps = 4
        dim = v.shape[-1]

        _token_attn_2_kernel[grid](
            prob,
            v,
            attn_out,
            kv_cache_loc,
            kv_cache_start_loc,
            kv_cache_seqlen,
            max_kv_cache_len,
            kv_cache_loc.stride(0),
            kv_cache_loc.stride(1),
            prob.stride(0),
            prob.stride(1),
            v.stride(0),
            v.stride(1),
            v.stride(2),
            attn_out.stride(0),
            attn_out.stride(1),
            attn_out.stride(2),
            HEAD_DIM=dim,
            BLOCK_N=BLOCK,
            num_warps=num_warps,
            num_stages=1,
        )
        return

    @torch.no_grad()
    def token_attention_fwd(
        q, k, v, attn_out, kv_cache_loc, kv_cache_start_loc, kv_cache_seq_len, max_len_in_batch, alibi=None
    ):
        head_num = k.shape[1]
        batch_size = kv_cache_seq_len.shape[0]
        calcu_shape1 = (batch_size, head_num, k.shape[2])
        total_token_num = k.shape[0]

        att_m_tensor = torch.empty((head_num, total_token_num), dtype=q.dtype, device="cuda")

        token_attn_fwd_1(
            q.view(calcu_shape1),
            k,
            att_m_tensor,
            kv_cache_loc,
            kv_cache_start_loc,
            kv_cache_seq_len,
            max_len_in_batch,
            alibi=alibi,
        )

        prob = torch.empty_like(att_m_tensor)

        token_attn_softmax_fwd(att_m_tensor, kv_cache_start_loc, kv_cache_seq_len, prob, max_len_in_batch)
        att_m_tensor = None
        token_attn_fwd_2(
            prob, v, attn_out.view(calcu_shape1), kv_cache_loc, kv_cache_start_loc, kv_cache_seq_len, max_len_in_batch
        )

        prob = None

        return


class Llama2TokenAttentionForwards:
    @staticmethod
    @triton.jit

    # this function is adapted from
    def _fwd_kernel(
        Logics,
        V,
        Out,
        B_Loc,
        B_Start_Loc,
        B_Seqlen,
        max_input_len,
        stride_logic_h,
        stride_logic_bs,
        stride_vbs,
        stride_vh,
        stride_vd,
        stride_obs,
        stride_oh,
        stride_od,
        stride_b_loc_b,
        stride_b_loc_s,
        other_kv_index,  # avoid nan information
        kv_group_num,
        BLOCK_DMODEL: tl.constexpr,
        BLOCK_N: tl.constexpr,
    ):
        cur_batch = tl.program_id(0)
        cur_head = tl.program_id(1)

        cur_kv_head = cur_head // kv_group_num

        cur_batch_seq_len = tl.load(B_Seqlen + cur_batch)
        cur_batch_start_loc = tl.load(B_Start_Loc + cur_batch)

        offs_n = tl.arange(0, BLOCK_N)
        offs_d = tl.arange(0, BLOCK_DMODEL)

        off_v = cur_kv_head * stride_vh + offs_d[None, :] * stride_vd
        off_b_loc = cur_batch * stride_b_loc_b + (max_input_len - cur_batch_seq_len) * stride_b_loc_s

        v_ptrs = V + off_v

        e_max = float("-inf")
        e_sum = 0.0
        acc = tl.zeros([BLOCK_DMODEL], dtype=tl.float32)

        for start_n in range(0, cur_batch_seq_len, BLOCK_N):
            start_n = tl.multiple_of(start_n, BLOCK_N)
            v_index = tl.load(
                B_Loc + off_b_loc + (start_n + offs_n) * stride_b_loc_s,
                mask=(start_n + offs_n) < cur_batch_seq_len,
                other=other_kv_index,
            )

            qk = tl.load(
                Logics + cur_head * stride_logic_h + (cur_batch_start_loc + start_n + offs_n) * stride_logic_bs,
                mask=start_n + offs_n < cur_batch_seq_len,
                other=float("-inf"),
            )

            n_e_max = tl.maximum(tl.max(qk, 0), e_max)
            old_scale = tl.exp(e_max - n_e_max)
            p = tl.exp(qk - n_e_max)
            e_sum = e_sum * old_scale + tl.sum(p, 0)
            v = tl.load(v_ptrs + v_index[:, None] * stride_vbs)
            acc = acc * old_scale + tl.sum(p[:, None] * v, 0)
            e_max = n_e_max

        acc = acc / e_sum
        off_o = cur_batch * stride_obs + cur_head * stride_oh + offs_d * stride_od
        out_ptrs = Out + off_o
        tl.store(out_ptrs, acc)
        return

    @staticmethod
    @torch.no_grad()
    def token_softmax_reducev_fwd(logics, v, o, b_loc, b_start_loc, b_seq_len, max_input_len, other_kv_index):
        BLOCK = 64
        batch, head = b_seq_len.shape[0], logics.shape[0]
        grid = (batch, head)
        kv_group_num = logics.shape[0] // v.shape[1]

        num_warps = 1
        Llama2TokenAttentionForwards._fwd_kernel[grid](
            logics,
            v,
            o,
            b_loc,
            b_start_loc,
            b_seq_len,
            max_input_len,
            logics.stride(0),
            logics.stride(1),
            v.stride(0),
            v.stride(1),
            v.stride(2),
            o.stride(0),
            o.stride(1),
            o.stride(2),
            b_loc.stride(0),
            b_loc.stride(1),
            other_kv_index,
            kv_group_num,
            BLOCK_DMODEL=v.shape[-1],
            BLOCK_N=BLOCK,
            num_warps=num_warps,
            num_stages=3,
        )
        return

    # this is the interface of llama2 attn forward
    @staticmethod
    @torch.no_grad()
    def token_attn(
        q, k, v, attn_out, kv_cache_loc, kv_cache_start_loc, kv_cache_seq_len, max_len_in_batch, other_kv_index
    ):
        total_token_num = k.shape[0]
        batch_size, head_num, head_dim = q.shape
        calcu_shape1 = (batch_size, head_num, head_dim)
        att_m_tensor = torch.empty((head_num, total_token_num), dtype=q.dtype, device="cuda")

        lightllm_llama2_token_att_fwd(
            q,
            k,
            att_m_tensor,
            kv_cache_loc,
            kv_cache_start_loc,
            kv_cache_seq_len,
            max_len_in_batch,
        )

        if triton.__version__ == "2.0.0":
            prob = torch.empty_like(att_m_tensor)
            lightllm_llama2_token_softmax_fwd(
                att_m_tensor, kv_cache_start_loc, kv_cache_seq_len, prob, max_len_in_batch
            )
            att_m_tensor = None

            lightllm_llama2_token_att_fwd2(
                prob,
                v,
                attn_out.view(calcu_shape1),
                kv_cache_loc,
                kv_cache_start_loc,
                kv_cache_seq_len,
                max_len_in_batch,
            )

            prob = None
            return

        elif triton.__version__ >= "2.1.0":
            Llama2TokenAttentionForwards.token_softmax_reducev_fwd(
                att_m_tensor,
                v,
                attn_out.view(calcu_shape1),
                kv_cache_loc,
                kv_cache_start_loc,
                kv_cache_seq_len,
                max_len_in_batch,
                other_kv_index,
            )
        else:
            raise Exception("not support triton version")
