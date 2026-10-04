#!/usr/bin/env python3
"""Create a set of Jira Cloud issues (epics > stories > sub-tasks) from a YAML template.

Examples
  # Offline structure check of every template (what CI runs on pull requests)
  python create_tickets.py --validate --template templates/*.yaml

  # Preview against Jira: read-only calls, nothing is created
  python create_tickets.py --template new-service --project PAY \\
      --var service_name="Payments API" --var start_date=2026-11-02 --dry-run

  # Create for real. Re-running is safe: tickets created earlier are found and skipped.
  python create_tickets.py --template new-service --project PAY \\
      --vars "service_name=Payments API; start_date=2026-11-02"

Credentials come from the environment: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import requests
import yaml

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "templates"
DEFAULT_CONFIG = ROOT / "config.yaml"

BULK_CREATE_MAX = 50   # POST /issue/bulk rejects batches larger than this
BULK_FETCH_MAX = 100   # POST /issue/bulkfetch limit
SEARCH_PAGE = 100

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")
REL_DUE_RE = re.compile(r"^([+-])?(\d+)(bd|d|w)$")

LEVELS = ("epic", "story", "subtask")
CHILD_KEY = {"epic": "stories", "story": "subtasks", "subtask": None}
ITEM_KEYS = {"id", "summary", "description", "type", "labels", "components", "priority",
             "points", "assignee", "due", "fields", "depends_on", "stories", "subtasks"}
DEFAULT_KEYS = {"labels", "components", "priority", "assignee", "fields"}

CONFIG_DEFAULTS: dict[str, Any] = {
    "issue_types": {"epic": "Epic", "story": "Story", "subtask": "Subtask"},
    "story_points_field": "Story point estimate",
    "link_type": "Blocks",
    "label_prefix": "tpl",
}


class TemplateError(Exception):
    """Problem in a template, config or argument. Raised before anything is written to Jira."""


class JiraError(Exception):
    """Jira rejected a request or could not be reached."""


def log(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", file=sys.stderr, flush=True)


def chunks(seq: list, size: int) -> Iterator[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


# --------------------------------------------------------------------------- templates

def load_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        raise TemplateError(f"File not found: {path}") from None
    except yaml.YAMLError as exc:
        raise TemplateError(
            f"Invalid YAML in {path}:\n{exc}\n"
            'Hint: quote any value that starts with "{{", e.g.  summary: "{{service_name}} setup"'
        ) from None


def resolve_template_path(ref: str) -> Path:
    path = Path(ref)
    if path.suffix in (".yaml", ".yml"):
        if path.is_file():
            return path.resolve()
        raise TemplateError(f"Template file not found: {ref}")
    if not ID_RE.match(ref):
        raise TemplateError(f"Invalid template name '{ref}'")
    for suffix in (".yaml", ".yml"):
        candidate = TEMPLATES_DIR / f"{ref}{suffix}"
        if candidate.is_file():
            return candidate
    available = ", ".join(sorted(p.stem for p in TEMPLATES_DIR.glob("*.y*ml"))) or "none"
    raise TemplateError(f"No template named '{ref}' in {TEMPLATES_DIR} (available: {available})")


def load_template(path: Path) -> dict:
    """Load a template and inline its `- include: <file>` entries."""
    tpl = load_yaml(path)
    if not isinstance(tpl, dict):
        raise TemplateError(f"{path}: the top level must be a mapping")
    tpl.setdefault("name", path.stem)
    tpl["variables"] = dict(tpl.get("variables") or {})
    for section in ("epics", "stories"):
        tpl[section] = _expand_includes(tpl.get(section) or [], section, path, tpl["variables"], (path,))
    if not tpl["epics"] and not tpl["stories"]:
        raise TemplateError(f"{path}: the template defines no epics or stories")
    defaults = tpl.get("defaults") or {}
    if not isinstance(defaults, dict) or set(defaults) - DEFAULT_KEYS:
        raise TemplateError(f"{path}: 'defaults' may only contain {sorted(DEFAULT_KEYS)}")
    tpl["defaults"] = defaults
    return tpl


def _expand_includes(entries: Any, section: str, src: Path, variables: dict, stack: tuple) -> list:
    if not isinstance(entries, list):
        raise TemplateError(f"{src}: '{section}' must be a list")
    out: list = []
    for entry in entries:
        if isinstance(entry, dict) and set(entry) == {"include"}:
            inc = (src.parent / str(entry["include"])).resolve()
            if inc in stack:
                raise TemplateError("Include cycle: " + " -> ".join(p.name for p in (*stack, inc)))
            fragment = load_yaml(inc)
            if not isinstance(fragment, dict):
                raise TemplateError(f"{inc}: a fragment must be a mapping containing '{section}:'")
            for name, spec in (fragment.get("variables") or {}).items():
                variables.setdefault(name, spec)  # the including template's declaration wins
            out.extend(_expand_includes(fragment.get(section) or [], section, inc, variables, (*stack, inc)))
        else:
            out.append(entry)
    return out


def parse_var_args(pairs: Iterable[str] | None, blob: str | None) -> dict[str, str]:
    raw = list(pairs or [])
    if blob:
        raw += [p for p in re.split(r"[;\n]", blob) if p.strip()]
    values: dict[str, str] = {}
    for pair in raw:
        if "=" not in pair:
            raise TemplateError(f"Variables must look like KEY=VALUE, got '{pair.strip()}'")
        key, value = pair.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def resolve_variables(tpl: dict, provided: dict[str, str], project_key: str,
                      placeholders: bool = False) -> dict[str, str]:
    builtins = {"project_key": project_key, "today": dt.date.today().isoformat()}
    declared = tpl["variables"]
    for name in provided:
        if name not in declared:
            warn(f"Variable '{name}' is not declared in template '{tpl['name']}' and is ignored")
    values, missing = dict(builtins), []
    for name, spec in declared.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise TemplateError(f"variables.{name} must be a mapping")
        if name in builtins:
            raise TemplateError(f"'{name}' is a built-in variable and cannot be redeclared")
        if provided.get(name, "") != "":
            values[name] = provided[name]
        elif spec.get("default") is not None:
            values[name] = VAR_RE.sub(lambda m: builtins.get(m.group(1), m.group(0)), str(spec["default"]))
        elif spec.get("required"):
            if placeholders:
                values[name] = f"<{name}>"
            else:
                missing.append(f"{name}: {spec.get('description', 'no description')}")
        else:
            values[name] = ""
    if missing:
        raise TemplateError("Missing required variables:\n  " + "\n  ".join(missing))
    return values


def substitute(node: Any, values: dict[str, str], where: str) -> Any:
    if isinstance(node, str):
        def repl(m: re.Match) -> str:
            if m.group(1) not in values:
                raise TemplateError(f"Unknown variable '{m.group(0)}' at {where}")
            return values[m.group(1)]
        return VAR_RE.sub(repl, node)
    if isinstance(node, list):
        return [substitute(v, values, f"{where}[{i}]") for i, v in enumerate(node)]
    if isinstance(node, dict):
        return {k: substitute(v, values, f"{where}.{k}") for k, v in node.items()}
    return node


def substitute_sections(tpl: dict, values: dict[str, str]) -> dict:
    out = dict(tpl)
    for section in ("epics", "stories", "defaults"):
        out[section] = substitute(tpl[section], values, section)
    return out


def parse_due(value: Any) -> tuple:
    if isinstance(value, dt.datetime):
        value = value.date()
    if isinstance(value, dt.date):  # unquoted YAML date
        return ("abs", value)
    text = str(value).strip()
    m = REL_DUE_RE.match(text)
    if m:
        n = int(m.group(2)) * (7 if m.group(3) == "w" else 1)
        return ("rel", -n if m.group(1) == "-" else n, m.group(3) == "bd")
    try:
        return ("abs", dt.date.fromisoformat(text))
    except ValueError:
        raise TemplateError(f"Invalid due '{text}': use YYYY-MM-DD or an offset from "
                            "start_date such as +10d, +2w or +5bd (business days)") from None


def resolve_due(value: Any, start: dt.date | None) -> str:
    kind, *rest = parse_due(value)
    if kind == "abs":
        return rest[0].isoformat()
    days, business = rest
    if start is None:
        raise TemplateError(f"Relative due '{value}' needs a 'start_date' variable (YYYY-MM-DD)")
    if not business:
        return (start + dt.timedelta(days=days)).isoformat()
    step, left, day = (1 if days >= 0 else -1), abs(days), start
    while left:
        day += dt.timedelta(days=step)
        if day.weekday() < 5:
            left -= 1
    return day.isoformat()


@dataclass
class Item:
    id: str
    level: str  # "epic" | "story" | "subtask"
    summary: str
    spec: dict
    parent: str | None = None
    depends_on: list[str] = field(default_factory=list)


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40].rstrip("-") or "item"


def build_items(tpl: dict) -> list[Item]:
    """Flatten the template into items in tree order, validating as we go."""
    items: dict[str, Item] = {}

    def add(raw: Any, level: str, parent: str | None, where: str) -> None:
        if isinstance(raw, str) and level == "subtask":
            raw = {"summary": raw}
        if not isinstance(raw, dict):
            raise TemplateError(f"{where}: expected a mapping")
        unknown = set(raw) - ITEM_KEYS
        if unknown:
            raise TemplateError(f"{where}: unknown key(s) {sorted(unknown)}")
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            raise TemplateError(f"{where}: 'summary' is required")
        if len(summary) > 255:
            raise TemplateError(f"{where}: summary is longer than 255 characters")
        item_id = raw.get("id")
        if item_id is None:
            if level != "subtask":
                raise TemplateError(f"{where}: 'id' is required for {level}s (it keeps re-runs idempotent)")
            item_id = f"{parent}.{slugify(summary)}"
        item_id = str(item_id)
        where = f"{level} '{item_id}'"
        if not ID_RE.match(item_id):
            raise TemplateError(f"{where}: ids may only contain letters, digits, '.', '_' and '-'")
        if item_id in items:
            raise TemplateError(f"{where}: duplicate id")
        for key in ("labels", "components", "depends_on"):
            if key in raw and not isinstance(raw[key], list):
                raise TemplateError(f"{where}: '{key}' must be a list")
        if "fields" in raw and not isinstance(raw["fields"], dict):
            raise TemplateError(f"{where}: 'fields' must be a mapping")
        for label in raw.get("labels") or []:
            if re.search(r"\s", str(label)):
                raise TemplateError(f"{where}: label '{label}' contains whitespace")
        points = raw.get("points")
        if points not in (None, "") and "<" not in str(points):
            try:
                float(points)
            except (TypeError, ValueError):
                raise TemplateError(f"{where}: points must be a number, got '{points}'") from None
        if raw.get("due") not in (None, "") and "<" not in str(raw["due"]):
            parse_due(raw["due"])
        for key in ("stories", "subtasks"):
            if key in raw and key != CHILD_KEY[level]:
                raise TemplateError(f"{where}: a {level} cannot contain '{key}'")

        items[item_id] = Item(item_id, level, summary, raw, parent,
                              [str(d) for d in raw.get("depends_on") or []])
        child_key = CHILD_KEY[level]
        if child_key:
            children = raw.get(child_key) or []
            if not isinstance(children, list):
                raise TemplateError(f"{where}: '{child_key}' must be a list")
            child_level = "story" if child_key == "stories" else "subtask"
            for i, child in enumerate(children):
                add(child, child_level, item_id, f"{where} > {child_key}[{i}]")

    for i, epic in enumerate(tpl["epics"]):
        add(epic, "epic", None, f"epics[{i}]")
    for i, story in enumerate(tpl["stories"]):
        add(story, "story", None, f"stories[{i}]")

    for item in items.values():
        for dep in item.depends_on:
            if dep == item.id:
                raise TemplateError(f"'{item.id}' depends on itself")
            if dep not in items:
                raise TemplateError(f"'{item.id}' depends on unknown id '{dep}'")
    return list(items.values())


def load_config(path: Path) -> dict:
    cfg = {k: dict(v) if isinstance(v, dict) else v for k, v in CONFIG_DEFAULTS.items()}
    if path.is_file():
        data = load_yaml(path) or {}
        if not isinstance(data, dict):
            raise TemplateError(f"{path}: must be a mapping")
        unknown = set(data) - set(CONFIG_DEFAULTS)
        if unknown:
            raise TemplateError(f"{path}: unknown key(s) {sorted(unknown)}")
        for key, value in data.items():
            if key == "issue_types":
                if not isinstance(value, dict) or set(value) - set(LEVELS):
                    raise TemplateError(f"{path}: issue_types may only set {list(LEVELS)}")
                cfg[key].update(value)
            else:
                cfg[key] = value
    if not ID_RE.match(str(cfg["label_prefix"])):
        raise TemplateError("label_prefix may only contain letters, digits, '.', '_' and '-'")
    return cfg


# --------------------------------------------------------------------------- markdown -> ADF

INLINE_RE = re.compile(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?://[^)\s]+)\)")
BULLET_RE = re.compile(r"^[-*]\s+")
ORDERED_RE = re.compile(r"^\d+[.)]\s+")


def _inline(text: str) -> list[dict]:
    nodes, pos = [], 0
    for m in INLINE_RE.finditer(text):
        if m.start() > pos:
            nodes.append({"type": "text", "text": text[pos:m.start()]})
        if m.group(1) is not None:
            nodes.append({"type": "text", "text": m.group(1), "marks": [{"type": "strong"}]})
        elif m.group(2) is not None:
            nodes.append({"type": "text", "text": m.group(2), "marks": [{"type": "code"}]})
        else:
            nodes.append({"type": "text", "text": m.group(3),
                          "marks": [{"type": "link", "attrs": {"href": m.group(4)}}]})
        pos = m.end()
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})
    return [n for n in nodes if n["text"]]


def markdown_to_adf(md: str) -> dict:
    """Small Markdown subset -> Atlassian Document Format (required by REST API v3).
    Supports headings, paragraphs, bullet/numbered lists, [ ] checkboxes, ``` code
    blocks, **bold**, `code` and [links](https://...)."""
    lines = md.strip("\n").splitlines()
    content: list[dict] = []
    para: list[str] = []

    def flush() -> None:
        if para:
            content.append({"type": "paragraph", "content": _inline(" ".join(para))})
            para.clear()

    i = 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            flush()
            i += 1
        elif s.startswith("```"):
            flush()
            lang, code = s[3:].strip(), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            node: dict = {"type": "codeBlock"}
            if code:
                node["content"] = [{"type": "text", "text": "\n".join(code)}]
            if lang:
                node["attrs"] = {"language": lang}
            content.append(node)
        elif m := re.match(r"^(#{1,6})\s+(.+)$", s):
            flush()
            content.append({"type": "heading", "attrs": {"level": len(m.group(1))},
                            "content": _inline(m.group(2))})
            i += 1
        elif BULLET_RE.match(s) or ORDERED_RE.match(s):
            flush()
            pattern = ORDERED_RE if ORDERED_RE.match(s) else BULLET_RE
            list_items = []
            while i < len(lines) and pattern.match(lines[i].strip()):
                text = pattern.sub("", lines[i].strip(), count=1)
                text = re.sub(r"^\[([ xX])\]\s+", lambda c: "☐ " if c.group(1) == " " else "☑ ", text)
                list_items.append({"type": "listItem",
                                   "content": [{"type": "paragraph", "content": _inline(text)}]})
                i += 1
            content.append({"type": "orderedList" if pattern is ORDERED_RE else "bulletList",
                            "content": list_items})
        else:
            para.append(s)
            i += 1
    flush()
    return {"type": "doc", "version": 1, "content": content}


