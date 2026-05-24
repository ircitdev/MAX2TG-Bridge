import asyncio
import io
import logging
import re
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import MessageEntityType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app.max_client import MaxClient
from app.topics import TopicStore

log = logging.getLogger(__name__)

MAX_CLIENT_KEY = "max_client"
TOPIC_STORE_KEY = "topic_store"
ALLOWED_USER_KEY = "allowed_user_id"
SUPERGROUP_KEY = "supergroup_id"

_MAX_URL_RE = re.compile(r"https?://(?:web\.)?max\.ru/(-?\d+)")

# Telegram entity type → MAX element type. The MAX names match what the
# existing codebase used (STRONG) and what MAX renders for the formatting
# styles surfaced in its UI (bold/italic/strike/underline/code).
_TG_TO_MAX_ELEMENT_TYPE = {
    MessageEntityType.BOLD: "STRONG",
    MessageEntityType.ITALIC: "EMPHASIZED",
    MessageEntityType.STRIKETHROUGH: "STRIKETHROUGH",
    MessageEntityType.UNDERLINE: "UNDERLINE",
    MessageEntityType.CODE: "MONOSPACED",
    # MAX has no boxed code-block style, so multi-line code from Telegram and
    # real Telegram blockquotes both land as MAX's quote style.
    MessageEntityType.PRE: "BLOCKQUOTE",
    MessageEntityType.BLOCKQUOTE: "BLOCKQUOTE",
    MessageEntityType.EXPANDABLE_BLOCKQUOTE: "BLOCKQUOTE",
}


def _utf16_to_char_offset(text: str, utf16_offset: int) -> int:
    """Convert a UTF-16 code-units offset (what Telegram uses for entity
    positions) into a Python codepoint index (which MAX appears to use)."""
    if utf16_offset <= 0 or not text:
        return 0
    encoded = text.encode("utf-16-le")
    truncated = encoded[: utf16_offset * 2]
    return len(truncated.decode("utf-16-le", errors="ignore"))


def _entities_to_max_elements(text: str, entities) -> list:
    """Map Telegram message entities to MAX `elements` descriptors so basic
    inline formatting (bold/italic/strike/underline/code/link) survives the
    Telegram → MAX bridge."""
    if not entities:
        return []
    elements: list[dict] = []
    for e in entities:
        start = _utf16_to_char_offset(text, e.offset)
        end = _utf16_to_char_offset(text, e.offset + e.length)
        length = end - start
        if length <= 0:
            continue
        max_type = _TG_TO_MAX_ELEMENT_TYPE.get(e.type)
        if max_type:
            elements.append({"type": max_type, "from": start, "length": length})
        elif e.type == MessageEntityType.TEXT_LINK and getattr(e, "url", None):
            elements.append({
                "type": "LINK",
                "from": start,
                "length": length,
                "attributes": {"url": e.url},
            })
    return elements


def _parse_max_chat_id(s: str) -> int | None:
    """Accept either a raw chat id or a web.max.ru URL."""
    s = s.strip()
    try:
        return int(s)
    except ValueError:
        pass
    m = _MAX_URL_RE.match(s)
    if m:
        return int(m.group(1))
    return None


def _peer_id_in_dm(resolver, chat_id) -> int | None:
    """Return the other participant of a DIALOG chat (i.e., not us)."""
    chat = resolver.chats_raw.get(chat_id) or {}
    my_id = resolver.my_id
    for uid_str in chat.get("participants") or {}:
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        if uid != my_id:
            return uid
    return None


