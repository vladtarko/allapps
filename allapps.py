#!/usr/bin/env python3
"""allapps — a Textual TUI listing installed applications and their sources."""

from __future__ import annotations

import subprocess

from rich.markup import escape
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Static

from discovery import (
    AppInfo,
    discover_apps,
    edit_original,
    edit_override,
    human_size,
    steam_uninstall_many,
    uninstall_hint,
    uninstall_targets,
)


def _short(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class ConfirmScreen(ModalScreen[bool]):
    """A small yes/no dialog."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "cancel", "Cancel"),
        Binding("y", "confirm", "Confirm"),
    ]

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            with VerticalScroll(id="confirm-scroll"):
                yield Static(self.message, id="confirm-message")
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", variant="primary", id="cancel")
                yield Button("Confirm", variant="error", id="confirm")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)


class AllAppsApp(App):
    TITLE = "allapps"
    SUB_TITLE = "scanning…"

    SORT_MODES = ("name", "size", "source")
    SORT_LABELS = {
        "name": "name",
        "size": "size (largest first)",
        "source": "source",
    }

    CSS = """
    Screen {
        layout: vertical;
    }

    #body {
        height: 1fr;
    }

    #left {
        width: 2fr;
        height: 1fr;
    }

    #search {
        dock: top;
        margin: 0;
    }

    #table {
        height: 1fr;
    }

    #details-panel {
        width: 46;
        min-width: 28;
        padding: 1 2;
        border-left: heavy $primary;
        background: $surface;
    }

    #details {
        width: 1fr;
    }

    ConfirmScreen {
        align: center middle;
    }

    #confirm-dialog {
        width: 70;
        max-width: 90%;
        max-height: 90%;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: thick $error;
    }

    #confirm-scroll {
        height: auto;
        max-height: 18;
    }

    #confirm-message {
        width: 1fr;
        height: auto;
        margin-bottom: 1;
    }

    #confirm-buttons {
        height: auto;
        align-horizontal: right;
    }

    #confirm-buttons Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "focus_search", "Search"),
        Binding("space", "toggle_select", "Select"),
        Binding("e", "edit_desktop", "Edit .desktop"),
        Binding("E", "edit_original", "Edit original"),
        Binding("u", "uninstall", "Uninstall"),
        Binding("p", "toggle_packages", "Packages"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("r", "refresh", "Refresh"),
        Binding("escape", "clear_search", "Clear search", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.all_apps: list[AppInfo] = []
        self.apps_by_key: dict[str, AppInfo] = {}
        self.selected_app: AppInfo | None = None
        self.selected_keys: set[str] = set()
        self.row_keys: list[str] = []
        self.include_packages = False
        self.sort_mode = "name"
        self._restore_index = 0
        self._steam_restore_index = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield Input(
                    placeholder="Filter by name, description, package, or source…",
                    id="search",
                )
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            with VerticalScroll(id="details-panel"):
                yield Static(
                    "[dim]Select an application to see details.[/dim]", id="details"
                )
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column("Sel", width=3, key="sel")
        table.add_column("Name", width=28)
        table.add_column("Source", width=16)
        table.add_column("Package / ID", width=24)
        table.add_column("Description", width=50)
        table.focus()
        self.load_apps()

    # ---------------------------------------------------------------- loading ---

    @work(thread=True, exclusive=True)
    def load_apps(self) -> None:
        try:
            apps = discover_apps(self.include_packages)
        except Exception as exc:  # pragma: no cover - defensive
            self.call_from_thread(
                self.notify, f"Discovery failed: {exc}", severity="error"
            )
            self.call_from_thread(setattr, self, "sub_title", "discovery failed")
            return
        self.call_from_thread(self._set_apps, apps)

    def _set_apps(self, apps: list[AppInfo]) -> None:
        self.all_apps = apps
        valid = {app.uid for app in apps}
        self.selected_keys &= valid
        self._update_subtitle()
        restore = self._restore_index
        self._restore_index = 0
        self.refresh_table(restore)
        self.notify(f"Found {len(apps)} applications", timeout=3)

    def _update_subtitle(self) -> None:
        mode = "packages + apps" if self.include_packages else "applications"
        sub_title = f"{len(self.all_apps)} {mode} · sort: {self.sort_mode}"
        if self.selected_keys:
            sub_title += f" · {len(self.selected_keys)} selected"
        self.sub_title = sub_title

    # --------------------------------------------------------------- filtering ---

    def refresh_table(self, restore_index: int = 0) -> None:
        query = self.query_one("#search", Input).value.strip().lower()
        table = self.query_one(DataTable)
        table.clear()
        self.apps_by_key.clear()
        self.selected_app = None
        self.row_keys = []

        visible: list[AppInfo] = []
        for app in self.all_apps:
            haystack = " ".join(
                (app.name, app.source, app.package, app.description)
            ).lower()
            if query and query not in haystack:
                continue
            visible.append(app)

        if self.sort_mode == "size":
            visible.sort(
                key=lambda a: (-a.size_bytes, a.name.lower(), a.source.lower())
            )
        elif self.sort_mode == "source":
            visible.sort(key=lambda a: (a.source.lower(), a.name.lower()))
        else:
            visible.sort(key=lambda a: (a.name.lower(), a.source.lower()))

        for app in visible:
            key = app.uid
            mark = "✓" if key in self.selected_keys else ""
            table.add_row(
                mark,
                app.name,
                app.source,
                app.package,
                _short(app.description),
                key=key,
            )
            self.apps_by_key[key] = app
            self.row_keys.append(key)

        if self.row_keys:
            index = max(0, min(restore_index, len(self.row_keys) - 1))
            table.move_cursor(row=index)
            self._show_details(self.apps_by_key[self.row_keys[index]])
        else:
            self.query_one("#details", Static).update(
                "[dim]No matching applications.[/dim]"
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        self.refresh_table()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.query_one(DataTable).focus()

    def on_data_table_row_highlighted(
        self, event: DataTable.RowHighlighted
    ) -> None:
        key = event.row_key.value
        app = self.apps_by_key.get(key) if key else None
        if app:
            self._show_details(app)

    def _show_details(self, app: AppInfo) -> None:
        self.selected_app = app
        lines = [
            f"[bold]{escape(app.name)}[/bold]",
            f"[dim]{escape(app.source)}[/dim]",
            "",
        ]
        if app.description:
            lines += [escape(app.description), ""]

        fields = [
            ("Package / ID", app.package),
            ("Version", app.version or "—"),
            ("Size", human_size(app.size_bytes)),
        ]
        if app.kind == "flatpak" and app.installation:
            fields.append(("Installation", app.installation))
        if app.steam_appid:
            fields.append(("Steam App ID", app.steam_appid))
        fields.append(("Desktop file", app.desktop_file or "—"))
        if app.exec_cmd:
            fields.append(("Exec", app.exec_cmd))

        for label, value in fields:
            lines.append(f"[bold]{label}:[/bold] {escape(value)}")

        self.query_one("#details", Static).update("\n".join(lines))

    # ------------------------------------------------------------------ actions ---

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        if search.value:
            search.value = ""
        self.query_one(DataTable).focus()

    def action_refresh(self) -> None:
        self._restore_index = self._current_index()
        self.sub_title = "scanning…"
        self.load_apps()

    def action_cycle_sort(self) -> None:
        anchor = self.selected_app.uid if self.selected_app else None
        index = self.SORT_MODES.index(self.sort_mode)
        self.sort_mode = self.SORT_MODES[(index + 1) % len(self.SORT_MODES)]
        self._update_subtitle()
        self.refresh_table()
        if anchor:
            self._focus_key(anchor)
        self.notify(
            f"Sorted by {self.SORT_LABELS[self.sort_mode]}", timeout=2
        )

    def _focus_key(self, key: str) -> None:
        if key not in self.row_keys:
            return
        index = self.row_keys.index(key)
        self.query_one(DataTable).move_cursor(row=index)

    def action_toggle_packages(self) -> None:
        self.include_packages = not self.include_packages
        state = "on" if self.include_packages else "off"
        self.notify(f"All RPM packages: {state}", timeout=3)
        self.sub_title = "scanning…"
        self.load_apps()

    def _current_index(self) -> int:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return 0
        return max(0, min(table.cursor_row, table.row_count - 1))

    def action_toggle_select(self) -> None:
        table = self.query_one(DataTable)
        if not self.row_keys:
            return
        row = self._current_index()
        key = self.row_keys[row]
        app = self.apps_by_key.get(key)
        if not app:
            return
        if key in self.selected_keys:
            self.selected_keys.discard(key)
            table.update_cell(key, "sel", "")
        else:
            self.selected_keys.add(key)
            table.update_cell(key, "sel", "✓")
        self._update_subtitle()
        if row + 1 < len(self.row_keys):
            table.move_cursor(row=row + 1)

    def _run_editor(self, cmd: list[str], target) -> None:
        try:
            with self.suspend():
                subprocess.call(cmd)
        except Exception as exc:  # SuspendNotSupported, OSError, …
            self.notify(f"Could not open editor: {exc}", severity="error")
            return
        self.notify(f"Edited {target}", timeout=3)
        self.sub_title = "scanning…"
        self.load_apps()

    def action_edit_desktop(self) -> None:
        app = self.selected_app
        if not app:
            return
        if app.kind == "package":
            self.notify(
                "This RPM package has no .desktop file.", severity="warning"
            )
            return
        try:
            cmd, target = edit_override(app)
        except OSError as exc:
            self.notify(f"Could not prepare .desktop file: {exc}", severity="error")
            return
        self._run_editor(cmd, target)

    def action_edit_original(self) -> None:
        app = self.selected_app
        if not app:
            return
        if app.kind == "package":
            self.notify(
                "This RPM package has no .desktop file.", severity="warning"
            )
            return
        result = edit_original(app)
        if not result:
            self.notify("This application has no .desktop file.", severity="warning")
            return
        cmd, target = result
        self._run_editor(cmd, target)

    def _target_apps(self) -> list[AppInfo]:
        if self.selected_keys:
            return [app for app in self.all_apps if app.uid in self.selected_keys]
        if self.selected_app:
            return [self.selected_app]
        return []

    def action_uninstall(self) -> None:
        apps = self._target_apps()
        if not apps:
            return
        commands, steam = uninstall_targets(apps)
        if not commands and not steam:
            self.notify("No uninstall action available.", severity="warning")
            return

        if len(apps) == 1:
            title = f"Uninstall [bold]{escape(apps[0].name)}[/bold]?"
            subtitle = (
                f"[dim]{escape(apps[0].source)} — "
                f"{escape(apps[0].package)}[/dim]"
            )
        else:
            title = f"Uninstall [bold]{len(apps)} applications[/bold]?"
            subtitle = "\n".join(
                f"• {escape(app.name)} [dim]({escape(app.source)})[/dim]"
                for app in apps
            )

        extra = ""
        if steam:
            extra = (
                f"\n\n[dim]Steam will receive {len(steam)} separate uninstall "
                "request(s), one at a time. Steam must be running and logged "
                "in to process them.[/dim]"
            )
        message = (
            f"{title}\n\n{subtitle}\n\n"
            f"[bold]$[/bold] {escape(uninstall_hint(apps))}{extra}"
        )
        restore_index = self._current_index()
        self.push_screen(
            ConfirmScreen(message),
            lambda confirmed: self._do_uninstall(
                confirmed, apps, restore_index
            ),
        )

    def _do_uninstall(
        self, confirmed: bool, apps: list[AppInfo], restore_index: int
    ) -> None:
        if not confirmed:
            return
        commands, steam = uninstall_targets(apps)
        return_code = 0
        try:
            if commands:
                with self.suspend():
                    for command in commands:
                        return_code = subprocess.call(command)
                        if return_code != 0:
                            break
        except Exception as exc:
            self.notify(f"Uninstall failed: {exc}", severity="error")
            return

        for app in apps:
            self.selected_keys.discard(app.uid)
        self._update_subtitle()

        if commands:
            if return_code != 0:
                self.notify(
                    f"Command exited with status {return_code}",
                    severity="warning",
                )
            elif len(apps) == 1:
                self.notify(f"Removed {apps[0].name}", timeout=4)
            else:
                self.notify(f"Removed {len(apps)} applications", timeout=4)

        if steam:
            self._dispatch_steam(apps, restore_index)
            return

        self._restore_index = restore_index
        self.sub_title = "scanning…"
        self.load_apps()

    @work(thread=True, exclusive=True, group="steam-uninstall")
    def _dispatch_steam(self, apps: list[AppInfo], restore_index: int) -> None:
        appids = [
            app.steam_appid for app in apps if app.steam_appid
        ]
        names = {
            app.steam_appid: app.name
            for app in apps
            if app.steam_appid
        }
        self.call_from_thread(
            self.notify,
            f"Starting Steam for {len(appids)} game(s)…",
            timeout=4,
        )

        def progress(
            index: int, total: int, appid: str, ok: bool
        ) -> None:
            status = "" if ok else " [failed]"
            self.call_from_thread(
                self.notify,
                f"Steam uninstall {index}/{total}: "
                f"{names.get(appid, appid)}{status}",
                timeout=2,
            )

        sent, failed = steam_uninstall_many(appids, progress=progress)
        if failed:
            self.call_from_thread(
                self.notify,
                f"Sent {len(sent)} of {len(appids)} Steam request(s); "
                f"{len(failed)} failed.",
                severity="warning",
                timeout=6,
            )
        else:
            self.call_from_thread(
                self.notify,
                f"Sent {len(sent)} Steam uninstall request(s); "
                "check Steam for progress.",
                timeout=6,
            )
        self.call_from_thread(self._schedule_steam_refresh, restore_index)

    def _schedule_steam_refresh(self, restore_index: int) -> None:
        self._steam_restore_index = restore_index
        self.set_timer(10.0, self._refresh_after_steam)

    def _refresh_after_steam(self) -> None:
        self._restore_index = self._steam_restore_index
        self.sub_title = "scanning…"
        self.load_apps()


def main() -> None:
    AllAppsApp().run()


if __name__ == "__main__":
    main()