# --------------------------------------------------------------------------- Jira client

def format_jira_errors(data: Any) -> str:
    if not isinstance(data, dict):
        return str(data)
    parts = [str(m) for m in data.get("errorMessages") or []]
    if isinstance(data.get("errors"), dict):
        parts += [f"{k}: {v}" for k, v in data["errors"].items()]
    return "; ".join(parts)


class Jira:
    def __init__(self, base_url: str, email: str, token: str, max_retries: int = 6):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update({"Accept": "application/json",
                                     "Content-Type": "application/json",
                                     "User-Agent": "jira-ticket-templates/1.0"})
        self.max_retries = max_retries

    def call(self, method: str, path: str, body: Any = None, *,
             safe_to_retry: bool | None = None, accept: tuple[int, ...] = ()) -> Any:
        """Retry throttled calls (429) always; retry 5xx/network failures only when
        repeating the call cannot create duplicates."""
        if safe_to_retry is None:
            safe_to_retry = method in ("GET", "DELETE")
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            try:
                resp = self.session.request(method, self.base + path, json=body, timeout=60)
            except requests.RequestException as exc:
                if safe_to_retry and not last:
                    self._backoff(attempt)
                    continue
                raise JiraError(f"{method} {path}: {exc}") from None
            transient = resp.status_code == 429 or (resp.status_code >= 500 and safe_to_retry)
            if transient and not last:
                self._backoff(attempt, resp.headers.get("Retry-After"))
                continue
            if resp.ok or resp.status_code in accept:
                try:
                    return resp.json() if resp.content else None
                except ValueError:
                    return None
            try:
                detail = format_jira_errors(resp.json())
            except ValueError:
                detail = resp.text[:300]
            raise JiraError(f"{method} {path} -> HTTP {resp.status_code}: {detail or resp.reason}")
        raise AssertionError("unreachable")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> None:
        try:
            delay = float(retry_after) if retry_after else 0.0
        except ValueError:
            delay = 0.0
        delay = max(delay, min(60.0, 2.0 ** attempt)) + random.uniform(0, 1)
        log(f"  throttled or transient error; retrying in {delay:.1f}s")
        time.sleep(delay)

    def myself(self) -> dict:
        return self.call("GET", "/rest/api/3/myself")

    def project(self, key: str) -> dict:
        return self.call("GET", f"/rest/api/3/project/{key}")

    def fields(self) -> list[dict]:
        return self.call("GET", "/rest/api/3/field")

    def search(self, jql: str, fields: list[str]) -> list[dict]:
        issues: dict[str, dict] = {}
        token, seen = None, set()
        while True:
            body: dict = {"jql": jql, "fields": fields, "maxResults": SEARCH_PAGE}
            if token:
                body["nextPageToken"] = token
            page = self.call("POST", "/rest/api/3/search/jql", body, safe_to_retry=True) or {}
            for issue in page.get("issues") or []:
                issues[issue["key"]] = issue
            token = page.get("nextPageToken")
            if not token or page.get("isLast") or token in seen:  # guard against looping tokens
                return list(issues.values())
            seen.add(token)

    def bulk_create(self, updates: list[dict]) -> tuple[list[dict], list[dict]]:
        data = self.call("POST", "/rest/api/3/issue/bulk", {"issueUpdates": updates}, accept=(400,)) or {}
        errors = data.get("errors") if isinstance(data.get("errors"), list) else []
        issues = data.get("issues") or []
        if not issues and not errors:
            raise JiraError(f"Bulk create rejected: {format_jira_errors(data) or data}")
        return issues, errors

    def bulk_fetch(self, keys: list[str], fields: list[str]) -> list[dict]:
        out: list[dict] = []
        for batch in chunks(keys, BULK_FETCH_MAX):
            data = self.call("POST", "/rest/api/3/issue/bulkfetch",
                             {"issueIdsOrKeys": batch, "fields": fields}, safe_to_retry=True) or {}
            out.extend(data.get("issues") or [])
        return out

    def issue_links(self, key: str) -> list[dict]:
        data = self.call("GET", f"/rest/api/3/issue/{key}?fields=issuelinks") or {}
        return (data.get("fields") or {}).get("issuelinks") or []

    def link(self, link_type: str, inward: str, outward: str) -> None:
        self.call("POST", "/rest/api/3/issueLink", {"type": {"name": link_type},
                                                    "inwardIssue": {"key": inward},
                                                    "outwardIssue": {"key": outward}})

    def unlink(self, link_id: str) -> None:
        self.call("DELETE", f"/rest/api/3/issueLink/{link_id}", accept=(404,))


