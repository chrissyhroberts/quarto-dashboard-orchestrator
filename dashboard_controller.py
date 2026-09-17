#!/usr/bin/env python3
"""
Quarto Dashboard Delivery System — Generic Starter
dashboard_controller.py
=======================

Copyright (c) 2026 Chrissy h. Roberts. Released under the MIT License.

Cron-friendly orchestration for Quarto dashboards/books.

For each project directory containing both `_quarto.yml` (or `_quarto.yaml`) and
`dashboard.yml`, the controller performs the following pipeline:

    acquire data -> render Quarto -> distribute -> verify/log -> remove transient data

Supported upstream data sources:
    * KoboToolbox (v2 named synchronous exports)
    * ODK Central (submission CSV exports)
    * REDCap (records API, CSV)
    * project (project handles its own acquisition)
    * none (no controller-managed acquisition)

Supported delivery targets:
    * local filesystem
    * SharePoint via an rclone remote
    * both

Security model:
    * NO passwords or API tokens are stored in dashboard.yml.
    * dashboard.yml contains only the NAMES of environment variables holding
      secrets (for example KOBO_TOKEN).
    * An optional protected KEY=VALUE secrets file can be loaded at startup.
    * SharePoint credentials stay in rclone's protected configuration; YAML only
      contains the rclone remote name.

"""

from __future__ import annotations

import argparse
import base64
import csv
import fcntl
import hashlib
import json
import re
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from action_file_formats import action_file_format, delivered_filename, stage_action_file

try:
    import yaml
except ImportError as exc:  # pragma: no cover - gives a useful startup error
    raise SystemExit(
        "PyYAML is required. Install with: python3 -m pip install -r requirements.txt"
    ) from exc


# -----------------------------------------------------------------------------
# Small result objects used in the JSON/CSV audit trail
# -----------------------------------------------------------------------------


@dataclass
class PullResult:
    name: str
    status: str
    output: Optional[str] = None
    bytes: Optional[int] = None
    rows: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[str] = None


@dataclass
class DeliveryResult:
    product: str
    audience: str
    target: str
    destination: str
    status: str
    bytes: Optional[int] = None
    rows: Optional[int] = None
    sha256: Optional[str] = None
    error: Optional[str] = None


@dataclass
class ProjectResult:
    project: str
    status: str
    duration_seconds: float
    pulls: list[PullResult] = field(default_factory=list)
    deliveries: list[DeliveryResult] = field(default_factory=list)
    output_directory: Optional[str] = None
    cleanup_status: Optional[str] = None
    cleanup_removed: list[str] = field(default_factory=list)
    cleanup_error: Optional[str] = None
    error: Optional[str] = None


# -----------------------------------------------------------------------------
# Generic helpers
# -----------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def env_required(name: str) -> str:
    """Return a required secret from the environment without ever logging it."""
    value = os.environ.get(name)
    if value is None or value == "":
        raise RuntimeError(f"Required environment variable is not set: {name}")
    return value


def load_secrets_file(path: Optional[Path]) -> None:
    """
    Load a deliberately simple KEY=VALUE secrets file.

    This is NOT a shell parser: no command substitution, `export`, interpolation,
    or executable syntax is supported. Existing environment variables win, which
    makes it safe for a deployment system to inject credentials directly.
    """
    if path is None:
        return
    if not path.is_file():
        raise FileNotFoundError(f"Secrets file not found: {path}")

    # On POSIX, warn if group/other permissions are present. We do not silently
    # chmod a file because ownership/permissions are an operator responsibility.
    if os.name == "posix":
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError(
                f"Secrets file {path} is too permissive ({oct(mode)}). Use chmod 600."
            )

    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid secrets file line {line_no}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"Invalid secrets file line {line_no}: empty key")
        if len(value) >= 2 and value[0] == value[-1] == '"':
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON-quoted secret on line {line_no}"
                ) from exc
        elif len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def configure_logging(log_dir: Path, rid: str) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"run_{rid}.log"
    logger = logging.getLogger("quarto-dashboard-delivery")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    return logger, path


def resolve_executable(name: str) -> str:
    if Path(name).is_absolute():
        if Path(name).is_file() and os.access(name, os.X_OK):
            return name
        raise FileNotFoundError(f"Executable not found or not executable: {name}")
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"Executable not found on PATH: {name}")
    return found


def run_command(
    command: list[str],
    logger: logging.Logger,
    *,
    cwd: Optional[Path] = None,
    timeout: int = 3600,
) -> subprocess.CompletedProcess[str]:
    logger.info("Command: %s", " ".join(command))
    result = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.stdout.strip():
        for line in result.stdout.rstrip().splitlines():
            logger.info("stdout | %s", line)
    if result.stderr.strip():
        for line in result.stderr.rstrip().splitlines():
            logger.warning("stderr | %s", line)
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, command, output=result.stdout, stderr=result.stderr
        )
    return result


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    """Return SHA-256 hashes keyed by relative path for all files below root."""
    root = Path(root)
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def csv_rows(path: Path) -> Optional[int]:
    """Best-effort CSV data-row count. Returns None for non-CSV/unreadable files."""
    if path.suffix.lower() != ".csv":
        return None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            count = sum(1 for _ in reader)
        return max(0, count - 1) if count else 0
    except Exception:
        return None


def safe_project_path(project_dir: Path, value: str) -> Path:
    """Resolve a project-relative path and reject `../` escapes."""
    raw = Path(value)
    resolved = raw.resolve() if raw.is_absolute() else (project_dir / raw).resolve()
    try:
        resolved.relative_to(project_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"Path escapes project directory: {value}") from exc
    return resolved


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.parent / f".{destination.name}.staging.{os.getpid()}"
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(source, tmp)
    os.replace(tmp, destination)


