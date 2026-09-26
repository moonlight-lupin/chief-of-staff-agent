#!/usr/bin/env python3
"""Attribution merge for the briefing archive only.

Regeneration replaces bodies inside ``<!-- cos:generated ... -->`` spans in
``project_root/briefing.md``. Operator text outside those spans is kept.
Ambiguous markers refuse the file with no write. Other CoS surfaces are out
of scope for this module.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

BRIEFING_ARCHIVE_SECTIONS = (
    "header",
    "urgent",
    "calendar",
    "deadlines",
    "pipeline",
    "finance",
    "pending-high",
    "pending-medium",
    "pending-low",
    "todos",
    "inbox-summary",
    "all-clear",
    "footer",
)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_CONCURRENT = 3
EXIT_LOCK_BUSY = 4

LOCK_TIMEOUT_S = 10.0
BACKUP_KEEP = 5
NONE_TODAY = "_(none today)_"
ARCHIVE_NAME = "briefing.md"
LOG_NAME = ".cos-briefing-merge-log.jsonl"

ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKER_RE = re.compile(
    r"^<!--\s+cos:generated\s+"
    r"(?P<id>[a-z0-9]+(?:-[a-z0-9]+)*)\s+"
    r"(?:begin\s+sha256=(?P<hash>[0-9a-f]{12})|end)"
    r"\s+-->$"
)
_LINE_BREAK = re.compile(r"(\r\n|\n|\r)")
_SCALAR_BREAK = re.compile(r"\r\n|[\n\r\u2028\u2029]")

_REGISTRY_SET = frozenset(BRIEFING_ARCHIVE_SECTIONS)
_REGISTRY_INDEX = {sid: i for i, sid in enumerate(BRIEFING_ARCHIVE_SECTIONS)}


def get_project_root(config: object = None) -> Path | None:
    """Resolve ``paths.project_root``. Tests replace this module global."""
    import config_loader

    if config is None:
        try:
            config = config_loader.load_config()
        except Exception:
            return None
    return config_loader.get_project_root(config)


def body_hash(body: str) -> str:
    """12-hex sha256 of the LF-normalized, per-line-stripped body."""
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    stripped = [line.rstrip() for line in text.split("\n")]
    payload = "\n".join(stripped).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def sanitize_scalar(value: object) -> str:
    """Collapse CR/LF/U+2028/U+2029 in a single-line field to one space."""
    return _SCALAR_BREAK.sub(" ", str(value))


def validate_body(body: str) -> str:
    """Neutralize marker and fence openers without joining lines.

    ``<!--`` becomes ``<!`` plus two U+2212 minus signs, ``-->`` becomes two
    U+2212 minus signs plus ``>``, and a line whose first non-space characters
    are a fence opener (``` or ~~~) is indented one space.
    """
    if not isinstance(body, str):
        body = str(body)
    parts = _LINE_BREAK.split(body)
    out: list[str] = []
    for part in parts:
        if part in ("\r\n", "\n", "\r"):
            out.append(part)
            continue
        line = part.replace("<!--", "<!\u2212\u2212").replace("-->", "\u2212\u2212>")
        stripped = line.lstrip(" \t")
        if stripped.startswith("```") or stripped.startswith("~~~"):
            line = " " + line
        out.append(line)
    return "".join(out)


def _prepare_body(body: str) -> str:
    """Drop a forged-marker line and everything after it, then neutralize."""
    kept: list[str] = []
    for line in body.split("\n"):
        if "<!--" in line or "-->" in line:
            break
        kept.append(line)
    return validate_body("\n".join(kept))


@dataclass
class _Span:
    id: str
    begin: int
    end: int
    hash12: str


@dataclass
class _Parse:
    spans: list[_Span] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refusal: str | None = None
    unclosed_fence: bool = False
    fence_line: int | None = None


def _fence_open(content: str) -> tuple[str, int] | None:
    stripped = content.lstrip(" \t")
    for kind in ("`", "~"):
        if not stripped.startswith(kind * 3):
            continue
        n = 0
        for ch in stripped:
            if ch != kind:
                break
            n += 1
        if n >= 3:
            return kind, n
    return None


def _fence_close(content: str, fence: tuple[str, int]) -> bool:
    kind, n = fence
    stripped = content.strip()
    return len(stripped) >= n and bool(stripped) and all(ch == kind for ch in stripped)


def _scan(lines: list[tuple[str, str]], track_fences: bool) -> _Parse:
    parsed = _Parse()
    stack: list[tuple[str, int, str]] = []
    fence: tuple[str, int] | None = None
    unknown: set[str] = set()

    for idx, (content, _ending) in enumerate(lines):
        line_no = idx + 1
        if track_fences and not stack:
            if fence is None:
                opened = _fence_open(content)
                if opened is not None:
                    fence = opened
                    parsed.fence_line = line_no
                    continue
            else:
                if _fence_close(content, fence):
                    fence = None
                continue

        if "<!--" in content and "cos:generated" in content:
            match = MARKER_RE.match(content.strip())
            if match is None:
                parsed.refusal = f"malformed at line {line_no}"
                return parsed
        else:
            match = None
        if match is None:
            continue

        sid = match.group("id")
        stored = match.group("hash")
        if sid not in _REGISTRY_SET:
            if sid not in unknown:
                unknown.add(sid)
                parsed.warnings.append(
                    f"unknown section id {sid} preserved as operator text"
                )
            continue

        if stored is not None:
            stack.append((sid, idx, stored))
            continue

        if not stack:
            parsed.refusal = f"stray-end at line {line_no}"
            return parsed
        top_id, begin_idx, top_hash = stack[-1]
        if top_id != sid:
            parsed.refusal = f"crossed at line {line_no}"
            return parsed
        stack.pop()
        if stack:
            parsed.refusal = f"nested at line {line_no}"
            return parsed
        parsed.spans.append(_Span(id=sid, begin=begin_idx, end=idx, hash12=top_hash))

    if stack:
        parsed.refusal = f"unclosed-span at line {stack[-1][1] + 1}"
        return parsed
    if fence is not None:
        parsed.unclosed_fence = True
    return parsed


def _parse(lines: list[tuple[str, str]]) -> _Parse:
    first = _scan(lines, track_fences=True)
    if first.refusal:
        return first
    if not first.unclosed_fence:
        return first
    second = _scan(lines, track_fences=False)
    if second.refusal:
        return second
    line = first.fence_line or 1
    second.refusal = f"unclosed-fence at line {line}"
    return second


def _split_lines(text: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    i = 0
    n = len(text)
    while i < n:
        j = i
        while j < n and text[j] not in "\n\r":
            j += 1
        if j >= n:
            lines.append((text[i:j], ""))
            break
        if text[j] == "\r" and j + 1 < n and text[j + 1] == "\n":
            lines.append((text[i:j], "\r\n"))
            i = j + 2
        elif text[j] == "\r":
            lines.append((text[i:j], "\r"))
            i = j + 1
        else:
            lines.append((text[i:j], "\n"))
            i = j + 1
    return lines


def _join(lines: list[tuple[str, str]], bom: bytes) -> bytes:
    return bom + "".join(content + ending for content, ending in lines).encode("utf-8")


def _file_ending(lines: list[tuple[str, str]]) -> str:
    endings = [ending for _content, ending in lines if ending]
    if endings and sum(ending == "\r\n" for ending in endings) > len(endings) / 2:
        return "\r\n"
    return "\n"


def _span_body(lines: list[tuple[str, str]], span: _Span) -> str:
    return "\n".join(content for content, _ending in lines[span.begin + 1 : span.end])


def _render_span(sid: str, body: str, ending: str) -> list[tuple[str, str]]:
    begin = f"<!-- cos:generated {sid} begin sha256={body_hash(body)} -->"
    end = f"<!-- cos:generated {sid} end -->"
    rendered = [(begin, ending)]
    if body != "":
        for part in body.split("\n"):
            rendered.append((part, ending))
    rendered.append((end, ending))
    return rendered


def _conflict_block(
    span: _Span, lines: list[tuple[str, str]], ending: str
) -> list[tuple[str, str]]:
    iso = datetime.now(timezone.utc).date().isoformat()
    fence = f"<!-- cos:conflict {span.id} {iso} -->"
    block = [(fence, ending)]
    for content, line_ending in lines[span.begin + 1 : span.end]:
        block.append((content, line_ending or ending))
    block.append((fence, ending))
    return block


def _has_operator_text(lines: list[tuple[str, str]], covered: set[int]) -> bool:
    for idx, (content, _ending) in enumerate(lines):
        if idx in covered:
            continue
        if content.strip():
            return True
    return False


def _insertion_plan(
    to_insert: list[str], live_ids: set[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
    before: dict[str, list[str]] = {}
    after: dict[str, list[str]] = {}
    eof: list[str] = []
    for sid in to_insert:
        idx = _REGISTRY_INDEX[sid]
        earlier = None
        for prev in reversed(BRIEFING_ARCHIVE_SECTIONS[:idx]):
            if prev in live_ids:
                earlier = prev
                break
        if earlier is not None:
            after.setdefault(earlier, []).append(sid)
            continue
        later = None
        for nxt in BRIEFING_ARCHIVE_SECTIONS[idx + 1 :]:
            if nxt in live_ids:
                later = nxt
                break
        if later is not None:
            before.setdefault(later, []).append(sid)
        else:
            eof.append(sid)
    return before, after, eof


def _ensure_separator(lines: list[tuple[str, str]]) -> None:
    if not lines:
        return
    content, ending = lines[-1]
    if ending == "":
        lines[-1] = (content, "\n")
        lines.append(("", "\n"))


def _assemble(
    lines: list[tuple[str, str]],
    live: dict[str, _Span],
    emitted: dict[str, str],
    conflicts: set[str],
) -> tuple[list[tuple[str, str]], dict[str, str], list[str]]:
    ending = _file_ending(lines)
    by_begin = {span.begin: span for span in live.values()}
    to_insert = [sid for sid in BRIEFING_ARCHIVE_SECTIONS if sid in emitted and sid not in live]
    before, after, eof = _insertion_plan(to_insert, set(live))
    out: list[tuple[str, str]] = []
    actions: dict[str, str] = {}
    warnings: list[str] = []
    i = 0
    while i < len(lines):
        span = by_begin.get(i)
        if span is None:
            out.append(lines[i])
            i += 1
            continue
        for sid in before.get(span.id, []):
            out.extend(_render_span(sid, emitted[sid], ending))
            actions[sid] = "inserted"
        if span.id in emitted:
            body = emitted[span.id]
            action = "replaced"
        else:
            body = NONE_TODAY
            action = "empty"
        if span.id in conflicts:
            out.extend(_conflict_block(span, lines, lines[span.begin][1] or ending))
            current = _span_body(lines, span)
            if span.hash12 != body_hash(current):
                warnings.append(
                    f"hash mismatch on {span.id}; operator edit preserved outside the span"
                )
            else:
                warnings.append(f"previous body of {span.id} preserved outside the span")
            action = "replaced"
        out.extend(_render_span(span.id, body, lines[span.begin][1] or ending))
        actions[span.id] = action
        for sid in after.get(span.id, []):
            out.extend(_render_span(sid, emitted[sid], ending))
            actions[sid] = "inserted"
        i = span.end + 1
    if eof:
        _ensure_separator(out)
        for sid in eof:
            out.extend(_render_span(sid, emitted[sid], ending))
            actions[sid] = "inserted"
    return out, actions, warnings


def _result(
    status: str,
    *,
    sections: dict[str, str] | None = None,
    warnings: list[str] | None = None,
    backup: str | None = None,
    archive: str | None = None,
    exit_code: int,
    reason: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "sections": sections or {},
        "warnings": list(warnings or []),
        "backup": backup,
        "archive": archive,
        "exit_code": exit_code,
    }
    if reason:
        payload["reason"] = reason
        if reason not in payload["warnings"]:
            payload["warnings"].append(reason)
    return payload


def _audit(root: Path, artifact: str, result: dict[str, Any]) -> None:
    entry = {
        "artifact": artifact,
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": result.get("status"),
        "backup": result.get("backup"),
    }
    path = root / LOG_NAME
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


def _validate_section_map(sections: object) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(sections, dict):
        return None, "sections must be an object"
    cleaned: dict[str, str] = {}
    for key, value in sections.items():
        if not isinstance(key, str) or key not in _REGISTRY_SET:
            return None, f"undeclared section id: {key}"
        if not isinstance(value, str):
            return None, f"section {key} must be a string"
        if value != "":
            cleaned[key] = _prepare_body(value)
    return cleaned, None


def _validate_envelope(data: object) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(data, dict) or data.get("version") != 1:
        return None, "envelope version must be 1"
    return _validate_section_map(data.get("sections"))


def _load_envelope(path: Path) -> tuple[dict[str, str] | None, str | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"envelope unreadable: {exc}"
    return _validate_envelope(data)


def _maybe_delete_envelope(path: Path | None, root: Path) -> None:
    if path is None:
        return
    try:
        resolved = path.resolve()
        allowed = (root / ".cos-tmp").resolve()
        if resolved.is_relative_to(allowed) and resolved.is_file():
            resolved.unlink()
    except OSError:
        return


def _backup_dir(root: Path, archive: Path) -> Path:
    key = hashlib.sha256(str(archive.resolve()).encode("utf-8")).hexdigest()
    return root / ".cos-backups" / "attribution" / key


def _backup_copy(src: Path, dst: Path) -> None:
    shutil.copy2(src, dst)


def _make_backup(root: Path, archive: Path) -> Path:
    dest_dir = _backup_dir(root, archive)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{archive.name}.{time.time_ns()}"
    _backup_copy(archive, dest)
    return dest


def _prune_backups(dest_dir: Path, keep: int = BACKUP_KEEP) -> None:
    files = [path for path in dest_dir.iterdir() if path.is_file()]
    files.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
    for old in files[keep:]:
        old.unlink()


def _verify_unchanged(path: Path, before: os.stat_result) -> bool:
    try:
        st = path.stat()
    except OSError:
        return False
    return st.st_mtime_ns == before.st_mtime_ns and st.st_size == before.st_size


def _unchanged(path: Path, before: os.stat_result) -> bool:
    try:
        return bool(_verify_unchanged(path, before))
    except OSError:
        try:
            st = path.stat()
        except OSError:
            return False
        return st.st_mtime_ns == before.st_mtime_ns and st.st_size == before.st_size


@contextmanager
def _exclusive_lock(root: Path) -> Iterator[bool]:
    path = root / ".cos-briefing.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    acquired = False
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        if acquired:
            try:
                path.unlink()
            except OSError:
                pass


def _decode(raw: bytes) -> tuple[str | None, bytes, str | None]:
    bom = b""
    payload = raw
    if raw.startswith(b"\xef\xbb\xbf"):
        bom = b"\xef\xbb\xbf"
        payload = raw[len(bom) :]
    try:
        return payload.decode("utf-8"), bom, None
    except UnicodeDecodeError:
        return None, b"", "encoding is not utf-8"


def _select_live(spans: list[_Span]) -> tuple[dict[str, _Span], list[str]]:
    live: dict[str, _Span] = {}
    warnings: list[str] = []
    warned: set[str] = set()
    for span in spans:
        if span.id in live and span.id not in warned:
            warnings.append(
                f"duplicate section {span.id} resolved; last occurrence is live"
            )
            warned.add(span.id)
        live[span.id] = span
    return live, warnings


def _merge_locked(
    archive: Path,
    emitted: dict[str, str],
) -> dict[str, Any]:
    archive_s = str(archive)
    existed = archive.exists()
    raw = archive.read_bytes() if existed else b""
    before = archive.stat() if existed else None
    if existed:
        text, bom, decode_error = _decode(raw)
        if decode_error:
            return _result(
                "error",
                archive=archive_s,
                exit_code=EXIT_REFUSED,
                reason=decode_error,
            )
    else:
        text, bom = "", b""
    assert text is not None
    lines = _split_lines(text)
    parsed = _parse(lines)
    if parsed.refusal:
        return _result(
            "refused",
            warnings=parsed.warnings,
            archive=archive_s,
            exit_code=EXIT_REFUSED,
            reason=parsed.refusal,
        )

    live, dup_warnings = _select_live(parsed.spans)
    warnings = [*parsed.warnings, *dup_warnings]
    covered: set[int] = set()
    for span in live.values():
        covered.update(range(span.begin, span.end + 1))
    # A matching hash is a normal replace when operator text sits outside the
    # live spans (annotations, legacy prose, an earlier duplicate). A body
    # change in a file that is only live spans is treated as a conflict so the
    # previous body is kept: the conflict fixtures are a single matching-hash
    # span and require that preservation. A real stored-hash mismatch conflicts
    # either way.
    outside = _has_operator_text(lines, covered)
    conflicts: set[str] = set()
    for span in live.values():
        current = _span_body(lines, span)
        incoming = emitted[span.id] if span.id in emitted else NONE_TODAY
        mismatch = span.hash12 != body_hash(current)
        if mismatch or (incoming != current and not outside):
            conflicts.add(span.id)
    if not live and _has_operator_text(lines, set()):
        warnings.append("legacy artifact adopted; previous generated content preserved")

    new_lines, actions, assemble_warnings = _assemble(lines, live, emitted, conflicts)
    warnings.extend(assemble_warnings)
    new_bytes = _join(new_lines, bom)
    original = raw if existed else None
    if existed and new_bytes == original:
        return _result(
            "noop",
            sections=actions,
            warnings=warnings,
            archive=archive_s,
            exit_code=EXIT_OK,
        )

    need_backup = bool(conflicts) or outside
    if not existed:
        need_backup = False

    tmp = archive.with_name(f".{archive.name}.{os.getpid()}.tmp")
    backup_path: Path | None = None
    try:
        tmp.write_bytes(new_bytes)
        if need_backup and existed:
            try:
                backup_path = _make_backup(archive.parent, archive)
            except OSError as exc:
                tmp.unlink(missing_ok=True)
                warnings.append(f"backup failed: {exc}")
                return _result(
                    "error",
                    sections=actions,
                    warnings=warnings,
                    archive=archive_s,
                    exit_code=EXIT_REFUSED,
                    reason="backup failed",
                )
        if before is not None and not _unchanged(archive, before):
            tmp.unlink(missing_ok=True)
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
            return _result(
                "refused",
                sections={sid: "quarantined" for sid in actions},
                warnings=warnings,
                archive=archive_s,
                exit_code=EXIT_CONCURRENT,
                reason="concurrent edit detected",
            )
        os.replace(tmp, archive)
        if backup_path is not None:
            _prune_backups(backup_path.parent)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)

    return _result(
        "merged",
        sections=actions,
        warnings=warnings,
        backup=str(backup_path) if backup_path else None,
        archive=archive_s,
        exit_code=EXIT_OK,
    )


def merge(
    artifact: str = "briefing",
    sections: dict[str, str] | None = None,
    envelope_path: str | None = None,
    envelope: object = None,
    config: object = None,
) -> dict[str, Any]:
    """Merge generated sections into the briefing archive.

    ``sections`` is a map of registry id to markdown body. Empty string and
    missing keys are not emitted. ``envelope`` / ``envelope_path`` carry the
    ``{"version": 1, "sections": {...}}`` document instead.
    """
    root = get_project_root(config)
    if root is None:
        return _result("error", exit_code=EXIT_REFUSED, reason="project root unresolved")
    root = Path(root)
    archive = root / ARCHIVE_NAME if artifact == "briefing" else None
    archive_s = str(archive) if archive is not None else None

    if artifact != "briefing":
        result = _result(
            "error",
            archive=archive_s,
            exit_code=EXIT_REFUSED,
            reason=f"unsupported artifact: {artifact}",
        )
        _audit(root, artifact, result)
        return result

    env_file = Path(envelope_path) if envelope_path else None
    if sections is not None:
        emitted, err = _validate_section_map(sections)
    elif envelope is not None:
        emitted, err = _validate_envelope(envelope)
    elif env_file is not None:
        emitted, err = _load_envelope(env_file)
    else:
        emitted, err = {}, None

    if err or emitted is None:
        _maybe_delete_envelope(env_file, root)
        result = _result(
            "error",
            archive=archive_s,
            exit_code=EXIT_REFUSED,
            reason=err or "invalid sections",
        )
        _audit(root, artifact, result)
        return result

    try:
        with _exclusive_lock(root) as acquired:
            if not acquired:
                result = _result(
                    "error",
                    archive=archive_s,
                    exit_code=EXIT_LOCK_BUSY,
                    reason="lock busy",
                )
            else:
                result = _merge_locked(archive, emitted)
            _audit(root, artifact, result)
            return result
    finally:
        _maybe_delete_envelope(env_file, root)


def run_cli(argv: list[str] | None = None) -> int:
    """``merge --artifact briefing --sections <envelope.json>``. No ``--target``."""
    parser = argparse.ArgumentParser(prog="briefing_attribution")
    sub = parser.add_subparsers(dest="cmd", required=True)
    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--artifact", required=True)
    merge_parser.add_argument("--sections", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        if code is None or code == 0:
            return EXIT_REFUSED
        return int(code) if isinstance(code, int) else EXIT_REFUSED
    result = merge(artifact=args.artifact, envelope_path=args.sections)
    payload = {
        "status": result.get("status"),
        "sections": result.get("sections") or {},
        "warnings": result.get("warnings") or [],
        "backup": result.get("backup"),
        "archive": result.get("archive"),
    }
    json.dump(payload, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
    return int(result.get("exit_code", EXIT_REFUSED))


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run_cli(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    main()
