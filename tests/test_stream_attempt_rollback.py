import requests

from backend.providers.transport import _transport_error
from backend.domain.input_message import InputMessage
from backend.domain.runtime_state import InMemoryNodeStore
from backend.runtime.core.events import RuntimeEvent
from backend.runtime.node_bridge import RuntimeEventNodeBridge


def test_partial_stream_is_removed_before_retry():
    store = InMemoryNodeStore()
    bridge = RuntimeEventNodeBridge(
        store, session_id="s", thread_id="s", turn_id="t",
        message=InputMessage.from_input("hello"), provider_name="local", emit=lambda frame: None,
    )
    bridge.start()
    bridge.handle(RuntimeEvent("model_request", "request"))
    bridge.handle(RuntimeEvent("response_start", ""))
    bridge.handle(RuntimeEvent("response_delta", "discard this partial answer"))
    bridge.handle(RuntimeEvent("model_retry", "read timed out", {"attempt": 1, "max_transport_retries": 5}))
    bridge.handle(RuntimeEvent("model_request", "request"))
    bridge.handle(RuntimeEvent("response_start", ""))
    bridge.handle(RuntimeEvent("response_delta", "successful answer"))
    bridge.handle(RuntimeEvent("response_end", ""))
    items = bridge.assistant.assistant_items
    assert [item.get("text") for item in items if item["type"] == "text"] == ["successful answer"]
    assert items[0]["type"] == "retry"


def test_read_timeout_after_stream_started_is_retryable():
    error = _transport_error(requests.ReadTimeout("timed out"), stream_started=True)
    assert error.retryable and error.stream_started
