from .base import WaveformModel

import torch
import torch.nn as nn
import math
from obspy.signal.trigger import trigger_onset
import numpy as np


class Conv1dSame(nn.Module):
    """
    Add PyTorch compatible support for Tensorflow/Keras padding option: padding='same'.

    Discussions regarding feature implementation:
    https://discuss.pytorch.org/t/converting-tensorflow-model-to-pytorch-issue-with-padding/84224
    https://github.com/pytorch/pytorch/issues/3867#issuecomment-598264120

    """

    def __init__(self, in_channels, out_channels, kernel_size, stride=1, dilation=1):
        super().__init__()
        self.cut_last_element = (
            kernel_size % 2 == 0 and stride == 1 and dilation % 2 == 1
        )
        self.padding = math.ceil((1 - stride + dilation * (kernel_size - 1)) / 2)
        self.conv = nn.Conv1d(
            in_channels,
            out_channels,
            kernel_size,
            padding=self.padding + 1,
            stride=stride,
            dilation=dilation,
        )

    def forward(self, x):
        if self.cut_last_element:
            return self.conv(x)[:, :, :-1]
        else:
            return self.conv(x)


class PhaseNet(WaveformModel):
    def __init__(self, in_channels=3, classes=3, phases="NPS", **kwargs):
        citation = (
            "Zhu, W., & Beroza, G. C. (2019). "
            "PhaseNet: a deep-neural-network-based seismic arrival-time picking method. "
            "Geophysical Journal International, 216(1), 261-273. "
            "https://doi.org/10.1093/gji/ggy423"
        )

        super().__init__(
            citation=citation,
            in_samples=3001,
            output_type="array",
            default_args={"overlap": 250},
            pred_sample=(0, 3001),
            labels=phases,
            **kwargs,
        )

        self.in_channels = in_channels
        self.classes = classes
        self.kernel_size = 7
        self.stride = 4
        self.activation = torch.relu

        self.inc = nn.Conv1d(self.in_channels, 8, 1)
        self.in_bn = nn.BatchNorm1d(8)

        self.conv1 = Conv1dSame(8, 11, self.kernel_size, self.stride)
        self.bnd1 = nn.BatchNorm1d(11)

        self.conv2 = Conv1dSame(11, 16, self.kernel_size, self.stride)
        self.bnd2 = nn.BatchNorm1d(16)

        self.conv3 = Conv1dSame(16, 22, self.kernel_size, self.stride)
        self.bnd3 = nn.BatchNorm1d(22)

        self.conv4 = Conv1dSame(22, 32, self.kernel_size, self.stride)
        self.bnd4 = nn.BatchNorm1d(32)

        self.up1 = nn.ConvTranspose1d(
            32, 22, self.kernel_size, self.stride, padding=self.conv4.padding
        )
        self.bnu1 = nn.BatchNorm1d(22)

        self.up2 = nn.ConvTranspose1d(
            44,
            16,
            self.kernel_size,
            self.stride,
            padding=self.conv3.padding,
            output_padding=1,
        )
        self.bnu2 = nn.BatchNorm1d(16)

        self.up3 = nn.ConvTranspose1d(
            32, 11, self.kernel_size, self.stride, padding=self.conv2.padding
        )
        self.bnu3 = nn.BatchNorm1d(11)

        self.up4 = nn.ConvTranspose1d(22, 8, self.kernel_size, self.stride, padding=3)
        self.bnu4 = nn.BatchNorm1d(8)

        self.out = nn.ConvTranspose1d(16, self.classes, 1)
        self.softmax = torch.nn.Softmax(dim=1)

    def forward(self, x):
        x_in = self.activation(self.in_bn(self.inc(x)))

        x1 = self.activation(self.bnd1(self.conv1(x_in)))
        x2 = self.activation(self.bnd2(self.conv2(x1)))
        x3 = self.activation(self.bnd3(self.conv3(x2)))
        x4 = self.activation(self.bnd4(self.conv4(x3)))

        x = torch.cat([self.activation(self.bnu1(self.up1(x4))), x3], dim=1)
        x = torch.cat([self.activation(self.bnu2(self.up2(x))), x2], dim=1)
        x = torch.cat([self.activation(self.bnu3(self.up3(x))), x1], dim=1)
        x = torch.cat([self.activation(self.bnu4(self.up4(x))), x_in], dim=1)

        x = self.out(x)
        x = self.softmax(x)

        return x

    def annotate_window_pre(self, window, argdict):
        # Add a demean and normalize step to the preprocessing
        window = window - np.mean(window, axis=-1, keepdims=True)
        std = np.std(window, axis=-1, keepdims=True)
        std[std == 0] = 1  # Avoid NaN errors
        window = window / std
        return window

    def annotate_window_post(self, pred, argdict):
        # Transpose predictions to correct shape
        return pred.T

    def classify_aggregate(self, annotations, argdict):
        """
        Converts the annotations to discrete thresholds using a classical trigger on/off.
        Trigger onset thresholds are derived from the argdict at keys "[phase]_threshold".
        For all triggers the lower threshold is set to half the higher threshold.
        For each pick a triple is returned, consisting of the trace_id ("net.sta.loc"), the pick time and the phase.

        :param annotations: See description in superclass
        :param argdict: See description in superclass
        :return: List of picks
        """
        picks = []
        for phase in self.labels:
            if phase == "N":
                # Don't pick noise
                continue

            pick_threshold = argdict.get(f"{phase}_threshold", 0.3)
            for trace in annotations.select(channel=f"PhaseNet_{phase}"):
                trace_id = f"{trace.stats.network}.{trace.stats.station}.{trace.stats.location}"
                triggers = trigger_onset(trace.data, pick_threshold, pick_threshold / 2)
                times = trace.times()
                for s0, _ in triggers:
                    t0 = trace.stats.starttime + times[s0]
                    picks.append((trace_id, t0, phase))

        return sorted(picks)
