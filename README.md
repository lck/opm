# odoo-devops-tools

A small set of utilities for **local Odoo development** and **simple Odoo deployments**.

The main entry point is **`odt-env`**, a CLI that provisions an Odoo workspace from a **single project file**.

---

## System requirements

- **git**: https://git-scm.com/install/
- **uv** (Python package & project manager): https://docs.astral.sh/uv/getting-started/installation/

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

---

## Usage

### 1. Minimal example

This is the minimal example for provisioning a workspace with Odoo 18.

#### 1.1. Create a project file

Create a file named `odoo-project.ini`.

> **Note**
> odoo-project.ini is only an example filename used in this README.
> The project file can have a different name.

```ini
[virtualenv]
requirements =
  lxml>=6

[odoo]
version = 18.0

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

#### 1.2. Create the workspace from the project file

Run `odt-env` against the project file:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

After provisioning, the workspace has the following structure:

```text
ROOT/
├── odoo-project.ini      # project definition
├── odoo/                 # Odoo source repository
├── odoo-addons/          # addon repositories from [addons.<name>] sections; unused in this minimal example
├── odoo-backups/         # backups created by helper scripts
├── odoo-configs/         # generated configuration, including odoo-server.conf
├── odoo-data/            # Odoo data directory
├── odoo-logs/            # runtime logs
├── odoo-scripts/         # generated helper scripts
│   ├── run.sh            # start Odoo in the foreground
│   ├── instance.sh       # manage Odoo as a background service (start|stop|restart|status)
│   ├── test.sh           # run Odoo tests
│   ├── shell.sh          # open an Odoo shell
│   ├── initdb.sh         # initialize the configured database
│   ├── backup.sh         # create a timestamped ZIP backup in ROOT/odoo-backups/
│   ├── restore.sh        # restore a backup into the configured database
│   ├── update.sh         # update modules, auto-detecting addons to update using file-content hashes stored in the DB
├── venv/                 # Python virtual environment
└── wheelhouse/           # wheelhouse for offline installs
```

#### 1.3. Initialize database and start Odoo

When the workspace is ready, initialize Odoo database:

```bash
./odoo-scripts/initdb.sh
```

Then start Odoo:

```bash
./odoo-scripts/run.sh
```

On Windows, use the `.bat` variants instead:

```bat
odoo-scripts\initdb.bat
odoo-scripts\run.bat
```

The server starts with the generated configuration from `ROOT/odoo-configs/odoo-server.conf`.

After the server starts, Odoo is available at http://localhost:8069.

---

### 2. Adding extra addons from Git and local folders

To extend Odoo with additional functionality, you can add extra addons through `[addons.<name>]` sections.

In this example, we add two addon repositories, `OCA/web` and `OCA/helpdesk`, and one local folder, `odoo-addons/my-custom-addons`, containing custom Odoo addons.

#### 2.1. Update the project file

Add the extra addons to the `odoo-project.ini` file.

```ini
[virtualenv]
requirements =
  lxml>=6

[odoo]
version = 18.0

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = ${odoo:version}

[addons.my-custom-addons]
path = odoo-addons/my-custom-addons

[config]
db_host = 127.0.0.1
db_name = odoo
db_user = odoo
db_password = odoo
```

#### 2.2. Update the workspace

After changing the project file, run `odt-env` again to update the workspace:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

This clones the Git-based addons into `ROOT/odoo-addons/oca-web/` and `ROOT/odoo-addons/oca-helpdesk/`.

Both Git-based addon directories and the local folder `ROOT/odoo-addons/my-custom-addons/` are then added to the generated `addons_path`.

If any of these addon sources contains a `requirements.txt` file, `odt-env` automatically installs the listed dependencies into the Python virtual environment.

#### 2.3. Optional: Use full clones instead of shallow clones

By default, `odt-env` uses shallow, single-branch clones for Git repositories.

In most cases, shallow clones are the right choice, especially for third-party addons and for the main Odoo repository.

A full clone usually only makes sense for custom addons that are actively being developed, where access to the full Git history is useful.

If you need the full Git history, set `shallow = false` in the relevant section and run `odt-env` again with a sync option.

If you set `commit`, `odt-env` automatically ignores `shallow` and fetches enough history to check out the requested commit.

Example:

```ini
[addons.my-custom-addons]
repo = https://github.com/example/my-custom-addons.git
branch = 18.0
shallow = false
```

#### 2.4. Optional: Pin Odoo or an addon to a specific commit

By default, git repositories are tracked by branch.

If you need a reproducible workspace tied to an exact Git revision, you can also specify `commit` in the relevant `[odoo]` or `[addons.<name>]` section.

Example for Odoo:

```ini
[odoo]
version = 18.0
repo = https://github.com/odoo/odoo.git
branch = 18.0
commit = e6ec487
```

Example for an addon repository:

```ini
[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}
commit = abcdef1
```

> **Note**
> when `commit` is set, `shallow` is ignored automatically, because a shallow clone may not contain the requested commit.

After changing the project file, run `odt-env` again to update the workspace:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

### 2.5. Update database and run Odoo

Once the workspace has been updated, refresh installed modules:

```bash
./odoo-scripts/update.sh
```

Then start Odoo:

```bash
./odoo-scripts/run.sh
```

---

### 3. Using system Python instead of managed Python

By default, `odt-env` uses `uv` to install and manage the requested Python version.

If you already have a suitable system Python installed, you can disable managed Python.

#### 3.1. Update the project file

Disable managed Python by adding `python_version = 3.11` and `managed_python = false` to the `odoo-project.ini` file.

> **Note**
> Set `python_version` to the Python version you want to use from your local system.
> In the example below, 3.11 is only illustrative.

```ini
[virtualenv]
python_version = 3.11
managed_python = false
requirements =
  lxml>=6