class FieldResolver:
    """Resolves field names (e.g. 'Story point estimate') to ids (e.g. customfield_10016)."""

    def __init__(self, fields: list[dict]):
        self.ids = {f["id"] for f in fields}
        self.by_name: dict[str, list[str]] = {}
        for f in fields:
            self.by_name.setdefault(str(f.get("name", "")).casefold(), []).append(f["id"])

    def resolve(self, ref: str) -> str:
        if ref in self.ids:
            return ref
        matches = self.by_name.get(ref.casefold(), [])
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise JiraError(f"No Jira field named '{ref}'; use the exact field name or its id "
                            "(e.g. customfield_10016)")
        raise JiraError(f"Several Jira fields are named '{ref}' ({', '.join(matches)}); use the id")


def resolve_issue_types(project: dict, wanted: dict[str, str]) -> tuple[dict[str, dict], dict[str, dict]]:
    types = project.get("issueTypes") or []
    by_name = {t["name"].casefold(): t for t in types}
    fallbacks = {
        "epic": lambda t: t.get("hierarchyLevel") == 1,
        "story": lambda t: t.get("hierarchyLevel") == 0 and not t.get("subtask")
                           and t["name"].casefold() in ("story", "task"),
        "subtask": lambda t: bool(t.get("subtask")),
    }
    chosen: dict[str, dict] = {}
    for level in LEVELS:
        t = by_name.get(str(wanted[level]).casefold()) or next((t for t in types if fallbacks[level](t)), None)
        if t is None:
            names = ", ".join(sorted(x["name"] for x in types))
            raise JiraError(f"Project {project.get('key')} has no issue type for {level}s "
                            f"(looked for '{wanted[level]}'; available: {names}). "
                            f"Set issue_types.{level} in config.yaml.")
        chosen[level] = t
    return chosen, by_name