def atomic_copy_directory(source: Path, destination: Path) -> None:
    """Replace a local directory only after a complete staged copy succeeds."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.staging.{os.getpid()}"
    backup = destination.parent / f".{destination.name}.previous.{os.getpid()}"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.copytree(source, staging, symlinks=True)
    moved_old = False
    try:
        if destination.exists():
            if not destination.is_dir() or destination.is_symlink():
                raise RuntimeError(f"Refusing to replace non-directory: {destination}")
            os.replace(destination, backup)
            moved_old = True
        os.replace(staging, destination)
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        if moved_old:
            shutil.rmtree(destination, ignore_errors=True)
            if backup.exists():
                os.replace(backup, destination)
        shutil.rmtree(staging, ignore_errors=True)
        raise


# -----------------------------------------------------------------------------
# HTTP helpers. Secrets are supplied only in headers/body and never logged.
# -----------------------------------------------------------------------------


def http_request(
    url: str,
    *,
    method: str = "GET",
    headers: Optional[dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 180,
) -> tuple[bytes, dict[str, str]]:
    request = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            response_headers = {k.lower(): v for k, v in response.headers.items()}
            return body, response_headers
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"HTTP request failed for {url}: {exc.reason}") from exc


def json_request(*args: Any, **kwargs: Any) -> Any:
    body, _ = http_request(*args, **kwargs)
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError("Server returned invalid JSON") from exc


# -----------------------------------------------------------------------------
# Source adapters
# -----------------------------------------------------------------------------


def pull_kobo(
    project_dir: Path,
    source: dict[str, Any],
    pulls: dict[str, Any],
    logger: logging.Logger,
) -> list[PullResult]:
    """
    Download Kobo named synchronous exports.

    The operator creates a named export in Kobo's DATA > Downloads screen and
    refers to it by name in dashboard.yml. The controller queries the v2
    export-settings endpoint to resolve the current CSV/XLSX URL automatically.
    """
    server = str(source.get("server", "")).rstrip("/")
    token_env = str(source.get("token_env", ""))
    if not server or not token_env:
        raise ValueError("Kobo source requires server and token_env")
    token = env_required(token_env)
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}

    settings_cache: dict[str, list[dict[str, Any]]] = {}
    results: list[PullResult] = []

    for name, spec_raw in pulls.items():
        spec = spec_raw or {}
        try:
            asset_uid = str(spec.get("asset_uid") or source.get("asset_uid") or "")
            if not asset_uid:
                raise ValueError(f"Kobo pull '{name}' requires asset_uid")
            output = safe_project_path(project_dir, str(spec.get("output", f"data/{name}.csv")))
            fmt = str(spec.get("format") or output.suffix.lstrip(".") or "csv").lower()
            if fmt not in {"csv", "xlsx"}:
                raise ValueError(f"Kobo pull '{name}' format must be csv or xlsx")

            direct_url = spec.get("url")
            if direct_url:
                data_url = str(direct_url)
            else:
                export_name = str(spec.get("export", ""))
                export_uid = str(spec.get("export_setting_uid", ""))
                if not export_name and not export_uid:
                    raise ValueError(
                        f"Kobo pull '{name}' requires export (named export) or export_setting_uid"
                    )

                if asset_uid not in settings_cache:
                    settings_url = f"{server}/api/v2/assets/{urllib.parse.quote(asset_uid, safe='')}/export-settings/"
                    payload = json_request(settings_url, headers=headers)
                    if isinstance(payload, dict):
                        items = payload.get("results", payload.get("data", []))
                    else:
                        items = payload
                    if not isinstance(items, list):
                        raise RuntimeError("Unexpected Kobo export-settings response")
                    settings_cache[asset_uid] = [x for x in items if isinstance(x, dict)]

                matches = settings_cache[asset_uid]
                chosen: Optional[dict[str, Any]] = None
                for item in matches:
                    if export_uid and str(item.get("uid", "")) == export_uid:
                        chosen = item
                        break
                    if export_name and str(item.get("name", "")) == export_name:
                        chosen = item
                        break
                if chosen is None:
                    wanted = export_uid or export_name
                    available = ", ".join(
                        str(x.get("name") or x.get("uid") or "?") for x in matches
                    )
                    raise RuntimeError(
                        f"Kobo export setting '{wanted}' not found for asset {asset_uid}. "
                        f"Available: {available or '(none)'}"
                    )

                key = f"data_url_{fmt}"
                data_url = str(chosen.get(key) or "")
                if not data_url:
                    uid = str(chosen.get("uid") or export_uid)
                    if not uid:
                        raise RuntimeError(f"Kobo export setting has no {key} or uid")
                    data_url = (
                        f"{server}/api/v2/assets/{urllib.parse.quote(asset_uid, safe='')}/"
                        f"export-settings/{urllib.parse.quote(uid, safe='')}/data.{fmt}"
                    )
                elif data_url.startswith("/"):
                    data_url = server + data_url

            logger.info("Kobo pull '%s' -> %s", name, output)
            body, _ = http_request(
                data_url,
                headers={"Authorization": f"Token {token}", "Accept": "*/*"},
            )
            atomic_write_bytes(output, body)
            result = PullResult(
                name=name,
                status="success",
                output=str(output),
                bytes=output.stat().st_size,
                rows=csv_rows(output),
                sha256=sha256_file(output),
            )
            results.append(result)
        except Exception as exc:
            results.append(PullResult(name=name, status="failed", error=str(exc)))
            logger.error("Kobo pull '%s' failed: %s", name, exc)
    return results


def odk_login(source: dict[str, Any]) -> tuple[str, str]:
    server = str(source.get("server", "")).rstrip("/")
    if not server:
        raise ValueError("ODK source requires server")
    bearer_env = source.get("bearer_token_env")
    if bearer_env:
        return server, env_required(str(bearer_env))

    email_env = str(source.get("email_env", ""))
    password_env = str(source.get("password_env", ""))
    if not email_env or not password_env:
        raise ValueError(
            "ODK source requires bearer_token_env, or email_env and password_env"
        )
    payload = json.dumps(
        {"email": env_required(email_env), "password": env_required(password_env)}
    ).encode("utf-8")
    response = json_request(
        f"{server}/v1/sessions",
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        data=payload,
    )
    token = response.get("token") if isinstance(response, dict) else None
    if not token:
        raise RuntimeError("ODK login succeeded but no session token was returned")
    return server, str(token)


def pull_odk(
    project_dir: Path,
    source: dict[str, Any],
    pulls: dict[str, Any],
    logger: logging.Logger,
) -> list[PullResult]:
    server, token = odk_login(source)
    project_id = source.get("project_id")
    if project_id is None:
        raise ValueError("ODK source requires project_id")
    results: list[PullResult] = []
    try:
        for name, spec_raw in pulls.items():
            spec = spec_raw or {}
            form_id = str(spec.get("form_id", ""))
            if not form_id:
                raise ValueError(f"ODK pull '{name}' requires form_id")
            output = safe_project_path(project_dir, str(spec.get("output", f"data/{name}.csv")))
            encoded_form = urllib.parse.quote(form_id, safe="")
            url = (
                f"{server}/v1/projects/{project_id}/forms/{encoded_form}/submissions.csv"
            )
            logger.info("ODK pull '%s' -> %s", name, output)
            body, _ = http_request(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "text/csv"},
            )
            atomic_write_bytes(output, body)
            results.append(
                PullResult(
                    name=name,
                    status="success",
                    output=str(output),
                    bytes=output.stat().st_size,
                    rows=csv_rows(output),
                    sha256=sha256_file(output),
                )
            )
    except Exception as exc:
        failed_name = name if "name" in locals() else "ODK"
        results.append(PullResult(name=str(failed_name), status="failed", error=str(exc)))
        logger.error("ODK pull '%s' failed: %s", failed_name, exc)
    finally:
        # Revoke sessions created from email/password. Static bearer tokens are not revoked.
        if not source.get("bearer_token_env"):
            try:
                http_request(
                    f"{server}/v1/sessions/current",
                    method="DELETE",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                )
            except Exception:
                logger.warning("Could not revoke ODK session token; it will expire normally.")
    return results


def pull_redcap(
    project_dir: Path,
    source: dict[str, Any],
    pulls: dict[str, Any],
    logger: logging.Logger,
) -> list[PullResult]:
    server = str(source.get("server", ""))
    token_env = str(source.get("token_env", ""))
    if not server or not token_env:
        raise ValueError("REDCap source requires server and token_env")
    token = env_required(token_env)
    results: list[PullResult] = []

    for name, spec_raw in pulls.items():
        spec = spec_raw or {}
        try:
            output = safe_project_path(project_dir, str(spec.get("output", f"data/{name}.csv")))
            fields: list[tuple[str, str]] = [
                ("token", token),
                ("content", "record"),
                ("action", "export"),
                ("format", "csv"),
                ("type", str(spec.get("type", "flat"))),
                ("csvDelimiter", str(spec.get("delimiter", ""))),
                ("rawOrLabel", str(spec.get("raw_or_label", "raw"))),
                ("rawOrLabelHeaders", str(spec.get("raw_or_label_headers", "raw"))),
                ("exportCheckboxLabel", "true" if spec.get("export_checkbox_label") else "false"),
                ("returnFormat", "json"),
            ]
            for i, value in enumerate(spec.get("forms", []) or []):
                fields.append((f"forms[{i}]", str(value)))
            for i, value in enumerate(spec.get("fields", []) or []):
                fields.append((f"fields[{i}]", str(value)))
            for i, value in enumerate(spec.get("records", []) or []):
                fields.append((f"records[{i}]", str(value)))
            if spec.get("filter_logic"):
                fields.append(("filterLogic", str(spec["filter_logic"])))

            logger.info("REDCap pull '%s' -> %s", name, output)
            body, _ = http_request(
                server,
                method="POST",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=urllib.parse.urlencode(fields).encode("utf-8"),
            )
            atomic_write_bytes(output, body)
            results.append(
                PullResult(
                    name=name,
                    status="success",
                    output=str(output),
                    bytes=output.stat().st_size,
                    rows=csv_rows(output),
                    sha256=sha256_file(output),
                )
            )
        except Exception as exc:
            results.append(PullResult(name=name, status="failed", error=str(exc)))
            logger.error("REDCap pull '%s' failed: %s", name, exc)
    return results


def run_source_pulls(
    project_dir: Path, config: dict[str, Any], logger: logging.Logger
) -> list[PullResult]:
    source = config.get("source") or {"type": "none"}
    source_type = str(source.get("type", "none")).lower()
    pulls = config.get("pulls") or {}
    if not pulls or source_type in {"none", "project"}:
        logger.info("No controller-managed data pulls configured.")
        return []
    if not isinstance(pulls, dict):
        raise ValueError("pulls must be a mapping")
    if source_type == "kobo":
        return pull_kobo(project_dir, source, pulls, logger)
    if source_type == "odk":
        return pull_odk(project_dir, source, pulls, logger)
    if source_type == "redcap":
        return pull_redcap(project_dir, source, pulls, logger)
    raise ValueError(f"Unsupported source.type: {source_type}")


# -----------------------------------------------------------------------------
# Quarto inspection/rendering
# -----------------------------------------------------------------------------


def inspect_project(project_dir: Path, quarto: str, logger: logging.Logger) -> dict[str, Any]:
    result = run_command([quarto, "inspect", str(project_dir)], logger)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("quarto inspect did not return valid JSON") from exc


def quarto_output_directory(project_dir: Path, inspection: dict[str, Any]) -> Path:
    config = inspection.get("config") or {}
    project = config.get("project") or {}
    output = project.get("output-dir")
    project_type = str(project.get("type") or "default").lower()
    if output:
        value = str(output)
    elif project_type == "book":
        value = "_book"
    elif project_type == "website":
        value = "_site"
    else:
        raise RuntimeError("Set project.output-dir in _quarto.yml for this project type")
    return safe_project_path(project_dir, value)


def prepare_quarto_output_directory(project_dir: Path, output_dir: Path, logger: logging.Logger) -> None:
    """Start every render from a clean Quarto output directory.

    This prevents stale files from an older or misconfigured render from being
    republished. The directory must be inside the project and must not be the
    project root itself.
    """
    project_root = project_dir.resolve()
    output = output_dir.resolve()
    if output == project_root:
        raise RuntimeError("Refusing to use the project root as the Quarto output directory")
    try:
        output.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError(f"Quarto output directory must be inside the project: {output}") from exc
    if output.exists():
        logger.info("Removing previous Quarto output before render: %s", output)
        if output.is_symlink() or output.is_file():
            output.unlink()
        else:
            shutil.rmtree(output)


def remove_routed_products_from_dashboard_output(
    project_dir: Path, output_dir: Path, config: dict[str, Any], logger: logging.Logger
) -> None:
    """Keep routed CSV/data products out of the common Dashboard publication.

    Products are delivered separately according to their target mapping. If a
    post-render hook copied one of those files into the Quarto output directory,
    remove that published copy before the Dashboard directory is synchronized.
    The original product beside the project is left intact for target delivery.
    """
    products = config.get("products") or {}
    for product_name, spec_raw in products.items():
        spec = spec_raw or {}
        if not spec.get("file"):
            continue
        source = safe_project_path(project_dir, str(spec["file"]))
        candidates = [output_dir / source.name]
        # Also allow an explicitly configured relative published path if needed later.
        if spec.get("dashboard_path"):
            candidates.append(output_dir / str(spec["dashboard_path"]))
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(output_dir.resolve())
            except ValueError:
                continue
            if resolved.is_file() or resolved.is_symlink():
                resolved.unlink(missing_ok=True)
                logger.info("Removed routed product from common Dashboard output: %s", resolved)



def remove_project_only_files_from_dashboard_output(
    output_dir: Path, logger: logging.Logger
) -> None:
    """Strip known project/controller artefacts that Quarto may copy into its output.

    This happens before the final safety validation. The common Dashboard is a
    publication surface, not a project-source/data surface.
    """
    output = output_dir.resolve()

    forbidden_exact = {
        "dashboard.yml", "dashboard.yaml",
        "dashboard_config.yml", "dashboard_config.yaml",
        "_quarto.yml", "_quarto.yaml",
        "secrets.env", "rclone.conf", ".env",
    }
    forbidden_dirs = {"Outputs", ".quarto", "__pycache__"}

    # Remove project-only directories wherever they occur under output.
    for path in sorted(output.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and path.name in forbidden_dirs:
            shutil.rmtree(path)
            logger.info("Removed project-only directory from Dashboard output: %s", path)

    # Remove exact project/config names and development dummy datasets.
    for path in list(output.rglob("*")):
        if not (path.is_file() or path.is_symlink()):
            continue
        if path.name in forbidden_exact or (path.name.startswith("dummy_") and path.suffix.lower() == ".csv"):
            path.unlink(missing_ok=True)
            logger.info("Removed project-only file from Dashboard output: %s", path)


def validate_dashboard_output(project_dir: Path, output_dir: Path) -> None:
    """Refuse to publish obviously unsafe/non-render output as the common dashboard."""
    project_root = project_dir.resolve()
    output = output_dir.resolve()
    if output == project_root:
        raise RuntimeError("Refusing Dashboard publication from the project root")
    try:
        output.relative_to(project_root)
    except ValueError as exc:
        raise RuntimeError("Dashboard publication source is outside the Quarto project") from exc
    if not output.is_dir():
        raise RuntimeError(f"Expected Quarto output directory not found: {output}")

    forbidden_names = {
        "dashboard.yml", "dashboard.yaml", "dashboard_config.yml", "dashboard_config.yaml",
        "_quarto.yml", "_quarto.yaml", "secrets.env", "rclone.conf", ".env"
    }
    forbidden_dirs = {"Outputs", ".quarto", "__pycache__"}
    leaked = []
    for path in output.rglob("*"):
        if path.is_dir() and path.name in forbidden_dirs:
            leaked.append(str(path.relative_to(output)) + "/")
        elif path.is_file() and (
            path.name in forbidden_names
            or (path.name.startswith("dummy_") and path.suffix.lower() == ".csv")
        ):
            leaked.append(str(path.relative_to(output)))
    if leaked:
        raise RuntimeError(
            "Refusing Dashboard publication because project/configuration files leaked into "
            f"the Quarto output: {', '.join(leaked[:10])}"
        )


def render_quarto(project_dir: Path, quarto: str, logger: logging.Logger, timeout: int) -> None:
    logger.info("Rendering Quarto project: %s", project_dir.name)
    run_command([quarto, "render", "."], logger, cwd=project_dir, timeout=timeout)


# -----------------------------------------------------------------------------
# Configuration validation and delivery planning
# -----------------------------------------------------------------------------


def load_project_config(project_dir: Path, config_name: str) -> dict[str, Any]:
    path = project_dir / config_name
    if not path.is_file():
        raise FileNotFoundError(f"Missing {config_name}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{config_name} must contain a YAML mapping")
    return data


def required_secret_names(config: dict[str, Any]) -> list[str]:
    source = config.get("source") or {}
    stype = str(source.get("type", "none")).lower()
    names: list[str] = []
    if stype in {"kobo", "redcap"} and source.get("token_env"):
        names.append(str(source["token_env"]))
    if stype == "odk":
        if source.get("bearer_token_env"):
            names.append(str(source["bearer_token_env"]))
        else:
            if source.get("email_env"):
                names.append(str(source["email_env"]))
            if source.get("password_env"):
                names.append(str(source["password_env"]))
    return names


def validate_config(project_dir: Path, config: dict[str, Any]) -> None:
    action_file_format(config)
    source = config.get("source") or {"type": "none"}
    stype = str(source.get("type", "none")).lower()
    if stype not in {"none", "project", "kobo", "odk", "redcap"}:
        raise ValueError("source.type must be one of: none, project, kobo, odk, redcap")

    delivery = config.get("delivery") or {}
    mode = str(delivery.get("mode", "local")).lower()
    if mode not in {"local", "sharepoint", "both", "none"}:
        raise ValueError("delivery.mode must be local, sharepoint, both, or none")
    if mode in {"local", "both"}:
        local = delivery.get("local") or {}
        if not local.get("root"):
            raise ValueError("delivery.local.root is required for local/both delivery")
    if mode in {"sharepoint", "both"}:
        sp = delivery.get("sharepoint") or {}
        if not sp.get("remote"):
            raise ValueError("delivery.sharepoint.remote is required for SharePoint delivery")

    archive = delivery.get("archive") or {}
    if archive and not isinstance(archive, dict):
        raise ValueError("delivery.archive must be a mapping")
    if isinstance(archive, dict):
        for key in ("current_folder", "archive_folder", "timestamp_format"):
            if key in archive and not str(archive[key]).strip():
                raise ValueError(f"delivery.archive.{key} must not be empty")
        current_name = str(archive.get("current_folder", "Current")).strip("/")
        archive_name = str(archive.get("archive_folder", "Archive")).strip("/")
        if current_name == archive_name:
            raise ValueError("delivery.archive.current_folder and archive_folder must differ")

    products = config.get("products") or {}
    if not isinstance(products, dict):
        raise ValueError("products must be a mapping")
    target_map = config.get("targets") or {}
    if not isinstance(target_map, dict):
        raise ValueError("targets must be a mapping")

    for target_name, spec_raw in target_map.items():
        spec = spec_raw or {}
        if not spec.get("folder"):
            raise ValueError(f"Target '{target_name}' requires folder")

    for product_name, spec_raw in products.items():
        spec = spec_raw or {}
        if not spec.get("file"):
            raise ValueError(f"Product '{product_name}' requires file")
        safe_project_path(project_dir, str(spec["file"]))
        route = spec.get("targets") or []
        if not isinstance(route, list):
            raise ValueError(f"Product '{product_name}'.targets must be a list")
        unknown = [x for x in route if x not in target_map]
        if unknown:
            raise ValueError(f"Product '{product_name}' references undefined target(s): {', '.join(unknown)}")

    for secret in required_secret_names(config):
        env_required(secret)

def join_remote_path(remote: str, *parts: str) -> str:
    """Build rclone's `remote:path` syntax without leaking credentials."""
    if ":" in remote:
        # Accept `name:` as well as `name`, but reject embedded paths here to keep
        # the split between remote identity and root folder obvious in YAML.
        if not remote.endswith(":") or remote.count(":") != 1:
            raise ValueError("SharePoint remote should be an rclone remote name, e.g. study_sharepoint")
        remote = remote[:-1]
    clean = [p.strip("/") for p in parts if p and p.strip("/")]
    suffix = "/".join(clean)
    return f"{remote}:{suffix}" if suffix else f"{remote}:"




