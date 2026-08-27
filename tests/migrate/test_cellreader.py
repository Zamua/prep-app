"""The fleet adapter's half of the `/_migrate/dump` contract.

The route's half is pinned by `worker/tests/migrate.dump.test.ts`; this
pins the query the verifier builds, the header it sends and what it does
with each refusal. An unreadable cell is never a clean cell, so every
branch here raises.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from migrate.cellreader import (
    INTERNAL_TOKEN_HEADER,
    CellSealed,
    CellUnreachable,
    FixtureCellReader,
    HttpCellReader,
    read_all,
    token_from_file,
)

TOKEN = "an-internal-token"


class Recorder(BaseHTTPRequestHandler):
    """Answers whatever `server.script` says and records what it was
    asked."""

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's name
        parsed = urlparse(self.path)
        self.server.seen.append(
            {
                "path": parsed.path,
                "query": {k: v[0] for k, v in parse_qs(parsed.query).items()},
                "token": self.headers.get(INTERNAL_TOKEN_HEADER),
            }
        )
        status, body = self.server.script.pop(0)
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


@pytest.fixture
def fleet():
    server = HTTPServer(("127.0.0.1", 0), Recorder)
    server.seen = []
    server.script = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def reader_for(server) -> HttpCellReader:
    host, port = server.server_address[0], server.server_address[1]
    return HttpCellReader(f"http://{host}:{port}", TOKEN, timeout=5)


def test_the_query_names_the_user_the_table_the_cursor_and_the_columns(fleet):
    fleet.script = [(200, {"table": "reviews", "rows": [{"id": 1}], "next": 9})]
    page = reader_for(fleet).page(
        table="reviews", user="a@example.com", after=4, limit=25, columns=["id", "ts"]
    )
    assert page.rows == [{"id": 1}]
    assert page.next == 9
    seen = fleet.seen[0]
    assert seen["path"] == "/_migrate/dump"
    assert seen["token"] == TOKEN
    assert seen["query"] == {
        "table": "reviews",
        "limit": "25",
        "user": "a@example.com",
        "after": "4",
        "columns": "id,ts",
    }


def test_a_global_cell_is_addressed_by_name(fleet):
    fleet.script = [(200, {"table": "users", "rows": [], "next": None})]
    reader_for(fleet).page(table="users", cell="directory")
    assert fleet.seen[0]["query"]["cell"] == "directory"
    assert "user" not in fleet.seen[0]["query"]


def test_read_all_follows_the_cursor_to_the_end(fleet):
    fleet.script = [
        (200, {"rows": [{"id": 1}, {"id": 2}], "next": 2}),
        (200, {"rows": [{"id": 3}], "next": None}),
    ]
    rows = list(read_all(reader_for(fleet), table="reviews", user="a@example.com"))
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert [s["query"].get("after") for s in fleet.seen] == [None, "2"]


def test_a_sealed_fleet_is_its_own_refusal(fleet):
    fleet.script = [(410, {"detail": "the migration is sealed"})]
    with pytest.raises(CellSealed, match="sealed"):
        reader_for(fleet).page(table="decks", user="a@example.com")


def test_every_other_refusal_names_the_status_and_the_body(fleet):
    fleet.script = [(401, {"detail": "invalid X-Internal-Token"})]
    with pytest.raises(CellUnreachable, match="401"):
        reader_for(fleet).page(table="decks", user="a@example.com")


def test_an_answer_without_rows_is_refused_rather_than_read_as_empty(fleet):
    fleet.script = [(200, {"detail": "who knows"})]
    with pytest.raises(CellUnreachable, match="without a rows array"):
        reader_for(fleet).page(table="decks", user="a@example.com")


def test_a_non_integer_cursor_is_refused(fleet):
    fleet.script = [(200, {"rows": [], "next": "later"})]
    with pytest.raises(CellUnreachable, match="non-integer cursor"):
        reader_for(fleet).page(table="decks", user="a@example.com")


def test_an_unreachable_fleet_raises_rather_than_returning_nothing():
    reader = HttpCellReader("http://127.0.0.1:1", TOKEN, timeout=1)
    with pytest.raises(CellUnreachable):
        reader.page(table="decks", user="a@example.com")


class Looping:
    def page(self, **_kwargs):
        from migrate.cellreader import Page

        return Page([{"id": 1}], 7)


def test_a_cursor_that_repeats_is_reported_rather_than_spun_on():
    with pytest.raises(CellUnreachable, match="paged back"):
        list(read_all(Looping(), table="reviews", user="a@example.com"))


def test_an_empty_token_file_is_refused(tmp_path):
    path = tmp_path / "token"
    path.write_text("   \n")
    with pytest.raises(CellUnreachable, match="PREP_INTERNAL_TOKEN"):
        token_from_file(path)
    path.write_text(f"{TOKEN}\n")
    assert token_from_file(path) == TOKEN


def test_the_fixture_reader_pages_the_way_the_fleet_does():
    rows = [{"id": i} for i in range(7)]
    reader = FixtureCellReader({("a@example.com", "reviews"): rows}, page_size=3)
    assert [r["id"] for r in read_all(reader, table="reviews", user="a@example.com")] == list(
        range(7)
    )
    assert [call[2] for call in reader.calls] == [None, 3, 6]
