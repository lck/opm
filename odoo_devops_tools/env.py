#!/usr/bin/env python3
"""
odt-env

Provision and sync a reproducible Odoo workspace from an INI configuration file
"""

from __future__ import annotations

import argparse
import configparser
import io
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from . import __version__

_logger = logging.getLogger("odt-env")

_DEFAULT_REQUIREMENTS = [
    "pip",
    "setuptools",
    "wheel",
    "click-odoo-contrib",
]

_SENSITIVE_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey", "private_key")


# -----------------------------
# Data models
# -----------------------------

@dataclass(frozen=True)
class RepoSpec:
    repo: str
    branch: str
    # If True, keep repo as a shallow, single-branch clone (depth=1).
    # If False (default), do a full clone/fetch.
    shallow_clone: bool = False


@dataclass(frozen=True)
class VirtualenvConfig:
    python_version: str
    build_constraints: list[str]
    requirements: list[str]
    requirements_ignore: list[str]
    managed_python: bool = True


@dataclass(frozen=True)
class ProjectConfig:
    virtualenv: VirtualenvConfig
    odoo: RepoSpec
    addons: Dict[str, RepoSpec]
    config: Dict[str, Any]


@dataclass(frozen=True)
class Layout:
    root: Path
    odoo_dir: Path
    addons_root: Path
    backups_dir: Path
    configs_dir: Path
    conf_path: Path
    data_dir: Path
    scripts_dir: Path
    wheelhouse_dir: Path
    run_sh: Path
    instance_sh: Path
    run_bat: Path
    test_sh: Path
    test_bat: Path
    shell_sh: Path
    shell_bat: Path
    initdb_sh: Path
    initdb_bat: Path
    update_sh: Path
    update_bat: Path
    update_all_sh: Path
    update_all_bat: Path
    backup_sh: Path
    backup_bat: Path
    restore_sh: Path
    restore_bat: Path
    restore_force_sh: Path
    restore_force_bat: Path

    @staticmethod
    def from_root(root: Path) -> "Layout":
        odoo_dir = root / "odoo"
        addons_root = root / "odoo-addons"
        backups_dir = root / "odoo-backups"
        configs_dir = root / "odoo-configs"
        conf_path = configs_dir / "odoo-server.conf"
        data_dir = root / "odoo-data"
        scripts_dir = root / "odoo-scripts"
        wheelhouse_dir = root / "wheelhouse"
        run_sh = scripts_dir / "run.sh"
        instance_sh = scripts_dir / "instance.sh"
        run_bat = scripts_dir / "run.bat"
        test_sh = scripts_dir / "test.sh"
        test_bat = scripts_dir / "test.bat"
        shell_sh = scripts_dir / "shell.sh"
        shell_bat = scripts_dir / "shell.bat"
        initdb_sh = scripts_dir / "initdb.sh"
        initdb_bat = scripts_dir / "initdb.bat"
        update_sh = scripts_dir / "update.sh"
        update_bat = scripts_dir / "update.bat"
        update_all_sh = scripts_dir / "update_all.sh"
        update_all_bat = scripts_dir / "update_all.bat"
        backup_sh = scripts_dir / "backup.sh"
        backup_bat = scripts_dir / "backup.bat"
        restore_sh = scripts_dir / "restore.sh"
        restore_bat = scripts_dir / "restore.bat"
        restore_force_sh = scripts_dir / "restore_force.sh"
        restore_force_bat = scripts_dir / "restore_force.bat"
        return Layout(
            root=root,
            odoo_dir=odoo_dir,
            addons_root=addons_root,
            backups_dir=backups_dir,
            configs_dir=configs_dir,
            conf_path=conf_path,
            data_dir=data_dir,
            scripts_dir=scripts_dir,
            wheelhouse_dir=wheelhouse_dir,
            run_sh=run_sh,
            instance_sh=instance_sh,
            run_bat=run_bat,
            test_sh=test_sh,
            test_bat=test_bat,
            shell_sh=shell_sh,
            shell_bat=shell_bat,
            initdb_sh=initdb_sh,
            initdb_bat=initdb_bat,
            update_sh=update_sh,
            update_bat=update_bat,
            update_all_sh=update_all_sh,
            update_all_bat=update_all_bat,
            backup_sh=backup_sh,
            backup_bat=backup_bat,
            restore_sh=restore_sh,
            restore_bat=restore_bat,
            restore_force_sh=restore_force_sh,
            restore_force_bat=restore_force_bat,
        )


# -----------------------------
# Helpers: validation & parsing
# -----------------------------

def _handle_process_output(p, err_msg: str):
    if p.stdout:
        _logger.info(p.stdout)
    if p.stderr:
        _logger.warning(p.stderr)
    if p.returncode != 0:
        raise Exception(err_msg)


def _rmtree(path: Path) -> None:
    """Remove a directory tree (best-effort handling for read-only files on Windows)."""
    if not path.exists():
        return

    def onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            raise

    shutil.rmtree(path, onerror=onerror)


def _require_table(d: Dict[str, Any], key: str) -> Dict[str, Any]:
    v = d.get(key)
    if not isinstance(v, dict):
        raise Exception(f"Missing or invalid [{key}] table in INI.")
    return v