def _resolve_topic_target(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Common prelude for topic handlers. Returns (message, max_chat_id, max_client)
    if the message should be routed, or None to drop silently."""
    message = update.message
    if message is None:
        return None
    thread_id = message.message_thread_id
    if thread_id is None or not message.is_topic_message:
        return None
    topic_store: TopicStore | None = context.bot_data.get(TOPIC_STORE_KEY)
    max_chat_id = topic_store.chat_for_topic(thread_id) if topic_store else None
    if max_chat_id is None:
        return None
    allowed_user_id = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_id and update.effective_user and update.effective_user.id != allowed_user_id:
        return None
    max_client: MaxClient | None = context.bot_data.get(MAX_CLIENT_KEY)
    return message, max_chat_id, max_client


async def _surface_send_result(message, resp) -> None:
    """Translate a Max send_message response into a Telegram reaction or warning."""
    err = (resp or {}).get("_max_error")
    if err:
        desc = (err.get("localizedMessage") or err.get("message")
                or err.get("error") or "не удалось отправить сообщение")
        await message.reply_text(f"⚠️ MAX: {desc}")
        return
    if not resp:
        await message.reply_text("⚠️ Таймаут от MAX — сообщение не подтверждено.")
        return
    try:
        await message.set_reaction("👀")
    except Exception:
        log.debug("Could not set reaction on confirmed message", exc_info=True)


async def _on_topic_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a text message typed in a forum topic back to the matching Max chat."""
    target = _resolve_topic_target(update, context)
    if not target:
        return
    message, max_chat_id, max_client = target
    if not message.text:
        return

    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return

    elements = _entities_to_max_elements(message.text, message.entities)
    try:
        resp = await max_client.send_message(max_chat_id, message.text,
                                              elements=elements)
    except Exception:
        log.exception("Failed to send reply to Max chat %s", max_chat_id)
        await message.reply_text("⚠️ Ошибка при отправке в Max.")
        return

    await _surface_send_result(message, resp)


async def _download_tg_file(file_obj) -> bytes | None:
    """Pull bytes from a Telegram File object via the Bot API."""
    try:
        tg_file = await file_obj.get_file()
        return bytes(await tg_file.download_as_bytearray())
    except Exception:
        log.exception("Failed to download Telegram file")
        return None


async def _on_topic_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a media message (photo / voice / document / audio / video) from a
    forum topic to the matching Max chat. Caption, if any, becomes the
    accompanying text."""
    target = _resolve_topic_target(update, context)
    if not target:
        return
    message, max_chat_id, max_client = target

    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return

    caption = message.caption or ""

    # ── pick the right uploader for the attached media ────────────
    attach = None
    if message.photo:
        # message.photo is a list of progressively larger PhotoSize objects;
        # the last one is the highest resolution.
        photo = message.photo[-1]
        data = await _download_tg_file(photo)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать фото из Telegram.")
            return
        attach = await max_client.upload_photo(data, chat_id=max_chat_id)

    elif message.voice:
        data = await _download_tg_file(message.voice)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать голосовое из Telegram.")
            return
        attach = await max_client.upload_audio(
            data, chat_id=max_chat_id,
            filename="voice.ogg",
            mimetype="audio/ogg",
        )

    elif message.audio:
        data = await _download_tg_file(message.audio)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать аудио из Telegram.")
            return
        attach = await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=message.audio.file_name or "audio",
            mimetype=message.audio.mime_type or "audio/mpeg",
        )

    elif message.document:
        data = await _download_tg_file(message.document)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать файл из Telegram.")
            return
        attach = await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=message.document.file_name or "file",
            mimetype=message.document.mime_type or "application/octet-stream",
        )

    elif message.video:
        data = await _download_tg_file(message.video)
        if data is None:
            await message.reply_text("⚠️ Не удалось скачать видео из Telegram.")
            return
        attach = await max_client.upload_file(
            data, chat_id=max_chat_id,
            filename=message.video.file_name or "video.mp4",
            mimetype=message.video.mime_type or "video/mp4",
        )

    else:
        return  # unsupported media kind

    if not attach:
        await message.reply_text("⚠️ Не удалось загрузить файл в MAX.")
        return

    elements = _entities_to_max_elements(caption, message.caption_entities)
    try:
        resp = await max_client.send_message(max_chat_id, text=caption,
                                              elements=elements,
                                              attaches=[attach])
    except Exception:
        log.exception("Failed to send media reply to Max chat %s", max_chat_id)
        await message.reply_text("⚠️ Ошибка при отправке в Max.")
        return

    await _surface_send_result(message, resp)


async def post_topic_intro(bot, supergroup_id, max_client: MaxClient,
                            max_chat_id, thread_id: int, *,
                            pin: bool = True) -> None:
    """Publish a profile/info card as the first message of a topic, then pin
    it. Called when a topic is freshly created (either auto on first
    incoming message or manually via /bind)."""
    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        return

    is_dm = resolver.is_dm(max_chat_id)

    if is_dm:
        peer_id = _peer_id_in_dm(resolver, max_chat_id)
        if peer_id is None:
            return
        contact = resolver.contacts_raw.get(peer_id)
        if contact is None:
            try:
                await max_client.fetch_contacts([peer_id])
            except Exception:
                log.exception("post_topic_intro: fetch_contacts failed")
            contact = resolver.contacts_raw.get(peer_id)

        name = resolver.user_name(peer_id)
        phone = (contact or {}).get("phone") or ""
        about = ((contact or {}).get("description")
                 or (contact or {}).get("about")
                 or (contact or {}).get("status") or "")
        username = (contact or {}).get("link") or (contact or {}).get("username") or ""

        lines = [f"<b>{escape(str(name))}</b>",
                 f"id: <code>{peer_id}</code>"]
        if phone:
            lines.append(f"📞 <code>{escape(str(phone))}</code>")
        if username:
            lines.append(f"🔗 {escape(str(username))}")
        if about:
            lines.append(f"\n{escape(str(about))}")
        body = "\n".join(lines)

        photo_url = None
        photo_obj = (contact or {}).get("photo") or (contact or {}).get("avatar")
        if isinstance(photo_obj, dict):
            photo_url = (photo_obj.get("baseUrl") or photo_obj.get("url")
                         or photo_obj.get("photoUrl"))
        photo_url = (photo_url
                     or (contact or {}).get("baseUrl")
                     or (contact or {}).get("baseRawUrl")
                     or (contact or {}).get("photoUrl")
                     or (contact or {}).get("baseRawIconUrl"))
    else:
        chat = resolver.chats_raw.get(max_chat_id) or {}
        title = chat.get("title") or resolver.chat_name(max_chat_id)
        ctype = chat.get("type") or "?"
        participants = chat.get("participants") or {}
        descr = chat.get("description") or ""
        link = chat.get("link") or ""

        lines = [f"<b>{escape(str(title))}</b> · {escape(str(ctype))}",
                 f"id: <code>{max_chat_id}</code>",
                 f"Участников: <b>{len(participants)}</b>"]
        if descr:
            lines.append(f"\n{escape(str(descr))}")
        if link:
            lines.append(f"\n🔗 {escape(str(link))}")
        body = "\n".join(lines)
        photo_url = chat.get("baseRawIconUrl") or chat.get("baseUrl")

    sent = None
    if photo_url:
        data = await max_client.download_file(photo_url)
        if data:
            try:
                sent = await bot.send_photo(
                    chat_id=int(supergroup_id),
                    photo=InputFile(io.BytesIO(data), filename="profile.jpg"),
                    caption=body, parse_mode="HTML",
                    message_thread_id=thread_id,
                )
            except Exception:
                log.exception("post_topic_intro: send_photo failed")
                sent = None

    if sent is None:
        try:
            sent = await bot.send_message(
                chat_id=int(supergroup_id), text=body, parse_mode="HTML",
                message_thread_id=thread_id,
            )
        except Exception:
            log.exception("post_topic_intro: send_message failed")
            return

    if pin and sent is not None:
        try:
            await bot.pin_chat_message(
                chat_id=int(supergroup_id),
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception:
            log.exception("post_topic_intro: pin_chat_message failed")


async def _cmd_bind(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a forum topic bound to a specific Max chat id.

    Usage: `/bind <chat_id-or-url> [optional-title]` — typed anywhere in the
    supergroup. The bot creates a new forum topic (or reports an existing
    binding) and stores the mapping so messages typed there get forwarded
    to the Max chat.
    """
    message = update.message
    if message is None:
        return

    allowed_user_id = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_id and update.effective_user and update.effective_user.id != allowed_user_id:
        return

    args = context.args or []
    if not args:
        await message.reply_text(
            "Использование: <code>/bind &lt;chat_id или https://web.max.ru/-...&gt; "
            "[название]</code>",
            parse_mode="HTML",
        )
        return

    max_chat_id = _parse_max_chat_id(args[0])
    if max_chat_id is None:
        await message.reply_text(
            "Не понял chat_id. Пример: <code>/bind -75107924425434</code>",
            parse_mode="HTML",
        )
        return

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    existing = topic_store.get_topic(max_chat_id)
    if existing is not None:
        await message.reply_text(
            f"Этот чат MAX уже привязан к топику (thread_id=<code>{existing}</code>).",
            parse_mode="HTML",
        )
        return

    max_client: MaxClient = context.bot_data[MAX_CLIENT_KEY]
    resolver = getattr(max_client, "resolver", None)

    # Build a topic title: explicit second arg → known chat title → chat id.
    if len(args) > 1:
        title = " ".join(args[1:]).strip()
    elif resolver and resolver.chat_name(max_chat_id) != str(max_chat_id):
        title = resolver.chat_name(max_chat_id)
    else:
        title = str(max_chat_id)
    title = title[:128]

    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    try:
        topic = await context.bot.create_forum_topic(
            chat_id=int(supergroup_id), name=title,
        )
    except Exception as exc:
        log.exception("Failed to create forum topic for %s", max_chat_id)
        await message.reply_text(f"Не удалось создать топик: {exc}")
        return

    thread_id = topic.message_thread_id
    topic_store.set_topic(max_chat_id, thread_id, title)
    await message.reply_text(
        f"Готово: <b>{escape(title)}</b> ↔ MAX <code>{max_chat_id}</code> "
        f"(thread_id=<code>{thread_id}</code>). Пиши в новом топике — улетит в MAX.",
        parse_mode="HTML",
    )
    # Post & pin a profile card in the freshly-created topic.
    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    asyncio.create_task(
        post_topic_intro(context.bot, supergroup_id, max_client,
                          max_chat_id, thread_id)
    )


_MAX_LINK_RE = re.compile(r"https?://max\.ru/[A-Za-z0-9_\-/]+")


def _extract_chat_id_from_open(resp: dict) -> int | None:
    """Pick a chat id out of the various shapes opcode 57 returns."""
    if not isinstance(resp, dict):
        return None
    # Direct fields seen in practice.
    for key in ("chatId", "conversationId"):
        v = resp.get(key)
        if isinstance(v, int):
            return v
        if isinstance(v, str):
            try:
                return int(v)
            except ValueError:
                pass
    # Nested chat object.
    chat = resp.get("chat")
    if isinstance(chat, dict):
        cid = chat.get("id")
        if isinstance(cid, int):
            return cid
        if isinstance(cid, str):
            try:
                return int(cid)
            except ValueError:
                pass
    return None


async def _cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open a max.ru link (user or chat invite) and bind it to a new topic.

    Usage: `/add https://max.ru/u/<token>` or `/add https://max.ru/join/<token>`.
    """
    message = update.message
    if message is None:
        return

    allowed_user_id = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_id and update.effective_user and update.effective_user.id != allowed_user_id:
        return

    args = context.args or []
    link = args[0] if args else ""
    # Try to extract a max.ru link from anywhere in the message text too,
    # so `/add` works if the link was just pasted alongside the command.
    if not link.startswith(("http://", "https://")) and message.text:
        m = _MAX_LINK_RE.search(message.text)
        if m:
            link = m.group(0)

    if not link.startswith(("http://", "https://")) or "max.ru/" not in link:
        await message.reply_text(
            "Использование: <code>/add https://max.ru/u/...</code> или "
            "<code>/add https://max.ru/join/...</code>",
            parse_mode="HTML",
        )
        return

    max_client: MaxClient = context.bot_data[MAX_CLIENT_KEY]
    try:
        resp = await max_client.open_by_link(link)
    except Exception as exc:
        log.exception("open_by_link failed")
        await message.reply_text(f"⚠️ Ошибка при обращении к MAX: {exc}")
        return

    err = (resp or {}).get("_max_error")
    if err:
        desc = (err.get("localizedMessage") or err.get("message")
                or err.get("error") or "MAX отказал")
        await message.reply_text(f"⚠️ MAX: {desc}")
        return
    if not resp:
        await message.reply_text("⚠️ Таймаут от MAX, ссылка не открылась.")
        return

    chat_id = _extract_chat_id_from_open(resp)
    if chat_id is None:
        log.warning("/add: cannot extract chat_id from response: %s", resp)
        await message.reply_text(
            "MAX принял ссылку, но не вернул chat_id, который я понимаю. "
            "Лог: <code>" + escape(str(resp)[:300]) + "</code>",
            parse_mode="HTML",
        )
        return

    # Let the resolver pick up the freshly arrived chat metadata, if any.
    resolver = getattr(max_client, "resolver", None)
    if resolver is not None and isinstance(resp.get("chat"), dict):
        chat_obj = resp["chat"]
        resolver.chats_raw[chat_id] = chat_obj
        if chat_obj.get("type"):
            resolver.chat_types[chat_id] = chat_obj["type"]
        if chat_obj.get("title"):
            resolver.chats[chat_id] = chat_obj["title"]

    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]
    existing = topic_store.get_topic(chat_id)
    if existing is not None:
        await message.reply_text(
            f"Этот чат MAX уже привязан к топику (thread_id=<code>{existing}</code>).",
            parse_mode="HTML",
        )
        return

    # Pick a title — prefer chat title or peer name from resolver.
    title = None
    if resolver is not None:
        title = resolver.chat_name(chat_id)
        if title == str(chat_id) and resolver.is_dm(chat_id):
            peer_id = _peer_id_in_dm(resolver, chat_id)
            if peer_id is not None:
                # Best-effort fetch contact name now.
                try:
                    await max_client.fetch_contacts([peer_id])
                except Exception:
                    pass
                title = resolver.user_name(peer_id)
    if not title or title == str(chat_id):
        title = str(chat_id)
    title = title[:128]

    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    try:
        topic = await context.bot.create_forum_topic(
            chat_id=int(supergroup_id), name=title,
        )
    except Exception as exc:
        log.exception("/add: create_forum_topic failed")
        await message.reply_text(f"Не удалось создать топик: {exc}")
        return

    thread_id = topic.message_thread_id
    topic_store.set_topic(chat_id, thread_id, title)
    await message.reply_text(
        f"Готово: <b>{escape(title)}</b> ↔ MAX <code>{chat_id}</code> "
        f"(thread_id=<code>{thread_id}</code>).",
        parse_mode="HTML",
    )
    asyncio.create_task(
        post_topic_intro(context.bot, supergroup_id, max_client,
                          chat_id, thread_id)
    )


HELP_TEXT = (
    "<b>max2tg — мост MAX ↔ Telegram</b>\n\n"
    "Команды в супергруппе:\n"
    "• <code>/bind &lt;chat_id или URL&gt; [название]</code> — привязать "
    "новый топик к чату MAX.\n"
    "• <code>/add &lt;https://max.ru/join/...&gt;</code> — открыть "
    "групповую/канальную ссылку MAX, создать топик и поставить карточку.\n"
    "• <code>/profile</code> — внутри топика: показать профиль собеседника "
    "из MAX (имя, id, аватар).\n"
    "• <code>/intro</code> — перепостить и закрепить карточку профиля "
    "в текущем топике (полезно после смены аватара).\n"
    "• <code>/del</code> — удалить текущий топик и связь с MAX-чатом "
    "(спросит подтверждение).\n"
    "• <code>/help</code> — эта справка.\n\n"
    "Просто пиши в любом привязанном топике — сообщение уйдёт в "
    "соответствующий чат MAX. Поддерживается жирный/курсив/зачёркнутый/"
    "подчёркнутый текст, моноширинный код, цитаты и ссылки. Фото, "
    "документы и видео тоже передаются. Голосовые приходят как .ogg "
    "файл (пока MAX не вернул нам опкод нативной загрузки).\n\n"
    "Если кто-то новый пишет тебе в MAX — топик создастся автоматически "
    "и в нём сразу появится карточка собеседника."
)


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    await message.reply_text(HELP_TEXT, parse_mode="HTML",
                              disable_web_page_preview=True)


async def _cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask the user to confirm deletion of the current topic. The actual
    deletion happens in ``_on_del_callback`` when the inline button is
    pressed."""
    message = update.message
    if message is None:
        return

    allowed_user_id = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_id and update.effective_user and update.effective_user.id != allowed_user_id:
        return

    target = _resolve_topic_target(update, context)
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с MAX-чатом."
        )
        return
    _, max_chat_id, _ = target
    thread_id = message.message_thread_id

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🗑 Удалить топик",
                                 callback_data=f"del:ok:{thread_id}:{max_chat_id}"),
            InlineKeyboardButton("Отмена", callback_data="del:cancel"),
        ]
    ])
    await message.reply_text(
        "Удалить этот топик вместе со всеми сообщениями и снять связь "
        f"с MAX-чатом <code>{max_chat_id}</code>?\n\n"
        "Восстановить нельзя. Новый топик создастся, если собеседник снова "
        "тебе напишет.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def _on_del_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()

    allowed_user_id = context.bot_data.get(ALLOWED_USER_KEY)
    if allowed_user_id and update.effective_user and update.effective_user.id != allowed_user_id:
        return

    parts = query.data.split(":")
    if parts[:2] == ["del", "cancel"]:
        try:
            await query.edit_message_text("Отменено.")
        except Exception:
            pass
        return

    if len(parts) != 4 or parts[0] != "del" or parts[1] != "ok":
        return
    try:
        thread_id = int(parts[2])
    except ValueError:
        return
    try:
        max_chat_id: int | str = int(parts[3])
    except ValueError:
        max_chat_id = parts[3]

    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    topic_store: TopicStore = context.bot_data[TOPIC_STORE_KEY]

    # Remove mapping first — even if delete_forum_topic fails the stale link
    # is gone, and a fresh topic can be made via /bind.
    topic_store.remove(max_chat_id)

    try:
        await context.bot.delete_forum_topic(
            chat_id=int(supergroup_id), message_thread_id=thread_id,
        )
    except Exception as exc:
        log.exception("/del: delete_forum_topic failed")
        try:
            await query.edit_message_text(
                f"⚠️ Связь снята, но удалить топик не получилось: {exc}"
            )
        except Exception:
            pass
        return

    log.info("/del: removed topic thread=%s for max_chat_id=%s",
             thread_id, max_chat_id)
    # The edit_message_text below will fail if the topic is already gone;
    # that's fine — the chat-level confirmation isn't critical.


async def _cmd_intro(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-publish the pinned profile card in the current topic.

    Useful for topics that were created before this feature existed, or to
    refresh the card after the contact updated their photo/name.
    """
    target = _resolve_topic_target(update, context)
    message = update.message
    if message is None:
        return
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с чатом MAX."
        )
        return
    _, max_chat_id, max_client = target
    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return
    supergroup_id = context.bot_data[SUPERGROUP_KEY]
    await post_topic_intro(
        context.bot, supergroup_id, max_client, max_chat_id,
        message.message_thread_id,
    )


async def _cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show profile of the Max peer linked to the current topic."""
    target = _resolve_topic_target(update, context)
    message = update.message
    if message is None:
        return
    if not target:
        await message.reply_text(
            "Команда работает только внутри топика, связанного с чатом MAX."
        )
        return
    _, max_chat_id, max_client = target
    if not max_client:
        await message.reply_text("⚠️ Max клиент не подключён.")
        return

    resolver = getattr(max_client, "resolver", None)
    if resolver is None:
        await message.reply_text("⚠️ Кеш контактов недоступен.")
        return

    is_dm = resolver.is_dm(max_chat_id)
    if not is_dm:
        # Group / channel: show whatever the snapshot exposes.
        chat = resolver.chats_raw.get(max_chat_id) or {}
        title = chat.get("title") or resolver.chat_name(max_chat_id)
        ctype = chat.get("type") or "?"
        participants = chat.get("participants") or {}
        descr = chat.get("description") or ""
        link = chat.get("link") or ""
        parts = [f"<b>{escape(str(title))}</b> · {escape(str(ctype))}"]
        parts.append(f"Участников: <b>{len(participants)}</b>")
        if descr:
            parts.append(f"\n{escape(str(descr))}")
        if link:
            parts.append(f"\n🔗 {escape(str(link))}")
        await message.reply_text("\n".join(parts), parse_mode="HTML")
        return

    peer_id = _peer_id_in_dm(resolver, max_chat_id)
    if peer_id is None:
        await message.reply_text("Не нашёл собеседника в этом чате.")
        return

    contact = resolver.contacts_raw.get(peer_id)
    if contact is None:
        # Probe several payload shapes / opcodes so we can see in the log
        # what MAX is willing to return for a peer that's not in contacts.
        probes = [
            (32, {"contactIds": [peer_id]}),
            (32, {"contactIds": [str(peer_id)]}),
            (32, {"userIds": [peer_id]}),
            (32, {"userId": peer_id}),
            (35, {"contactIds": [peer_id]}),   # CONTACT_PRESENCE
            (33, {"contactIds": [peer_id]}),
            (36, {"contactIds": [peer_id]}),
        ]
        for op, payload in probes:
            try:
                resp = await max_client.cmd(op, payload)
            except Exception:
                log.exception("/profile probe op=%d failed", op)
                continue
            log.info("/profile probe op=%d payload=%s → %s",
                     op, payload, str(resp)[:600])
            if resp and "_max_error" not in resp:
                # Let the resolver opportunistically pick up new fields.
                resolver._parse_contacts_response(resp)
                if peer_id in resolver.contacts_raw:
                    break
        contact = resolver.contacts_raw.get(peer_id)

    if contact is None:
        # MAX won't share extended profile for users that aren't in your
        # contact list. Show whatever we already know.
        name = resolver.users.get(peer_id)
        if name:
            await message.reply_text(
                f"<b>{escape(str(name))}</b>\nid: <code>{peer_id}</code>\n\n"
                "<i>MAX не отдал расширенный профиль для этого собеседника "
                "(скорее всего, он не у тебя в контактах).</i>",
                parse_mode="HTML",
            )
            return
        await message.reply_text(
            f"Не удалось получить профиль из MAX. id: <code>{peer_id}</code>",
            parse_mode="HTML",
        )
        return

    log.info("/profile contact raw fields: %s", list(contact.keys()))

    name = resolver.user_name(peer_id)
    phone = contact.get("phone") or ""
    about = (contact.get("description") or contact.get("about")
             or contact.get("status") or "")
    username = contact.get("link") or contact.get("username") or ""

    lines = [f"<b>{escape(str(name))}</b>",
             f"id: <code>{peer_id}</code>"]
    if phone:
        lines.append(f"📞 <code>{escape(str(phone))}</code>")
    if username:
        lines.append(f"🔗 {escape(str(username))}")
    if about:
        lines.append(f"\n{escape(str(about))}")
    body = "\n".join(lines)

    # Find a photo if any. MAX puts the avatar URL at the top level of the
    # contact dict as `baseUrl` / `baseRawUrl`, sometimes also wrapped in a
    # nested photo/avatar dict.
    photo_url = None
    photo_obj = contact.get("photo") or contact.get("avatar")
    if isinstance(photo_obj, dict):
        photo_url = (photo_obj.get("baseUrl") or photo_obj.get("url")
                     or photo_obj.get("photoUrl"))
    photo_url = (photo_url
                 or contact.get("baseUrl")
                 or contact.get("baseRawUrl")
                 or contact.get("photoUrl")
                 or contact.get("baseRawIconUrl"))

    if photo_url:
        data = await max_client.download_file(photo_url)
        if data:
            try:
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=data,
                    caption=body,
                    parse_mode="HTML",
                    message_thread_id=message.message_thread_id,
                )
                return
            except Exception:
                log.exception("send_photo failed in /profile")

    await message.reply_text(body, parse_mode="HTML")


def build_tg_app(token: str, max_client: MaxClient, supergroup_id: str,
                 topic_store: TopicStore, allowed_user_id: int | None = None,
                 proxy_url: str | None = None) -> Application:
    """Build the Telegram Application that routes topic replies back to Max."""
    builder = Application.builder().token(token)
    if proxy_url:
        builder = builder.proxy(proxy_url).get_updates_proxy(proxy_url)
    app = builder.build()
    app.bot_data[MAX_CLIENT_KEY] = max_client
    app.bot_data[TOPIC_STORE_KEY] = topic_store
    app.bot_data[ALLOWED_USER_KEY] = int(allowed_user_id) if allowed_user_id else None
    app.bot_data[SUPERGROUP_KEY] = int(supergroup_id)

    chat_filter = filters.Chat(chat_id=int(supergroup_id))
    app.add_handler(CommandHandler("bind", _cmd_bind, filters=chat_filter))
    app.add_handler(CommandHandler("add", _cmd_add, filters=chat_filter))
    app.add_handler(CommandHandler("profile", _cmd_profile, filters=chat_filter))
    app.add_handler(CommandHandler("intro", _cmd_intro, filters=chat_filter))
    app.add_handler(CommandHandler("del", _cmd_del, filters=chat_filter))
    app.add_handler(CommandHandler("help", _cmd_help, filters=chat_filter))
    app.add_handler(CallbackQueryHandler(_on_del_callback, pattern=r"^del:"))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND & chat_filter, _on_topic_message)
    )
    media_filter = (
        filters.PHOTO | filters.VOICE | filters.AUDIO
        | filters.Document.ALL | filters.VIDEO
    )
    app.add_handler(
        MessageHandler(media_filter & chat_filter, _on_topic_media)
    )

    return app
