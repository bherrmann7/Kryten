"""SQLite database for exercise tracking and usage logging."""

import sqlite3
import os
from datetime import datetime, date

DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DB_DIR, "kryten.db")


def get_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


PHOTOS_DIR = os.path.join(DB_DIR, "photos")


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS exercises (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            exercise TEXT NOT NULL,
            count REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT 'reps',
            notes TEXT,
            recorded_at TEXT NOT NULL DEFAULT (datetime('now')),
            recorded_date TEXT NOT NULL DEFAULT (date('now'))
        );

        CREATE TABLE IF NOT EXISTS exercise_photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            exercise_id INTEGER NOT NULL,
            file_id TEXT NOT NULL,
            local_path TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (exercise_id) REFERENCES exercises(id)
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            added_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS api_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            model TEXT,
            cost_usd REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS access_control (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            username TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            requested_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_exercises_user_date
            ON exercises(user_id, recorded_date);
        CREATE INDEX IF NOT EXISTS idx_exercises_date
            ON exercises(recorded_date);
    """)
    # Migrations
    cols = [r[1] for r in conn.execute("PRAGMA table_info(exercises)").fetchall()]
    if "unit" not in cols:
        conn.execute("ALTER TABLE exercises ADD COLUMN unit TEXT NOT NULL DEFAULT 'reps'")
    if "notes" not in cols:
        conn.execute("ALTER TABLE exercises ADD COLUMN notes TEXT")
    conn.commit()
    os.makedirs(PHOTOS_DIR, exist_ok=True)
    conn.close()


def upsert_user(user_id, username=None, first_name=None):
    conn = get_db()
    conn.execute(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               username=COALESCE(excluded.username, username),
               first_name=COALESCE(excluded.first_name, first_name)""",
        (user_id, username, first_name),
    )
    conn.commit()
    conn.close()


def find_user_by_name(name):
    """Find a user by first name (case-insensitive). Returns user_id or None."""
    conn = get_db()
    row = conn.execute(
        "SELECT user_id FROM users WHERE LOWER(first_name) = LOWER(?)",
        (name,),
    ).fetchone()
    conn.close()
    return row["user_id"] if row else None


def log_exercise(user_id, exercise, count, unit="reps", username=None, notes=None):
    """Log an exercise and return the new exercise row ID."""
    conn = get_db()
    cur = conn.execute(
        "INSERT INTO exercises (user_id, username, exercise, count, unit, notes) VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, username, exercise.lower().strip(), count, unit.lower().strip(), notes),
    )
    exercise_id = cur.lastrowid
    conn.commit()
    conn.close()
    return exercise_id


def add_exercise_photo(exercise_id, file_id, local_path=None):
    """Attach a photo to an exercise entry."""
    conn = get_db()
    conn.execute(
        "INSERT INTO exercise_photos (exercise_id, file_id, local_path) VALUES (?, ?, ?)",
        (exercise_id, file_id, local_path),
    )
    conn.commit()
    conn.close()




def get_stats(days=7, user_id=None):
    """Get exercise stats for the last N days."""
    conn = get_db()
    if user_id:
        rows = conn.execute(
            """SELECT u.first_name, e.recorded_date, e.exercise, e.unit, SUM(e.count) as total,
                      COUNT(DISTINCT ep.id) as photos
               FROM exercises e
               JOIN users u ON e.user_id = u.user_id
               LEFT JOIN exercise_photos ep ON e.id = ep.exercise_id
               WHERE e.user_id = ? AND e.recorded_date >= date('now', ? || ' days')
               GROUP BY e.user_id, e.recorded_date, e.exercise, e.unit
               ORDER BY e.recorded_date, u.first_name""",
            (user_id, -days),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT u.first_name, e.recorded_date, e.exercise, e.unit, SUM(e.count) as total,
                      COUNT(DISTINCT ep.id) as photos
               FROM exercises e
               JOIN users u ON e.user_id = u.user_id
               LEFT JOIN exercise_photos ep ON e.id = ep.exercise_id
               WHERE e.recorded_date >= date('now', ? || ' days')
               GROUP BY e.user_id, e.recorded_date, e.exercise, e.unit
               ORDER BY e.recorded_date, u.first_name""",
            (-days,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]




def log_api_usage(user_id, input_tokens, output_tokens, model, cost_usd=None):
    conn = get_db()
    conn.execute(
        """INSERT INTO api_usage (user_id, input_tokens, output_tokens, model, cost_usd)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, input_tokens, output_tokens, model, cost_usd),
    )
    conn.commit()
    conn.close()


def get_photos_for_date(date_str=None):
    """Get unique photos for a given date (default today). Groups participants
    when the same photo is attached to multiple users (shared workouts)."""
    conn = get_db()
    if not date_str:
        date_str = date.today().isoformat()
    rows = conn.execute(
        """SELECT ep.file_id, ep.local_path, e.exercise, e.count, e.unit, e.notes,
                  u.first_name
           FROM exercise_photos ep
           JOIN exercises e ON ep.exercise_id = e.id
           JOIN users u ON e.user_id = u.user_id
           WHERE e.recorded_date = ?
           ORDER BY e.recorded_at""",
        (date_str,),
    ).fetchall()
    conn.close()
    # Deduplicate by file_id, merging participant names
    seen = {}
    result = []
    for r in rows:
        d = dict(r)
        fid = d["file_id"]
        if fid in seen:
            # Add this person's name to the existing entry
            existing = seen[fid]
            if d["first_name"] not in existing["first_name"]:
                existing["first_name"] += " & " + d["first_name"]
        else:
            seen[fid] = d
            result.append(d)
    return result


def get_usage_summary():
    conn = get_db()
    row = conn.execute(
        """SELECT
               COUNT(*) as total_calls,
               SUM(input_tokens) as total_input,
               SUM(output_tokens) as total_output,
               SUM(cost_usd) as total_cost
           FROM api_usage"""
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def get_access_status(user_id):
    """Return 'approved', 'denied', 'pending', or None (never seen)."""
    conn = get_db()
    row = conn.execute(
        "SELECT status FROM access_control WHERE user_id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return row["status"] if row else None


def request_access(user_id, first_name=None, username=None):
    """Record a new access request. Returns True if newly created, False if already exists."""
    conn = get_db()
    existing = conn.execute(
        "SELECT status FROM access_control WHERE user_id = ?", (user_id,)
    ).fetchone()
    if existing:
        conn.close()
        return False
    conn.execute(
        "INSERT INTO access_control (user_id, first_name, username) VALUES (?, ?, ?)",
        (user_id, first_name, username),
    )
    conn.commit()
    conn.close()
    return True


def approve_access(user_id):
    """Approve a pending access request."""
    conn = get_db()
    conn.execute(
        "UPDATE access_control SET status = 'approved', resolved_at = datetime('now') "
        "WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()


def deny_access(user_id):
    """Deny a pending access request."""
    conn = get_db()
    conn.execute(
        "UPDATE access_control SET status = 'denied', resolved_at = datetime('now') "
        "WHERE user_id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()
