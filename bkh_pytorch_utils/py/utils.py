from typing import Union

from .cm_helper import pretty_plot_confusion_matrix

import os
import math
import random
import pickle
import torch

import numpy as np
import pandas as pd
from tqdm.auto import tqdm
import monai as mn
import pytorch_lightning as pl

from string import ascii_uppercase
from sklearn.metrics import confusion_matrix
from sklearn.model_selection import KFold, GroupKFold, StratifiedKFold, StratifiedGroupKFold

class CosineAnnealingWarmupRestarts(torch.optim.lr_scheduler._LRScheduler):
    def __init__(self,
                 optimizer : torch.optim.Optimizer,
                 first_cycle_steps : int,
                 cycle_mult : float = 1.,
                 max_lr : float = 0.1,
                 min_lr : float = 0.001,
                 warmup_steps : int = 0,
                 gamma : float = 1.,
                 last_epoch : int = -1
        ):
        assert warmup_steps < first_cycle_steps

        self.first_cycle_steps = first_cycle_steps # first cycle step size
        self.cycle_mult = cycle_mult # cycle steps magnification
        self.base_max_lr = max_lr # first max learning rate
        self.max_lr = max_lr # max learning rate in the current cycle
        self.min_lr = min_lr # min learning rate
        self.warmup_steps = warmup_steps # warmup step size
        self.gamma = gamma # decrease rate of max learning rate by cycle

        self.cur_cycle_steps = first_cycle_steps # first cycle step size
        self.cycle = 0 # cycle count
        self.step_in_cycle = last_epoch # step size of the current cycle

        super(CosineAnnealingWarmupRestarts, self).__init__(optimizer, last_epoch)

        # set learning rate min_lr
        self.init_lr()

    def init_lr(self):
        self.base_lrs = []
        for param_group in self.optimizer.param_groups:
            param_group['lr'] = self.min_lr
            self.base_lrs.append(self.min_lr)

    def get_lr(self):
        if self.cycle>0 and self.cycle_mult==0:
            return self.base_lrs
        elif self.step_in_cycle == -1:
            return self.base_lrs
        elif self.step_in_cycle < self.warmup_steps:
            return [(self.max_lr - base_lr)*self.step_in_cycle / self.warmup_steps + base_lr for base_lr in self.base_lrs]
        else:
            return [base_lr + (self.max_lr - base_lr) \
                    * (1 + math.cos(math.pi * (self.step_in_cycle-self.warmup_steps) \
                                    / (self.cur_cycle_steps - self.warmup_steps))) / 2
                    for base_lr in self.base_lrs]

    def step(self, epoch=None):
        if epoch is None:
            epoch = self.last_epoch + 1
            self.step_in_cycle = self.step_in_cycle + 1
            if self.step_in_cycle >= self.cur_cycle_steps:
                self.cycle += 1
                self.step_in_cycle = self.step_in_cycle - self.cur_cycle_steps
                self.cur_cycle_steps = int((self.cur_cycle_steps - self.warmup_steps) * self.cycle_mult) + self.warmup_steps
        else:
            if epoch >= self.first_cycle_steps:
                if self.cycle_mult == 1.:
                    self.step_in_cycle = epoch % self.first_cycle_steps
                    self.cycle = epoch // self.first_cycle_steps
                else:
                    n = int(math.log((epoch / self.first_cycle_steps * (self.cycle_mult - 1) + 1), self.cycle_mult))
                    self.cycle = n
                    self.step_in_cycle = epoch - int(self.first_cycle_steps * (self.cycle_mult ** n - 1) / (self.cycle_mult - 1))
                    self.cur_cycle_steps = self.first_cycle_steps * self.cycle_mult ** (n)
            else:
                self.cur_cycle_steps = self.first_cycle_steps
                self.step_in_cycle = epoch

        self.max_lr = self.base_max_lr * (self.gamma**self.cycle)
        self.last_epoch = math.floor(epoch)
        for param_group, lr in zip(self.optimizer.param_groups, self.get_lr()):
            param_group['lr'] = lr

class NonSparseCrossEntropyLoss(torch.nn.modules.loss._WeightedLoss):
    def __init__(self, weight=None, reduction='mean'):
        super().__init__(weight=weight, reduction=reduction)
        self.weight = weight
        self.reduction = reduction

    def forward(self, inputs, targets):
        lsm = torch.nn.functional.log_softmax(inputs, -1)

        if self.weight is not None:
            lsm = lsm * self.weight.unsqueeze(0)

        loss = -(targets * lsm).sum(-1)

        if self.reduction == 'sum':
            loss = loss.sum()
        elif self.reduction == 'mean':
            loss = loss.mean()

        return loss


def seed_all(seed:int) -> None:
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    mn.utils.misc.set_determinism(seed=seed)
    pl.seed_everything(seed,workers=True)

