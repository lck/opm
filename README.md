# odoo-devops-tools

A small set of DevOps utilities for **Odoo deployments** and **local development**.

The main entrypoint is **`odt-env`** — a CLI that provisions and syncs a reproducible Odoo workspace from an **INI** file.

## What `odt-env` does

- Clones and updates Odoo and addon repositories
- Generates a ready-to-run `odoo-server.conf`
- Generates helper scripts (run, test, backup/restore, update, …)
- Optionally provisions a Python virtual environment and a wheelhouse (useful for offline installs / CI builds)

`odt-env` is designed to be **repeatable**: re-running it should converge your workspace to what the INI file describes.

---

## Table of contents

- [Installation](#installation)
- [`odt-env`](#odt-env)
  - [Requirements](#requirements)
  - [Quick start](#quick-start)
  - [Concepts: ROOT vs. DEST_ROOT](#concepts-root-vs-dest_root)
  - [Workspace layout](#workspace-layout)
  - [Configuration (INI)](#configuration-ini)
    - [Sections overview](#sections-overview)
    - [Example configuration](#example-configuration)
    - [Includes (config inheritance)](#includes-config-inheritance)
    - [Variables & interpolation](#variables--interpolation)
    - [`addons_path` behavior](#addons_path-behavior)
    - [Shallow clones (`shallow_clone`)](#shallow-clones-shallow_clone)
  - [Command-line options](#command-line-options)
  - [Python environment & wheelhouse](#python-environment--wheelhouse)
  - [Generated helper scripts](#generated-helper-scripts)
  - [Safety: local changes policy](#safety-local-changes-policy)

---

## Installation

Using `pip`:

```bash
pip install odoo-devops-tools
```

Or using `uv`:

```bash
uv tool install --reinstall odoo-devops-tools
```

Verify:

```bash
odt-env --help
```

---

## `odt-env`

Provision and sync a reproducible Odoo workspace from an INI configuration file.

### Requirements

- **git**
- **uv** (Python package & project manager): https://docs.astral.sh/uv/

Optional (only for some helper scripts):
- PostgreSQL client tools (e.g. `pg_dump`, `psql`) for backup/restore.

### Quick start

**1) Typical local dev (sync + venv + configs/scripts)**

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all --create-venv
```

**2) Sync repositories only (no Python provisioning)**

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all
```

**3) CI / build machine: create wheelhouse (no install into venv)**

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all --create-wheelhouse
```

**4) Offline install from an existing wheelhouse**

```bash
odt-env /path/to/ROOT/odoo-project.ini --create-venv --reuse-wheelhouse
```

**5) Hard rebuild of the venv**

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all --rebuild-venv
```

If you run **no options**, `odt-env` only regenerates configs and helper scripts:

```bash
odt-env /path/to/ROOT/odoo-project.ini
```

### Concepts: ROOT vs. DEST_ROOT

- **ROOT** is where the workspace is *physically created* (repos, venv, wheelhouse, generated files).
  - Default: the directory containing the INI file.
  - Override with `--root` (must point to an existing directory).

- **DEST_ROOT** controls what paths get *embedded* into generated files (configs/scripts).
  - Default: same as ROOT.
  - Override with `--dest-root` (does **not** need to exist on the build machine).

A typical deployment workflow is: build under a temporary directory, but generate configs/scripts that reference the final location on the target host:

```bash
odt-env /path/to/odoo-project.ini --sync-all --create-wheelhouse \
  --root /tmp/build-root \
  --dest-root /srv/odoo/myproject
```

### Workspace layout

`ROOT` is the directory containing `odoo-project.ini` (or the path passed via `--root`).

If you pass `--dest-root`, `odt-env` still writes files under `ROOT`, but **paths embedded in generated files** are based on `DEST_ROOT`.

Default layout:

- `ROOT/odoo/` — Odoo repository
- `ROOT/odoo-addons/<name>/` — addon repositories
- `ROOT/odoo-backups/` — backups directory
- `ROOT/odoo-data/` — data directory (customizable via `[config] data_dir`)
- `ROOT/odoo-configs/` — generated config (e.g. `odoo-server.conf`)
- `ROOT/odoo-scripts/` — generated helper scripts
- `ROOT/odoo-logs/` — runtime logs (created by `instance.sh`)
- `ROOT/venv/` — virtualenv / Python toolchain (when enabled)
- `ROOT/wheelhouse/` — wheel cache for offline installs (when enabled)

---

## Configuration (INI)

### Sections overview

`odt-env` reads an INI file with these sections:

- `[virtualenv]` — Python version, managed Python, requirements, constraints…
- `[odoo]` — Odoo repo + branch
- `[addons.<name>]` — addon repos (repeatable per addon)
- `[config]` — values rendered into `odoo-server.conf` (db, ports, log settings, …)
- `[include]` — optional includes (config inheritance)

### Example configuration

This example provisions an Odoo **18.0** workspace with two addon repositories:

```ini
[virtualenv]
python_version = 3.11
# Optional (default: true): when false, odt-env will NOT install a managed CPython via `uv python install`.
# Instead it will rely on an existing system Python that matches `python_version`.
# managed_python = false
build_constraints =
requirements =
requirements_ignore =

[odoo]
repo = https://github.com/odoo/odoo.git
branch = 18.0
# Optional: If true, keep repo as a shallow, single-branch clone (depth=1). If false (default), do a full clone/fetch.
shallow_clone = true

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = 18.0

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = 18.0

[config]
http_port = 8069
gevent_port = 8072
db_host = 127.0.0.1
db_port = 5432
db_name = sample_odoo18
db_user = sample_odoo18
db_password = sample_odoo18
log_level = debug
max_cron_threads = 0
```

### Includes (config inheritance)

You can split configuration across multiple INI files (for example a shared base + environment-specific overrides).

Rules:
- paths are resolved relative to the INI file that declares the include,
- included files load first; the including file loads last (**later values override earlier ones**),
- prefix a file path with `?` to make it optional (missing file is skipped).

Example:

`odoo-base.ini`:

```ini
[virtualenv]
python_version = 3.11
build_constraints =
requirements =
requirements_ignore =

[odoo]
repo = https://github.com/odoo/odoo.git
branch = 18.0

[config]
http_port = 8069
gevent_port = 8072
db_host = 127.0.0.1
db_port = 5432
db_name = sample_odoo18
db_user = sample_odoo18
db_password = sample_odoo18
```

`odoo-dev.ini`:

```ini
[include]
files = odoo-base.ini

[odoo]
branch = develop

[config]
db_name = sample_odoo18_dev
```

`odoo-test.ini`:

```ini
[include]
files =
  odoo-base.ini
  ?odoo-dev.ini

[config]
db_name = sample_odoo18_test
```

### Variables & interpolation

`odt-env` uses Python **ExtendedInterpolation**, so you can reference values with `${section:option}`.

It also injects workspace path variables into the INI `DEFAULT` scope (available from any section):

- `${ini_dir}` — directory containing the INI file
- `${root_dir}` — workspace root directory
- `${odoo_dir}` — `ROOT/odoo`
- `${addons_dir}` — `ROOT/odoo-addons`
- `${backups_dir}` — `ROOT/odoo-backups`
- `${configs_dir}` — `ROOT/odoo-configs`
- `${config_path}` — full path to the generated `odoo-server.conf`
- `${scripts_dir}` — `ROOT/odoo-scripts`
- `${venv_python}` — full path to the venv Python executable

**Note on `--dest-root`:**
- `odt-env` always builds under filesystem **ROOT**.
- When `--dest-root` is provided, variables like `${root_dir}`, `${odoo_dir}`, `${addons_dir}`, … are evaluated against **DEST_ROOT** **for the `[config]` section** (so generated `odoo-server.conf` uses deployment paths).
- Other sections (`[odoo]`, `[addons.*]`, `[virtualenv]`) continue to use filesystem **ROOT** (so git/venv/wheelhouse operations work locally).
- `${ini_dir}` always points to the directory of the entry INI file on the build machine (useful for includes).

Tip: create your own helper section (e.g. `[vars]`) and reuse it elsewhere:

```ini
[vars]
project = sample_odoo18
branch = 18.0

[virtualenv]
python_version = 3.11
requirements =
  -r ${ini_dir}/requirements-dev.txt

[odoo]
repo = https://github.com/odoo/odoo.git
branch = ${vars:branch}

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:branch}

[config]
db_name = ${vars:project}
data_dir = ${root_dir}/odoo-data/${vars:project}
logfile = ${root_dir}/odoo-logs/${vars:project}.log
```

### `addons_path` behavior

`odt-env` always computes a base `addons_path` for `odoo-server.conf` that includes:
- the Odoo core addons directory (`ROOT/odoo/addons` and/or `ROOT/odoo/odoo/addons`),
- every synced addon repository (`ROOT/odoo-addons/<name>`).

If you set `addons_path` in `[config]`, it **extends** (appends to) the computed base list (it does **not** replace it). Duplicates are removed.

Format:
- comma-separated list and/or multi-line value,
- relative paths are resolved relative to `ROOT` (same as `${root_dir}`).

Example:

```ini
[config]
addons_path =
  odoo-addons/3rd_party_addons,
  ${addons_dir}/extra_addons,
```

### Shallow clones (`shallow_clone`)

By default, repositories are kept as **full clones** (full history, branches, tags). For very large repositories (especially `odoo/odoo`), you can enable a **shallow, single-branch** workflow.

Where to set it:
- `[odoo] shallow_clone = true`
- `[addons.<name>] shallow_clone = true`

Behavior:
- initial clone: `git clone --depth 1 --single-branch --branch <branch> …`
- sync/update: fetch only `origin <branch>` with `--depth 1`, then hard-reset to `origin/<branch>`

Pros:
- faster clone/fetch
- less disk usage

Limitations:
- depth is fixed to **1** (only the branch tip is available),
- operations requiring history (e.g. long `git log`, `git bisect`, `git describe` on older tags) won’t work as expected,
- switching from full → shallow does not automatically drop history; delete the repo directory and re-sync to realize the benefits.

Switching back to full:
- set `shallow_clone = false` (or remove it) and re-run a sync; `odt-env` will unshallow and widen the fetch refspec so later `fetch --all --tags` can pull all branches/tags.

---

## Command-line options

> Tip: The quick-start section covers the most common workflows. Use this section as a reference when you need to fine-tune behavior.

### Paths & outputs

- `--root` — workspace ROOT directory (default: directory containing the INI)
- `--dest-root` — deployment root for paths embedded in generated configs/scripts (default: same as ROOT)
- `--no-configs` — do not generate config files (e.g. `odoo-server.conf`)
- `--no-scripts` — do not generate helper scripts under `ROOT/odoo-scripts/`
- `--no-data-dir` — do not create the Odoo data folder under `ROOT/odoo-data/` (or custom `[config] data_dir`)

### Repository sync

- `--sync-odoo` — sync only `ROOT/odoo`
- `--sync-addons` — sync only `ROOT/odoo-addons/*` (no-op if no `[addons.*]` sections exist)
- `--sync-all` — sync both Odoo + addons

### Python / venv / wheelhouse

- `--create-venv` — create/update `ROOT/venv` and **install** Python dependencies (from wheelhouse)
- `--rebuild-venv` — delete + recreate `ROOT/venv` (implies `--create-venv`)
- `--create-wheelhouse` — build/update the lock + `ROOT/wheelhouse/` **without installing** requirements into venv  
  (may still create/update `ROOT/venv/` as a Python toolchain)
- `--reuse-wheelhouse` — reuse an existing `ROOT/wheelhouse/` and install strictly offline (requires `--create-venv`)
- `--clear-pip-wheel-cache` — remove all items from pip’s wheel cache

---

## Python environment & wheelhouse

Python dependencies are installed into the venv **only** when you pass `--create-venv` (or `--rebuild-venv`).

- venv location: `ROOT/venv`
- Python version: `[virtualenv] python_version`
- tooling: `uv venv` and `uv pip`
- wheelhouse location: `ROOT/wheelhouse/`
- installs use offline mode from wheelhouse (`--offline --no-index`)

### Reuse an existing wheelhouse (offline)

If `ROOT/wheelhouse/` is already prepared:

```bash
odt-env /path/to/odoo-project.ini --create-venv --reuse-wheelhouse
```

This skips lock compilation and wheel building and performs a strict offline install from the existing wheelhouse. You can combine it with `--sync-all/--sync-odoo/--sync-addons` (repo sync still happens; deps install is offline).

### Managed Python install

By default, `odt-env` manages the requested CPython version via `uv`:

- Option: `[virtualenv] managed_python`
- Default: `true`

Behavior:
- `managed_python = true`: when creating `ROOT/venv/`, `odt-env` ensures the requested Python exists by running `uv python install`.
- `managed_python = false`: `odt-env` skips `uv python install` and relies on an already-installed system Python that matches `python_version`.

Example:

```ini
[virtualenv]
python_version = 3.11
managed_python = false
```

---

## Generated helper scripts

Scripts are generated into `ROOT/odoo-scripts/`. All scripts:
- use the generated config file (`ROOT/odoo-configs/odoo-server.conf`),
- forward extra CLI arguments to the underlying command.

### Script list

Linux/macOS:
- `run.sh` — start Odoo in the foreground
- `instance.sh` — manage Odoo as a background service (start/stop/restart/status) and log to `ROOT/odoo-logs/odoo-server.log`
- `test.sh` — run tests
- `shell.sh` — open an interactive Odoo shell
- `initdb.sh` — initialize database
- `backup.sh` — create a timestamped ZIP backup (DB + filestore) into `ROOT/odoo-backups/`
- `restore.sh` — restore a backup ZIP
- `restore_force.sh` — restore and overwrite an existing DB
- `update.sh` — update modules, auto-detecting addons to update using file-content hashes stored in the DB
- `update_all.sh` — force a full upgrade (`-u base`)

Windows:
- `run.bat`, `test.bat`, `shell.bat`, `initdb.bat`, `backup.bat`, `restore.bat`, `restore_force.bat`, `update.bat`, `update_all.bat`

### Common usage

Start the server:

```bash
./odoo-scripts/run.sh
```

```bat
odoo-scripts\run.bat
```

Manage the background instance (Linux only):

```bash
./odoo-scripts/instance.sh start
./odoo-scripts/instance.sh status
./odoo-scripts/instance.sh restart
./odoo-scripts/instance.sh stop
```

Update modules:

```bash
./odoo-scripts/update.sh
```

```bat
odoo-scripts\update.bat
```

Run tests:

```bash
./odoo-scripts/test.sh -u base
./odoo-scripts/test.sh -u mail,web
./odoo-scripts/test.sh -u my_custom_addon
```

```bat
odoo-scripts\test.bat -u base
odoo-scripts\test.bat -u mail,web
odoo-scripts\test.bat -u my_custom_addon
```

Backup / restore:

```bash
./odoo-scripts/backup.sh
./odoo-scripts/restore.sh PATH/TO/BACKUP.zip
./odoo-scripts/restore_force.sh PATH/TO/BACKUP.zip
```

```bat
odoo-scripts\backup.bat
odoo-scripts\restore.bat PATH\TO\BACKUP.zip
odoo-scripts\restore_force.bat PATH\TO\BACKUP.zip
```

---

## Safety: local changes policy

If any target repository (Odoo or an addon) contains local uncommitted changes (including untracked files), `odt-env` aborts. Commit/stash/clean your working tree before running `odt-env`.
