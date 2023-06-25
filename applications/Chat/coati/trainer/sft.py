import time
from typing import List, Optional

import torch
import torch.distributed as dist
import wandb
from torch.optim import Optimizer
from torch.optim.lr_scheduler import _LRScheduler
from torch.utils.data import DataLoader
from tqdm import tqdm

from .base import SLTrainer
from .callbacks import Callback
from .strategies import ColossalAIStrategy, Strategy
from .utils import is_rank_0, to_device


class SFTTrainer(SLTrainer):
    """
        Trainer to use while training reward model.

    Args:
        model (torch.nn.Module): the model to train
        strategy (Strategy): the strategy to use for training
        optim(Optimizer): the optimizer to use for training
        lr_scheduler(_LRScheduler): the lr scheduler to use for training
        train_dataloader: the dataloader to use for training
        eval_dataloader: the dataloader to use for evaluation
        max_epochs (int, defaults to 2): the number of epochs to train
        accumulation_steps (int, defaults to 8): the number of steps to accumulate gradients
    """

    def __init__(
        self,
        model,
        strategy: Strategy,
        optim: Optimizer,
        lr_scheduler: _LRScheduler,
        train_dataloader: DataLoader,
        eval_dataloader: Optional[DataLoader] = None,
        max_epochs: int = 2,
        accumulation_steps: int = 8,
    ) -> None:
        if accumulation_steps > 1 and isinstance(strategy, ColossalAIStrategy):
            from colossalai.booster.plugin import GeminiPlugin
            assert not isinstance(strategy.plugin, GeminiPlugin), \
                "Accumulation steps are not supported in stage 3 of ColossalAI"
        super().__init__(
            strategy, max_epochs,
            model, optim, train_dataloader
        )

        self.eval_dataloader = eval_dataloader

        self.accumulation_steps = accumulation_steps
        self.scheduler = lr_scheduler

    def fit(self, logger, use_wandb: bool = False):
        if use_wandb:
            wandb.init(project="Coati", name=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
            wandb.watch(self.model)
        total_loss = 0
        # epoch_bar = tqdm(range(self.epochs), desc='Epochs', disable=not is_rank_0())
        step_bar = tqdm(range(len(self.train_dataloader) // self.accumulation_steps * self.max_epochs),
                        desc=f'steps',
                        disable=not is_rank_0())
        for epoch in range(self.max_epochs):

            # process_bar = tqdm(range(len(self.train_dataloader)), desc=f'Train process for{epoch}', disable=not is_rank_0())
            # train
            self.model.train()
            for batch_id, batch in enumerate(self.train_dataloader):

                batch = to_device(batch, torch.cuda.current_device())
                outputs = self.model(batch["input_ids"], attention_mask=batch["attention_mask"], labels=batch["labels"])

                loss = outputs.loss

                if loss >= 2.5 and is_rank_0():
                    logger.warning(f"batch_id:{batch_id}, abnormal loss: {loss}")

                loss = loss / self.accumulation_steps

                self.strategy.backward(loss, self.model, self.optimizer)

                total_loss += loss.item()

                # gradient accumulation
                if (batch_id + 1) % self.accumulation_steps == 0:
                    self.strategy.optimizer_step(self.optimizer)
                    self.optimizer.zero_grad()
                    self.scheduler.step()
                    if is_rank_0() and use_wandb:
                        wandb.log({
                            "loss": total_loss / self.accumulation_steps,
                            "lr": self.scheduler.get_last_lr()[0],
                            "epoch": epoch,
                            "batch_id": batch_id
                        })
                    total_loss = 0
                    step_bar.update()

                # if batch_id % log_interval == 0:
                # logger.info(f'Train Epoch {epoch}/{self.epochs} Batch {batch_id} Rank {dist.get_rank()} loss {loss.item()}')
                # wandb.log({"loss": loss.item()})

                # process_bar.update()

            # eval
            if self.eval_dataloader is not None:
                self.model.eval()
                with torch.no_grad():
                    loss_sum = 0
                    num_seen = 0
                    for batch in self.eval_dataloader:
                        batch = to_device(batch, torch.cuda.current_device())
                        outputs = self.model(batch["input_ids"],
                                             attention_mask=batch["attention_mask"],
                                             labels=batch["labels"])
                        loss = outputs.loss

                        loss_sum += loss.item()
                        num_seen += batch["input_ids"].size(0)

                    loss_mean = loss_sum / num_seen
                    if dist.get_rank() == 0:
                        logger.info(f'Eval Epoch {epoch}/{self.max_epochs} loss {loss_mean}')

            # epoch_bar.update()
