"""JSON state, written atomically (temp file + rename) so a crash cannot leave a half-written file."""
import json
import logging
import os
from pathlib import Path


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as error:
            logging.warning(f"State file unreadable ({type(error).__name__}); starting empty.")
            return {}

    def save(self, state: dict) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
