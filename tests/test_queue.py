from parakeet_dictation.queue import OutputConfig, OutputMode, QueueItem, TranscriptionQueue


def test_add_and_list_items():
    q = TranscriptionQueue()
    q.add("/path/to/file.mp3")
    q.add("/path/to/other.wav")

    items = q.items()
    assert len(items) == 2
    assert items[0].filename == "file.mp3"
    assert items[1].filename == "other.wav"
    assert items[0].status == "pending"


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


def test_clear_done():
    q = TranscriptionQueue()
    q.add_many(["/a.mp3", "/b.wav", "/c.flac"])
    items = q.items()
    q.set_status(items[0].id, "done", result_text="text")
    q.set_status(items[1].id, "failed", error="oops")

    q.clear_done()
    remaining = q.items()
    assert len(remaining) == 1
    assert remaining[0].filename == "c.flac"


def test_set_status():
    q = TranscriptionQueue()
    q.add("/a.mp3")
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
    q.add("/a.mp3")

    items1 = q.items()
    items1[0].status = "done"

    items2 = q.items()
    assert items2[0].status == "pending"
