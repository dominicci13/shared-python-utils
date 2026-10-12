"""Write one knowledge-base note per crash: the vault's durable crash history (Crash Explainer R7).

The crash archive (:mod:`fleet_state`) is pruned after 30 days; these notes are
not. Each note sits at ``<kb>/Crashes/<repo>/<YYYY-MM-DD> <error key>.md`` and
holds the record of every occurrence of that error on that day: the AI
summary of the latest one (labelled AI-generated), and for each occurrence its
time, archive folder, traceback, open tabs and log tail (see Size).

**No screenshots.** A portal screenshot can show buyer details, and the vault
syncs to OneDrive and is kept indefinitely, so ``screenshot.png`` is never
copied into it and a note embeds no image. The screenshot stays in the crash
archive (30 days for live crashes), whose folder each occurrence names, and
is attached to the ``[CRASH]`` email. A record
written before this rule may still carry a ``screenshot`` name; it is dropped
on the next write and never rendered.

**How occurrences are stored.** The note is regenerated in full on every
write. The data it is built from sits in the note itself, as one line of JSON
in a fenced block under :data:`DATA_MARKER` at the very end. A second crash
with the same key on the same day reads that line back, appends itself and
rewrites the note, so the Markdown above can never drift from the data. One
JSON line inside a fence cannot be closed early by anything it contains: a
closing fence must be a line of its own, and JSON escapes every newline. A
note whose data line is missing or unreadable (edited by hand) is never
overwritten; the write is refused with a warning.

**Untrusted text.** The traceback, tabs and log tail are captured text (an
exception message can quote a scraped page), and the summary is model output
from a model that read that page. Captured text goes only inside fenced code
blocks whose fence is longer than any backtick run in it, so Obsidian renders
none of it. Model text is rendered as text: wikilinks, embeds, Markdown links,
raw HTML, Obsidian comments, URL schemes and backticks (so no inline code and
no Dataview inline query) and Dataview inline fields (``key:: value``) are broken before it is written, and it is kept on
one source line so it cannot open a heading, a callout or a code block.
Captured text has URL query strings and token-shaped values masked, as in the
crash prompt, and each field is capped at :data:`CAPTURE_CAP` characters.

**Size.** Only the first :data:`DETAIL_CAP` occurrences keep their
traceback, tabs and log tail; later ones add a row to the occurrences table
and, while latest, the summary.

Nothing here raises into the crash handler: :func:`write_crash_note` returns
None and logs a warning on any failure, and :func:`recent_crash_notes`
returns ``"None found."``.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from seller_automation_utils.crash_explainer import (
    CRASH_NOTES_DIR,
    _cloud_only,
    _head,
    _is_third_party,
    _redact,
    _traceback_sections,
    _TRACEBACK_START,
    kb_note_path,
)

log = logging.getLogger(__name__)

__all__ = [
    "DATA_MARKER",
    "FIELD_CAP",
    "WHY_CAP",
    "STEM_CAP",
    "CAPTURE_CAP",
    "DETAIL_CAP",
    "error_key",
    "note_path",
    "write_crash_note",
    "recent_crash_notes",
]

DATA_MARKER = "<!-- crash-note data: written by code, read back on the next occurrence. Do not edit. -->"
FIELD_CAP = 1_000
WHY_CAP = 300
STEM_CAP = 120
CAPTURE_CAP = 20_000
DETAIL_CAP = 10
_REPO_CAP = 80
_KEY_PART_CAP = 60
_KEY_TYPE_CAP = 80
_HEAD_BYTES = 4_096
_FORMAT = 1
# Far past the note step's 60 s deadline, so a temporary file this old has no live writer.
_STALE_TMP_SECONDS = 600

_FRAME_LINE = re.compile(r'^  File "([^"\r\n]+)", line \d+, in ([\w.<>]+)\s*$')
_EXCEPTION_TYPE = re.compile(r"^([A-Za-z_][\w.]*)(?::|$)")
# Windows forbids <>:"/\|?* and control characters; Obsidian cannot link or embed a name with [ ] # ^ |.
_UNSAFE_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f\[\]#^]')
_RESERVED_NAMES = {
    "con", "prn", "aux", "nul", "conin$", "conout$",
    *(f"{port}{n}" for port in ("com", "lpt") for n in [*range(1, 10), "¹", "²", "³"]),
}
# An exception message can forge a traceback section, so the key's parts may hold only name characters.
_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_WWW = re.compile(r"(www)\.", re.I)
_DOUBLE_BRACKET = re.compile(r"\[(?=\[)")
_DOUBLE_PERCENT = re.compile(r"%(?=%)")
_DOUBLE_COLON = re.compile(r":(?=:)")
_TAG_UNSAFE = re.compile(r"[^\w/-]")
_RERUN = {"yes": "Yes", "yes_after_check": "Yes, after the check below", "no": "No"}


def error_key(traceback_text: str, repo_root: Path | None) -> str:
    """Name the error so the same crash on the same day lands in one note.

    ``<ExceptionType> in <file stem>-<function>``. No line number: lines move
    with every edit, and a moved line is still the same error.

    Args:
        traceback_text (str): The formatted traceback. A chained traceback is
            keyed by its LAST section, the exception that actually escaped.
        repo_root (Path | None): The crashed repo's checkout. The frame is the
            innermost one in the last section that lies inside it and outside
            site-packages and ``.venv``; when none does (or this is None), the
            innermost frame of that section; when it has none, ``unknown``.

    Returns:
        str: For example ``TimeoutException in report-download``. The
            exception's module path is dropped; angle brackets are dropped
            from names such as ``<module>``. Any character outside
            ``[A-Za-z0-9_.-]`` becomes ``_``; the exception type is capped at
            80 characters, the file stem and the function at 60 each.
            Unparseable input gives ``unknown in unknown``.
    """
    lines = traceback_text.splitlines()
    starts = [index for index, line in enumerate(lines) if line == _TRACEBACK_START]
    exception = "unknown"
    if starts:
        tail = (line for line in lines[starts[-1] + 1:] if not line.startswith("  "))
        if (match := _EXCEPTION_TYPE.match(next(tail, ""))) is not None:
            exception = _key_part(match.group(1).rsplit(".", 1)[-1], _KEY_TYPE_CAP)

    sections = _traceback_sections(traceback_text)
    frames = [match.groups() for line in (sections[-1] if sections else []) if (match := _FRAME_LINE.match(line))]
    chosen = None
    if repo_root is not None:
        # Lexical and case-insensitive: these paths are captured text, never touched on disk.
        root = Path(str(repo_root).casefold())
        chosen = next(
            (
                frame for frame in reversed(frames)
                if Path(frame[0].casefold()).is_relative_to(root) and not _is_third_party(Path(frame[0]))
            ),
            None,
        )
    if chosen is None and frames:
        chosen = frames[-1]
    if chosen is None:
        return f"{exception} in unknown"
    stem = _key_part(Path(chosen[0]).stem.strip("<>"), _KEY_PART_CAP)
    return f"{exception} in {stem}-{_key_part(chosen[1].strip('<>'), _KEY_PART_CAP)}"


def _key_part(text: str, cap: int) -> str:
    """``text`` with anything but ``[A-Za-z0-9_.-]`` replaced by ``_``, capped, or ``unknown`` when empty."""
    return _KEY_UNSAFE.sub("_", text)[:cap] or "unknown"


def _safe_name(name: str, cap: int) -> str:
    """``name`` usable as one Windows file or folder name that Obsidian can link to."""
    cleaned = _UNSAFE_NAME.sub("_", name)[:cap].rstrip(" .")
    if not cleaned:
        return "unknown"
    if cleaned.split(".")[0].casefold() in _RESERVED_NAMES:
        return f"_{cleaned}"
    return cleaned


def note_path(kb_dir: Path, repo_name: str, day: date, key: str) -> Path:
    """Where the note for one error of one repo on one day lives.

    Args:
        kb_dir (Path): Root of the knowledge base.
        repo_name (str): The crashed repo's directory name.
        day (date): The crash's local date.
        key (str): :func:`error_key` output.

    Returns:
        Path: ``kb_dir/Crashes/<repo>/<YYYY-MM-DD> <key>.md``, with characters
            Windows or Obsidian links reject replaced by ``_``, the repo folder
            capped at 80 characters and the file stem at :data:`STEM_CAP`.
    """
    stem = _safe_name(f"{day.isoformat()} {key}", STEM_CAP)
    return kb_dir / CRASH_NOTES_DIR / _safe_name(repo_name, _REPO_CAP) / f"{stem}.md"


def _model_text(value: Any, cap: int = FIELD_CAP, line_break: str = "<br>") -> str:
    """Model (or other untrusted) text made inert for Obsidian, capped, on one source line.

    Mirrors ``alert_utils._model_html``: the model read a scraped page, so a
    link, embed or inline code (a Dataview query) in its answer may be one the
    page planted. Line breaks are kept for the reader as ``<br>`` (added after
    ``<`` is escaped, so it is the only HTML in the result), which also stops
    any line from starting a heading, callout, list or code fence. ``line_break=" "`` suits a title or
    a frontmatter value.
    """
    text = str(value)[:cap]
    # An entity, not "\`": a code span starting "=" or "$=" is a Dataview inline query, which runs.
    text = text.replace("`", "&#96;")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("<", "&lt;")
    # Lookaheads, not str.replace: "[[[" would leave a "[[" behind.
    # Before the "[[" check: "[://x" would otherwise become "[[:]//x" after it.
    text = text.replace("://", "[:]//")
    text = _DOUBLE_BRACKET.sub("[ ", text)
    text = _DOUBLE_PERCENT.sub("% ", text)
    # "key:: value" is a Dataview inline field: a page could plant "safe_to_rerun:: yes" into queries.
    text = _DOUBLE_COLON.sub(": ", text)
    text = text.replace("![", "! [").replace("](", "] (")
    text = _WWW.sub(r"\1[.]", text)
    return line_break.join(line.strip() for line in text.split("\n"))


def _yaml(value: Any) -> str:
    """A frontmatter value as JSON, which YAML reads as the same scalar or list.

    ASCII-only, so no character can act as a YAML line break.
    """
    return json.dumps(value, ensure_ascii=True)


def _fenced(text: str) -> str:
    """``text`` in a backtick fence longer than any backtick run inside it, so it cannot close early."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") or "(empty)"
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _code_span(text: str) -> str:
    """``text`` as inline code that its own backticks cannot end early."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    ticks = "`" * (longest + 1)
    return f"{ticks} {text} {ticks}" if longest else f"{ticks}{text}{ticks}"


def _summary_fields(summary: Any) -> dict[str, Any] | None:
    """The schema's fields made inert and capped, or None when ``summary`` is not a usable answer."""
    if not isinstance(summary, dict):
        return None
    fix = summary.get("technical_fix") if isinstance(summary.get("technical_fix"), dict) else {}
    line = fix.get("line")
    return {
        "what_happened": _model_text(summary.get("what_happened", "")),
        "why": _model_text(summary.get("why", "")),
        "how_to_fix": _model_text(summary.get("how_to_fix", "")),
        "technical_fix": {
            "file": _model_text(fix["file"]) if fix.get("file") else None,
            "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
            "change": _model_text(fix.get("change", "")),
        },
        "safe_to_rerun": _model_text(summary.get("safe_to_rerun", ""), 40),
        "safe_to_rerun_reason": _model_text(summary.get("safe_to_rerun_reason", "")),
        "confidence": _model_text(summary.get("confidence", ""), 40),
    }


