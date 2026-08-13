import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp_servers" / "multi-gpt" / "server.mjs"
MCP_PROCESS_TIMEOUT_SECONDS = 30


def mcp_response(method: str, params: dict) -> dict:
    request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    completed = subprocess.run(
        ["node", str(SERVER)],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=True,
        # CI can take longer than a local shell to cold-start Node and load the
        # MCP server. Keep this bounded so a genuinely hung server still fails.
        timeout=MCP_PROCESS_TIMEOUT_SECONDS,
    )
    return json.loads(completed.stdout.strip())


def mcp_tools() -> dict[str, dict]:
    response = mcp_response("tools/list", {})
    return {tool["name"]: tool for tool in response["result"]["tools"]}


def test_mcp_schema_exposes_only_the_fixed_execution_contract() -> None:
    tool = mcp_tools()["multi_gpt_start"]
    properties = tool["inputSchema"]["properties"]
    assert properties["model"]["enum"] == ["gpt-5.6-luna"]
    assert properties["reasoning_effort"]["enum"] == ["max"]


def test_mcp_rejects_noncontract_overrides_before_a_job_or_child_starts() -> None:
    for arguments in (
        {"prompt": "contract test", "model": "gpt-5.6-sol"},
        {"prompt": "contract test", "reasoning_effort": "high"},
        {"prompt": "contract test", "reasoning_effort": "xhigh"},
    ):
        response = mcp_response(
            "tools/call", {"name": "multi_gpt_start", "arguments": arguments}
        )
        payload = json.loads(response["result"]["content"][0]["text"])
        assert payload["ok"] is False
        assert "execution-contract violation" in payload["error"]


def test_runtime_defaults_reject_overrides_and_pin_every_stage_argv() -> None:
    source = SERVER.read_text(encoding="utf-8")

    assert "const DEFAULT_MODEL = 'gpt-5.6-luna';" in source
    assert "const DEFAULT_REASONING_EFFORT = 'max';" in source
    assert "const EXECUTION_CONTRACT = Object.freeze({" in source
    assert "model: 'gpt-5.6-luna'" in source
    assert "reasoning_effort: 'max'" in source
    assert "const model = requestedContract.model || DEFAULT_MODEL;" in source
    assert "const reasoningEffort = requestedContract.reasoning_effort || DEFAULT_REASONING_EFFORT;" in source
    assert "assertExecutionContract(model, reasoningEffort);" in source
    assert "model must be exactly ${EXECUTION_CONTRACT.model}" in source
    assert "reasoning_effort must be exactly ${EXECUTION_CONTRACT.reasoning_effort}" in source

    # Every Planner/Solver/Refiner/Merger/Judge/Organizer call converges at this
    # one launcher, which re-checks the contract before building argv.
    assert source.count("async function runCodexStage(") == 1
    launcher = source[source.index("async function runCodexStage("):source.index("function spawnWithInput(")]
    assert "assertExecutionContract(model, reasoningEffort);" in launcher
    assert "'--model', EXECUTION_CONTRACT.model," in launcher
    assert "`model_reasoning_effort=\"${reasoningEffort}\"`" in launcher
    assert "'--ignore-user-config'" not in launcher
    assert "'-c', 'responses_websockets=false'" in launcher
    assert "args.splice" not in launcher

    assert "function resolveCodexCommand()" in source
    assert "process.env.MULTI_GPT_CODEX_CLI_PATH" in source
    assert "process.env.CODEX_CLI_PATH" in source
    assert "codex.opencodex-real.cmd" in source
    assert "existsSync(openCodexReal) ? openCodexReal : 'codex.cmd'" in source


def test_job_and_result_surfaces_preserve_contract_evidence() -> None:
    source = SERVER.read_text(encoding="utf-8")
    assert "requested_contract: options.requestedContract" in source
    assert "enforced_launch_contract: options.enforcedLaunchContract" in source
    assert "requested_contract: job.requested_contract" in source
    assert "enforced_launch_contract: job.enforced_launch_contract" in source


def test_packaging_and_installer_keep_multi_gpt_opt_in() -> None:
    manifest = json.loads((ROOT / "install-manifest.json").read_text(encoding="utf-8"))
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    required = {"mcp_servers/multi-gpt/server.mjs", "skills/multi-gpt/SKILL.md"}
    assert required.isdisjoint(set(manifest["include"]))
    optional = manifest["optional_components"]["local_multi_gpt"]
    assert optional["default_install"] is False
    assert required <= set(optional["include"])
    assert optional["upstream_ref"] == "4f5e130fe12f9841eb956c69d8316871c4e955f7"
    assert required <= set(package["files"])

    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "elseif($pattern.StartsWith('mcp_servers/')){Join-Path $Root 'mcp_servers'}" in installer
    assert "Local Multi-GPT도 설치할까요? [y/N]" in (ROOT / "install-manifest.json").read_text(encoding="utf-8")
    assert "EnableLocalMultiGpt" in installer
    assert "Console]::IsInputRedirected" in installer


def test_planner_failure_preserves_codex_stderr_for_diagnosis() -> None:
    source = SERVER.read_text(encoding="utf-8")
    assert "stderr: planner.stderr || ''" in source
