"""Turn an arbitrary JSON document into (channel, value, tags) rows.

Rules
  * dict keys become dotted channel-name segments
  * a list of objects keeps ONE channel name per field; each element is told apart by
    a tag named after the list (`activity=0`, `activity=1`, ...), so channel names do
    not multiply when the list grows
  * a list of scalars is indexed in the channel name when it has more than one element
  * numbers and booleans become floats, other strings become string channels
  * UUIDs and timestamps are dropped (identity, not telemetry) unless keep_ids is set
"""
from __future__ import annotations

import re

DEFAULT_SKIP = frozenset({
    "SchemaVersion", "Classification", "OwnerProducer", "GovernmentIdentifier",
    "SecurityInformation", "MessageHeader",
})
_ID_OR_TIME = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F-]{22}|\d{4}-\d\d-\d\dT.*")

Row = tuple[str, "float | str", dict[str, str]]


def flatten(obj, prefix: str = "", *, skip_keys=DEFAULT_SKIP, max_fields: int = 80,
            max_list_items: int = 16, keep_ids: bool = False, lowercase: bool = False
            ) -> list[Row]:
    rows: list[Row] = []

    def name(parent: str, key: str) -> str:
        key = key.lower() if lowercase else key
        return f"{parent}.{key}" if parent else key

    def walk(node, path: str, tags: dict[str, str]) -> None:
        if len(rows) >= max_fields:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                if k in skip_keys:
                    continue
                walk(v, name(path, str(k)), tags)
        elif isinstance(node, list):
            items = node[:max_list_items]
            if items and all(isinstance(i, dict) for i in items):
                tag_key = path.rsplit(".", 1)[-1].lower()
                for i, item in enumerate(items):
                    walk(item, path, {**tags, tag_key: str(i)})
            else:
                for i, item in enumerate(items):
                    walk(item, f"{path}.{i}" if len(items) > 1 else path, tags)
        elif isinstance(node, bool):
            rows.append((path, 1.0 if node else 0.0, tags))
        elif isinstance(node, (int, float)):
            rows.append((path, float(node), tags))
        elif isinstance(node, str):
            if keep_ids or not _ID_OR_TIME.fullmatch(node):
                rows.append((path, node, tags))

    walk(obj, prefix, {})
    return rows
