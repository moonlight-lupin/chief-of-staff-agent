#!/usr/bin/env python3
"""Neutral query model -> provider dialect compiler (with Gmail parser).

A "query model" is either:

* a plain ``str`` — Gmail search syntax, the dialect the bundled
  ``queries.yaml`` templates are written in (e.g. ``"is:unread newer_than:3d"``).
  For the ``gmail`` dialect the string is returned unchanged.  For the ``m365``
  dialect the string is *parsed* by :func:`parse_gmail_query` into a neutral
  model and then compiled to Graph ``$filter`` / ``$search`` — so the bundled
  Gmail queries produce correct Microsoft 365 results instead of being forwarded
  as opaque best-effort text.

* a ``dict`` with any of the neutral keys:
    unread(bool), read(bool), from_sender(str), from_any(list[str]),
    domain(str), to_recipient(str), newer_than_days(int), older_than_days(int),
    has_attachment(bool), subject_contains(str), subject_any(list[str]),
    filename(str), tag(str), folder(str), any_terms(list[str]), text(str),
    negations(list[str]),
    raw({"gmail": str, "m365": {"filter":.., "search":..} | str})

``compile_query(model, dialect, now=None)`` returns:

* dialect ``"gmail"`` -> a query string
  (``"is:unread from:x newer_than:2d has:attachment"``).
* dialect ``"m365"``  -> ``{"folder": str | None, "filter": str | None,
  "search": str | None}`` — folder scope is carried OUT-OF-BAND (the Graph
  messages endpoint scopes folders by URL path,
  ``/mailFolders/{well-known-or-id}/messages``, NOT by a ``$filter``), so it
  survives the KQL fold unconditionally.

The ``raw`` override wins per-dialect: if ``raw["gmail"]`` / ``raw["m365"]`` is
present it is returned directly for that dialect.

Supported Gmail operators (parser -> neutral field -> m365 output)
------------------------------------------------------------------
    is:unread / is:read       -> unread / read      -> isRead eq false/true
    in:inbox / in:anywhere    -> folder             -> out-of-band folder scope
                                                        (well-known mailFolder name:
                                                         inbox/sentitems/drafts/
                                                         deleteditems/junkemail;
                                                         anywhere/all = None)
    label:INBOX (system) ...  -> folder/unread/text -> out-of-band folder scope
                                                        (INBOX/SENT/DRAFT/TRASH/SPAM),
                                                        the unread flag (UNREAD), or
                                                        free-text $search + warning
                                                        (STARRED/IMPORTANT)
    label:X / category:X      -> tag                -> categories/any(c:c eq 'X')
                                                        (NON-system labels only)
    from:addr                 -> from_sender        -> from/emailAddress/address eq 'addr'
    from:@domain              -> domain             -> $search from:@domain
    from:(a OR b)             -> from_any           -> $search (from:a OR from:b)
    to:addr                   -> to_recipient       -> $search to:addr
    newer_than:Nd/Nw/Nm/Ny    -> newer_than_days    -> receivedDateTime ge <ts>
    older_than:Nd/Nw/Nm/Ny    -> older_than_days    -> receivedDateTime le <ts>
    has:attachment            -> has_attachment     -> hasAttachments eq true
    subject:word              -> subject_contains   -> $search subject:word
    subject:(A OR B)          -> subject_any        -> $search (subject:A OR subject:B)
    filename:pdf              -> filename           -> $search attachment:pdf
    "quoted phrase"           -> text               -> $search "quoted phrase"
    (a OR b) at top level     -> any_terms          -> $search (a OR b)
    bare word                 -> text               -> $search word
    -operator / -term         -> negations          -> $search NOT ...

Microsoft Graph rule (messages): ``$filter`` and ``$search`` may NOT be combined
on ``/messages``.  If both would be produced we FOLD everything into a single
``$search`` KQL string (``isread:false``, ``from:x``, ``hasattachments:true``,
``received>=YYYY-MM-DD``, ``subject:x`` and ``category:...`` are valid KQL) and
set ``filter=None``.  See :func:`_m365_fold_to_kql`.  Folder scope is NEVER folded
and NEVER dropped: it is resolved to a well-known mailFolder name and returned in
the out-of-band ``folder`` key regardless of whether a fold happens, because the
Graph messages endpoint scopes folders by URL path
(``/mailFolders/{name}/messages``), not by ``$filter``/``$search``.

Untranslatable-token policy (NO SILENT EMPTINESS)
-------------------------------------------------
If the parser meets a token it cannot map to a neutral field (e.g. an unknown
operator like ``weirdop:x`` or ``is:starred``), it does NOT drop it: the raw
token is carried as free-text ``$search`` and a :class:`QueryTranslationWarning`
is emitted naming the token.  Consequently ``compile_query(<non-empty string>,
"m365")`` never returns an all-``None`` result.  The non-empty check now considers
the out-of-band ``folder`` too, so a folder-only query like ``in:inbox`` is a
valid non-empty translation (``{"folder": "inbox", "filter": None,
"search": None}``).  Only when all three keys would be ``None`` is a
:class:`ValueError` raised, instead of silently returning a query that matches
everything (or nothing).

Pure functions, no I/O.  ``now`` is injectable so date math is deterministic in
tests.
"""
from __future__ import annotations

