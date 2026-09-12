"""Automatic title policy for the first main-thread Turn."""

from backend.domain.runtime_state import RuntimeRootState
from backend.planning.llm.titles import normalize_conversation_title
from backend.storage.codec import is_default_session_title


def _first_main_user_text(store, session_id: str, turn_id: str) -> str:
    """Return text only when ``turn_id`` is the main Thread's first Turn below the synthetic root."""

    first_turn = store.get_node(session_id, turn_id)
    if first_turn is None or isinstance(first_turn, RuntimeRootState) or first_turn.thread_id != session_id:
        return ""
    parent = store.get_node(session_id, first_turn.parent_id)
    if not isinstance(parent, RuntimeRootState):
        return ""
    content = first_turn.user_message.get("content", [])
    if not content or content[0].get("type") != "text":
        return ""
    text = content[0].get("text")
    return text if isinstance(text, str) else ""


def _auto_title_main_thread(
    conversation,
    store,
    *,
    session_id: str,
    thread_id: str,
    turn_id: str,
) -> None:
    """Name an untouched main Thread without affecting the completed chat result."""

    if thread_id != session_id:
        return
    sidebar = store.get_sidebar_thread(thread_id, session_id=session_id)
    if sidebar is None or sidebar.title_is_custom or not is_default_session_title(sidebar.title):
        return
    first_user_text = _first_main_user_text(store, session_id, turn_id)
    fallback = normalize_conversation_title(first_user_text)
    if not fallback:
        return
    try:
        title = normalize_conversation_title(conversation.generate_title(first_user_text)) or fallback
    except Exception:
        title = fallback
    latest = store.get_sidebar_thread(thread_id, session_id=session_id)
    if latest is None or latest.title_is_custom or not is_default_session_title(latest.title):
        return
    store.update_sidebar_thread(thread_id, session_id=session_id, title=title, title_is_custom=False)
