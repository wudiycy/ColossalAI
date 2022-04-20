import functools
from .api import (
    _register_stateful_op,)


def stateful_op_impl(func):
    """
    Provides a way for users to write their own custom operator. This
    can be used to override existing StatefulTensorV2 operators or write a new
    one not supported by StatefulTensorV2. If the operator in question is covered
    by ``__torch_function__`` dispatch and has a StatefulTensorV2 as any of its
    parameters, the function provided will be invoked for that operator.

    Example::
        >>> @custom_stateful_op(torch.nn.functional.linear)
        >>> def my_custom_linear(types, args, kwargs, process_group):
        >>>   ....
        >>>
        >>> input = torch.rand(10, 32)
        >>> weight = sharded_tensor.rand(32, 16)
        >>> bias = torch.rand(16)
        >>> # This will call 'my_custom_stateful_linear'
        >>> torch.nn.functional.linear(input, weight, bias)

    The types, args and kwargs parameters are the same parameters that are
    passed to ``__torch_function__`` dispatch API
    (https://pytorch.org/docs/stable/notes/extending.html#extending-torch).

    Args:
        func(Callable): Torch function for which we want to provide a sharded
            implementation (ex: torch.nn.functional.linear)
    """

    def decorator_sharded_func(wrapped_func):
        _register_stateful_op(func, wrapped_func)

        @functools.wraps(wrapped_func)
        def wrapper(*args, **kwargs):
            return wrapped_func(*args, **kwargs)

        return wrapper

    return decorator_sharded_func
