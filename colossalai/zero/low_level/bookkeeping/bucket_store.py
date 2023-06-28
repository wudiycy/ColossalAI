from typing import Dict

import torch
from torch import Tensor
from torch._utils import _flatten_dense_tensors
from torch.distributed import ProcessGroup

from .base_store import BaseStore


class BucketStore(BaseStore):

    def __init__(self, torch_pg: ProcessGroup):
        super().__init__(torch_pg)

        # init and reset
        self.current_group_id = 0
        # mapping gardient slices and parameter
        self.grad_to_param_mapping = dict()

        self._param_list = []
        self._padding_size = []

        self.reset()

    def num_elements_in_bucket(self) -> int:
        """
        Return the total number of elements in bucket

        :return: Return the total number of elements in bucket
        :rtype: int

        """
        return self._num_elements_in_bucket

    def add_param_grad(self, group_id: int, param: Tensor, padding_size: int):
        """
        Add a param to bucket and record the padding size of a param for gradient padding
        :param group_id: The index of a parameter group
        :param param: The parameter
        :param paddind_size: The padding size of the parameter
        :type group_id: int
        :type param: Tensor
        :type padding_size: int

        """
        self._param_list.append(param)
        self._padding_size.append(padding_size)
        self._num_elements_in_bucket += (param.numel() + padding_size)
        self.current_group_id = group_id

    def build_grad_in_bucket(self):
        """
        Orgnize parameters' gradient(padding and split), follows the paramters' splitting method

        Data structure of self._grad_in_bucket:
        {
        rank0: [grad0_rank0, grad1_rank0, ...]
        rank1: [grad1_rank1, grad1_rank1, ...]
        }
        """

        for param, padding_size in zip(self._param_list, self._padding_size):
            with torch.no_grad():
                grad = param.grad.view(-1)
                if padding_size > 0:
                    grad = torch.nn.function.pad(grad, [0, padding_size])
                grad_list = grad.split(grad.numel() // self._world_size)
                for rank in range(self._world_size):
                    grad_current_rank = grad_list[rank].detach()
                    self.grad_to_param_mapping[id(grad_current_rank)] = id(param)
                    self._grad_in_bucket[rank].append(grad_current_rank)
            param.grad = None

    def get_grad(self) -> Dict:
        """
        Return the dictionary of gradients slices, of which the keys are ranks

        :return: Return the dictionary of gradients slices.
        :rtype: List[torch.Tensor] or []
        """
        return self._grad_in_bucket

    def get_flatten_grad(self) -> Tensor:
        """
        Return the flattened gradients slices in the bucket, the data orginization of the flattened tensor:
        [grad0_rank0, grad1_rank0, ..., grad_1_rank0, grad1_rank1, ....]


        :return: Return the flattened gradients slices in the bucket
        :rtype: Tensor
        """
        flat_grad = []
        for _, grad_list in self._grad_in_bucket.items():
            flat_grad.append(_flatten_dense_tensors(grad_list))
        flat_grad = _flatten_dense_tensors(flat_grad)
        return flat_grad

    def get_param_id_of_grad(self, grad: Tensor) -> int:
        """
        Return the id of a parameter which the gradient slice belongs to

        :return: Return the id of a parameter which the gradient slice belongs to
        :rtype: int

        """
        return self.grad_to_param_mapping[id(grad)]

    def reset(self):
        self.grad_to_param_mapping = dict()
        self._num_elements_in_bucket = 0
        self._param_list = []
        self._grad_in_bucket = dict()
        for rank in range(self._world_size):
            self._grad_in_bucket[rank] = []
