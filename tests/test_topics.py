"""Tests for app/topics.py — TopicStore persistence."""

import json
import os

from app.topics import TopicStore


def _path(tmp_path) -> str:
    return os.path.join(str(tmp_path), "topics.json")


class TestSetAndGet:
    def test_set_then_get_topic(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, 10, "Alice")
        assert store.get_topic(42) == 10

    def test_get_topic_missing_returns_none(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        assert store.get_topic(999) is None

    def test_get_title(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, 10, "Alice")
        assert store.get_title(42) == "Alice"

    def test_chat_for_topic(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, 10, "Alice")
        assert store.chat_for_topic(10) == 42

    def test_chat_for_topic_missing_returns_none(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        assert store.chat_for_topic(123) is None

    def test_update_title(self, tmp_path):
        store = TopicStore(_path(tmp_path))
        store.set_topic(42, 10, "12345")
        store.update_title(42, "Alice")
        assert store.get_title(42) == "Alice"


class TestPersistence:
    def test_mapping_survives_reload(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, 10, "Alice")
        store.set_topic(-100777, 20, "Team")

        reloaded = TopicStore(path)
        assert reloaded.get_topic(42) == 10
        assert reloaded.get_topic(-100777) == 20

    def test_reverse_lookup_survives_reload(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, 10, "Alice")

        reloaded = TopicStore(path)
        # numeric chat IDs round-trip back to int, not str
        assert reloaded.chat_for_topic(10) == 42

    def test_title_survives_reload(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, 10, "Alice")

        reloaded = TopicStore(path)
        assert reloaded.get_title(42) == "Alice"

    def test_file_is_valid_json(self, tmp_path):
        path = _path(tmp_path)
        store = TopicStore(path)
        store.set_topic(42, 10, "Alice")

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        assert data["chats"]["42"]["topic_id"] == 10

    def test_corrupt_file_starts_empty(self, tmp_path):
        path = _path(tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ not valid json")

        store = TopicStore(path)
        assert store.get_topic(42) is None
