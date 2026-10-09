"""Telegram sender attribution for text and attached media forwarded to MAX."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Chat, MessageEntity, User
from telegram.constants import MessageEntityType

from app.tg_handler import (
    ALLOWED_USER_KEY,
    MAX_CLIENT_KEY,
    SENDER_PREFIX_KEY,
    TOPIC_STORE_KEY,
    _on_topic_media,
    _on_topic_message,
    build_tg_app,
)


MEDIA_KINDS = ["photo", "voice", "audio", "document", "video"]


def _make_update(text="Привет", *, user=None, sender_chat=None):
    message = MagicMock()
    message.text = text
    message.entities = []
    message.caption = None
    message.caption_entities = []
    message.from_user = user
    message.sender_chat = sender_chat
    message.message_thread_id = 10
    message.is_topic_message = True
    message.reply_text = AsyncMock()
    message.set_reaction = AsyncMock()
    for kind in MEDIA_KINDS:
        setattr(message, kind, None)

    update = MagicMock()
    update.message = message
    update.effective_user = user
    return update


def _make_context(sender_prefix=True):
    max_client = MagicMock()
    max_client.send_message = AsyncMock(return_value={"ok": True})
    max_client.upload_photo = AsyncMock(
        return_value={"_type": "PHOTO", "photoToken": "photo-token"}
    )
    max_client.upload_audio = AsyncMock(
        return_value={"_type": "FILE", "fileId": "voice-file"}
    )
    max_client.upload_file = AsyncMock(
        return_value={"_type": "FILE", "fileId": "uploaded-file"}
    )
    topic_store = MagicMock()
    topic_store.chat_for_topic.return_value = 42
    context = MagicMock()
    context.bot_data = {
        MAX_CLIENT_KEY: max_client,
        TOPIC_STORE_KEY: topic_store,
        ALLOWED_USER_KEY: None,
    }
    if sender_prefix is not None:
        context.bot_data[SENDER_PREFIX_KEY] = sender_prefix
    return context, max_client


def _set_media(message, kind, caption):
    tg_file = MagicMock()
    tg_file.download_as_bytearray = AsyncMock(return_value=bytearray(b"media"))
    media = MagicMock()
    media.get_file = AsyncMock(return_value=tg_file)
    media.file_name = f"attachment.{kind}"
    media.mime_type = "application/octet-stream"
    setattr(message, kind, [media] if kind == "photo" else media)
    message.caption = caption
    return media


def _sender_link(text, name, username):
    return {
        "type": "LINK",
        "from": text.index(name),
        "length": len(name),
        "attributes": {"url": f"https://t.me/{username}"},
    }


async def test_text_uses_display_name_and_native_max_link():
    user = User(100, "Иван", False, last_name="Петров", username="ivan_petrov")
    update = _make_update("Привет!", user=user)
    context, max_client = _make_context()

    await _on_topic_message(update, context)

    expected = "Сообщение из Telegram от Иван Петров:\n\nПривет!"
    max_client.send_message.assert_awaited_once_with(
        42,
        expected,
        elements=[_sender_link(expected, "Иван Петров", "ivan_petrov")],
    )
    update.message.reply_text.assert_not_awaited()


async def test_text_uses_first_name_without_username_or_last_name():
    user = User(100, "Мария", False)
    update = _make_update("Привет!", user=user)
    context, max_client = _make_context()

    await _on_topic_message(update, context)

    max_client.send_message.assert_awaited_once_with(
        42, "Сообщение из Telegram от Мария:\n\nПривет!", elements=[]
    )


async def test_sender_name_is_literal_text_without_html_or_markdown_escaping():
    user = User(100, "<Анна> & [друзья]", False, username="anna")
    update = _make_update("Текст <b>как написан</b>", user=user)
    context, max_client = _make_context()

    await _on_topic_message(update, context)

    expected = (
        "Сообщение из Telegram от <Анна> & [друзья]:\n\n"
        "Текст <b>как написан</b>"
    )
    max_client.send_message.assert_awaited_once_with(
        42,
        expected,
        elements=[_sender_link(expected, user.full_name, "anna")],
    )


async def test_unicode_sender_and_body_preserve_and_shift_formatting():
    user = User(100, "Анна 🦊", False, last_name="Иванова", username="anna")
    body = "😀 Привет ссылка"
    update = _make_update(body, user=user)
    update.message.entities = [
        MessageEntity(MessageEntityType.BOLD, offset=3, length=6),
        MessageEntity(
            MessageEntityType.TEXT_LINK,
            offset=10,
            length=6,
            url="https://example.com/telegram",
        ),
    ]
    context, max_client = _make_context()

    await _on_topic_message(update, context)

    expected = "Сообщение из Telegram от Анна 🦊 Иванова:\n\n😀 Привет ссылка"
    max_client.send_message.assert_awaited_once_with(
        42,
        expected,
        elements=[
            _sender_link(expected, user.full_name, "anna"),
            {"type": "STRONG", "from": expected.index("Привет"), "length": 6},
            {
                "type": "LINK",
                "from": expected.index("ссылка"),
                "length": 6,
                "attributes": {"url": "https://example.com/telegram"},
            },
        ],
    )


@pytest.mark.parametrize("sender_prefix", [None, False], ids=["unset", "false"])
async def test_disabled_text_keeps_original_text_and_formatting(sender_prefix):
    user = User(100, "Анна", False, username="anna")
    update = _make_update("😀 Привет", user=user)
    update.message.entities = [
        MessageEntity(MessageEntityType.BOLD, offset=3, length=6)
    ]
    context, max_client = _make_context(sender_prefix)

    await _on_topic_message(update, context)

    max_client.send_message.assert_awaited_once_with(
        42,
        "😀 Привет",
        elements=[{"type": "STRONG", "from": 2, "length": 6}],
    )


@pytest.mark.parametrize("kind", MEDIA_KINDS)
@pytest.mark.parametrize("caption", [None, "😀 Подпись"])
async def test_media_attributes_sender_in_same_message_as_attachment(kind, caption):
    user = User(100, "Анна 🦊", False, last_name="Иванова", username="anna")
    update = _make_update(text=None, user=user)
    media = _set_media(update.message, kind, caption)
    if caption:
        update.message.caption_entities = [
            MessageEntity(MessageEntityType.BOLD, offset=3, length=7),
            MessageEntity(
                MessageEntityType.TEXT_LINK,
                offset=3,
                length=7,
                url="https://example.com/caption",
            ),
        ]
    context, max_client = _make_context()

    await _on_topic_media(update, context)

    expected = "Сообщение из Telegram от Анна 🦊 Иванова:"
    expected_elements = [_sender_link(expected, user.full_name, "anna")]
    if caption:
        expected += "\n\n😀 Подпись"
        expected_elements.append(
            {"type": "STRONG", "from": expected.index("Подпись"), "length": 7}
        )
        expected_elements.append(
            {
                "type": "LINK",
                "from": expected.index("Подпись"),
                "length": 7,
                "attributes": {"url": "https://example.com/caption"},
            }
        )
    uploader = {
        "photo": max_client.upload_photo,
        "voice": max_client.upload_audio,
    }.get(kind, max_client.upload_file)
    assert uploader.await_count == 1
    assert uploader.await_args.args == (b"media",)
    assert uploader.await_args.kwargs["chat_id"] == 42
    media.get_file.assert_awaited_once()
    max_client.send_message.assert_awaited_once_with(
        42,
        text=expected,
        elements=expected_elements,
        attaches=[uploader.return_value],
    )
    update.message.reply_text.assert_not_awaited()


async def test_media_without_username_still_attributes_display_name():
    user = User(100, "Иван", False, last_name="Петров")
    update = _make_update(text=None, user=user)
    _set_media(update.message, "photo", "Подпись")
    context, max_client = _make_context()

    await _on_topic_media(update, context)

    max_client.send_message.assert_awaited_once_with(
        42,
        text="Сообщение из Telegram от Иван Петров:\n\nПодпись",
        elements=[],
        attaches=[max_client.upload_photo.return_value],
    )


@pytest.mark.parametrize("kind", MEDIA_KINDS)
@pytest.mark.parametrize("caption", [None, "Подпись"])
@pytest.mark.parametrize("sender_prefix", [None, False], ids=["unset", "false"])
async def test_disabled_media_keeps_original_caption(kind, caption, sender_prefix):
    user = User(100, "Иван", False, username="ivan")
    update = _make_update(text=None, user=user)
    _set_media(update.message, kind, caption)
    if caption:
        update.message.caption_entities = [
            MessageEntity(MessageEntityType.BOLD, offset=0, length=7)
        ]
    context, max_client = _make_context(sender_prefix)

    await _on_topic_media(update, context)

    max_client.send_message.assert_awaited_once()
    call = max_client.send_message.await_args
    assert call.args == (42,)
    assert call.kwargs["text"] == (caption or "")
    assert call.kwargs["elements"] == (
        [{"type": "STRONG", "from": 0, "length": 7}] if caption else []
    )
    assert len(call.kwargs["attaches"]) == 1


@pytest.mark.parametrize("username", [None, "group_name"])
async def test_anonymous_admin_is_attributed_to_sender_chat(username):
    anonymous_bot = User(
        1087968824, "Group", True, last_name="Anonymous Bot", username="GroupAnonymousBot"
    )
    sender_chat = Chat(
        -100123456, "supergroup", title="Команда 🦊", username=username
    )
    update = _make_update("Объявление", user=anonymous_bot, sender_chat=sender_chat)
    context, max_client = _make_context()

    await _on_topic_message(update, context)

    expected = "Сообщение из Telegram от Команда 🦊:\n\nОбъявление"
    max_client.send_message.assert_awaited_once_with(
        42,
        expected,
        elements=[_sender_link(expected, "Команда 🦊", username)] if username else [],
    )


async def test_media_from_channel_uses_channel_title_and_link():
    sender_chat = Chat(-100123456, "channel", title="Новости", username="news_channel")
    update = _make_update(text=None, sender_chat=sender_chat)
    _set_media(update.message, "document", None)
    context, max_client = _make_context()

    await _on_topic_media(update, context)

    expected = "Сообщение из Telegram от Новости:"
    max_client.send_message.assert_awaited_once_with(
        42,
        text=expected,
        elements=[_sender_link(expected, "Новости", "news_channel")],
        attaches=[max_client.upload_file.return_value],
    )


@pytest.mark.parametrize("media", [False, True], ids=["text", "photo"])
async def test_missing_sender_has_readable_fallback(media):
    update = _make_update("Привет")
    context, max_client = _make_context()
    if media:
        update.message.text = None
        _set_media(update.message, "photo", None)
        await _on_topic_media(update, context)
        expected = "Сообщение из Telegram от неизвестного отправителя:"
        max_client.send_message.assert_awaited_once_with(
            42,
            text=expected,
            elements=[],
            attaches=[max_client.upload_photo.return_value],
        )
    else:
        await _on_topic_message(update, context)
        max_client.send_message.assert_awaited_once_with(
            42,
            "Сообщение из Telegram от неизвестного отправителя:\n\nПривет",
            elements=[],
        )


@pytest.mark.parametrize("sender_prefix", [None, False, True], ids=["default", "false", "true"])
def test_build_tg_app_wires_sender_prefix(sender_prefix):
    kwargs = {} if sender_prefix is None else {"sender_prefix": sender_prefix}

    app = build_tg_app(
        "123456:AAABBBCCC", MagicMock(), "-100123456", MagicMock(), **kwargs
    )

    assert app.bot_data[SENDER_PREFIX_KEY] is (sender_prefix is True)
