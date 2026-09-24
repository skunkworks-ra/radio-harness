"""Record every API request a ``claude -p`` session makes, into SQLite.

Purpose: find out exactly what a Claude Code session is given while it reduces
data (system prompt, CLAUDE.md files, skill index, tool schemas, every tool
result, every injected reminder), so the harness can be made to give a small
model the same input.

How: a local forwarding proxy. ``claude -p`` is started with
``ANTHROPIC_BASE_URL`` pointing at it; each request is stored, forwarded
unchanged to the real API, and the response is streamed back unchanged while a
copy is stored. The ``--output-format stream-json`` event stream is stored
alongside.

Storage, one SQLite file:

- ``blobs``     content addressed by sha256, zlib-compressed. Every request
                resends the whole prefix, so system blocks, tool schemas and
                messages are stored once each.
- ``captures``  one row per session.
- ``requests``  one row per HTTP request: the raw body (byte-exact), its parts
                by hash, the reassembled response, usage.
- ``events``    the stream-json lines, in order.

Credentials: request headers are stored only from ``_KEEP_HEADERS``. The
``authorization`` header is forwarded and never written.

Usage::

    python -m analyst_driver.capture run --db capture.db --label NAME \\
        --prompt-file prompt.txt [--cwd DIR] -- <extra claude args>
    python -m analyst_driver.capture report --db capture.db [--capture ID] --out report.md
    python -m analyst_driver.capture show --db capture.db --request ID
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import http.server
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

UPSTREAM = "https://api.anthropic.com"

#: Request headers worth keeping. Everything else, credentials included, is
#: forwarded but not stored.
_KEEP_HEADERS = frozenset(
    {
        "anthropic-beta",
        "anthropic-version",
        "content-type",
        "user-agent",
        "x-app",
        "x-claude-code-session-id",
        "x-stainless-package-version",
        "x-stainless-retry-count",
    }
)

#: Hop-by-hop or re-computed headers, never copied across the proxy.
_DROP_FORWARD = frozenset(
    {"host", "content-length", "connection", "accept-encoding", "transfer-encoding", "keep-alive"}
)
_DROP_BACK = frozenset({"content-length", "transfer-encoding", "connection", "keep-alive"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    sha256  TEXT PRIMARY KEY,
    kind    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    zdata   BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS captures (
    id           INTEGER PRIMARY KEY,
    label        TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    cwd          TEXT NOT NULL,
    argv_json    TEXT NOT NULL,
    env_json     TEXT NOT NULL,
    prompt_sha   TEXT,
    claude_version TEXT,
    files_json   TEXT,
    exit_code    INTEGER,
    stderr_sha   TEXT
);
CREATE TABLE IF NOT EXISTS requests (
    id            INTEGER PRIMARY KEY,
    capture_id    INTEGER NOT NULL REFERENCES captures(id),
    ordinal       INTEGER NOT NULL,
    method        TEXT NOT NULL,
    path          TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        INTEGER,
    headers_json  TEXT NOT NULL,
    body_sha      TEXT,
    params_json   TEXT,
    system_json   TEXT,
    tools_json    TEXT,
    messages_json TEXT,
    model         TEXT,
    raw_response_sha TEXT,
    response_sha  TEXT,
    stop_reason   TEXT,
    usage_json    TEXT,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS events (
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    ordinal     INTEGER NOT NULL,
    at          TEXT NOT NULL,
    type        TEXT,
    subtype     TEXT,
    line        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS requests_capture ON requests(capture_id, ordinal);
CREATE INDEX IF NOT EXISTS events_capture ON events(capture_id, ordinal);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def canonical(obj: Any) -> bytes:
    """Stable bytes for hashing a JSON value."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class CaptureDB:
    """All SQL lives here. Safe to share across the proxy's threads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # -- blobs ---------------------------------------------------------------

    def put(self, data: bytes, kind: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO blobs (sha256, kind, size, zdata) VALUES (?, ?, ?, ?)",
                (sha, kind, len(data), zlib.compress(data, 6)),
            )
            self.conn.commit()
        return sha

    def put_json(self, obj: Any, kind: str) -> str:
        return self.put(canonical(obj), kind)

    def get(self, sha: str) -> bytes:
        row = self.conn.execute("SELECT zdata FROM blobs WHERE sha256 = ?", (sha,)).fetchone()
        if row is None:
            raise KeyError(sha)
        return zlib.decompress(row[0])

    def get_json(self, sha: str) -> Any:
        return json.loads(self.get(sha))

    # -- captures ------------------------------------------------------------

    def new_capture(self, **cols: Any) -> int:
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        with self._lock:
            cur = self.conn.execute(
                f"INSERT INTO captures ({keys}) VALUES ({marks})", tuple(cols.values())
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def update(self, table: str, row_id: int, **cols: Any) -> None:
        sets = ", ".join(f"{k} = ?" for k in cols)
        with self._lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*cols.values(), row_id))
            self.conn.commit()

    # -- requests ------------------------------------------------------------

    def new_request(self, capture_id: int, method: str, path: str, headers: dict) -> int:
        with self._lock:
            (n,) = self.conn.execute(
                "SELECT COUNT(*) FROM requests WHERE capture_id = ?", (capture_id,)
            ).fetchone()
            cur = self.conn.execute(
                "INSERT INTO requests (capture_id, ordinal, method, path, started_at, headers_json)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (capture_id, n + 1, method, path, _now(), json.dumps(headers, sort_keys=True)),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def add_event(self, capture_id: int, ordinal: int, line: str) -> None:
        etype = subtype = None
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                etype, subtype = obj.get("type"), obj.get("subtype")
        except json.JSONDecodeError:
            etype = "unparsed"
        with self._lock:
            self.conn.execute(
                "INSERT INTO events (capture_id, ordinal, at, type, subtype, line)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (capture_id, ordinal, _now(), etype, subtype, line),
            )
            self.conn.commit()


# -- request body -------------------------------------------------------------


def store_body(db: CaptureDB, request_id: int, body: bytes) -> None:
    """Store the raw body, and when it is a Messages request, its parts by hash."""
    cols: dict[str, Any] = {"body_sha": db.put(body, "request_body")}
    try:
        obj = json.loads(body) if body else None
    except (json.JSONDecodeError, UnicodeDecodeError):
        obj = None
    if isinstance(obj, dict):
        system = obj.get("system")
        if isinstance(system, list):
            cols["system_json"] = json.dumps([db.put_json(b, "system_block") for b in system])
        elif isinstance(system, str):
            cols["system_json"] = json.dumps(db.put_json(system, "system_text"))
        if isinstance(obj.get("tools"), list):
            cols["tools_json"] = json.dumps(
                [[t.get("name"), db.put_json(t, "tool")] for t in obj["tools"]]
            )
        if isinstance(obj.get("messages"), list):
            cols["messages_json"] = json.dumps([db.put_json(m, "message") for m in obj["messages"]])
        params = {k: v for k, v in obj.items() if k not in ("system", "tools", "messages")}
        cols["params_json"] = json.dumps(params, sort_keys=True)
        cols["model"] = obj.get("model")
    db.update("requests", request_id, **cols)


def reconstruct(db: CaptureDB, request_id: int) -> dict:
    """Rebuild the request body from its parts. Equal, as JSON, to the raw body."""
    row = db.conn.execute(
        "SELECT params_json, system_json, tools_json, messages_json FROM requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if row is None or row[0] is None:
        raise KeyError(request_id)
    params_json, system_json, tools_json, messages_json = row
    body = json.loads(params_json)
    if system_json is not None:
        ref = json.loads(system_json)
        body["system"] = (
            [db.get_json(s) for s in ref] if isinstance(ref, list) else db.get_json(ref)
        )
    if tools_json is not None:
        body["tools"] = [db.get_json(sha) for _name, sha in json.loads(tools_json)]
    if messages_json is not None:
        body["messages"] = [db.get_json(sha) for sha in json.loads(messages_json)]
    return body


# -- response -----------------------------------------------------------------


def reassemble_sse(raw: bytes) -> dict:
    """Rebuild the final Messages response from a server-sent-event stream.

    Returns the message dict. Unknown event or delta types are kept under
    ``_unknown`` so nothing is dropped silently.
    """
    msg: dict[str, Any] = {}
    blocks: dict[int, dict] = {}
    partial: dict[int, str] = {}
    unknown: list[Any] = []
    errors: list[Any] = []
    for chunk in raw.decode("utf-8", errors="replace").split("\n\n"):
        data_lines = [ln[5:].lstrip() for ln in chunk.split("\n") if ln.startswith("data:")]
        if not data_lines:
            continue
        try:
            ev = json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            unknown.append("\n".join(data_lines))
            continue
        et = ev.get("type")
        if et == "message_start":
            msg = dict(ev.get("message", {}))
        elif et == "content_block_start":
            blocks[ev["index"]] = dict(ev.get("content_block", {}))
        elif et == "content_block_delta":
            i, d = ev["index"], ev.get("delta", {})
            b = blocks.setdefault(i, {})
            dt = d.get("type")
            if dt == "text_delta":
                b["text"] = b.get("text", "") + d.get("text", "")
            elif dt == "input_json_delta":
                partial[i] = partial.get(i, "") + d.get("partial_json", "")
            elif dt == "thinking_delta":
                b["thinking"] = b.get("thinking", "") + d.get("thinking", "")
            elif dt == "signature_delta":
                b["signature"] = b.get("signature", "") + d.get("signature", "")
            elif dt == "citations_delta":
                b.setdefault("citations", []).append(d.get("citation"))
            else:
                unknown.append(ev)
        elif et == "content_block_stop":
            i = ev["index"]
            if i in partial:
                try:
                    blocks[i]["input"] = json.loads(partial[i]) if partial[i] else {}
                except json.JSONDecodeError:
                    blocks[i]["input_raw"] = partial[i]
        elif et == "message_delta":
            msg.update(ev.get("delta", {}))
            if "usage" in ev:
                msg["usage"] = {**msg.get("usage", {}), **ev["usage"]}
        elif et in ("message_stop", "ping"):
            pass
        elif et == "error":
            errors.append(ev.get("error", ev))
        else:
            unknown.append(ev)
    msg["content"] = [blocks[i] for i in sorted(blocks)]
    if errors:
        msg["_errors"] = errors
    if unknown:
        msg["_unknown"] = unknown
    return msg


def store_response(
    db: CaptureDB, request_id: int, status: int, headers: dict, raw: bytes, error: str | None
) -> None:
    cols: dict[str, Any] = {
        "finished_at": _now(),
        "status": status,
        "raw_response_sha": db.put(raw, "response_raw"),
        "error": error,
    }
    body = raw
    if headers.get("content-encoding") == "gzip":
        try:
            body = gzip.decompress(raw)
        except OSError as e:
            cols["error"] = f"gzip: {e}"
    ctype = headers.get("content-type", "")
    msg: Any = None
    try:
        if "text/event-stream" in ctype:
            msg = reassemble_sse(body)
        elif "json" in ctype and body:
            msg = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        cols["error"] = f"response parse: {e}"
    if isinstance(msg, dict):
        cols["response_sha"] = db.put_json(msg, "response")
        cols["stop_reason"] = msg.get("stop_reason")
        if "usage" in msg:
            cols["usage_json"] = json.dumps(msg["usage"], sort_keys=True)
    db.update("requests", request_id, **cols)


# -- proxy --------------------------------------------------------------------


def make_proxy(
    db: CaptureDB, capture_id: int, upstream: str = UPSTREAM, port: int = 0
) -> http.server.ThreadingHTTPServer:
    client = httpx.Client(
        base_url=upstream, timeout=httpx.Timeout(connect=30, read=900, write=120, pool=60)
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"  # body ends at close; no chunked framing

        def _handle(self) -> None:
            n = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(n) if n else b""
            kept = {k.lower(): v for k, v in self.headers.items() if k.lower() in _KEEP_HEADERS}
            rid = db.new_request(capture_id, self.command, self.path, kept)
            if body:
                store_body(db, rid, body)
            fwd = {k: v for k, v in self.headers.items() if k.lower() not in _DROP_FORWARD}
            fwd["accept-encoding"] = "identity"
            raw = bytearray()
            status, back, error = 502, {}, None
            try:
                req = client.build_request(self.command, self.path, headers=fwd, content=body)
                resp = client.send(req, stream=True)
                try:
                    status = resp.status_code
                    back = {k.lower(): v for k, v in resp.headers.items()}
                    self.send_response(status)
                    for k, v in resp.headers.items():
                        if k.lower() not in _DROP_BACK:
                            self.send_header(k, v)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for part in resp.iter_raw():
                        raw.extend(part)
                        self.wfile.write(part)
                        self.wfile.flush()
                finally:
                    resp.close()
            except (httpx.HTTPError, OSError) as e:
                error = f"{type(e).__name__}: {e}"
                if not raw:
                    try:
                        self.send_response(502)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(canonical({"type": "error", "error": {"message": error}}))
                    except OSError:
                        pass
            store_response(db, rid, status, back, bytes(raw), error)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _handle

        def log_message(self, *args: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


# -- run ----------------------------------------------------------------------


def run_capture(
    db_path: Path,
    label: str,
    prompt: str,
    cwd: Path,
    claude_args: list[str],
    snapshot_files: list[Path],
    claude_bin: str = "claude",
    upstream: str = UPSTREAM,
) -> int:
    db = CaptureDB(db_path)
    try:
        version = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
        files = {str(p): db.put(p.read_bytes(), "file") for p in snapshot_files if p.is_file()}
        argv = [claude_bin, "-p", "--output-format", "stream-json", "--verbose", *claude_args]
        env_keep = {
            k: os.environ[k] for k in ("MCP_TIMEOUT", "MCP_TOOL_TIMEOUT") if k in os.environ
        }
        cid = db.new_capture(
            label=label,
            started_at=_now(),
            cwd=str(cwd),
            argv_json=json.dumps(argv),
            env_json=json.dumps(env_keep, sort_keys=True),
            prompt_sha=db.put(prompt.encode(), "prompt"),
            claude_version=version,
            files_json=json.dumps(files, sort_keys=True),
        )
        server = make_proxy(db, cid, upstream=upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        env = {**os.environ, "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}"}
        print(f"capture {cid}: proxy on 127.0.0.1:{port}, db {db_path}", file=sys.stderr)

        stderr_path = db_path.with_suffix(f".capture{cid}.stderr")
        with open(stderr_path, "wb") as err:
            proc = subprocess.Popen(
                argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(prompt.encode())
            proc.stdin.close()
            n = 0
            for raw_line in proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
                if not line:
                    continue
                n += 1
                db.add_event(cid, n, line)
                _progress(n, line)
            code = proc.wait()
        time.sleep(1)  # let a trailing request finish writing
        server.shutdown()
        db.update(
            "captures",
            cid,
            finished_at=_now(),
            exit_code=code,
            stderr_sha=db.put(stderr_path.read_bytes(), "stderr"),
        )
        stderr_path.unlink()
        print(f"capture {cid}: claude exited {code}; {n} events", file=sys.stderr)
        return code
    finally:
        db.close()


def _progress(n: int, line: str) -> None:
    """One short line per event on stderr so a watcher can see it is alive."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return
    t = obj.get("type")
    if t == "assistant":
        for b in obj.get("message", {}).get("content", []):
            if b.get("type") == "tool_use":
                print(f"[{n}] tool_use {b.get('name')}", file=sys.stderr, flush=True)
    elif t == "result":
        print(f"[{n}] result {obj.get('subtype')}", file=sys.stderr, flush=True)


