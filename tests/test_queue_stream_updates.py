from backend.domain.input_message import InputMessage
from backend.domain.message_queue import QueuedMessage
from backend.storage.memory_message_queue import MemoryMessageQueue
from backend.storage.runtime_event_stream import MemoryRuntimeEventStream


def test_queue_changes_publish_after_the_authoritative_mutation():
    queue = MemoryMessageQueue()
    changes = []
    queue.on_queue_change = lambda thread: changes.append(
        [(item.message.text, item.state) for item in queue.list(thread)]
    )
    queue.create(QueuedMessage("first", "thread", InputMessage.from_input("first")))
    queue.update("thread", "first", message=InputMessage.from_input("edited"))
    queue.create(QueuedMessage("second", "thread", InputMessage.from_input("second")))
    envelope = queue.dispatch(
        delivery_id="batch", message_ids=["first", "second"], session_id="session", thread_id="thread", turn_id="turn"
    )
    queue.release_turn("turn")
    queue.dispatch(
        delivery_id="batch", message_ids=["first", "second"], session_id="session", thread_id="thread", turn_id="turn"
    )
    queue.ack(queue.claim("turn", "test"))
    assert envelope.message.text == "edited\n\nsecond"
    assert changes == [
        [("first", "pending")],
        [("edited", "pending")],
        [("edited", "pending"), ("second", "pending")],
        [("edited", "dispatched"), ("second", "dispatched")],
        [("edited", "pending"), ("second", "pending")],
        [("edited", "dispatched"), ("second", "dispatched")],
        [],
    ]
    queue.close()


def test_stream_reads_preserve_order_and_batch_limit_after_a_long_history():
    stream = MemoryRuntimeEventStream()
    for index in range(1000):
        stream.publish(
            event_id=str(index), turn_id="turn", thread_id="thread", sequence=index, payload={"index": index}
        )
    first = stream.read_thread("thread", "800", block_ms=0)
    second = stream.read_thread("thread", first[-1].stream_id, block_ms=0)
    assert [entry.payload["index"] for entry in first + second] == list(range(800, 1000))
    assert len(first) == len(second) == 100
    stream.close()