# --------------------------------------------------------------------------- run

@dataclass
class Run:
    jira: Jira
    cfg: dict
    project_key: str
    run_label: str
    item_prefix: str
    types: dict[str, dict]
    types_by_name: dict[str, dict]
    resolver: FieldResolver
    defaults: dict
    start_date: dt.date | None
    keys: dict[str, str] = field(default_factory=dict)                # item id -> issue key
    status: dict[str, tuple[str, str]] = field(default_factory=dict)  # item id -> (status, detail)
    links: list[tuple[str, str, str]] = field(default_factory=list)   # (label, status, detail)
    _points_field: str | None = None

    @property
    def points_field(self) -> str:
        if self._points_field is None:
            self._points_field = self.resolver.resolve(str(self.cfg["story_points_field"]))
        return self._points_field

    def issue_type(self, item: Item) -> dict:
        name = item.spec.get("type")
        if not name:
            return self.types[item.level]
        t = self.types_by_name.get(str(name).casefold())
        if t is None:
            raise JiraError(f"project has no issue type '{name}'")
        if bool(t.get("subtask")) != (item.level == "subtask"):
            raise JiraError(f"issue type '{name}' cannot be used for a {item.level}")
        return t

    def fields_for(self, item: Item, parent_key: str | None) -> dict:
        spec = {**self.defaults, **item.spec}
        labels = [*(self.defaults.get("labels") or []), *(item.spec.get("labels") or [])]
        f: dict[str, Any] = {
            "project": {"key": self.project_key},
            "issuetype": {"id": self.issue_type(item)["id"]},
            "summary": item.summary,
            "labels": sorted({str(x) for x in labels} | {self.run_label, self.item_prefix + item.id}),
        }
        if parent_key:
            f["parent"] = {"key": parent_key}
        if spec.get("description"):
            f["description"] = markdown_to_adf(str(spec["description"]))
        if spec.get("components"):
            f["components"] = [{"name": str(c)} for c in spec["components"]]
        if spec.get("priority"):
            f["priority"] = {"name": str(spec["priority"])}
        if spec.get("assignee"):
            f["assignee"] = {"accountId": str(spec["assignee"])}
        if spec.get("due") not in (None, ""):
            f["duedate"] = resolve_due(spec["due"], self.start_date)
        if spec.get("points") not in (None, ""):
            points = float(spec["points"])
            f[self.points_field] = int(points) if points.is_integer() else points
        for ref, value in (spec.get("fields") or {}).items():
            f[self.resolver.resolve(str(ref))] = value
        return f


