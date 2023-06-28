from typing import List

from torch import Tensor
from torch._utils import _flatten_dense_tensors

from .base_store import BaseStore


class GradientStore(BaseStore):

    def __init__(self, *args, partition_grad: bool = False):
        super().__init__(*args)
        """
        self._grads_of_params mapping the paramater and its gradient slices
        data structure:
        {
         group_id:{
            param_id: [grad_rank0, grad_rank1, ...]
          }
        }
        """
        self._grads_of_params = dict()
        # for zero2, it's `param_id: [grad_local_rank]`
        self._working_index = 0 if partition_grad else self._local_rank

    def get_partitioned_gradients_by_param_id(self, group_id: int, param_id: int) -> List:
        """
        Return list of gradient slices of a specific parameter
        :param group_id: The index of a parameter group
        :param param_id: The id of a parameter
        :type group_id: int
        :type param_id: int

        :return: Return the list of gradient slices of a parameter. Each element is a gradient, not a parameter.
        :rtype: List[torch.Tensor] or []
        """
        if group_id in self._grads_of_params:
            if param_id in self._grads_of_params[group_id]:
                return self._grads_of_params[group_id][param_id]
        # the param has no grad, for instance, in layer drop
        return []

    def append_gradients_by_param_id(self, grad: Tensor, group_id: int, param_id: int):
        """
        Append a gradient slice to the parameter's gradient slice list

        :param grad: The gradient slice to append to list
        :param group_id: The index of a parameter group
        :param param_id: The id of a parameter
        :type grad: torch.Tensor
        :type group_id: int
        :type param_id: int

        """
        if group_id not in self._grads_of_params:
            self._grads_of_params[group_id] = dict()
        if param_id not in self._grads_of_params[group_id]:
            self._grads_of_params[group_id][param_id] = [grad]
        else:
            self._grads_of_params[group_id][param_id].append(grad)

    def add_gradients_by_param_id(self, grad: Tensor, grad_idx: int, group_id: int, param_id: int):
        """
        For old gradient accumulation, not in use now.

        Add a gradient slice on an existing slice of the parameter's gradient

        :param grad: The split gradient to append to list
        :param grad_idx: The index of the existing slice
        :param group_id: The index of a parameter group
        :param param_id: The id of a parameter
        :type grad: torch.Tensor
        :type grad_idx: int
        :type group_id: int
        :type param_id: int

        """
        self._grads_of_params[group_id][param_id][grad_idx].add_(grad)

    def get_working_grads_by_group_id(self, group_id: int) -> List:
        """
        Return list of working gradient slices in the group
        :param group_id: The index of a parameter group
        :type group_id: int

        :return: Return the list working gradient slices in the group.
        :rtype: List[torch.Tensor]
        """
        grad_list = []
        for param_grads in self._grads_of_params[group_id].values():
            grad_list.append(param_grads[self._working_index])

        return grad_list

    def reset_grads_by_group_id(self, group_id: int):
        self._grads_of_params[group_id] = dict()

    def reset_all_gradients(self):
        self._grads_of_params = dict()
