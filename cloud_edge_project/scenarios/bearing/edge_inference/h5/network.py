"""Frozen H5 three-branch architecture used by the edge runtime."""

import torch
import torch.nn as nn


class PhysicalFusionModel(nn.Module):
    """CNN, physical-feature, and operating-condition fusion network."""

    def __init__(
        self,
        num_classes: int = 3,
        phys_dim: int = 19,
        cond_dim: int = 13,
        cond_scale: float = 0.25,
        cond_dropout: float = 0.5,
        use_h4_cnn: bool = False,
    ) -> None:
        super().__init__()
        self.cond_scale = cond_scale
        self.cond_dropout = cond_dropout
        self.use_h4_cnn = use_h4_cnn
        if use_h4_cnn:
            self.cnn = nn.Sequential(
                nn.Conv1d(1, 16, kernel_size=15, stride=4, padding=7),
                nn.BatchNorm1d(16), nn.GELU(),
                nn.Conv1d(16, 32, kernel_size=7, stride=4, padding=3),
                nn.BatchNorm1d(32), nn.GELU(),
                nn.Conv1d(32, 64, kernel_size=5, stride=2, padding=2),
                nn.BatchNorm1d(64), nn.GELU(), nn.AdaptiveAvgPool1d(1),
            )
            self.vibration_dim = 64
        else:
            self.cnn = nn.Sequential(
                nn.Conv1d(1, 32, kernel_size=3, padding=1),
                nn.BatchNorm1d(32), nn.ReLU(), nn.MaxPool1d(2, 2),
                nn.Conv1d(32, 64, kernel_size=3, padding=1),
                nn.BatchNorm1d(64), nn.ReLU(), nn.MaxPool1d(2, 2),
                nn.Conv1d(64, 128, kernel_size=3, padding=1),
                nn.BatchNorm1d(128), nn.ReLU(), nn.AdaptiveAvgPool1d(1),
            )
            self.vibration_dim = 128
        self.phys_encoder = nn.Sequential(
            nn.Linear(phys_dim, 32), nn.LayerNorm(32), nn.GELU(), nn.Dropout(0.1)
        )
        self.phys_dim = 32
        self.condition_encoder = nn.Sequential(
            nn.Linear(cond_dim, 16), nn.LayerNorm(16), nn.GELU()
        )
        self.condition_dim = 16
        self.classifier = nn.Sequential(
            nn.Linear(self.vibration_dim + self.phys_dim + self.condition_dim, 64),
            nn.GELU(), nn.Dropout(0.2), nn.Linear(64, num_classes),
        )

    def forward(
        self, vibration: torch.Tensor, physical_features: torch.Tensor, condition: torch.Tensor
    ) -> tuple[torch.Tensor, torch.Tensor]:
        vib_feat = self.cnn(vibration.unsqueeze(1)).squeeze(-1)
        phys_feat = self.phys_encoder(physical_features)
        cond_feat = self.condition_encoder(condition) * self.cond_scale
        if self.training and self.cond_dropout > 0:
            mask = torch.rand(cond_feat.size(0), 1, device=cond_feat.device) > self.cond_dropout
            cond_feat = cond_feat * mask
        fused = torch.cat([vib_feat, phys_feat, cond_feat], dim=1)
        return self.classifier(fused), fused


__all__ = ["PhysicalFusionModel"]
