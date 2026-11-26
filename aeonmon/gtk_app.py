"""GTK4 + libadwaita frontend (PyGObject). Runs the backend as a subprocess and consumes its JSON.

    bin/aeonmon-gtk [--no-root] [--backend "backend command"]

By default it asks for privileges with pkexec at startup; if the dialog is cancelled it keeps
going without them and offers an "Authenticate" button.
"""
from __future__ import annotations

import argparse
import shlex
import sys
import threading

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, GLib, Gtk
except (ImportError, ValueError) as e:  # PyGObject o typelibs ausentes
    sys.exit(f"No se pudo cargar GTK4/libadwaita vía PyGObject: {e}")

from . import client

APP_ID = "dev.aeonmon.Monitor"
AUTO_REFRESH_S = 60
ACTION_CSS = {"upgraded": "accent", "installed": "success", "removed": "error",
              "downgraded": "warning", "reinstalled": "dim-label"}
ACTION_ES = {"upgraded": "actualizado", "installed": "instalado", "removed": "eliminado",
             "downgraded": "versión anterior", "reinstalled": "reinstalado"}


def _short(iso: str | None) -> str:
    return iso.replace("T", " ")[:16] if iso else "-"


def _esc(s) -> str:
    return GLib.markup_escape_text(str(s if s is not None else "-"))


def _row(title, subtitle="", suffix: str | None = None, css: str = "") -> Adw.ActionRow:
    r = Adw.ActionRow(title=_esc(title), subtitle=_esc(subtitle) if subtitle != "" else "")
    r.set_subtitle_selectable(True)
    if suffix:
        lbl = Gtk.Label(label=suffix, valign=Gtk.Align.CENTER)
        if css:
            lbl.add_css_class(css)
        r.add_suffix(lbl)
    return r


def _clear(box: Gtk.Box) -> None:
    while (c := box.get_first_child()) is not None:
        box.remove(c)


