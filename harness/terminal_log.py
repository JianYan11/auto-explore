"""Mirror CLI text output to a durable, per-run UTF-8 log."""

from __future__ import annotations

import sys
import traceback
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Iterable, Iterator, Optional, TextIO
from uuid import uuid4


class _TeeStream:
    """Keep terminal behavior while flushing every write to the shared log."""

    def __init__(self, terminal: TextIO, log: TextIO, lock):
        self._terminal = terminal
        self._log = log
        self._lock = lock

    def write(self, text: str) -> int:
        with self._lock:
            self._log.write(text)
            self._log.flush()
            written = self._terminal.write(text)
            self._terminal.flush()
            return written

    def writelines(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        with self._lock:
            self._log.flush()
            self._terminal.flush()

    def __getattr__(self, name: str):
        # Preserve encoding, isatty(), fileno(), etc. for terminal-aware callers.
        return getattr(self._terminal, name)


@contextmanager
def capture_terminal_output(
    output_dir: str, log_file: Optional[str] = None,
) -> Iterator[Path]:
    """Capture stdout/stderr and escaping tracebacks without changing exit behavior.

    Explicit paths append across invocations. Default paths are unique per run.
    This redirects Python text streams, not native subprocess file descriptors.
    """
    if log_file:
        path = Path(log_file).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = (Path(output_dir) / "logs" / f"console_{stamp}_{uuid4().hex}.log").resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", errors="backslashreplace") as log:
        lock = RLock()
        stdout = _TeeStream(sys.stdout, log, lock)
        stderr = _TeeStream(sys.stderr, log, lock)
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = "completed"
            try:
                print(f"\n[Log] Terminal output: {path}")
                print(f"[Log] Started: {datetime.now().astimezone().isoformat(timespec='seconds')}")
                yield path
            except SystemExit as exc:
                status = f"exit {exc.code}"
                raise
            except BaseException as exc:
                status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                # The interpreter prints the exception after streams are restored.
                # Save it to the file here without duplicating it on the terminal.
                with lock:
                    traceback.print_exc(file=log)
                    log.flush()
                raise
            finally:
                print(f"\n[Log] Finished: {datetime.now().astimezone().isoformat(timespec='seconds')} ({status})")