def find_existing(run: Run) -> dict[str, dict]:
    jql = f'project = "{run.project_key}" AND labels = "{run.run_label}"'
    found: dict[str, dict] = {}
    for issue in run.jira.search(jql, ["labels", "issuelinks"]):
        for label in issue["fields"].get("labels") or []:
            if label.startswith(run.item_prefix):
                item_id = label[len(run.item_prefix):]
                if item_id in found:
                    warn(f"'{item_id}' is on {found[item_id]['key']} and {issue['key']}; using the first")
                    continue
                found[item_id] = issue
    return found


def preflight(run: Run, items: list[Item], verbose: bool) -> None:
    """Build every payload before the first write, so config and field errors fail fast."""
    problems: list[str] = []
    for item in items:
        parent_key = run.keys.get(item.parent or "") or (f"<new:{item.parent}>" if item.parent else None)
        try:
            payload = run.fields_for(item, parent_key)
            if verbose:
                log(f"--- {item.id}\n{json.dumps(payload, indent=2, ensure_ascii=False)}")
        except (TemplateError, JiraError) as exc:
            problems.append(f"{item.id}: {exc}")
    if problems:
        raise TemplateError("The template does not fit this Jira project:\n  " + "\n  ".join(problems))


def map_keys_by_label(run: Run, keys: list[str]) -> dict[str, str]:
    """Map created issue keys back to item ids via their labels (not response order)."""
    mapped: dict[str, str] = {}
    for issue in run.jira.bulk_fetch(keys, ["labels"]) if keys else []:
        for label in issue["fields"].get("labels") or []:
            if label.startswith(run.item_prefix):
                mapped[label[len(run.item_prefix):]] = issue["key"]
    return mapped


