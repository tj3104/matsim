"""SQLite の接続とスキーマ。"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from . import settings

SCHEMA = """
-- ジョブの持ち主。MatSimApp のアカウントで、Python・MCP から投入したジョブの user_id は NULL
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS structures (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    formula TEXT NOT NULL,
    n_atoms INTEGER NOT NULL,
    info TEXT NOT NULL,              -- StructureInfo.as_dict() の JSON
    source TEXT NOT NULL,            -- upload / mp / job
    source_detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS potentials (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    elements TEXT NOT NULL,          -- JSON の元素リスト
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- md
    status TEXT NOT NULL,            -- preparing / queued / running / done / failed / cancelled
    params TEXT NOT NULL,            -- 投入時の入力 (JSON)。再投入に使う
    summary TEXT NOT NULL DEFAULT '{}',  -- 一覧に出す要約 (構造名・ポテンシャル名など)
    n_atoms INTEGER NOT NULL,
    total_steps INTEGER NOT NULL,
    pid INTEGER,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    speed REAL,                      -- 実測の速度 [原子×ステップ/秒]
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    finished_at TEXT,
    series TEXT                      -- 温度スキャンなどで一緒に投入したジョブの組の名前
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""


def connect() -> sqlite3.Connection:
    # リクエストの前処理と後処理が別のスレッドで動くことがあるので、スレッドの確認は外す
    # (接続は 1 リクエストまたは見張りスレッドの中でしか使わない)
    conn = sqlite3.connect(settings.DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    for directory in (settings.DATA_DIR, settings.STRUCTURE_DIR, settings.POTENTIAL_DIR,
                      settings.JOB_DIR, settings.TMP_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    os.chmod(settings.DATA_DIR, 0o700)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        # 後から足した列 (前の版で作ったデータベースにはないので足す)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "series" not in columns:
            conn.execute("ALTER TABLE jobs ADD COLUMN series TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_series ON jobs(series)")
    finally:
        conn.close()
    os.chmod(settings.DB_PATH, 0o600)


def get_db() -> Iterator[sqlite3.Connection]:
    """リクエストや Client の呼び出しごとの接続。正常終了でコミット、例外でロールバックする。"""
    conn = connect()
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


transaction = contextmanager(get_db)
