#!/usr/bin/env python
# encoding: utf-8

# The MIT License (MIT)

# Copyright (c) 2019-2020 CNRS

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

# AUTHORS
# Hervé BREDIN - http://herve.niderb.fr

"""Domain classification"""

import random
import math
import numpy as np

from torch.utils.data import IterableDataset
from typing import List, Dict

from pyannote.database import ProtocolFile
from pyannote.database import Protocol
from pyannote.database import Subset

from pyannote.core import Segment

from pyannote.audio.train.task import BaseTask
from pyannote.audio.train.task import Problem
from pyannote.audio.train.task import Resolution

from collections import Counter


class Dataset(IterableDataset):
    def __init__(self, task: "DomainClassification"):
        super().__init__()
        self.task = task

    def __iter__(self):

        while True:

            # select one file at random (with probability proportional to its annotated duration)
            file, *_ = random.choices(
                self.task.files,
                weights=[file["_dataloader_duration"] for file in self.task.files],
                k=1,
            )

            # select one annotated region at random (with probability proportional to its duration)
            segment, *_ = random.choices(
                file["annotated"], weights=[s.duration for s in file["annotated"]], k=1,
            )

            # select one chunk at random (with uniform distribution)
            start_time = random.uniform(
                segment.start, segment.end - self.task.hparams.duration
            )
            chunk = Segment(start_time, start_time + self.task.hparams.duration)

            # extract features
            X = self.task.feature_extraction.crop(
                file, chunk, mode="center", fixed=self.task.hparams.duration
            )

            # extract target
            y = file["_dataloader_target"]

            # yield batch
            yield {"X": X, "y": y}

    def __len__(self):
        num_samples = math.ceil(
            self.task._dataloader_duration / self.task.hparams.duration
        )

        # TODO: remove when https://github.com/pytorch/pytorch/pull/38925 is released
        num_samples = max(1, num_samples // self.task.hparams.batch_size)

        return num_samples


class DomainClassification(BaseTask):

    problem = Problem.MULTI_CLASS_CLASSIFICATION
    resolution_input = Resolution.FRAME
    resolution_output = Resolution.CHUNK

    def get_classes(self):
        return sorted(set(file[self.hparams.domain] for file in self.files))

    def prepare_data(self):

        for file in self.files:
            file["_dataloader_duration"] = sum(
                s.duration
                for s in file["annotated"]
                if s.duration > self.hparams.duration
            )

            file["_dataloader_target"] = self.classes.index(file[self.hparams.domain])

        # estimate what an 'epoch' is
        self._dataloader_duration = sum(
            file["_dataloader_duration"] for file in self.files
        )

    def train_dataset(self) -> IterableDataset:
        return Dataset(self)

    @staticmethod
    def validation_criterion(protocol: Protocol):
        return "accuracy"

    def validation(
        self,
        files: List[ProtocolFile],
        protocol: Protocol = None,
        subset: Subset = "development",
        warm_start: Dict = None,
        epoch: int = None,
    ):
        """Validation

        Validation consists in looking for the value of the detection threshold 
        that maximizes the f-score of recall and precision.
        """

        criterion = self.validation_criterion(protocol)
        domains = self.classes

        y_true_file, y_pred_file = [], []

        for file in files:
            y_pred = np.argmax(file["scores"], axis=1)
            y_pred_file.append(Counter(y_pred).most_common(1)[0][0])

            y_true = domains.index(file[self.hparams.domain])
            y_true_file.append(y_true)

        accuracy = np.mean(np.array(y_true_file) == np.array(y_pred_file))

        return {
            "metric": criterion,
            "minimize": False,
            "value": float(accuracy),
        }
