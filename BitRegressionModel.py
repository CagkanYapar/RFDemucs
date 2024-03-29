#Adaptation of DEMUCS for "Bit Regression"
#Adapted from https://github.com/facebookresearch/denoiser/

import torch
import torch.nn as nn
import torch.nn.functional as F
import math

torch.autograd.set_detect_anomaly(True)

import numpy as np
from util import weight_scaling_init


import time

import torch as th
from torch import nn
from torch.nn import functional as F

from .resample import downsample2, upsample2
from .utils import capture_init

class BLSTM(nn.Module):
    def __init__(self, dim, layers=2, bi=True):
        super().__init__()
        klass = nn.LSTM
        self.lstm = klass(bidirectional=bi, num_layers=layers, hidden_size=dim, input_size=dim)
        self.linear = None
        if bi:
            self.linear = nn.Linear(2 * dim, dim)

    def forward(self, x, hidden=None):
        x, hidden = self.lstm(x, hidden)
        if self.linear:
            x = self.linear(x)
        return x, hidden


def rescale_conv(conv, reference):
    std = conv.weight.std().detach()
    scale = (std / reference)**0.5
    conv.weight.data /= scale
    if conv.bias is not None:
        conv.bias.data /= scale


def rescale_module(module, reference):
    for sub in module.modules():
        if isinstance(sub, (nn.Conv1d, nn.ConvTranspose1d)):
            rescale_conv(sub, reference)


