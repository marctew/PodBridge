"""My List: episodes saved for later, in an order you choose (position 1 is the top)."""

from __future__ import annotations

import sqlite3


def ids(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT episode_id FROM my_list ORDER BY position, added_at")]


def contains(conn: sqlite3.Connection, episode_id: int) -> bool:
    return conn.execute("SELECT 1 FROM my_list WHERE episode_id = ?", (episode_id,)).fetchone() is not None


def add(conn: sqlite3.Connection, episode_id: int) -> None:
    """Add to the bottom of the list (no-op if already there)."""
    conn.execute("INSERT OR IGNORE INTO my_list (episode_id, position) "
                 "SELECT ?, COALESCE(MAX(position), 0) + 1 FROM my_list", (episode_id,))


def remove(conn: sqlite3.Connection, episode_id: int) -> None:
    conn.execute("DELETE FROM my_list WHERE episode_id = ?", (episode_id,))


def reorder(conn: sqlite3.Connection, order: list[int]) -> None:
    """Apply a new order. IDs not in the list are ignored; listed episodes missing from `order`
    keep their relative order after the ones given."""
    current = ids(conn)
    present = set(current)
    wanted = list(dict.fromkeys(i for i in order if i in present))
    final = wanted + [i for i in current if i not in set(wanted)]
    conn.executemany("UPDATE my_list SET position = ? WHERE episode_id = ?",
                     [(pos, episode_id) for pos, episode_id in enumerate(final, start=1)])


def move(conn: sqlite3.Connection, episode_id: int, offset: int) -> None:
    """Move one place up (-1) or down (+1)."""
    current = ids(conn)
    if episode_id not in current:
        return
    i = current.index(episode_id)
    j = max(0, min(len(current) - 1, i + offset))
    current.insert(j, current.pop(i))
    reorder(conn, current)
