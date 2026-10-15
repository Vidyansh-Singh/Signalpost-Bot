"""Single entry point: works on Windows, macOS and Linux without setting PYTHONPATH.

    python run.py run --organisations smoke-companies.jsonl --output out/smoke.jsonl --report out/smoke-report.json --run-id smoke-001 --expected-count 100 --site site
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from signalpost.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
