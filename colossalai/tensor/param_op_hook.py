import torch
from contextlib import contextmanager
from abc import ABC, abstractmethod
from typing import List, Tuple, Any


class ParamOpHook(ABC):
    """Hook which is triggered by each operation when operands contain ColoParameter.
    To customize it, you must inherit this abstract class, and implement ``pre_forward``,
    ``post_forward``, ``pre_backward`` and ``post_backward``. These four methods take a list
    of ColoParameter.
    """

    @abstractmethod
    def pre_forward(self, params: List[torch.Tensor]) -> None:
        pass

    @abstractmethod
    def post_forward(self, params: List[torch.Tensor]) -> None:
        pass

    @abstractmethod
    def pre_backward(self, params: List[torch.Tensor]) -> None:
        pass

    @abstractmethod
    def post_backward(self, params: List[torch.Tensor]) -> None:
        pass


class ParamOpHookManager:
    """Manage your param op hooks. It only has static methods.
    The only static method you should call is ``use_hooks(*hooks)``.
    """
    hooks: Tuple[ParamOpHook, ...] = tuple()

    @staticmethod
    @contextmanager
    def use_hooks(*hooks: ParamOpHook):
        """Change the param op hooks you use. Nested calling is allowed.

        Example::
            >>> with ParamOpHookManager.use_hooks(*hooks):
            >>>     do_something()
            >>>     with ParamOpHookManager.use_hooks():
            >>>         // clear hooks
            >>>         do_something()
        """
        try:
            old_param_op_hooks = ParamOpHookManager.hooks
            ParamOpHookManager.hooks = hooks
            yield
        finally:
            ParamOpHookManager.hooks = old_param_op_hooks

    @staticmethod
    def _trigger_pre_forward(params: List[torch.Tensor]) -> None:
        for hook in ParamOpHookManager.hooks:
            hook.pre_forward(params)

    @staticmethod
    def _trigger_post_forward(params: List[torch.Tensor]) -> None:
        for hook in ParamOpHookManager.hooks:
            hook.post_forward(params)

    @staticmethod
    def _trigger_pre_backward(params: List[torch.Tensor]) -> None:
        for hook in ParamOpHookManager.hooks:
            hook.pre_backward(params)

    @staticmethod
    def _trigger_post_backward(params: List[torch.Tensor]) -> None:
        for hook in ParamOpHookManager.hooks:
            hook.post_backward(params)

    @staticmethod
    def pre_op(params: List[torch.Tensor], *args: Any) -> Any:
        ParamOpHookManager._trigger_pre_forward(params)
        return PreFwdPostBwd.apply(params, *args)

    @staticmethod
    def post_op(params: List[torch.Tensor], args: Any) -> Any:
        ParamOpHookManager._trigger_post_backward(params)
        return PostFwdPreBwd.apply(params, args)

    @staticmethod
    def has_hook() -> bool:
        return len(ParamOpHookManager.hooks) > 0


class PreFwdPostBwd(torch.autograd.Function):

    @staticmethod
    def forward(ctx, params, *args):
        ctx.params = params
        if len(args) == 1:
            return args[0]
        return args

    @staticmethod
    def backward(ctx, *grads):
        ParamOpHookManager._trigger_post_backward(ctx.params)
        return (None,) + grads


class PostFwdPreBwd(torch.autograd.Function):

    @staticmethod
    def forward(ctx, params, args):
        ctx.params = params
        return args

    @staticmethod
    def backward(ctx, *grads):
        ParamOpHookManager._trigger_pre_backward(ctx.params)
        return (None,) + grads
