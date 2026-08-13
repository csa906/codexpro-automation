from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


AUTH = load("v3_authority_smoke", ROOT / "bin" / "codexpro_exact_unit_authority.py")
PROC = load("v3_process_smoke", ROOT / "bin" / "codexpro_windows_process_identity.py")
GITISO = load("v3_git_smoke", ROOT / "bin" / "chatgpt_git_isolation.py")
RUNTIME = load("v3_runtime_smoke", ROOT / "bin" / "chatgpt_parallel_implementation_runtime.py")
DRIVER = load(
    "v3_driver_smoke",
    ROOT / "skills" / "chatgpt-pro-plan-handoff" / "scripts" / "run_parallel_implementation.py",
)
TABS = load("v3_tabs_smoke", ROOT / "bin" / "chatgpt_agbrowse_tabs.py")
BRIDGE = load("v3_bridge_smoke", ROOT / "bin" / "chatgpt_agbrowse_bridge.py")
STATE = DRIVER.STATE


def expect_code(callable_obj, code: str) -> None:
    try:
        callable_obj()
    except Exception as exc:
        if getattr(exc, "code", None) != code:
            raise AssertionError(f"expected {code}, got {type(exc).__name__}:{exc}") from exc
    else:
        raise AssertionError(f"expected {code}")


def run_authority(root: Path) -> None:
    state = root / "state"
    canonical = root / "canonical"
    state.mkdir()
    canonical.mkdir()
    topology = AUTH.derive_parent_run_topology(state, "project", "parent", "component", "unit", "attempt")
    Path(topology["parent_run_dir"]).mkdir(parents=True)
    payload = {
        "state_root": str(state),
        "canonical_project_key": "project",
        "parent_run_id": "parent",
        "component_id": "component",
        "unit_id": "unit",
        "attempt_id": "attempt",
        "canonical_project_root": str(canonical),
        "staging_common_git_dir": str(Path(topology["staging_repo_root"]) / ".git"),
        "allowed_roots": [topology["unit_workspace_root"]],
    }
    receipt = AUTH.validate_and_build(payload, phase="planned")
    assert receipt["allowed_roots"] == [receipt["unit_workspace_root"]["logical"]]
    assert len(receipt["topology_receipt_sha256"]) == 64
    bad = dict(payload)
    bad["allowed_roots"] = [str(root)]
    expect_code(lambda: AUTH.validate_and_build(bad, phase="planned"), "EXACT_UNIT_ALLOWED_ROOTS_NOT_SINGLETON")
    overlap = dict(payload)
    overlap["canonical_project_root"] = str(state)
    expect_code(lambda: AUTH.validate_and_build(overlap, phase="planned"), "EXACT_UNIT_TOPOLOGY_OVERLAP")


class FakeRunner:
    def __init__(self, rows):
        self.rows = rows

    def run_fixed_query(self, query_id, arguments):
        key = int(arguments.get("port") or arguments.get("pid") or 0)
        return self.rows.get((query_id, key), [] if query_id != "process" else {})


def run_process(root: Path) -> None:
    launcher = root / "node.exe"
    server = root / "server.exe"
    tunnel = root / "cloudflared.exe"
    launcher.write_bytes(b"node")
    server.write_bytes(b"server")
    tunnel.write_bytes(b"tunnel")

    def row(pid, parent, executable, command):
        return {
            "ProcessId": pid,
            "ParentProcessId": parent,
            "CreationDate": f"created-{pid}",
            "ExecutablePath": str(executable),
            "CommandLine": command,
        }

    rows = {
        ("listeners", 8790): [{"LocalAddress": "127.0.0.1", "LocalPort": 8790, "OwningProcess": 200}],
        ("process", 100): row(100, 1, launcher, f'"{launcher}" launcher'),
        ("process", 200): row(200, 100, server, f'"{server}" --port 8790'),
        ("process", 300): row(300, 100, tunnel, f'"{tunnel}" tunnel --url http://127.0.0.1:8790'),
        ("process", 1): row(1, 0, launcher, f'"{launcher}" root'),
        ("children", 100): [row(200, 100, server, f'"{server}" --port 8790'), row(300, 100, tunnel, f'"{tunnel}" tunnel --url http://127.0.0.1:8790')],
        ("children", 200): [],
        ("children", 300): [],
    }
    runner = FakeRunner(rows)
    listener_receipt = PROC.collect_listener_identity(
        runner=runner,
        port=8790,
        endpoint_key="https://example.invalid/mcp",
        topology_receipt_sha256="a" * 64,
        launcher_pid=100,
        launcher_creation_time_utc="created-100",
    )
    assert listener_receipt["listener_pid"] == 200
    tunnel_receipt = PROC.collect_tunnel_identity(
        runner=runner,
        launcher_pid=100,
        launcher_creation_time_utc="created-100",
        port=8790,
        endpoint_key="https://example.invalid/mcp",
        topology_receipt_sha256="b" * 64,
        public_url_sha256="c" * 64,
    )
    assert tunnel_receipt["tunnel_pid"] == 300


