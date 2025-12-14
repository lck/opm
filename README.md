# odoo-devops-tools

A set of DevOps tools for Odoo deployments and local dev.

## Installation

Install with pip:

```bash
pip install odoo-devops-tools
```

Or install with uv:

```bash
uv tool install odoo-devops-tools
```

```bash
odt-env --help
```

## Command: `odt-env`

Provision and sync a reproducible Odoo workspace from an INI configuration file.

#### Requirements

- **git**
- **uv** [Python package and project manager](https://docs.astral.sh/uv/)

#### Usage

Full **default** behavior (clone repositories, create venv, generate config and helper scripts):

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all --create-venv
```

By default, `ROOT` is the directory containing the INI file. You can override where the workspace is created with `--root` (must point to an existing directory):

```bash
odt-env /path/to/odoo-project.ini --sync-all --create-venv --root /path/to/NEW-ROOT
```

If you want to **build** the workspace in one location but have the generated configs/scripts refer to a different **deployment root**, use `--dest-root`. This path does **not** need to exist on the build machine (it may only exist on the target host):

```bash
odt-env /path/to/odoo-project.ini --sync-all --create-wheelhouse --root /tmp/build-root --dest-root /srv/odoo/myproject
```

Sync without venv provisioning:

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all
```

Build wheelhouse without venv provisioning:

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all --create-wheelhouse
```

Offline venv provisioning from an existing wheelhouse:

```bash
odt-env /path/to/ROOT/odoo-project.ini --create-venv --reuse-wheelhouse
```

Hard rebuild of the venv:

```bash
odt-env /path/to/ROOT/odoo-project.ini --sync-all --rebuild-venv
```

If no options are specified, odt-env only regenerates config and helper scripts:

```bash
odt-env /path/to/ROOT/odoo-project.ini
```

### What odt-env creates (directory layout)

`ROOT` is the directory containing `odoo-project.ini` (or the path passed via `--root`).

If you pass `--dest-root`, odt-env still creates files under `ROOT`, but **paths embedded inside generated files** are based on `DEST_ROOT`.

- `ROOT/odoo/` - Odoo repository
- `ROOT/odoo-addons/<name>/` - addon repositories
- `ROOT/odoo-backups/` - backups directory
- `ROOT/odoo-data/` - data directory (can be customized via `[config] data_dir` in the INI file).
- `ROOT/odoo-configs/` - generated `odoo-server.conf`
- `ROOT/odoo-scripts/` - helper scripts (see below)
- `ROOT/odoo-logs/` - runtime logs (created by `instance.sh`)
- `ROOT/venv/` - Virtualenv / Python toolchain (created/updated with `--create-venv` / `--rebuild-venv` / `--create-wheelhouse`).
- `ROOT/wheelhouse/` - Local cache of python packages in wheel format (created/updated with `--create-venv` / `--rebuild-venv` / `--create-wheelhouse`)

### Example configuration

This configuration creates a ready-to-run Odoo 18.0 development workspace, including:

- Python 3.11 virtual environment (`ROOT/venv/`)
- Odoo 18.0 checkout (`ROOT/odoo/`)
- Addons checkouts (`ROOT/odoo-addons/oca-web/` and `ROOT/odoo-addons/oca-helpdesk/`)

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

### Using multiple configuration files with inheritance

odt-env supports a lightweight include mechanism so you can split configuration across multiple INI files (for example a shared `base.ini` plus a local override file).

Notes:

- Paths are resolved relative to the INI file that declares the include.
- Included files are loaded first; the including file is loaded last (**later values override earlier ones**).
- Prefix a path with `?` to make it optional (missing file is skipped).

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


### Configuration variables & interpolation

odt-env uses Python's **ExtendedInterpolation**, so you can reference values via `${section:option}` (for example `${vars:branch}`).

In addition, odt-env injects a set of **runtime variables** (workspace paths) into the INI `DEFAULT` scope, so they are available from *any* section in the INI file:

- `${ini_dir}` - directory containing the INI file
- `${root_dir}` - workspace root directory
- `${odoo_dir}` - `ROOT/odoo`
- `${addons_dir}` - `ROOT/odoo-addons`
- `${backups_dir}` - `ROOT/odoo-backups`
- `${configs_dir}` - `ROOT/odoo-configs`
- `${config_path}` - full path to the generated `odoo-server.conf`
- `${scripts_dir}` - `ROOT/odoo-scripts`
- `${venv_python}` - full path to the virtualenv Python executable

**Note on `--dest-root`:**

- odt-env always *builds* under `ROOT` (filesystem workspace).
- When `--dest-root` is provided, values like `${root_dir}`, `${odoo_dir}`, `${addons_dir}` etc. are evaluated against `DEST_ROOT` **for the `[config]` section** (used to render generated files like `odoo-server.conf`).
- Other sections (for example `[odoo]`, `[addons.*]`, `[virtualenv]`) continue to use the filesystem `ROOT` so build-time operations (git/venv/wheelhouse) are unaffected.
- `${ini_dir}` always points to the directory containing the entry INI on the build machine (needed for includes).

Tip: You can define your own helper section (for example `[vars]`) and reuse it across other sections.

Example:

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

### Extending `addons_path`

odt-env always generates an `addons_path` for `odoo-server.conf` that includes:

- Odoo core addons directory (`ROOT/odoo/addons` and/or `ROOT/odoo/odoo/addons`)
- every synced addon repository (`ROOT/odoo-addons/<name>`)

If you specify `addons_path` in the INI `[config]` section, it **extends** (appends to) this computed base list (it does **not** replace it). Duplicates are removed.

Format details:

- you can use a comma-separated list and/or a multi-line value
- relative paths are resolved relative to `ROOT` (same as `${root_dir}`)

Example:

```ini
[config]
addons_path =
  odoo-addons/3rd_party_addons,
  ${addons_dir}/extra_addons,
```


### Shallow clones (`shallow_clone`)

By default, odt-env keeps repositories as **full clones** (all branches + tags + full history). For very large repositories (especially `odoo/odoo`) you can enable a **shallow, single-branch** workflow.

Where to set it:

- `[odoo] shallow_clone = true` (Odoo core repo)
- `[addons.<name>] shallow_clone = true` (per addon repo)

What it does when enabled:

- initial clone: `git clone --depth 1 --single-branch --branch <branch> ...`
- sync/update: fetch only `origin <branch>` with `--depth 1`, then force local `<branch>` to match `origin/<branch>` (hard reset)
- no `git fetch --all`; other branches are not kept locally, and tags are not fetched broadly

Pros:

- much faster clone/fetch
- significantly less disk usage

Limitations / gotchas:

- depth is fixed to **1** (only the tip of the branch is available)
- git operations that require history (for example long `git log`, `git bisect`, `git describe` on older tags/commits) will not work as expected
- if you need to work across multiple branches, keep `shallow_clone = false`
- switching from a full clone to `shallow_clone = true` does **not** automatically drop existing history; to actually get the disk/speed benefits, delete the repo directory (`ROOT/odoo` or `ROOT/odoo-addons/<name>`) and re-sync.

Switching back to a full clone:

Set `shallow_clone = false` (or remove it) and re-run a sync. If the repo is shallow and/or single-branch, odt-env automatically converts it back to full history (unshallow) and widens the `origin` fetch refspec so a subsequent `fetch --all --tags` can pull all remote branches.

### Command-line Options

- `--root` - override workspace ROOT directory (default: directory containing INI)
- `--dest-root` - override DEST_ROOT used for paths embedded in generated configs/scripts (default: same as ROOT; DEST_ROOT does not need to exist on the build machine)
- `--sync-odoo` - sync only `ROOT/odoo`
- `--sync-addons` - sync only `ROOT/odoo-addons/*` (optional; if there are no `[addons.*]` sections in the INI, this is a no-op)
- `--sync-all` - sync both
- `--create-venv` - enable virtualenv provisioning: create/update `ROOT/venv` and **install** Python dependencies (from the wheelhouse).
- `--rebuild-venv` - delete `ROOT/venv` and recreate it (implies `--create-venv`).
- `--reuse-wheelhouse` - reuse an existing `ROOT/wheelhouse/` and install offline only (skip lock/wheel build). Requires `--create-venv`.
- `--create-wheelhouse` - build/update the lock file + `ROOT/wheelhouse/` **without installing** project requirements into the venv.
  (odt-env may create/update `ROOT/venv/` as a Python toolchain.)
- `--clear-pip-wheel-cache` - remove all items from the pip's wheel cache.
- `--no-configs` - don’t generate config files (e.g. `ROOT/odoo-configs/odoo-server.conf`).
- `--no-scripts` - don’t generate helper scripts under `ROOT/odoo-scripts/`.
- `--no-data-dir` - don’t create the Odoo data folder under `ROOT/odoo-data/` (or a custom path set via `[config] data_dir` in the INI file).

If no options are specified, odt-env only regenerates configs and helper scripts.

### Virtualenv

odt-env installs project Python dependencies into the venv only when you pass `--create-venv` (or `--rebuild-venv`).
You can (re)build the `ROOT/wheelhouse/` either as part of venv provisioning, or separately with `--create-wheelhouse`.

- The venv is created/used at: `ROOT/venv`
- Python version is taken from: `[virtualenv] python_version`
- venv + installs are done via: `uv venv` and `uv pip`
- If the requested Python version is not available on the machine and managed Python is enabled, odt-env installs it via `uv python install`.
- Requirements are resolved into a single lock file and installed into the venv from `ROOT/wheelhouse/` using offline mode (`--offline --no-index`).

#### Reusing an existing wheelhouse

If `ROOT/wheelhouse/` is already prepared (wheels + `all-requirements.lock.txt`), you can run (requires `--create-venv`):

```bash
odt-env /path/to/odoo-project.ini --create-venv --reuse-wheelhouse
```

This skips lock compilation and wheel building and does a strict offline install from the wheelhouse.
It also works together with `--sync-all/--sync-odoo/--sync-addons` (repo sync still happens, but python deps are installed
from the existing lock/wheels).


### Managed Python Install

By default, odt-env manages the requested CPython version for you using `uv`.

- Option: `[virtualenv] managed_python`
- Default: `true` (you can omit it)

Behavior:

- `managed_python = true` (default): when odt-env needs to create `ROOT/venv/`, it ensures the requested `python_version` exists by running `uv python install`.
- `managed_python = false`: odt-env **skips** `uv python install` and relies on an already-installed system Python that matches `python_version`. If such Python is not available, venv creation will fail.

Example:

```ini
[virtualenv]
python_version = 3.11
managed_python = false
```


### Safety / local changes policy

If any target repository (Odoo or an addon) contains local uncommitted changes
(including untracked files), odt-env aborts. Commit/stash/clean your working tree
before running odt-env.

### Generated scripts

odt-env generates helper scripts into `ROOT/odoo-scripts/`. All scripts use the generated configuration file (`odoo-configs/odoo-server.conf`) and forward any additional arguments to the underlying command.

#### Linux/macOS

- `ROOT/odoo-scripts/run.sh`
- `ROOT/odoo-scripts/instance.sh`
- `ROOT/odoo-scripts/test.sh`
- `ROOT/odoo-scripts/shell.sh`
- `ROOT/odoo-scripts/initdb.sh`
- `ROOT/odoo-scripts/backup.sh`
- `ROOT/odoo-scripts/restore.sh`
- `ROOT/odoo-scripts/restore_force.sh`
- `ROOT/odoo-scripts/update.sh`
- `ROOT/odoo-scripts/update_all.sh`

#### Windows

- `ROOT/odoo-scripts/run.bat`
- `ROOT/odoo-scripts/test.bat`
- `ROOT/odoo-scripts/shell.bat`
- `ROOT/odoo-scripts/initdb.bat`
- `ROOT/odoo-scripts/backup.bat`
- `ROOT/odoo-scripts/restore.bat`
- `ROOT/odoo-scripts/restore_force.bat`
- `ROOT/odoo-scripts/update.bat`
- `ROOT/odoo-scripts/update_all.bat`

#### 1. Start the Odoo server

Starts the Odoo server.

**Linux/macOS**
```bash
./odoo-scripts/run.sh
```

**Windows**
```bat
odoo-scripts\run.bat
```

#### 1a. Manage the Odoo server instance (Linux only)

Starts/stops Odoo in the background and writes logs into `ROOT/odoo-logs/odoo-server.log` (the `ROOT/odoo-logs/` directory is created automatically if it doesn't exist).

Supported commands: `start`, `stop`, `restart`, `status`.

- `status` prints the **PID** if the server is running (exit code 0), otherwise prints `NOT RUNNING` (exit code 1).
- `start` accepts additional arguments, which are forwarded to `odoo-bin`.

**Linux/macOS**
```bash
./odoo-scripts/instance.sh start
./odoo-scripts/instance.sh status
./odoo-scripts/instance.sh restart
./odoo-scripts/instance.sh stop
```

#### 2. Update modules

Update an Odoo database (odoo -u), automatically detecting addons to
update based on a hash of their file content, compared to the hashes
stored in the database

**Linux/macOS**
```bash
./odoo-scripts/update.sh
```

**Windows**
```bat
odoo-scripts\update.bat
```

#### 3. Update all modules

Force a complete upgrade (-u base)

**Linux/macOS**
```bash
./odoo-scripts/update_all.sh
```

**Windows**
```bat
odoo-scripts\update_all.bat
```

#### 4. Run tests

Runs Odoo tests.

**Linux/macOS**
```bash
./odoo-scripts/test.sh -u base
./odoo-scripts/test.sh -u mail,web
./odoo-scripts/test.sh -u my_custom_addon
```

**Windows**
```bat
odoo-scripts\test.bat -u base
odoo-scripts\test.bat -u mail,web
odoo-scripts\test.bat -u my_custom_addon
```

#### 5. Open an Odoo shell

Opens an interactive Odoo shell.

**Linux/macOS**
```bash
./odoo-scripts/shell.sh
```

**Windows**
```bat
odoo-scripts\shell.bat
```

#### 6. Initialize the database

Create or initialize an Odoo database with pre-installed modules

**Linux/macOS**
```bash
./odoo-scripts/initdb.sh
```

**Windows**
```bat
odoo-scripts\initdb.bat
```

#### 7. Create a database backup

Creates a timestamped ZIP backup of the database into `ROOT/odoo-backups/`.
This script dumps the database using pg_dump.
It also copies the filestore

**Linux/macOS**
```bash
./odoo-scripts/backup.sh
```

**Windows**
```bat
odoo-scripts\backup.bat
```

#### 8. Restore a database backup

Restores a database from a given backup.
Neutralizing a database as well

**Linux/macOS**
```bash
./odoo-scripts/restore.sh PATH/TO/BACKUP.zip
```

**Windows**
```bat
odoo-scripts\restore.bat PATH\TO\BACKUP.zip
```

#### 9. Restore a backup (force)

Same as restore, but overwrites an existing database

**Linux/macOS**
```bash
./odoo-scripts/restore_force.sh PATH/TO/BACKUP.zip
```

**Windows**
```bat
odoo-scripts\restore_force.bat PATH\TO\BACKUP.zip
```