# -- report -------------------------------------------------------------------


def _text_of(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return str(block.get("text", ""))
    return ""


def _preview(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + " ..."


def _short_args(args: Any, n: int = 140) -> str:
    return _preview(json.dumps(args, ensure_ascii=False), n)


def report(db: CaptureDB, capture_id: int) -> str:
    cap = db.conn.execute(
        "SELECT label, started_at, finished_at, cwd, argv_json, env_json, claude_version,"
        " exit_code, prompt_sha, files_json FROM captures WHERE id = ?",
        (capture_id,),
    ).fetchone()
    if cap is None:
        raise KeyError(capture_id)
    label, t0, t1, cwd, argv, envj, version, code, prompt_sha, files_json = cap
    reqs = db.conn.execute(
        "SELECT id, ordinal, method, path, status, model, system_json, tools_json,"
        " messages_json, response_sha, stop_reason, usage_json, error"
        " FROM requests WHERE capture_id = ? ORDER BY ordinal",
        (capture_id,),
    ).fetchall()
    out: list[str] = []
    w = out.append

    w(f"# Capture {capture_id}: {label}\n")
    w("| field | value |\n|---|---|")
    for k, v in (
        ("started", t0),
        ("finished", t1),
        ("claude", version),
        ("exit code", code),
        ("cwd", cwd),
        ("argv", " ".join(json.loads(argv))),
        ("env", envj),
        ("requests", len(reqs)),
    ):
        w(f"| {k} | `{v}` |")
    w("\n## Prompt\n")
    prompt = db.get(prompt_sha).decode() if prompt_sha else "(not recorded)"
    w("```\n" + prompt + "\n```\n")
    if files_json and json.loads(files_json):
        w("Files snapshotted at start (full text in `blobs`):\n")
        for path, sha in json.loads(files_json).items():
            w(f"- `{path}` sha256 `{sha[:12]}`")
        w("")

    # Group requests by (model, system prefix): the main thread and any side
    # calls (titles, subagents, summaries) have different prefixes.
    groups: dict[tuple, list] = {}
    for r in reqs:
        groups.setdefault((r[5], r[6]), []).append(r)
    w("## Request groups (model, system prompt)\n")
    w(
        "| group | model | requests | system blocks | tools | first ordinal |\n|---|---|---|---|---|---|"
    )
    names: dict[tuple, str] = {}
    for gi, (key, rs) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1])), 1):
        names[key] = f"G{gi}"
        sysref = json.loads(key[1]) if key[1] else []
        nsys = len(sysref) if isinstance(sysref, list) else 1
        ntools = len(json.loads(rs[0][7])) if rs[0][7] else 0
        w(f"| G{gi} | `{key[0]}` | {len(rs)} | {nsys} | {ntools} | {rs[0][1]} |")
    w("")

    # System prompt of each group, in full.
    for key, gname in names.items():
        if not key[1]:
            continue
        w(f"## {gname} system prompt\n")
        ref = json.loads(key[1])
        blocks = [db.get_json(s) for s in ref] if isinstance(ref, list) else [db.get_json(ref)]
        for i, b in enumerate(blocks, 1):
            text = _text_of(b)
            cache = b.get("cache_control") if isinstance(b, dict) else None
            w(f"### Block {i}: {len(text)} chars, cache_control `{cache}`\n")
            w("````text\n" + text + "\n````\n")

    # Tools offered, per distinct tool set.
    tool_sets: dict[str, list] = {}
    for r in reqs:
        if r[7]:
            tool_sets.setdefault(r[7], []).append(r[1])
    for ti, (tj, ords) in enumerate(tool_sets.items(), 1):
        tools = json.loads(tj)
        w(f"## Tool set T{ti}: {len(tools)} tools, used by {len(ords)} requests\n")
        w("| tool | source | schema chars | description (start) |\n|---|---|---|---|")
        for name, sha in tools:
            spec = db.get_json(sha)
            src = "mcp:" + name.split("__")[1] if name.startswith("mcp__") else "builtin"
            desc = _preview(str(spec.get("description", "")), 100).replace("|", "\\|")
            w(f"| `{name}` | {src} | {len(canonical(spec))} | {desc} |")
        w("")

    # Timeline of the conversation: what each request added.
    w("## Timeline\n")
    w("Each row is one API request. `added` lists what the messages gained since")
    w("the previous request in the same group; `reply` is the model's answer.\n")
    w("| # | group | added | reply | stop | in | cache read | cache write | out |")
    w("|---|---|---|---|---|---|---|---|---|")
    seen_msgs: dict[str, int] = {}
    reminders: list[tuple[int, str]] = []
    skill_events: list[tuple[int, str]] = []
    tool_counts: dict[str, int] = {}
    result_chars: dict[str, int] = {}
    pending_names: dict[str, str] = {}
    for r in reqs:
        rid, ordn, _m, path, status, model, sysj, _tj, msgj, resp_sha, stop, usagej, err = r
        added: list[str] = []
        if msgj:
            shas = json.loads(msgj)
            gkey = (model, sysj)
            start = seen_msgs.get(str(gkey), 0)
            for sha in shas[start:]:
                m = db.get_json(sha)
                content = m.get("content")
                items = (
                    content if isinstance(content, list) else [{"type": "text", "text": content}]
                )
                for c in items:
                    ct = c.get("type")
                    if ct == "text":
                        t = c.get("text", "")
                        if "<system-reminder>" in t:
                            reminders.append((ordn, t))
                            added.append("reminder")
                        elif m.get("role") == "user":
                            added.append("user text")
                    elif ct == "tool_result":
                        name = pending_names.get(c.get("tool_use_id"), "?")
                        body = c.get("content")
                        body_text = body if isinstance(body, str) else json.dumps(body)
                        size = len(body_text)
                        if "<system-reminder>" in body_text:
                            reminders.append((ordn, body_text))
                        result_chars[name] = result_chars.get(name, 0) + size
                        err_flag = " ERR" if c.get("is_error") else ""
                        added.append(f"result {name} {size}c{err_flag}")
            seen_msgs[str(gkey)] = len(shas)
        reply = ""
        if resp_sha:
            resp = db.get_json(resp_sha)
            parts = []
            for b in resp.get("content", []):
                bt = b.get("type")
                if bt == "tool_use":
                    name = b.get("name", "?")
                    pending_names[b.get("id")] = name
                    tool_counts[name] = tool_counts.get(name, 0) + 1
                    parts.append(f"{name} {_short_args(b.get('input'), 80)}")
                    inp = b.get("input") or {}
                    if name == "Skill" or (
                        name == "Read" and "skill" in str(inp.get("file_path", "")).lower()
                    ):
                        skill_events.append((ordn, f"{name} {_short_args(inp, 200)}"))
                elif bt == "text" and b.get("text", "").strip():
                    parts.append("text")
                elif bt == "thinking":
                    parts.append("thinking")
            reply = "; ".join(parts)
        u = json.loads(usagej) if usagej else {}
        row = [
            str(ordn),
            names.get((model, sysj), "-") if msgj else f"{r[2]} {path} {status}",
            _preview(", ".join(added), 200),
            _preview(reply, 200),
            str(stop or err or ""),
            str(u.get("input_tokens", "")),
            str(u.get("cache_read_input_tokens", "")),
            str(u.get("cache_creation_input_tokens", "")),
            str(u.get("output_tokens", "")),
        ]
        w("| " + " | ".join(x.replace("|", "\\|") for x in row) + " |")
    w("")

    w("## Tool calls by tool\n")
    w("| tool | calls | result chars |\n|---|---|---|")
    for name in sorted(tool_counts, key=lambda k: -tool_counts[k]):
        w(f"| `{name}` | {tool_counts[name]} | {result_chars.get(name, 0)} |")
    w("")

    w("## Skill loads (Skill tool, and Read on a path containing 'skill')\n")
    for ordn, s in skill_events or [(0, "none")]:
        w(f"- request {ordn}: `{s}`")
    w("")

    w("## Injected system reminders (first time each distinct text appears)\n")
    distinct: dict[str, int] = {}
    for ordn, t in reminders:
        distinct.setdefault(t, ordn)
    for t, ordn in distinct.items():
        w(f"### At request {ordn}, {len(t)} chars\n")
        w("````text\n" + t + "\n````\n")

    total = {
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
    }
    for r in reqs:
        u = json.loads(r[11]) if r[11] else {}
        for k in total:
            total[k] += int(u.get(k) or 0)
    w("## Usage totals (measured, from responses)\n")
    w("| field | tokens |\n|---|---|")
    for k, v in total.items():
        w(f"| {k} | {v} |")
    return "\n".join(out) + "\n"


