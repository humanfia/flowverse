"""A candidate kernel for the remote benchmark example."""

import triton
import triton.language as tl


@triton.jit
def add(x, y, output, size: tl.constexpr, BLOCK: tl.constexpr):
    index = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    mask = index < size
    tl.store(output + index, tl.load(x + index, mask) + tl.load(y + index, mask), mask)