class Demucs(nn.Module):
    """
    Demucs speech enhancement model.
    Args:
        - chin (int): number of input channels.
        - chout (int): number of output channels.
        - hidden (int): number of initial hidden channels.
        - depth (int): number of layers.
        - kernel_size (int): kernel size for each layer.
        - stride (int): stride for each layer.
        - causal (bool): if false, uses BiLSTM instead of LSTM.
        - resample (int): amount of resampling to apply to the input/output.
            Can be one of 1, 2 or 4.
        - growth (float): number of channels is multiplied by this for every layer.
        - max_hidden (int): maximum number of channels. Can be useful to
            control the size/speed of the model.
        - normalize (bool): if true, normalize the input.
        - glu (bool): if true uses GLU instead of ReLU in 1x1 convolutions.
        - rescale (float): controls custom weight initialization.
            See https://arxiv.org/abs/1911.13254.
        - floor (float): stability flooring when normalizing.
        - BIT REGRESSION ADAPTATION, n_bits (int): number of message bits 
        - BIT REGRESSION ADAPTATION, kernel_out (int): size of the output sequence to estimate the bits 
        - sample_rate (float): sample_rate used for training the model.

    """
    @capture_init
    def __init__(self,
                 chin=2,
                 chout=2,
                 hidden=64,
                 depth=5,
                 kernel_size=8,
                 stride=2,
                 causal=False,
                 resample=2,
                 growth=2,
                 max_hidden=10_000,
                 normalize=False,
                 glu=True,
                 rescale=0.1,
                 floor=1e-3,
                 n_bits = 5120, 
                 kernel_out = 64,
                 sample_rate=16_000):

        
        super(Demucs, self).__init__()
        
        if resample not in [1, 2, 4]:
            raise ValueError("Resample should be 1, 2 or 4.")

        self.chin = chin
        self.chout = chout
        self.hidden = hidden
        self.depth = depth
        self.kernel_size = kernel_size
        self.stride = stride
        self.causal = causal
        self.floor = floor
        self.resample = resample
        self.normalize = normalize
        
        self.n_bits=n_bits
        self.kernel_out=kernel_out
        
        
        self.sample_rate = sample_rate

        self.encoder = nn.ModuleList()
        self.decoder = nn.ModuleList()
        activation = nn.GLU(1) if glu else nn.ReLU()
        ch_scale = 2 if glu else 1
        
        #BIT REGRESSION ADAPTATION
        ##########################
        factor = 40960//kernel_out
        self.output_bit = nn.Conv1d(self.chout, self.n_bits//factor, kernel_size=self.kernel_out, stride=self.kernel_out)
        ##########################
                     
        #BIT REGRESSION ADAPTATION v2 - 2 layer FCN
        ##########################
        factor = 40960//kernel_out
        self.output_bit1 = nn.Conv1d(self.chout, self.kernel_out, kernel_size=self.kernel_out, stride=self.kernel_out)
        self.output_bit2 = nn.Conv1d(1, self.n_bits//factor, kernel_size=self.kernel_out, stride=self.kernel_out)
        ##########################

        for index in range(depth):
            encode = []
            encode += [
                nn.Conv1d(chin, hidden, kernel_size, stride),
                nn.ReLU(),
                nn.Conv1d(hidden, hidden * ch_scale, 1), activation,
            ]
            self.encoder.append(nn.Sequential(*encode))

            decode = []
            decode += [
                nn.Conv1d(hidden, ch_scale * hidden, 1), activation,
                nn.ConvTranspose1d(hidden, chout, kernel_size, stride),
            ]
            if index > 0:
                decode.append(nn.ReLU())
            self.decoder.insert(0, nn.Sequential(*decode))
            chout = hidden
            chin = hidden
            hidden = min(int(growth * hidden), max_hidden)

        self.lstm = BLSTM(chin, bi=not causal)
        if rescale:
            rescale_module(self, reference=rescale)

    def valid_length(self, length):
        """
        Return the nearest valid length to use with the model so that
        there is no time steps left over in a convolutions, e.g. for all
        layers, size of the input - kernel_size % stride = 0.

        If the mixture has a valid length, the estimated sources
        will have exactly the same length.
        """
        length = math.ceil(length * self.resample)
        for idx in range(self.depth):
            length = math.ceil((length - self.kernel_size) / self.stride) + 1
            length = max(length, 1)
        for idx in range(self.depth):
            length = (length - 1) * self.stride + self.kernel_size
        length = int(math.ceil(length / self.resample))
        return int(length)

    @property
    def total_stride(self):
        return self.stride ** self.depth // self.resample

    def forward(self, mix):
        if mix.dim() == 2:
            mix = mix.unsqueeze(1)

        if self.normalize:
            mono = mix.mean(dim=1, keepdim=True)
            std = mono.std(dim=-1, keepdim=True)
            mix = mix / (self.floor + std)
        else:
            std = 1
        length = mix.shape[-1]
        x = mix
        x = F.pad(x, (0, self.valid_length(length) - length))
        if self.resample == 2:
            x = upsample2(x)
        elif self.resample == 4:
            x = upsample2(x)
            x = upsample2(x)
        skips = []
        for encode in self.encoder:
            x = encode(x)
            skips.append(x)
        x = x.permute(2, 0, 1)
        x, _ = self.lstm(x)
        x = x.permute(1, 2, 0)
        for decode in self.decoder:
            skip = skips.pop(-1)
            x = x + skip[..., :x.shape[-1]]
            x = decode(x)
        if self.resample == 2:
            x = downsample2(x)
        elif self.resample == 4:
            x = downsample2(x)
            x = downsample2(x)

        x = x[..., :length]
        
        #BIT REGRESSION ADAPTATION
        ##########################
        x = self.output_bit(x)
        flattened_x = torch.flatten(torch.transpose(x,1,2), start_dim=1) #B x C x H to B x CH as if dense layer on blocks of kernel_out
        x = flattened_x
        ##########################
        
        #BIT REGRESSION ADAPTATION v2 - 2 layer FCN
        ##########################
        x1 = self.output_bit1(x)
        flattened_x1 = torch.flatten(torch.transpose(x1,1,2), start_dim=1) 
        x_v2 = torch.unsqueeze(flattened_x1, 1)
        x_v2 = self.output_bit2(x_v2)
        flattened_x2 = torch.flatten(torch.transpose(x_v2,1,2), start_dim=1) 
        x_v2 = flattened_x2
        ##########################
        return std * x


