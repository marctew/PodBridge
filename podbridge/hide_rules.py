"""Per-source auto-hide rules: hide new junk (trailers, clips, posts that will never be in the
podcast) without doing it by hand each time.

Rules run after every refresh and sync, and when a rule is added. An episode you unhide by
hand gets hide_override set and is never re-hidden. Deleting a rule unhides what it hid.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .library import fold

KINDS = {
    "title_contains": "Title contains",
    "shorter_than": "Shorter than (minutes)",
    "unmatched_after_days": "No Pocket Casts match after (days)",
}


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: int
    source_id: int
    kind: str
    value: str

    def describe(self) -> str:
        if self.kind == "title_contains":
            return f"Title contains “{self.value}”"
        if self.kind == "shorter_than":
            return f"Shorter than {self.value} minute{'' if self.value == '1' else 's'}"
        return f"No Pocket Casts match after {self.value} day{'' if self.value == '1' else 's'}"

    def matches(self, episode: sqlite3.Row, now: datetime) -> bool:
        if self.kind == "title_contains":
            return fold(self.value) in fold(episode["title"] or "")
        if self.kind == "shorter_than":
            return episode["duration_secs"] is not None and episode["duration_secs"] < float(self.value) * 60
        if episode["match_method"] != "none":
            return False
        first_seen = datetime.fromisoformat(episode["created_at"].replace("Z", "+00:00"))
        return now - first_seen >= timedelta(days=float(self.value))


def validate(kind: str, value: str) -> str:
    value = value.strip()
    if kind not in KINDS:
        raise RuleError("Pick a rule type.")
    if kind == "title_contains":
        if not value or len(value) > 100:
            raise RuleError("Enter some text to look for in titles (up to 100 characters).")
        return value
    try:
        number = float(value)
    except ValueError:
        raise RuleError("Enter a number.") from None
    if not 0 < number <= 10000:
        raise RuleError("Enter a number above 0.")
    return f"{number:g}"


def rules_for(conn: sqlite3.Connection, source_id: int | None = None) -> list[Rule]:
    where, args = ("WHERE source_id = ?", (source_id,)) if source_id is not None else ("", ())
    return [Rule(r["id"], r["source_id"], r["kind"], r["value"])
            for r in conn.execute(f"SELECT * FROM hide_rules {where} ORDER BY id", args)]


def add_rule(conn: sqlite3.Connection, source_id: int, kind: str, value: str) -> tuple[Rule, int]:
    value = validate(kind, value)
    with conn:
        rule_id = conn.execute("INSERT INTO hide_rules (source_id, kind, value) VALUES (?, ?, ?)",
                               (source_id, kind, value)).lastrowid
    rule = Rule(rule_id, source_id, kind, value)
    return rule, apply_rules(conn, [rule])


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> int:
    """Delete a rule and unhide what it hid. Returns how many were unhidden."""
    with conn:
        unhidden = conn.execute("UPDATE episodes SET hidden = 0, hidden_by_rule = NULL WHERE hidden_by_rule = ?",
                                (rule_id,)).rowcount
        conn.execute("DELETE FROM hide_rules WHERE id = ?", (rule_id,))
    return unhidden


def apply_rules(conn: sqlite3.Connection, rules: list[Rule] | None = None,
                now: datetime | None = None) -> int:
    """Hide visible episodes caught by a rule (skipping ones you've unhidden by hand).
    Returns how many were newly hidden."""
    rules = rules if rules is not None else rules_for(conn)
    now = now or datetime.now(timezone.utc)
    hidden = 0
    with conn:
        for rule in rules:
            candidates = conn.execute(
                "SELECT id, title, duration_secs, match_method, created_at FROM episodes "
                "WHERE source_id = ? AND hidden = 0 AND hide_override = 0", (rule.source_id,)).fetchall()
            for ep in candidates:
                if rule.matches(ep, now):
                    conn.execute("UPDATE episodes SET hidden = 1, hidden_by_rule = ? WHERE id = ?", (rule.id, ep["id"]))
                    hidden += 1
    return hidden


def caught_counts(conn: sqlite3.Connection) -> dict[int, int]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT hidden_by_rule, COUNT(*) FROM episodes WHERE hidden_by_rule IS NOT NULL GROUP BY hidden_by_rule")}
