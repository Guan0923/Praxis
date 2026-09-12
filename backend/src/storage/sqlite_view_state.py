"""Backend-owned per-Thread reading and composer state."""

from backend.domain.state import utc_now


class SQLiteViewStateMixin:
    def get_view_state(self, session_id: str, thread_id: str) -> dict:
        with self._connection(session_id) as connection:
            return self._json_object(connection, session_id, "view_state", thread_id) or {
                "session_id": session_id,
                "thread_id": thread_id,
                "revision": 0,
                "draft": "",
                "references": [],
                "uploads": [],
                "reading": None,
                "expanded": {},
            }

    def patch_view_state(self, session_id: str, thread_id: str, patch: dict) -> dict:
        with self._connection(session_id, write=True, refresh_index=False, notify=False) as connection:
            current = self._json_object(connection, session_id, "view_state", thread_id) or {
                "session_id": session_id,
                "thread_id": thread_id,
                "revision": 0,
                "draft": "",
                "references": [],
                "uploads": [],
                "reading": None,
                "expanded": {},
            }
            current.update(patch)
            current["revision"] += 1
            self._put_json_object(connection, session_id, "view_state", thread_id, current, utc_now())
        return current