def _require_str(d: Dict[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v.strip():
        raise Exception(f"Missing or invalid '{key}' (expected non-empty string).")
    return v


def _require_int(d: Dict[str, Any], key: str) -> int:
    v = d.get(key)
    if not isinstance(v, int):
        raise Exception(f"Missing or invalid '{key}' (expected integer).")
    return v


def _require_list_str(d: Dict[str, Any], key: str) -> list[str]:
    v = d.get(key)
    if v is None:
        return []
    if not isinstance(v, list) or any((not isinstance(x, str) or not x.strip()) for x in v):
        raise Exception(f"Missing or invalid '{key}' (expected list of non-empty strings).")
    return [x.strip() for x in v]


def _ini_for_audit_log(cp: configparser.ConfigParser) -> str:
    """
    Return a resolved (interpolated) INI representation suitable for audit logging.
    Comments are not preserved by ConfigParser by design.
    """
    # Build a resolved copy (so ${vars:...} etc. is expanded in the log).
    resolved = configparser.ConfigParser(interpolation=None)

    for section in cp.sections():
        if not resolved.has_section(section):
            resolved.add_section(section)

        for option in cp._sections.get(section, {}).keys():
            value = cp.get(section, option, raw=False)  # resolve interpolation
            opt_l = option.lower()
            if any(k in opt_l for k in _SENSITIVE_KEYS):
                value = "******"
            resolved.set(section, option, value)

    buf = io.StringIO()
    resolved.write(buf)
    return buf.getvalue()


# -----------------------------
# Include support
# -----------------------------

_INCLUDE_SECTION = "include"
_INCLUDE_OPTION = "files"


def _split_ini_list(value: str) -> list[str]:
    """Split a multi-line and/or comma-separated INI value into a list of tokens."""
    parts: list[str] = []
    for ln in (value or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        for chunk in ln.split(","):
            chunk = chunk.strip()
            if chunk:
                parts.append(chunk)
    return parts


def _expand_include_token(token: str, runtime_vars: Optional[Dict[str, str]]) -> str:
    """Expand runtime vars like ${ini_dir} in include paths (does NOT evaluate ${section:option})."""
    s = token
    if runtime_vars:
        for k, v in runtime_vars.items():
            if v is None:
                continue
            s = s.replace(f"${{{k}}}", str(v))
    return os.path.expandvars(s)


def _read_ini_with_includes(
        entry_ini: Path,
        runtime_vars: Optional[Dict[str, str]] = None,
) -> tuple[configparser.ConfigParser, list[Path]]:
    """
    Read an INI config with a lightweight include mechanism:

      [include]
      files =
        base.ini
        ?local.ini

    Rules:
      - Paths are resolved relative to the INI that declares the include.
      - Included files are loaded first; the including file overrides them.
      - Prefix a path with '?' to make it optional (missing => skipped).
      - Cycles are detected and reported.
    """
    cp = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
    loaded_order: list[Path] = []
    loaded: set[Path] = set()
    stack: list[Path] = []

    def _resolve_token(token: str, base_dir: Path) -> tuple[Path, bool]:
        raw = (token or "").strip()
        optional = raw.startswith("?")
        if optional:
            raw = raw[1:].strip()

        raw = _expand_include_token(raw, runtime_vars=runtime_vars)
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (base_dir / p).resolve()
        else:
            p = p.resolve()
        return p, optional

    def _load_token(token: str, base_dir: Path) -> None:
        p, optional = _resolve_token(token, base_dir=base_dir)

        if p in loaded:
            return
        if p in stack:
            cycle = " -> ".join([str(x) for x in stack] + [str(p)])
            raise Exception(f"INI include cycle detected: {cycle}")

        if not p.exists():
            if optional:
                _logger.info("Optional included INI not found (skipping): %s", p)
                return
            raise Exception(f"Included INI not found: {p}")
        if not p.is_file():
            raise Exception(f"Included INI path is not a file: {p}")

        stack.append(p)

        # Probe includes without interpolation to avoid requiring runtime/default vars at this stage.
        probe = configparser.ConfigParser(interpolation=None)
        probe.read(p, encoding="utf-8")

        if probe.has_section(_INCLUDE_SECTION) and probe.has_option(_INCLUDE_SECTION, _INCLUDE_OPTION):
            inc_raw = probe.get(_INCLUDE_SECTION, _INCLUDE_OPTION, fallback="")
            for inc in _split_ini_list(inc_raw):
                _load_token(inc, base_dir=p.parent)

        stack.pop()

        read_ok = cp.read(p, encoding="utf-8")
        if not read_ok:
            raise Exception(f"Failed to read INI config: {p}")
        loaded.add(p)
        loaded_order.append(p)

    # Entry INI is required and loaded last (after its includes).
    _load_token(str(entry_ini), base_dir=entry_ini.parent)

    return cp, loaded_order


def load_project_config(
        ini_path: Path,
        runtime_vars: Optional[Dict[str, str]] = None,
        include_runtime_vars: Optional[Dict[str, str]] = None,
) -> ProjectConfig:
    if not ini_path.exists():
        raise Exception(f"INI config not found: {ini_path}")

    include_vars = include_runtime_vars if include_runtime_vars is not None else runtime_vars
    cp, loaded_files = _read_ini_with_includes(ini_path, runtime_vars=include_vars)

    # Inject runtime variables into DEFAULT so ${root_dir} etc. work with ExtendedInterpolation.
    # NOTE: interpolation is resolved on access (cp.get / cp.items), so it is safe to set these
    # after cp.read() but before we access options.
    if runtime_vars:
        for k, v in runtime_vars.items():
            if v is None:
                continue
            cp["DEFAULT"][str(k)] = str(v)

    loaded_label = "\n".join([f"  - {p}" for p in loaded_files])
    _logger.info("Loaded INI stack (resolved) from %s:\n%s\n\nMerged INI (resolved):\n%s", ini_path, loaded_label, _ini_for_audit_log(cp))

    def _require_option(section: str, option: str) -> str:
        if not cp.has_section(section):
            raise Exception(f"Missing INI section: [{section}]")
        if not cp.has_option(section, option):
            raise Exception(f"Missing option '{option}' in section [{section}]")
        return cp.get(section, option)

    def _get_list(section: str, option: str) -> list[str]:
        if not cp.has_section(section) or not cp.has_option(section, option):
            return []
        raw = cp.get(section, option)
        # Multi-line INI values are used to represent lists. Empty value => empty list.
        return [ln.strip() for ln in raw.splitlines() if ln.strip()]

    def _get_bool(section: str, option: str, default: bool = False) -> bool:
        if not cp.has_section(section) or not cp.has_option(section, option):
            return default
        try:
            return cp.getboolean(section, option)
        except ValueError as e:
            raise Exception(
                f"Invalid value for option '{option}' in section [{section}] (expected a boolean like true/false)."
            ) from e

    # Sections expected:
    #   [virtualenv]
    #   [odoo]
    #   [addons.<name>] for each addon
    #   [config]

    if not cp.has_section("virtualenv"):
        raise Exception("Missing INI section: [virtualenv]")

    python_version = cp.get("virtualenv", "python_version", fallback="").strip()
    if not python_version:
        raise Exception("Missing option 'python_version' in section [virtualenv].")

    venv = VirtualenvConfig(
        python_version=python_version,
        build_constraints=_get_list("virtualenv", "build_constraints"),
        requirements=_get_list("virtualenv", "requirements"),
        requirements_ignore=_get_list("virtualenv", "requirements_ignore"),
        managed_python=_get_bool("virtualenv", "managed_python", default=True),
    )

    odoo = RepoSpec(
        repo=_require_option("odoo", "repo"),
        branch=_require_option("odoo", "branch"),
        shallow_clone=_get_bool("odoo", "shallow_clone", default=False),
    )

    # Addons are optional. If there are no [addons.<name>] sections, keep addons empty.
    addons: Dict[str, RepoSpec] = {}
    for sec in cp.sections():
        if sec.startswith("addons."):
            name = sec.split(".", 1)[1]
            addons[name] = RepoSpec(
                repo=_require_option(sec, "repo"),
                branch=_require_option(sec, "branch"),
                shallow_clone=_get_bool(sec, "shallow_clone", default=False),
            )

    if not cp.has_section("config"):
        raise Exception("Missing INI section: [config]")
    config: Dict[str, Any] = {}
    # Only include keys explicitly defined in [config] (exclude DEFAULT/runtime vars).
    for key in cp._sections.get("config", {}).keys():
        config[key] = cp.get("config", key)

    return ProjectConfig(virtualenv=venv, odoo=odoo, addons=addons, config=config)


def require_venv(
        layout: Layout,
        python_version: str,
        reuse_wheelhouse: bool = False,
        managed_python: bool = True,
) -> None:
    venv_dir = layout.root / "venv"

    if not (python_version or "").strip():
        raise Exception("Missing required uv python version (python_version).")

    # Validate that 'uv' exists in PATH before doing anything else.
    if shutil.which("uv") is None:
        print("ERROR: 'uv' command not found in PATH.", file=sys.stderr)
        raise SystemExit(1)

    if venv_dir.exists() and not venv_dir.is_dir():
        raise Exception(f"venv path exists but is not a directory: {venv_dir}")

    if not venv_dir.exists():
        # Install managed python
        if managed_python:
            is_windows = sys.platform.startswith("win")
            if is_windows:
                cpy_tag = f"cpython-{python_version}-windows-x86_64-none"
            else:
                cpy_tag = f"cpython-{python_version}-linux-x86_64-gnu"
            cmd = ["uv", "python", "install", cpy_tag]
            _logger.info(f"Installing managed python {python_version} (x64) with uv: {cpy_tag}")
            p = subprocess.run(
                cmd,
                cwd=str(layout.root),
                text=True,
                capture_output=True,
            )
            _handle_process_output(p, err_msg=(
                f"Failed to install managed python {python_version} (x64) with uv: {cpy_tag}\n"
                f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
                f"{p.stdout}\n{p.stderr}"
            ))

        # Create virtualenv
        _logger.info("Creating virtualenv with uv: %s (python=%s)", venv_dir, python_version)
        cmd = [
            "uv", "venv",
            "-p", python_version,
            str(venv_dir),
        ]
        if not managed_python:
            cmd.extend([
                "--no-managed-python",
            ])
        p = subprocess.run(
            cmd,
            cwd=str(layout.root),
            text=True,
            capture_output=True,
        )
        _handle_process_output(p, err_msg=(
            f"Failed to create virtualenv at: {venv_dir}\n"
            f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
            f"{p.stdout}\n{p.stderr}"
        ))

        # Install seed packages into virtualenv
        if not reuse_wheelhouse:
            venv_py = venv_dir / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
            if not venv_py.exists():
                raise Exception(f"venv python not found at expected path: {venv_py}")
            seed_packages = [
                "pip",
                "setuptools",
                "wheel",
            ]
            _logger.info("Installing seed packages into venv: %s", venv_dir)
            cmd = ["uv", "pip", "install", "-p", str(venv_py), *seed_packages]
            p = subprocess.run(
                cmd,
                cwd=str(layout.root),
                text=True,
                capture_output=True,
            )
            _handle_process_output(p, err_msg=(
                "Failed to install seed packages into venv.\n"
                f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
                f"{p.stdout}\n{p.stderr}"
            ))


# -----------------------------
# Requirements filtering helpers
# -----------------------------

def _canonicalize_project_name(name: str) -> str:
    """Canonicalize a Python distribution name similar to packaging.utils.canonicalize_name."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _strip_inline_comment(line: str) -> str:
    """Remove trailing comments (a '#' preceded by whitespace)."""
    m = re.search(r"\s+#", line)
    return line[: m.start()].rstrip() if m else line.rstrip()


def _extract_req_name_from_spec(spec: str) -> Optional[str]:
    """Best-effort extraction of a requirement project name from a requirement spec line."""
    s = spec.strip()
    if not s:
        return None

    # VCS/URL requirement like: git+...#egg=foo
    if "egg=" in s:
        m = re.search(r"[#&]egg=([^&]+)", s)
        if m:
            return _canonicalize_project_name(m.group(1))

    # Direct reference: name @ https://...
    if "@" in s:
        left, right = s.split("@", 1)
        if left.strip() and right.strip():
            return _canonicalize_project_name(left.strip())

    # Standard requirement: name[extra] >= 1.0 ; markers
    m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", s)
    if m:
        return _canonicalize_project_name(m.group(1))

    return None


def _filter_requirements_file(
        req_path: Path,
        ignore_names: set[str],
        visited: set[Path],
) -> list[str]:
    """Return requirements file content with ignored packages removed. Supports nested -r includes."""
    out_lines: list[str] = []

    try:
        raw_lines = req_path.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        raise Exception(f"Failed to read requirements file: {req_path} ({e})") from e

    for raw in raw_lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out_lines.append(raw)
            continue

        no_comment = _strip_inline_comment(raw)

        # Include other requirement files (inline them so ignore works recursively).
        if no_comment.startswith(("-r ", "--requirement ")):
            parts = no_comment.split(maxsplit=1)
            if len(parts) == 2:
                include_rel = parts[1].strip()
                include_path = (req_path.parent / include_rel).resolve()

                out_lines.append(f"# odt-env: begin include {include_rel}")
                if include_path in visited:
                    out_lines.append(f"# odt-env: skipped recursive include {include_rel}")
                else:
                    visited.add(include_path)
                    out_lines.extend(_filter_requirements_file(include_path, ignore_names, visited=visited))
                out_lines.append(f"# odt-env: end include {include_rel}")
                continue

        # Editable installs: -e <spec> / --editable <spec>
        spec = no_comment.strip()
        if spec.startswith(("-e ", "--editable ")):
            parts = spec.split(maxsplit=1)
            spec = parts[1] if len(parts) == 2 else ""

        name = _extract_req_name_from_spec(spec)
        if name and name in ignore_names:
            out_lines.append(f"# odt-env: skipped (ignored package '{name}'): {raw}")
            continue

        out_lines.append(raw)

    return out_lines


def compile_all_requirements_lock(
        venv_python: Path,
        workspace_root: Path,
        requirement_files: list[Path],
        base_requirements: list[str],
        requirements_ignore: list[str],
        output_lock_path: Path,
        wheelhouse_dir: Path,
        build_constraints_path: Path,
) -> Path:
    """Compile a single lock file from multiple requirements sources using `uv pip compile`.

    - Collects `base_requirements` (packages listed in INI + odt-env defaults)
    - Inlines and filters each requirements.txt (supports nested -r includes)
    - Applies `requirements_ignore` consistently before compilation
    - Writes:
        - ROOT/wheelhouse/all-requirements.in.txt   (input)
        - ROOT/wheelhouse/all-requirements.lock.txt (lock output)
    - Reads:
        - ROOT/wheelhouse/build-constraints.txt

    Returns the path to the generated lock file.
    """
    if shutil.which("uv") is None:
        raise Exception("Required command not found in PATH: uv")

    ignore_set = {_canonicalize_project_name(x) for x in (requirements_ignore or []) if x.strip()}
    wheelhouse_dir.mkdir(parents=True, exist_ok=True)
    in_path = wheelhouse_dir / "all-requirements.in.txt"

    req_lines: list[str] = []
    for req_path in requirement_files:
        if not req_path.exists():
            # Skip silently; the caller may include optional files.
            continue

        try:
            rel = req_path.resolve().relative_to(workspace_root.resolve())
            rel_label = rel.as_posix()
        except Exception:
            rel_label = str(req_path)

        req_lines.append(f"# --- from {rel_label} ---")
        visited = {req_path.resolve()}
        filtered_lines = _filter_requirements_file(req_path.resolve(), ignore_set, visited=visited)
        req_lines.extend(filtered_lines)
        req_lines.append("")

    lines: list[str] = [
        "# This file is generated by odt-env (DO NOT EDIT).",
        "# Source: Odoo + addon repository requirements, plus [virtualenv].requirements and odt-env defaults.",
        "",
    ]

    if base_requirements:
        lines.append("# --- base requirements (from INI + odt-env defaults) ---")
        for spec in base_requirements:
            lines.append(spec)
        lines.append("")

    lines.extend(req_lines)

    in_path.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")

    _logger.info("Compiling lock file with uv: %s -> %s", in_path, output_lock_path)
    cmd = [
        "uv", "pip", "compile",
        "-p", str(venv_python),
        str(in_path),
        "-o", str(output_lock_path),
    ]

    if build_constraints_path.is_file():
        cmd.extend([
            '--build-constraints', str(build_constraints_path),
        ])

    p = subprocess.run(
        cmd,
        cwd=str(workspace_root),
        text=True,
        capture_output=True,
    )

    _handle_process_output(p, err_msg=(
        "Failed to compile requirements lock file.\n"
        f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
        f"Input: {in_path}\n"
        f"Output: {output_lock_path}\n"
        f"{p.stdout}\n{p.stderr}"
    ))

    return output_lock_path


def build_wheelhouse_from_requirements(
        venv_python: Path,
        workspace_root: Path,
        requirements_path: Path,
        wheelhouse_dir: Path,
        build_constraints_path: Path,
        clear_pip_wheel_cache: bool = True,
) -> None:
    """Build wheelhouse."""
    if not requirements_path.exists():
        raise Exception(f"Requirements file not found: {requirements_path}")

    if shutil.which("uv") is None:
        raise Exception("Required command not found in PATH: uv")

    wheelhouse_dir.mkdir(parents=True, exist_ok=True)

    # Clear pip's wheel cache
    if clear_pip_wheel_cache:
        cmd = [
            str(venv_python), "-m", "pip", "cache", "purge",
        ]
        _logger.info("Clearing pip's wheel cache")
        p = subprocess.run(
            cmd,
            cwd=str(workspace_root),
            text=True,
            capture_output=True,
        )
        _handle_process_output(p, err_msg=(
            "Failed to clear pip's wheel cache.\n"
            f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
            f"{p.stdout}\n{p.stderr}"
        ))

    # Install build constraints to virtualenv before creating wheelhouse
    if build_constraints_path.is_file():
        _logger.info(f"Installing build constraints to virtualenv: {build_constraints_path}")
        cmd = [
            "uv", "pip", "install", "-p", str(venv_python),
            "-U", "-r", str(build_constraints_path),
        ]
        p = subprocess.run(
            cmd,
            cwd=str(workspace_root),
            text=True,
            capture_output=True,
        )
        _handle_process_output(p, err_msg=(
            f"Failed to install build constraints to virtualenv: {build_constraints_path}\n"
            f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
            f"{p.stdout}\n{p.stderr}"
        ))

    # Create wheelhouse
    cmd = [
        str(venv_python), "-m", "pip", "wheel",
        "-r", str(requirements_path),
        "-w", str(wheelhouse_dir),
        "--no-deps",
    ]
    _logger.info("Creating wheelhouse: %s -> %s", requirements_path, wheelhouse_dir)
    p = subprocess.run(
        cmd,
        cwd=str(workspace_root),
        text=True,
        capture_output=True,
    )
    _handle_process_output(p, err_msg=(
        "Failed to create wheelhouse.\n"
        f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
        f"{p.stdout}\n{p.stderr}"
    ))


def pip_install_requirements_file(
        venv_python: Path,
        workspace_root: Path,
        requirements_path: Path,
        wheelhouse_dir: Path,
) -> None:
    """Install a requirements.txt file (via `uv pip`). Optionally (re)build wheelhouse first."""
    if not requirements_path.exists():
        raise Exception(f"Requirements file not found: {requirements_path}")

    if shutil.which("uv") is None:
        raise Exception("Required command not found in PATH: uv")

    # Installing requirements from wheelhouse (always offline)
    pip_cmd: list[str] = [
        "uv", "pip", "sync", "-p", str(venv_python),
        "--offline", "--no-index",
        "-f", str(wheelhouse_dir),
        str(requirements_path),
    ]

    _logger.info("Installing requirements from wheelhouse: %s", requirements_path)
    p = subprocess.run(
        pip_cmd,
        cwd=str(workspace_root),
        text=True,
        capture_output=True,
    )
    _handle_process_output(p, err_msg=(
        "Failed to install requirements from wheelhouse.\n"
        f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
        f"{p.stdout}\n{p.stderr}"
    ))


# -----------------------------
# Git operations
# -----------------------------

def _run(cmd: list[str], cwd: Optional[Path] = None) -> str:
    # Log every git command we execute (stdout only; configured in main()).
    if cmd and cmd[0] == "git":
        _logger.info("git: %s (cwd=%s)", " ".join(cmd), str(cwd) if cwd else "<cwd>")

    p = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
    )

    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if cmd and cmd[0] == "git":
        if out:
            _logger.info("git stdout: %s", out)
        if err:
            _logger.info("git stderr: %s", err)
    if p.returncode != 0:
        raise Exception(f"Command failed: {' '.join(cmd)} {p.stdout} {p.stderr}")
    return out


def assert_clean_worktree(repo_dir: Path) -> None:
    _logger.info("assert_clean_worktree: %s", repo_dir)
    out = _run(["git", "status", "--porcelain"], cwd=repo_dir)
    if out.strip():
        raise Exception(
            f"Local changes detected in repository: {repo_dir}\n"
            "You must commit and push your local changes (or clean the working tree) before syncing.\n"
            "Hint: `git status` to inspect, then commit/push or stash/clean as appropriate."
        )


def _is_shallow_repo(repo_dir: Path) -> bool:
    """Return True if the repository is shallow.

    We primarily rely on the presence of `.git/shallow` because it is stable
    across git versions.
    """
    return (repo_dir / ".git" / "shallow").exists()


def _ensure_full_origin_refspec(repo_dir: Path) -> None:
    """Ensure origin is configured to fetch all branches.

    Repos cloned with `--single-branch` may have a restricted refspec. When the
    user switches Odoo to a full clone/fetch, we widen origin's fetch refspec so
    a subsequent `git fetch --all` can actually bring all remote branches.
    """
    wildcard = "+refs/heads/*:refs/remotes/origin/*"

    p = subprocess.run(
        ["git", "config", "--get-all", "remote.origin.fetch"],
        cwd=str(repo_dir),
        text=True,
        capture_output=True,
    )
    existing = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
    if wildcard in existing:
        return

    # Replace whatever refspec was there with a wildcard.
    subprocess.run(
        ["git", "config", "--unset-all", "remote.origin.fetch"],
        cwd=str(repo_dir),
        text=True,
        capture_output=True,
    )
    _run(["git", "config", "--add", "remote.origin.fetch", wildcard], cwd=repo_dir)


def _unshallow_if_needed(repo_dir: Path) -> None:
    """Convert a shallow repo into a full-history repo (if needed)."""
    if not _is_shallow_repo(repo_dir):
        return

    _logger.info("Repository is shallow; converting to full history: %s", repo_dir)
    # `--unshallow` turns the repo into a full clone; safe because we check first.
    _run(["git", "fetch", "--unshallow", "--tags", "origin"], cwd=repo_dir)


def ensure_repo(
        repo_url: str,
        dest: Path,
        branch: Optional[str] = None,
        depth: Optional[int] = None,
        single_branch: bool = False,
        fetch_all: bool = True,
) -> None:
    """
    Ensure a git repository exists at `dest`.

    - If the repo does not exist, it is cloned.
      * If `branch` is provided, the clone will initially checkout that branch.
      * If `single_branch` is True, only that branch will be fetched/kept.
      * If `depth` is provided, the clone will be shallow (depth=N).

    - If the repo exists, it is fetched/updated according to the chosen strategy:
      * fetch_all=True  -> `git fetch --all --prune`
      * fetch_all=False -> fetch only `branch` from origin (optionally shallow)
    """
    _logger.info("ensure_repo: %s -> %s (branch=%s, depth=%s, single_branch=%s, fetch_all=%s)",
                 repo_url, dest, branch, depth, single_branch, fetch_all)

    if dest.exists() and (dest / ".git").exists():
        assert_clean_worktree(dest)

        if fetch_all:
            # If the caller wants a full clone/fetch and the repo is currently shallow
            # or restricted to a single branch, convert it.
            if depth is None:
                _ensure_full_origin_refspec(dest)
                _unshallow_if_needed(dest)
            _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)
            return

        # Fetch only the required branch (useful for shallow/single-branch workflows).
        fetch_cmd: list[str] = ["git", "fetch", "--prune"]
        if depth is not None:
            fetch_cmd += ["--depth", str(depth)]
        fetch_cmd += ["origin"]
        if branch:
            fetch_cmd += [branch]
        _run(fetch_cmd, cwd=dest)
        return

    dest.parent.mkdir(parents=True, exist_ok=True)

    cmd: list[str] = ["git", "clone"]
    if depth is not None:
        cmd += ["--depth", str(depth)]
    if branch is not None:
        cmd += ["--branch", branch]
    if single_branch:
        cmd += ["--single-branch"]
    cmd += [repo_url, str(dest)]
    _run(cmd)


def checkout_branch(dest: Path, branch: str, fetch_all: bool = True, depth: Optional[int] = None) -> None:
    """
    Checkout the requested `branch` in an existing repo.

    - fetch_all=True (default): do a broad fetch (all remotes/branches + tags), then checkout.
      This matches the previous behavior and is suitable for full clones (e.g. addons).

    - fetch_all=False: fetch ONLY `origin/<branch>` (optionally shallow via depth),
      then force local branch `<branch>` to match `origin/<branch>`.
      This is intended for the Odoo repo where we want single-branch + shallow clones.
    """
    _logger.info("checkout_branch: %s @ %s (fetch_all=%s, depth=%s)", dest, branch, fetch_all, depth)
    assert_clean_worktree(dest)

    if fetch_all:
        # If the caller wants a full clone/fetch and the repo is currently shallow
        # or restricted to a single branch, convert it.
        if depth is None:
            _ensure_full_origin_refspec(dest)
            _unshallow_if_needed(dest)
        _run(["git", "fetch", "--all", "--tags", "--prune"], cwd=dest)

        try:
            _run(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=dest)
            _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=dest)
            assert_clean_worktree(dest)
            _run(["git", "pull", "--ff-only"], cwd=dest)
            return
        except:
            pass

        _run(["git", "checkout", branch], cwd=dest)
        return

    # Narrow fetch: only the needed branch, optionally shallow.
    fetch_cmd: list[str] = ["git", "fetch", "--prune"]
    if depth is not None:
        fetch_cmd += ["--depth", str(depth)]
    fetch_cmd += ["origin", branch]
    _run(fetch_cmd, cwd=dest)

    _run(["git", "rev-parse", "--verify", f"origin/{branch}"], cwd=dest)
    _run(["git", "checkout", "-B", branch, f"origin/{branch}"], cwd=dest)
    # Ensure the working tree exactly matches the remote branch (no pull, no extra refs).
    _run(["git", "reset", "--hard", f"origin/{branch}"], cwd=dest)
    assert_clean_worktree(dest)


# -----------------------------
# Odoo config generation
# -----------------------------

def _join_addons_path(paths: Iterable[Path]) -> str:
    return ",".join(str(p) for p in paths)


def _format_conf_value(value: Any) -> str:
    # Render INI-parsed values into an Odoo .conf compatible scalar.
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_format_conf_value(v) for v in value)
    return str(value)


def render_odoo_conf(cfg: Dict[str, Any], layout: Layout, addon_paths: list[Path]) -> str:
    def _parse_addons_path_value(raw: str) -> list[str]:
        # Support either comma-separated or multi-line values.
        parts: list[str] = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            for chunk in ln.split(","):
                chunk = chunk.strip()
                if chunk:
                    parts.append(chunk)
        return parts

    odoo_addons_candidates = [
        layout.odoo_dir / "addons",
        layout.odoo_dir / "odoo" / "addons",
    ]

    # Base addons_path always includes Odoo's addons plus every synced addon repository.
    base_paths: list[Path] = [*odoo_addons_candidates, *addon_paths]

    # Extra addons_path entries from INI should EXTEND (append to) the computed base.
    extra_paths: list[Path] = []
    user_addons_raw = cfg.get("addons_path")
    if isinstance(user_addons_raw, str) and user_addons_raw.strip():
        for token in _parse_addons_path_value(user_addons_raw):
            p = Path(token).expanduser()
            if not p.is_absolute():
                p = (layout.root / p).resolve()
            else:
                p = p.resolve()
            extra_paths.append(p)

    merged: list[str] = []
    seen: set[str] = set()
    for p in [*base_paths, *extra_paths]:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        merged.append(s)
    merged_addons_path = ",".join(merged)

    lines: list[str] = ["[options]"]

    # Write every key from [config] (dynamic; no fixed schema), but treat addons_path specially.
    for key, value in cfg.items():
        if key in ("addons_path", "data_dir"):
            continue
        lines.append(f"{key} = {_format_conf_value(value)}")

    # Always write merged addons_path.
    lines.append(f"addons_path = {merged_addons_path}")

    # Always write data_dir from layout
    lines.append(f"data_dir = {layout.data_dir}")

    return "\n".join(lines) + "\n"


# -----------------------------
# Script generation
# -----------------------------

def write_run_sh(layout: Layout) -> None:
    content = """#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_DIR="${ROOT_DIR}/venv"
PY="${VENV_DIR}/bin/python"
ODOO_BIN="${ROOT_DIR}/odoo/odoo-bin"
CONF="${ROOT_DIR}/odoo-configs/odoo-server.conf"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "ERROR: required venv directory not found at ${VENV_DIR}" >&2
  exit 1
fi
if [[ ! -x "${PY}" ]]; then
  echo "ERROR: venv python not found/executable at ${PY}" >&2
  exit 1
fi
if [[ ! -f "${ODOO_BIN}" ]]; then
  echo "ERROR: odoo-bin not found at ${ODOO_BIN}" >&2
  exit 1
fi

echo "INFO: Starting Odoo server using config ${CONF}. Passing through any extra arguments."
exec "${PY}" "${ODOO_BIN}" -c "${CONF}" "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.run_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.run_sh.stat().st_mode
        layout.run_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_instance_sh(layout: Layout) -> None:
    content = """#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_DIR="${ROOT_DIR}/venv"
PY="${VENV_DIR}/bin/python"
ODOO_BIN="${ROOT_DIR}/odoo/odoo-bin"
CONF="${ROOT_DIR}/odoo-configs/odoo-server.conf"

LOGS_DIR="${ROOT_DIR}/odoo-logs"
LOG_FILE="${LOGS_DIR}/odoo-server.log"
PID_FILE="${LOGS_DIR}/odoo-server.pid"

require_paths() {
  if [[ ! -d "${VENV_DIR}" ]]; then
    echo "ERROR: required venv directory not found at ${VENV_DIR}" >&2
    exit 1
  fi
  if [[ ! -x "${PY}" ]]; then
    echo "ERROR: venv python not found/executable at ${PY}" >&2
    exit 1
  fi
  if [[ ! -f "${ODOO_BIN}" ]]; then
    echo "ERROR: odoo-bin not found at ${ODOO_BIN}" >&2
    exit 1
  fi
  if [[ ! -f "${CONF}" ]]; then
    echo "ERROR: Odoo config not found at ${CONF}" >&2
    exit 1
  fi
}

is_running() {
  if [[ -f "${PID_FILE}" ]]; then
    local pid
    pid="$(cat "${PID_FILE}" 2>/dev/null || true)"
    if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
      echo "${pid}"
      return 0
    fi
  fi
  return 1
}

start() {
  mkdir -p "${LOGS_DIR}"
  require_paths

  local pid
  if pid="$(is_running)"; then
    echo "INFO: Odoo already running (PID=${pid})"
    return 0
  fi

  echo "----- $(date -Is) START -----" >> "${LOG_FILE}"
  nohup "${PY}" "${ODOO_BIN}" -c "${CONF}" "$@" >> "${LOG_FILE}" 2>&1 &

  pid=$!
  echo "${pid}" > "${PID_FILE}"
  echo "INFO: Started Odoo (PID=${pid}). Logging to ${LOG_FILE}"
}

stop() {
  mkdir -p "${LOGS_DIR}"

  local pid
  if pid="$(is_running)"; then
    echo "INFO: Stopping Odoo (PID=${pid})"
    kill "${pid}" 2>/dev/null || true

    # Wait up to ~30 seconds for a graceful shutdown
    for _ in {1..30}; do
      if kill -0 "${pid}" 2>/dev/null; then
        sleep 1
      else
        break
      fi
    done

    if kill -0 "${pid}" 2>/dev/null; then
      echo "WARN: Odoo did not stop gracefully; sending SIGKILL" >&2
      kill -9 "${pid}" 2>/dev/null || true
    fi

    rm -f "${PID_FILE}"
    echo "INFO: Stopped."
    return 0
  fi

  # Cleanup stale PID file (if any)
  rm -f "${PID_FILE}"
  echo "INFO: Odoo not running."
}

status() {
  local pid
  if pid="$(is_running)"; then
    # Requirement: print PID if running
    echo "${pid}"
    return 0
  fi
  echo "NOT RUNNING" >&2
  return 1
}

cmd="${1:-}"
shift || true

case "${cmd}" in
  start)
    start "$@"
    ;;
  stop)
    stop
    ;;
  restart)
    stop
    start "$@"
    ;;
  status)
    status
    ;;
  *)
    echo "Usage: $(basename "$0") {start|stop|restart|status} [odoo args...]" >&2
    exit 2
    ;;
esac
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.instance_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.instance_sh.stat().st_mode
        layout.instance_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_run_bat(layout: Layout) -> None:
    content = r"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set PY=%VENV_DIR%\Scripts\python.exe
set ODOO_BIN=%ROOT_DIR%\odoo\odoo-bin
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%PY%" (
  echo ERROR: venv python not found at %PY%
  exit /b 1
)
if not exist "%ODOO_BIN%" (
  echo ERROR: odoo-bin not found at %ODOO_BIN%
  exit /b 1
)

echo INFO: Starting Odoo server using config %CONF%. Passing through any extra arguments.
"%PY%" "%ODOO_BIN%" -c "%CONF%" %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.run_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_test_sh(layout: Layout) -> None:
    content = """#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_DIR="${ROOT_DIR}/venv"
PY="${VENV_DIR}/bin/python"
ODOO_BIN="${ROOT_DIR}/odoo/odoo-bin"
CONF="${ROOT_DIR}/odoo-configs/odoo-server.conf"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "ERROR: required venv directory not found at ${VENV_DIR}" >&2
  exit 1
fi
if [[ ! -x "${PY}" ]]; then
  echo "ERROR: venv python not found/executable at ${PY}" >&2
  exit 1
fi
if [[ ! -f "${ODOO_BIN}" ]]; then
  echo "ERROR: odoo-bin not found at ${ODOO_BIN}" >&2
  exit 1
fi

echo "INFO: Running Odoo tests using config ${CONF}. Passing through any extra arguments."
exec "${PY}" "${ODOO_BIN}" -c "${CONF}" --test-enable --stop-after-init "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.test_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.test_sh.stat().st_mode
        layout.test_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_test_bat(layout: Layout) -> None:
    content = r"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set PY=%VENV_DIR%\Scripts\python.exe
set ODOO_BIN=%ROOT_DIR%\odoo\odoo-bin
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%PY%" (
  echo ERROR: venv python not found at %PY%
  exit /b 1
)
if not exist "%ODOO_BIN%" (
  echo ERROR: odoo-bin not found at %ODOO_BIN%
  exit /b 1
)

echo INFO: Running Odoo tests using config %CONF%. Passing through any extra arguments.
"%PY%" "%ODOO_BIN%" -c "%CONF%" --test-enable --stop-after-init %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.test_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_shell_sh(layout: Layout) -> None:
    content = """#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

VENV_DIR="${ROOT_DIR}/venv"
PY="${VENV_DIR}/bin/python"
ODOO_BIN="${ROOT_DIR}/odoo/odoo-bin"
CONF="${ROOT_DIR}/odoo-configs/odoo-server.conf"

if [[ ! -d "${VENV_DIR}" ]]; then
  echo "ERROR: required venv directory not found at ${VENV_DIR}" >&2
  exit 1
fi
if [[ ! -x "${PY}" ]]; then
  echo "ERROR: venv python not found/executable at ${PY}" >&2
  exit 1
fi
if [[ ! -f "${ODOO_BIN}" ]]; then
  echo "ERROR: odoo-bin not found at ${ODOO_BIN}" >&2
  exit 1
fi

echo "INFO: Starting Odoo shell using config ${CONF}. Passing through any extra arguments."
exec "${PY}" "${ODOO_BIN}" shell -c "${CONF}" "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.shell_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.shell_sh.stat().st_mode
        layout.shell_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_shell_bat(layout: Layout) -> None:
    content = r"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set PY=%VENV_DIR%\Scripts\python.exe
set ODOO_BIN=%ROOT_DIR%\odoo\odoo-bin
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%PY%" (
  echo ERROR: venv python not found at %PY%
  exit /b 1
)
if not exist "%ODOO_BIN%" (
  echo ERROR: odoo-bin not found at %ODOO_BIN%
  exit /b 1
)

echo INFO: Starting Odoo shell using config %CONF%. Passing through any extra arguments.
"%PY%" "%ODOO_BIN%" shell -c "%CONF%" %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.shell_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_initdb_sh(layout: Layout, db_name: str) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
INITDB_BIN="${{VENV_DIR}}/bin/click-odoo-initdb"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{INITDB_BIN}}" ]]; then
  echo "ERROR: click-odoo-initdb not found/executable at ${{INITDB_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

echo "INFO: Initializing Odoo database '{db_name}' (unless exists; no demo; no cache) using config ${{CONF}}. Passing through any extra arguments."
exec "${{INITDB_BIN}}" -c "${{CONF}}" --no-demo --no-cache --unless-exists --log-level debug -n "{db_name}" "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.initdb_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.initdb_sh.stat().st_mode
        layout.initdb_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_initdb_bat(layout: Layout, db_name: str) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set INITDB_BIN=%VENV_DIR%\Scripts\click-odoo-initdb.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%INITDB_BIN%" (
  echo ERROR: click-odoo-initdb not found at %INITDB_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

echo INFO: Initializing Odoo database "{db_name}" (unless exists; no demo; no cache) using config %CONF%. Passing through any extra arguments.
"%INITDB_BIN%" -c "%CONF%" --no-demo --no-cache --unless-exists --log-level debug -n "{db_name}" %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.initdb_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_backup_sh(layout: Layout, db_name: str) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
BACKUPS_DIR="${{ROOT_DIR}}/odoo-backups"
BACKUP_BIN="${{VENV_DIR}}/bin/click-odoo-backupdb"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

TODAY=$(date +%Y%m%d)
TIME=$(date +%H%M%S)
BACKUP_FILENAME="{db_name}_${{TODAY}}_${{TIME}}.zip"
FULL_BACKUP_PATH="${{BACKUPS_DIR}}/${{BACKUP_FILENAME}}"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -d "${{BACKUPS_DIR}}" ]]; then
  echo "ERROR: required odoo-backups directory not found at ${{BACKUPS_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{BACKUP_BIN}}" ]]; then
  echo "ERROR: click-odoo-backupdb not found/executable at ${{BACKUP_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

echo "INFO: Creating new backup '${{FULL_BACKUP_PATH}}' using config ${{CONF}}. Passing through any extra arguments."
exec "${{BACKUP_BIN}}" -c "${{CONF}}" --format zip "{db_name}" "${{FULL_BACKUP_PATH}}" --log-level debug "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.backup_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.backup_sh.stat().st_mode
        layout.backup_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_backup_bat(layout: Layout, db_name: str) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set BACKUPS_DIR=%ROOT_DIR%\odoo-backups
set BACKUP_BIN=%VENV_DIR%\Scripts\click-odoo-backupdb.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%BACKUPS_DIR%" (
  echo ERROR: required odoo-backups directory not found at %BACKUPS_DIR%
  exit /b 1
)
if not exist "%BACKUP_BIN%" (
  echo ERROR: click-odoo-backupdb not found at %BACKUP_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

REM Build timestamped filename (yyyyMMdd_HHmmss) via PowerShell for reliable formatting
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set TODAY=%%i
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format HHmmss"') do set TIME=%%i

set BACKUP_FILENAME={db_name}_%TODAY%_%TIME%.zip
set FULL_BACKUP_PATH=%BACKUPS_DIR%\%BACKUP_FILENAME%

echo INFO: Creating new backup "%FULL_BACKUP_PATH%" using config %CONF%. Passing through any extra arguments.
"%BACKUP_BIN%" -c "%CONF%" --format zip "{db_name}" "%FULL_BACKUP_PATH%" --log-level debug %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.backup_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_restore_sh(layout: Layout, db_name: str) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
RESTORE_BIN="${{VENV_DIR}}/bin/click-odoo-restoredb"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{RESTORE_BIN}}" ]]; then
  echo "ERROR: click-odoo-restoredb not found/executable at ${{RESTORE_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "ERROR: missing restore source (backup file/path). Provide it as the first argument." >&2
  echo "Example: ./restore.sh /path/to/backup.zip" >&2
  exit 2
fi

echo "INFO: Restoring Odoo database '{db_name}' using config ${{CONF}}. Passing through any extra arguments."
exec "${{RESTORE_BIN}}" -c "${{CONF}}" --copy --neutralize --log-level debug "{db_name}" "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.restore_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.restore_sh.stat().st_mode
        layout.restore_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_restore_bat(layout: Layout, db_name: str) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set RESTORE_BIN=%VENV_DIR%\Scripts\click-odoo-restoredb.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%RESTORE_BIN%" (
  echo ERROR: click-odoo-restoredb not found at %RESTORE_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

if "%~1"=="" (
  echo ERROR: missing restore source ^(backup file/path^). Provide it as the first argument.
  echo Example: restore.bat C:\path\to\backup.zip
  exit /b 2
)

echo INFO: Restoring Odoo database "{db_name}" using config %CONF%. Passing through any extra arguments.
"%RESTORE_BIN%" -c "%CONF%" --copy --neutralize --log-level debug "{db_name}" %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.restore_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_restore_force_sh(layout: Layout, db_name: str) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
RESTORE_BIN="${{VENV_DIR}}/bin/click-odoo-restoredb"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{RESTORE_BIN}}" ]]; then
  echo "ERROR: click-odoo-restoredb not found/executable at ${{RESTORE_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "ERROR: missing restore source (backup file/path). Provide it as the first argument." >&2
  echo "Example: ./restore_force.sh /path/to/backup.zip" >&2
  exit 2
fi

echo "INFO: Restoring Odoo database '{db_name}' using config ${{CONF}}. Passing through any extra arguments."
exec "${{RESTORE_BIN}}" -c "${{CONF}}" --copy --neutralize --force --log-level debug "{db_name}" "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.restore_force_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.restore_force_sh.stat().st_mode
        layout.restore_force_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_restore_force_bat(layout: Layout, db_name: str) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set RESTORE_BIN=%VENV_DIR%\Scripts\click-odoo-restoredb.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%RESTORE_BIN%" (
  echo ERROR: click-odoo-restoredb not found at %RESTORE_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

if "%~1"=="" (
  echo ERROR: missing restore source ^(backup file/path^). Provide it as the first argument.
  echo Example: restore.bat C:\path\to\backup.zip
  exit /b 2
)

echo INFO: Restoring Odoo database "{db_name}" using config %CONF%. Passing through any extra arguments.
"%RESTORE_BIN%" -c "%CONF%" --copy --neutralize --force --log-level debug "{db_name}" %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.restore_force_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_update_sh(layout: Layout) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
UPDATE_BIN="${{VENV_DIR}}/bin/click-odoo-update"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{UPDATE_BIN}}" ]]; then
  echo "ERROR: click-odoo-update not found/executable at ${{UPDATE_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

echo "INFO: Updating Odoo addons using config ${{CONF}}. Passing through any extra arguments."
exec "${{UPDATE_BIN}}" -c "${{CONF}}" --log-level debug "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.update_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.update_sh.stat().st_mode
        layout.update_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_update_bat(layout: Layout) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set UPDATE_BIN=%VENV_DIR%\Scripts\click-odoo-update.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%UPDATE_BIN%" (
  echo ERROR: click-odoo-update not found at %UPDATE_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

echo INFO: Updating Odoo addons using config %CONF%. Passing through any extra arguments.
"%UPDATE_BIN%" -c "%CONF%" --log-level debug %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.update_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


def write_update_all_sh(layout: Layout) -> None:
    content = f"""#!/usr/bin/env bash
set -euo pipefail

# Resolve script directory
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
ROOT_DIR="$(cd "${{SCRIPT_DIR}}/.." && pwd)"

VENV_DIR="${{ROOT_DIR}}/venv"
UPDATE_BIN="${{VENV_DIR}}/bin/click-odoo-update"
CONF="${{ROOT_DIR}}/odoo-configs/odoo-server.conf"

if [[ ! -d "${{VENV_DIR}}" ]]; then
  echo "ERROR: required venv directory not found at ${{VENV_DIR}}" >&2
  exit 1
fi
if [[ ! -x "${{UPDATE_BIN}}" ]]; then
  echo "ERROR: click-odoo-update not found/executable at ${{UPDATE_BIN}}" >&2
  exit 1
fi
if [[ ! -f "${{CONF}}" ]]; then
  echo "ERROR: Odoo config not found at ${{CONF}}" >&2
  exit 1
fi

echo "INFO: Updating all Odoo addons using config ${{CONF}}. Passing through any extra arguments."
exec "${{UPDATE_BIN}}" -c "${{CONF}}" --update-all --log-level debug "$@"
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.update_all_sh.write_text(content, encoding="utf-8")

    try:
        mode = layout.update_all_sh.stat().st_mode
        layout.update_all_sh.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def write_update_all_bat(layout: Layout) -> None:
    content = rf"""@echo off
setlocal enabledelayedexpansion

REM Resolve ROOT directory (parent of this script directory)
set SCRIPT_DIR=%~dp0
if "%SCRIPT_DIR:~-1%"=="\" set SCRIPT_DIR=%SCRIPT_DIR:~0,-1%
for %%I in ("%SCRIPT_DIR%\..") do set ROOT_DIR=%%~fI

set VENV_DIR=%ROOT_DIR%\venv
set UPDATE_BIN=%VENV_DIR%\Scripts\click-odoo-update.exe
set CONF=%ROOT_DIR%\odoo-configs\odoo-server.conf

if not exist "%VENV_DIR%" (
  echo ERROR: required venv directory not found at %VENV_DIR%
  exit /b 1
)
if not exist "%UPDATE_BIN%" (
  echo ERROR: click-odoo-update not found at %UPDATE_BIN%
  exit /b 1
)
if not exist "%CONF%" (
  echo ERROR: Odoo config not found at %CONF%
  exit /b 1
)

echo INFO: Updating Odoo addons using config %CONF%. Passing through any extra arguments.
"%UPDATE_BIN%" -c "%CONF%" --update-all --log-level debug %*

endlocal
"""
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.update_all_bat.write_text(content.replace("\n", "\r\n"), encoding="utf-8")


# -----------------------------
# Main logic
# -----------------------------

def sync_project(
        ini_path: Path,
        sync_odoo: bool,
        sync_addons: bool,
        root_override: Optional[Path] = None,
        dest_root_override: Optional[Path] = None,
        create_wheelhouse: bool = False,
        reuse_wheelhouse: bool = False,
        create_venv: bool = False,
        rebuild_venv: bool = False,
        clear_pip_wheel_cache: bool = False,
        no_configs: bool = False,
        no_scripts: bool = False,
        no_data_dir: bool = False,
) -> None:
    root = (root_override or ini_path.parent).resolve()
    if root.exists() and not root.is_dir():
        raise Exception(f"ROOT exists but is not a directory: {root}")

    # dest_root is used only for paths embedded in generated configs/scripts.
    # It does NOT need to exist on the build system.
    if dest_root_override is None:
        dest_root = root
    else:
        candidate = Path(dest_root_override).expanduser()
        if not candidate.is_absolute():
            # Interpret relative dest roots relative to the workspace ROOT for stability.
            candidate = root / candidate
        try:
            dest_root = candidate.resolve()
        except Exception:
            dest_root = candidate.absolute()

    layout = Layout.from_root(root)
    dest_layout = Layout.from_root(dest_root)

    # Runtime variables for INI evaluation:
    # - FS vars: used for repo/venv operations and include resolution (must exist on build host)
    # - DEST vars: used for [config] interpolation (paths embedded into generated files)
    fs_runtime_vars = {
        # Common workspace (filesystem) paths
        "ini_dir": str(ini_path.parent.resolve()),
        "root_dir": str(layout.root),
        "odoo_dir": str(layout.odoo_dir),
        "addons_dir": str(layout.addons_root),
        "backups_dir": str(layout.backups_dir),
        "configs_dir": str(layout.configs_dir),
        "config_path": str(layout.conf_path),
        "scripts_dir": str(layout.scripts_dir),
        "venv_python": str((layout.root / "venv") / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")),
    }
    dest_runtime_vars = {
        # Destination (deployment) paths
        "ini_dir": str(ini_path.parent.resolve()),
        "root_dir": str(dest_layout.root),
        "odoo_dir": str(dest_layout.odoo_dir),
        "addons_dir": str(dest_layout.addons_root),
        "backups_dir": str(dest_layout.backups_dir),
        "configs_dir": str(dest_layout.configs_dir),
        "config_path": str(dest_layout.conf_path),
        "scripts_dir": str(dest_layout.scripts_dir),
        "venv_python": str((dest_layout.root / "venv") / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")),
    }

    # Load config twice: filesystem vars for repos/venv, and destination vars for the [config] section.
    cfg_fs = load_project_config(ini_path, runtime_vars=fs_runtime_vars, include_runtime_vars=fs_runtime_vars)
    cfg_dest = load_project_config(ini_path, runtime_vars=dest_runtime_vars, include_runtime_vars=fs_runtime_vars)
    cfg = ProjectConfig(
        virtualenv=cfg_fs.virtualenv,
        odoo=cfg_fs.odoo,
        addons=cfg_fs.addons,
        config=cfg_dest.config,
    )

    # If user overrides "data_dir" via [config] section, propagate changes to dest_layout->data_dir.
    if "data_dir" in cfg.config:
        cfg_data_dir_raw = cfg.config.get("data_dir")
        cfg_data_dir_path = Path(cfg_data_dir_raw.strip()).expanduser()
        if not cfg_data_dir_path.is_absolute():
            cfg_data_dir_path = dest_layout.root / cfg_data_dir_path
        try:
            cfg_data_dir = cfg_data_dir_path.resolve()
        except Exception:
            cfg_data_dir = cfg_data_dir_path.absolute()
        _logger.warning(f"data_dir override via [config] section: from={dest_layout.data_dir}, to={cfg_data_dir}")
        dest_layout = replace(dest_layout, data_dir=cfg_data_dir)

    # We optionally create/ensure the venv early so we can use its Python for `uv pip compile` / installs.
    venv_py: Optional[Path] = None
    venv_enabled = create_venv or rebuild_venv

    # Validate combinations (defensive; CLI also enforces these).
    if reuse_wheelhouse and not venv_enabled:
        raise Exception("--reuse-wheelhouse requires --create-venv (or --rebuild-venv).")
    if create_wheelhouse and reuse_wheelhouse:
        raise Exception('--create-wheelhouse can not be used with --reuse-wheelhouse')

    if venv_enabled or create_wheelhouse:
        venv_dir = layout.root / "venv"

        if (rebuild_venv or create_wheelhouse) and venv_dir.exists():
            _logger.info("Rebuilding venv: removing %s", venv_dir)
            _rmtree(venv_dir)

        # Wheelhouse handling: either reuse, or rebuild from scratch.
        if reuse_wheelhouse:
            if not layout.wheelhouse_dir.exists() or not layout.wheelhouse_dir.is_dir():
                raise Exception(f"--reuse-wheelhouse set but wheelhouse dir not found: {layout.wheelhouse_dir}")
        else:
            if layout.wheelhouse_dir.exists():
                _logger.info("Rebuilding wheelhouse: removing %s", layout.wheelhouse_dir)
                _rmtree(layout.wheelhouse_dir)
            layout.wheelhouse_dir.mkdir(parents=True, exist_ok=True)

        # Create venv (requirements are installed later from a single lock file).
        require_venv(
            layout=layout,
            python_version=cfg.virtualenv.python_version,
            reuse_wheelhouse=reuse_wheelhouse,
            managed_python=cfg.virtualenv.managed_python,
        )
        venv_py = venv_dir / ("Scripts/python.exe" if sys.platform.startswith("win") else "bin/python")
        if not venv_py.exists():
            raise Exception(f"venv python not found at expected path: {venv_py}")

        if reuse_wheelhouse and (sync_odoo or sync_addons):
            _logger.warning(
                "--reuse-wheelhouse is set together with repo sync targets; "
                "dependency lock/wheelhouse rebuild will be skipped. "
                "If requirements changed, re-run without --reuse-wheelhouse."
            )
    else:
        if sync_odoo or sync_addons:
            _logger.info(
                "Repo sync selected, but venv/wheelhouse provisioning is disabled; "
                "skipping venv/wheelhouse. Use --create-venv or --create-wheelhouse to enable."
            )
        else:
            _logger.info(
                "No sync target selected; regenerating config and helper scripts only (skipping venv/repo operations)."
            )

    layout.configs_dir.mkdir(parents=True, exist_ok=True)
    layout.addons_root.mkdir(parents=True, exist_ok=True)
    layout.scripts_dir.mkdir(parents=True, exist_ok=True)
    layout.backups_dir.mkdir(parents=True, exist_ok=True)
    if not no_data_dir:
        dest_layout.data_dir.mkdir(parents=True, exist_ok=True)

    # Sync repositories first, collect all requirements, then compile + install once.
    req_files: list[Path] = []

    if sync_odoo:
        if cfg.odoo.shallow_clone:
            # Shallow + single branch (enabled only when [odoo] shallow_clone=true).
            ensure_repo(
                cfg.odoo.repo,
                layout.odoo_dir,
                branch=cfg.odoo.branch,
                depth=1,
                single_branch=True,
                fetch_all=False,
            )
            checkout_branch(layout.odoo_dir, cfg.odoo.branch, fetch_all=False, depth=1)
        else:
            # Full clone/fetch (default).
            ensure_repo(
                cfg.odoo.repo,
                layout.odoo_dir,
                branch=cfg.odoo.branch,
                depth=None,
                single_branch=False,
                fetch_all=True,
            )
            checkout_branch(layout.odoo_dir, cfg.odoo.branch, fetch_all=True, depth=None)

        odoo_req = layout.odoo_dir / "requirements.txt"
        if not odoo_req.exists():
            raise Exception(f"Odoo requirements file not found: {odoo_req}")
        req_files.append(odoo_req)
    else:
        # If we're provisioning python but not syncing repos, use whatever is already present in the workspace.
        if venv_py is not None:
            odoo_req = layout.odoo_dir / "requirements.txt"
            if odoo_req.exists():
                req_files.append(odoo_req)

    if sync_addons:
        if not cfg.addons:
            _logger.info("No [addons.*] sections configured; skipping addons sync.")
        for addon_name, spec in cfg.addons.items():
            dest = layout.addons_root / addon_name

            if spec.shallow_clone:
                # Shallow + single branch (enabled only when [addons.<name>] shallow_clone=true).
                ensure_repo(
                    spec.repo,
                    dest,
                    branch=spec.branch,
                    depth=1,
                    single_branch=True,
                    fetch_all=False,
                )
                checkout_branch(dest, spec.branch, fetch_all=False, depth=1)
            else:
                # Full clone/fetch (default).
                ensure_repo(
                    spec.repo,
                    dest,
                    branch=spec.branch,
                    depth=None,
                    single_branch=False,
                    fetch_all=True,
                )
                checkout_branch(dest, spec.branch, fetch_all=True, depth=None)

            addon_req = dest / "requirements.txt"
            if addon_req.exists():
                req_files.append(addon_req)
    else:
        # If we're provisioning python but not syncing repos, use existing addon requirements (if present).
        if venv_py is not None and cfg.addons:
            for addon_name in cfg.addons.keys():
                dest = layout.addons_root / addon_name
                addon_req = dest / "requirements.txt"
                if addon_req.exists():
                    req_files.append(addon_req)

    # Compile and install a single lock file from all synced repos + base requirements.
    # In --reuse-wheelhouse mode we skip compilation + wheel build and only install offline from existing lock/wheels.
    if venv_py is not None:
        # The generated scripts assume ROOT/odoo exists.
        if not layout.odoo_dir.exists() or not layout.odoo_dir.is_dir():
            raise Exception(
                f"Odoo directory not found: {layout.odoo_dir}. "
                "Run with --sync-odoo/--sync-all first (or ensure ROOT/odoo exists)."
            )

        lock_path = layout.wheelhouse_dir / "all-requirements.lock.txt"
        build_constraints_path = layout.wheelhouse_dir / "build-constraints.txt"

        if reuse_wheelhouse:
            # Reuse existing wheelhouse (offline-only mode)
            if not layout.wheelhouse_dir.exists() or not layout.wheelhouse_dir.is_dir():
                raise Exception(f"Wheelhouse directory not found: {layout.wheelhouse_dir}")
            if not any(layout.wheelhouse_dir.glob("*.whl")):
                raise Exception(f"Wheelhouse looks empty (no .whl files): {layout.wheelhouse_dir}")

            if not lock_path.exists():
                raise Exception(
                    f"--reuse-wheelhouse set but lock file not found: {lock_path} "
                    "(expected existing wheelhouse from a previous run)"
                )

            if cfg.virtualenv.build_constraints and not build_constraints_path.is_file():
                raise Exception(
                    f"--reuse-wheelhouse and build_constraints set in INI but build_constraints file not found: {build_constraints_path} "
                    "(expected existing wheelhouse from a previous run)"
                )

            pip_install_requirements_file(
                venv_python=venv_py,
                workspace_root=layout.root,
                wheelhouse_dir=layout.wheelhouse_dir,
                requirements_path=lock_path,
            )
        else:
            # Write build constraints to file
            if cfg.virtualenv.build_constraints:
                build_constraints_path.write_text(
                    "\n".join(cfg.virtualenv.build_constraints).rstrip("\n") + "\n", encoding="utf-8")

            # We need Odoo requirements to produce a correct lock.
            odoo_req = layout.odoo_dir / "requirements.txt"
            if not odoo_req.exists():
                raise Exception(f"Odoo requirements file not found: {odoo_req}")

            base_requirements = list(_DEFAULT_REQUIREMENTS)
            if cfg.virtualenv.requirements:
                base_requirements.extend(cfg.virtualenv.requirements)

            compile_all_requirements_lock(
                venv_python=venv_py,
                workspace_root=layout.root,
                wheelhouse_dir=layout.wheelhouse_dir,
                requirement_files=req_files,
                base_requirements=base_requirements,
                requirements_ignore=cfg.virtualenv.requirements_ignore,
                output_lock_path=lock_path,
                build_constraints_path=build_constraints_path,
            )

            build_wheelhouse_from_requirements(
                venv_python=venv_py,
                workspace_root=layout.root,
                requirements_path=lock_path,
                wheelhouse_dir=layout.wheelhouse_dir,
                build_constraints_path=build_constraints_path,
                clear_pip_wheel_cache=clear_pip_wheel_cache,
            )

            if venv_enabled:
                pip_install_requirements_file(
                    venv_python=venv_py,
                    workspace_root=layout.root,
                    wheelhouse_dir=layout.wheelhouse_dir,
                    requirements_path=lock_path,
                )

        if venv_enabled:
            # Install Odoo itself in editable mode (so local source changes are reflected).
            _logger.info("Installing Odoo in editable mode: %s", layout.odoo_dir)
            cmd = [
                str(venv_py), "-m", "pip", "install",
                "--no-deps",
                "--no-build-isolation",
                "-e", str(layout.odoo_dir),
            ]
            p = subprocess.run(
                cmd,
                cwd=str(layout.root),
                text=True,
                capture_output=True,
            )
            _handle_process_output(p, err_msg=(
                "Failed to install Odoo in editable mode.\n"
                f"Command: {' '.join(p.args if isinstance(p.args, list) else [str(p.args)])}\n"
                f"{p.stdout}\n{p.stderr}"
            ))

    # Generate config (unless disabled).
    if not no_configs:
        addon_paths: list[Path] = [dest_layout.addons_root / name for name in cfg.addons.keys()]
        conf_text = render_odoo_conf(cfg.config, dest_layout, addon_paths)
        layout.conf_path.write_text(conf_text, encoding="utf-8")
    else:
        _logger.info("Skipping config generation (--no-configs).")

    is_windows = sys.platform.startswith("win")

    # Generate helper scripts (unless disabled).
    if not no_scripts:
        if is_windows:
            write_run_bat(layout)
            write_test_bat(layout)
            write_shell_bat(layout)
            write_update_bat(layout)
            write_update_all_bat(layout)
        else:
            write_run_sh(layout)
            write_instance_sh(layout)
            write_test_sh(layout)
            write_shell_sh(layout)
            write_update_sh(layout)
            write_update_all_sh(layout)

        db_name = cfg.config.get("db_name")
        if not isinstance(db_name, str) or not db_name.strip():
            _logger.warning(
                "Missing or invalid 'db_name' in [config] (expected non-empty string)."
                "Database scripts (initdb/backup/restore/restore-force) will NOT be generated."
            )
        else:
            if is_windows:
                write_initdb_bat(layout, db_name.strip())
                write_backup_bat(layout, db_name.strip())
                write_restore_bat(layout, db_name.strip())
                write_restore_force_bat(layout, db_name.strip())
            else:
                write_initdb_sh(layout, db_name.strip())
                write_backup_sh(layout, db_name.strip())
                write_restore_sh(layout, db_name.strip())
                write_restore_force_sh(layout, db_name.strip())
    else:
        _logger.info("Skipping script generation (--no-scripts).")

    synced: list[str] = []
    if sync_odoo:
        synced.append("odoo")
    if sync_addons:
        synced.append("addons")

    print("OK")
    if synced:
        synced_label = ", ".join(synced)
    else:
        synced_label = "none"

    generated: list[str] = []
    if not no_configs:
        generated.append("configs")
    if not no_scripts:
        generated.append("scripts")
    if generated:
        synced_label = f"{synced_label} (generated: {', '.join(generated)})"
    else:
        synced_label = f"{synced_label} (no configs and scripts generated)"

    print(f"  Synced:             {synced_label}")
    print(f"  ROOT:               {layout.root}")
    if dest_layout.root != layout.root:
        print(f"  DEST_ROOT:          {dest_layout.root}")
    print(f"  Odoo:               {layout.odoo_dir}")
    print(f"  Addons:             {layout.addons_root}")
    print(f"  Backups:            {layout.backups_dir}")
    if no_data_dir:
        print(f"  Data:               SKIPPED (--no-data-dir)")
    else:
        print(f"  Data:               {dest_layout.data_dir}")
    if no_configs:
        print(f"  Config:             SKIPPED (--no-configs) [{layout.conf_path}]")
    else:
        print(f"  Config:             {layout.conf_path}")
    if venv_py is not None:
        print(f"  Venv:               {layout.root / 'venv'}")
        lock_path = layout.wheelhouse_dir / "all-requirements.lock.txt"
        if lock_path.exists():
            print(f"  Requirements:       {lock_path}")
            print(f"  Wheelhouse:         {layout.wheelhouse_dir}")
        if cfg.virtualenv.build_constraints:
            bc = layout.wheelhouse_dir / "build-constraints.txt"
            if bc.exists():
                print(f"  Build Constraints:  {bc}")

    if no_scripts:
        print("  Scripts:            SKIPPED (--no-scripts)")
    else:
        print(f"  Scripts:")
        if is_windows:
            print(f"  - run:              {layout.run_bat}")
            print(f"  - test:             {layout.test_bat}")
            print(f"  - shell:            {layout.shell_bat}")
            print(f"  - initdb:           {layout.initdb_bat}")
            print(f"  - backup:           {layout.backup_bat}")
            print(f"  - restore:          {layout.restore_bat}")
            print(f"  - restore_force:    {layout.restore_force_bat}")
            print(f"  - update:           {layout.update_bat}")
            print(f"  - update-all:       {layout.update_all_bat}")
        else:
            print(f"  - run:              {layout.run_sh}")
            print(f"  - test:             {layout.test_sh}")
            print(f"  - shell:            {layout.shell_sh}")
            print(f"  - initdb:           {layout.initdb_sh}")
            print(f"  - backup:           {layout.backup_sh}")
            print(f"  - restore:          {layout.restore_sh}")
            print(f"  - restore_force:    {layout.restore_force_sh}")
            print(f"  - update:           {layout.update_sh}")
            print(f"  - update-all:       {layout.update_all_sh}")


# -----------------------------
# CLI
# -----------------------------

def build_parser() -> argparse.ArgumentParser:
    epilog = """If no options are specified, odt-env only regenerates configs and helper scripts.

ROOT selection:
  By default, ROOT is the directory containing the INI.
  Use --root to override where the workspace (repos/venv/configs/scripts) is created.
  Use --dest-root to override the ROOT path embedded into generated configs/scripts (deployment root).

Examples:
  odt-env /path/to/odoo-project.ini --sync-all --create-venv
  odt-env /path/to/odoo-project.ini --sync-all --create-wheelhouse
  odt-env /path/to/odoo-project.ini --sync-all --create-venv --root /path/to/workspace-root
  odt-env /path/to/odoo-project.ini --rebuild-venv --reuse-wheelhouse
  odt-env /path/to/odoo-project.ini --rebuild-venv --reuse-wheelhouse --root /path/to/workspace-root
"""

    parser = argparse.ArgumentParser(
        prog="odt-env",
        description=f"odt-env {__version__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    parser.add_argument(
        "--version",
        action="version",
        version=f"odt-env {__version__}",
        help="Show the program version and exit.",
    )

    parser.add_argument(
        "ini",
        metavar="INI",
        help="Path to odoo-project.ini (default ROOT is its directory; override with --root)",
    )

    parser.add_argument(
        "--root",
        metavar="ROOT",
        default=None,
        help="Override workspace ROOT directory (default: directory containing INI).",
    )

    parser.add_argument(
        "--dest-root",
        dest="dest_root",
        metavar="DEST_ROOT",
        default=None,
        help=(
            "Override DEST_ROOT used for paths embedded in generated configs/scripts. "
            "This does not change where the workspace is created (see --root). "
            "DEST_ROOT does not need to exist on the build machine. "
            "Default: same as ROOT."
        ),
    )

    target = parser.add_mutually_exclusive_group()
    target.add_argument("--sync-odoo", dest="odoo", action="store_true", help="Sync only Odoo repository")
    target.add_argument("--sync-addons", dest="addons", action="store_true", help="Sync only addon repositories")
    target.add_argument("--sync-all", dest="all", action="store_true", help="Sync Odoo + addons")

    parser.add_argument(
        "--create-wheelhouse",
        action="store_true",
        help=(
            "Create/refresh ROOT/wheelhouse (and all-requirements.lock.txt). "
            "Ensures ROOT/venv exists so wheels can be built, but does NOT install project requirements into the venv "
            "unless --create-venv/--rebuild-venv is also set."
        ),
    )
    parser.add_argument(
        "--create-venv",
        action="store_true",
        help=(
            "Enable virtualenv provisioning (create/update ROOT/venv + wheelhouse). "
            "Without this flag, odt-env will not touch venv/wheelhouse."
        ),
    )
    parser.add_argument(
        "--rebuild-venv",
        action="store_true",
        help="Delete ROOT/venv and recreate it (implies --create-venv).",
    )
    parser.add_argument(
        "--reuse-wheelhouse",
        action="store_true",
        help=(
            "Reuse existing ROOT/wheelhouse (and all-requirements.lock.txt) and install offline only. "
            "Skips lock compilation and wheelhouse build. Requires --create-venv."
        ),
    )
    parser.add_argument(
        "--clear-pip-wheel-cache",
        action="store_true",
        help="Remove all items from the pip's wheel cache.",
    )

    parser.add_argument(
        "--no-configs",
        action="store_true",
        help="Do not (re)generate config files (e.g. ROOT/odoo-configs/odoo-server.conf).",
    )
    parser.add_argument(
        "--no-scripts",
        action="store_true",
        help="Do not (re)generate helper scripts under ROOT/odoo-scripts/.",
    )
    parser.add_argument(
        "--no-data-dir",
        action="store_true",
        help="Do not generate odoo data folder.",
    )

    return parser


def _validate_root_override(parser: argparse.ArgumentParser, raw_root: str) -> Path:
    """Validate and normalize the --root override.

    - Expands '~'
    - Resolves to an absolute path
    - Ensures it exists and is a directory

    Returns the normalized Path.
    """
    _logger.info('CLI --root provided: %s', raw_root)

    candidate = Path(raw_root).expanduser()

    # Resolve to an absolute path for consistent workspace layout and logging.
    try:
        resolved = candidate.resolve()
    except Exception:
        # Fallback: make it absolute without resolving symlinks.
        resolved = candidate.absolute()

    if not resolved.exists():
        parser.error(f'--root path does not exist: {resolved}')
    if not resolved.is_dir():
        parser.error(f'--root path is not a directory: {resolved}')

    _logger.info('Validated --root: %s', resolved)
    return resolved


def main() -> None:
    # Standard logging to stdout only.
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = build_parser()

    if len(sys.argv) == 1:
        parser.print_help()
        raise SystemExit(2)

    args = parser.parse_args()
    clear_pip_wheel_cache = bool(getattr(args, 'clear_pip_wheel_cache', False))
    reuse_wheelhouse = bool(getattr(args, 'reuse_wheelhouse', False))
    rebuild_venv = bool(getattr(args, 'rebuild_venv', False))
    create_venv = bool(getattr(args, 'create_venv', False)) or rebuild_venv
    create_wheelhouse = bool(getattr(args, 'create_wheelhouse', False))
    no_configs = bool(getattr(args, 'no_configs', False))
    no_scripts = bool(getattr(args, 'no_scripts', False))
    no_data_dir = bool(getattr(args, 'no_data_dir', False))
    if reuse_wheelhouse and not create_venv:
        parser.error('--reuse-wheelhouse requires --create-venv (or --rebuild-venv)')
    if create_wheelhouse and reuse_wheelhouse:
        parser.error('--create-wheelhouse can not be used with --reuse-wheelhouse')

    ini_path = Path(args.ini).expanduser().resolve()
    if not ini_path.exists():
        parser.error(f'INI file does not exist: {ini_path}')
    if not ini_path.is_file():
        parser.error(f'INI path is not a file: {ini_path}')

    root_override: Optional[Path] = None
    if args.root:
        root_override = _validate_root_override(parser, args.root)
    else:
        _logger.info('Workspace ROOT default (INI directory): %s', ini_path.parent.resolve())

    dest_root_override: Optional[Path] = None
    if getattr(args, 'dest_root', None):
        # NOTE: DEST_ROOT does not need to exist on this machine.
        dest_root_override = Path(args.dest_root).expanduser()

    if args.all:
        sync_odoo, sync_addons = True, True
    elif args.odoo:
        sync_odoo, sync_addons = True, False
    elif args.addons:
        sync_odoo, sync_addons = False, True
    else:
        # No sync target selected -> only regenerate configs + helper scripts.
        sync_odoo, sync_addons = False, False

    ini_path = Path(args.ini).resolve()
    sync_project(
        ini_path,
        sync_odoo=sync_odoo,
        sync_addons=sync_addons,
        root_override=root_override,
        dest_root_override=dest_root_override,
        create_wheelhouse=create_wheelhouse,
        reuse_wheelhouse=reuse_wheelhouse,
        create_venv=create_venv,
        rebuild_venv=rebuild_venv,
        clear_pip_wheel_cache=clear_pip_wheel_cache,
        no_configs=no_configs,
        no_scripts=no_scripts,
        no_data_dir=no_data_dir,
    )


if __name__ == "__main__":
    main()
