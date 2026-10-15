"""A TEXT-storage-class ``display_identity`` must not brick the session reader.

Observed in the wild: a writer path stored the 32-byte raw SHA-256 digest with
TEXT storage class (the raw invalid-UTF-8 digest bytes). Every Python reader
using the default ``text_factory`` then died with ``Could not decode to UTF-8
column 'display_identity'`` while fetching those rows, and the messages API
500'd the whole session load — one cosmetic metadata cell bricking a
multi-thousand-message transcript.

The repair migration re-types legacy TEXT cells to BLOB (``CAST`` preserves
the raw digest bytes, so the dedupe index keeps working), and the SCHEMA_SQL
guard triggers normalize any future TEXT-class write at insert/update time.
"""

import sqlite3
from pathlib import Path

from hermes_state import SessionDB


def _seed(path: Path) -> None:
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    db.append_message("chat", "user", "q", timestamp=1727000000.0)
    db.append_message("chat", "assistant", "a", timestamp=1727000001.0)
    db.close()


def _corrupt_all(path: Path) -> None:
    """Re-type every digest cell to the observed corrupt state (CAST keeps the raw bytes).

    Drops the SCHEMA_SQL guard triggers first: the wild writer predates them, and a
    guard left in place would normalize the poison back to BLOB mid-statement.
    """
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS messages_display_identity_text_insert_guard")
    conn.execute("DROP TRIGGER IF EXISTS messages_display_identity_text_update_guard")
    conn.execute("UPDATE messages SET display_identity = CAST(display_identity AS TEXT)")
    conn.commit()
    conn.close()


def _downgrade_to_v31(path: Path) -> None:
    """Replay a pre-32 store: created before the repair, then corrupted in the wild."""
    conn = sqlite3.connect(path)
    conn.execute("UPDATE schema_version SET version = 31")
    conn.commit()
    conn.close()


def _text_cells(path: Path) -> int:
    conn = sqlite3.connect(path)
    conn.text_factory = bytes
    n = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE typeof(display_identity) = 'text'"
    ).fetchone()[0]
    conn.close()
    return n


def test_migration_repairs_text_storage_class(tmp_path):
    path = Path(tmp_path) / "state.db"
    _seed(path)
    _corrupt_all(path)
    assert _text_cells(path) >= 1  # the corrupt state exists on disk
    _downgrade_to_v31(path)

    # Re-opening runs the v32 repair; afterwards the reader must survive the
    # default text_factory (this raised `Could not decode to UTF-8` before).
    db = SessionDB(path)
    rows = db.get_messages("chat", include_compacted=True)
    assert [m["role"] for m in rows] == ["user", "assistant"]
    assert _text_cells(path) == 0


def test_repair_is_lossless_for_the_dedupe_index(tmp_path):
    path = Path(tmp_path) / "state.db"
    _seed(path)
    _corrupt_all(path)
    _downgrade_to_v31(path)
    conn = sqlite3.connect(path)
    conn.text_factory = bytes
    before = dict(conn.execute("SELECT id, display_identity FROM messages"))
    conn.close()

    SessionDB(path)  # runs the repair

    conn = sqlite3.connect(path)
    conn.text_factory = bytes
    after = dict(conn.execute("SELECT id, display_identity FROM messages"))
    conn.close()
    # CAST blob->text keeps the raw bytes; CAST text->blob restores them: the
    # dedupe index sees the identical digest, not a re-encoded value.
    assert before == after


def test_guard_normalizes_future_text_writes(tmp_path):
    path = Path(tmp_path) / "state.db"
    db = SessionDB(path)
    db.create_session("chat", source="desktop")
    db.append_message("chat", "user", "q", timestamp=1727000000.0)
    conn = sqlite3.connect(path)
    conn.text_factory = bytes
    original = conn.execute(
        "SELECT display_identity FROM messages WHERE session_id = 'chat'"
    ).fetchone()[0]

    # A writer that lands the digest as TEXT (however it happens) self-heals:
    def _poison(conn):
        conn.execute("UPDATE messages SET display_identity = CAST(display_identity AS TEXT)")

    db._execute_write(_poison)
    assert conn.execute(
        "SELECT COUNT(*) FROM messages WHERE typeof(display_identity) = 'text'"
    ).fetchone()[0] == 0
    repaired = conn.execute(
        "SELECT display_identity FROM messages WHERE session_id = 'chat'"
    ).fetchone()[0]
    assert repaired == original  # same raw digest bytes, just the right storage class
    conn.close()