def run_runtime() -> None:
    manifest = {"schema": "codex.chatgpt.comprehensive-workflow/v3", "features": {"parallel_implementation_v1": True}}
    expect_code(lambda: RUNTIME.assert_feature_enabled(manifest, {}), "PARALLEL_IMPLEMENTATION_FEATURE_DISABLED")
    RUNTIME.assert_feature_enabled(manifest, {RUNTIME.FEATURE_ENV: "1"})
    graph = {
        "schema": "codex.chatgpt.implementation-graph-result/v1",
        "units": [
            {"unit_id": "u-a", "required": True, "mission": "a", "claimed_paths": ["src/a.py"], "depends_on": [], "test_ids": ["unit"]},
            {"unit_id": "u-a2", "required": True, "mission": "a2", "claimed_paths": ["src/a.py/helpers"], "depends_on": [], "test_ids": ["unit"]},
            {"unit_id": "u-b", "required": True, "mission": "b", "claimed_paths": ["src/b.py"], "depends_on": [], "test_ids": ["unit"]},
        ],
    }
    bound = RUNTIME.bind_graph(graph, baseline_oid="1" * 40)
    assert len(bound["components"]) == 2
    state = RUNTIME.initial_runtime_state(bound, parent_run_id="parent", canonical_baseline_identity_sha256="d" * 64)
    dispatch = {item["unit_id"]: item for item in RUNTIME.dispatchable_units(state)}
    RUNTIME.start_unit(state, component_id=dispatch["u-a"]["component_id"], unit_id="u-a", attempt_id="attempt-a")
    RUNTIME.start_unit(state, component_id=dispatch["u-b"]["component_id"], unit_id="u-b", attempt_id="attempt-b")
    RUNTIME.record_send_claim(state, unit_id="u-a", claim_sha256="e" * 64, invocation_state="INVOKED_MUTATION_UNKNOWN")
    assert not RUNTIME.same_claim_retry_allowed(state["units"]["u-a"])
    RUNTIME.complete_unit(state, unit_id="u-b", commit_oid="2" * 40)
    assert not RUNTIME.apply_ready(state)


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def run_git(root: Path) -> None:
    if shutil.which("git") is None:
        return
    canonical = root / "canonical"
    canonical.mkdir()
    git(canonical, "init")
    git(canonical, "config", "user.name", "Test")
    git(canonical, "config", "user.email", "test@example.invalid")
    (canonical / "src").mkdir()
    (canonical / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    (canonical / "src" / "b.py").write_text("b = 1\n", encoding="utf-8")
    git(canonical, "add", ".")
    git(canonical, "commit", "-m", "baseline")
    snapshot = GITISO.canonical_snapshot(canonical)
    assert git(canonical, "status", "--porcelain=v2", "--untracked-files=all") == ""
    staging = root / "staging"
    clone = GITISO.safe_clone(canonical, staging)
    assert clone["clone_argv"][2:5] == ["--no-local", "--no-hardlinks", "--no-checkout"]
    baseline = snapshot["head_oid"]
    unit = root / "unit"
    GITISO.create_unit_worktree(staging, unit, baseline)
    (unit / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
    GITISO.validate_unit_diff(unit, ["src/a.py"])
    commit = GITISO.deterministic_commit(unit, parent_oid=baseline, unit_id="u-a", message="u-a")
    assert commit["parent_oid"] == baseline


def run_compatibility() -> None:
    assert BRIDGE._mode_args({"mode_label": "GPT-5.6", "mode_variant": "High"})[-1] == "high"
    previous = os.environ.get("CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES")
    os.environ["CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES"] = "Very High,High"
    try:
        assert BRIDGE._mode_args({"mode_label": "GPT-5.6", "mode_variant": "Very High"})[-1] == "xhigh"
        deep = BRIDGE._mode_args({"mode_label": "Deep Research", "mode_variant": "Very High"})
        assert deep[deep.index("--effort") + 1] == "xhigh"
        assert deep[deep.index("--research") + 1] == "deep"
    finally:
        if previous is None:
            os.environ.pop("CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES", None)
        else:
            os.environ["CODEX_CHATGPT_REGULAR_MODE_CAPABILITIES"] = previous
    assert BRIDGE.app_decision_scope_matches(Path("C:/unit"), Path("C:/unit"), "parallel-exact-unit")
    assert not BRIDGE.app_decision_scope_matches(Path("C:/unit"), Path("C:/"), "parallel-exact-unit")

    parent = {
        "schema": "codex.chatgpt.agbrowse-run/v1",
        "record_kind": "parent",
        "parent_family": "parallel-implementation",
        "run_id": "parent",
        "parent_run_id": "parent",
        "workflow_id": "workflow",
        "lease_nonce": "lease",
        "project_root": "C:/project",
        "manifest_path": "C:/project/workflow.json",
        "manifest_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "requested": {"workflow": "parallel-implementation-v1", "mode": "GPT-5.6", "app_policy": "required"},
        "agbrowse": {"version": "v3"},
        "owner": {"pid": 1, "nonce": "owner", "epoch": 1},
        "phase": "PARENT_ACTIVE",
        "phase_events": [{"from": "PARENT_CREATED", "to": "PARENT_ACTIVE", "at": "2026-07-21T00:00:00Z"}],
        "children": [],
        "owned_open_tabs": 0,
    }
    assert TABS.TabLifecycle._foreign_parent_coordinator_valid(parent)
    with_identity = dict(parent)
    with_identity["session_id"] = None
    assert not TABS.TabLifecycle._foreign_parent_coordinator_valid(with_identity)
    malformed = dict(parent)
    malformed["parent_family"] = "unknown"
    assert not TABS.TabLifecycle._foreign_parent_coordinator_valid(malformed)

    legacy = dict(parent)
    legacy.pop("parent_family")
    legacy["requested"] = {"workflow": "web-multi-gpt", "mode": "GPT-5.6", "app_policy": "required"}
    assert STATE.classify_parent_family(legacy) == "web-multi"
    assert STATE.classify_parent_family(parent) == "parallel-implementation"


def run_driver(root: Path) -> None:
    if shutil.which("git") is None:
        return
    canonical = root / "canonical"
    canonical.mkdir()
    git(canonical, "init")
    git(canonical, "config", "user.name", "Test")
    git(canonical, "config", "user.email", "test@example.invalid")
    (canonical / "src").mkdir()
    (canonical / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    git(canonical, "add", ".")
    git(canonical, "commit", "-m", "baseline")

    state_root = root / "state"
    output_dir = root / "output"
    manifest_path = root / "workflow-v3.json"
    graph_path = root / "graph.json"
    check_code = "from pathlib import Path; assert Path('src/a.py').read_text(encoding='utf-8') == 'a = 2\\n'"
    manifest = {
        "schema": "codex.chatgpt.comprehensive-workflow/v3",
        "workflow_id": "driver-smoke",
        "project_root": str(canonical),
        "question": "Implement the graph.",
        "output_dir": str(output_dir),
        "state_root": str(state_root),
        "chatgpt_app_name": "CodexPro-Smoke",
        "features": {"parallel_implementation_v1": True},
        "parallel_implementation": {
            "enabled": True,
            "max_units": 4,
            "test_registry": {
                "unit": {"argv": [sys.executable, "-c", check_code], "cwd": ".", "timeout_seconds": 30},
                "full": {"argv": [sys.executable, "-c", check_code], "cwd": ".", "timeout_seconds": 30},
            },
            "full_test_ids": ["full"],
            "allowed_claim_roots": ["src"],
        },
    }
    graph = {
        "schema": "codex.chatgpt.implementation-graph-result/v1",
        "units": [
            {
                "unit_id": "u-a",
                "required": True,
                "mission": "Change src/a.py to a = 2.",
                "claimed_paths": ["src/a.py"],
                "depends_on": [],
                "test_ids": ["unit"],
            }
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    graph_path.write_text(json.dumps(graph, indent=2), encoding="utf-8")

    previous = os.environ.pop(RUNTIME.FEATURE_ENV, None)
    expect_code(lambda: DRIVER.prepare(manifest_path, graph_path), "PARALLEL_IMPLEMENTATION_FEATURE_DISABLED")
    assert not state_root.exists()
    assert not output_dir.exists()
    assert git(canonical, "status", "--porcelain=v2", "--untracked-files=all") == ""
    os.environ[RUNTIME.FEATURE_ENV] = "1"
    try:
        prepared = DRIVER.prepare(manifest_path, graph_path)
        assert prepared["status"] == "PREPARED"
        assert len(prepared["dispatches"]) == 1
        dispatch = prepared["dispatches"][0]
        unit_root = Path(dispatch["unit_workspace_root"])
        (unit_root / "src" / "a.py").write_text("a = 2\n", encoding="utf-8")
        result_path = root / "unit-result.json"
        result = {
            "schema": "codex.chatgpt.implementation-unit-result/v1",
            "unit_id": dispatch["unit_id"],
            "attempt_id": dispatch["attempt_id"],
            "input_base_oid": dispatch["input_base_oid"],
            "status": "IMPLEMENTED",
            "summary": "Changed a.",
            "changed_paths": ["src/a.py"],
            "test_results": [],
        }
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        recorded = DRIVER.record_unit(Path(prepared["parent_run_dir"]), result_path)
        assert recorded["status"] == "INTEGRATED"
        final = DRIVER.finalize(Path(prepared["parent_run_dir"]))
        assert final["status"] == "IMPLEMENTED"
        assert (canonical / "src" / "a.py").read_text(encoding="utf-8") == "a = 2\n"
        assert git(canonical, "status", "--porcelain=v2", "--untracked-files=all") == ""
    finally:
        if previous is None:
            os.environ.pop(RUNTIME.FEATURE_ENV, None)
        else:
            os.environ[RUNTIME.FEATURE_ENV] = previous


def cleanup_contract_root(path: Path, *, expected_parent: Path, attempts: int = 8) -> None:
    """Tolerate short Windows Git/antivirus races without leaking test repos."""
    expected_parent = expected_parent.resolve()
    resolved = path.resolve()
    if resolved.parent != expected_parent or not resolved.name.startswith("codexpro-v3-contract-"):
        raise RuntimeError(f"refusing unsafe contract cleanup: {resolved}")

    def clear_readonly(function, target, error):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            # Python 3.11's shutil callback receives sys.exc_info(), while
            # newer onexc receives the exception directly.  The release
            # matrix intentionally stays on 3.11, so preserve both shapes.
            if isinstance(error, tuple) and len(error) > 1:
                raise error[1]
            raise error

    for attempt in range(max(1, attempts)):
        try:
            if os.name == "nt":
                for candidate in resolved.rglob("*"):
                    try:
                        os.chmod(candidate, stat.S_IREAD | stat.S_IWRITE)
                    except OSError:
                        pass
                shutil.rmtree(resolved, onerror=clear_readonly)
            else:
                # POSIX directories require the execute bit for traversal.
                # Windows-style chmod(0600) would make cleanup fail on macOS.
                shutil.rmtree(resolved)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.2 * (attempt + 1))


def main() -> int:
    results: list[dict[str, str]] = []
    git_config = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "gc.auto",
        "GIT_CONFIG_VALUE_0": "0",
        "GIT_CONFIG_KEY_1": "maintenance.auto",
        "GIT_CONFIG_VALUE_1": "false",
    }
    previous_git_config = {name: os.environ.get(name) for name in git_config}
    os.environ.update(git_config)
    temp_parent = Path(tempfile.gettempdir()).resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        temp_parent = (Path(os.environ["LOCALAPPDATA"]) / "Temp").resolve()
        temp_parent.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="codexpro-v3-contract-", dir=temp_parent))
    try:
        for name, function in (
            ("authority", run_authority),
            ("process-identity", run_process),
            ("runtime", lambda _: run_runtime()),
            ("compatibility", lambda _: run_compatibility()),
            ("git-isolation", run_git),
            ("driver-e2e", run_driver),
        ):
            target = base / name
            target.mkdir()
            try:
                function(target)
            except Exception as exc:
                print(json.dumps({
                    "status": "FAIL",
                    "test": name,
                    "error_type": type(exc).__name__,
                    "error_code": str(getattr(exc, "code", "")),
                    "error": str(exc),
                    "evidence": getattr(exc, "evidence", None),
                }, sort_keys=True, default=str), file=sys.stderr)
                raise
            results.append({"name": name, "result": "PASS"})
    finally:
        try:
            cleanup_contract_root(base, expected_parent=temp_parent)
        finally:
            for name, previous in previous_git_config.items():
                if previous is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = previous
    print(json.dumps({"status": "PASS", "tests": results}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
