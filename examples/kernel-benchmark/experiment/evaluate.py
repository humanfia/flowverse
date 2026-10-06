"""Check correctness before timing; report device time separately from transport."""

import json
from pathlib import Path

import torch
import triton
from kernel import add

torch.manual_seed(0)
size = 2**20 + 17
x = torch.randn(size, device="cuda")
y = torch.randn_like(x)
output = torch.empty_like(x)


def launch():
    add[(triton.cdiv(size, 256),)](x, y, output, size, BLOCK=256)


launch()
torch.testing.assert_close(output, x + y)
for _ in range(25):
    launch()
start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
start.record()
for _ in range(100):
    launch()
end.record()
end.synchronize()
report = {
    "correct": True,
    "mean_us": start.elapsed_time(end) * 1000 / 100,
    "device": torch.cuda.get_device_name(),
    "torch": torch.__version__,
    "triton": triton.__version__,
    "elements": size,
}
Path("results").mkdir(exist_ok=True)
Path("results/report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report))
