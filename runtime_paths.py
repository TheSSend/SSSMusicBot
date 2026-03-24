import os
from pathlib import Path


def get_data_dir() -> Path:
    data_dir = Path(os.getenv("MUSICBOT_DATA_DIR", "."))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def data_path(filename: str) -> Path:
    return get_data_dir() / filename