```

#### 3.2. Update the workspace

After changing the project file, run `odt-env` again to update the workspace:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

This recreates the virtual environment at `ROOT/venv` using the system Python.

---

### 4. Simple offline deployment using a prebuilt wheelhouse

This example shows a simple deployment workflow:

1. On an internet-connected build machine, prepare the workspace and build the wheelhouse.
2. Copy the prepared workspace to the target machine.
3. On the target machine, recreate the virtual environment strictly offline from the existing wheelhouse.

#### 4.1. Prepare the workspace on the build machine

On the build machine, run `odt-env` normally:

```bash
odt-env odoo-project.ini --sync-all --create-venv
```

This syncs Odoo and addon repositories, resolves and locks Python dependencies, and builds `ROOT/wheelhouse/` for offline installation.

After that, transfer the prepared workspace to the target machine. The simplest approach is to copy the entire `ROOT/` directory.

#### 4.2. Recreate the virtual environment on the target machine

On the target machine, run:

```bash
odt-env /path/to/odoo-project.ini --create-venv-from-wheelhouse
```

This recreates `ROOT/venv`, skips lock compilation and wheelhouse build, and performs a strict offline install from the existing `ROOT/wheelhouse/`.

This is useful for simple deployments where Python dependencies are prepared on a connected build machine, while the target machine creates the virtual environment without internet access.

---

## Command-line reference

### Paths and outputs

- `--root` — workspace root directory (default: the directory containing the INI file)
- `-e KEY=VALUE`, `--extra-var KEY=VALUE` — override or inject a value in the optional `[vars]` section; can be repeated
- `--no-configs` — do not generate config files
- `--no-scripts` — do not generate helper scripts under `ROOT/odoo-scripts/`
- `--no-data-dir` — do not create the Odoo data directory

### Repository sync

- `--sync-odoo` — sync only `ROOT/odoo`
- `--sync-addons` — sync only `ROOT/odoo-addons/*`
- `--sync-all` — sync both Odoo and addons

> **Note**
> If any target repository contains local uncommitted changes, `odt-env` aborts the sync operation.
> Commit, stash, or discard the changes before running a sync command.

### Python, virtual environment, and wheelhouse

- `--create-venv` — recreate `ROOT/venv` and refresh the wheelhouse; if `ROOT/venv` already exists, it is deleted and created again
- `--create-venv-from-wheelhouse` — recreate `ROOT/venv` from an existing `ROOT/wheelhouse/` and `all-requirements.lock.txt`, install strictly offline, and skip lock compilation and wheelhouse build
- `--clear-pip-wheel-cache` — remove all items from pip's wheel cache

---

## Project file reference

The `odt-env` project file is an INI file that describes the Odoo workspace to create.

At minimum, the project file must contain these sections:

- `[odoo]`
- `[config]`

The following sections are supported:

- `[vars]` — optional reusable variables for INI interpolation
- `[virtualenv]` — optional Python and dependency settings
- `[odoo]` — required Odoo source settings
- `[addons.<name>]` — optional addon sources
- `[config]` — required Odoo server configuration values

### General rules

- The project file can have any filename. In this README, `odoo-project.ini` is only an example.
- INI interpolation is supported, so values such as `${odoo:version}` can be reused across sections.
- The optional `[vars]` section is useful for reusable values referenced as `${vars:name}`.
- Values from `[vars]` can be overridden from the CLI with `-e name=value` / `--extra-var name=value`.
- Multi-line values are used for lists such as `requirements`, `build_constraints`, and `requirements_ignore`.

### `[vars]`

This section is optional.

Use it for reusable values that you want to interpolate in other sections.

A major advantage of `[vars]` is that its values can also be overridden directly from the CLI with `-e KEY=VALUE` / `--extra-var KEY=VALUE`. This makes it easy to keep a single project file and adjust things like Odoo version, branch, commit, or database name per run without editing the file.

Example:

```ini
[vars]
branch = 18.0
db = odoo

[odoo]
version = 18.0
branch = ${vars:branch}

[config]
db_name = ${vars:db}
db_user = odoo
db_password = odoo
```

CLI override example:

```bash
odt-env odoo-project.ini --sync-all --create-venv -e branch=dev -e db=odoo_dev
```

### `[virtualenv]`

This section is optional.

- `python_version` — Python version for the virtual environment. If omitted, `odt-env` chooses a default version based on the selected Odoo version.
- `managed_python` — whether `uv` should install and manage Python automatically. Default: `true`.
- `requirements` — additional Python requirements to install. Multi-line list.
- `build_constraints` — additional build constraints used during dependency compilation. Multi-line list.
- `requirements_ignore` — package names to ignore when collecting requirements from addon repositories. Multi-line list.

Example:

```ini
[virtualenv]
python_version = 3.11
managed_python = false
build_constraints =
  setuptools<82
requirements =
  lxml>=6
  requests
requirements_ignore =
  babel
```

### `[odoo]`

This section is required.

- `version` — Odoo version in `X.0` format, for example `18.0`. Required.
- `repo` — Git repository URL for Odoo. Default: the official Odoo repository.
- `branch` — Git branch to check out. Default: the same value as `version`.
- `commit` — optional Git commit to check out after fetching the selected branch. When set, the repository is pinned to that exact revision.
- `shallow` — whether to use a shallow clone. Default: `true`. Ignored when `commit` is set.

Example:

```ini
[odoo]
version = 18.0
repo = https://github.com/odoo/odoo.git
branch = 18.0
commit = e6ec487
shallow = true
```

### `[addons.<name>]`

Addon sections are optional. You can define as many as needed.

Each addon must use exactly one of these source types:

- local addon path: `path`
- git repository: `repo` + `branch` (+ optional `commit` and `shallow`)

Rules:

- For a local addon, use only `path`.
- For a git addon, `repo` and `branch` are required.
- `commit` is optional for a git addon. When set, the repository is pinned to that exact revision.
- `shallow` is optional for git addons and defaults to `true`. It is ignored when `commit` is set.
- Relative local paths are resolved relative to `ROOT/`.
- Git-based addons are cloned into `ROOT/odoo-addons/<name>/`.
- All configured addon directories are automatically appended to the generated `addons_path`.

Examples:

```ini
[addons.my-custom-addons]
path = odoo-addons/my-custom-addons

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = ${odoo:version}
commit = abcdef1
```

### `[config]`

This section is required.

It contains Odoo server configuration values written into `ROOT/odoo-configs/odoo-server.conf`.

You can define standard Odoo configuration options here.

Special rules:

- `addons_path` must not be set in `[config]`. `odt-env` always generates it automatically.
- `data_dir` may be set in `[config]`. If provided, it overrides the default data directory location.

Example:

```ini
[config]
db_host = 127.0.0.1
db_port = 5432
db_name = odoo
db_user = odoo
db_password = odoo
http_port = 8069
```

---

## Script reference

All generated scripts are available in both Unix (`.sh`) and Windows (`.bat`) variants.
The examples below use the Unix form.

### run

Starts Odoo in the foreground.

Any extra arguments are forwarded to the underlying command `odoo-bin`.

Examples:

```bash
./odoo-scripts/run.sh
./odoo-scripts/run.sh --dev=all
```

### instance

Manages Odoo as a background service on Unix-like systems.

Logs are written to `ROOT/odoo-logs/odoo-server.log` and the PID is stored in `ROOT/odoo-logs/odoo-server.pid`.

Examples:

```bash
./odoo-scripts/instance.sh start
./odoo-scripts/instance.sh stop
./odoo-scripts/instance.sh restart
./odoo-scripts/instance.sh status
```

### test

Runs Odoo tests.

The script always adds `--test-enable --stop-after-init`.

Any extra arguments are forwarded to the underlying command `odoo-bin`.

Examples:

```bash
./odoo-scripts/test.sh
./odoo-scripts/test.sh -i sale --test-tags /sale
```

### shell

Opens an Odoo shell.

Examples:

```bash
./odoo-scripts/shell.sh
```

### initdb

Creates or initializes an Odoo database.

The script always adds `--no-demo --no-cache --unless-exists -n <db_name>`.

Any extra arguments are forwarded to the underlying command `click-odoo-initdb` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-initdb-stable) package.

Examples:

```bash
./odoo-scripts/initdb.sh
./odoo-scripts/initdb.sh -m sale,crm
```

### backup

Creates a timestamped ZIP backup under `ROOT/odoo-backups/`.

Any extra arguments are forwarded to the underlying command `click-odoo-backupdb` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-backupdb-beta) package.

Examples:

```bash
./odoo-scripts/backup.sh
```

### restore

Restores a backup into the configured database.

The script always adds `--copy --neutralize`.

Any extra arguments are forwarded to the underlying command `click-odoo-restoredb` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-restoredb-beta) package.

Examples:

```bash
./odoo-scripts/restore.sh ./odoo-backups/odoo_20260331_221443.zip
./odoo-scripts/restore.sh ./odoo-backups/odoo_20260331_221443.zip --force
```

### update

Updates an Odoo database automatically detecting addons to update based on a hash of their file content.

Any extra arguments are forwarded to the underlying command `click-odoo-update` from [`click-odoo-contrib`](https://pypi.org/project/click-odoo-contrib/#click-odoo-update-stable) package.

Examples:

```bash
./odoo-scripts/update.sh
./odoo-scripts/update.sh --update-all
```
