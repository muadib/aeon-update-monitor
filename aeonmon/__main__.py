"""Backend: ejecuta, recolecta y escribe JSON en stdout. Sin servidor ni archivos.

    python3 -m aeonmon {status,updates,snapshots,pending,all} [--limit N] [--since YYYY-MM-DD] [--pretty]

Código de salida 0 = JSON válido en stdout; != 0 = {"error": ...} en stderr.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import collectors


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aeonmon", description="Aeon update monitor backend: prints JSON to stdout and exits.")
    p.add_argument("command", choices=[*collectors.ENDPOINTS, "all"],
                   help="what to report: status, updates (zypp history), snapshots (snapper), pending (zypper), or all")
    p.add_argument("--limit", type=int, default=20, help="max transactions/snapshots to return (default: %(default)s)")
    p.add_argument("--since", help="only updates since YYYY-MM-DD")
    p.add_argument("--pretty", action="store_true", help="indent the JSON output")
    a = p.parse_args(argv)
    params = {"limit": a.limit, **({"since": a.since} if a.since else {})}
    try:
        data = collectors.respond(a.command, params)
    except Exception as e:  # noqa: BLE001 - el frontend solo necesita el motivo
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2 if a.pretty else None, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
