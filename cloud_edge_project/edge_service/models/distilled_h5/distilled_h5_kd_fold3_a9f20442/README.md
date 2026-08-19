---
library_name: pytorch
tags:
  - predictive-maintenance
  - vibration-diagnosis
  - bearing-fault-diagnosis
---

# Distilled H5 Edge Model

This directory is the self-contained runtime package for the formal edge model.

`best_model.pt` is the distilled three-branch H5 checkpoint. Its SHA-256 must
match `checkpoint_sha256.txt`; the service verifies this before loading.

Runtime inputs are a 50 ms raw packet: 3,200 vibration samples at 64 kHz,
three 200-sample operating-condition channels at 4 kHz, and one temperature
value. The runner applies the training-time 4x polyphase downsampling only to
the CNN branch; physical features use the original vibration data.

Release mirror: [wanfengaodaliya/intelligent-maintenance-distilled-h5](https://huggingface.co/wanfengaodaliya/intelligent-maintenance-distilled-h5).

The project copy is the default runtime source. The public Hugging Face mirror
contains the same release package for recovery and teammate distribution.

Run the service from the repository root with `conda activate moment`, then
`python cloud_edge_project/edge_service/run_edge_service.py`.
