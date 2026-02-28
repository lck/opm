# odoo-devops-tools

A small set of DevOps utilities for **local Odoo development** and **simple Odoo deployments**.

The main entry point is **`odt-env`**, a CLI that provisions an Odoo workspace from a **single project file**.

## Main features

- **Clone and update** Odoo and addon repositories
- **Provision** a Python virtual environment and automatically install Python dependencies from addons
- **Generate** helper scripts for running, testing, updating, shell access, database initialization, backup, and restore

---

## Requirements

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

Verify:

```bash
odt-env --help
```

---

## Usage

> All examples assume that PostgreSQL is running on `127.0.0.1`, listening on the default port `5432` and that PostgreSQL role `odoo` already exists.
> 
> If your setup is different, update the relevant db_* settings in the project file:
>
> ```ini
> [config]
> db_host = 127.0.0.1
> db_port = 5432
> db_user = odoo
> db_password = odoo
> ```

### 1. Minimal example

This is the minimal example for provisioning a workspace with Odoo 18.

#### 1.1. Create a project file

Create a file named `odoo-project.ini`.

```ini
[virtualenv]
python_version = 3.11

[odoo]
repo = https://github.com/odoo/odoo.git
branch = 18.0

[config]
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
│   ├── restore_force.sh  # restore a backup and overwrite an existing database
│   ├── update.sh         # update modules, auto-detecting addons to update using file-content hashes stored in the DB
│   └── update_all.sh     # force a full upgrade (-u base)
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
python_version = 3.11

[odoo]
repo = https://github.com/odoo/odoo.git
branch = 18.0

[addons.oca-web]
repo = https://github.com/OCA/web.git
branch = 18.0

[addons.oca-helpdesk]
repo = https://github.com/OCA/helpdesk.git
branch = 18.0

[addons.my-custom-addons]
path = odoo-addons/my-custom-addons

[config]
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

Example:

```ini
[addons.my-custom-addons-git]
repo = https://github.com/example/my-custom-addons.git
branch = 18.0
shallow = false
```

#### 2.4. Update database and run Odoo

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

Disable managed Python by adding `managed_python = false` to the `odoo-project.ini` file.

```ini
[virtualenv]
python_version = 3.11
managed_python = false
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
