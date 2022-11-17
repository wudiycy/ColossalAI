from colossalai.gemini.memory_tracer import SyncCudaMemoryMonitor
from colossalai.utils.memory import colo_device_memory_used, colo_device_memory_capacity
from colossalai.utils import get_current_device
from colossalai.gemini.stateful_tensor import StatefulTensor
from colossalai.gemini.chunk import ChunkManager

import torch
import torch.nn as nn
import time
from typing import List, Optional

from colossalai.fx.tracer.tracer import ColoTracer
from colossalai.fx.passes.meta_info_prop import MetaInfoProp
from colossalai.fx.profiler import (calculate_fwd_out, calculate_fwd_tmp, is_compatible_with_meta, parameter_size)
from torch.fx import symbolic_trace

if is_compatible_with_meta():
    from colossalai.fx.profiler import MetaTensor


class MemStatsCollector:
    """
    A Memory statistic collector.
    It works in two phases. 
    Phase 1. Collection Phase: collect memory usage statistics of CPU and GPU.
    The first iteration of DNN training.
    Phase 2. Runtime Phase: use the read-only collected stats
    The rest iterations of DNN training.

    It has a Sampling counter which is reset after DNN training iteration.
    """

    def __init__(self) -> None:
        self._mem_monitor = SyncCudaMemoryMonitor()
        self._model_data_cuda_list = []
        self._overall_cuda_list = []

        self._model_data_cpu_list = []
        self._overall_cpu_list = []

        self._non_model_data_cuda_list = []
        self._non_model_data_cpu_list = []
        self._sampling_time = []

        self._start_flag = False
        self._step_idx = 0
        self._step_total = 0

    def overall_mem_stats(self, device_type: str) -> List[int]:
        if device_type == 'cuda':
            return self._overall_cuda_list
        elif device_type == 'cpu':
            return self._overall_cpu_list
        else:
            raise TypeError

    def model_data_list(self, device_type: str) -> List[int]:
        if device_type == 'cuda':
            return self._model_data_cuda_list
        elif device_type == 'cpu':
            return self._model_data_cpu_list
        else:
            raise TypeError

    def non_model_data_list(self, device_type: str) -> List[int]:
        if device_type == 'cuda':
            return self._non_model_data_cuda_list
        elif device_type == 'cpu':
            return self._non_model_data_cpu_list
        else:
            raise TypeError

    def next_period_non_model_data_usage(self, device_type: str) -> int:
        """Get max non model data memory usage of current sampling period

        Args:
            device_type (str): device type, can be 'cpu' or 'cuda'.

        Returns:
            int: max non model data memory usage of current sampling period
        """
        assert not self._start_flag, 'Cannot get mem stats info during collection phase.'
        assert self._step_total > 0, 'Cannot get mem stats info before collection phase.'
        next_non_model_data = self.non_model_data_list(device_type)[self._step_idx]
        self._step_idx = (self._step_idx + 1) % self._step_total
        return next_non_model_data

    @property
    def sampling_time(self):
        return [t - self._sampling_time[0] for t in self._sampling_time]

    def start_collection(self):
        self._start_flag = True
        self._mem_monitor.start()

    def finish_collection(self):
        self.sample_overall_data()
        self._step_total = len(self._sampling_time)
        self._start_flag = False
        self._mem_monitor.finish()

    def sample_model_data(self) -> None:
        """Sampling model data statistics.
        """
        if self._start_flag:
            cuda_mem = StatefulTensor.GST_MGR.total_mem['cuda']
            cpu_mem = StatefulTensor.GST_MGR.total_mem['cpu']
            self._model_data_cuda_list.append(cuda_mem)
            self._model_data_cpu_list.append(cpu_mem)

    def sample_overall_data(self) -> None:
        """Sampling non model data statistics.
        """
        if self._start_flag:
            # overall data recording is after model data recording
            if len(self._model_data_cuda_list) == 0:
                return

            self._overall_cuda_list.append(self._mem_monitor.finish())
            self._overall_cpu_list.append(colo_device_memory_used(torch.device('cpu')))

            assert len(self._model_data_cuda_list) == len(self._overall_cuda_list)

            self._non_model_data_cuda_list.append(self._overall_cuda_list[-1] - self._model_data_cuda_list[-1])
            self._non_model_data_cpu_list.append(self._overall_cpu_list[-1] - self._model_data_cpu_list[-1])
            self._sampling_time.append(time.time())
            self._mem_monitor.start()

    def clear(self) -> None:
        self._model_data_cuda_list = []
        self._overall_cuda_list = []

        self._model_data_cpu_list = []
        self._overall_cpu_list = []

        self._non_model_data_cpu_list = []
        self._non_model_data_cuda_list = []

        self._start_flag = False
        self._step_idx = 0
        self._step_total = 0


