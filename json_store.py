import json
import os
import logging
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


class JsonStore:
    """Thread-safe, crash-safe JSON file store with atomic writes."""

    def __init__(self, path: Path):
        self.path = path
        if not self.path.exists():
            self.path.write_text("{}", encoding="utf-8")

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            logger.exception("Не удалось прочитать %s", self.path.name)
            return {}

    def save(self, data: dict) -> None:
        """Atomic write: write to temp file, then os.replace() to final path."""
        content = json.dumps(data, ensure_ascii=False, indent=2)
        dir_path = self.path.parent

        fd, tmp_path = tempfile.mkstemp(
            dir=str(dir_path),
            prefix=f".{self.path.stem}_",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, str(self.path))
        except Exception:
            logger.exception("Не удалось атомарно записать %s", self.path.name)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
