"""Record rare native import errors without changing their failure semantics."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from uuid import uuid4


def _receipt(exc: OSError) -> None:
    if exc.errno == 2:
        return
    try:
        root = Path(os.environ.get('RESEARCH_INTEL_HOME', Path.cwd())).resolve()
        directory = root / 'data/diagnostics'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        entry = {
            'errno': exc.errno,
            'source': str(getattr(exc, 'filename', '') or ''),
            'pid': os.getpid(),
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'cwd': str(Path.cwd()),
            'run_id': os.environ.get('RESEARCH_INTEL_RUN_ID') or uuid4().hex,
        }
        with (directory / 'import-errors.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
    except OSError:
        pass


def main() -> int:
    try:
        from agent_research_intelligence.cli.main import main as command_main
        return command_main()
    except OSError as exc:
        _receipt(exc)
        raise


if __name__ == '__main__':
    sys.exit(main())
