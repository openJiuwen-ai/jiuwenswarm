#!/usr/bin/env python3
"""加载原始 profiling tensor（.pt），调用 trace_utils 按核类型拆分并保存。"""

import argparse
import logging

import torch
from trace_utils import save_profiling_data


def main():
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Save raw profiling tensor to per-rank .pt files.")
    parser.add_argument("input", help="raw profiling tensor file (.pt)")
    parser.add_argument("--rank", type=int, default=0, help="rank ID")
    parser.add_argument("--output", default="profiling_data", help="output directory")
    parser.add_argument("--base-h", default=None, help="path to _base.h for macro reading")
    args = parser.parse_args()

    profiling_raw = torch.load(args.input, map_location="cpu")
    save_profiling_data(profiling_raw, args.rank, args.output, args.base_h)


if __name__ == "__main__":
    main()