def create_issues(run: Run, items: list[Item]) -> None:
    for level in LEVELS:  # parents before children
        todo: list[Item] = []
        for item in items:
            if item.level != level or item.id in run.keys:
                continue
            if item.parent and item.parent not in run.keys:
                run.status[item.id] = ("skipped", f"parent '{item.parent}' was not created")
            else:
                todo.append(item)
        for batch in chunks(todo, BULK_CREATE_MAX):
            log(f"Creating {len(batch)} {level}(s)...")
            updates = [{"fields": run.fields_for(i, run.keys.get(i.parent or ""))} for i in batch]
            created, errors = run.jira.bulk_create(updates)
            for err in errors:
                idx = err.get("failedElementNumber")
                message = format_jira_errors(err.get("elementErrors") or {}) or str(err)
                if isinstance(idx, int) and 0 <= idx < len(batch):
                    run.status[batch[idx].id] = ("failed", message)
                else:
                    warn(f"Unattributed bulk-create error: {message}")
            mapped = map_keys_by_label(run, [c["key"] for c in created])
            for item in batch:
                if item.id in mapped:
                    run.keys[item.id] = mapped[item.id]
                    run.status[item.id] = ("created", "")
                elif item.id not in run.status:
                    run.status[item.id] = ("failed", "not reported as created by Jira")


def existing_link_pairs(issues: Iterable[dict], link_type: str) -> set[tuple[str, str]]:
    """(blocker, blocked) pairs already linked. On issue X, an entry holding
    `outwardIssue: Y` reads 'X <outward label> Y', i.e. X blocks Y."""
    pairs: set[tuple[str, str]] = set()
    for issue in issues:
        for link in issue["fields"].get("issuelinks") or []:
            if str(link.get("type", {}).get("name", "")).casefold() != link_type.casefold():
                continue
            if "outwardIssue" in link:
                pairs.add((issue["key"], link["outwardIssue"]["key"]))
            if "inwardIssue" in link:
                pairs.add((link["inwardIssue"]["key"], issue["key"]))
    return pairs


