from typing import List
import numpy as np
from torch import nn
from torch.nn import functional as F
import torch
from lightly.models.modules.heads import VICRegProjectionHead


def build_mlp(layers_dims: List[int]):
    layers = []
    for i in range(len(layers_dims) - 2):
        layers.append(nn.Linear(layers_dims[i], layers_dims[i + 1]))
        layers.append(nn.BatchNorm1d(layers_dims[i + 1]))
        layers.append(nn.ReLU(True))
    layers.append(nn.Linear(layers_dims[-2], layers_dims[-1]))
    return nn.Sequential(*layers)


class MockModel(torch.nn.Module):
    """
    Does nothing. Just for testing.
    """

    def __init__(self, device="cuda", bs=64, n_steps=17, output_dim=256):
        super().__init__()
        self.device = device
        self.bs = bs
        self.n_steps = n_steps
        self.repr_dim = 256

    def forward(self, states, actions):
        """
        Args:
            During training:
                states: [B, T, Ch, H, W]
            During inference:
                states: [B, 1, Ch, H, W]
            actions: [B, T-1, 2]

        Output:
            predictions: [B, T, D]
        """
        return torch.randn((self.bs, self.n_steps, self.repr_dim)).to(self.device)


class Prober(torch.nn.Module):
    def __init__(
        self,
        embedding: int,
        arch: str,
        output_shape: List[int],
    ):
        super().__init__()
        self.output_dim = np.prod(output_shape)
        self.output_shape = output_shape
        self.arch = arch

        arch_list = list(map(int, arch.split("-"))) if arch != "" else []
        f = [embedding] + arch_list + [self.output_dim]
        layers = []
        for i in range(len(f) - 2):
            layers.append(torch.nn.Linear(f[i], f[i + 1]))
            layers.append(torch.nn.ReLU(True))
        layers.append(torch.nn.Linear(f[-2], f[-1]))
        self.prober = torch.nn.Sequential(*layers)

    def forward(self, e):
        output = self.prober(e)
        return output


class Encoder(nn.Module):
    def __init__(self, backbone):
        super().__init__()
        self.backbone = backbone
        self.projection_head = VICRegProjectionHead(
            input_dim=512,
            hidden_dim=1024,
            output_dim=1024,
            num_layers=3,
        )

    def forward(self, x):
        x = self.backbone(x).flatten(start_dim=1)
        z = self.projection_head(x)
        return x, z
    

class SimpleCNN(nn.Module):
    def __init__(self, embed_size, input_channel=3):
        super().__init__()
        self.conv1 = nn.Conv2d(input_channel, 12, padding=1, kernel_size=3)
        self.conv2 = nn.Conv2d(12, 12, padding=1, kernel_size=3)
        self.conv3 = nn.Conv2d(12, 12, padding=1, kernel_size=3)
        self.bn1 = nn.BatchNorm2d(12)
        self.bn2 = nn.BatchNorm2d(12)
        self.bn3 = nn.BatchNorm2d(12)
        self.relu = nn.ReLU()
        self.pool1 = nn.MaxPool2d((5, 5), stride=2)
        self.pool2 = nn.MaxPool2d((5, 5), stride=5)
        # h -> (5, 5, stride=1) -> (3, 3)
        # h = 65 -> 8748
        self.fc1 = nn.Linear(432, 4096)
        self.fc2 = nn.Linear(4096, embed_size)

    def forward(self, x):
        # h,w = 65
        x = self.conv1(x)        
        x = self.bn1(x)
        x = self.relu(x)
        x1 = x

        x2 = self.conv2(x1)
        x2 = self.bn2(x2)
        x2 = self.relu(x2)
        x2 = x2 + x1
        x2 = self.pool1(x2)
        # h,w = 31 

        x3 = self.conv3(x2)
        x3 = self.bn3(x3)
        x3 = self.relu(x3)
        x3 = x3 + x2
        x3 = self.pool2(x3)
        # h,w = 6

        x3 = x3.view(x3.size(0), -1)
        # (b,12*6*6)
        x3 = self.fc1(x3)
        x3 = self.relu(x3)
        x3 = self.fc2(x3)
        return x3


class Predictor(nn.Module):
    def __init__(self, input_size, hidden_size) -> None:
        super().__init__()
        self.input_size = input_size
        self.hidden_size = hidden_size
        self.lstm_cell = nn.LSTMCell(input_size, hidden_size)

        self.h = None
        self.c = None

    def set_hc(self, h, c):
        self.h = h
        self.c = c 
    
    def reset_hc(self):
        self.h = self.h.zero_() 
        self.c = self.c.zero_()

    def forward(self, action):
        self.h, self.c = self.lstm_cell(action, (self.h, self.c))
        return self.h
    

# Combine encoder and predictor into a single model-like callable
class JEPAModel(nn.Module):
    def __init__(self, encoder, predictor, repr_dim=1024):
        super().__init__()
        self.encoder = encoder
        self.predictor = predictor
        self.repr_dim = repr_dim
    
    def set_predictor(self, o, co, use_expander=False):
        x, z = self.encoder.forward(o)
        so = z if use_expander else x
        self.predictor.set_hc(so, co)
        return so

    def reset_predictor(self):
        self.predictor.reset_hc()
    
    def forward(self, action=None, state=None):
        sy_hat, sy = None, None
        if action is not None:
            sy_hat = self.predictor(action)
        if state is not None:
            sy = self.encoder(state)

        return sy_hat, sy
    
    def forward_inference(self, actions, states):
        B, L, D = states.shape[0], actions.shape[1], self.predictor.hidden_size

        o = states[:, 0, :, :, :]
        co = torch.zeros((B, D)).to(o.device)
        self.set_predictor(o, co, use_expander=True)

        result = torch.empty((B, L + 1, D))
        result[:, 0, :] = (self.predictor.h)
        for i in range(1, L):
            sy_hat, _ = self.forward(actions[:, i, :], states[:, i+1, :, :, :])
            result[:, i, :] = sy_hat

        return result