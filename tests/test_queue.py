from parakeet_dictation.file_queue import QueueStatus, TranscriptionQueue


def test_add_many():
    q = TranscriptionQueue()
    added = q.add_many(["/a.mp3", "/b.wav", "/c.flac"])

    assert len(added) == 3
    assert q.items()[0].filename == "a.mp3"
    assert q.items()[2].filename == "c.flac"


def test_remove():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3", "/b.wav", "/c.flac"])
    items = q.items()
    q.remove(items[1].id)

    remaining = q.items()
    assert len(remaining) == 2
    assert remaining[0].filename == "a.mp3"
    assert remaining[1].filename == "c.flac"


def test_move_item():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3", "/b.wav", "/c.flac"])
    items = q.items()

    # Move last item to first position
    q.move(items[2].id, 0)
    reordered = q.items()
    assert reordered[0].filename == "c.flac"
    assert reordered[1].filename == "a.mp3"
    assert reordered[2].filename == "b.wav"


def test_move_clamps_to_bounds():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3", "/b.wav"])
    items = q.items()

    q.move(items[0].id, -5)
    assert q.items()[0].filename == "a.mp3"

    q.move(items[1].id, 100)
    assert q.items()[-1].filename == "b.wav"


def test_clear():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3", "/b.wav"])
    q.clear()
    assert q.items() == []


def test_cancelled_files_run_again_but_failed_ones_do_not():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3", "/b.wav", "/c.flac"])
    items = q.items()
    q.set_status(items[0].id, QueueStatus.DONE, result_text="text")
    q.set_status(items[1].id, QueueStatus.CANCELLED)
    q.set_status(items[2].id, QueueStatus.FAILED, error="oops")
    assert q.pending_count() == 0
    q.requeue_cancelled()
    assert [item.status for item in q.items()] == ["done", "pending", "failed"]
    assert q.pending_count() == 1


def test_set_status():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3"])
    item_id = q.items()[0].id

    q.set_status(item_id, "done", result_text="hello world")
    updated = q.items()[0]
    assert updated.status == "done"
    assert updated.result_text == "hello world"


def test_pending_count():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3", "/b.wav", "/c.flac"])
    items = q.items()
    q.set_status(items[0].id, "done")

    assert q.pending_count() == 2


def test_items_returns_copies():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3"])

    items1 = q.items()
    items1[0].status = "done"

    items2 = q.items()
    assert items2[0].status == "pending"
