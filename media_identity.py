import hashlib
import os
from typing import Dict


def build_media_identity(filepath: str, duration: float) -> Dict[str, object]:
    file_size = os.path.getsize(filepath)
    sha256 = hashlib.sha256()

    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)

    content_hash = sha256.hexdigest()
    unique_id = f"{content_hash}_{file_size}_{float(duration):.3f}"

    return {
        "content_hash": content_hash,
        "file_size": file_size,
        "unique_id": unique_id,
    }
