#!/usr/bin/env python3
"""Install and verify the optional Local Multi-GPT MCP registration."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SERVER_NAME = "multi_gpt"
RECEIPT_SCHEMA = "codex.web-gpt.local-multi-gpt-registration/v1"


class SetupError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _run(argv: Sequence[str], *, codex_home: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        list(argv), text=True, encoding="utf-8", errors="replace",
        capture_output=True, env=env, timeout=30, check=False,
    )


def _candidate_codex_commands() -> list[Path]:
    values: list[Path] = []
    for name in ("MULTI_GPT_CODEX_CLI_PATH", "CODEX_CLI_PATH"):
        if os.environ.get(name):
            values.append(Path(os.environ[name]).expanduser())
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "OpenAI" / "Codex" / "bin"
        if root.is_dir():
            values.extend(sorted(root.glob("*/codex.exe"), key=lambda item: item.stat().st_mtime_ns, reverse=True))
    for name in (("codex.exe", "codex.cmd", "codex") if os.name == "nt" else ("codex",)):
        resolved = shutil.which(name)
        if resolved:
            values.append(Path(resolved))
    unique: list[Path] = []
    seen: set[str] = set()
    for value in values:
        try:
            resolved = value.resolve(strict=True)
        except OSError:
            continue
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return unique


def resolve_codex_cli(codex_home: Path) -> tuple[Path, str]:
    failures: list[str] = []
    for candidate in _candidate_codex_commands():
        version = _run([str(candidate), "--version"], codex_home=codex_home)
        if version.returncode != 0:
            failures.append(f"{candidate}:version")
            continue
        # This catches old CLIs that exist but cannot parse the active config.
        probe = _run([str(candidate), "mcp", "list", "--json"], codex_home=codex_home)
        if probe.returncode != 0:
            failures.append(f"{candidate}:config")
            continue
        return candidate, version.stdout.strip()
    raise SetupError("COMPATIBLE_CODEX_CLI_NOT_FOUND: " + ",".join(failures))


def _registration(cli: Path, codex_home: Path) -> dict[str, Any] | None:
    result = _run([str(cli), "mcp", "get", SERVER_NAME, "--json"], codex_home=codex_home)
    if result.returncode != 0:
        if "No MCP server named" in result.stderr:
            return None
        raise SetupError("MCP_GET_FAILED: " + (result.stderr.strip() or result.stdout.strip()))
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise SetupError("MCP_GET_INVALID_JSON") from exc
    if not isinstance(value, dict):
        raise SetupError("MCP_GET_INVALID_SHAPE")
    return value


def _matches(value: dict[str, Any] | None, server: Path, cli: Path) -> bool:
    if not value or value.get("enabled") is not True:
        return False
    transport = value.get("transport")
    if not isinstance(transport, dict) or transport.get("type") != "stdio":
        return False
    args = transport.get("args")
    env = transport.get("env")
    return (
        str(transport.get("command") or "").lower() in {"node", "node.exe"}
        and isinstance(args, list)
        and len(args) == 1
        and os.path.normcase(str(Path(str(args[0])).resolve(strict=False))) == os.path.normcase(str(server))
        and isinstance(env, dict)
        and os.path.normcase(str(Path(str(env.get("MULTI_GPT_CODEX_CLI_PATH") or "")).resolve(strict=False))) == os.path.normcase(str(cli))
    )


def enable(codex_home: Path) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    server = (codex_home / "mcp_servers" / "multi-gpt" / "server.mjs").resolve()
    skill = codex_home / "skills" / "multi-gpt" / "SKILL.md"
    if not server.is_file() or not skill.is_file():
        raise SetupError("LOCAL_MULTI_GPT_FILES_MISSING")
    cli, version = resolve_codex_cli(codex_home)
    current = _registration(cli, codex_home)
    if _matches(current, server, cli):
        return {"ok": True, "enabled": True, "changed": False, "cli": str(cli), "cli_version": version, "receipt": None}
    if current is not None:
        raise SetupError("LOCAL_MULTI_GPT_REGISTRATION_CONFLICT")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S%f")
    nonce = uuid.uuid4().hex
    backup_root = codex_home / "backups" / "local-multi-gpt-registration" / f"{stamp}-{nonce}"
    receipt = codex_home / "receipts" / f"local-multi-gpt-registration-{stamp}-{nonce}.json"
    config = codex_home / "config.toml"
    existed = config.is_file()
    before_hash = _sha256(config) if existed else None
    backup = backup_root / "config.toml"
    if existed:
        _copy_atomic(config, backup)
    command = [
        str(cli), "mcp", "add", SERVER_NAME,
        "--env", f"MULTI_GPT_CODEX_CLI_PATH={cli}",
        "--", "node", str(server),
    ]
    result = _run(command, codex_home=codex_home)
    if result.returncode != 0:
        raise SetupError("MCP_ADD_FAILED: " + (result.stderr.strip() or result.stdout.strip()))
    verified = _registration(cli, codex_home)
    if not _matches(verified, server, cli):
        if existed and backup.is_file():
            _copy_atomic(backup, config)
        elif config.exists():
            config.unlink()
        raise SetupError("MCP_ADD_POSTCONDITION_FAILED")
    after_hash = _sha256(config)
    payload = {
        "schema": RECEIPT_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "codex_home": str(codex_home),
        "config": str(config),
        "config_existed": existed,
        "before_sha256": before_hash,
        "after_sha256": after_hash,
        "backup": str(backup) if existed else None,
        "server": str(server),
        "server_sha256": _sha256(server),
        "cli": str(cli),
        "cli_version": version,
    }
    _write_json_atomic(receipt, payload)
    return {"ok": True, "enabled": True, "changed": True, "cli": str(cli), "cli_version": version, "receipt": str(receipt)}


def doctor(codex_home: Path) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    server = (codex_home / "mcp_servers" / "multi-gpt" / "server.mjs").resolve()
    cli, version = resolve_codex_cli(codex_home)
    registration = _registration(cli, codex_home)
    ok = server.is_file() and (codex_home / "skills" / "multi-gpt" / "SKILL.md").is_file() and _matches(registration, server, cli)
    return {"ok": ok, "enabled": bool(registration), "registered_exactly": _matches(registration, server, cli), "cli": str(cli), "cli_version": version, "server": str(server)}


def rollback(codex_home: Path, receipt_path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    codex_home = codex_home.expanduser().resolve()
    receipt_path = receipt_path.expanduser().resolve()
    receipt_root = (codex_home / "receipts").resolve()
    try:
        receipt_path.relative_to(receipt_root)
    except ValueError as exc:
        raise SetupError("REGISTRATION_RECEIPT_NOT_OWNED") from exc
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError("REGISTRATION_RECEIPT_INVALID") from exc
    if value.get("schema") != RECEIPT_SCHEMA:
        raise SetupError("REGISTRATION_RECEIPT_SCHEMA_UNSUPPORTED")
    config = Path(str(value.get("config") or "")).expanduser().resolve()
    if config != (codex_home / "config.toml").resolve():
        raise SetupError("REGISTRATION_CONFIG_PATH_MISMATCH")
    if not config.is_file() or _sha256(config) != value.get("after_sha256"):
        raise SetupError("REGISTRATION_ROLLBACK_CONFLICT")
    if value.get("config_existed"):
        backup = Path(str(value.get("backup") or "")).expanduser().resolve()
        if not backup.is_file() or _sha256(backup) != value.get("before_sha256"):
            raise SetupError("REGISTRATION_BACKUP_INVALID")
    if dry_run:
        return {"ok": True, "status": "READY", "receipt": str(receipt_path), "dry_run": True}
    if value.get("config_existed"):
        backup = Path(str(value.get("backup") or "")).expanduser().resolve()
        _copy_atomic(backup, config)
    else:
        config.unlink()
    return {"ok": True, "status": "COMPLETE", "receipt": str(receipt_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("enable", "doctor", "rollback"))
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "enable":
            result = enable(args.codex_home)
        elif args.action == "doctor":
            result = doctor(args.codex_home)
        else:
            if args.receipt is None:
                raise SetupError("--receipt is required for rollback")
            result = rollback(args.codex_home, args.receipt, dry_run=args.dry_run)
    except (OSError, subprocess.SubprocessError, SetupError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