def _occurrence(crash_dir: Path, record: dict[str, Any], log_tail_text: str) -> dict[str, Any]:
    """One occurrence as stored in the note's data line, from ``crash.json``."""
    summary = _summary_fields(record.get("ai_summary"))
    error = record.get("ai_summary_error") or "no summary recorded"
    return {
        "at": str(record["at"]),
        "archive": str(crash_dir),
        # Redact before capping, so a cut cannot split a secret into a shape the masks miss. The
        # error sits at the end of a traceback or log.
        "traceback": _redact(str(record.get("traceback") or ""))[-CAPTURE_CAP:],
        "tabs": _redact(str(record.get("tabs") or "No tab information captured."))[:CAPTURE_CAP],
        "log_tail": _redact(log_tail_text)[-CAPTURE_CAP:] if log_tail_text else "No log tail captured.",
        "ai_summary": summary,
        "ai_summary_error": None if summary else _model_text(error, 300),
    }


def _read_data(note: Path) -> dict[str, Any] | None:
    """The data line of an existing note, or None when it is missing or unreadable."""
    lines = note.read_text(encoding="utf-8").split("\n")
    # The last marker is ours: the data block is always last, and a copy of the marker inside a
    # captured log line sits in an earlier fence.
    starts = [index for index, line in enumerate(lines) if line == DATA_MARKER]
    if not starts or len(lines) < starts[-1] + 3:
        return None
    try:
        data = json.loads(lines[starts[-1] + 2])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("occurrences"), list):
        return None
    return data


