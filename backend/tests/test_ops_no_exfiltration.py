"""meyar-ops no-exfiltration/no-candidate-content acceptance (issue #35
PR1 §3/§12). Companion to `test_no_exfiltration.py`'s LLM/embedding
network-boundary proof: this module proves the ops package itself never
constructs its own HTTP client (it can only ever reach Ollama through the
existing `meyar.llm`/`meyar.embedding` provider boundaries) and that its
JSON output never carries a secret or candidate-shaped value."""

import ast
from pathlib import Path

from meyar.ops.redact import redact_database_url, safe_exception_text

_OPS_SRC_DIR = Path(__file__).resolve().parent.parent / "src" / "meyar" / "ops"


def _all_ops_modules() -> list[Path]:
    return sorted(_OPS_SRC_DIR.glob("*.py"))


def test_ops_package_never_imports_httpx_directly() -> None:
    """meyar-ops must reach Ollama only via meyar.llm/meyar.embedding's
    existing provider boundary — never construct its own HTTP client."""
    offenders = []
    for path in _all_ops_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] == "httpx" for alias in node.names):
                    offenders.append(path.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "httpx":
                    offenders.append(path.name)
    assert offenders == [], f"ops modules importing httpx directly: {offenders}"


def test_ops_package_never_imports_ollama_package_directly() -> None:
    offenders = []
    for path in _all_ops_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] == "ollama" for alias in node.names):
                    offenders.append(path.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == "ollama":
                    offenders.append(path.name)
    assert offenders == [], f"ops modules importing 'ollama' directly: {offenders}"


def test_verify_release_and_archive_safety_never_call_extractall() -> None:
    """Static proof alongside the runtime monkeypatch proof in
    test_ops_verify_release.py: neither module's source even mentions
    extractall/extract."""
    for name in ("verify_release.py", "archive_safety.py", "build_release.py"):
        source = (_OPS_SRC_DIR / name).read_text()
        assert ".extractall(" not in source
        assert ".extract(" not in source


def test_build_release_never_uses_a_shell() -> None:
    """`build_release.py` invokes Git only via fixed-argv `subprocess.run`
    (never `shell=True`, never a shell command string) — see
    `default_git_runner`. AST-based (not a raw string search) so the
    module's own docstring mentioning `shell=True` as a design invariant
    never trips this check."""
    path = _OPS_SRC_DIR / "build_release.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant):
                    assert kw.value.value is not True, "subprocess call passes shell=True"


def test_build_release_cli_never_accepts_a_release_version_override() -> None:
    """The accepted release-version authority is always the selected
    commit's backend/pyproject.toml [project].version — build-release
    must never expose a CLI override for it, nor an arbitrary archive
    path/include argument."""
    import meyar.ops.cli as cli_module

    parser = cli_module._build_parser()
    build_release_actions = next(
        action.choices["build-release"]
        for action in parser._subparsers._group_actions  # type: ignore[union-attr]
        if hasattr(action, "choices") and "build-release" in action.choices
    )
    option_strings = {s for action in build_release_actions._actions for s in action.option_strings}
    assert "--release-version" not in option_strings
    assert "--include" not in option_strings
    assert "--path" not in option_strings


def test_redact_database_url_strips_credentials() -> None:
    url = "postgresql+asyncpg://meyar:super-secret-password@localhost:5432/meyar"
    redacted = redact_database_url(url)
    assert redacted == "localhost:5432/meyar"
    assert "super-secret-password" not in redacted
    assert "@" not in redacted


def test_safe_exception_text_strips_credentials() -> None:
    exc = ValueError(
        "connection failed: postgresql+asyncpg://meyar:hunter2@127.0.0.1:1/meyar refused"
    )
    text = safe_exception_text(exc)
    assert "hunter2" not in text


def test_safe_exception_text_is_bounded_length() -> None:
    exc = ValueError("x" * 10_000)
    text = safe_exception_text(exc)
    assert len(text) <= 300
