"""Load local settings at executable boundaries, before harness imports.

The repository's .env is optional. Explicit process environment variables win;
workers inherit them without putting credentials in arguments or task records.
"""

from pathlib import Path
import os


REPOSITORY_ROOT = Path(__file__).resolve().parent


def default_adb() -> str:
    """Use an explicit override, a repository-local tool, or the user's PATH."""
    if os.environ.get("ADB_PATH"):
        return os.environ["ADB_PATH"]
    bundled = REPOSITORY_ROOT / ".tools" / "platform-tools" / "adb"
    return str(bundled) if bundled.is_file() else "adb"


def load_environment(path: Path | None = None) -> bool:
    target = path if path is not None else REPOSITORY_ROOT / ".env"
    if not target.is_file():
        if path is not None:
            raise FileNotFoundError(target)
        return False
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise RuntimeError("Loading .env requires python-dotenv; install requirements.txt") from exc
    return load_dotenv(target, override=False)