def _local_time(at: str) -> str:
    """``YYYY-MM-DD HH:MM:SS`` of an ISO timestamp, or the text made inert when it does not parse."""
    try:
        return f"{datetime.fromisoformat(at):%Y-%m-%d %H:%M:%S}"
    except ValueError:
        # Only a read-back record's time can fail to parse, and a hand-edited one can hold anything.
        return _model_text(at, 64, " ")


def _stored_text(value: Any) -> Any:
    """Read-back model text made inert again, since the data line can be edited by hand.

    Idempotent on what :func:`_model_text` already wrote: its ``<br>`` turns
    back into a line break first, and nothing is cut, because escaping can
    leave stored text longer than its cap. Dicts are walked; other values
    pass through.
    """
    if isinstance(value, dict):
        return {key: _stored_text(item) for key, item in value.items()}
    if not isinstance(value, str):
        return value
    text = value.replace("<br>", "\n")
    return _model_text(text, len(text))


def _render(data: dict[str, Any], *, automation_link: str | None) -> str:
    """The whole note, from its data."""
    occurrences = data["occurrences"]
    latest = occurrences[-1]
    # Rewriting an earlier archive leaves a read-back occurrence as the latest.
    summary = _stored_text(latest.get("ai_summary"))
    repo = data["repo"]
    front = {
        "type": "crash",
        "repo": repo,
        "automation": data["automation"],
        "date": data["date"],
        "error_key": data["error_key"],
        "occurrences": len(occurrences),
        "confidence": summary["confidence"] if summary else None,
        "safe_to_rerun": summary["safe_to_rerun"] if summary else None,
        "ai_generated": True,
        "ai_why": summary["why"].replace("<br>", " ")[:WHY_CAP] if summary else None,
        "display_name": data["display_name"],
        "tags": ["crash", _TAG_UNSAFE.sub("-", repo)],
    }
    parts = ["---", *(f"{key}: {_yaml(value)}" for key, value in front.items()), "---", ""]
    parts += [
        f"# {data['display_name']}: {_model_text(data['error_key'])} ({data['date']})",
        "",
        "> [!warning] AI-generated summary: written by a model that read the crash page and logs. "
        "Check it before acting on it.",
        "",
        ", ".join(link for link in (automation_link, "[[Crash Explainer PRD]]") if link),
        "",
        "## Summary (AI-generated, latest occurrence)",
        "",
    ]
    if summary:
        fix = summary["technical_fix"]
        where = fix["file"] or ""
        if where and fix["line"] is not None:
            where = f"{where}, line {fix['line']}"
        rerun = _RERUN.get(summary["safe_to_rerun"], summary["safe_to_rerun"])
        parts += [
            f"**What happened:** {summary['what_happened']}",
            "",
            f"**Why:** {summary['why']}",
            "",
            f"**How to fix:** {summary['how_to_fix']}",
            "",
            f"**Technical fix:** {f'{where}: ' if where else ''}{fix['change']}",
            "",
            f"**Safe to rerun:** {rerun}. {summary['safe_to_rerun_reason']}",
            "",
            f"**Confidence:** {summary['confidence'].capitalize()}",
            "",
        ]
    else:
        parts += [f"AI summary unavailable: {_stored_text(str(latest['ai_summary_error']))}", ""]

    parts += ["## Occurrences", "", "| # | Time | Archive folder |", "|---|---|---|"]
    parts += [
        f"| {number} | {_local_time(item['at'])} | {_code_span(item['archive']).replace('|', chr(92) + '|')} |"
        for number, item in enumerate(occurrences, 1)
    ]
    if len(occurrences) > DETAIL_CAP:
        parts += ["", f"Only the first {DETAIL_CAP} occurrences keep their details below."]
    for number, item in enumerate(occurrences[:DETAIL_CAP], 1):
        parts += ["", f"## Occurrence {number}: {_local_time(item['at'])}", ""]
        parts += [f"Archive: {_code_span(item['archive'])}", ""]
        parts += ["### Traceback", "", _fenced(item["traceback"]), ""]
        parts += ["### Open tabs", "", _fenced(item["tabs"]), ""]
        parts += ["### Log tail", "", _fenced(item["log_tail"])]
    parts += ["", "## Record", "", DATA_MARKER, "```json", json.dumps(data, ensure_ascii=True), "```", ""]
    return "\n".join(parts)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` through a temporary file in the same folder and ``os.replace``.

    A writer thread abandoned at the crash handler's deadline can leave its
    ``.<stem>.*.tmp`` file behind; any older than :data:`_STALE_TMP_SECONDS`
    is deleted here first.

    Raises:
        OSError: When the file cannot be written; the temporary file is removed.
    """
    prefix = f".{path.stem[:40]}."
    cutoff = time.time() - _STALE_TMP_SECONDS
    for stale in path.parent.glob(f"{glob.escape(prefix)}*.tmp"):
        try:
            if stale.is_file() and stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            continue
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        # OneDrive and virus scanners hold a just-written file open for a moment.
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.1)
    finally:
        tmp.unlink(missing_ok=True)


def write_crash_note(
    crash_dir: Path,
    *,
    kb_dir: Path | None,
    repo_root: Path | None,
    log_tail_text: str = "",
) -> Path | None:
    """Create the knowledge-base note for an archived crash, or add an occurrence to it. Never raises.

    The same repo with the same :func:`error_key` on the same local day adds
    an occurrence to one note; anything else gets a new note. Writing the same
    archive folder twice updates its occurrence instead of adding one. The
    archived ``screenshot.png`` is never copied into the knowledge base and
    the note embeds no image: it can show buyer details, so it stays in the
    local archive folder that each occurrence names.

    Args:
        crash_dir (Path): The crash archive folder holding ``crash.json``.
        kb_dir (Path | None): Root of the knowledge base; None writes nothing.
        repo_root (Path | None): The crashed repo's checkout. Names the repo
            folder (else ``crash.json``'s ``name``) and picks the error's frame.
        log_tail_text (str): The log tail read at crash time; secrets are
            masked again here.

    Returns:
        Path | None: The note, or None when ``kb_dir`` is None or anything
            failed: an unreadable ``crash.json``, an existing note whose data
            line is unreadable or cloud-only (never overwritten), a disk
            error. Each failure is logged as a warning.
    """
    return _write_note(crash_dir, kb_dir=kb_dir, repo_root=repo_root, log_tail_text=log_tail_text)[0]


def _write_note(
    crash_dir: Path,
    *,
    kb_dir: Path | None,
    repo_root: Path | None,
    log_tail_text: str = "",
) -> tuple[Path | None, str | None]:
    """:func:`write_crash_note`, also saying why no note was written. Never raises.

    ``alert_utils.handle_crash`` records the reason in ``crash.json``
    (``kb_note_error``), so a missing note is visible without the log.

    Returns:
        tuple[Path | None, str | None]: ``(note, None)``, or ``(None, reason)``.
    """
    if kb_dir is None:
        return None, "no knowledge base folder"
    try:
        record = json.loads((crash_dir / "crash.json").read_text(encoding="utf-8"))
        day = datetime.fromisoformat(str(record["at"])).date()
        repo_name = repo_root.name if repo_root is not None else str(record["name"])
        key = error_key(str(record.get("traceback") or ""), repo_root)
        note = note_path(kb_dir, repo_name, day, key)
        note.parent.mkdir(parents=True, exist_ok=True)

        data: dict[str, Any] = {"format": _FORMAT, "occurrences": []}
        if note.exists():
            if _cloud_only(note):
                log.warning(f"Crash note not updated: {note} is online-only in OneDrive and is never downloaded here.")
                return None, "the existing note is online-only in OneDrive"
            existing = _read_data(note)
            if existing is None:
                log.warning(f"Crash note not updated: the data line in {note} is missing or unreadable.")
                return None, "the existing note's data line is missing or unreadable"
            data = existing
            for item in data["occurrences"]:
                # Written before screenshots were kept out of the vault; the name must not survive a rewrite.
                item.pop("screenshot", None)

        occurrence = _occurrence(crash_dir, record, log_tail_text)
        archives = [str(item.get("archive", "")).casefold() for item in data["occurrences"]]
        if occurrence["archive"].casefold() in archives:
            index = archives.index(occurrence["archive"].casefold())
        else:
            data["occurrences"].append(occurrence)
            index = len(data["occurrences"]) - 1

        if index >= DETAIL_CAP:
            occurrence.update(traceback=None, tabs=None, log_tail=None)
        data["occurrences"][index] = occurrence
        # Past DETAIL_CAP only the latest occurrence's summary is ever shown.
        for item in data["occurrences"][DETAIL_CAP:-1]:
            item.update(ai_summary=None, ai_summary_error=None)

        display_name = str(record.get("display_name") or record["name"])
        data.update(
            repo=repo_name,
            automation=str(record["name"]),
            display_name=_model_text(display_name, 200, " "),
            date=day.isoformat(),
            error_key=key,
        )
        automation_note = kb_note_path(kb_dir, repo_name)
        link = None
        if automation_note is not None and not _UNSAFE_NAME.search(automation_note.stem):
            link = f"[[{automation_note.stem}]]"
        _atomic_write(note, _render(data, automation_link=link).encode("utf-8"))
        return note, None
    except Exception as exc:
        # Runs inside the crash handler after the email: a failure here must cost only the note.
        log.warning(f"Could not write the knowledge-base crash note for {crash_dir}.", exc_info=True)
        return None, f"unexpected error: {type(exc).__name__}"


def _frontmatter(text: str) -> dict[str, Any]:
    """The ``key: <json>`` lines between the opening and closing ``---``; unparseable lines are skipped."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, Any] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(": ")
        if not sep:
            continue
        try:
            fields[key.strip()] = json.loads(value)
        except ValueError:
            continue
    return fields


def recent_crash_notes(kb_dir: Path | None, repo_name: str, before: date, limit: int = 5) -> str:
    """The repo's crash notes dated before ``before``, newest first, one line each. Never raises.

    Reads only the first 4 KB of each note (the frontmatter) and skips
    OneDrive cloud-only notes unopened, so it never waits on a download.

    Args:
        kb_dir (Path | None): Root of the knowledge base.
        repo_name (str): The crashed repo's directory name.
        before (date): Only notes dated strictly earlier count.
        limit (int): At most this many lines.

    Returns:
        str: ``<date>  <error_key>  (x<occurrences>)  why: <ai_why>`` lines,
            or ``"None found."``. The key is capped at :data:`STEM_CAP`
            characters and the why at :data:`WHY_CAP`; an occurrence count
            that is not a whole number shows as ``?``. The text came from a
            model and from captured tracebacks, so it is untrusted: a prompt
            must put it inside an UNTRUSTED slot, never beside its
            instructions.
    """
    if kb_dir is None:
        return "None found."
    rows: list[tuple[str, str, str]] = []
    try:
        folder = kb_dir / CRASH_NOTES_DIR / _safe_name(repo_name, _REPO_CAP)
        for note in folder.glob("*.md"):
            if _cloud_only(note):
                continue
            fields = _frontmatter(_head(note, _HEAD_BYTES))
            day = fields.get("date")
            if fields.get("type") != "crash" or not isinstance(day, str):
                continue
            try:
                if date.fromisoformat(day) >= before:
                    continue
            except ValueError:
                continue
            # One short line per note even if a hand edit put a line break or a list into a value.
            why = " ".join(str(fields.get("ai_why") or "unavailable").split())[:WHY_CAP]
            key = " ".join(str(fields.get("error_key") or "unknown").split())[:STEM_CAP]
            count = fields.get("occurrences")
            count = count if isinstance(count, int) and not isinstance(count, bool) else "?"
            line = f"{day}  {key}  (x{count})  why: {why}"
            rows.append((day, note.name, line))
    except Exception:
        log.warning(f"Could not list the crash notes of {repo_name}.", exc_info=True)
        return "None found."
    rows.sort(reverse=True)
    return "\n".join(line for _, _, line in rows[:limit]) or "None found."
