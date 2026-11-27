from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Tuple

from .node_spec import CID
from .tree import CIDTree


@dataclass(frozen=True)
class WorkerCIDConverters:
    worker_id_to_cid_map: Dict[str, str]
    cid_to_worker_id_map: Dict[str, str]

    def worker_id_to_cid(self, worker_id: str) -> str | None:
        key = str(worker_id or "").strip()
        if not key:
            return None
        return self.worker_id_to_cid_map.get(key)

    def cid_to_worker_id(self, cid: str | CID) -> str | None:
        if isinstance(cid, CID):
            key = cid.to_str()
        else:
            key = str(cid or "").strip()
        if not key:
            return None
        return self.cid_to_worker_id_map.get(key)

    def as_functions(self) -> Tuple[Callable[[str], str | None], Callable[[str | CID], str | None]]:
        return self.worker_id_to_cid, self.cid_to_worker_id


def build_worker_cid_converters(cid_tree: CIDTree) -> WorkerCIDConverters:
    worker_id_to_cid: Dict[str, str] = {}
    cid_to_worker_id: Dict[str, str] = {}

    for spec in cid_tree.all():
        worker_id = str(spec.worker_id or "").strip()
        if not worker_id:
            continue
        cid = spec.cid.to_str()
        if not cid:
            continue
        existing_cid = worker_id_to_cid.get(worker_id)
        if existing_cid is not None and existing_cid != cid:
            raise ValueError(f"Duplicate worker_id '{worker_id}' for CIDs '{existing_cid}' and '{cid}'")
        worker_id_to_cid[worker_id] = cid
        cid_to_worker_id[cid] = worker_id

    return WorkerCIDConverters(
        worker_id_to_cid_map=worker_id_to_cid,
        cid_to_worker_id_map=cid_to_worker_id,
    )


def build_worker_cid_converters_from_yaml_text(preset_yaml: str) -> WorkerCIDConverters:
    return build_worker_cid_converters(CIDTree.from_yaml(str(preset_yaml or "")))


def build_worker_cid_converters_from_yaml_file(path: str | Path) -> WorkerCIDConverters:
    preset_path = Path(path)
    return build_worker_cid_converters_from_yaml_text(preset_path.read_text(encoding="utf-8"))


def load_worker_cid_converter_functions_from_yaml_file(
    path: str | Path,
) -> Tuple[Callable[[str], str | None], Callable[[str | CID], str | None]]:
    return build_worker_cid_converters_from_yaml_file(path).as_functions()
