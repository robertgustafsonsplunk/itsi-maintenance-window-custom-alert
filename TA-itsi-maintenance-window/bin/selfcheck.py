#!/usr/bin/env python3
"""Fails if parse/title/GET-batch helpers break. Run: python3 bin/selfcheck.py"""
from itsi_maintenance_window import (
    IN_CHUNK,
    chunked,
    map_identifiers,
    map_windows,
    parse_epoch,
    window_title,
)


def main() -> None:
    assert parse_epoch("1710000000") == 1710000000
    assert parse_epoch("1710000000000") == 1710000000
    assert parse_epoch("2024-01-02T03:04:05Z") == parse_epoch("2024-01-02T03:04:05")
    assert parse_epoch("2024-01-02 03:04:05") == parse_epoch("2024-01-02T03:04:05")
    assert parse_epoch("") is None
    assert window_title("CMDB Maintenance", "app-server-01", 10, 20) == (
        "CMDB Maintenance app-server-01 1970-01-01T00:00:10Z/1970-01-01T00:00:20Z"
    )

    assert list(chunked(["a", "b", "c"], 2)) == [["a", "b"], ["c"]]
    assert list(chunked([], IN_CHUNK)) == []

    records = [
        {"_key": "ent-title", "title": "app-server-01", "identifiervalues": ["other"]},
        {"_key": "ent-ident", "title": "web-01", "identifiervalues": ["ci-99"]},
        {"_key": "ent-later", "title": "app-server-01", "identifiervalues": ["app-server-01"]},
    ]
    mapped = map_identifiers(["app-server-01", "ci-99", "missing"], records)
    assert mapped["app-server-01"] == "ent-title"
    assert mapped["ci-99"] == "ent-ident"
    assert "missing" not in mapped

    windows = map_windows(
        [
            {
                "title": "CMDB Maintenance app-server-01 1970-01-01T00:00:10Z/1970-01-01T00:00:20Z",
                "_key": "mw-1",
            },
            {"title": "", "_key": "mw-skip"},
        ]
    )
    assert windows == {
        "CMDB Maintenance app-server-01 1970-01-01T00:00:10Z/1970-01-01T00:00:20Z": "mw-1"
    }
    print("selfcheck ok")


if __name__ == "__main__":
    main()