def _page() -> tuple[Gtk.ScrolledWindow, Gtk.Box]:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18,
                  margin_top=18, margin_bottom=18, margin_start=12, margin_end=12)
    clamp = Adw.Clamp(maximum_size=760, child=box)
    return Gtk.ScrolledWindow(child=clamp, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER), box


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application, backend: str | None, elevate: bool = True):
        super().__init__(application=app, title="Aeon Update Monitor", default_width=720, default_height=640)
        self.backend = backend
        self.elevate = elevate and not backend
        self.state = ""  # custom | privileged | declined | plain (see client.fetch_auto)
        self._loading = False

        self.stack = Adw.ViewStack()
        self.status_page, self.status_box = _page()
        self.updates_page, self.updates_box = _page()
        self.snaps_page, self.snaps_box = _page()
        self.stack.add_titled_with_icon(self.status_page, "status", "Estado", "computer-symbolic")
        self.stack.add_titled_with_icon(self.updates_page, "updates", "Actualizaciones", "software-update-available-symbolic")
        self.stack.add_titled_with_icon(self.snaps_page, "snaps", "Snapshots", "drive-harddisk-symbolic")

        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        self.refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Recargar")
        self.refresh_btn.connect("clicked", lambda *_: self.load())  # re-asks only if not declined
        self.spinner = Gtk.Spinner()
        header = Adw.HeaderBar(title_widget=switcher)
        header.pack_start(self.refresh_btn)
        header.pack_end(self.spinner)
        self.lock = Gtk.Image(icon_name="changes-prevent-symbolic")
        header.pack_end(self.lock)

        self.banner = Adw.Banner(title="Reinicio pendiente: hay un snapshot nuevo que aún no arrancó")
        toolbar = Adw.ToolbarView(content=self.stack)
        toolbar.add_top_bar(header)
        toolbar.add_top_bar(self.banner)
        self.toasts = Adw.ToastOverlay(child=toolbar)
        self.set_content(self.toasts)

        self.load()
        # Con pkexec/sudo cada recarga pediría contraseña: solo auto-refresco sin elevación.
        if not any(w in (backend or "") for w in ("pkexec", "sudo")):
            GLib.timeout_add_seconds(AUTO_REFRESH_S, self._tick)

    def _tick(self) -> bool:
        if self.state != "privileged":  # with pkexec every refresh would prompt for a password
            self.load(ask=False)
        return True

    # ------------------------------------------------------------ carga
    def load(self, ask: bool | None = None) -> None:
        """ask=None: manual refresh (prompts unless the user already declined); False: silent."""
        if self._loading:
            return
        if ask is None:
            ask = self.state != "declined"
        self._loading = True
        self.spinner.start()
        self.refresh_btn.set_sensitive(False)

        def work():
            try:
                resp, state = client.fetch_auto("all", backend=self.backend,
                                                elevate=ask and self.elevate, limit=30)
                if state == "plain" and self.elevate:  # silent refresh after a declined prompt
                    state = "declined"
                err = None
            except Exception as e:  # noqa: BLE001
                resp, state, err = None, self.state, str(e)
            GLib.idle_add(self._loaded, resp, err, state)

        threading.Thread(target=work, daemon=True).start()

    def _loaded(self, resp: dict | None, err: str | None, state: str) -> bool:
        self._loading = False
        self.spinner.stop()
        self.refresh_btn.set_sensitive(True)
        if err:
            self.toasts.add_toast(Adw.Toast(title=f"Error del backend: {err}", timeout=0))
            return False
        was, self.state = self.state, state
        privileged = state == "privileged"
        self.lock.set_from_icon_name("changes-allow-symbolic" if privileged else "changes-prevent-symbolic")
        self.lock.set_tooltip_text("Running with administrator privileges" if privileged
                                   else "Running without privileges (snapper may be unavailable)")
        if state == "declined" and was != "declined":
            toast = Adw.Toast(title="Running without privileges", button_label="Authenticate", timeout=10)
            toast.connect("button-clicked", lambda *_: self.load(ask=True))
            self.toasts.add_toast(toast)
        d = resp["data"]
        self._fill_status(d["status"])
        self._fill_updates(d["updates"])
        self._fill_snapshots(d["snapshots"])
        return False

    # ----------------------------------------------------------- pestañas
    def _fill_status(self, s: dict) -> None:
        _clear(self.status_box)
        self.banner.set_revealed(bool(s["reboot_pending"]))
        g = Adw.PreferencesGroup(title="Sistema")
        for k, v in [("Host", s["hostname"]), ("Sistema", s["os"]), ("Versión", s["os_version"]),
                     ("Kernel", s["kernel"]), ("Arrancó", _short(s["booted_at"]))]:
            g.add(_row(k, v))
        self.status_box.append(g)

        g = Adw.PreferencesGroup(title="Actualizaciones")
        last, dup = s["last_update"] or {}, s["last_dup"] or {}
        g.add(_row("Última actualización", _short(last.get("time")),
                   f"{last['packages']} paquetes" if last else None))
        g.add(_row("Último dup", _short(dup.get("time")), f"{dup['packages']} paquetes" if dup else None))
        g.add(_row("Snapshot en uso", s["running_snapshot"]))
        g.add(_row("Snapshot del próximo arranque", s["next_boot_snapshot"]))
        g.add(_row("Próxima ejecución del timer", s["timer"].get("next") or "-"))
        self.status_box.append(g)

        if s.get("snapper_error"):
            g = Adw.PreferencesGroup(title="Avisos")
            g.add(_row("snapper", s["snapper_error"]))
            self.status_box.append(g)

    def _fill_updates(self, u: dict) -> None:
        _clear(self.updates_box)
        txs = u.get("transactions", [])
        if not txs:
            self.updates_box.append(Adw.StatusPage(title="Sin historial", description=u.get("error", ""),
                                                   icon_name="software-update-available-symbolic"))
            return
        g = Adw.PreferencesGroup(title="Historial", description=f"{u.get('total_transactions', len(txs))} transacciones")
        for t in txs:
            counts = ", ".join(f"{n} {ACTION_ES.get(k, k)}" for k, n in t["counts"].items())
            exp = Adw.ExpanderRow(title=_esc(f"{_short(t['time'])} · {t['kind']}"),
                                  subtitle=_esc(f"{t['total']} paquetes ({counts})"))
            exp._filled = False  # relleno perezoso: un dup puede traer cientos de paquetes
            exp.connect("notify::expanded", self._expand_tx, t)
            g.add(exp)
        self.updates_box.append(g)

    @staticmethod
    def _expand_tx(exp: Adw.ExpanderRow, _pspec, tx: dict) -> None:
        if not exp.get_expanded() or exp._filled:
            return
        exp._filled = True
        for c in tx["changes"]:
            ver = f"{c['from']} → {c['to']}" if c["from"] and c["to"] else (c["to"] or c["from"])
            exp.add_row(_row(c["name"], ver, ACTION_ES.get(c["action"], c["action"]), ACTION_CSS.get(c["action"], "")))

    def _fill_snapshots(self, sn: dict) -> None:
        _clear(self.snaps_box)
        snaps = [x for x in sn.get("snapshots", []) if x["number"] != 0]  # #0 = "current", marcador interno de snapper
        if not snaps:
            self.snaps_box.append(Adw.StatusPage(title="Sin snapshots", description=sn.get("error", ""),
                                                 icon_name="drive-harddisk-symbolic"))
            return
        g = Adw.PreferencesGroup(title="Snapshots")
        for s in snaps:
            row = _row(f"#{s['number']} · {s['description'] or s['type']}", f"{_short(s['time'])} · {s['type']}")
            for flag, text, css in (("active", "en uso", "accent"), ("default", "próximo arranque", "warning")):
                if s[flag]:
                    lbl = Gtk.Label(label=text, valign=Gtk.Align.CENTER)
                    lbl.add_css_class(css)
                    row.add_suffix(lbl)
            g.add(row)
        self.snaps_box.append(g)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aeonmon-gtk", description="GTK4 GUI for the Aeon update monitor.")
    p.add_argument("--backend", help="backend command to run as-is (disables automatic pkexec); "
                                     "default: the bundled bin/aeonmon, with pkexec")
    p.add_argument("--no-root", action="store_true", help="never ask for privileges (skip pkexec)")
    a = p.parse_args(argv)
    app = Adw.Application(application_id=APP_ID)
    app.connect("activate", lambda app: Window(app, a.backend, elevate=not a.no_root).present())
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    sys.exit(main())
