# aeonmon — update monitor for Aeon

Two decoupled pieces (Python 3 standard library only):

* **Backend** (`python3 -m aeonmon`): runs, collects data, prints **JSON to stdout** and exits.
  No server, nothing written to disk.
* **Frontend**: any program that runs the backend and parses its stdout. Included: a terminal UI
  **without curses** (plain ANSI + `termios`, so it works with Aeon's stock Python) and a
  GTK4/libadwaita GUI. You can write your own in Qt, Rust, bash + jq, etc.

## Backend

```bash
python3 -m aeonmon status --pretty
python3 -m aeonmon updates --limit 5 --since 2026-11-01
python3 -m aeonmon snapshots --limit 10
python3 -m aeonmon pending      # zypper list-updates (slower)
python3 -m aeonmon all          # status + updates + snapshots in a single call
```

Success: exit code 0 and JSON on stdout. Failure: non-zero exit code and `{"error": "..."}` on stderr.

The launchers in `bin/` (`aeonmon`, `aeonmon-tui`, `aeonmon-gtk`) work from any directory: they locate
the project from their own path, so they also work under `pkexec`/`sudo`.

## Frontends

### Privileges

`snapper` needs root, so both frontends **ask for privileges automatically** (via `pkexec`) when they start.
If you cancel the dialog (or `pkexec` is not available) they **keep going without privileges**: you still
get the zypp history and system info, and the snapshot data shows a permission error.

* Cancelled? TUI: press `p` to authenticate later. GUI: click **Authenticate** in the notification.
* While privileged there is no auto-refresh (it would prompt every time); reload manually (`r` / refresh button).
* `--no-root` never asks for privileges. `--backend "CMD"` runs exactly that command (no automatic `pkexec`).

### Terminal UI

```bash
bin/aeonmon-tui              # asks for privileges, falls back to unprivileged if cancelled
bin/aeonmon-tui --no-root    # never ask
```

Keys: `←`/`→` or `Tab` switch tab, `↑`/`↓` move, `Enter` expand, `r` reload, `p` authenticate, `q`/`Esc` quit.
Also available as `python3 -m aeonmon.tui`.

### GUI (GTK4 + libadwaita)

```bash
bin/aeonmon-gtk              # asks for privileges, falls back to unprivileged if cancelled
bin/aeonmon-gtk --no-root    # never ask
```

A lock icon in the header bar shows whether it is running with privileges.

Requires PyGObject with the Gtk 4 and Adw 1 typelibs. Check with:

```bash
python3 -c "import gi; gi.require_version('Gtk','4.0'); gi.require_version('Adw','1'); from gi.repository import Gtk, Adw; print('ok')"
```

### From Python

```python
from aeonmon.client import fetch
fetch("all", limit=30)["data"]
```

## JSON contract (schema 1)

Every response: `{"schema": 1, "generated_at": ISO, "endpoint": str, "data": {...}}`.

| command | `data` |
|---|---|
| `status` | `hostname, os, os_version, kernel, booted_at, running_snapshot, next_boot_snapshot, reboot_pending, last_update{time,kind,packages}, last_dup, timer{active,next,last}` |
| `updates` | `transactions[]` (newest first): `time, epoch, user, command, kind (dup/update/patch/install/remove/other), counts{}, total, changes[]{name, arch, action (upgraded/downgraded/installed/removed/reinstalled), from, to, repo}` |
| `snapshots` | `snapshots[]`: `number, type, time, description, cleanup, userdata, active, default` |
| `pending` | `available, items[]{name, from, to, arch, kind}` |
| `all` | `{status, updates, snapshots}` |

Partial failures (e.g. snapper without permissions) appear as an `"error"` key inside `data` and do
not break the rest of the response.

## Aeon notes

* `reboot_pending` = **active** snapshot ≠ **default** snapshot (or `/run/reboot-needed` exists).
* `updates` timestamps come from `/var/log/zypp/history` (local time). Each `transactional-update dup` is one transaction.
* `snapper list` needs root. To avoid the `pkexec` prompt entirely, grant your user access once:
  `sudo snapper -c root set-config ALLOW_USERS=$USER SYNC_ACL=yes`, then run with `--no-root`.
* `pkexec` runs in a different working directory and without your `PYTHONPATH`, so the frontends
  call the `bin/aeonmon` launcher by absolute path (never `-m aeonmon`).

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

Uses fixtures; Aeon is not required.
