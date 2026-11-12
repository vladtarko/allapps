"""Discover installed applications and their sources.

Sources:
  * RPM packages that ship a .desktop entry
  * Flatpak applications
  * AppImages (plus any associated user .desktop entry)
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
USER_APPS = HOME / ".local/share/applications"
SYSTEM_APPS = Path("/usr/share/applications")
LOCAL_APPS = Path("/usr/local/share/applications")
DESKTOP_DIRS = [USER_APPS, LOCAL_APPS, SYSTEM_APPS]

STEAM_PIPE_CANDIDATES = [
    HOME / ".steam/steam.pipe",
    HOME / ".steam/root/steam.pipe",
    HOME / ".local/share/Steam/steam.pipe",
]
STEAM_START_TIMEOUT = 90.0
STEAM_REQUEST_TIMEOUT = 30.0
STEAM_REQUEST_DELAY = 1.0

FLATPAK_EXPORTS = [
    Path("/var/lib/flatpak/exports/share/applications"),
    HOME / ".local/share/flatpak/exports/share/applications",
]



@dataclass
class AppInfo:
    name: str
    source: str
    package: str
    version: str = ""
    description: str = ""
    desktop_file: str | None = None
    exec_cmd: str = ""
    kind: str = "rpm"  # rpm | package | flatpak | appimage | steam | local
    installation: str = ""
    icon: str = ""
    size_bytes: int = 0
    steam_appid: str = ""
    uid: str = ""


def _run(args: list[str], timeout: int = 60) -> str:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
        )
        return proc.stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _clean(value: str) -> str:
    return value.replace("\\n", " ").replace("\\s", " ").strip()


def _first_localized(data: dict[str, str], key: str) -> str:
    if data.get(key):
        return data[key]
    for suffix in ("[en]", "[en_US]", "[en_GB]"):
        if data.get(key + suffix):
            return data[key + suffix]
    for k, v in data.items():
        if k.startswith(key + "[") and v:
            return v
    return ""


def parse_desktop(path: str | Path) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data

    section = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section != "Desktop Entry" or "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = value.strip()
    return data


def _desktop_visible(data: dict[str, str]) -> bool:
    if data.get("Type", "Application") != "Application":
        return False
    for key in ("NoDisplay", "Hidden"):
        if data.get(key, "").strip().lower() == "true":
            return False
    return True


def _rpm_desktop_owners() -> dict[str, str]:
    owners: dict[str, str] = {}
    for line in _run(["rpm", "-qa", "--filesbypkg"], timeout=180).splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[1].endswith(".desktop"):
            owners[parts[1]] = parts[0]
    return owners


def _rpm_all_metadata() -> dict[str, tuple[str, str, str, int]]:
    fmt = "%{NAME}\t%{VENDOR}\t%{VERSION}-%{RELEASE}\t%{SIZE}\t%{SUMMARY}\n"
    out = _run(["rpm", "-qa", "--qf", fmt], timeout=180)
    meta: dict[str, tuple[str, str, str, int]] = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 5:
            name, vendor, version = parts[0], parts[1], parts[2]
            try:
                size = int(parts[3])
            except ValueError:
                size = 0
            summary = "\t".join(parts[4:])
            if vendor == "(none)":
                vendor = ""
            meta[name] = (vendor, version, summary, size)
    return meta


def human_size(value: int) -> str:
    if value <= 0:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def classify_rpm(vendor: str) -> str:
    if not vendor:
        return "RPM (unknown vendor)"
    if vendor.startswith("Fedora Copr - user "):
        return "COPR: " + vendor[len("Fedora Copr - user ") :]
    if vendor == "Fedora Project":
        return "Fedora"
    if "RPM Fusion" in vendor:
        return "RPM Fusion"
    return vendor


def _discover_flatpak() -> tuple[list[AppInfo], set[str]]:
    out = _run(
        [
            "flatpak",
            "list",
            "--app",
            "--columns=application,name,origin,version,installation",
        ],
        timeout=60,
    )
    rows: list[tuple[str, str, str, str, str]] = []
    ids: set[str] = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0].strip():
            continue
        parts += [""] * (5 - len(parts))
        appid, name, origin, version, installation = parts[:5]
        ids.add(appid)
        rows.append((appid, name, origin, version, installation))

    apps: list[AppInfo] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        sizes = list(
            pool.map(lambda row: _flatpak_size(row[0], row[4]), rows)
        )
    for (appid, name, origin, version, installation), size in zip(rows, sizes):
        desktop = _find_flatpak_desktop(appid)
        data = parse_desktop(desktop) if desktop else {}
        description = _clean(_first_localized(data, "Comment")) or _clean(
            _first_localized(data, "GenericName")
        )
        display = name or _clean(_first_localized(data, "Name")) or appid
        apps.append(
            AppInfo(
                name=display,
                source=f"Flatpak ({origin or 'unknown'})",
                package=appid,
                version=version,
                description=description,
                desktop_file=str(desktop) if desktop else None,
                exec_cmd=data.get("Exec", ""),
                kind="flatpak",
                installation=installation,
                icon=data.get("Icon", ""),
                size_bytes=size,
            )
        )
    return apps, ids


def _flatpak_size(appid: str, installation: str) -> int:
    args = ["flatpak", "info", "--show-size"]
    if installation in ("user", "system"):
        args.append(f"--{installation}")
    args.append(appid)
    output = _run(args, timeout=30).strip()
    try:
        return int(output.splitlines()[0])
    except (ValueError, IndexError):
        return 0


def _find_flatpak_desktop(appid: str) -> Path | None:
    candidates = [USER_APPS / f"{appid}.desktop"] + [
        directory / f"{appid}.desktop" for directory in FLATPAK_EXPORTS
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _steam_appid(data: dict[str, str]) -> str:
    for value in (data.get("Exec", ""), data.get("Icon", "")):
        match = re.search(r"(?:steam://rungameid/|steam_icon_)(\d+)", value)
        if match:
            return match.group(1)
    return ""


def _steam_app_sizes() -> dict[str, int]:
    sizes: dict[str, int] = {}
    candidates = [
        HOME / ".local/share/Steam/steamapps/libraryfolders.vdf",
        HOME / ".steam/steam/steamapps/libraryfolders.vdf",
        HOME / ".steam/root/steamapps/libraryfolders.vdf",
    ]
    steamapps_dirs: set[Path] = {
        HOME / ".local/share/Steam/steamapps",
        HOME / ".steam/steam/steamapps",
    }

    for vdf in candidates:
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for match in re.finditer(r'"path"\s+"([^"]+)"', text):
            steamapps_dirs.add(Path(match.group(1)) / "steamapps")
        for match in re.finditer(r'^\s*"(\d+)"\s+"(\d+)"\s*$', text, re.M):
            appid, size = match.group(1), int(match.group(2))
            if size > 0:
                sizes[appid] = max(sizes.get(appid, 0), size)

    for directory in steamapps_dirs:
        if not directory.is_dir():
            continue
        for manifest in directory.glob("appmanifest_*.acf"):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid_match = re.search(r'"appid"\s+"(\d+)"', text)
            size_match = re.search(r'"SizeOnDisk"\s+"(\d+)"', text)
            if appid_match and size_match:
                appid = appid_match.group(1)
                size = int(size_match.group(1))
                if size > 0:
                    sizes[appid] = max(sizes.get(appid, 0), size)
    return sizes


def _local_size(data: dict[str, str], desktop_path: str | Path) -> int:
    try:
        tokens = shlex.split(data.get("Exec", ""))
    except ValueError:
        tokens = []
    for token in tokens:
        token = re.sub(r"%[fFuUdDnNickvm]", "", token)
        if not token.startswith("/"):
            continue
        path = Path(token)
        try:
            if path.is_file():
                return path.stat().st_size
        except OSError:
            continue
    try:
        return Path(desktop_path).stat().st_size
    except OSError:
        return 0


def _discover_desktop_apps(
    flatpak_ids: set[str],
    owners: dict[str, str],
    meta: dict[str, tuple[str, str, str, int]],
    steam_sizes: dict[str, int],
    skip_paths: set[str] | None = None,
) -> list[AppInfo]:
    apps: list[AppInfo] = []
    seen_basenames: set[str] = set()
    skip_paths = skip_paths or set()

    for directory in [d for d in DESKTOP_DIRS if d.is_dir()]:
        for path in sorted(directory.glob("*.desktop")):
            if str(path) in skip_paths:
                continue
            basename = path.name
            if basename in seen_basenames:
                continue
            seen_basenames.add(basename)

            stem = basename[: -len(".desktop")]
            if stem in flatpak_ids:
                continue

            data = parse_desktop(path)
            if not data or not _desktop_visible(data):
                continue
            name = _clean(_first_localized(data, "Name"))
            if not name:
                continue
            comment = _clean(_first_localized(data, "Comment")) or _clean(
                _first_localized(data, "GenericName")
            )

            package = owners.get(str(path))
            if not package:
                for candidate_dir in (SYSTEM_APPS, LOCAL_APPS, USER_APPS):
                    package = owners.get(str(candidate_dir / basename))
                    if package:
                        break

            steam_appid = _steam_appid(data) if not package else ""

            if package:
                vendor, version, summary, size = meta.get(
                    package, ("", "", "", 0)
                )
                apps.append(
                    AppInfo(
                        name=name,
                        source=classify_rpm(vendor),
                        package=package,
                        version=version,
                        description=comment or _clean(summary),
                        desktop_file=str(path),
                        exec_cmd=data.get("Exec", ""),
                        kind="rpm",
                        icon=data.get("Icon", ""),
                        size_bytes=size,
                    )
                )
            elif steam_appid:
                apps.append(
                    AppInfo(
                        name=name,
                        source="Steam",
                        package=f"steam:{steam_appid}",
                        description=comment or "Steam game",
                        desktop_file=str(path),
                        exec_cmd=data.get("Exec", ""),
                        kind="steam",
                        icon=data.get("Icon", ""),
                        size_bytes=steam_sizes.get(steam_appid, 0),
                        steam_appid=steam_appid,
                    )
                )
            else:
                apps.append(
                    AppInfo(
                        name=name,
                        source="Local .desktop",
                        package=str(path),
                        description=comment,
                        desktop_file=str(path),
                        exec_cmd=data.get("Exec", ""),
                        kind="local",
                        icon=data.get("Icon", ""),
                        size_bytes=_local_size(data, path),
                    )
                )
    return apps


def _appimage_paths_in_exec(exec_cmd: str) -> list[str]:
    try:
        tokens = shlex.split(exec_cmd)
    except ValueError:
        tokens = exec_cmd.split()
    paths: list[str] = []
    for token in tokens:
        token = re.sub(r"%[fFuUdDnNickvm]", "", token)
        if token.lower().endswith(".appimage"):
            paths.append(token)
    return paths


def _desktop_files_by_exec() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for directory in (USER_APPS, LOCAL_APPS):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.desktop"):
            data = parse_desktop(path)
            for token in _appimage_paths_in_exec(data.get("Exec", "")):
                mapping[token] = str(path)
    return mapping


def _appimage_name(path: Path) -> str:
    stem = path.name
    if stem.lower().endswith(".appimage"):
        stem = stem[: -len(".AppImage")]
    stem = re.sub(
        r"[-_.](x86_64|amd64|aarch64|arm64|i386|i686)$", "", stem, flags=re.I
    )
    stem = re.sub(r"[-_.]linux.*$", "", stem, flags=re.I)
    return stem or path.stem


def _find_appimage_paths() -> list[Path]:
    paths: set[Path] = set()

    def scan(directories, depth: int) -> None:
        existing = [str(d) for d in directories if Path(d).is_dir()]
        if not existing:
            return
        args = [
            "find",
            *existing,
            "-maxdepth",
            str(depth),
            "-iname",
            "*.AppImage",
            "-type",
            "f",
            "-print",
        ]
        for line in _run(args, timeout=60).splitlines():
            path = Path(line)
            if path.is_file():
                paths.add(path)

    scan([HOME, "/opt", "/usr/local"], 4)
    scan(
        [
            HOME / "Applications",
            HOME / "AppImages",
            HOME / "Apps",
            HOME / "Downloads",
            HOME / "Desktop",
            HOME / ".local/bin",
            "/opt",
            "/usr/local",
        ],
        8,
    )
    return sorted(paths)


def _discover_appimages(desktop_by_exec: dict[str, str]) -> list[AppInfo]:
    apps: list[AppInfo] = []
    for path in _find_appimage_paths():
        desktop = desktop_by_exec.get(str(path))
        data = parse_desktop(desktop) if desktop else {}
        if data:
            name = _clean(_first_localized(data, "Name")) or _appimage_name(path)
            description = _clean(_first_localized(data, "Comment")) or _clean(
                _first_localized(data, "GenericName")
            )
            exec_cmd = data.get("Exec", "")
            icon = data.get("Icon", "")
        else:
            name = _appimage_name(path)
            description = "AppImage"
            exec_cmd = str(path)
            icon = ""
        apps.append(
            AppInfo(
                name=name,
                source="AppImage",
                package=str(path),
                description=description,
                desktop_file=str(desktop) if desktop else None,
                exec_cmd=exec_cmd,
                kind="appimage",
                icon=icon,
                size_bytes=_safe_size(path),
            )
        )
    return apps


def _safe_size(path: str | Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return 0


def discover_apps(include_packages: bool = False) -> list[AppInfo]:
    with ThreadPoolExecutor(max_workers=5) as pool:
        owners_future = pool.submit(_rpm_desktop_owners)
        meta_future = pool.submit(_rpm_all_metadata)
        flatpak_future = pool.submit(_discover_flatpak)
        exec_future = pool.submit(_desktop_files_by_exec)
        steam_future = pool.submit(_steam_app_sizes)
        owners = owners_future.result()
        meta = meta_future.result()
        flatpak_apps, flatpak_ids = flatpak_future.result()
        desktop_by_exec = exec_future.result()
        steam_sizes = steam_future.result()

    desktop_apps = _discover_desktop_apps(
        flatpak_ids,
        owners,
        meta,
        steam_sizes,
        skip_paths=set(desktop_by_exec.values()),
    )
    appimages = _discover_appimages(desktop_by_exec)

    apps = desktop_apps + flatpak_apps + appimages

    if include_packages:
        represented = {a.package for a in apps if a.kind == "rpm"}
        for name in sorted(meta):
            if name in represented or name == "gpg-pubkey":
                continue
            vendor, version, summary, size = meta[name]
            apps.append(
                AppInfo(
                    name=name,
                    source=classify_rpm(vendor),
                    package=name,
                    version=version,
                    description=_clean(summary),
                    desktop_file=None,
                    kind="package",
                    size_bytes=size,
                )
            )

    apps.sort(key=lambda a: (a.name.lower(), a.source.lower()))

    seen: set[str] = set()
    result: list[AppInfo] = []
    for app in apps:
        base = f"{app.kind}|{app.package}|{app.desktop_file or ''}|{app.name}"
        uid = base
        counter = 2
        while uid in seen:
            uid = f"{base}#{counter}"
            counter += 1
        seen.add(uid)
        app.uid = uid
        result.append(app)
    return result


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-").lower()
    return slug or "app"


def _is_under_home(path: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(HOME.resolve())
        return True
    except (ValueError, OSError):
        return False


def user_override_target(app: AppInfo) -> Path:
    """Return a user-writable .desktop file for *app* (creating an override)."""
    USER_APPS.mkdir(parents=True, exist_ok=True)

    if app.desktop_file:
        source = Path(app.desktop_file)
        if source.parent.resolve() == USER_APPS.resolve() and source.exists():
            return source
        target = USER_APPS / source.name
        if not target.exists() and source.exists():
            shutil.copyfile(source, target)
        if target.exists():
            return target

    target = USER_APPS / f"{_slug(app.name)}.desktop"
    if not target.exists():
        exec_cmd = app.exec_cmd or (
            app.package if app.kind == "appimage" else f"/usr/bin/{app.package}"
        )
        target.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={app.name}\n"
            f"Comment={app.description or 'AppImage'}\n"
            f"Exec={exec_cmd}\n"
            f"Path={Path(app.package).parent}\n"
            "Terminal=false\n"
            "Categories=Utility;\n",
            encoding="utf-8",
        )
    return target


def editor_command(path: Path) -> list[str]:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    return shlex.split(editor) + [str(path)]


def edit_override(app: AppInfo) -> tuple[list[str], Path]:
    target = user_override_target(app)
    cmd = editor_command(target)
    if not os.access(target, os.W_OK):
        cmd = ["sudo", "-e", str(target)]
    return cmd, target


def edit_original(app: AppInfo) -> tuple[list[str], Path] | None:
    if not app.desktop_file:
        return None
    target = Path(app.desktop_file)
    if not target.exists():
        return None
    cmd = editor_command(target)
    if not os.access(target, os.W_OK):
        cmd = ["sudo", "-e", str(target)]
    return cmd, target


def _trash_command(path: str | Path) -> list[str]:
    if shutil.which("gio"):
        return ["gio", "trash", str(path)]
    return ["rm", "-f", str(path)]


def uninstall_targets(
    apps: list[AppInfo],
) -> tuple[list[list[str]], list[str]]:
    """Build grouped uninstall commands plus Steam app IDs."""
    rpm: list[str] = []
    flat_system: list[str] = []
    flat_user: list[str] = []
    trash: list[str] = []
    steam: list[str] = []

    for app in apps:
        if app.steam_appid:
            if app.steam_appid not in steam:
                steam.append(app.steam_appid)
            continue
        if app.kind in ("rpm", "package"):
            if app.package not in rpm:
                rpm.append(app.package)
        elif app.kind == "flatpak":
            target = flat_system if app.installation == "system" else flat_user
            if app.package not in target:
                target.append(app.package)
        elif app.kind == "appimage":
            if app.desktop_file and _is_under_home(app.desktop_file):
                trash.append(app.desktop_file)
            trash.append(app.package)
        elif app.kind == "local":
            trash.append(app.desktop_file or app.package)

    commands: list[list[str]] = []
    if rpm:
        commands.append(["sudo", "dnf", "remove", *sorted(rpm)])
    if flat_system:
        commands.append(
            ["sudo", "flatpak", "uninstall", "--system", "-y", *sorted(flat_system)]
        )
    if flat_user:
        commands.append(
            ["flatpak", "uninstall", "--user", "-y", *sorted(flat_user)]
        )
    for path in trash:
        commands.append(_trash_command(path))
    return commands, steam


def _steam_pipe_ready() -> bool:
    return any(path.is_fifo() for path in STEAM_PIPE_CANDIDATES)


def _wait_for_steam(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _steam_pipe_ready():
            return True
        time.sleep(0.5)
    return _steam_pipe_ready()


def _spawn_steam_uri(appid: str) -> bool:
    try:
        subprocess.Popen(
            ["steam", f"steam://uninstall/{appid}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except OSError:
        return False


def _forward_steam_uri(appid: str) -> bool:
    try:
        result = subprocess.run(
            ["steam", f"steam://uninstall/{appid}"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=STEAM_REQUEST_TIMEOUT,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def request_steam_uninstall(appid: str) -> bool:
    return _spawn_steam_uri(appid)


def steam_uninstall_many(
    appids: list[str],
    *,
    delay: float = STEAM_REQUEST_DELAY,
    start_timeout: float = STEAM_START_TIMEOUT,
    progress: Callable[[int, int, str, bool], None] | None = None,
) -> tuple[list[str], list[str]]:
    """Send one Steam uninstall URI per app, waiting for Steam to be ready.

    Steam cannot batch-uninstall from the CLI, so requests are forwarded
    sequentially through the running client.  Returns (sent, failed).
    """
    pending = list(dict.fromkeys(appid for appid in appids if appid))
    total = len(pending)
    if not total:
        return [], []

    sent: list[str] = []
    failed: list[str] = []
    index = 1

    def report(appid: str, ok: bool) -> None:
        if progress is not None:
            progress(index, total, appid, ok)

    if not _steam_pipe_ready():
        first = pending[0]
        started = _spawn_steam_uri(first)
        ready = started and _wait_for_steam(start_timeout)
        if ready:
            sent.append(first)
        else:
            failed.append(first)
        report(first, ready)
        pending = pending[1:]
        index += 1

        if not ready:
            for appid in pending:
                failed.append(appid)
                report(appid, False)
                index += 1
            return sent, failed
        if delay:
            time.sleep(delay)

    for appid in pending:
        ok = _forward_steam_uri(appid)
        if not ok and _steam_pipe_ready():
            ok = _forward_steam_uri(appid)
        if ok:
            sent.append(appid)
        else:
            failed.append(appid)
        report(appid, ok)
        index += 1
        if delay and index <= total:
            time.sleep(delay)

    return sent, failed


def uninstall_hint(apps: AppInfo | list[AppInfo]) -> str:
    targets = [apps] if isinstance(apps, AppInfo) else list(apps)
    commands, steam = uninstall_targets(targets)
    lines = [" ".join(shlex.quote(part) for part in cmd) for cmd in commands]
    for appid in steam:
        lines.append(f"steam steam://uninstall/{appid}")
    if not lines:
        return "No uninstall action available."
    return "\n".join(lines)
