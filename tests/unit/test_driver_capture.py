"""Unit tests for analyst_driver.capture — the recording proxy.

The load-bearing tests: bytes pass through the proxy unchanged, the stored
parts rebuild the request body exactly, and a credential header never reaches
the database file.
"""

import http.server
import json
import sqlite3
import sys
import threading
import zlib

import httpx
import pytest

from analyst_driver.capture import (
    CaptureDB,
    make_proxy,
    reassemble_sse,
    reconstruct,
    report,
    run_capture,
    store_body,
)

SECRET = "sk-ant-oat01-THIS-MUST-NOT-BE-STORED"


def _assert_no_secret(db_path):
    """Scan raw files and every decompressed blob: blobs are zlib, so a raw scan alone proves nothing."""
    for f in db_path.parent.glob(db_path.name + "*"):
        assert SECRET.encode() not in f.read_bytes(), f
    conn = sqlite3.connect(db_path)
    for (z,) in conn.execute("SELECT zdata FROM blobs"):
        assert SECRET.encode() not in zlib.decompress(z)
    for row in conn.execute("SELECT headers_json FROM requests"):
        assert SECRET not in row[0]
    conn.close()


def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM = _sse(
    [
        {
            "type": "message_start",
            "message": {
                "id": "m1",
                "role": "assistant",
                "model": "x",
                "content": [],
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 7},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "hm"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "SIG"},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Hel"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "lo"}},
        {"type": "content_block_stop", "index": 1},
        {
            "type": "content_block_start",
            "index": 2,
            "content_block": {"type": "tool_use", "id": "tu1", "name": "Read", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": '{"file_'},
        },
        {
            "type": "content_block_delta",
            "index": 2,
            "delta": {"type": "input_json_delta", "partial_json": 'path": "/a/skills/x.md"}'},
        },
        {"type": "content_block_stop", "index": 2},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 5},
        },
        {"type": "message_stop"},
    ]
)

BODY = {
    "model": "claude-sonnet-5",
    "max_tokens": 100,
    "stream": True,
    "system": [
        {"type": "text", "text": "You are Claude Code."},
        {"type": "text", "text": "CLAUDE.md here", "cache_control": {"type": "ephemeral"}},
    ],
    "tools": [
        {"name": "Read", "input_schema": {"type": "object"}},
        {"name": "mcp__ms-inspect__ms_field_list", "input_schema": {"type": "object"}},
    ],
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "<system-reminder>skills: x</system-reminder>"},
                {"type": "text", "text": "reduce it"},
            ],
        }
    ],
}


def test_reassemble_sse():
    msg = reassemble_sse(STREAM)
    assert msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 10, "cache_read_input_tokens": 7, "output_tokens": 5}
    assert msg["content"][0] == {"type": "thinking", "thinking": "hm", "signature": "SIG"}
    assert msg["content"][1] == {"type": "text", "text": "Hello"}
    assert msg["content"][2]["input"] == {"file_path": "/a/skills/x.md"}
    assert "_unknown" not in msg


def test_reassemble_keeps_unknown_events():
    msg = reassemble_sse(_sse([{"type": "message_start", "message": {}}, {"type": "brand_new"}]))
    assert msg["_unknown"] == [{"type": "brand_new"}]


def test_body_round_trip_and_dedup(tmp_path):
    db = CaptureDB(tmp_path / "c.db")
    cid = db.new_capture(label="t", started_at="now", cwd="/", argv_json="[]", env_json="{}")
    r1 = db.new_request(cid, "POST", "/v1/messages", {})
    r2 = db.new_request(cid, "POST", "/v1/messages", {})
    raw = json.dumps(BODY).encode()
    store_body(db, r1, raw)
    store_body(db, r2, raw)
    assert reconstruct(db, r1) == BODY
    assert reconstruct(db, r2) == BODY
    # One raw body, two system blocks, two tools, one message: stored once each.
    (n,) = db.conn.execute("SELECT COUNT(*) FROM blobs").fetchone()
    assert n == 6
    db.close()


