# ITSI Maintenance Window Alert

Splunk custom alert action that creates [ITSI](https://docs.splunk.com/Documentation/ITSI) maintenance windows from CMDB search results.

When a CI’s `operation_status` is `maintenance`, a saved search fires this action. The action looks up the matching ITSI entity (or service) by **title** and creates a maintenance window from `maintenance_start_time` and `maintenance_end_time`.

App ID: `TA-itsi-maintenance-window`

## How it works

```
CMDB events
        │
        ▼
Saved search: operation_status=maintenance
        │
        ▼
Alert action: ITSI Maintenance Window
        │
        ├─ parse maintenance_start_time / maintenance_end_time (epoch or ISO)
        ├─ skip if a window with the same title already exists
        ├─ look up ITSI entity/service by title (or identifiervalues)
        └─ POST /servicesNS/nobody/SA-ITOA/maintenance_services_interface/maintenance_calendar
```

The ITSI REST body still uses `start_time` / `end_time`. Those are API field names, not CMDB fields.

Auth is the Splunk alert `session_key`. No passwords are stored in the app. The user who owns the saved search needs permission to create ITSI maintenance windows (`itoa_admin` is enough).

Window titles use UTC ISO 8601 (`YYYY-MM-DDTHH:MM:SSZ`) for the start/end range:

```
CMDB Maintenance app-server-01 2026-09-30T21:20:35Z/2026-09-30T23:20:35Z
```

## Requirements

- Splunk Enterprise with **ITSI installed and configured**
- ITSI entities (or services) whose **title** matches the CMDB `title` field
- Python 3 (the action sets `python.version = python3`)

ITSI 5.0.x needs Splunk 10.2+ / Python 3.10. On Splunk 9.3.x use ITSI 4.20.x or 4.21.x.

## Install

```bash
cp -R TA-itsi-maintenance-window $SPLUNK_HOME/etc/apps/
```

Restart Splunk, or reload the app. This app does **not** create an index or sourcetype. Point your saved search at whatever index already holds the CMDB.

## CMDB event shape

Use whatever index and sourcetype you already have. The action only cares about the search **results**.


| Field                    | Required                     | Notes                                                                                                                   |
| ------------------------ | ---------------------------- | ----------------------------------------------------------------------------------------------------------------------- |
| `operation_status`       | yes (for the example search) | Example search keeps `"maintenance"`                                                                                    |
| `title`                  | yes                          | Same value as the ITSI entity **title**. Do not send a JSON field named `host`; Splunk’s metadata `host` overwrites it. |
| `maintenance_start_time` | yes when in maintenance      | Epoch seconds, epoch milliseconds, `YYYY-MM-DDTHH:MM:SS[Z]`, or `YYYY-MM-DD HH:MM:SS` (UTC if no offset)                |
| `maintenance_end_time`   | yes when in maintenance      | Same formats. Must be after `maintenance_start_time`.                                                                   |


Example:

```json
{
  "title": "app-server-01",
  "environment": "lab",
  "entity_type": "linux_server",
  "operation_status": "maintenance",
  "maintenance_start_time": "1790803235",
  "maintenance_end_time": "1790810435",
  "change_ticket": "CHG-1001"
}
```

CIs that are not in maintenance can omit the times:

```json
{"title": "web-server-01", "operation_status": "operational"}
```



## Saved search

Create a scheduled alert against your CMDB index and attach **ITSI Maintenance Window**.

### Search

```spl
index=<your_cmdb_index> operation_status="maintenance"
| where isnotnull(title) AND isnotnull(maintenance_start_time) AND isnotnull(maintenance_end_time)
| dedup title sortby -_indextime
| table title, maintenance_start_time, maintenance_end_time
```

That keeps one row per entity title, taken from the most recently **indexed** CMDB event.

### Alert and schedule settings


| Setting                               | Example                                             | Why                                              |
| ------------------------------------- | --------------------------------------------------- | ------------------------------------------------ |
| `cron_schedule`                       | `*/5 * * * *`                                       | Aligns with a 5-minute CMDB refresh              |
| `dispatch.earliest_time`              | `-15m`                                              | Overlap so a slow ingest is not missed           |
| `counttype` / `relation` / `quantity` | number of events greater than 0                     | Fire when any CI is in maintenance               |
| `alert.digest_mode`                   | `1`                                                 | One action invocation with all rows              |
| `alert.suppress`                      | `1`                                                 |                                                  |
| `alert.suppress.fields`               | `title,maintenance_start_time,maintenance_end_time` | Same entity + same window is not re-fired for 4h |
| `alert.suppress.period`               | `4h`                                                |                                                  |
| `alert.track`                         | `1`                                                 | Show in Triggered Alerts                         |




### Action stanza

```ini
action.itsi_maintenance_window = 1
action.itsi_maintenance_window.param.title_prefix = CMDB Maintenance
action.itsi_maintenance_window.param.identifier_field = title
action.itsi_maintenance_window.param.object_type = entity
```

You can attach the same action to any other search as long as the results include:

- the identifier field (`title` by default)
- `maintenance_start_time`
- `maintenance_end_time`



## Alert action parameters

Shown in the UI when you add **ITSI Maintenance Window** to an alert.


| Parameter          | Default            | Description                                              |
| ------------------ | ------------------ | -------------------------------------------------------- |
| `title_prefix`     | `CMDB Maintenance` | Prefix of the ITSI window title                          |
| `identifier_field` | `title`            | Result field matched to the ITSI entity or service title |
| `object_type`      | `entity`           | `entity` or `service`                                    |


Lookup order for the ITSI object: `title == identifier`, then `identifiervalues == identifier`.

## ITSI entities

The action does not create entities. Create them first so **title** matches the CMDB `title` values. Example REST body:

```json
{
  "title": "app-server-01",
  "object_type": "entity",
  "identifier": {"fields": ["host"]},
  "host": ["app-server-01"]
}
```

`POST` to `/servicesNS/nobody/SA-ITOA/itoa_interface/entity`.

If a row has no matching object, the action logs `no ITSI entity matched identifier=...` and continues with the rest.

## Logs and troubleshooting

Action output is in `splunkd.log`:

```
index=_internal sendmodalert action=itsi_maintenance_window
```

Useful lines:

- `created MW ... key=...`
- `skip existing MW ...`
- `no ITSI entity matched identifier=...`
- `done created=N skipped=N failed=N`

Confirm windows in ITSI: **Configuration → Maintenance Windows**, or:

```
| rest /servicesNS/nobody/SA-ITOA/maintenance_services_interface/maintenance_calendar
```