def get_data_stats(dataset:torch.utils.data.Dataset, img_key:str, num_channels:int = 1)->None:
    pixels_sum=torch.zeros(num_channels)
    pixels_count=torch.zeros(num_channels)
    sum_squared_err=torch.zeros(num_channels)

    for i,b in enumerate(tqdm(dataset)):
        image = b[img_key]
        pixels_sum = pixels_sum+image.sum((1,2))
        pixels_count = pixels_count+torch.tensor([image.shape[1]*image.shape[2]]*num_channels)

    mean = pixels_sum/pixels_count

    for i,b in enumerate(tqdm(dataset)):
        image = b[img_key].reshape(-1,num_channels)
        sum_squared_err = sum_squared_err + ((image - mean).pow(2)).sum()

    std = torch.sqrt(sum_squared_err / pixels_count)

    print("Final Mean:",mean)
    print("Final Std:",std)

def one_hot_encode(true_labels: torch.Tensor, classes: int, smoothing=0.0):
    assert 0 <= smoothing < 1
    confidence = 1.0 - smoothing
    label_shape = torch.Size((true_labels.size(0), classes))
    if label_shape == true_labels.size():
        with torch.no_grad():
            true_dist = torch.where(true_labels==1.0, confidence, smoothing)
    else:
        with torch.no_grad():
            true_dist = torch.empty(size=label_shape, device=true_labels.device)
            true_dist.fill_(smoothing / (classes - 1))
            true_dist.scatter_(1, true_labels.data.unsqueeze(1), confidence)

    return true_dist

def plot_confusion_matrix(preds:np.array, targets:np.array, columns:list=None, annot:bool=True, cmap:str="Oranges",
      fmt:str='.2f', fz:int=13, lw:float=0.5, cbar:bool=False, figsize:list=[9,9], show_null_values:int=1, pred_val_axis:str='x', save_name=None):

    if columns is None:
        columns = ['Class %s' %(i) for i in list(ascii_uppercase)[0:len(np.unique(targets))]]

    matrix = confusion_matrix(targets, preds)
    df_cm = pd.DataFrame(matrix, index=columns, columns=columns)

    pretty_plot_confusion_matrix(df_cm, fz=fz, cmap=cmap, figsize=figsize, annot=annot, fmt=fmt, lw=lw, cbar=cbar, show_null_values=show_null_values, pred_val_axis=pred_val_axis, save_name = save_name)

def load_weights(model: torch.nn.Module, weight_path: str = None):
    weights = torch.load(weight_path)
    model_dict = model.state_dict()
    weights = {k: v for k, v in weights.items() if k in model_dict}
    model_dict.update(weights) 
    model.load_state_dict(model_dict)

    return model


def add_weight_decay(model: torch.nn.Module, weight_decay:float=1e-5, skip_list:list=[]):
    #########################################################################################################
    ### Adapted from: https://github.com/rwightman/pytorch-image-models/tree/master/timm
    #########################################################################################################

    decay = []
    no_decay = []
    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue  # frozen weights
        if len(param.shape) == 1 or name.endswith(".bias") or name in skip_list:
            no_decay.append(param)
        else:
            decay.append(param)

    return [
        {'params': no_decay, 'weight_decay': 0.},
        {'params': decay, 'weight_decay': weight_decay},
    ]
    
def is_notebook_running():
    try:
        shell = get_ipython().__class__.__name__
        if shell == 'ZMQInteractiveShell':
            return True   # Jupyter notebook or qtconsole
        elif shell == 'TerminalInteractiveShell':
            return False  # Terminal running IPython
        else:
            return False  # Other type (?)
    except NameError:
        return False      # Probably standard Python interpreter

def split_data(df: pd.DataFrame, n_splits: int, y_column: str=None, group_column:str=None, fold_column: str="Fold", shuffle=False, random_state=None):
    df = df.copy()

    if random_state is not None:
        shuffle = True
    elif shuffle and random_state is None:
        random_state = 42

    if y_column is None and group_column is None:
        splitter = KFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
        print("Using simple KFold split...")
    elif y_column is not None and group_column is None:
        splitter = StratifiedKFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
        print("Using StratifiedKFold split...")
    elif y_column is None and group_column is not None:
        splitter = GroupKFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
        print("Using GroupKFold split...")
    elif y_column is not None and group_column is not None:
        splitter = StratifiedGroupKFold(n_splits=n_splits, shuffle=shuffle, random_state=random_state)
        print("Using StratifiedGroupKFold split...")

    df[fold_column] = 0

    for fold_idx, (train_index, val_index) in enumerate(splitter.split(df, y=df[y_column].tolist() if y_column is not None else None, groups=df[group_column].tolist() if group_column is not None else None)):
        df.loc[val_index,fold_column]=fold_idx

    return df
    