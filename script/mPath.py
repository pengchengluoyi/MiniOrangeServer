# script/mPath.py

import os
from pathlib import Path

from script.log import SLog
from server.core.database import APP_DATA_DIR


UPLOAD_DIR = os.path.join(APP_DATA_DIR, "uploads")

def get_final_path(input_str):
    base_path = UPLOAD_DIR
    input_path = Path(input_str)
    if input_path.is_absolute():
        return str(input_path)
    return str(base_path / input_path)


def add_suffix_before_ext(filepath, suffix):
    base, ext = os.path.splitext(filepath)
    return base + suffix + ext