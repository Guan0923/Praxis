"""Compare HEAD and working-tree reads against local databases, always read-only."""

import sqlite3
import statistics
import subprocess
from contextlib import contextmanager
from time import perf_counter

from backend.configuration import ClientPaths
from backend.storage.sqlite import SQLiteSessionStore


class ReadOnlyStore(SQLiteSessionStore):
    def __init__(self):
        self.paths = ClientPaths.from_home()
        self.agent_thread_index = None

    @contextmanager
    def _connection(self, session_id, **_kwargs):
        connection = sqlite3.connect(self.paths.session_db(session_id).as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN")
            yield connection
        finally:
            connection.close()


def original_mixin(path, name):
    source = subprocess.check_output(["git", "show", "HEAD:" + path], encoding="utf-8")
    namespace = {"__name__": "backend.storage.profiling", "__package__": "backend.storage"}
    exec(compile(source, path, "exec"), namespace)
    return namespace[name]


def main():
    sessions = original_mixin("backend/src/storage/sqlite_sessions.py", "SQLiteSessionMixin")
    sidebar = original_mixin("backend/src/storage/sqlite_sidebar_threads.py", "SQLiteSidebarThreadMixin")
    before = type("OriginalReadOnlyStore", (sidebar, sessions, ReadOnlyStore), {})()
    after = ReadOnlyStore()
    for label, store in (("HEAD", before), ("working-tree", after)):
        for operation in ("queue-owner", "catalog"):
            elapsed = []
            for _ in range(3):
                started = perf_counter()
                if operation == "queue-owner":
                    store.get_sidebar_thread("session_6e251693f05048ce95f1988845753481")
                else:
                    store.list_sidebar_thread_summaries(state="all")
                elapsed.append((perf_counter() - started) * 1000)
            print(label, operation, "median_ms", round(statistics.median(elapsed), 2))


if __name__ == "__main__":
    main()
