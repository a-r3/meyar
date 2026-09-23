"""Non-executing Alembic revision-graph introspection for `build-release`
(issue #35 PR3 security-corrective pass, PR #56).

**Why this module exists.** `meyar.ops.alembic_introspect
.get_code_alembic_heads` computes heads via real Alembic
`ScriptDirectory.get_heads()`, which loads every migration `.py` file as
a Python module — executing its top-level code — while building its
revision map. That is the correct, accepted mechanism for `status`/
`readiness`/`preflight`, which only ever point it at the *installed*
working-tree code the operator already trusts and runs.

`build-release`, however, selects and packages an arbitrary Git commit
via `--source-sha` — content that has not been reviewed as
"trusted, running code" merely by virtue of being selected. Reusing
`get_code_alembic_heads` there would mean `build-release` **executes**
migration Python from the selected commit as a side effect of computing
`alembic_heads` for the manifest — a code-execution boundary violation
for a command whose entire contract is READ selected source metadata,
never EXECUTE it (see `build_release`'s module docstring, "Provenance is
the whole point").

This module instead parses each `backend/alembic/versions/*.py` blob with
`ast` and extracts only the static `revision`/`down_revision` module-level
assignments — never `exec`, `eval`, `importlib`, `runpy`, or a Python
subprocess. `ast.literal_eval` is used to turn a value's AST node into a
Python object; it recognizes only literal shapes (strings, `None`,
numbers, tuples/lists/dicts/sets of literals, and the numeric-`+`/`-`
forms `ast.literal_eval` itself defines for constructing e.g. complex
numbers) and raises for anything else — a function call, a name lookup,
an attribute access, an f-string, or a computed expression (e.g. string
concatenation) is never evaluated, so a malicious migration file's
top-level side effects (arbitrary statements outside the two tracked
assignments) are never run and its `upgrade()`/`downgrade()` bodies are
never even reached, since this module never executes ANY code from the
parsed module — only `ast.parse` (parsing only, no execution) is used."""

from __future__ import annotations

import ast
from dataclasses import dataclass

_VERSIONS_PREFIX = "backend/alembic/versions/"


class AlembicStaticMetadataError(Exception):
    """Raised for any malformed, ambiguous, or unsafe static
    revision-metadata shape found in the selected commit's migration
    set — never for an I/O failure (callers pass already-fetched Git
    blob bytes, not file paths)."""


class _Missing:
    """Sentinel distinguishing "assignment absent" from a `None` literal
    value, which is itself a valid `down_revision`."""


_MISSING = _Missing()


@dataclass(frozen=True)
class _RevisionMeta:
    path: str
    revision: str
    down_revisions: tuple[str, ...] | None


def _is_versions_file(path: str) -> bool:
    return path.startswith(_VERSIONS_PREFIX) and path.endswith(".py")


def _extract_literal_assignment(module: ast.Module, name: str, *, path: str) -> object:
    """Finds exactly one top-level `name = <literal>` (or annotated
    `name: T = <literal>`) assignment in `module.body` and returns
    `ast.literal_eval` of its value. Returns the `_MISSING` sentinel if
    no such top-level assignment exists. Raises `AlembicStaticMetadataError`
    for more than one top-level assignment to `name`, or a value that is
    not a literal `ast.literal_eval` can safely evaluate (a call, an
    attribute lookup, a name reference, an f-string, a computed
    expression, ...) — never falls back to executing or otherwise
    evaluating the node."""
    found: list[ast.expr] = []
    for node in module.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    found.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            found.append(node.value)

    if not found:
        return _MISSING
    if len(found) > 1:
        raise AlembicStaticMetadataError(
            f"{path}: multiple top-level assignments to '{name}' — ambiguous"
        )

    try:
        return ast.literal_eval(found[0])
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise AlembicStaticMetadataError(
            f"{path}: '{name}' is not a statically evaluable literal"
        ) from exc


def _parse_down_revisions(value: object, *, path: str) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if not value:
            raise AlembicStaticMetadataError(f"{path}: 'down_revision' string must be non-empty")
        return (value,)
    if isinstance(value, (tuple, list)):
        if not value or not all(isinstance(item, str) and item for item in value):
            raise AlembicStaticMetadataError(
                f"{path}: 'down_revision' tuple/list must contain only non-empty strings"
            )
        return tuple(value)
    raise AlembicStaticMetadataError(
        f"{path}: 'down_revision' must be None, a non-empty string, or a "
        "tuple/list of non-empty strings"
    )


def _parse_revision_file(path: str, content: bytes) -> _RevisionMeta:
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AlembicStaticMetadataError(f"{path}: not valid UTF-8") from exc
    try:
        module = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise AlembicStaticMetadataError(
            f"{path}: could not parse as Python source: {exc}"
        ) from exc

    revision_value = _extract_literal_assignment(module, "revision", path=path)
    if revision_value is _MISSING:
        raise AlembicStaticMetadataError(f"{path}: missing top-level 'revision' assignment")
    if not isinstance(revision_value, str) or not revision_value:
        raise AlembicStaticMetadataError(f"{path}: 'revision' must be a non-empty string literal")

    down_value = _extract_literal_assignment(module, "down_revision", path=path)
    if down_value is _MISSING:
        raise AlembicStaticMetadataError(f"{path}: missing top-level 'down_revision' assignment")
    down_revisions = _parse_down_revisions(down_value, path=path)

    return _RevisionMeta(path=path, revision=revision_value, down_revisions=down_revisions)


def compute_static_alembic_heads(content_by_path: dict[str, bytes]) -> list[str]:
    """Computes Alembic heads for the selected commit's migration set from
    `backend/alembic/versions/*.py` blob content, without ever executing
    any of it. Deterministic: revisions are processed in sorted path
    order and the returned heads list is itself sorted.

    Validates the same graph-integrity properties a release manifest
    needs: at least one revision found; no duplicate `revision` id; every
    non-`None` `down_revision` refers to another revision in the selected
    set; at least one head remains after removing every revision that is
    itself referenced as someone else's parent."""
    version_files = {
        path: content for path, content in content_by_path.items() if _is_versions_file(path)
    }
    if not version_files:
        raise AlembicStaticMetadataError(
            "no migration files found under 'backend/alembic/versions/'"
        )

    metas = [_parse_revision_file(path, content) for path, content in sorted(version_files.items())]

    by_revision: dict[str, _RevisionMeta] = {}
    for meta in metas:
        if meta.revision in by_revision:
            raise AlembicStaticMetadataError(
                f"duplicate revision id '{meta.revision}' "
                f"({by_revision[meta.revision].path} and {meta.path})"
            )
        by_revision[meta.revision] = meta

    referenced: set[str] = set()
    for meta in metas:
        if meta.down_revisions is None:
            continue
        for parent in meta.down_revisions:
            if parent not in by_revision:
                raise AlembicStaticMetadataError(
                    f"{meta.path}: down_revision '{parent}' does not refer to any "
                    "revision in the selected migration set"
                )
            referenced.add(parent)

    heads = sorted(revision for revision in by_revision if revision not in referenced)
    if not heads:
        raise AlembicStaticMetadataError(
            "no Alembic head found in the selected commit's migration set "
            "(every revision is referenced as another revision's parent)"
        )
    return heads
