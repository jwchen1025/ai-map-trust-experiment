import sqlite3

from flask import current_app, g


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
 id INTEGER PRIMARY KEY AUTOINCREMENT, participant_code TEXT NOT NULL UNIQUE,
 age INTEGER NOT NULL CHECK(age >= 16), gender TEXT NOT NULL,
 education_level TEXT NOT NULL, consent_at TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conditions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE,
 source_type TEXT NOT NULL, error_level TEXT NOT NULL, map_filename TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, participant_id INTEGER NOT NULL REFERENCES participants(id),
 condition_id INTEGER REFERENCES conditions(id), source_label TEXT,
 status TEXT NOT NULL DEFAULT 'intro', intro_confirmed_at TEXT, assigned_at TEXT,
 map_opened_at TEXT, map_closed_at TEXT, map_view_duration_ms INTEGER, completed_at TEXT
);
CREATE TABLE IF NOT EXISTS scenario_responses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id),
 scenario_code TEXT NOT NULL, selected_exit TEXT NOT NULL, confidence_score INTEGER NOT NULL CHECK(confidence_score BETWEEN 1 AND 7),
 started_at TEXT NOT NULL, submitted_at TEXT NOT NULL, decision_time_ms INTEGER NOT NULL, UNIQUE(session_id, scenario_code)
);
CREATE TABLE IF NOT EXISTS scenario_timing_state (
 session_id INTEGER NOT NULL REFERENCES sessions(id), scenario_code TEXT NOT NULL, started_at TEXT NOT NULL,
 UNIQUE(session_id, scenario_code)
);
CREATE TABLE IF NOT EXISTS baseline_visual_responses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id), task_code TEXT NOT NULL,
 selected_answer TEXT NOT NULL, correct_answer TEXT NOT NULL, started_at TEXT NOT NULL, submitted_at TEXT NOT NULL,
 response_time_ms INTEGER NOT NULL, UNIQUE(session_id, task_code)
);
CREATE TABLE IF NOT EXISTS baseline_profile_responses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id), item_code TEXT NOT NULL,
 item_text TEXT NOT NULL, response_value TEXT NOT NULL, response_label TEXT NOT NULL, answered_at TEXT NOT NULL,
 UNIQUE(session_id, item_code)
);
CREATE TABLE IF NOT EXISTS revisit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id),
 scenario_code TEXT NOT NULL, opened_at TEXT NOT NULL, closed_at TEXT, duration_ms INTEGER, UNIQUE(session_id, scenario_code)
);
CREATE TABLE IF NOT EXISTS revisit_reason_responses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
 reason_code TEXT NOT NULL, other_reason TEXT, answered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scale_responses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES sessions(id),
 scale_code TEXT NOT NULL, item_code TEXT NOT NULL, item_text TEXT NOT NULL,
 score INTEGER NOT NULL CHECK(score BETWEEN 1 AND 7), answered_at TEXT NOT NULL, UNIQUE(session_id, item_code)
);
CREATE TABLE IF NOT EXISTS manipulation_checks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
 perceived_source TEXT NOT NULL, perceived_error TEXT NOT NULL, answered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS open_responses (
 id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL UNIQUE REFERENCES sessions(id),
 production_description TEXT NOT NULL, ai_trust_change TEXT NOT NULL, answered_at TEXT NOT NULL
);
"""

# PostgreSQL accepts the rest of the schema syntax. SERIAL keeps foreign-key IDs
# compatible with the existing INTEGER columns.
POSTGRES_SCHEMA = SQLITE_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")


class PostgresConnection:
    """Small compatibility layer for the existing SQLite-style query calls."""

    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _convert(sql):
        return sql.replace("?", "%s")

    def execute(self, sql, params=None):
        return self.connection.execute(self._convert(sql), params or ())

    def executemany(self, sql, params):
        return self.connection.executemany(self._convert(sql), params)

    def commit(self):
        self.connection.commit()

    def close(self):
        self.connection.close()


def using_postgres():
    return bool(current_app.config.get("DATABASE_URL"))


def get_db():
    if "db" not in g:
        if using_postgres():
            from psycopg import connect
            from psycopg.rows import dict_row
            g.db = PostgresConnection(connect(current_app.config["DATABASE_URL"], row_factory=dict_row))
        else:
            g.db = sqlite3.connect(current_app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
            g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db:
        db.close()


def _run_schema(db, schema):
    if using_postgres():
        for statement in schema.split(";"):
            if statement.strip():
                db.execute(statement)
    else:
        db.executescript(schema)


def _migrate_sqlite_age_constraint(db):
    participant_sql = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='participants'").fetchone()[0]
    if "BETWEEN 18 AND 30" not in participant_sql:
        return
    db.execute("PRAGMA foreign_keys = OFF")
    db.executescript("""
        CREATE TABLE participants_new (
          id INTEGER PRIMARY KEY AUTOINCREMENT, participant_code TEXT NOT NULL UNIQUE,
          age INTEGER NOT NULL CHECK(age >= 16), gender TEXT NOT NULL,
          education_level TEXT NOT NULL, consent_at TEXT NOT NULL, created_at TEXT NOT NULL
        );
        INSERT INTO participants_new(id,participant_code,age,gender,education_level,consent_at,created_at)
          SELECT id,participant_code,age,gender,education_level,consent_at,created_at FROM participants;
        DROP TABLE participants;
        ALTER TABLE participants_new RENAME TO participants;
    """)
    db.execute("PRAGMA foreign_keys = ON")


def initialize_database():
    db = get_db()
    _run_schema(db, POSTGRES_SCHEMA if using_postgres() else SQLITE_SCHEMA)
    if not using_postgres():
        _migrate_sqlite_age_constraint(db)
    db.executemany("""INSERT INTO conditions(code,source_type,error_level,map_filename) VALUES(?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET source_type=excluded.source_type, error_level=excluded.error_level, map_filename=excluded.map_filename""", current_app.config["CONDITIONS"])
    db.commit()


def init_app(app):
    app.teardown_appcontext(close_db)
