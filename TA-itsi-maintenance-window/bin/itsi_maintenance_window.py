#!/usr/bin/env python3
"""Create ITSI maintenance windows from alert results.

Auth is the alert session_key only — no stored passwords.
Local splunkd TLS is not verified: management certs rarely match 127.0.0.1.
"""
from __future__ import annotations

import csv
import gzip
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ponytail: GET filter lives in the query string; ~80 ids stays under typical URL limits.
# Raise IN_CHUNK or switch to another $in paging scheme if titles get huge.
IN_CHUNK = 80


def parse_epoch(value: Any) -> Optional[int]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        n = int(s)
        if n > 10**12:
            n //= 1000
        return n
    s_norm = s.replace("Z", "+0000")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            sample = s_norm if fmt.endswith("%z") else s.rstrip("Z")
            dt = datetime.strptime(sample, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue
    return None


def iso_utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def window_title(prefix: str, identifier: str, start_epoch: int, end_epoch: int) -> str:
    prefix = (prefix or "CMDB Maintenance").strip()
    return "%s %s %s/%s" % (prefix, identifier, iso_utc(start_epoch), iso_utc(end_epoch))


def read_results(path: str) -> List[Dict[str, str]]:
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", newline="") as fh:
        return list(csv.DictReader(fh))


def results_from_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    path = payload.get("results_file")
    if path:
        try:
            rows = read_results(path)
            if rows:
                return rows
        except OSError as exc:
            sys.stderr.write("WARN could not read results_file %s: %s\n" % (path, exc))
    result = payload.get("result") or {}
    return [result] if result else []


def chunked(items: List[str], size: int = IN_CHUNK) -> Iterable[List[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def as_records(data: Any) -> List[Dict[str, Any]]:
    if not data:
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


def ident_values(raw: Any) -> List[str]:
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    out = []
    for value in values:
        s = str(value).strip()
        if s:
            out.append(s)
    return out


def map_identifiers(identifiers: List[str], records: List[Dict[str, Any]]) -> Dict[str, str]:
    """Title match wins over identifiervalues; first record wins ties."""
    by_title: Dict[str, str] = {}
    by_ident: Dict[str, str] = {}
    for rec in records:
        key = rec.get("_key")
        if not key:
            continue
        title = str(rec.get("title") or "").strip()
        if title and title not in by_title:
            by_title[title] = key
        for value in ident_values(rec.get("identifiervalues")):
            if value not in by_ident:
                by_ident[value] = key
    mapped: Dict[str, str] = {}
    for ident in identifiers:
        key = by_title.get(ident) or by_ident.get(ident)
        if key:
            mapped[ident] = key
    return mapped


def map_windows(records: List[Dict[str, Any]]) -> Dict[str, str]:
    found: Dict[str, str] = {}
    for rec in records:
        title = str(rec.get("title") or "").strip()
        key = rec.get("_key")
        if title and key and title not in found:
            found[title] = key
    return found


class SplunkRest:
    def __init__(self, server_uri: str, session_key: str) -> None:
        self.server_uri = server_uri.rstrip("/")
        self.session_key = session_key
        # ponytail: local splunkd cert hostname rarely matches; verify if you pin it.
        self.ctx = ssl._create_unverified_context()

    def json(self, method: str, path: str, body: Any = None, query: Optional[Dict[str, str]] = None) -> Any:
        url = self.server_uri + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": "Splunk %s" % self.session_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            raise RuntimeError("HTTP %s %s: %s" % (exc.code, url, detail[:500])) from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")


def lookup_objects(rest: SplunkRest, object_type: str, identifiers: List[str]) -> Dict[str, str]:
    identifiers = list(dict.fromkeys(identifiers))
    if not identifiers:
        return {}
    path = "/servicesNS/nobody/SA-ITOA/itoa_interface/%s" % urllib.parse.quote(object_type)
    records: List[Dict[str, Any]] = []
    for chunk in chunked(identifiers):
        filt = json.dumps({"$or": [{"title": {"$in": chunk}}, {"identifiervalues": {"$in": chunk}}]})
        data = rest.json("GET", path, query={"filter": filt, "fields": "title,_key,identifiervalues"})
        records.extend(as_records(data))
    return map_identifiers(identifiers, records)


def lookup_windows(rest: SplunkRest, titles: List[str]) -> Dict[str, str]:
    titles = list(dict.fromkeys(titles))
    if not titles:
        return {}
    path = "/servicesNS/nobody/SA-ITOA/maintenance_services_interface/maintenance_calendar"
    found: Dict[str, str] = {}
    for chunk in chunked(titles):
        filt = json.dumps({"title": {"$in": chunk}})
        data = rest.json("GET", path, query={"filter": filt, "fields": "title,_key"})
        found.update(map_windows(as_records(data)))
    return found


def create_window(rest: SplunkRest, title: str, start_epoch: int, end_epoch: int, objects: List[Dict[str, str]]) -> str:
    payload = {
        "title": title,
        "comment": "Created by TA-itsi-maintenance-window custom alert action",
        "start_time": start_epoch,
        "end_time": end_epoch,
        "objects": objects,
    }
    data = rest.json(
        "POST",
        "/servicesNS/nobody/SA-ITOA/maintenance_services_interface/maintenance_calendar",
        body=payload,
    )
    if isinstance(data, dict) and data.get("_key"):
        return data["_key"]
    return str(data)


def process(payload: Dict[str, Any]) -> int:
    cfg = payload.get("configuration") or {}
    prefix = cfg.get("title_prefix") or "CMDB Maintenance"
    identifier_field = (cfg.get("identifier_field") or "title").strip()
    object_type = (cfg.get("object_type") or "entity").strip()
    if object_type not in ("entity", "service"):
        sys.stderr.write("ERROR object_type must be entity or service, got %s\n" % object_type)
        return 2

    rows = results_from_payload(payload)
    if not rows:
        sys.stderr.write("INFO no results to process\n")
        return 0

    pending: List[Tuple[str, int, int, str]] = []
    created = skipped = failed = 0
    for row in rows:
        identifier = (row.get(identifier_field) or row.get("title") or "").strip()
        start_epoch = parse_epoch(row.get("maintenance_start_time"))
        end_epoch = parse_epoch(row.get("maintenance_end_time"))
        if not identifier or start_epoch is None or end_epoch is None:
            sys.stderr.write("WARN skip row missing title/maintenance_start_time/maintenance_end_time: %s\n" % json.dumps(row)[:300])
            failed += 1
            continue
        if end_epoch <= start_epoch:
            sys.stderr.write("WARN skip %s: maintenance_end_time <= maintenance_start_time\n" % identifier)
            failed += 1
            continue
        pending.append((identifier, start_epoch, end_epoch, window_title(prefix, identifier, start_epoch, end_epoch)))

    if not pending:
        sys.stderr.write("INFO done created=%s skipped=%s failed=%s\n" % (created, skipped, failed))
        return 2 if failed else 0

    rest = SplunkRest(payload["server_uri"], payload["session_key"])
    objects = lookup_objects(rest, object_type, [row[0] for row in pending])
    existing = lookup_windows(rest, [row[3] for row in pending])
    sys.stderr.write("INFO prefetch objects=%s existing_windows=%s pending=%s\n" % (len(objects), len(existing), len(pending)))

    for identifier, start_epoch, end_epoch, title in pending:
        already = existing.get(title)
        if already:
            sys.stderr.write("INFO skip existing MW %s key=%s\n" % (title, already))
            skipped += 1
            continue

        obj_key = objects.get(identifier)
        if not obj_key:
            sys.stderr.write("WARN no ITSI %s matched identifier=%s\n" % (object_type, identifier))
            failed += 1
            continue

        key = create_window(
            rest,
            title,
            start_epoch,
            end_epoch,
            [{"object_type": object_type, "_key": obj_key}],
        )
        existing[title] = key
        sys.stderr.write("INFO created MW %s key=%s object=%s\n" % (title, key, identifier))
        created += 1

    sys.stderr.write("INFO done created=%s skipped=%s failed=%s\n" % (created, skipped, failed))
    return 2 if failed and not created and not skipped else 0


def main(argv: Iterable[str] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2 or argv[1] != "--execute":
        sys.stderr.write("FATAL Unsupported execution mode (expected --execute flag)\n")
        return 1
    try:
        payload = json.loads(sys.stdin.read())
        return process(payload)
    except Exception as exc:
        sys.stderr.write("ERROR Unexpected error: %s\n" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
