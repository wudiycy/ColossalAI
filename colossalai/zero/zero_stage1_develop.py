from colossalai.utils.cuda import get_current_device
import torch
import torch.distributed as dist
from colossalai.logging import get_dist_logger
from torch.optim import Optimizer
from .bookkeeping import ParameterStore, GradientStore, BucketStore
from colossalai.context import ParallelMode
from colossalai.core import global_context as gpc
from colossalai.amp.naive_amp._fp16_optimizer import DynamicGradScaler
from colossalai.nn.optimizer import ColossalaiOptimizer
from ._utils import (move_tensor, flatten, get_grad_accumulate_object,
                     split_half_float_double, reduce_tensor, release_param_grad, calculate_global_norm_from_list,
                     compute_norm, sync_param, has_inf_or_nan)
from colossalai.communication import TensorBucket


class ShardedOptimizer(ColossalaiOptimizer):

    def __init__(
            self,
            optimizer: Optimizer,

            # grad scaler config
            initial_scale=2**32,
            min_scale=1,
            growth_factor=2,
            backoff_factor=0.5,
            growth_interval=1000,
            hysteresis=2,
            max_scale: int = 2**32,

            # grad clipping
            clip_grad_norm=2.0,
            verbose=False,

            # communication
            reduce_bucket_size=500000000,
            communication_dtype=torch.float16,
            overlap_communication=False,

            # stage 2
            dp_parallel_mode=ParallelMode.DATA,
            mp_parallel_mode=ParallelMode.MODEL):

        # TODO: add support for
        # 1. fp16 master weights
        # 2. contiguous gradients
        # 3. cpu offload
        # 4. support when some parameters requires_grad = False

        self._optimizer = optimizer
        self._dtype = self._optimizer.param_groups[0]['params'][0].dtype
        self._logger = get_dist_logger()
        self._verbose = verbose

        # get process groups
        self._dp_parallel_mode = dp_parallel_mode
        self._mp_parallel_mode = mp_parallel_mode
        self._local_rank = gpc.get_local_rank(dp_parallel_mode)
        self._world_size = gpc.get_world_size(dp_parallel_mode)

        self._dp_group = gpc.get_group(dp_parallel_mode)
        if gpc.is_initialized(mp_parallel_mode) and gpc.get_world_size(mp_parallel_mode) > 1:
            self._mp_group = gpc.get_group(mp_parallel_mode)
        else:
            self._mp_group = None

        # fp16 and fp32 params for mixed precision training
        self._fp16_param_groups = dict()
        self._fp32_flat_param_groups_of_current_rank = dict()

        # communication params
        self._overlap_communication = overlap_communication
        self._reduce_bucket_size = reduce_bucket_size
        self._communication_dtype = communication_dtype

        # gradient scaler
        self.grad_scaler = DynamicGradScaler(initial_scale=initial_scale,
                                             min_scale=min_scale,
                                             growth_factor=growth_factor,
                                             backoff_factor=backoff_factor,
                                             growth_interval=growth_interval,
                                             hysteresis=hysteresis,
                                             max_scale=max_scale,
                                             verbose=verbose)
        self._found_overflow = torch.FloatTensor([0]).to(get_current_device())

        # gradient clipping
        self._clip_grad_norm = clip_grad_norm

        # check argument conflict
        self._sanity_checks()

        # ParameterStore will manage the tensor buffers used for zero
        # it will not manage the tensors used by mixed precision training
        self._param_store = ParameterStore()
        self._grad_store = GradientStore()
        self._bucket_store = BucketStore()

        # iterate over the param group in the optimizer
        # partition these param groups for data parallel training
        # and add buffers to parameter store for future access
        for group_id, param_group in enumerate(self._optimizer.param_groups):
            params = param_group['params']

            # add the fp16 params to fp16_param_groups for bookkeeping
            self._fp16_param_groups[group_id] = params

            # assign parameters to ranks
            # the params in the list are sorted 
            params_per_rank = self._partition_param_list(params)

            # store the mapping between param to rank
            # each param should belong to only one rank
            for rank, params in enumerate(params_per_rank):
                self._param_store.add_fp16_param_list_by_rank_group(rank, group_id, params)
                for param in params:
                    self._param_store.set_param_to_rank(param, rank)

            # move to cpu to make room to create the flat tensor
            move_tensor(params, device='cpu')

            # flatten the reordered tensors
            for rank in range(self._world_size):
                tensor_list = self._param_store.get_fp16_params_by_rank_group(rank, group_id)
                flat_tensor = flatten(tensor_list)
                flat_tensor = flat_tensor.cuda()
                self._param_store.add_flat_fp16_param_by_rank_group(rank, group_id, flat_tensor)

            # sync parameters
            for rank in range(self._world_size):
                flat_tensor = self._param_store.get_flat_fp16_param_by_rank_group(rank, group_id)
                tensor_list = self._param_store.get_fp16_params_by_rank_group(rank, group_id)
                sync_param(flat_tensor=flat_tensor, tensor_list=tensor_list)

            # create a copy of fp32 weights of the parameters for which this rank is responsible
            fp16_flat_current_rank = self._param_store.get_flat_fp16_param_by_rank_group(self._local_rank, group_id)
            fp32_flat_current_rank = fp16_flat_current_rank.clone().float().detach()
            fp32_flat_current_rank = fp32_flat_current_rank.to(get_current_device())
            fp32_flat_current_rank.requires_grad = True
            self._fp32_flat_param_groups_of_current_rank[group_id] = fp32_flat_current_rank

            # need to replace the params in the `params` field in the optimizer
            # so that when the optimizer calls step(), it only updates the tensors
            # managed by this data parallel rank
            param_group['params'] = [fp32_flat_current_rank]

            # set reduction state
            for param in self._fp16_param_groups[group_id]:
                self._param_store.set_param_reduction_state(param, False)

        # intialize communication stream for
        # communication-compuation overlapping
        if self._overlap_communication:
            self._comm_stream = torch.cuda.Stream()

        # reduction hook is only used if overlapping communication
        # or stage 2 is used
        # if it is stage 1 without overlapping, no hook will be attached
        if self._overlap_communication:
            self._attach_reduce_grad_hook()

        self._initialize_optimizer_states()

    @property
    def loss_scale(self):
        return self.grad_scaler.scale

    def _partition_param_list(self, param_list):
        params_per_rank = [[] for _ in range(self._world_size)]
        numel_per_rank = [0 for _ in range(self._world_size)]

        # partititon the parameters in a greedy fashion
        sorted_params = sorted(param_list, key=lambda x: x.numel(), reverse=True)
        for param in sorted_params:
            # allocate this parameter to the rank with
            # the smallest numel for load balancing purpose
            rank_to_go = numel_per_rank.index(min(numel_per_rank))
            params_per_rank[rank_to_go].append(param)
            numel_per_rank[rank_to_go] += param.numel()

        if self._verbose:
            self._logger.info(f'Number of elements on ranks: {numel_per_rank}',
                              ranks=[0],
                              parallel_mode=self._dp_parallel_mode)
        return params_per_rank

    def _initialize_optimizer_states(self):
        for group_id in range(len(self._fp32_flat_param_groups_of_current_rank)):
            fp32_partition_param = self._fp32_flat_param_groups_of_current_rank[group_id]
            fp32_partition_grad = torch.zeros_like(fp32_partition_param)
            fp32_partition_param.grad = fp32_partition_grad

        self._optimizer.step()

        for group_id, fp32_flat_tensor in self._fp32_flat_param_groups_of_current_rank.items():
            fp32_flat_tensor.grad = None

    def _sanity_checks(self):
        assert torch.cuda.is_available(), 'CUDA is required'
        assert self._dtype == torch.float16, \
            f'Parameters are expected to be of type torch.float16, but got {self._dtype}'

    ###########################################################
    # Backward Reduction Hook
    ###########################################################

    def _attach_reduce_grad_hook(self):
        # we iterate over the fp16 params
        # on each param, we register a hook to its AccumulateGrad object
        for group_id, param_group in enumerate(self._fp16_param_groups):
            for param in param_group:
                if param.requires_grad:
                    # get the AccumulateGrad object of the param itself
                    accum_grad_obj = get_grad_accumulate_object(param)
                    self._grad_store.add_accumulate_grad_object(accum_grad_obj)

                    # define hook
                    # NOT IMPORTANT BUT GOOD TO KNOW:
                    # args here is not grad, but allow_unreacable and accumulate_grad
                    def reduce_grad_hook(*args):
                        self._reduce_and_remove_grads(param, group_id)

                    accum_grad_obj.register_hook(reduce_grad_hook)

    def _reduce_and_remove_grads(self, param, group_id):
        # the condititon in deepspeed is self_partition_grads or is_gradient_accumulation_boundary
        # we ignore gradient accumulation first, so it is always true
        self._reduce_and_remove_grads_by_bucket(param, group_id)

    def _reduce_and_remove_grads_by_bucket(self, param, group_id):
        param_size = param.numel()

        # check if the bucket is full
        # if full, will reduce the grads already in the bucket
        # after reduction, the bucket will be empty
        if self._bucket_store.num_elements_in_bucket + param_size > self._reduce_bucket_size:
            self._reduce_grads_in_bucket()

        # the param must not be reduced to ensure correctness
        is_param_reduced = self._param_store.is_param_reduced(param)
        if is_param_reduced:
            msg = f'Parameter of size ({param.size()}) has already been reduced, ' \
                + 'duplicate reduction will lead to arithmetic incorrectness'
            raise RuntimeError(msg)

        # TODO: handle oversized param

        # the param must have grad for reduction
        assert param.grad is not None, f'Parameter of size ({param.size()}) has None grad, cannot be reduced'

        self._bucket_store.num_elements_in_bucket += param_size
        self._bucket_store.add_grad(param.grad)
        self._bucket_store.add_param(param, group_id)

    def _reduce_grads_in_bucket(self):
        # reduce grads
        self._buffered_reduce_fallback(dst_rank=None,
                                       grads=self._bucket_store.get_grad(),
                                       bucket_size=self._bucket_store.num_elements_in_bucket)

        # use communication stream if overlapping
        # communication with computation
        if self._overlap_communication:
            stream = self.comm_stream
        else:
            stream = torch.cuda.current_stream()

        with torch.cuda.stream(stream):
            params_in_bucket = self._bucket_store.get_param()

            for group_id in range(len(params_in_bucket)):
                params = params_in_bucket[group_id]

                for param in params:
                    # the is_param_reduced flag should be False showing that
                    # this param is not reduced before calling self._buffered_reduce_fallback
                    is_param_reduced = self._param_store.is_param_reduced(param)
                    if is_param_reduced:
                        msg = f'Parameter of size ({param.size()}) has been reduced, ' + \
                            'duplicate reduction will lead to arithmetic incorrectness'
                        raise RuntimeError(msg)

                    # update the flag
                    self._param_store.set_param_reduction_state(param, True)

        self._bucket_store.reset()

    def _buffered_reduce_fallback(self, dst_rank, grads, bucket_size):
        grad_buckets_by_dtype = split_half_float_double(grads)

        for tensor_list in grad_buckets_by_dtype:
            self._reduce_no_retain(tensor_list=tensor_list, bucket_size=bucket_size, dst_rank=dst_rank)

    ##########################
    # Handle Utility Function
    ##########################
    def _reduce_no_retain(self, tensor_list, bucket_size, dst_rank):
        param_bucket = TensorBucket(size=bucket_size)

        for tensor in tensor_list:
            param_bucket.add_to_bucket(tensor, allow_oversize=True)

            if param_bucket.is_max_size_exceeded():
                self._reduce_and_copy(bucket=param_bucket, dst_rank=dst_rank)
                param_bucket.empty()

        if not param_bucket.is_empty():
            self._reduce_and_copy(bucket=param_bucket, dst_rank=dst_rank)

    def _reduce_and_copy(self, bucket: TensorBucket, dst_rank):
        if self._overlap_communication:
            torch.cuda.synchronize()
            self._param_store.clear_grads_of_previous_reduced_params()
            stream = self._comm_stream
        else:
            stream = torch.cuda.current_stream()

        with torch.cuda.stream(stream):
            flat = bucket.flatten()
            reduced_flat = reduce_tensor(tensor=flat,
                                         dtype=self._communication_dtype,
                                         dst_rank=dst_rank,
                                         parallel_mode=self._dp_parallel_mode)

            # update the reduced tensor
            if dst_rank is None or dst_rank == gpc.get_local_rank(self._dp_parallel_mode):
                bucket.unflatten_and_copy(reduced_flat)

    ################################
    # torch.optim.Optimizer methods
    ################################

    def backward(self, loss):
        loss = loss.float()
        loss = self.loss_scale * loss
        loss.backward(loss, retain_graph=True)

    def zero_grad(self, set_to_none=True):
        """
        Set parameter gradients to zero. If set_to_none = True, gradient
        will be set to None to save memory.

        :param set_to_none: Whether set the gradient to None. Default value is True.
        :type set_to_none: bool
        """
        for group_id, param_group in self._fp16_param_groups.items():
            for param in param_group:
                if set_to_none:
                    param.grad = None
                else:
                    if param.grad is not None:
                        param.grad.detach()
                        param.grad.zero_()

    def step(self, closure=None):
        assert closure is None, 'closure is not supported by step()'
        self._step_stage_1()

    def sync_grad(self):
        self._sync_grad_stage1()

    ####################
    # Update Parameter #
    ####################

    def _step_stage_1(self, closure=None):
        # check for overflow
        found_inf = self._check_overflow()

        # update loss scale if overflow occurs
        if found_inf:
            self.grad_scaler.update(found_inf)
            self._grad_store._averaged_gradients = dict()
            self.zero_grad()
            return

        # copy the grad of fp16 param to fp32 param
        single_grad_partition_groups = []
        norm_groups = []

        for group_id in range(len(self._fp16_param_groups)):
            param_group = self._fp16_param_groups[group_id]

            # compute norm
            norm_group = compute_norm(gradients=self._grad_store._averaged_gradients[group_id],
                                      params=self._param_store.get_fp16_params_by_rank_group(group_id=group_id,
                                                                                             rank=self._local_rank),
                                      dp_group=self._dp_group,
                                      mp_group=self._mp_group)
            norm_groups.append(norm_group)

            # release grads of unneeded params
            # TODO: this is not necessary as param.grad has been set to None
            # during sync
            params_not_in_current_rank = []
            for param in param_group:
                if not self._param_store.belongs_to_current_rank(param):
                    params_not_in_current_rank.append(param)
            release_param_grad(params_not_in_current_rank)

            # create flat gradient for the flat fp32 params
            fp16_avg_grads = self._grad_store.get_averaged_gradients_by_group(group_id)
            dtype = self._fp32_flat_param_groups_of_current_rank[group_id].dtype
            grad_list = [grad.clone().to(dtype).detach() for grad in fp16_avg_grads]
            fp32_avg_grads = flatten(grad_list)

            param_shape = self._fp32_flat_param_groups_of_current_rank[group_id].shape
            assert param_shape == fp32_avg_grads.shape, \
                f'fp32 param and grad have different shape {param_shape} vs {fp32_avg_grads.shape}'

            self._fp32_flat_param_groups_of_current_rank[group_id].grad = fp32_avg_grads

            # release grads of unneeded params
            # this is required to clear the fp16 grads
            # TODO: this is no longer needed as it is cleared in sync_grad
            params_in_current_rank = []
            for param in param_group:
                if self._param_store.belongs_to_current_rank(param):
                    params_not_in_current_rank.append(param)
            release_param_grad(params_in_current_rank)
            self._grad_store._averaged_gradients[group_id] = []

            single_grad_partition_groups.append(fp32_avg_grads)

        # unscale and clip grads
        global_norm = calculate_global_norm_from_list(norm_list=norm_groups)
        self._unscale_and_clip_grads(single_grad_partition_groups, global_norm)

        # update the parameters
        self._optimizer.step()

        # release the fp32 grad
        release_param_grad(self._fp32_flat_param_groups_of_current_rank.values())

        # update fp16 partition updated by the current rank
        for group_id in range(len(self._fp16_param_groups)):
            fp16_param = self._param_store.get_flat_fp16_param_by_rank_group(rank=self._local_rank, group_id=group_id)
            fp32_param = self._fp32_flat_param_groups_of_current_rank[group_id]
            fp16_param.data.copy_(fp32_param)

        # broadcast the updated model weights
        handles = []
        for group_id in range(len(self._fp16_param_groups)):
            for rank in range(self._world_size):
                fp16_param = self._param_store.get_flat_fp16_param_by_rank_group(rank=rank, group_id=group_id)
                handle = dist.broadcast(fp16_param, src=rank, group=self._dp_group, async_op=True)
                handles.append(handle)

        for handle in handles:
            handle.wait()

    ##################
    # FP16 Utilities #
    ##################

    def _check_overflow(self):
        # clear previous overflow record
        self._found_overflow.fill_(0.0)

        # check for overflow
        for group_id in range(len(self._fp16_param_groups)):
            for avg_grad in self._grad_store.get_averaged_gradients_by_group(group_id):
                if avg_grad is not None and has_inf_or_nan(avg_grad):
                    self._found_overflow.fill_(1.0)
                    break

        # all-reduce across dp group
        dist.all_reduce(self._found_overflow, op=dist.ReduceOp.MAX, group=self._dp_group)

        # all-reduce over model parallel group
        if self._mp_group:
            dist.all_reduce(self._found_overflow, op=dist.ReduceOp.MAX, group=self._mp_group)

        if self._found_overflow.item() > 0:
            return True
        else:
            return False

    def _unscale_and_clip_grads(self, grad_groups_flat, total_norm):
        # compute combined scale factor for this group
        combined_scale = self.loss_scale
        if self._clip_grad_norm > 0.:
            # norm is in fact norm*scale
            clip = ((total_norm / self.loss_scale) + 1e-6) / self._clip_grad_norm
            if clip > 1:
                combined_scale = clip * self.loss_scale

        for grad in grad_groups_flat:
            if isinstance(grad, list):
                sub_partitions = grad
                for g in sub_partitions:
                    g.data.mul_(1. / combined_scale)
            else:
                grad.data.mul_(1. / combined_scale)

    ########################################
    # Gradient Synchronization for Stage 1 #
    ########################################

    def _sync_grad_stage1(self):
        if not self._overlap_communication:
            for group_id in range(len(self._fp16_param_groups)):
                param_group = self._fp16_param_groups[group_id]
                for param in param_group:
                    if param.grad is not None:
                        self._reduce_and_remove_grads_by_bucket(param, group_id)

        self._reduce_grads_in_bucket()

        # update param already reduced flag
        reduction_states = self._param_store.get_param_reduction_states()
        for tensor, state in reduction_states.items():
            reduction_states[tensor] = False

        # clear reduced grads
        if self._overlap_communication:
            torch.cuda.synchronize()
            self._param_store.clear_grads_of_previous_reduced_params()

        # accumulate gradient
        avg_gradients = self._grad_store._averaged_gradients
        for group_id in range(len(self._fp16_param_groups)):
            param_group = self._param_store.get_fp16_params_by_rank_group(self._local_rank, group_id)

            if group_id not in avg_gradients:
                avg_gradients[group_id] = []

            param_idx = 0
            for param in param_group:
                if param.grad is not None:
                    if len(avg_gradients[group_id]) == param_idx:
                        avg_gradients[group_id].append(param.grad)
                    else:
                        avg_gradients[group_id].add_(param.grad)
                    param_idx += 1

        # the gradients needed are stored in the avg_gradients buffer
        # thus, can clear this
        self.zero_grad()