# Copyright (c) 2023 BAAI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License")
import math
import time
import torch
import torch.utils.data
from torch.types import Device
import sys

from model import create_model
from optimizers import create_optimizer
from schedulers import create_scheduler
from train.evaluator import Evaluator
from train.training_state import TrainingState
from dataloaders.dataloader import get_coco_api_from_dataset
import utils.utils
from driver import Driver, dist_pytorch


class Trainer:

    def __init__(self, driver: Driver, adapter, evaluator: Evaluator,
                 training_state: TrainingState, device: Device, config):
        super(Trainer, self).__init__()
        self.driver = driver
        self.adapter = adapter
        self.training_state = training_state
        self.device = device
        self.config = config
        self.evaluator = evaluator

    def init(self):
        torch.set_num_threads(1)
        device = torch.device(self.config.device)
        dist_pytorch.main_proc_print("Init progress:")
        self.model = create_model()
        self.model.to(self.device)

        self.model = self.adapter.convert_model(self.model)
        self.model = self.adapter.model_to_fp16(self.model)
        self.model = self.adapter.model_to_ddp(self.model)

        self.optimizer = create_optimizer(self.model, self.config)
        self.lr_scheduler = create_scheduler(self.optimizer, self.config)

    def train_one_epoch(self, train_dataloader, eval_dataloader):
        model = self.model
        optimizer = self.optimizer
        data_loader = train_dataloader
        device = self.device
        state = self.training_state
        config = self.config
        epoch = state.epoch

        if self.config.distributed:
            train_dataloader.batch_sampler.sampler.set_epoch(epoch)

        model.train()
        noeval_start_time = time.time()
        metric_logger = utils.utils.MetricLogger(delimiter="  ")
        metric_logger.add_meter(
            'lr', utils.utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
        header = 'Epoch: [{}]'.format(epoch)

        lr_scheduler = None
        if epoch == 0:
            warmup_factor = 1. / 1000
            warmup_iters = min(1000, len(data_loader) - 1)

            lr_scheduler = utils.utils.warmup_lr_scheduler(
                optimizer, warmup_iters, warmup_factor)

        for images, targets in metric_logger.log_every(data_loader,
                                                       self.config.log_freq,
                                                       header):
            images = list(image.to(device) for image in images)
            targets = [{k: v.to(device)
                        for k, v in t.items()} for t in targets]

            pure_compute_start_time = time.time()
            loss_dict = model(images, targets)

            losses = sum(loss for loss in loss_dict.values())

            # reduce losses over all GPUs for logging purposes
            loss_dict_reduced = utils.utils.reduce_dict(loss_dict)
            losses_reduced = sum(loss for loss in loss_dict_reduced.values())

            loss_value = losses_reduced.item()

            if not math.isfinite(loss_value):
                print("Loss is {}, stopping training".format(loss_value))
                print(loss_dict_reduced)
                sys.exit(1)

            self.adapter.backward(losses, optimizer)

            if lr_scheduler is not None:
                lr_scheduler.step()

            metric_logger.update(loss=losses_reduced, **loss_dict_reduced)
            metric_logger.update(lr=optimizer.param_groups[0]["lr"])

            self.training_state.pure_compute_time += time.time(
            ) - pure_compute_start_time

        self.lr_scheduler.step()
        state.num_trained_samples += len(data_loader.dataset)
        self.training_state.no_eval_time += time.time() - noeval_start_time

        # evaluate
        self.evaluate(self.model, eval_dataloader, device=self.device)

        state.eval_mAP = self.evaluator.coco_eval['bbox'].stats.tolist()[0]
        print(state.eval_mAP)
        if state.eval_mAP >= config.target_mAP:
            dist_pytorch.main_proc_print(
                f"converged_success. eval_mAP: {state.eval_mAP}, target_mAP: {config.target_mAP}"
            )
            state.converged_success()

        if epoch >= config.max_epoch:
            state.end_training = True

    @torch.no_grad()
    def evaluate(self, model, data_loader, device):
        coco = get_coco_api_from_dataset(data_loader.dataset)
        self.evaluator = Evaluator(coco)
        cpu_device = torch.device("cpu")
        model.eval()
        metric_logger = utils.utils.MetricLogger(delimiter="  ")
        header = 'Test:'

        for images, targets in metric_logger.log_every(data_loader,
                                                       self.config.log_freq,
                                                       header):
            images = list(img.to(device) for img in images)

            torch.cuda.synchronize()
            model_time = time.time()
            outputs = model(images)

            outputs = [{k: v.to(cpu_device)
                        for k, v in t.items()} for t in outputs]
            model_time = time.time() - model_time

            res = {
                target["image_id"].item(): output
                for target, output in zip(targets, outputs)
            }
            evaluator_time = time.time()
            self.evaluator.update(res)
            evaluator_time = time.time() - evaluator_time
            metric_logger.update(model_time=model_time,
                                 evaluator_time=evaluator_time)

        # gather the stats from all processes
        metric_logger.synchronize_between_processes()
        print("Averaged stats:", metric_logger)
        self.evaluator.synchronize_between_processes()

        # accumulate predictions from all images
        self.evaluator.accumulate()
        self.evaluator.summarize()
        return self.evaluator
