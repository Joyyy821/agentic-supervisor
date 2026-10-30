"""Detached supervisor worker process entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .runtime import Runtime, WorkerAlreadyRunning, run_worker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic-supervisor-worker")
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("run_id")
    args = parser.parse_args(argv)
    root = Path(args.runtime_root).expanduser().resolve()
    try:
        asyncio.run(run_worker(root, args.run_id))
        return 0
    except WorkerAlreadyRunning as exc:
        print(f"supervisor worker was not started: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        try:
            runtime = Runtime(root)
            runtime.store.update_run(args.run_id, status="recovering", last_error=str(exc))
            runtime.store.add_event(args.run_id, "supervisor/worker_failed", {"error": str(exc)})
        except Exception:
            pass
        print(f"supervisor worker failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
