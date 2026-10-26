from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from pathlib import Path


APP_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = Path(os.getenv("ERL_SKILL_ROOT", APP_ROOT / "evidence-research-loop")).resolve()
CONTROLLER = SKILL_ROOT / "scripts" / "evolution_controller.py"
SCIENTIST_RUNTIME = SKILL_ROOT / "scripts" / "scientist_runtime.py"
WORKER_COMMANDS = {
    "start": (CONTROLLER, "start"),
    "resume": (CONTROLLER, "resume"),
    "scientist-start": (SCIENTIST_RUNTIME, "start"),
    "scientist-resume": (SCIENTIST_RUNTIME, "resume"),
}


def atomic_json(path: Path, value: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)


def main() -> int:
    if len(sys.argv) != 4 or sys.argv[1] not in WORKER_COMMANDS:
        raise SystemExit(
            "usage: worker.py <start|resume|scientist-start|scientist-resume> "
            "<project> <result.json>"
        )
    mode = sys.argv[1]
    project = Path(sys.argv[2]).resolve()
    result_path = Path(sys.argv[3]).resolve()
    started = time.time()
    output: dict = {"mode": mode, "started_at_unix": started}
    try:
        executable, runtime_mode = WORKER_COMMANDS[mode]
        command = [sys.executable, str(executable), runtime_mode, str(project)]
        print(json.dumps({"event": "worker_started", "mode": mode, "project": project.name}), flush=True)
        result = subprocess.run(command, cwd=project, text=True, encoding="utf-8", errors="replace")
        output.update({"exit_code": result.returncode, "finished_at_unix": time.time(), "elapsed_sec": time.time() - started})
        print(json.dumps({"event": "worker_finished", **output}), flush=True)
    except BaseException as exc:
        output.update({
            "exit_code": None,
            "finished_at_unix": time.time(),
            "elapsed_sec": time.time() - started,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        print(json.dumps({"event": "worker_failed", "error": output["error"]}), flush=True)
    atomic_json(result_path, output)
    return 0 if output.get("exit_code") == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

