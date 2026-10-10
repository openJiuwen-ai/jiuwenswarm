"""Formal publication compiler: Tectonic only, with no preview fallback."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _tectonic_path() -> Path:
    configured = os.environ.get("JIUWENSWARM_TECTONIC_PATH", "").strip()
    candidate = Path(configured) if configured else Path(__file__).resolve().parent.parent / "tools" / "tectonic" / "tectonic.exe"
    if not candidate.is_file():
        raise FileNotFoundError(f"Tectonic executable is unavailable: {candidate}")
    return candidate


def compile_paper(full_tex: str, output_dir: str, jobname: str = "paper", conference: str = "iclr2024") -> dict:
    """Materialize official assets and compile a formal PDF with Tectonic.

    A PDF is returned only when TeX compilation succeeds. ReportLab/fpdf2 and
    system-engine fallbacks are deliberately excluded from the publication path.
    """
    from scripts.template_registry import materialize_assets, resolve_template

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    materialize_assets(resolve_template(conference), out)
    tex_path = out / f"{jobname}.tex"
    tex_path.write_text(full_tex, encoding="utf-8")
    log_path = out / f"{jobname}.tectonic.log"
    cache = Path(os.environ.get("JIUWENSWARM_TECTONIC_CACHE_DIR", str(Path(__file__).resolve().parent.parent / "tools" / "tectonic" / "cache")))
    cache.mkdir(parents=True, exist_ok=True)
    try:
        completed = subprocess.run(
            [str(_tectonic_path()), "--keep-logs", "--outdir", str(out.resolve()), tex_path.name],
            cwd=out,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
            env={**os.environ, "TECTONIC_CACHE_DIR": str(cache)},
            check=True,
        )
        log_path.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        stdout = getattr(exc, "stdout", "") or ""
        stderr = getattr(exc, "stderr", "") or ""
        log_path.write_text(str(stdout) + str(stderr) + "\n" + str(exc), encoding="utf-8")
        return {"pdf_path": None, "tex_path": str(tex_path), "status": "failed", "error": f"Tectonic compilation failed: {exc}", "log_path": str(log_path)}
    pdf_path = out / f"{jobname}.pdf"
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 5000:
        return {"pdf_path": None, "tex_path": str(tex_path), "status": "failed", "error": "Tectonic finished without a valid PDF", "log_path": str(log_path)}
    return {"pdf_path": str(pdf_path.resolve()), "tex_path": str(tex_path), "status": "success", "error": "", "log_path": str(log_path)}