# -- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analyst_driver.capture")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run claude -p behind the recording proxy")
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--label", required=True)
    p.add_argument("--prompt-file", required=True, type=Path)
    p.add_argument("--cwd", type=Path, default=Path.cwd())
    p.add_argument(
        "--snapshot",
        type=Path,
        action="append",
        default=[],
        help="file to store verbatim at start (repeatable); never a credentials file",
    )
    p.add_argument("--claude", default=shutil.which("claude") or "claude")
    p.add_argument(
        "claude_args", nargs=argparse.REMAINDER, help="after --, extra arguments for claude -p"
    )

    p = sub.add_parser("report", help="write a markdown report of one capture")
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--capture", type=int, default=None, help="default: the latest")
    p.add_argument("--out", type=Path, default=None)

    p = sub.add_parser("show", help="print one request body, rebuilt from its parts")
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--request", required=True, type=int)

    args = parser.parse_args(argv)
    if args.command == "run":
        extra = args.claude_args[1:] if args.claude_args[:1] == ["--"] else args.claude_args
        return run_capture(
            args.db,
            args.label,
            args.prompt_file.read_text(),
            args.cwd.resolve(),
            extra,
            args.snapshot,
            claude_bin=args.claude,
        )
    db = CaptureDB(args.db)
    try:
        if args.command == "report":
            cid = args.capture or db.conn.execute("SELECT MAX(id) FROM captures").fetchone()[0]
            text = report(db, cid)
            if args.out:
                args.out.write_text(text)
            else:
                sys.stdout.write(text)
        else:
            json.dump(reconstruct(db, args.request), sys.stdout, indent=1, ensure_ascii=False)
            sys.stdout.write("\n")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
