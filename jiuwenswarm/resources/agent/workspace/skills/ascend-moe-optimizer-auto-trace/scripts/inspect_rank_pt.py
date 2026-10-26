#!/usr/bin/env python3
"""Inspect rank*.pt produced by save_profiling_data: shapes, counters, sample trace IDs."""

import argparse
import logging
import os
import sys

import torch

logger = logging.getLogger(__name__)


def main():
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="Inspect profiling rank*.pt contents")
    p.add_argument("rank_pt", help="path to rank000.pt etc.")
    p.add_argument("--cores", type=int, default=8, help="max cores per type to print detail")
    args = p.parse_args()

    path = args.rank_pt
    if not os.path.isfile(path):
        logger.error("not found: %s", path)
        sys.exit(1)

    obj = torch.load(path, map_location="cpu")
    logger.info("file: %s", path)
    logger.info("loaded type: %s", type(obj).__name__)

    if isinstance(obj, (list, tuple)):
        logger.info("num groups (core types): %d", len(obj))
        for gi, t in enumerate(obj):
            if not isinstance(t, torch.Tensor):
                logger.info("  [%d] %s (skip)", gi, type(t))
                continue
            nz = int((t != 0).sum().item())
            logger.info(
                "  [%d] shape=%s dtype=%s nonzero=%d/%d (%.4f%%)",
                gi,
                tuple(t.shape),
                t.dtype,
                nz,
                t.numel(),
                100.0 * nz / max(1, t.numel()),
            )
            # Counters: trace_collector uses record_count = tensor[cid,0] - 1
            n_with_records = 0
            max_rc = 0
            for cid in range(t.shape[0]):
                rc = int(t[cid, 0].item())
                if rc > 1:
                    n_with_records += 1
                max_rc = max(max_rc, rc)
            logger.info(
                "      cores with counter>1 (likely has trace records): %d/%d, max(counter)=%s",
                n_with_records,
                t.shape[0],
                max_rc,
            )
            nprint = min(args.cores, t.shape[0])
            for cid in range(nprint):
                rc = int(t[cid, 0].item())
                last = int(t[cid, -1].item()) if t.shape[1] > 1 else 0
                # trace_collector: record_count = raw_count - 1
                nrec = max(0, rc - 1)
                s1 = int(t[cid, 1].item()) if t.shape[1] > 1 and rc > 1 else 0
                s2 = int(t[cid, 2].item()) if t.shape[1] > 2 and rc > 2 else 0
                logger.info(
                    "      core[%s] [0]=counter=%s => ~%s records, [-1]=ts=%s, [1]=%s, [2]=%s",
                    cid,
                    rc,
                    nrec,
                    last,
                    s1,
                    s2,
                )
        return

    if isinstance(obj, torch.Tensor):
        t = obj
        nz = int((t != 0).sum().item())
        logger.info("single tensor shape=%s nonzero=%d/%d", tuple(t.shape), nz, t.numel())
        return

    logger.info("unexpected payload: %s", repr(obj)[:500])


if __name__ == "__main__":
    main()
