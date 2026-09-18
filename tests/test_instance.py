from parakeet_dictation.instance import InstanceLock


def test_only_one_instance_owns_shared_storage_and_lock_is_reusable(tmp_path):
    path = tmp_path / "app.lock"
    first, second = InstanceLock(path), InstanceLock(path)
    try:
        assert first.acquire()
        assert first.acquire()
        assert not second.acquire()
        first.close()
        assert second.acquire()
        assert not first.acquire()
    finally:
        first.close()
        second.close()
    assert path.exists()  # Preserve the inode so waiting processes agree.
