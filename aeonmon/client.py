"""Helpers for Python frontends: run the backend and return its parsed JSON.

Other languages only need to run the command and read stdout.

Privileges: ``fetch_auto`` tries ``pkexec`` first; if the user cancels the dialog (or pkexec is
not available) it transparently falls back to running the backend without privileges.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

LAUNCHER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "aeonmon")
DEFAULT_BACKEND = f"{shlex.quote(sys.executable)} {shlex.quote(LAUNCHER)}"
PKEXEC_BACKEND = f"pkexec {shlex.quote(LAUNCHER)}"
# pkexec: 126 = authentication dialog dismissed, 127 = not authorized / authentication failed / error
AUTH_DECLINED = (126, 127)


class BackendError(RuntimeError):
    def __init__(self, message: str, returncode: int):
        super().__init__(message)
        self.returncode = returncode


def fetch(command: str, *, backend: str | None = None, timeout: int = 120, **params) -> dict:
    cmd = shlex.split(backend or DEFAULT_BACKEND) + [command]
    for k, v in params.items():
        if v is not None:
            cmd += [f"--{k}", str(v)]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise BackendError(f"{cmd[0]}: command not found", 127) from e
    except subprocess.TimeoutExpired as e:
        raise BackendError(f"{cmd[0]}: timed out", 124) from e
    if p.returncode != 0:
        try:
            msg = json.loads(p.stderr)["error"]
        except (ValueError, KeyError, TypeError):
            msg = p.stderr.strip() or f"backend exited with code {p.returncode}"
        raise BackendError(msg, p.returncode)
    return json.loads(p.stdout)


def fetch_auto(command: str, *, backend: str | None = None, elevate: bool = True, **params) -> tuple[dict, str]:
    """Run the backend, asking for privileges when possible. Returns ``(response, state)``.

    state:
      ``custom``     an explicit backend command was given; used as-is
      ``privileged`` ran as root (via pkexec, or we already were root)
      ``declined``   pkexec was cancelled/unavailable; ran without privileges
      ``plain``      elevation was not requested; ran without privileges
    """
    if backend:
        return fetch(command, backend=backend, **params), "custom"
    if os.geteuid() == 0:
        return fetch(command, **params), "privileged"
    if not elevate:
        return fetch(command, **params), "plain"
    try:
        return fetch(command, backend=PKEXEC_BACKEND, **params), "privileged"
    except BackendError as e:
        if e.returncode not in AUTH_DECLINED:
            raise
    return fetch(command, **params), "declined"
