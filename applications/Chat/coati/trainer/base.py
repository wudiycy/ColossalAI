from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Union

import torch
import torch.nn as nn
import tqdm
from coati.experience_maker import Experience
from torch.optim import Optimizer
from torch.utils.data import DataLoader

from .callbacks import Callback
from .strategies import Strategy
from .utils import is_rank_0


class SLTrainer(ABC):
    """
        Base class for supervised learning trainers.

    Args:
        strategy (Strategy):the strategy to use for training
        max_epochs (int, defaults to 1): the number of epochs of training process
        model (nn.Module): the model to train
        optim (Optimizer): the optimizer to use for training
        train_dataloader (DataLoader): the dataloader to use for training
    """

    def __init__(self,
                 strategy: Strategy,
                 max_epochs: int,
                 model: nn.Module,
                 optimizer: Optimizer,
                 train_dataloader: DataLoader,
                 ) -> None:
        super().__init__()
        self.strategy = strategy
        self.max_epochs = max_epochs
        self.model = model
        self.optimizer = optimizer
        self.train_dataloader = train_dataloader

    @abstractmethod
    def _train(self, epoch):
        raise NotImplementedError()

    @abstractmethod
    def _eval(self, epoch):
        raise NotImplementedError()

    def _before_fit(self):
        self.no_epoch_bar = False

    def fit(self, *args, **kwargs):
        self._before_fit(*args, **kwargs)
        for epoch in tqdm.trange(self.max_epochs,
                                 desc="Epochs",
                                 disable=not is_rank_0() or self.no_epoch_bar
                                 ):
            self._train(epoch)
            self._eval(epoch)

    def _on_fit_end(self) -> None:
        for callback in self.callbacks:
            callback.on_fit_end()

    def _on_episode_start(self, episode: int) -> None:
        for callback in self.callbacks:
            callback.on_episode_start(episode)

    def _on_episode_end(self, episode: int) -> None:
        for callback in self.callbacks:
            callback.on_episode_end(episode)

    def _on_make_experience_start(self) -> None:
        for callback in self.callbacks:
            callback.on_make_experience_start()

    def _on_make_experience_end(self, experience: Experience) -> None:
        for callback in self.callbacks:
            callback.on_make_experience_end(experience)

    def _on_learn_epoch_start(self, epoch: int) -> None:
        for callback in self.callbacks:
            callback.on_learn_epoch_start(epoch)

    def _on_learn_epoch_end(self, epoch: int) -> None:
        for callback in self.callbacks:
            callback.on_learn_epoch_end(epoch)

    def _on_learn_batch_start(self) -> None:
        for callback in self.callbacks:
            callback.on_learn_batch_start()

    def _on_learn_batch_end(self, metrics: dict, experience: Experience) -> None:
        for callback in self.callbacks:
            callback.on_learn_batch_end(metrics, experience)
