from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "chatgpt_devspace_preflight.py"


def load_module():
    spec = importlib.util.spec_from_file_location("chatgpt_devspace_preflight_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_config(path: Path, roots: list[Path]) -> None:
    path.write_text(json.dumps({"allowedRoots": [str(root) for root in roots]}), encoding="utf-8")


def test_first_exact_root_qualification_is_cached_until_config_changes(tmp_path: Path) -> None:
    module = load_module()
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.json"
    write_config(config, [project])
    state = tmp_path / "qualifications"
    parse_calls: list[str] = []

    def parser(text: str):
        parse_calls.append(text)
        return json.loads(text)

    first = module.ensure_exact_root_qualified(
        project,
        config_path=config,
        qualification_root=state,
        bootstrap_path=tmp_path / "missing-bootstrap.json",
        json_loader=parser,
    )
    second = module.ensure_exact_root_qualified(
        project,
        config_path=config,
        qualification_root=state,
        bootstrap_path=tmp_path / "missing-bootstrap.json",
        json_loader=lambda _text: (_ for _ in ()).throw(AssertionError("cached config must not be reparsed")),
    )

    assert first["qualified"] is True and first["cached"] is False
    assert second["qualified"] is True and second["cached"] is True
    assert len(parse_calls) == 1

    other = tmp_path / "other"
    other.mkdir()
    write_config(config, [other])
    with pytest.raises(module.DevSpacePreflightError) as changed:
        module.ensure_exact_root_qualified(
            project,
            config_path=config,
            qualification_root=state,
            bootstrap_path=tmp_path / "missing-bootstrap.json",
        )
    assert changed.value.code == "DEVSPACE_EXACT_ROOT_UNAVAILABLE"


@pytest.mark.parametrize("registered_kind", ["parent", "child", "similar"])
def test_parent_child_or_similar_root_never_qualifies_exact_project(
    tmp_path: Path,
    registered_kind: str,
) -> None:
    module = load_module()
    parent = tmp_path / "workspace"
    project = parent / "Coin"
    child = project / "child"
    similar = parent / "Coin-copy"
    child.mkdir(parents=True)
    similar.mkdir()
    registered = {"parent": parent, "child": child, "similar": similar}[registered_kind]
    config = tmp_path / "config.json"
    write_config(config, [registered])

    with pytest.raises(module.DevSpacePreflightError) as exc:
        module.ensure_exact_root_qualified(
            project,
            config_path=config,
            qualification_root=tmp_path / "qualifications",
            bootstrap_path=tmp_path / "missing-bootstrap.json",
        )

    assert exc.value.code == "DEVSPACE_EXACT_ROOT_UNAVAILABLE"
    assert exc.value.evidence["missing_root"] == str(project.resolve())
    assert exc.value.evidence["configured_roots"] == [str(registered.resolve())]


def test_missing_root_error_includes_registration_and_preserves_existing_roots(tmp_path: Path) -> None:
    module = load_module()
    existing = tmp_path / "existing"
    project = tmp_path / "Coin"
    existing.mkdir()
    project.mkdir()
    config = tmp_path / "config.json"
    write_config(config, [existing])
    bootstrap = tmp_path / "bootstrap.json"
    bootstrap.write_text(
        json.dumps({"hostname": "device.tailnet.ts.net", "public_port": 443}),
        encoding="utf-8",
    )

    with pytest.raises(module.DevSpacePreflightError) as exc:
        module.ensure_exact_root_qualified(
            project,
            config_path=config,
            qualification_root=tmp_path / "qualifications",
            bootstrap_path=bootstrap,
        )

    evidence = exc.value.evidence
    assert evidence["registration_url"] == "https://device.tailnet.ts.net/mcp"
    root_arguments = [
        evidence["setup_argv"][index + 1]
        for index, value in enumerate(evidence["setup_argv"])
        if value == "--root"
    ]
    assert root_arguments == [str(existing.resolve()), str(project.resolve())]
