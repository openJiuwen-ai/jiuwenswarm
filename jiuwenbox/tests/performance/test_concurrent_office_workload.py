"""Performance tests for concurrent jiuwenbox office-style workloads."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import statistics
import time
from dataclasses import dataclass

import pytest

pytestmark = [pytest.mark.performance, pytest.mark.slow, pytest.mark.asyncio]

logger = logging.getLogger(__name__)

OFFICE_EDIT_SCRIPT = """
import hashlib
import json
import sys
from pathlib import Path

doc_path = Path(sys.argv[1])
report_path = Path(sys.argv[2])
task_id = sys.argv[3]

content = doc_path.read_text()
base_content = content.split("\\n## Meeting Notes\\n", 1)[0]
score = sum((index + ord(task_id[0])) * index for index in range(1800000))
edited = (
    base_content
    + "\\n## Meeting Notes\\n"
    + f"- task: {task_id}\\n"
    + f"- score: {score}\\n"
)
doc_path.write_text(edited)
report_path.write_text(json.dumps({
    "task_id": task_id,
    "bytes": len(edited.encode()),
    "sha256": hashlib.sha256(edited.encode()).hexdigest(),
}))
print(hashlib.sha256(edited.encode()).hexdigest())
""".strip()


@dataclass(frozen=True)
class PerfSample:
    task_id: int
    total_ms: float
    upload_ms: float
    exec_ms: float
    list_ms: float
    download_ms: float


@dataclass(frozen=True)
class OfficeEditCall:
    """Per-task inputs for ``_execute_office_edit``.

    Bundled into a value object so the helper signature stays at or below
    five arguments (G.FNM.03) without losing readability at the call site.
    """

    sandbox_id: str
    doc_path: str
    report_path: str
    task_id: int
    timeout_seconds: int


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = (len(ordered) - 1) * percentile
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _record_summary(
    name: str,
    samples: list[PerfSample],
    elapsed_s: float,
    *,
    sandbox_count: int,
    concurrency: int,
    loop: int,
    setup_s: float,
    workload_s: float,
) -> None:
    totals = [sample.total_ms for sample in samples]
    summary = {
        "operations": len(samples),
        "sandbox_count": sandbox_count,
        "concurrency": concurrency,
        "loop": loop,
        "elapsed_s": round(elapsed_s, 3),
        "setup_s": round(setup_s, 3),
        "workload_s": round(workload_s, 3),
        "throughput_ops_per_s": round(len(samples) / elapsed_s, 3) if elapsed_s else 0,
        "workload_ops_per_s": round(len(samples) / workload_s, 3) if workload_s else 0,
        "avg_ms": round(statistics.fmean(totals), 3),
        "p50_ms": round(_percentile(totals, 0.50), 3),
        "p95_ms": round(_percentile(totals, 0.95), 3),
        "max_ms": round(max(totals), 3),
        "avg_upload_ms": round(statistics.fmean(sample.upload_ms for sample in samples), 3),
        "avg_exec_ms": round(statistics.fmean(sample.exec_ms for sample in samples), 3),
        "avg_list_ms": round(statistics.fmean(sample.list_ms for sample in samples), 3),
        "avg_download_ms": round(statistics.fmean(sample.download_ms for sample in samples), 3),
    }
    summary_text = (
        f"[jiuwenbox-perf] {name}\n"
        f"  sandbox_count={summary['sandbox_count']}  concurrency={summary['concurrency']}  "
        f"loop={summary['loop']}  operations={summary['operations']}\n"
        f"  elapsed={summary['elapsed_s']}s  setup={summary['setup_s']}s  "
        f"workload={summary['workload_s']}s  "
        f"throughput={summary['throughput_ops_per_s']} ops/s  "
        f"workload_throughput={summary['workload_ops_per_s']} ops/s\n"
        f"  total_ms: avg={summary['avg_ms']}  p50={summary['p50_ms']}  "
        f"p95={summary['p95_ms']}  max={summary['max_ms']}\n"
        f"  stage_avg_ms: upload={summary['avg_upload_ms']}  exec={summary['avg_exec_ms']}  "
        f"list={summary['avg_list_ms']}  download={summary['avg_download_ms']}"
    )
    logger.info("%s", summary_text)


async def _create_ready_sandbox(client) -> dict:
    response = await client.post("/api/v1/sandboxes", json={})
    assert response.status_code == 201, response.text
    sandbox = response.json()
    assert sandbox["phase"] == "ready", sandbox
    return sandbox


def _office_source(task_id: int) -> bytes:
    return (
        f"# Weekly Brief {task_id}\\n\\n"
        "Project update: review pull requests, refresh planning notes, "
        "and prepare the customer-facing summary.\\n"
        "Action items:\\n"
        "- reconcile spreadsheet changes\\n"
        "- summarize design discussion\\n"
        "- prepare next sprint checklist\\n"
    ).encode() * 100


async def _upload_office_document(client, sandbox_id: str, doc_path: str, source: bytes) -> float:
    upload_started = time.perf_counter()
    upload = await client.post(
        f"/api/v1/sandboxes/{sandbox_id}/upload",
        params={"sandbox_path": doc_path},
        files={"file": ("weekly-brief.md", source, "text/markdown")},
    )
    upload_ms = (time.perf_counter() - upload_started) * 1000
    assert upload.status_code == 204, upload.text
    return upload_ms


async def _execute_office_edit(
    client, call: OfficeEditCall,
) -> tuple[float, str]:
    exec_started = time.perf_counter()
    execution = await client.post(
        f"/api/v1/sandboxes/{call.sandbox_id}/exec",
        json={
            "command": [
                "python3",
                "-c",
                OFFICE_EDIT_SCRIPT,
                call.doc_path,
                call.report_path,
                str(call.task_id),
            ],
            "timeout_seconds": call.timeout_seconds,
        },
    )
    exec_ms = (time.perf_counter() - exec_started) * 1000
    assert execution.status_code == 200, execution.text
    exec_data = execution.json()
    assert exec_data["exit_code"] == 0, exec_data
    expected_digest = exec_data["stdout"].strip()
    return exec_ms, expected_digest


async def _list_office_outputs(
    client,
    sandbox_id: str,
    task_dir: str,
    doc_path: str,
    report_path: str,
) -> float:
    list_started = time.perf_counter()
    listing = await client.get(
        f"/api/v1/sandboxes/{sandbox_id}/files",
        params={"sandbox_path": task_dir, "recursive": True},
    )
    list_ms = (time.perf_counter() - list_started) * 1000
    assert listing.status_code == 200, listing.text
    paths = {item["path"] for item in listing.json()["items"]}
    assert doc_path in paths
    assert report_path in paths
    return list_ms


async def _download_office_document(
    client,
    sandbox_id: str,
    doc_path: str,
    expected_digest: str,
) -> float:
    download_started = time.perf_counter()
    download = await client.get(
        f"/api/v1/sandboxes/{sandbox_id}/download",
        params={"sandbox_path": doc_path},
    )
    download_ms = (time.perf_counter() - download_started) * 1000
    assert download.status_code == 200, download.text
    assert b"## Meeting Notes" in download.content
    assert hashlib.sha256(download.content).hexdigest() == expected_digest
    return download_ms


async def _run_office_task(
    client,
    sandbox_id: str,
    task_id: int,
    exec_timeout_seconds: int,
) -> PerfSample:
    task_dir = f"/tmp/jiuwenbox-perf/task-{task_id}"
    doc_path = f"{task_dir}/weekly-brief.md"
    report_path = f"{task_dir}/analysis.json"
    source = _office_source(task_id)

    started = time.perf_counter()
    upload_ms = await _upload_office_document(client, sandbox_id, doc_path, source)
    exec_ms, expected_digest = await _execute_office_edit(
        client,
        OfficeEditCall(
            sandbox_id=sandbox_id,
            doc_path=doc_path,
            report_path=report_path,
            task_id=task_id,
            timeout_seconds=exec_timeout_seconds,
        ),
    )
    list_ms = await _list_office_outputs(client, sandbox_id, task_dir, doc_path, report_path)
    download_ms = await _download_office_document(
        client,
        sandbox_id,
        doc_path,
        expected_digest,
    )

    total_ms = (time.perf_counter() - started) * 1000
    return PerfSample(
        task_id=task_id,
        total_ms=total_ms,
        upload_ms=upload_ms,
        exec_ms=exec_ms,
        list_ms=list_ms,
        download_ms=download_ms,
    )


async def test_concurrent_office_workload(
    perf_client,
    perf_sandbox_count,
    perf_concurrency,
    perf_loop,
    perf_exec_timeout_seconds,
):
    started = time.perf_counter()
    sandboxes = await asyncio.gather(
        *(_create_ready_sandbox(perf_client) for _ in range(perf_sandbox_count)),
    )
    setup_s = time.perf_counter() - started

    async def run_worker(
        sandbox_index: int,
        sandbox_id: str,
        worker_index: int,
    ) -> list[PerfSample]:
        samples = []
        base_task_id = sandbox_index * perf_concurrency * perf_loop + worker_index * perf_loop
        for loop_index in range(perf_loop):
            samples.append(
                await _run_office_task(
                    perf_client,
                    sandbox_id,
                    base_task_id + loop_index,
                    perf_exec_timeout_seconds,
                )
            )
        return samples

    try:
        workload_started = time.perf_counter()
        worker_samples = await asyncio.gather(
            *(
                run_worker(sandbox_index, sandbox["id"], worker_index)
                for sandbox_index, sandbox in enumerate(sandboxes)
                for worker_index in range(perf_concurrency)
            ),
        )
        workload_s = time.perf_counter() - workload_started
        elapsed_s = time.perf_counter() - started
    finally:
        await asyncio.gather(
            *(perf_client.delete(f"/api/v1/sandboxes/{sandbox['id']}") for sandbox in sandboxes),
            return_exceptions=True,
        )

    samples = [sample for worker_result in worker_samples for sample in worker_result]
    assert len(samples) == perf_sandbox_count * perf_concurrency * perf_loop
    _record_summary(
        "concurrent_office_workload",
        samples,
        elapsed_s,
        sandbox_count=perf_sandbox_count,
        concurrency=perf_concurrency,
        loop=perf_loop,
        setup_s=setup_s,
        workload_s=workload_s,
    )