def verify_direction(run: Run, link_type: str, blocker: str, blocked: str, swap: bool) -> bool:
    """The POST /issueLink docs and observed behaviour disagree on which field is the
    'outward' end, so check the first link created and fix it if it came out reversed.
    Returns the swap setting to use for the rest of the run."""
    for link in run.jira.issue_links(blocker):
        if str(link.get("type", {}).get("name", "")).casefold() != link_type.casefold():
            continue
        if link.get("outwardIssue", {}).get("key") == blocked:
            return swap
        if link.get("inwardIssue", {}).get("key") == blocked:
            log("  Link direction came out reversed; fixing it and adjusting the remaining links.")
            run.jira.unlink(link["id"])
            swap = not swap
            run.jira.link(link_type, *((blocked, blocker) if swap else (blocker, blocked)))
            return swap
    warn(f"Could not verify the link direction on {blocker}; assuming it is correct")
    return swap


def create_links(run: Run, items: list[Item], existing: dict[str, dict]) -> None:
    link_type = str(run.cfg["link_type"])
    have = existing_link_pairs(existing.values(), link_type)
    swap, verified = False, False
    for item in items:
        for dep in item.depends_on:
            label = f"{dep} -> {item.id}"
            blocker, blocked = run.keys.get(dep), run.keys.get(item.id)
            if not blocker or not blocked:
                run.links.append((label, "skipped", "an issue in this link was not created"))
                continue
            if (blocker, blocked) in have:
                run.links.append((label, "exists", f"{blocker} {link_type} {blocked}"))
                continue
            try:
                run.jira.link(link_type, *((blocked, blocker) if swap else (blocker, blocked)))
                if not verified and (blocked, blocker) not in have:
                    swap = verify_direction(run, link_type, blocker, blocked, swap)
                    verified = True
                have.add((blocker, blocked))
                run.links.append((label, "created", f"{blocker} {link_type} {blocked}"))
            except JiraError as exc:
                run.links.append((label, "failed", str(exc)))


