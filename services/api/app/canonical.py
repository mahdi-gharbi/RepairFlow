import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(value: Any) -> str:
    data = value if isinstance(value, (bytes, str)) else canonical_json(value)
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()
