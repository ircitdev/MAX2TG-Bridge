"""Tests for TelegramSender.send_status — status routing to a private chat."""

from unittest.mock import AsyncMock, MagicMock, patch

from app.tg_sender import TelegramSender


def _make_sender(status_chat_id=None):
    with patch("app.tg_sender.Bot") as bot_cls:
        bot_cls.return_value = AsyncMock()
        s = TelegramSender("tok", "-100123", MagicMock(), status_chat_id=status_chat_id)
    return s


class TestSendStatusRouting:
    async def test_goes_to_status_chat_when_configured(self):
        s = _make_sender(status_chat_id="271753809")
        await s.send_status("✅ подключён")
        s.bot.send_message.assert_awaited_once()
        kwargs = s.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == "271753809"
        assert "message_thread_id" not in kwargs

    async def test_falls_back_to_group_when_not_configured(self):
        s = _make_sender(status_chat_id=None)
        await s.send_status("✅ подключён")
        s.bot.send_message.assert_awaited_once()
        assert s.bot.send_message.await_args.kwargs["chat_id"] == "-100123"

    async def test_empty_text_sends_nothing(self):
        s = _make_sender(status_chat_id="271753809")
        await s.send_status("")
        s.bot.send_message.assert_not_awaited()

    async def test_does_not_fall_back_to_group_on_failure(self):
        """A failed DM must NOT leak the status into the shared supergroup."""
        s = _make_sender(status_chat_id="271753809")
        s.bot.send_message = AsyncMock(side_effect=Exception("bot was blocked"))
        with patch("app.tg_sender.asyncio.sleep", new=AsyncMock()):
            await s.send_status("⚠️ соединение потеряно")
        for call in s.bot.send_message.await_args_list:
            assert call.kwargs["chat_id"] == "271753809"