class MemStatsCollectorV2(MemStatsCollector):

    def __init__(self, chunk_manager: ChunkManager) -> None:
        super().__init__()
        self._chunk_manager = chunk_manager

    def sample_model_data(self) -> None:
        """Sampling model data statistics.
        """
        if self._start_flag:
            cuda_mem = self._chunk_manager.total_mem['cuda']
            cpu_mem = self._chunk_manager.total_mem['cpu']
            self._model_data_cuda_list.append(cuda_mem)
            self._model_data_cpu_list.append(cpu_mem)

    @property
    def cuda_margin_mem(self) -> float:
        return colo_device_memory_capacity(get_current_device()) - max(self.overall_mem_stats('cuda'))


class MemStatsCollectorStatic(MemStatsCollector):
    """
    A Static Memory statistic collector.
    """

    def __init__(self, module: nn.Module) -> None:
        super().__init__()
        self.module = module
        self.module_info_list = []


    def init_mem_stats(self, **kwargs):

        self.register_opnodes_recursively(self.module)
        self.refactor_module()

        # self.module = self.module.cpu()
        self.module.train()

        graph = ColoTracer().trace(self.module, meta_args=kwargs)
        gm = torch.fx.GraphModule(self.module, graph)
        interp = MetaInfoProp(gm)
        interp.propagate(*[MetaTensor(v, fake_device='cuda:0') for k, v in kwargs.items()])

        module_name_list = [mInfo.module_full_name for mInfo in self.module_info_list]
        fwd_out_released = {}
        total_mem = 0

        # forward
        for node in gm.graph.nodes:
            total_mem = total_mem + calculate_fwd_tmp(node) + calculate_fwd_out(node)
            if calculate_fwd_out(node) > 0:
                fwd_out_released[node] = False
            if node.op == "call_module":
                if node.name.endswith("_0") and node.name[:-2] in module_name_list:
                    self._non_model_data_cuda_list.append(total_mem)

        self._non_model_data_cuda_list.append(total_mem)
        self._non_model_data_cuda_list = self._non_model_data_cuda_list[1:]

        peak_mem = total_mem
        grad_in_computed = {}

        # backward
        for node in gm.graph.nodes.__reversed__():

            if node.name.__contains__("where") or node.name.__contains__("truediv"):
                continue

            # before run backward of the node

            total_mem = total_mem + node.meta["bwd_mem_tmp"] + node.meta["bwd_mem_out"]
            if total_mem >= peak_mem:
                peak_mem = total_mem

            # after run backward of the node

            total_mem -= node.meta["bwd_mem_tmp"]
            total_mem -= calculate_fwd_tmp(node)

            # release grad_in of current node
            for grad_in in node.meta["fwd_out"]:
                if isinstance(grad_in, torch.Tensor):
                    total_mem -= grad_in.numel() * torch.tensor([], dtype=grad_in.dtype).element_size()

            for in_node in node.args:
                if isinstance(in_node, torch.fx.node.Node):
                    # release fwd_in (fwd_out) of current node (input nodes)
                    if calculate_fwd_out(in_node) > 0 and (not fwd_out_released[in_node]):
                        total_mem -= calculate_fwd_out(in_node)
                        fwd_out_released[in_node] = True
                    # map multiple gradients of output to one tensor
                    if grad_in_computed.get(in_node, False):
                        total_mem -= calculate_fwd_out(in_node)
                        grad_in_computed[in_node] = True

            if node.name == "output":
                for in_node in node.args:
                    if isinstance(in_node, torch.fx.node.Node):
                        total_mem += calculate_fwd_out(in_node)

            if node.op == "call_module":
                if node.name.endswith("_0") and node.name[:-2] in module_name_list:
                    self._non_model_data_cuda_list.append(peak_mem)
                    peak_mem = total_mem

        self._step_total = len(self._non_model_data_cuda_list)
        self.recover_module()


    def refactor_module(self):
        for modInfo in self.module_info_list:
            temp_node = nn.Sequential(nn.Identity(), modInfo.module)
            modInfo.parent_module.__setattr__(modInfo.module_name, temp_node)


    def recover_module(self):
        for modInfo in self.module_info_list:
            modInfo.parent_module.__setattr__(modInfo.module_name, modInfo.module)


    def register_opnodes_recursively(self,
                                     module: torch.nn.Module,
                                     name: str = "",
                                     full_name: str = "",
                                     parent_module: Optional[torch.nn.Module] = None):

        assert isinstance(module, torch.nn.Module)

        for child_name, child in module.named_children():
            self.register_opnodes_recursively(child, child_name, full_name + "_" + child_name, module)

        # Early return on modules with no parameters.
        if len(list(module.parameters(recurse=False))) == 0:
            return

        self.module_info_list.append(ModuleInfos(module, name, full_name[1:], parent_module))


class ModuleInfos:

    def __init__(self,
                 module: torch.nn.Module,
                 module_name: str,
                 module_full_name: str,
                 parent_module: torch.nn.Module):
        self.module = module
        self.module_name = module_name
        self.module_full_name = module_full_name
        self.parent_module = parent_module