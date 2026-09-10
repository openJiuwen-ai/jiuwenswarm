"""FAISS-backed prompt memory that reuses ``symphony.experience.ExperienceBank``.

The bank provides the vector index (embeddings + FAISS + integrity manifest); we
keep the full :class:`PromptRecord` payloads in a sidecar JSONL keyed by the bank's
item id, because ``ExperienceItem`` is skill-shaped and we don't want to overload
its semantics. This is the "integrate with the existing memory system" path from
the design.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from openjiuwen.dev_tools.tune.optimizer.prompt_search.memory import PromptMemory
from openjiuwen.dev_tools.tune.optimizer.prompt_search.models import PromptRecord, PromptTaskSpec

LOGGER = logging.getLogger(__name__)

_SIDECAR = "prompt_records.jsonl"


class ExperienceBankPromptMemory(PromptMemory):
    """Semantic prompt memory backed by an ``ExperienceBank`` vector index."""

    def __init__(self, bank: Any, directory: str | Path, *, threshold: float = 0.55) -> None:
        self._bank = bank
        self._dir = Path(directory)
        self._sidecar = self._dir / _SIDECAR
        self._threshold = threshold
        self._lock = threading.Lock()
        self._records: dict[str, PromptRecord] = {}  # item_id -> record
        self._load_sidecar()

    def _load_sidecar(self) -> None:
        if not self._sidecar.is_file():
            return
        try:
            for line in self._sidecar.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                item_id = str(data.get("item_id", ""))
                if item_id:
                    self._records[item_id] = PromptRecord.from_dict(data.get("record", {}))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("ExperienceBankPromptMemory: failed to load sidecar: %s", exc)

    def add(self, record: PromptRecord) -> None:
        text = record.task_characteristics or record.objective
        with self._lock:
            try:
                item = self._bank.create_item(
                    query_pattern=text,
                    query_examples=[record.objective],
                    skill_ids=[record.record_id],
                    success_count=1,
                )
                self._bank.add(item)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("ExperienceBankPromptMemory: bank add failed: %s", exc)
                return
            self._records[item.id] = record
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._sidecar.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {"item_id": item.id, "record": record.to_dict()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def search_similar(self, task: PromptTaskSpec, top_k: int = 3) -> list[PromptRecord]:
        try:
            results = self._bank.search_by_embedding(
                task.characteristics, top_k=top_k, threshold=self._threshold
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("ExperienceBankPromptMemory: search failed: %s", exc)
            return []
        records: list[PromptRecord] = []
        for _score, item in results:
            record = self._records.get(item.id)
            if record is not None:
                records.append(record)
        return records

    def best_for_objective(self, objective: str) -> PromptRecord | None:
        matches = [r for r in self._records.values() if r.objective == objective]
        return max(matches, key=lambda r: r.reward, default=None)

    def pending(self, threshold: float = 0.0) -> list[PromptRecord]:
        candidates = [
            r for r in self._records.values() if not r.applied and r.gain > threshold
        ]
        candidates.sort(key=lambda r: r.gain, reverse=True)
        return candidates

    def mark_applied(self, record_id: str) -> bool:
        with self._lock:
            target_item_id = None
            for item_id, record in self._records.items():
                if record.record_id == record_id:
                    record.applied = True
                    record.applied_at = time.time()
                    target_item_id = item_id
                    break
            if target_item_id is None:
                return False
            self._rewrite_sidecar()
            return True

    def _rewrite_sidecar(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp_path = self._sidecar.with_suffix(self._sidecar.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for item_id, record in self._records.items():
                handle.write(
                    json.dumps(
                        {"item_id": item_id, "record": record.to_dict()},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self._sidecar)


__all__ = ["ExperienceBankPromptMemory"]
