"""Tests for album batching: several MAX frames → one Telegram post."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.max_listener import _AlbumBuffer, _groupable_item
from app.tg_sender import TelegramSender


def _buffer(window=0.05, max_wait=1.0):
    sender = AsyncMock()
    return _AlbumBuffer(sender, window=window, max_wait=max_wait), sender


def _item(n):
    return ("photo", f"bytes{n}".encode(), f"{n}.jpg")


class TestAlbumBuffer:
    async def test_items_from_one_call_are_posted_as_one_album(self):
        buf, sender = _buffer()
        await buf.add(("c", "u"), 7, "подпись", [_item(1), _item(2), _item(3)])
        await asyncio.sleep(0.15)

        sender.send_media_group.assert_awaited_once()
        items, kwargs = sender.send_media_group.await_args
        assert len(items[0]) == 3
        assert kwargs["caption"] == "подпись"
        assert kwargs["message_thread_id"] == 7

    async def test_separate_frames_within_window_merge(self):
        """The actual bug: MAX splits one album across several messages."""
        buf, sender = _buffer()
        key = ("c", "u")
        await buf.add(key, 7, "подпись", [_item(1)])
        await asyncio.sleep(0.01)
        await buf.add(key, 7, "", [_item(2)])
        await asyncio.sleep(0.01)
        await buf.add(key, 7, "", [_item(3)])
        await asyncio.sleep(0.15)

        sender.send_media_group.assert_awaited_once()
        assert len(sender.send_media_group.await_args[0][0]) == 3

    async def test_empty_caption_does_not_override_the_first(self):
        buf, sender = _buffer()
        key = ("c", "u")
        await buf.add(key, 7, "первая", [_item(1)])
        await buf.add(key, 7, "", [_item(2)])
        await asyncio.sleep(0.15)
        sender.send_media_group.assert_awaited_once()
        assert sender.send_media_group.await_args[1]["caption"] == "первая"

    async def test_a_different_caption_starts_a_new_album(self):
        """Two captioned messages back to back are two posts — the second
        caption must not be silently dropped into the first album."""
        buf, sender = _buffer()
        key = ("c", "u")
        await buf.add(key, 7, "первая", [_item(1)])
        await buf.add(key, 7, "вторая", [_item(2)])
        await asyncio.sleep(0.15)
        captions = [c[1]["caption"] for c in sender.send_media_group.await_args_list]
        assert captions == ["первая", "вторая"]

    async def test_thread_id_is_adopted_once_a_real_one_appears(self):
        """The first part can arrive while ensure_topic is still failing; the
        album must not end up in General because of it."""
        buf, sender = _buffer()
        key = ("c", "u")
        await buf.add(key, None, "подпись", [_item(1)])
        await buf.add(key, 42, "", [_item(2)])
        await asyncio.sleep(0.15)
        assert sender.send_media_group.await_args[1]["message_thread_id"] == 42

    async def test_a_real_thread_id_is_not_replaced_by_none(self):
        buf, sender = _buffer()
        key = ("c", "u")
        await buf.add(key, 42, "подпись", [_item(1)])
        await buf.add(key, None, "", [_item(2)])
        await asyncio.sleep(0.15)
        assert sender.send_media_group.await_args[1]["message_thread_id"] == 42

    async def test_gap_longer_than_window_splits_albums(self):
        buf, sender = _buffer(window=0.05)
        key = ("c", "u")
        await buf.add(key, 7, "a", [_item(1)])
        await asyncio.sleep(0.2)
        await buf.add(key, 7, "b", [_item(2)])
        await asyncio.sleep(0.2)
        assert sender.send_media_group.await_count == 2

    async def test_different_senders_do_not_merge(self):
        buf, sender = _buffer()
        await buf.add(("c", "u1"), 7, "a", [_item(1)])
        await buf.add(("c", "u2"), 7, "b", [_item(2)])
        await asyncio.sleep(0.15)
        assert sender.send_media_group.await_count == 2

    async def test_explicit_flush_posts_immediately(self):
        buf, sender = _buffer(window=10.0)
        key = ("c", "u")
        await buf.add(key, 7, "a", [_item(1), _item(2)])
        await buf.flush(key)
        sender.send_media_group.assert_awaited_once()

    async def test_flush_of_empty_key_is_a_noop(self):
        buf, sender = _buffer()
        await buf.flush(("nothing", "here"))
        sender.send_media_group.assert_not_awaited()

    async def test_bucket_is_not_posted_twice(self):
        buf, sender = _buffer(window=0.05)
        key = ("c", "u")
        await buf.add(key, 7, "a", [_item(1)])
        await buf.flush(key)
        await asyncio.sleep(0.2)          # the timer must not fire as well
        sender.send_media_group.assert_awaited_once()

    async def test_max_wait_caps_a_continuous_trickle(self):
        """A steady stream keeps resetting the window; max_wait forces a post."""
        buf, sender = _buffer(window=0.1, max_wait=0.25)
        key = ("c", "u")
        for i in range(10):
            await buf.add(key, 7, "a" if i == 0 else "", [_item(i)])
            await asyncio.sleep(0.05)
        assert sender.send_media_group.await_count >= 1

    async def test_send_failure_does_not_propagate(self):
        buf, sender = _buffer()
        sender.send_media_group.side_effect = Exception("telegram down")
        await buf.add(("c", "u"), 7, "a", [_item(1)])
        await asyncio.sleep(0.15)   # must not raise out of the timer task


class TestGroupableItem:
    async def test_photo_is_groupable(self):
        client = AsyncMock()
        client.download_file.return_value = b"img"
        item = await _groupable_item({"_type": "PHOTO", "baseUrl": "http://x/1"}, client)
        assert item == ("photo", b"img", "photo.jpg")

    async def test_image_file_is_groupable(self):
        client = AsyncMock()
        client.download_file.return_value = b"img"
        item = await _groupable_item(
            {"_type": "FILE", "name": "photo.png", "url": "http://x/1"}, client)
        assert item == ("photo", b"img", "photo.png")

    async def test_video_type_is_not_groupable(self):
        """MAX only gives a thumbnail for _type=VIDEO — grouping it would hide
        that the original is a video."""
        client = AsyncMock()
        assert await _groupable_item({"_type": "VIDEO", "thumbnail": "http://x"}, client) is None

    async def test_document_is_not_groupable(self):
        client = AsyncMock()
        item = await _groupable_item(
            {"_type": "FILE", "name": "report.pdf", "url": "http://x/1"}, client)
        assert item is None

    async def test_failed_download_is_not_groupable(self):
        client = AsyncMock()
        client.download_file.return_value = None
        assert await _groupable_item({"_type": "PHOTO", "baseUrl": "http://x"}, client) is None


class TestSendMediaGroup:
    def _sender(self):
        with patch("app.tg_sender.Bot") as bot_cls:
            bot_cls.return_value = AsyncMock()
            return TelegramSender("tok", "-100123", MagicMock())

    async def test_single_item_is_sent_as_a_plain_photo(self):
        """Telegram rejects a one-element media group."""
        s = self._sender()
        await s.send_media_group([_item(1)], caption="подпись", message_thread_id=7)
        s.bot.send_media_group.assert_not_awaited()
        s.bot.send_photo.assert_awaited_once()

    async def test_album_carries_caption_on_first_item_only(self):
        s = self._sender()
        await s.send_media_group([_item(1), _item(2), _item(3)], caption="подпись")
        s.bot.send_media_group.assert_awaited_once()
        media = s.bot.send_media_group.await_args.kwargs["media"]
        assert media[0].caption == "подпись"
        assert [m.caption for m in media[1:]] == [None, None]

    async def test_more_than_ten_items_are_chunked(self):
        s = self._sender()
        await s.send_media_group([_item(i) for i in range(23)], caption="подпись")
        assert s.bot.send_media_group.await_count == 3
        sizes = [len(c.kwargs["media"]) for c in s.bot.send_media_group.await_args_list]
        assert sizes == [10, 10, 3]

    async def test_empty_list_sends_nothing(self):
        s = self._sender()
        await s.send_media_group([], caption="x")
        s.bot.send_media_group.assert_not_awaited()
        s.bot.send_photo.assert_not_awaited()


class TestAttachRouting:
    """_send_attaches must not let a non-groupable attachment overtake the album."""

    def _ctx(self):
        client = AsyncMock()
        client.download_file.return_value = b"img"
        sender = AsyncMock()
        albums = AsyncMock()
        return client, sender, albums

    async def test_photos_go_to_the_buffer_not_straight_out(self):
        from app.max_listener import _send_attaches
        client, sender, albums = self._ctx()
        attaches = [{"_type": "PHOTO", "baseUrl": "http://x/1"},
                    {"_type": "PHOTO", "baseUrl": "http://x/2"}]

        used = await _send_attaches(attaches, client, sender, "хедер", "текст",
                                    7, None, albums, ("c", "u"))

        assert albums.add.await_count == 2
        assert used is True
        sender.send_photo.assert_not_awaited()

    async def test_mixed_attachments_keep_their_original_order(self):
        """PHOTO → DOC → PHOTO must not become PHOTO → PHOTO → DOC."""
        from app.max_listener import _send_attaches
        client, sender, albums = self._ctx()
        order = []
        albums.add.side_effect = lambda *a, **k: order.append("photo")
        albums.flush.side_effect = lambda *a, **k: order.append("flush")

        with patch("app.max_listener._send_attach",
                   new=AsyncMock(side_effect=lambda *a, **k: order.append("doc"))):
            await _send_attaches(
                [{"_type": "PHOTO", "baseUrl": "http://x/1"},
                 {"_type": "FILE", "name": "report.pdf", "url": "http://x/2"},
                 {"_type": "PHOTO", "baseUrl": "http://x/3"}],
                client, sender, "хедер", "", 7, None, albums, ("c", "u"))

        assert order == ["photo", "flush", "doc", "photo"]

    async def test_document_flushes_the_album_first(self):
        from app.max_listener import _send_attaches
        client, sender, albums = self._ctx()
        order = []
        albums.flush.side_effect = lambda *a, **k: order.append("flush")

        with patch("app.max_listener._send_attach",
                   new=AsyncMock(side_effect=lambda *a, **k: order.append("doc"))):
            await _send_attaches(
                [{"_type": "PHOTO", "baseUrl": "http://x/1"},
                 {"_type": "FILE", "name": "report.pdf", "url": "http://x/2"}],
                client, sender, "хедер", "", 7, None, albums, ("c", "u"))

        assert order == ["flush", "doc"], "album must be posted before the document"

    async def test_caption_is_not_reused_after_the_album_took_it(self):
        from app.max_listener import _send_attaches
        client, sender, albums = self._ctx()
        captions = []

        async def capture(attach, cl, sn, cap, **kw):
            captions.append(cap)

        with patch("app.max_listener._send_attach", new=AsyncMock(side_effect=capture)):
            await _send_attaches(
                [{"_type": "PHOTO", "baseUrl": "http://x/1"},
                 {"_type": "FILE", "name": "report.pdf", "url": "http://x/2"}],
                client, sender, "хедер", "текст", 7, None, albums, ("c", "u"))

        assert albums.add.await_args[0][2] == "хедер\nтекст"
        assert captions == ["хедер"], "body text must not be repeated on the document"

    async def test_without_a_buffer_media_is_sent_directly(self):
        from app.max_listener import _send_attaches
        client, sender, _ = self._ctx()
        await _send_attaches([{"_type": "PHOTO", "baseUrl": "http://x/1"}],
                             client, sender, "хедер", "", 7, None, None, ("c", "u"))
        sender.send_media_group.assert_awaited_once()
