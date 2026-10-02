from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

_VOLATILE_SEGMENT = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f-]{36})$", re.I)


def path_and_query(url: str) -> str:
    p = urlsplit(url)
    return p.path + (f"?{p.query}" if p.query else "")


def canonical_path_regex(url: str) -> str:
    """/app/member.do?mid=48213 -> ^/app/member\\.do(\\?.*)?$ ; /item/12345 -> ^/item/[^/]+(\\?.*)?$.

    Query values are always dropped (they carry record ids, nonces, and PII); numeric/opaque
    path segments are generalised so the checkpoint is valid for every parameter value.
    """
    path = urlsplit(url).path or "/"
    segments = [("[^/]+" if _VOLATILE_SEGMENT.match(s) else re.escape(s)) for s in path.split("/")]
    return "^" + "/".join(segments) + r"(\?.*)?$"


def query_keys(url: str) -> list[str]:
    return sorted({k for k, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True)})
