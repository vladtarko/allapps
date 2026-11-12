# allapps

A terminal UI (TUI) for browsing installed applications, inspecting their disk
usage, editing their `.desktop` launchers, and uninstalling them from one
place.

```
allapps.py      Textual UI and entry point
discovery.py    app discovery, metadata, sizes, uninstall helpers
```

## Features

- Lists GUI applications from RPM packages, Flatpak, AppImage, local
  `.desktop` files, and Steam.
- Shows the installed size in the details pane.
- Filters by name, description, package, or source.
- Sorts by name (default), size (largest first), or source (A–Z).
- Multi-select with `space` and uninstall several apps in one action.
- Edits a user override (`e`) or the original `.desktop` file in place (`E`).
- Optional package mode (`p`) that includes every installed RPM package, even
  ones without a `.desktop` file.
- Uninstalls Steam games through the native Steam client, one request per game.

## Requirements

allapps is written for Fedora-style systems. RPM discovery and removal call
`rpm` and `dnf` directly; other distros would need code changes.

| Component | Required | Used for |
| --- | --- | --- |
| Linux | Yes | Tested on Fedora 44 |
| Python 3.9+ | Yes | Tested with Python 3.14 |
| [Textual](https://github.com/Textualize/textual) (8.1.1 tested) | Yes | TUI framework (installs Rich) |
| `rpm` | Yes | RPM package discovery |
| `dnf` | Yes | RPM uninstall |
| `find` (findutils) | Yes | AppImage search |
| `sudo` | Yes | System Flatpak/RPM removal and `E` |
| `flatpak` | Optional | Flatpak discovery and removal |
| `gio` (glib2) | Optional | Trashing AppImages and `.desktop` files |
| `steam` (native) | Optional | Steam game uninstall |

`sudo` is invoked only for individual actions that need it. Do not run the
whole TUI as root; Steam refuses to run as root.

Steam game removal requires the native `steam` command on `PATH` (for example
the RPM Fusion package). Flatpak Steam is not supported for Steam game
uninstall.

## Installation

### 1. Install system packages

On Fedora:

```sh
sudo dnf install python3 python3-pip rpm dnf5 findutils sudo flatpak glib2
```

`flatpak` and `glib2` are only needed if you use Flatpak apps or want safe
trashing instead of permanent deletion.

### 2. Put allapps somewhere

Place `allapps.py` and `discovery.py` together in a directory, for example:

```sh
mkdir -p ~/scripts/allapps
cd ~/scripts/allapps
```

The two files must stay in the same directory because `allapps.py` imports
`discovery.py`.

### 3. Install Textual

#### Option A — virtual environment (recommended)

```sh
cd ~/scripts/allapps
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install textual
```

Run it with the virtual environment's interpreter:

```sh
.venv/bin/python allapps.py
```

Or activate the environment first:

```sh
source .venv/bin/activate
./allapps.py
```

#### Option B — distro package

```sh
sudo dnf install python3-textual
chmod +x allapps.py
./allapps.py
```

Fedora's Textual package may be older than the tested 8.1.1 release. Use the
virtual environment if you encounter missing or changed APIs.

If `pip` refuses to install outside a virtual environment
(`externally-managed-environment`), use Option A.

### 4. Optional launcher

If Textual is installed for the system Python (Option B), you can symlink the
script:

```sh
mkdir -p ~/.local/bin
ln -sf "$HOME/scripts/allapps/allapps.py" ~/.local/bin/allapps
```

For a virtual environment, create a small wrapper instead:

```sh
mkdir -p ~/.local/bin
cat > ~/.local/bin/allapps <<'EOF'
#!/bin/sh
exec "$HOME/scripts/allapps/.venv/bin/python" \
     "$HOME/scripts/allapps/allapps.py" "$@"
EOF
chmod +x ~/.local/bin/allapps
```

Make sure `~/.local/bin` is on your `PATH`.

## Usage

Run from a terminal:

```sh
./allapps.py
```

Discovery takes a few seconds. The table and details pane update as you move
the cursor. Press `r` to rescan.

### Keyboard shortcuts

| Key | Action |
| --- | --- |
| `/` | Focus the search box |
| `Esc` | Clear the search / return focus to the table |
| `Up` / `Down` | Move the cursor |
| `PageUp` / `PageDown` | Move a page at a time |
| `Home` / `End` | Jump to the top or bottom |
| `space` | Select/deselect the current app and advance |
| `u` | Uninstall selected apps, or the current app if none are selected |
| `s` | Cycle sorting: name → size → source |
| `e` | Edit a user override of the `.desktop` file |
| `E` | Edit the original `.desktop` file (uses `sudo -e` if needed) |
| `p` | Toggle "all RPM packages" mode |
| `r` | Refresh discovery |
| `q` | Quit |

The subtitle shows the active sort mode and the number of selected apps.

### Editing `.desktop` files

- `e` copies the entry into `~/.local/share/applications/` if necessary and
  edits that **user override**. It takes precedence and survives package
  updates.
- `E` edits the exact file allapps found (for example
  `/usr/share/applications/foo.desktop` or a Flatpak export) **in place**. It
  affects all users and may be overwritten by package or Flatpak updates.

The editor is taken from `$VISUAL`, then `$EDITOR`, then falls back to `nano`.

### Sorting

Press `s` to cycle:

1. **name** — case-insensitive, ascending (default)
2. **size** — largest first; unknown sizes last
3. **source** — source name A–Z, then app name

The cursor stays on the same app when the sort mode changes.

## What gets discovered

- **RPM apps** — packages that own a `.desktop` file, found through
  `rpm -qa --filesbypkg`.
- **Flatpak** — apps from `flatpak list --app`, with desktop files from the
  Flatpak export directories.
- **AppImage** — `.AppImage` files found in common locations such as
  `~/Applications`, `~/AppImages`, `~/Apps`, `~/Downloads`, `~/Desktop`,
  `~/.local/bin`, `/opt`, and `/usr/local`, plus any referenced by a user
  `.desktop` file.
- **Local `.desktop`** — desktop entries not owned by an RPM package or
  Flatpak.
- **Steam** — desktop entries using `steam://rungameid/<id>` or
  `Icon=steam_icon_<id>`.

Sizes come from:

- RPM: the package `SIZE` tag.
- Flatpak: `flatpak info --show-size`.
- AppImage: the file size.
- Steam: `libraryfolders.vdf` and `appmanifest_<id>.acf`.
- Local `.desktop`: the referenced executable or the `.desktop` file itself.

If a size cannot be determined, the details pane shows `—`.

Press `p` to include every installed RPM package, including packages without a
desktop entry. These rows can be uninstalled, but `e`/`E` are unavailable
because there is no `.desktop` file.

## Uninstall behavior

The confirmation dialog shows the commands that will run.

- **RPM / package mode** — selected packages are removed together with
  `sudo dnf remove`.
- **Flatpak** — selected apps are removed together with
  `sudo flatpak uninstall --system -y` or
  `flatpak uninstall --user -y`, depending on the installation.
- **AppImage** — the AppImage and its user `.desktop` file are moved to the
  trash with `gio trash`. If `gio` is unavailable, they are deleted with
  `rm -f`.
- **Local `.desktop`** — the `.desktop` file is moved to the trash. The
  application binary itself is not deleted.
- **Steam** — one `steam steam://uninstall/<appid>` request is sent per game.
  Steam has no batch-uninstall CLI, so requests are forwarded sequentially and
  may wait up to about 90 seconds for Steam to start. Steam must be running
  and logged in; confirm any prompts in Steam.

For Steam selections, allapps sends the requests in a background worker and
shows progress notifications. The table refreshes automatically after a short
delay; press `r` to refresh sooner.

## Limitations

- Fedora/dnf-oriented: the RPM uninstall command is hardcoded to `dnf`.
- Only GUI applications are shown by default. Use `p` for all RPM packages.
- Flatpak Steam cannot be used for Steam game uninstall.
- Steam uninstall requests are asynchronous; allapps cannot tell when Steam
  has actually finished removing a game.
- Sizes are the sizes reported by the package managers or on-disk files and
  may not include shared dependencies, saves, or cache data.
- Flatpak sizes require a working `flatpak` command and installed app.

## Troubleshooting

**`ModuleNotFoundError: No module named 'textual'`**

Textual is not installed for the interpreter running the script. Either
activate the virtual environment, run `.venv/bin/python allapps.py`, or install
Textual for the same Python interpreter.

**`error: externally-managed-environment` when using pip**

Fedora blocks system-wide pip installs. Use the virtual environment described
above.

**The app list is empty**

Check that `rpm` and `flatpak` are installed and that you are running as your
normal user. Press `r` to rescan.

**`e` or `E` does not open an editor**

Install an editor such as `nano` or `vim`, or set `EDITOR`:

```sh
export EDITOR=vim
```

**Steam games are not uninstalled**

Make sure the native `steam` command is on `PATH`, start Steam once, and log
in. Steam receives one request per selected game.

**AppImage or `.desktop` removal is permanent**

Install `glib2` (which provides `gio`) so files are moved to the trash instead
of being deleted with `rm -f`.

**Sizes show `—`**

The relevant package manager could not report a size. This is expected for
Steam entries that are not installed and for some local files.

## Removing allapps

Remove the launcher if you created one:

```sh
rm -f ~/.local/bin/allapps
```

Then delete the project directory, including the virtual environment:

```sh
rm -rf ~/scripts/allapps
```

No configuration files or databases are created by the app itself. User
`.desktop` overrides created with `e` live in
`~/.local/share/applications/` and are not removed by the command above.
