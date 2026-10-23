"""Load LUANTI_* variables from the project's .env file.

``import luantibot`` reads ``.env`` from the repository root, so the
examples and the MCP server work without the shell exporting anything.
Standard library only.

Rules:

* Only ``LUANTI_``-prefixed keys are read; anything else in the file
  is ignored.
* An environment variable that is already set to a non-empty value
  wins over the file, so shell exports and explicit MCP configuration
  still override it. An empty value counts as unset.
* Full-line comments start with ``#``. Values may be wrapped in
  single or double quotes (quote anything containing spaces).
"""
import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load(path=None):
    """Populate ``os.environ`` from *path* (default: the repository's .env)."""
    try:
        lines = Path(path or ENV_FILE).read_text(
            encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key.startswith("LUANTI_"):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not os.environ.get(key):
            os.environ[key] = value
