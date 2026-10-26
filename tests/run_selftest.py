"""Run core and MCP regressions with isolated workspace-local fixtures."""
from pathlib import Path
import runpy

if __name__ == '__main__':
    runpy.run_path(str(Path(__file__).resolve().parents[1] / 'review-probes' / 'selftest_20261026.py'), run_name='__main__')
