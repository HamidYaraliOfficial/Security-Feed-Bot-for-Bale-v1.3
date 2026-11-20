# -*- coding: utf-8 -*-
"""Tiny SQLite store.

queue : posts waiting to be sent (full content).  A row is DELETED right after it is sent.
seen  : only a 20-char hash + timestamp per sent post (needed so the same article is not sent twice).
        Rows older than `seen_retention_days` are pruned automatically.
"""
import os
import sqlite3
import threading
import time


class Store:
    def __init__(self, path):
        self.path = str(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()
        self._init()

    def _run(self, fn):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                conn.row_factory = sqlite3.Row
                result = fn(conn)
                conn.commit()
                return result
            finally:
                conn.close()

    def _init(self):
        def fn(c):
            c.execute("PRAGMA auto_vacuum=FULL")      # file shrinks by itself after deletes (new DBs only)
            c.executescript("""
                CREATE TABLE IF NOT EXISTS queue(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    uid TEXT UNIQUE NOT NULL,
                    source TEXT, category TEXT, title TEXT, link TEXT, summary TEXT,
                    image_url TEXT, published_ts INTEGER, created_ts INTEGER,
                    tries INTEGER DEFAULT 0,
                    content_html TEXT DEFAULT '', plan TEXT DEFAULT '', progress INTEGER DEFAULT 0,
                    tr_defer INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS seen(uid TEXT PRIMARY KEY, ts INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen(ts);
                CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
            """)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(queue)")}      # migrate older databases
            for name, ddl in (("content_html", "TEXT DEFAULT ''"), ("plan", "TEXT DEFAULT ''"),
                              ("progress", "INTEGER DEFAULT 0"), ("tr_defer", "INTEGER DEFAULT 0")):
                if name not in cols:
                    c.execute(f"ALTER TABLE queue ADD COLUMN {name} {ddl}")
        self._run(fn)

    # ---- meta ----
    def get_meta(self, k, default=None):
        row = self._run(lambda c: c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone())
        return row["v"] if row else default

    def set_meta(self, k, v):
        self._run(lambda c: c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v))))

    # ---- queue / seen ----
    def is_known(self, uid):
        def fn(c):
            return c.execute("SELECT 1 FROM seen WHERE uid=? UNION ALL SELECT 1 FROM queue WHERE uid=? LIMIT 1",
                             (uid, uid)).fetchone()
        return self._run(fn) is not None

    def add_pending(self, it):
        def fn(c):
            cur = c.execute(
                "INSERT OR IGNORE INTO queue(uid,source,category,title,link,summary,image_url,published_ts,created_ts,"
                "content_html) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (it["uid"], it["source"], it["category"], it["title"], it["link"], it["summary"][:3000],
                 it.get("image_url", ""), int(it["published_ts"]), int(time.time()),
                 it.get("content_html", "")[:400_000]))
            return cur.rowcount > 0
        return self._run(fn)

    def mark_seen(self, uid):
        self._run(lambda c: c.execute("INSERT OR REPLACE INTO seen(uid,ts) VALUES(?,?)", (uid, int(time.time()))))

    def pending(self, limit):
        rows = self._run(lambda c: c.execute(
            "SELECT * FROM queue ORDER BY published_ts ASC, id ASC LIMIT ?", (limit,)).fetchall())
        return [dict(r) for r in rows]

    def finish(self, row_id, uid):
        """After a successful send (or giving up): remove from queue, keep only the tiny 'seen' marker."""
        def fn(c):
            c.execute("DELETE FROM queue WHERE id=?", (row_id,))
            c.execute("INSERT OR REPLACE INTO seen(uid,ts) VALUES(?,?)", (uid, int(time.time())))
        self._run(fn)

    def save_plan(self, row_id, plan_json):
        """Store the fully built post (all parts); the raw feed HTML is no longer needed."""
        self._run(lambda c: c.execute("UPDATE queue SET plan=?, content_html='', progress=0 WHERE id=?",
                                      (plan_json, row_id)))

    def update_plan(self, row_id, plan_json, progress=None):
        """Persist a changed plan (translation results, or the final list of messages)."""
        if progress is None:
            self._run(lambda c: c.execute("UPDATE queue SET plan=?, content_html='' WHERE id=?", (plan_json, row_id)))
        else:
            self._run(lambda c: c.execute("UPDATE queue SET plan=?, progress=?, content_html='' WHERE id=?",
                                          (plan_json, progress, row_id)))

    def set_progress(self, row_id, n):
        self._run(lambda c: c.execute("UPDATE queue SET progress=? WHERE id=?", (n, row_id)))

    def bump_defer(self, row_id):
        def fn(c):
            c.execute("UPDATE queue SET tr_defer=tr_defer+1 WHERE id=?", (row_id,))
            return c.execute("SELECT tr_defer FROM queue WHERE id=?", (row_id,)).fetchone()["tr_defer"]
        return self._run(fn)

    def reset_defer(self, row_id):
        self._run(lambda c: c.execute("UPDATE queue SET tr_defer=0 WHERE id=?", (row_id,)))

    def bump(self, row_id):
        def fn(c):
            c.execute("UPDATE queue SET tries=tries+1 WHERE id=?", (row_id,))
            return c.execute("SELECT tries FROM queue WHERE id=?", (row_id,)).fetchone()["tries"]
        return self._run(fn)

    def prune(self, retention_days, max_queue=500):
        now = int(time.time())

        def fn(c):
            c.execute("DELETE FROM seen WHERE ts < ?", (now - retention_days * 86400,))
            over = c.execute("SELECT id, uid FROM queue ORDER BY published_ts DESC LIMIT -1 OFFSET ?",
                             (max_queue,)).fetchall()
            for r in over:
                c.execute("INSERT OR REPLACE INTO seen(uid,ts) VALUES(?,?)", (r["uid"], now))
                c.execute("DELETE FROM queue WHERE id=?", (r["id"],))
            return len(over)
        return self._run(fn)

    def stats(self):
        def fn(c):
            q = c.execute("SELECT COUNT(*) n FROM queue").fetchone()["n"]
            s = c.execute("SELECT COUNT(*) n FROM seen").fetchone()["n"]
            return q, s
        q, s = self._run(fn)
        try:
            size = os.path.getsize(self.path)
        except OSError:
            size = 0
        return {"queue": q, "seen": s, "db_bytes": size}

    def vacuum(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                conn.isolation_level = None
                conn.execute("VACUUM")
            finally:
                conn.close()

    def clear_queue(self):
        self._run(lambda c: c.execute("DELETE FROM queue"))
        self.vacuum()

    def clear_all(self):
        def fn(c):
            c.execute("DELETE FROM queue")
            c.execute("DELETE FROM seen")
            c.execute("DELETE FROM meta WHERE k='initialized'")
        self._run(fn)
        self.vacuum()