def local_delivery_root(project_dir: Path, delivery: dict[str, Any]) -> Path:
    """Resolve a local delivery root; relative paths are relative to the project."""
    raw = Path(str((delivery.get("local") or {})["root"])).expanduser()
    return raw.resolve() if raw.is_absolute() else (project_dir / raw).resolve()

def targets_for_delivery(delivery: dict[str, Any]) -> list[str]:
    mode = str(delivery.get("mode", "local")).lower()
    if mode == "local":
        return ["local"]
    if mode == "sharepoint":
        return ["sharepoint"]
    if mode == "both":
        return ["local", "sharepoint"]
    return []



def archive_settings(delivery: dict[str, Any]) -> dict[str, Any]:
    """Return normalized Current/Archive policy for routed data products."""
    raw = delivery.get("archive") or {}
    return {
        "enabled": bool(raw.get("enabled", True)),
        "current_folder": str(raw.get("current_folder", "Current")).strip("/") or "Current",
        "archive_folder": str(raw.get("archive_folder", "Archive")).strip("/") or "Archive",
        "timestamp_format": str(raw.get("timestamp_format", "%Y-%m-%dT%H%M%SZ")),
    }


def zip_directory(source_dir: Path, zip_path: Path) -> int:
    """Create a ZIP of source_dir and return its byte size."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(source_dir))
                count += 1
    if count == 0:
        zip_path.unlink(missing_ok=True)
        return 0
    return zip_path.stat().st_size


def local_target_transaction(
    *,
    root: Path,
    folder: str,
    staged_new: Path,
    project_name: str,
    target_name: str,
    policy: dict[str, Any],
    logger: logging.Logger,
) -> tuple[str, Optional[str]]:
    """
    Archive existing Current/, verify the ZIP, then atomically replace Current/.

    If archiving fails, Current/ is left untouched.
    """
    target_root = root / folder
    current = target_root / policy["current_folder"]
    archive = target_root / policy["archive_folder"]
    archive.mkdir(parents=True, exist_ok=True)

    if current.is_dir() and tree_hashes(current) == tree_hashes(staged_new):
        logger.info("Current already matches verified payload: %s", target_name)
        return str(current), None

    archive_path: Optional[Path] = None
    if policy["enabled"] and current.is_dir() and any(p.is_file() for p in current.rglob("*")):
        stamp = datetime.now(timezone.utc).strftime(policy["timestamp_format"])
        safe_project = re.sub(r"[^A-Za-z0-9._-]+", "_", project_name).strip("_") or "project"
        safe_target = re.sub(r"[^A-Za-z0-9._-]+", "_", target_name).strip("_") or "target"
        archive_path = archive / f"{safe_project}_{safe_target}_{stamp}.zip"
        tmp_zip = archive / f".{archive_path.name}.staging.{os.getpid()}"
        try:
            size = zip_directory(current, tmp_zip)
            if size <= 0:
                raise RuntimeError("Archive ZIP was empty")
            os.replace(tmp_zip, archive_path)
            if not archive_path.is_file() or archive_path.stat().st_size <= 0:
                raise RuntimeError(f"Archive verification failed: {archive_path}")
            logger.info("Archive verified: %s (%d bytes)", archive_path, archive_path.stat().st_size)
        except Exception:
            tmp_zip.unlink(missing_ok=True)
            raise

    atomic_copy_directory(staged_new, current)
    if tree_hashes(current) != tree_hashes(staged_new):
        raise RuntimeError("Local Current failed SHA-256 verification")
    logger.info("Current replaced: %s", current)
    return str(current), str(archive_path) if archive_path else None


def remote_target_transaction(
    *,
    rclone: str,
    remote: str,
    root_path: str,
    folder: str,
    staged_new: Path,
    project_name: str,
    target_name: str,
    policy: dict[str, Any],
    logger: logging.Logger,
) -> tuple[str, Optional[str]]:
    """
    Archive remote Current/ into a timestamped ZIP, verify upload, then sync new Current/.

    The remote Current/ is not modified until archive creation/upload/verification succeeds.
    """
    current_remote = join_remote_path(remote, root_path, folder, policy["current_folder"])
    archive_remote_dir = join_remote_path(remote, root_path, folder, policy["archive_folder"])
    archive_remote: Optional[str] = None

    with tempfile.TemporaryDirectory(prefix="dashboard_archive_") as tmp:
        tmp_root = Path(tmp)
        old_current = tmp_root / "old_current"
        old_current.mkdir(parents=True, exist_ok=True)

        # Download the current delivery set. A missing/empty directory is not an error.
        copy_result = subprocess.run(
            [rclone, "copy", current_remote, str(old_current)],
            check=False, capture_output=True, text=True
        )
        if copy_result.returncode != 0:
            combined = (copy_result.stdout or "") + "\n" + (copy_result.stderr or "")
            low = combined.lower()
            # rclone can report a non-existent source as an error depending on backend/version.
            if "directory not found" not in low and "not found" not in low:
                raise RuntimeError(f"Could not read existing Current for archive: {combined.strip()}")
            logger.info("No existing remote Current found for %s", target_name)

        has_old = any(p.is_file() for p in old_current.rglob("*"))
        if has_old and tree_hashes(old_current) == tree_hashes(staged_new):
            logger.info("Remote Current already matches verified payload: %s", target_name)
            return current_remote, None
        if policy["enabled"] and has_old:
            stamp = datetime.now(timezone.utc).strftime(policy["timestamp_format"])
            safe_project = re.sub(r"[^A-Za-z0-9._-]+", "_", project_name).strip("_") or "project"
            safe_target = re.sub(r"[^A-Za-z0-9._-]+", "_", target_name).strip("_") or "target"
            zip_name = f"{safe_project}_{safe_target}_{stamp}.zip"
            zip_path = tmp_root / zip_name
            size = zip_directory(old_current, zip_path)
            if size <= 0:
                raise RuntimeError("Archive ZIP was empty")
            archive_remote = join_remote_path(remote, root_path, folder, policy["archive_folder"], zip_name)
            run_command([rclone, "copyto", str(zip_path), archive_remote], logger)

            # Verify by asking rclone for the remote file's metadata/size.
            verify = subprocess.run(
                [rclone, "size", archive_remote, "--json"],
                check=False, capture_output=True, text=True
            )
            if verify.returncode != 0:
                raise RuntimeError(f"Archive verification failed: {(verify.stderr or verify.stdout).strip()}")
            try:
                meta = json.loads(verify.stdout or "{}")
                remote_bytes = int(meta.get("bytes", 0))
            except Exception as exc:
                raise RuntimeError("Archive verification returned invalid JSON") from exc
            if remote_bytes != size:
                raise RuntimeError(f"Archive verification found incorrect byte count: {archive_remote}")
            logger.info("Remote archive verified: %s (%d bytes)", archive_remote, remote_bytes)

        # `sync` makes Current exactly represent the new routing set and removes stale CSVs.
        run_command([rclone, "sync", str(staged_new), current_remote], logger)
        run_command([rclone, "check", str(staged_new), current_remote, "--download"], logger)
        logger.info("Remote Current replaced: %s", current_remote)

    return current_remote, archive_remote


def distribute(
    project_dir: Path,
    output_dir: Path,
    config: dict[str, Any],
    logger: logging.Logger,
    rclone: Optional[str],
) -> list[DeliveryResult]:
    delivery = config.get("delivery") or {}
    delivery_modes = targets_for_delivery(delivery)
    results: list[DeliveryResult] = []
    if not delivery_modes:
        logger.info("Delivery disabled for this project.")
        return results

    project_name = str((config.get("project") or {}).get("name") or project_dir.name)
    dashboard_cfg = config.get("dashboard") or {}
    dashboard_folder = str(dashboard_cfg.get("folder", "Dashboard"))

    # Complete rendered dashboard/book goes to the common Dashboard folder.
    # It is intentionally NOT archived: Dashboard represents the latest published render.
    for delivery_mode in delivery_modes:
        dest_text = ""
        try:
            if delivery_mode == "local":
                root = local_delivery_root(project_dir, delivery)
                destination = root / dashboard_folder
                atomic_copy_directory(output_dir, destination)
                if tree_hashes(output_dir) != tree_hashes(destination):
                    raise RuntimeError("Local dashboard failed SHA-256 verification")
                dest_text = str(destination)
            else:
                if not rclone:
                    raise RuntimeError("rclone is required for SharePoint delivery")
                sp = delivery.get("sharepoint") or {}
                destination = join_remote_path(str(sp["remote"]), str(sp.get("root", "")), dashboard_folder)
                run_command([rclone, "sync", str(output_dir), destination], logger)
                run_command([rclone, "check", str(output_dir), destination, "--download"], logger)
                dest_text = destination
            results.append(DeliveryResult("dashboard", "everyone", delivery_mode, dest_text, "success"))
        except Exception as exc:
            results.append(DeliveryResult("dashboard", "everyone", delivery_mode, dest_text, "failed", error=str(exc)))
            logger.exception("Dashboard delivery failed (%s)", delivery_mode)

    products = config.get("products") or {}
    target_map = config.get("targets") or {}
    policy = archive_settings(delivery)

    # Resolve products first.  The analysis pipeline may create CSV internally;
    # action_files.format controls the recipient-facing representation.
    fmt = action_file_format(config)
    product_meta: dict[str, dict[str, Any]] = {}
    for product_name, product_raw in products.items():
        product = product_raw or {}
        route = product.get("targets") or []
        if not route:
            logger.info("Product has no targets; not distributed: %s", product_name)
            continue
        source_file = safe_project_path(project_dir, str(product["file"]))
        if not source_file.is_file():
            error = f"Product file does not exist: {source_file}"
            for audience in route:
                for delivery_mode in delivery_modes:
                    results.append(DeliveryResult(product_name, str(audience), delivery_mode, "", "failed", error=error))
            logger.error("%s: %s", product_name, error)
            continue
        requested_name = str(product.get("filename") or source_file.name)
        product_meta[product_name] = {
            "spec": product,
            "source": source_file,
            "filename": delivered_filename(requested_name, fmt),
            "rows": csv_rows(source_file) if source_file.suffix.lower() == ".csv" else None,
        }

    # Deliver per target as a transaction. Each Current/ directory is the exact set
    # that target should receive on this run.
    for audience, target_raw in target_map.items():
        target_cfg = target_raw or {}
        folder = str(target_cfg["folder"])
        routed_names = [
            name for name, meta in product_meta.items()
            if audience in (meta["spec"].get("targets") or [])
        ]
        if not routed_names:
            logger.info("No products routed to target: %s", audience)
            continue

        for delivery_mode in delivery_modes:
            # Stage the complete NEW Current set before touching the durable destination.
            with tempfile.TemporaryDirectory(prefix=f"dashboard_current_{audience}_") as tmp:
                staged = Path(tmp)

                try:
                    for name in routed_names:
                        meta = product_meta[name]
                        delivered = staged / meta["filename"]
                        stage_action_file(meta["source"], delivered, fmt)
                        meta["size"] = delivered.stat().st_size
                        meta["sha256"] = sha256_file(delivered)
                    if delivery_mode == "local":
                        current_dest, archive_dest = local_target_transaction(
                            root=local_delivery_root(project_dir, delivery),
                            folder=folder,
                            staged_new=staged,
                            project_name=project_name,
                            target_name=str(audience),
                            policy=policy,
                            logger=logger,
                        )
                    else:
                        if not rclone:
                            raise RuntimeError("rclone is required for SharePoint delivery")
                        sp = delivery.get("sharepoint") or {}
                        current_dest, archive_dest = remote_target_transaction(
                            rclone=rclone,
                            remote=str(sp["remote"]),
                            root_path=str(sp.get("root", "")),
                            folder=folder,
                            staged_new=staged,
                            project_name=project_name,
                            target_name=str(audience),
                            policy=policy,
                            logger=logger,
                        )

                    if archive_dest:
                        logger.info("Archived previous Current for %s (%s): %s", audience, delivery_mode, archive_dest)

                    # Mark each routed product successful only after the whole target
                    # transaction (archive + Current replacement) has completed.
                    for name in routed_names:
                        meta = product_meta[name]
                        destination = (
                            f"{current_dest.rstrip('/')}/{meta['filename']}"
                            if delivery_mode == "sharepoint"
                            else str(Path(current_dest) / meta["filename"])
                        )
                        results.append(
                            DeliveryResult(
                                name, str(audience), delivery_mode, destination, "success",
                                meta["size"], meta["rows"], meta["sha256"]
                            )
                        )
                except Exception as exc:
                    # Every product assigned to this target/mode is considered failed.
                    # Cleanup therefore retains their VM/source copies.
                    logger.exception("Target transaction failed: %s (%s)", audience, delivery_mode)
                    for name in routed_names:
                        meta = product_meta[name]
                        results.append(
                            DeliveryResult(
                                name, str(audience), delivery_mode, "", "failed",
                                meta.get("size"), meta.get("rows"), meta.get("sha256"), str(exc)
                            )
                        )

    return results

# -----------------------------------------------------------------------------
# Safe post-run cleanup
# -----------------------------------------------------------------------------

def _is_within(path: Path, parent: Path) -> bool:
    """Return True when path is parent itself or is below parent."""
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def cleanup_project_artifacts(
    project_dir: Path,
    config: dict[str, Any],
    output_dir: Optional[Path],
    deliveries: list[DeliveryResult],
    logger: logging.Logger,
) -> tuple[str, list[str], Optional[str]]:
    """
    Remove transient research-data artefacts after a run.

    Safety rules:
      * Cleanup runs only when runtime.cleanup is true (enabled only when requested in project configuration).
      * Automatic cleanup is restricted to paths inside the Quarto project.
      * The controller removes only paths it can identify from configuration:
          - source pull output files;
          - declared distributable product files;
          - the Quarto rendered output directory;
          - runtime.remove_after_run paths explicitly named by the operator.
      * A configured local delivery root is protected and is never removed.
      * Project source/configuration files are never inferred as cleanup targets.
    """
    runtime = config.get("runtime") or {}
    enabled = runtime.get("cleanup", False)
    if not enabled:
        logger.info("Runtime cleanup disabled for project.")
        return "disabled", [], None

    project_root = project_dir.resolve()
    protected: list[Path] = []

    delivery = config.get("delivery") or {}
    mode = str(delivery.get("mode", "local")).lower()
    if mode in {"local", "both"}:
        try:
            protected.append(local_delivery_root(project_dir, delivery).resolve())
        except Exception:
            pass

    candidates: list[Path] = []

    # Files downloaded by source adapters.
    pulls = config.get("pulls") or {}
    if isinstance(pulls, dict):
        for name, spec_raw in pulls.items():
            spec = spec_raw or {}
            default_ext = "csv"
            raw = str(spec.get("output", f"data/{name}.{default_ext}"))
            try:
                candidates.append(safe_project_path(project_dir, raw))
            except Exception as exc:
                logger.warning("Cleanup skipped unsafe pull path %s: %s", raw, exc)

    # Generated products are deleted only when every required delivery succeeded.
    products = config.get("products") or {}
    delivery_modes = targets_for_delivery(delivery)
    if isinstance(products, dict):
        for product_name, spec_raw in products.items():
            spec = spec_raw or {}
            route = spec.get("targets") or []
            if not spec.get("file"):
                continue
            required = {(str(a), str(m)) for a in route for m in delivery_modes}
            observed = {(d.audience, d.target) for d in deliveries if d.product == product_name and d.status == "success"}
            if required and not required.issubset(observed):
                missing = sorted(required - observed)
                logger.warning("Retaining product after incomplete delivery: %s missing=%s", product_name, missing)
                continue
            if not required:
                logger.info("Retaining unrouted product: %s", product_name)
                continue
            raw = str(spec["file"])
            try:
                candidates.append(safe_project_path(project_dir, raw))
            except Exception as exc:
                logger.warning("Cleanup skipped unsafe product path %s: %s", raw, exc)

    # Rendered Quarto output. This is recreated on every run.
    if output_dir is not None:
        candidates.append(output_dir.resolve())

    # Optional additional transient paths, still restricted to the project tree.
    extra = runtime.get("remove_after_run") or []
    if not isinstance(extra, list):
        return "failed", [], "runtime.remove_after_run must be a list"
    for raw in extra:
        try:
            candidates.append(safe_project_path(project_dir, str(raw)))
        except Exception as exc:
            logger.warning("Cleanup skipped unsafe extra path %s: %s", raw, exc)

    # Deduplicate and remove children before parents.
    unique = sorted(
        {p.resolve() for p in candidates},
        key=lambda p: len(p.parts),
        reverse=True,
    )

    removed: list[str] = []
    try:
        for path in unique:
            if path == project_root:
                logger.warning("Refusing cleanup of project root: %s", path)
                continue
            if not _is_within(path, project_root):
                logger.warning("Refusing cleanup outside project: %s", path)
                continue
            if any(_is_within(path, keep) or _is_within(keep, path) for keep in protected):
                logger.info("Cleanup protected local-delivery path: %s", path)
                continue
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
                removed.append(str(path))
                logger.info("Cleanup removed file: %s", path)
            elif path.is_dir():
                shutil.rmtree(path)
                removed.append(str(path))
                logger.info("Cleanup removed directory: %s", path)

        # Tidy common empty working directories only; never remove non-empty dirs.
        for dirname in ("data", "exports", ".runtime"):
            d = project_dir / dirname
            try:
                d.rmdir()
            except (FileNotFoundError, OSError):
                pass

        logger.info("Runtime cleanup completed: %d path(s) removed", len(removed))
        return "success", removed, None
    except Exception as exc:
        logger.exception("Runtime cleanup failed")
        return "failed", removed, str(exc)


# -----------------------------------------------------------------------------
# Audit logs
# -----------------------------------------------------------------------------


def append_delivery_csv(log_dir: Path, rid: str, project: str, rows: Iterable[DeliveryResult]) -> None:
    path = log_dir / "delivery_history.csv"
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "timestamp_utc", "run_id", "project", "product", "audience", "target",
                "destination", "status", "bytes", "rows", "sha256", "error",
            ],
        )
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    "timestamp_utc": utc_now(),
                    "run_id": rid,
                    "project": project,
                    **asdict(row),
                }
            )


# -----------------------------------------------------------------------------
# Discovery, locking, and main orchestration
# -----------------------------------------------------------------------------


def discover_projects(root: Path, config_name: str) -> list[Path]:
    if not root.is_dir():
        raise FileNotFoundError(f"Projects root not found: {root}")
    found = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.startswith("."):
            continue
        quarto = (child / "_quarto.yml").is_file() or (child / "_quarto.yaml").is_file()
        if quarto and (child / config_name).is_file():
            found.append(child)
    return sorted(found, key=lambda x: x.name.casefold())


def acquire_lock(log_dir: Path, logger: logging.Logger):
    lock_path = log_dir / "dashboard_controller.lock"
    handle = lock_path.open("w", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        logger.error("Another controller run is already active; exiting.")
        handle.close()
        return None
    handle.write(f"pid={os.getpid()} started={utc_now()}\n")
    handle.flush()
    return handle


def process_project(
    project_dir: Path,
    *,
    config_name: str,
    quarto: str,
    rclone: Optional[str],
    logger: logging.Logger,
    rid: str,
    log_dir: Path,
    timeout: int,
    validate_only: bool,
) -> ProjectResult:
    """Run one configured dashboard from acquisition through delivery and cleanup."""
    started = time.monotonic()
    pulls: list[PullResult] = []
    deliveries: list[DeliveryResult] = []
    config: Optional[dict[str, Any]] = None
    output_dir: Optional[Path] = None
    result: Optional[ProjectResult] = None

    try:
        logger.info("=== PROJECT: %s ===", project_dir.name)
        config = load_project_config(project_dir, config_name)
        validate_config(project_dir, config)
        logger.info("Configuration valid: %s", project_dir / config_name)

        if validate_only:
            return ProjectResult(
                project=project_dir.name,
                status="validated",
                duration_seconds=round(time.monotonic() - started, 2),
            )

        pulls = run_source_pulls(project_dir, config, logger)
        failed_pulls = [x for x in pulls if x.status != "success"]
        if failed_pulls:
            names = ", ".join(x.name for x in failed_pulls)
            raise RuntimeError(f"Source acquisition failed for: {names}")

        inspection = inspect_project(project_dir, quarto, logger)
        output_dir = quarto_output_directory(project_dir, inspection)
        prepare_quarto_output_directory(project_dir, output_dir, logger)
        render_quarto(project_dir, quarto, logger, timeout)

        remove_routed_products_from_dashboard_output(project_dir, output_dir, config, logger)
        remove_project_only_files_from_dashboard_output(output_dir, logger)
        validate_dashboard_output(project_dir, output_dir)

        deliveries = distribute(project_dir, output_dir, config, logger, rclone)
        append_delivery_csv(log_dir, rid, project_dir.name, deliveries)

        failed_deliveries = [x for x in deliveries if x.status != "success"]
        status = "success" if not failed_deliveries else "delivery_errors"
        result = ProjectResult(
            project=project_dir.name,
            status=status,
            duration_seconds=round(time.monotonic() - started, 2),
            pulls=pulls,
            deliveries=deliveries,
            output_directory=str(output_dir),
            error=(f"{len(failed_deliveries)} delivery operation(s) failed" if failed_deliveries else None),
        )
    except Exception as exc:
        logger.exception("Project failed: %s", project_dir.name)
        result = ProjectResult(
            project=project_dir.name,
            status="failed",
            duration_seconds=round(time.monotonic() - started, 2),
            pulls=pulls,
            deliveries=deliveries,
            output_directory=str(output_dir) if output_dir else None,
            error=str(exc),
        )
    finally:
        if not validate_only and config is not None and result is not None and result.status == "success":
            c_status, c_removed, c_error = cleanup_project_artifacts(
                project_dir, config, output_dir, deliveries, logger
            )
            result.cleanup_status = c_status
            result.cleanup_removed = c_removed
            result.cleanup_error = c_error
            if c_status == "failed":
                result.status = "cleanup_errors"
                result.error = "Project completed, but transient-data cleanup failed"
        elif not validate_only and result is not None:
            result.cleanup_status = "retained_incomplete_run"
            logger.warning("Local working files retained because the run is incomplete")

    assert result is not None
    return result

def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render and distribute Quarto dashboards")
    parser.add_argument("--projects-root", type=Path, required=True, help="Dashboard root directory containing one Quarto dashboard per immediate child folder")
    parser.add_argument("--logs-dir", type=Path, default=None, help="Audit/log directory (default: <projects-root>/_controller_logs)")
    parser.add_argument("--config-name", default="dashboard.yml", help="Per-project delivery config filename")
    parser.add_argument("--secrets", type=Path, default=None, help="Optional chmod-600 KEY=VALUE secrets file")
    parser.add_argument("--quarto-bin", default=os.environ.get("QUARTO_BIN", "quarto"))
    parser.add_argument("--rclone-bin", default=os.environ.get("RCLONE_BIN", "rclone"))
    parser.add_argument("--render-timeout", type=int, default=3600)
    parser.add_argument("--project", action="append", default=[], help="Run only this project folder name; repeatable")
    parser.add_argument("--validate", action="store_true", help="Validate YAML/secrets only; no network, render, or delivery")
    parser.add_argument(
        "--run-id",
        help="Optional stable run ID containing only letters, numbers, dot, underscore or hyphen",
    )
    return parser


def main() -> int:
    os.umask(0o077)
    args = build_arg_parser().parse_args()
    projects_root = args.projects_root.expanduser().resolve()
    log_dir = (args.logs_dir or (projects_root / "_controller_logs")).expanduser().resolve()
    rid = args.run_id or run_id()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", rid):
        raise SystemExit("--run-id may contain only letters, numbers, dot, underscore and hyphen")
    logger, log_path = configure_logging(log_dir, rid)
    logger.info("Controller run %s started", rid)
    logger.info("Dashboards root: %s", projects_root)
    logger.info("Run log: %s", log_path)

    try:
        load_secrets_file(args.secrets.expanduser().resolve() if args.secrets else None)
    except Exception as exc:
        logger.error("Could not load secrets: %s", exc)
        return 2

    lock = acquire_lock(log_dir, logger)
    if lock is None:
        return 3

    started = time.monotonic()
    started_utc = utc_now()
    results: list[ProjectResult] = []
    try:
        try:
            projects = discover_projects(projects_root, args.config_name)
            if args.project:
                wanted = set(args.project)
                projects = [p for p in projects if p.name in wanted]
                missing = wanted - {p.name for p in projects}
                if missing:
                    raise RuntimeError(f"Requested project(s) not found: {', '.join(sorted(missing))}")
            if not projects:
                raise RuntimeError("No configured Quarto dashboards found")
        except Exception as exc:
            logger.error("Project discovery failed: %s", exc)
            return 2

        quarto: Optional[str] = None
        rclone: Optional[str] = None
        if not args.validate:
            try:
                quarto = resolve_executable(args.quarto_bin)
            except Exception as exc:
                logger.error("Quarto unavailable: %s", exc)
                return 2

            # Resolve rclone only if at least one selected project actually asks for SharePoint.
            needs_rclone = False
            for p in projects:
                try:
                    cfg = load_project_config(p, args.config_name)
                    mode = str((cfg.get("delivery") or {}).get("mode", "local")).lower()
                    if mode in {"sharepoint", "both"}:
                        needs_rclone = True
                        break
                except Exception:
                    pass
            if needs_rclone:
                try:
                    rclone = resolve_executable(args.rclone_bin)
                except Exception as exc:
                    logger.error("rclone unavailable but SharePoint delivery is configured: %s", exc)
                    return 2

        for project in projects:
            results.append(
                process_project(
                    project,
                    config_name=args.config_name,
                    quarto=quarto or args.quarto_bin,
                    rclone=rclone,
                    logger=logger,
                    rid=rid,
                    log_dir=log_dir,
                    timeout=args.render_timeout,
                    validate_only=args.validate,
                )
            )

        summary = {
            "run_id": rid,
            "started_utc": started_utc,
            "finished_utc": utc_now(),
            "duration_seconds": round(time.monotonic() - started, 2),
            "validate_only": args.validate,
            "projects_root": str(projects_root),
            "projects": [asdict(x) for x in results],
        }
        summary_path = log_dir / "latest_run_summary.json"
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info("Run summary: %s", summary_path)

        bad = [x for x in results if x.status not in {"success", "validated"}]
        logger.info("Controller finished: %d project(s), %d with errors", len(results), len(bad))
        return 1 if bad else 0
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