import warnings
from datetime import datetime, timedelta, timezone
from typing import Any


class QueryTranslationWarning(UserWarning):
    """Raised (as a warning) when a Gmail token cannot be cleanly translated and
    is carried through as best-effort free-text search instead of being dropped.
    """


# ── OData / KQL helpers ────────────────────────────────────────────────

def _odata_escape(value: str) -> str:
    """Escape a string literal for an OData filter (single quote doubled)."""
    return str(value).replace("'", "''")


def _needs_quote(value: str) -> bool:
    return any(c.isspace() for c in value)


def _kql_term(prefix: str, value: str) -> str:
    """Build a KQL term, quoting the value if it contains whitespace."""
    val = str(value)
    if _needs_quote(val):
        return f'{prefix}:"{val}"'
    return f"{prefix}:{val}"


def _kql_text(value: str) -> str:
    """Free-text KQL term, quoted if it contains whitespace."""
    val = str(value)
    return f'"{val}"' if _needs_quote(val) else val


def _kql_or_group(prefix: str, values: list[str]) -> str:
    """Build ``(pfx:a OR pfx:b ...)`` — no parens when there is a single value.

    ``prefix`` may be empty for a free-text OR group ``(a OR "b c")``.
    """
    if prefix:
        parts = [_kql_term(prefix, v) for v in values]
    else:
        parts = [_kql_text(v) for v in values]
    if len(parts) == 1:
        return parts[0]
    return "(" + " OR ".join(parts) + ")"


