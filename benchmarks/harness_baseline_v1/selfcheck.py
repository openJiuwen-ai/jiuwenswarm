"""Model-free positive and negative acceptance checks for the benchmark."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    assert (ROOT / 'build_cases.py').is_file(), 'benchmark case generator is not implemented yet'
    assert (ROOT / 'grader.py').is_file(), 'deterministic benchmark grader is not implemented yet'
    from contract_checks import run_checks
    run_checks(ROOT)


if __name__ == '__main__':
    main()
