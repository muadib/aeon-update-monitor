"""Dependency-free terminal frontend: plain ANSI escapes + termios (no curses).

    bin/aeonmon-tui [--no-root] [--backend "backend command"]

Runs the backend as a subprocess and consumes its JSON. By default it asks for privileges with
pkexec; if the dialog is cancelled it keeps going without them.
Keys: ←/→ or Tab switch tab, ↑/↓ move, Enter expand, r reload, p authenticate, q quit.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import select
import shutil
import sys
import termios
import tty

from . import client

TABS = ["Estado", "Actualizaciones", "Snapshots"]
STYLE = {0: "", 1: "\x1b[36m", 2: "\x1b[1;33m"}  # normal, cian, amarillo negrita
REV, RESET = "\x1b[7m", "\x1b[0m"
REFRESH_S = 30


def _short(iso: str | None) -> str:
    return iso.replace("T", " ")[:16] if iso else "-"


def _lines(tab: int, d: dict, expanded: set) -> list[tuple[str, int]]:
    """(texto, estilo) por línea. estilo: 0 normal, 1 resalte, 2 aviso."""
    out: list[tuple[str, int]] = []
    if tab == 0:
        s = d["status"]
        rows = [("Host", s["hostname"]), ("Sistema", s["os"]), ("Versión", s["os_version"]),
                ("Kernel", s["kernel"]), ("Arrancó", _short(s["booted_at"])),
                ("Snapshot en uso", s["running_snapshot"]), ("Snapshot próximo boot", s["next_boot_snapshot"]),
                ("Última actualización", _short((s["last_update"] or {}).get("time"))),
                ("Último dup", _short((s["last_dup"] or {}).get("time"))),
                ("Próx. timer", s["timer"].get("next") or "-")]
        out += [(f"{k:<24}{v}", 0) for k, v in rows]
        if s["reboot_pending"]:
            out += [("", 0), ("⚠ REINICIO PENDIENTE: hay un snapshot nuevo sin arrancar", 2)]
        if s.get("snapper_error"):
            out.append((f"snapper: {s['snapper_error']}", 2))
    elif tab == 1:
        for i, t in enumerate(d["updates"].get("transactions", [])):
            c = ", ".join(f"{n} {k}" for k, n in t["counts"].items())
            out.append((f"{'▼' if i in expanded else '▶'} {_short(t['time'])}  {t['kind']:<8} {t['total']:>4} pkgs  ({c})", 1))
            if i in expanded:
                for ch in t["changes"]:
                    v = f"{ch['from']} → {ch['to']}" if ch["from"] and ch["to"] else (ch["to"] or ch["from"])
                    out.append((f"     {ch['action']:<10} {ch['name']:<36} {v}", 0))
    else:
        for sn in d["snapshots"].get("snapshots", []):
            mark = ("A" if sn["active"] else " ") + ("D" if sn["default"] else " ")
            out.append((f"#{sn['number']:<5} {mark} {_short(sn['time'])}  {sn['type']:<7} {sn['description']}",
                        1 if sn["active"] else 0))
        if d["snapshots"].get("error"):
            out.append((f"snapper: {d['snapshots']['error']}", 2))
    return out


def _read_key(fd: int, timeout: float) -> str | None:
    """Devuelve 'up','down','left','right','enter','tab','esc' o el carácter; None = timeout."""
    if not select.select([fd], [], [], timeout)[0]:
        return None
    b = os.read(fd, 1)
    if b == b"\x1b":
        if not select.select([fd], [], [], 0.05)[0]:
            return "esc"
        seq = os.read(fd, 8)
        return {b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left",
                b"OA": "up", b"OB": "down", b"OC": "right", b"OD": "left"}.get(seq, "")
    return {b"\r": "enter", b"\n": "enter", b"\t": "tab"}.get(b, b.decode(errors="ignore"))


def _draw(tab, top, sel, data, err, expanded, state="") -> tuple[int, int]:
    cols, rows = shutil.get_terminal_size((80, 24))
    buf = ["\x1b[H\x1b[2J"]  # cursor al inicio + limpiar

    def put(y, text, style=0, rev=False):
        buf.append(f"\x1b[{y};1H{REV if rev else ''}{STYLE[style]}{text[: cols - 2].ljust(cols - 2) if rev else text[: cols - 2]}{RESET}")

    put(1, " " + "".join(f"{REV if i == tab else ''} {t} {RESET} " for i, t in enumerate(TABS)))
    if err or data is None:
        put(3, f"Error: {err}", 2)
    else:
        lines = _lines(tab, data, expanded)
        sel = max(0, min(sel, len(lines) - 1))
        view = max(1, rows - 4)
        top = sel if sel < top else max(top, sel - view + 1)
        for r, (txt, st) in enumerate(lines[top: top + view]):
            put(3 + r, txt, st, rev=(tab == 1 and top + r == sel))
        hint = {"privileged": "[root]", "declined": "[sin privilegios — p: autenticar]",
                "plain": "[sin privilegios]"}.get(state, "")
        put(rows, f"←/→ pestaña  ↑/↓ mover  Enter expandir  r recargar  q salir  {hint}  [{_short(data['generated_at'])}]")
    sys.stdout.write("".join(buf))
    sys.stdout.flush()
    return top, sel


def _tx_at(data: dict, sel: int, expanded: set) -> int:
    """Línea seleccionada -> índice de transacción (-1 si ninguno)."""
    n = 0
    for i, t in enumerate(data["updates"].get("transactions", [])):
        span = 1 + (len(t["changes"]) if i in expanded else 0)
        if n <= sel < n + span:
            return i
        n += span
    return -1


@contextlib.contextmanager
def _suspended(fd: int, old):
    """Leave the alternate screen so a text-mode pkexec prompt can use the terminal."""
    sys.stdout.write("\x1b[?25h\x1b[?1049l")
    sys.stdout.flush()
    termios.tcsetattr(fd, termios.TCSADRAIN, old)
    try:
        yield
    finally:
        tty.setcbreak(fd)
        sys.stdout.write("\x1b[?1049h\x1b[?25l")
        sys.stdout.flush()


def run(backend: str | None, elevate: bool = True) -> int:
    fd = sys.stdin.fileno()
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("aeonmon.tui necesita un terminal interactivo", file=sys.stderr)
        return 2
    old = termios.tcgetattr(fd)
    tab, top, sel, expanded, err, data, state = 0, 0, 0, set(), "", None, ""

    def load(elevate_now: bool):
        nonlocal data, err, state
        try:
            resp, state = client.fetch_auto("all", backend=backend, elevate=elevate_now, limit=30)
            if state == "plain" and elevate:  # silent refresh after a cancelled/skipped prompt
                state = "declined"
            data, err = {**resp["data"], "generated_at": resp["generated_at"]}, ""
        except Exception as e:  # noqa: BLE001
            err = str(e)

    def reload(ask: bool):
        """ask=True: try pkexec (the screen is suspended so any prompt is visible)."""
        if ask:
            with _suspended(fd, old):
                load(True)
        else:
            load(False)

    load(elevate)  # before entering the alternate screen: a text-mode pkexec prompt stays readable
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # pantalla alterna + ocultar cursor
    try:
        tty.setcbreak(fd)  # sin eco ni buffer de línea; Ctrl-C sigue funcionando
        while True:
            top, sel = _draw(tab, top, sel, data, err, expanded, state)
            # Auto-refresh only without privileges: with pkexec it would prompt every time.
            k = _read_key(fd, None if state == "privileged" else REFRESH_S)
            if k in ("q", "esc"):
                return 0
            if k in ("right", "tab"):
                tab, top, sel = (tab + 1) % 3, 0, 0
            elif k == "left":
                tab, top, sel = (tab - 1) % 3, 0, 0
            elif k == "down":
                sel += 1
            elif k == "up":
                sel = max(0, sel - 1)
            elif k == "enter" and tab == 1 and data:
                i = _tx_at(data, sel, expanded)
                if i >= 0:
                    expanded ^= {i}
            elif k == "r":  # manual: re-ask only if we are root-via-pkexec (no cached auth)
                reload(state == "privileged")
            elif k == "p" and state == "declined":
                reload(True)
            elif k is None:  # periodic refresh, never prompts
                reload(False)
    except KeyboardInterrupt:
        return 130
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aeonmon-tui", description="Terminal UI for the Aeon update monitor.")
    p.add_argument("--backend", help="backend command to run as-is (disables automatic pkexec); "
                                     "default: the bundled bin/aeonmon, with pkexec")
    p.add_argument("--no-root", action="store_true", help="never ask for privileges (skip pkexec)")
    a = p.parse_args(argv)
    return run(a.backend, elevate=not a.no_root)


if __name__ == "__main__":
    raise SystemExit(main())