def report(run: Run, items: list[Item], base_url: str, dry_run: bool, planned_links: int) -> None:
    rows = []
    for item in items:
        status, detail = run.status.get(item.id, ("to create" if dry_run else "pending", ""))
        rows.append((status, run.keys.get(item.id, ""), item.level, item.id, item.summary, detail))

    log("")
    for status, key, level, item_id, summary, detail in rows:
        indent = "  " * LEVELS.index(level)
        log(f"{status:<10} {key:<11} {indent}{summary}  [{item_id}]" + (f"\n{'':<22}-> {detail}" if detail else ""))
    for label, status, detail in run.links:
        log(f"link {status:<8} {label}  ({detail})")

    counts = Counter(r[0] for r in rows)
    link_counts = Counter(s for _, s, _ in run.links)
    log("\nIssues: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    log("Links:  " + (f"{planned_links} to create" if dry_run else
                      ", ".join(f"{n} {s}" for s, n in sorted(link_counts.items())) or "none"))
    log(f"Find them in Jira with:  labels = {run.run_label}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return

    def esc(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ")

    with open(summary_path, "a", encoding="utf-8") as fh:
        fh.write(f"### Jira tickets for {run.project_key} ({'dry run' if dry_run else 'run'})\n\n")
        fh.write("| Status | Issue | Type | Summary | Detail |\n|---|---|---|---|---|\n")
        for status, key, level, _, summary, detail in rows:
            link = f"[{key}]({base_url}/browse/{key})" if key else ""
            fh.write(f"| {status} | {link} | {level} | {esc(summary)} | {esc(detail)} |\n")
        if run.links:
            fh.write("\n| Link | Status | Detail |\n|---|---|---|\n")
            for label, status, detail in run.links:
                fh.write(f"| {esc(label)} | {status} | {esc(detail)} |\n")
        fh.write(f"\nJQL: `labels = {run.run_label}`\n")


def validate_templates(refs: list[str]) -> int:
    failed = 0
    for ref in refs:
        try:
            path = resolve_template_path(ref)
            tpl = load_template(path)
            values = resolve_variables(tpl, {}, "PROJ", placeholders=True)
            items = build_items(substitute_sections(tpl, values))
            counts = Counter(i.level for i in items)
            links = sum(len(i.depends_on) for i in items)
            log(f"OK    {ref}: {counts['epic']} epics, {counts['story']} stories, "
                f"{counts['subtask']} sub-tasks, {links} dependencies")
        except TemplateError as exc:
            failed += 1
            log(f"FAIL  {ref}: {exc}")
    return 2 if failed else 0


def run_template(args: argparse.Namespace, cfg: dict) -> int:
    project_key = args.project.strip().upper()
    if not PROJECT_KEY_RE.match(project_key):
        raise TemplateError(f"Invalid project key '{args.project}'")
    tpl = load_template(resolve_template_path(args.template[0]))
    values = resolve_variables(tpl, parse_var_args(args.var, args.vars), project_key)
    items = build_items(substitute_sections(tpl, values))
    start = None
    if values.get("start_date"):
        try:
            start = dt.date.fromisoformat(values["start_date"])
        except ValueError:
            raise TemplateError(f"start_date must be YYYY-MM-DD, got '{values['start_date']}'") from None
    run_key = args.run_key or str(tpl["name"])
    if not ID_RE.match(run_key):
        raise TemplateError(f"run key '{run_key}' may only contain letters, digits, '.', '_' and '-'")

    env = {k: os.environ.get(k, "").strip() for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")}
    missing = [k for k, v in env.items() if not v]
    if missing:
        raise TemplateError(f"Set environment variable(s): {', '.join(missing)}")
    base_url = env["JIRA_BASE_URL"].rstrip("/")
    if not base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        raise TemplateError("JIRA_BASE_URL must look like https://your-site.atlassian.net")

    jira = Jira(base_url, env["JIRA_EMAIL"], env["JIRA_API_TOKEN"])
    log(f"Connected to {base_url} as {jira.myself().get('displayName', env['JIRA_EMAIL'])}")
    types, by_name = resolve_issue_types(jira.project(project_key), cfg["issue_types"])
    log("Issue types: " + ", ".join(f"{lvl} = {types[lvl]['name']}" for lvl in LEVELS))

    prefix = cfg["label_prefix"]
    run = Run(jira=jira, cfg=cfg, project_key=project_key,
              run_label=f"{prefix}-{run_key}", item_prefix=f"{prefix}-{run_key}--",
              types=types, types_by_name=by_name, resolver=FieldResolver(jira.fields()),
              defaults=tpl["defaults"], start_date=start)

    existing = find_existing(run)
    ids = {i.id for i in items}
    for item_id, issue in existing.items():
        if item_id in ids:
            run.keys[item_id] = issue["key"]
            run.status[item_id] = ("exists", "")
    stale = sorted(f"{issue['key']} ({iid})" for iid, issue in existing.items() if iid not in ids)
    if stale:
        log("Not in the template any more (left untouched): " + ", ".join(stale))

    preflight(run, items, args.verbose)
    have = existing_link_pairs(existing.values(), str(cfg["link_type"]))
    planned_links = sum(1 for i in items for d in i.depends_on
                        if (run.keys.get(d), run.keys.get(i.id)) not in have)

    if args.dry_run:
        report(run, items, base_url, True, planned_links)
        log("\nDry run: nothing was created.")
        return 0

    create_issues(run, items)
    create_links(run, items, existing)
    report(run, items, base_url, False, planned_links)
    problems = any(s in ("failed", "skipped") for s, _ in run.status.values()) or \
        any(s in ("failed", "skipped") for _, s, _ in run.links)
    if problems:
        log("\nSome items were not created. Fix the cause and re-run; existing tickets are skipped.")
    return 1 if problems else 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Create Jira issues from a YAML template.",
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--template", nargs="+", required=True,
                   help="template name (file in templates/ without .yaml) or a path; several with --validate")
    p.add_argument("--project", help="Jira project key, e.g. PAY")
    p.add_argument("--var", action="append", metavar="KEY=VALUE", help="template variable (repeatable)")
    p.add_argument("--vars", metavar="'K=V; K2=V2'", help="variables as one string, separated by ';' or newlines")
    p.add_argument("--run-key", help="idempotency key (default: template name); set it to apply "
                                     "the same template twice to one project")
    p.add_argument("--config", default=str(DEFAULT_CONFIG), help="path to config.yaml")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--validate", action="store_true", help="offline template check, no Jira calls")
    mode.add_argument("--dry-run", action="store_true", help="read-only Jira calls; show what would happen")
    p.add_argument("--verbose", action="store_true", help="print every issue payload during preflight")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.validate:
            load_config(Path(args.config))
            return validate_templates(args.template)
        if len(args.template) != 1:
            raise TemplateError("Pass exactly one --template when creating tickets")
        if not args.project:
            raise TemplateError("--project is required")
        return run_template(args, load_config(Path(args.config)))
    except TemplateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except JiraError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if not args.dry_run:
            print("Re-running is safe: tickets that already exist are detected and skipped.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