def _rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date_only(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


# ── Gmail-syntax parser ────────────────────────────────────────────────

_DURATION_UNITS = {"d": 1, "w": 7, "m": 30, "y": 365}


def _parse_duration_days(value: str) -> int | None:
    """Parse ``14d`` / ``2w`` / ``3m`` / ``1y`` -> number of days.  ``12`` (no
    unit) is treated as days.  Returns ``None`` if unparseable."""
    value = value.strip()
    if not value:
        return None
    unit = value[-1].lower()
    if unit.isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    if unit not in _DURATION_UNITS:
        return None
    try:
        n = int(value[:-1])
    except ValueError:
        return None
    return n * _DURATION_UNITS[unit]


def _read_word(s: str, i: int, n: int) -> tuple[str, int]:
    """Read a whitespace-delimited token starting at ``i``, but swallow any
    balanced ``(...)`` group or ``"..."`` phrase whole (they may contain spaces).
    Returns ``(token, next_index)``."""
    buf: list[str] = []
    while i < n and not s[i].isspace():
        c = s[i]
        if c == "(":
            depth = 0
            j = i
            while j < n:
                if s[j] == "(":
                    depth += 1
                elif s[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            buf.append(s[i:j + 1])
            i = j + 1
        elif c == '"':
            j = s.find('"', i + 1)
            if j == -1:
                buf.append(s[i:])
                i = n
            else:
                buf.append(s[i:j + 1])
                i = j + 1
        else:
            buf.append(c)
            i += 1
    return "".join(buf), i


def _tokenize(s: str) -> list[str]:
    """Split a Gmail query into top-level raw tokens (respecting quotes/parens)."""
    tokens: list[str] = []
    i, n = 0, len(s)
    while i < n:
        if s[i].isspace():
            i += 1
            continue
        word, i = _read_word(s, i, n)
        if word:
            tokens.append(word)
    return tokens


def _parse_or_terms(inner: str) -> list[str]:
    """Parse the inside of a ``(...)`` group into a list of terms, dropping the
    ``OR`` connectives and unquoting phrases."""
    terms: list[str] = []
    for tok in _tokenize(inner):
        if tok.upper() == "OR":
            continue
        if tok.startswith('"') and tok.endswith('"') and len(tok) >= 2:
            terms.append(tok[1:-1])
        else:
            terms.append(tok)
    return terms


def _group_by_or(tokens: list[str]) -> list[list[str]]:
    """Collapse ``A OR B OR C`` runs into a single group ``[A, B, C]``."""
    if not tokens:
        return []
    groups: list[list[str]] = []
    current = [tokens[0]]
    i = 1
    while i < len(tokens):
        if tokens[i].upper() == "OR" and i + 1 < len(tokens):
            current.append(tokens[i + 1])
            i += 2
        else:
            groups.append(current)
            current = [tokens[i]]
            i += 1
    groups.append(current)
    return groups


def _unquote(tok: str) -> str:
    if len(tok) >= 2 and tok.startswith('"') and tok.endswith('"'):
        return tok[1:-1]
    return tok


def _apply_operator(op: str, val: str, model: dict[str, Any], neg: bool) -> None:
    op = op.lower()

    if op == "is":
        v = val.lower()
        if v == "unread":
            model["read" if neg else "unread"] = True
        elif v == "read":
            model["unread" if neg else "read"] = True
        else:  # is:starred / is:important / ... not mappable
            _carry_unknown(f"is:{val}", model, neg)
        return

    if op == "in":
        # folder scope; negation of a folder scope is not representable.
        if neg:
            _carry_unknown(f"in:{val}", model, neg)
        else:
            model["folder"] = val
        return

    if op in ("label", "category"):
        if neg:
            model.setdefault("negations", []).append(_kql_term("category", val))
        else:
            model["tag"] = val
        return

    if op == "from":
        if val.startswith("(") and val.endswith(")"):
            terms = _parse_or_terms(val[1:-1])
            if neg:
                for t in terms:
                    model.setdefault("negations", []).append(_kql_term("from", t))
            elif len(terms) == 1:
                _apply_from_single(terms[0], model)
            else:
                model.setdefault("from_any", []).extend(terms)
            return
        if neg:
            model.setdefault("negations", []).append(_kql_term("from", val))
        else:
            _apply_from_single(val, model)
        return

    if op == "to":
        if neg:
            model.setdefault("negations", []).append(_kql_term("to", val))
        else:
            model["to_recipient"] = _unquote(val)
        return

    if op == "subject":
        if val.startswith("(") and val.endswith(")"):
            terms = _parse_or_terms(val[1:-1])
            if neg:
                for t in terms:
                    model.setdefault("negations", []).append(_kql_term("subject", t))
            elif len(terms) == 1:
                model["subject_contains"] = terms[0]
            else:
                model.setdefault("subject_any", []).extend(terms)
        else:
            v = _unquote(val)
            if neg:
                model.setdefault("negations", []).append(_kql_term("subject", v))
            else:
                model["subject_contains"] = v
        return

    if op == "newer_than":
        days = _parse_duration_days(val)
        if days is None:
            _carry_unknown(f"newer_than:{val}", model, neg)
        else:
            model["newer_than_days"] = days
        return

    if op == "older_than":
        days = _parse_duration_days(val)
        if days is None:
            _carry_unknown(f"older_than:{val}", model, neg)
        else:
            model["older_than_days"] = days
        return

    if op == "has":
        if val.lower() == "attachment":
            if neg:
                model.setdefault("negations", []).append("hasattachments:true")
            else:
                model["has_attachment"] = True
        else:
            _carry_unknown(f"has:{val}", model, neg)
        return

    if op == "filename":
        v = _unquote(val)
        if neg:
            model.setdefault("negations", []).append(_kql_term("attachment", v))
        else:
            model["filename"] = v
        return

    # Unknown operator -> do NOT drop; carry as free text + warn.
    _carry_unknown(f"{op}:{val}", model, neg)


def _apply_from_single(val: str, model: dict[str, Any]) -> None:
    val = _unquote(val)
    # A bare @domain (no local part) is a domain match, not an exact address.
    if val.startswith("@"):
        model["domain"] = val
    else:
        model["from_sender"] = val


def _warn_untranslatable(raw: str) -> None:
    """Emit a QueryTranslationWarning naming a token carried as best-effort text."""
    warnings.warn(
        f"query_compiler: could not translate token {raw!r}; "
        f"carrying it as free-text $search",
        QueryTranslationWarning,
        stacklevel=3,
    )


def _carry_unknown(raw: str, model: dict[str, Any], neg: bool) -> None:
    """Carry an untranslatable token as free text and warn (no silent drop)."""
    _warn_untranslatable(raw)
    if neg:
        model.setdefault("negations", []).append(_kql_text(raw))
    else:
        _append_text(model, raw)


def _append_text(model: dict[str, Any], term: str) -> None:
    existing = model.get("text")
    model["text"] = f"{existing} {term}" if existing else term


def parse_gmail_query(query: str) -> dict[str, Any]:
    """Parse a Gmail-syntax search string into a neutral query model dict.

    Unmapped tokens are carried as free text and a :class:`QueryTranslationWarning`
    is emitted (see module docstring).  Never raises on unknown operators.
    """
    model: dict[str, Any] = {}
    if not query or not query.strip():
        return model

    for group in _group_by_or(_tokenize(query)):
        if len(group) > 1:
            # Top-level ``A OR B`` run -> free-text any_terms (best effort).
            for tok in group:
                model.setdefault("any_terms", []).append(_unquote(tok))
            continue

        tok = group[0]
        neg = False
        if tok.startswith("-") and len(tok) > 1:
            neg = True
            tok = tok[1:]

        if tok.startswith("(") and tok.endswith(")"):
            terms = _parse_or_terms(tok[1:-1])
            if neg:
                for t in terms:
                    model.setdefault("negations", []).append(_kql_text(t))
            else:
                model.setdefault("any_terms", []).extend(terms)
            continue

        if tok.startswith('"'):
            phrase = _unquote(tok)
            if neg:
                model.setdefault("negations", []).append(_kql_text(phrase))
            else:
                _append_text(model, phrase)
            continue

        if ":" in tok:
            op, val = tok.split(":", 1)
            _apply_operator(op, val, model, neg)
            continue

        # Bare word.
        if neg:
            model.setdefault("negations", []).append(_kql_text(tok))
        else:
            _append_text(model, tok)

    return model


# ── Gmail dialect ──────────────────────────────────────────────────────

def _compile_gmail(model: dict[str, Any]) -> str:
    raw = model.get("raw")
    if isinstance(raw, dict) and raw.get("gmail"):
        return str(raw["gmail"])

    # NB: field order is kept back-compatible with the original compiler.
    parts: list[str] = []
    if model.get("unread"):
        parts.append("is:unread")
    if model.get("read"):
        parts.append("is:read")
    if model.get("from_sender"):
        parts.append(f"from:{model['from_sender']}")
    if model.get("domain"):
        parts.append(f"from:{model['domain']}")
    if model.get("from_any"):
        parts.append("from:(" + " OR ".join(model["from_any"]) + ")")
    if model.get("to_recipient"):
        parts.append(f"to:{model['to_recipient']}")
    if model.get("newer_than_days") is not None:
        parts.append(f"newer_than:{int(model['newer_than_days'])}d")
    if model.get("older_than_days") is not None:
        parts.append(f"older_than:{int(model['older_than_days'])}d")
    if model.get("has_attachment"):
        parts.append("has:attachment")
    if model.get("subject_contains"):
        subj = str(model["subject_contains"])
        parts.append(f'subject:"{subj}"' if _needs_quote(subj) else f"subject:{subj}")
    if model.get("subject_any"):
        subj = " OR ".join(
            f'"{t}"' if _needs_quote(t) else t for t in model["subject_any"]
        )
        parts.append(f"subject:({subj})")
    if model.get("filename"):
        parts.append(f"filename:{model['filename']}")
    if model.get("tag"):
        parts.append(f"label:{model['tag']}")
    if model.get("folder"):
        parts.append(f"in:{model['folder']}")
    if model.get("any_terms"):
        terms = " OR ".join(
            f'"{t}"' if _needs_quote(t) else t for t in model["any_terms"]
        )
        parts.append(f"({terms})")
    if model.get("text"):
        parts.append(str(model["text"]))
    return " ".join(parts)


# ── Microsoft 365 (Graph) dialect ──────────────────────────────────────

# Gmail ``in:`` folder-scope values (and the neutral ``folder`` field) mapped to
# Graph well-known mailFolder names.  Graph's parentFolderId is an opaque unique
# id, NOT a well-known name, so folder scope is applied via the URL path
# ``/mailFolders/{name}/messages`` — never as a ``$filter``.  Identity entries
# let a well-known name be passed through the neutral ``folder`` field directly.
_M365_IN_FOLDER_MAP = {
    "inbox": "inbox",
    "sent": "sentitems",
    "sentitems": "sentitems",
    "drafts": "drafts",
    "draft": "drafts",
    "trash": "deleteditems",
    "deleteditems": "deleteditems",
    "spam": "junkemail",
    "junk": "junkemail",
    "junkemail": "junkemail",
}

# ``in:`` / ``folder`` values meaning "no folder scope" (explicit whole-mailbox).
_M365_NO_SCOPE = {"anywhere", "all", "allmail"}

# Gmail SYSTEM labels: ``label:NAME`` (and neutral ``tag``) that are NOT Outlook
# categories but system folders.  Case-insensitive.
_M365_SYSTEM_LABEL_FOLDER = {
    "inbox": "inbox",
    "sent": "sentitems",
    "draft": "drafts",
    "drafts": "drafts",
    "trash": "deleteditems",
    "spam": "junkemail",
    "junk": "junkemail",
}


def _resolve_m365_folder(model: dict[str, Any]) -> str | None:
    """Resolve out-of-band folder scope from the ``folder`` field and any
    system-label ``tag``.

    Mutates ``model`` in place (it is a private copy owned by :func:`_compile_m365`):
      * pops ``folder`` — folder scope is carried out-of-band, never as a
        parentFolderId ``$filter``;
      * when ``tag`` is a Gmail system label, pops/rewrites it: system-folder
        labels (INBOX/SENT/DRAFT/TRASH/SPAM/JUNK) become folder scope, ``UNREAD``
        sets the unread flag, and STARRED/IMPORTANT are carried as free-text
        ``$search`` with a :class:`QueryTranslationWarning` (no folder equivalent).
        NON-system labels are left untouched so they compile to a category filter.

    Returns the well-known Graph folder name, or ``None`` for no scope.  If an
    explicit ``in:``/``folder`` scope and a system-label-derived folder conflict,
    the first (the explicit ``folder``) is kept and a warning is emitted.
    """
    folder: str | None = None
    folder_specified = False

    raw_folder = model.pop("folder", None)
    if raw_folder is not None:
        key = str(raw_folder).strip().lower()
        if key in _M365_NO_SCOPE:
            folder_specified = True  # explicit whole-mailbox (in:anywhere)
        elif key in _M365_IN_FOLDER_MAP:
            folder = _M365_IN_FOLDER_MAP[key]
            folder_specified = True
        else:
            # Unknown folder name -> carry the value as best-effort free text + warn.
            _warn_untranslatable(f"in:{raw_folder}")
            _append_text(model, str(raw_folder))

    tag = model.get("tag")
    if tag is not None:
        key = str(tag).strip().lower()
        if key in _M365_SYSTEM_LABEL_FOLDER:
            model.pop("tag")
            mapped = _M365_SYSTEM_LABEL_FOLDER[key]
            if not folder_specified:
                folder = mapped
                folder_specified = True
            elif folder != mapped:
                warnings.warn(
                    f"query_compiler: folder scope conflict — keeping {folder!r} "
                    f"and ignoring system label {tag!r} (-> {mapped!r})",
                    QueryTranslationWarning,
                    stacklevel=2,
                )
        elif key == "unread":
            # Gmail's system label UNREAD means is:unread.
            model.pop("tag")
            model["unread"] = True
        elif key in ("starred", "important"):
            # No Outlook folder/flag equivalent -> best-effort free text + warn.
            model.pop("tag")
            _warn_untranslatable(f"label:{tag}")
            _append_text(model, str(tag))
        # else: non-system label -> leave as `tag` for a category filter.

    return folder


def _m365_search_tokens(model: dict[str, Any]) -> list[str]:
    """KQL tokens for the ``$search``-eligible neutral fields."""
    tokens: list[str] = []
    if model.get("from_any"):
        tokens.append(_kql_or_group("from", list(model["from_any"])))
    if model.get("domain"):
        tokens.append(_kql_term("from", model["domain"]))
    if model.get("to_recipient"):
        tokens.append(_kql_term("to", model["to_recipient"]))
    if model.get("subject_contains"):
        tokens.append(_kql_term("subject", model["subject_contains"]))
    if model.get("subject_any"):
        tokens.append(_kql_or_group("subject", list(model["subject_any"])))
    if model.get("filename"):
        tokens.append(_kql_term("attachment", model["filename"]))
    if model.get("any_terms"):
        tokens.append(_kql_or_group("", list(model["any_terms"])))
    if model.get("text"):
        tokens.append(_kql_text(str(model["text"])))
    for neg in model.get("negations", []):
        tokens.append(f"NOT {neg}")
    return tokens


def _m365_filter_parts(model: dict[str, Any], now: datetime) -> list[str]:
    """OData ``$filter`` clauses for the filter-eligible neutral fields."""
    parts: list[str] = []
    if model.get("unread"):
        parts.append("isRead eq false")
    if model.get("read"):
        parts.append("isRead eq true")
    if model.get("from_sender"):
        parts.append(
            f"from/emailAddress/address eq '{_odata_escape(model['from_sender'])}'"
        )
    # NB: folder scope is resolved out-of-band by _resolve_m365_folder and popped
    # before this runs — it is NEVER emitted as a parentFolderId $filter (that id
    # is opaque, not the well-known folder name).
    if model.get("newer_than_days") is not None:
        threshold = now - timedelta(days=int(model["newer_than_days"]))
        parts.append(f"receivedDateTime ge {_rfc3339(threshold)}")
    if model.get("older_than_days") is not None:
        threshold = now - timedelta(days=int(model["older_than_days"]))
        parts.append(f"receivedDateTime le {_rfc3339(threshold)}")
    if model.get("has_attachment"):
        parts.append("hasAttachments eq true")
    if model.get("tag"):
        parts.append(f"categories/any(c:c eq '{_odata_escape(model['tag'])}')")
    return parts


def _m365_fold_to_kql(model: dict[str, Any], now: datetime) -> str:
    """Fold the entire model into a single Graph ``$search`` KQL string.

    Used when both a ``$filter`` and a ``$search`` would otherwise be produced,
    which Graph forbids on ``/messages``.  Folder scope is NOT handled here: it is
    resolved out-of-band by :func:`_resolve_m365_folder` (and popped from ``model``)
    before folding, so it survives the fold unconditionally via the URL path.
    """
    tokens: list[str] = []
    if model.get("unread"):
        tokens.append("isread:false")
    if model.get("read"):
        tokens.append("isread:true")
    if model.get("from_sender"):
        tokens.append(_kql_term("from", model["from_sender"]))
    if model.get("from_any"):
        tokens.append(_kql_or_group("from", list(model["from_any"])))
    if model.get("domain"):
        tokens.append(_kql_term("from", model["domain"]))
    if model.get("to_recipient"):
        tokens.append(_kql_term("to", model["to_recipient"]))
    if model.get("newer_than_days") is not None:
        threshold = now - timedelta(days=int(model["newer_than_days"]))
        tokens.append(f"received>={_date_only(threshold)}")
    if model.get("older_than_days") is not None:
        threshold = now - timedelta(days=int(model["older_than_days"]))
        tokens.append(f"received<={_date_only(threshold)}")
    if model.get("has_attachment"):
        tokens.append("hasattachments:true")
    if model.get("subject_contains"):
        tokens.append(_kql_term("subject", model["subject_contains"]))
    if model.get("subject_any"):
        tokens.append(_kql_or_group("subject", list(model["subject_any"])))
    if model.get("filename"):
        tokens.append(_kql_term("attachment", model["filename"]))
    if model.get("tag"):
        tokens.append(_kql_term("category", model["tag"]))
    if model.get("any_terms"):
        tokens.append(_kql_or_group("", list(model["any_terms"])))
    if model.get("text"):
        tokens.append(_kql_text(str(model["text"])))
    for neg in model.get("negations", []):
        tokens.append(f"NOT {neg}")
    return " ".join(tokens)


def _compile_m365(
    model: dict[str, Any],
    now: datetime,
    *,
    fold_filter_search: bool = True,
) -> dict[str, str | None]:
    raw = model.get("raw")
    if isinstance(raw, dict) and "m365" in raw:
        m = raw["m365"]
        if isinstance(m, dict):
            return {"folder": m.get("folder"),
                    "filter": m.get("filter"), "search": m.get("search")}
        # A bare string raw override is treated as a $search KQL string.
        return {"folder": None, "filter": None, "search": str(m)}

    # Work on a private copy: _resolve_m365_folder pops/rewrites folder + tag.
    model = dict(model)
    folder = _resolve_m365_folder(model)

    filter_parts = _m365_filter_parts(model, now)
    search_parts = _m365_search_tokens(model)

    # Graph forbids combining $filter + $search on /messages -> fold to KQL.
    # Folder scope is out-of-band, so it is preserved across the fold.
    # Callers that cannot issue $search (e.g. Composio OUTLOOK_QUERY_EMAILS)
    # pass fold_filter_search=False so both components remain visible and the
    # filter-only subset can be preferred.
    if fold_filter_search and filter_parts and search_parts:
        return {"folder": folder, "filter": None,
                "search": _m365_fold_to_kql(model, now)}

    return {
        "folder": folder,
        "filter": " and ".join(filter_parts) if filter_parts else None,
        "search": " ".join(search_parts) if search_parts else None,
    }


# ── Operational instrumentation (v0.3.4) ───────────────────────────────

def _log_query_compiled(dialect: str, result: Any) -> None:
    """Emit a ``query_compiled`` runtime event describing the compiled query
    SHAPE only — never the query/filter/search text (which can contain client
    names). runtime_log is imported lazily and guarded so this pure module stays
    dependency-light; any failure (including a missing runtime_log) is silent.
    """
    try:
        from runtime_log import log_event
    except ImportError:
        return
    except Exception:  # pragma: no cover - defensive
        return
    if isinstance(result, dict):
        folder = result.get("folder")
        has_filter = bool(result.get("filter"))
        has_search = bool(result.get("search"))
    else:
        folder = None
        has_filter = False
        has_search = bool(result)
    try:
        log_event(
            "query_compiled", level="debug", component="query_compiler",
            dialect=str(dialect), has_filter=has_filter, has_search=has_search,
            folder=folder,
        )
    except Exception:  # pragma: no cover - logging must never break the caller
        pass


# ── Public API ─────────────────────────────────────────────────────────

def compile_query(
    model: str | dict[str, Any],
    dialect: str,
    now: datetime | None = None,
    *,
    fold_filter_search: bool = True,
) -> Any:
    """Compile a neutral query model into a provider dialect.

    Args:
        model: a Gmail-syntax string or a neutral query dict.
        dialect: ``"gmail"`` or ``"m365"``.
        now: reference time for relative-date math (defaults to UTC now).
        fold_filter_search: when True (default), an m365 compile that would
            produce both ``$filter`` and ``$search`` folds into a single KQL
            ``$search`` string (Graph forbids combining them on ``/messages``).
            Pass False to keep both components so a caller that cannot issue
            ``$search`` can prefer the filter-only subset.

    Returns:
        ``str`` for the gmail dialect; ``{"folder": .., "filter": .., "search": ..}``
        for m365 (folder scope carried out-of-band; see the module docstring).

    Raises:
        ValueError: for an unknown dialect, or when an m365 translation of a
            non-empty string would be empty (see the no-silent-emptiness policy).
        TypeError: when ``model`` is neither ``str`` nor ``dict``.
    """
    now = now or datetime.now(timezone.utc)

    def _emit(res: Any) -> Any:
        # Log the compiled shape (no text) on every successful compile.
        _log_query_compiled(dialect, res)
        return res

    if dialect == "gmail":
        if isinstance(model, str):
            return _emit(model)
        if isinstance(model, dict):
            return _emit(_compile_gmail(model))
        raise TypeError(f"query model must be str or dict, got {type(model).__name__}")

    if dialect == "m365":
        if isinstance(model, str):
            if not model.strip():
                # Empty input -> empty result (allowed; not a translation loss).
                return _emit({"filter": None, "search": None})
            parsed = parse_gmail_query(model)
            result = _compile_m365(
                parsed, now, fold_filter_search=fold_filter_search,
            )
            if (result.get("folder") is None
                    and result.get("filter") is None
                    and result.get("search") is None):
                raise ValueError(
                    f"could not translate query to m365 (empty result): {model!r}"
                )
            return _emit(result)
        if isinstance(model, dict):
            return _emit(_compile_m365(
                model, now, fold_filter_search=fold_filter_search,
            ))
        raise TypeError(f"query model must be str or dict, got {type(model).__name__}")

    raise ValueError(f"unknown dialect: {dialect!r} (expected 'gmail' or 'm365')")
