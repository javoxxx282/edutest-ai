import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "stats.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id         INTEGER PRIMARY KEY,
                username        TEXT,
                full_name       TEXT,
                quizzes         INTEGER NOT NULL DEFAULT 0,
                questions       INTEGER NOT NULL DEFAULT 0,
                correct         INTEGER NOT NULL DEFAULT 0,
                best_pct        REAL    NOT NULL DEFAULT 0,
                last_quiz       TEXT,
                joined_at       TEXT,
                referred_by     INTEGER,
                referral_count  INTEGER NOT NULL DEFAULT 0
            )
        """)
        # Migrations for existing DBs
        for col, definition in [
            ("joined_at",      "TEXT"),
            ("referred_by",    "INTEGER"),
            ("referral_count", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE user_stats ADD COLUMN {col} {definition}")
            except sqlite3.OperationalError:
                pass
        conn.commit()


def ensure_user(user_id: int, username: str | None, full_name: str | None) -> None:
    """Register a user the first time they appear (e.g. on /start)."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO user_stats (user_id, username, full_name, joined_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name,
                joined_at = COALESCE(joined_at, excluded.joined_at)
        """, (user_id, username, full_name, now))
        conn.commit()


def record_referral(new_user_id: int, referrer_id: int) -> bool:
    """
    Link new_user_id to referrer_id and increment the referrer's count.
    Returns True if the referral was recorded, False if already referred or self-referral.
    """
    if new_user_id == referrer_id:
        return False
    with _get_conn() as conn:
        # Only record once — if referred_by already set, skip
        row = conn.execute(
            "SELECT referred_by FROM user_stats WHERE user_id = ?", (new_user_id,)
        ).fetchone()
        if row and row["referred_by"] is not None:
            return False
        # Mark who referred this user
        conn.execute(
            "UPDATE user_stats SET referred_by = ? WHERE user_id = ?",
            (referrer_id, new_user_id),
        )
        # Increment referrer's count
        conn.execute(
            "UPDATE user_stats SET referral_count = referral_count + 1 WHERE user_id = ?",
            (referrer_id,),
        )
        conn.commit()
    return True


def get_referral_count(user_id: int) -> int:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT referral_count FROM user_stats WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["referral_count"] if row else 0


def get_top_referrers(limit: int = 10) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT user_id, username, full_name, referral_count
            FROM user_stats
            WHERE referral_count > 0
            ORDER BY referral_count DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def record_quiz(
    user_id: int,
    username: str | None,
    full_name: str | None,
    score: int,
    total: int,
) -> None:
    pct = round(score / total * 100, 1) if total > 0 else 0.0
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    with _get_conn() as conn:
        conn.execute("""
            INSERT INTO user_stats (user_id, username, full_name, quizzes, questions, correct, best_pct, last_quiz, joined_at)
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username  = excluded.username,
                full_name = excluded.full_name,
                quizzes   = quizzes + 1,
                questions = questions + excluded.questions,
                correct   = correct + excluded.correct,
                best_pct  = MAX(best_pct, excluded.best_pct),
                last_quiz = excluded.last_quiz,
                joined_at = COALESCE(joined_at, excluded.joined_at)
        """, (user_id, username, full_name, total, score, pct, now, now))
        conn.commit()


def get_stats(user_id: int) -> dict | None:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_stats WHERE user_id = ?", (user_id,)
        ).fetchone()
    if row is None:
        return None
    return dict(row)


def get_top(limit: int = 10) -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute("""
            SELECT *, ROUND(CAST(correct AS REAL) / NULLIF(questions, 0) * 100, 1) AS avg_pct
            FROM user_stats
            WHERE quizzes > 0
            ORDER BY avg_pct DESC, correct DESC
            LIMIT ?
        """, (limit,)).fetchall()
    return [dict(r) for r in rows]


def get_all_user_ids() -> list[int]:
    with _get_conn() as conn:
        rows = conn.execute("SELECT user_id FROM user_stats").fetchall()
    return [r["user_id"] for r in rows]