class _Upstream(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    seen_auth: list = []

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        _Upstream.seen_auth.append(self.headers.get("authorization"))
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        # Two writes, so the proxy has to relay a stream in pieces.
        half = len(STREAM) // 2
        self.wfile.write(STREAM[:half])
        self.wfile.flush()
        self.wfile.write(STREAM[half:])

    def log_message(self, *a):
        pass


@pytest.fixture
def upstream():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_proxy_passthrough_and_no_secret(tmp_path, upstream):
    db = CaptureDB(tmp_path / "c.db")
    cid = db.new_capture(label="t", started_at="now", cwd="/", argv_json="[]", env_json="{}")
    proxy = make_proxy(db, cid, upstream=upstream)
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{proxy.server_address[1]}/v1/messages?beta=true"
    resp = httpx.post(
        url,
        content=json.dumps(BODY).encode(),
        headers={
            "authorization": f"Bearer {SECRET}",
            "x-api-key": SECRET,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    proxy.shutdown()
    assert resp.status_code == 200
    assert resp.content == STREAM
    assert _Upstream.seen_auth[-1] == f"Bearer {SECRET}"

    row = db.conn.execute(
        "SELECT id, headers_json, stop_reason, usage_json, path FROM requests"
    ).fetchone()
    kept = json.loads(row[1])
    assert set(kept) == {"anthropic-version", "content-type", "user-agent"}
    assert row[2] == "tool_use"
    assert json.loads(row[3])["output_tokens"] == 5
    assert row[4] == "/v1/messages?beta=true"
    assert reconstruct(db, row[0]) == BODY

    text = report(db, cid)
    assert "CLAUDE.md here" in text
    assert "mcp__ms-inspect__ms_field_list" in text
    assert "<system-reminder>skills: x</system-reminder>" in text
    assert "/a/skills/x.md" in text
    db.close()
    _assert_no_secret(tmp_path / "c.db")


def test_run_capture_with_fake_claude(tmp_path, upstream):
    """A stand-in for claude: posts one request to ANTHROPIC_BASE_URL, prints stream-json."""
    fake = tmp_path / "fake_claude.py"
    fake.write_text(
        "import os, sys, json, httpx\n"
        "if sys.argv[1:] == ['--version']:\n"
        "    print('9.9.9 (fake)'); sys.exit(0)\n"
        "prompt = sys.stdin.read()\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'tools': ['Read']}))\n"
        f"r = httpx.post(os.environ['ANTHROPIC_BASE_URL'] + '/v1/messages', content={json.dumps(json.dumps(BODY))},"
        f" headers={{'authorization': 'Bearer {SECRET}', 'content-type': 'application/json'}})\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'prompt': prompt}))\n"
    )
    launcher = tmp_path / "claude"
    launcher.write_text(f'#!/bin/sh\nexec {sys.executable} {fake} "$@"\n')
    launcher.chmod(0o755)
    snap = tmp_path / "mcp.json"
    snap.write_text('{"mcpServers": {}}')

    code = run_capture(
        tmp_path / "c.db",
        "fake",
        "reduce 3C391",
        tmp_path,
        [],
        [snap],
        claude_bin=str(launcher),
        upstream=upstream,
    )
    assert code == 0
    db = CaptureDB(tmp_path / "c.db")
    events = db.conn.execute("SELECT type, subtype FROM events ORDER BY ordinal").fetchall()
    assert events == [("system", "init"), ("result", "success")]
    cap_row = db.conn.execute(
        "SELECT claude_version, exit_code, files_json FROM captures"
    ).fetchone()
    assert cap_row[0] == "9.9.9 (fake)" and cap_row[1] == 0
    assert str(snap) in json.loads(cap_row[2])
    (rid,) = db.conn.execute("SELECT id FROM requests").fetchone()
    assert reconstruct(db, rid) == BODY
    db.close()
    _assert_no_secret(tmp_path / "c.db")
