import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

from app.max_client import MaxClient, MaxMessage
from app.resolver import ContactResolver
from app.tg_sender import TelegramSender

log = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

# MAX delivers one album as several DISPATCH frames arriving a few hundred
# milliseconds apart. Wait this long for more parts before posting to Telegram…
ALBUM_WINDOW_SEC = 2.0
# …but never hold media longer than this after the first item, so a steady
# trickle of photos still gets posted.
ALBUM_MAX_WAIT_SEC = 15.0


@dataclass
class _AlbumBucket:
    thread_id: int | None
    caption: str
    deadline: float
    items: list = field(default_factory=list)
    timer: asyncio.Task | None = None


class _AlbumBuffer:
    """Collects groupable media so one logical album becomes one Telegram post.

    A forwarded album reaches us as several separate MAX messages, and a single
    message can itself carry several photos. Sent one at a time they land in
    Telegram as a wall of individual posts. Media is therefore buffered per
    (chat, sender) and flushed once nothing new has arrived for ``window``
    seconds, or ``max_wait`` seconds after the first item if parts keep coming.
    """

    def __init__(self, sender, window: float = ALBUM_WINDOW_SEC,
                 max_wait: float = ALBUM_MAX_WAIT_SEC):
        self._sender = sender
        self._window = window
        self._max_wait = max_wait
        self._buckets: dict = {}
        self._lock = asyncio.Lock()

    async def add(self, key, thread_id, caption: str, items: list) -> None:
        if not items:
            return

        # A second, different caption means a second post — flush rather than
        # silently drop the text that came with it.
        async with self._lock:
            bucket = self._buckets.get(key)
            caption_clash = (
                bucket is not None and caption
                and bucket.caption and caption != bucket.caption
            )
        if caption_clash:
            await self._flush(key, from_timer=False)

        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _AlbumBucket(
                    thread_id=thread_id,
                    caption=caption,
                    deadline=time.monotonic() + self._max_wait,
                )
                self._buckets[key] = bucket
            else:
                if not bucket.caption and caption:
                    bucket.caption = caption
                # The first part of an album can arrive while ensure_topic is
                # still failing, leaving thread_id None. Adopt a real one as
                # soon as any later part carries it, or the whole album lands
                # in General instead of the chat's topic.
                if bucket.thread_id is None and thread_id is not None:
                    bucket.thread_id = thread_id
            bucket.items.extend(items)
            if bucket.timer is not None:
                bucket.timer.cancel()
            delay = max(0.0, min(self._window, bucket.deadline - time.monotonic()))
            bucket.timer = asyncio.create_task(self._flush_after(key, delay))

    async def _flush_after(self, key, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        await self._flush(key, from_timer=True)

    async def flush(self, key) -> None:
        """Post whatever is buffered for ``key`` right now.

        Called before anything that must not overtake the album — a document,
        a sticker, a plain-text message.
        """
        await self._flush(key, from_timer=False)

    async def _flush(self, key, from_timer: bool) -> None:
        async with self._lock:
            bucket = self._buckets.pop(key, None)
            if bucket is None:
                return
            # Cancelling from inside the timer task would cancel ourselves.
            if bucket.timer is not None and not from_timer:
                bucket.timer.cancel()
        try:
            await self._sender.send_media_group(
                bucket.items,
                caption=bucket.caption,
                message_thread_id=bucket.thread_id,
            )
            log.info("Posted album of %d item(s) for %s", len(bucket.items), key)
        except Exception:
            log.exception("Failed to post album for %s", key)


async def _groupable_item(attach: dict, client) -> tuple[str, bytes, str] | None:
    """Download an attachment if it can ride inside a Telegram album.

    Only real photos and image/video files qualify. ``_type=VIDEO`` is left out
    on purpose: MAX gives us just a thumbnail for those, and quietly dropping it
    into an album would hide that the original is a video.
    """
    atype = attach.get("_type")

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            return None
        data = await client.download_file(url)
        return ("photo", data, "photo.jpg") if data else None

    if atype == "FILE":
        name = attach.get("name", "file")
        kind = _guess_media_kind(name)
        if kind not in ("photo", "video"):
            return None
        url = _extract_file_url(attach)
        if not url:
            return None
        data = await client.download_file(url)
        return (kind, data, name) if data else None

    return None


def _header(msg: MaxMessage, sender_label: str, chat_label: str, is_dm: bool) -> str:
    if is_dm:
        return f"✉ <b>{sender_label}</b>"
    return f"💬 <b>{chat_label}</b> | {sender_label}"


def _extract_photo_url(attach: dict) -> str | None:
    """Extract the best available URL for a PHOTO attachment."""
    return attach.get("baseUrl") or attach.get("url")


def _extract_file_url(attach: dict) -> str | None:
    """Extract download URL for a FILE attachment (url field takes priority)."""
    url = attach.get("url")
    if url and url.startswith("http"):
        return url
    return None


def _guess_media_kind(filename: str) -> str:
    name_lower = filename.lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "document"



async def _flush_album(albums, key) -> None:
    if albums is not None:
        await albums.flush(key)


async def _send_attaches(attaches: list, client, sender, header_text: str,
                         body_text: str, thread_id, msg, albums, key) -> bool:
    """Route one message's attachments to Telegram.

    Attachments are walked in order. Album-capable media accumulates in the
    buffer (so parts of one album arriving as separate MAX frames end up as a
    single Telegram post); anything else flushes that run first and is sent on
    its own, which keeps mixed attachments in the order MAX sent them.

    Returns True when ``body_text`` was already used as a caption.
    """
    caption = f"{header_text}\n{escape(body_text)}" if body_text else header_text

    text_used = False
    for attach in attaches:
        cap = header_text if text_used else caption
        item = await _groupable_item(attach, client)

        if item is not None:
            if albums is not None:
                await albums.add(key, thread_id, cap, [item])
            else:
                await sender.send_media_group([item], caption=cap,
                                              message_thread_id=thread_id)
            log.info("Buffered album item → TG")
        else:
            # Anything that cannot ride in an album ends the current run, so
            # the attachments keep the order MAX sent them in.
            await _flush_album(albums, key)
            await _send_attach(attach, client, sender, cap, thread_id=thread_id, msg=msg)
            log.info("Forwarded attach _type=%s → TG", attach.get("_type"))

        text_used = text_used or bool(body_text)

    return text_used


async def _send_attach(
    attach: dict,
    client: MaxClient,
    sender: TelegramSender,
    header_text: str,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
) -> bool:
    """Process and send a single attachment. Returns True if handled."""
    atype = attach.get("_type", "")
    log.info("Processing attach _type=%s keys=%s", atype, list(attach.keys()))

    if atype == "CONTROL" or atype == "WIDGET" or atype == "INLINE_KEYBOARD":
        return False

    # MAX's newer client sends voice messages with `_type=UNSUPPORTED` plus an
    # `audioId` + `token` (our ver=11 client doesn't speak its native AUDIO
    # variant). Treat this shape as audio.
    if atype == "UNSUPPORTED" and attach.get("audioId") is not None:
        audio_id = attach.get("audioId")
        token = attach.get("token")
        duration = attach.get("duration", 0)
        chat_id = msg.chat_id if msg else None
        message_id = msg.message_id if msg else None
        url = None
        if chat_id is not None and message_id:
            url = await client.download_audio_url(
                audio_id, chat_id, message_id, token=token,
            )
        if url:
            data = await client.download_file(url)
            if data:
                await sender.send_voice(data, caption=header_text,
                                         message_thread_id=thread_id)
                return True
        dur_s = f" ({duration // 1000}с)" if duration else ""
        await sender.send(
            f"{header_text}\n🎙 <i>[голосовое сообщение{dur_s} — не удалось скачать]</i>",
            message_thread_id=thread_id,
        )
        return True

    if atype == "PHOTO":
        url = _extract_photo_url(attach)
        if not url:
            log.warning("PHOTO attach has no URL: %s", attach)
            return False
        data = await client.download_file(url)
        if data:
            await sender.send_photo(data, caption=header_text, message_thread_id=thread_id)
            return True
        await sender.send(f"{header_text}\n<i>[фото — не удалось загрузить]</i>", message_thread_id=thread_id)
        return True

    if atype == "VIDEO":
        thumb = attach.get("thumbnail")
        if thumb:
            data = await client.download_file(thumb)
            if data:
                await sender.send_photo(data, caption=f"{header_text}\n<i>[видео — превью]</i>", message_thread_id=thread_id)
                return True
        await sender.send(f"{header_text}\n<i>[видео]</i>", message_thread_id=thread_id)
        return True

    if atype == "FILE":
        name = attach.get("name", "file")
        size = attach.get("size", 0)
        token_url = _extract_file_url(attach)
        if token_url:
            data = await client.download_file(token_url)
            if data:
                kind = _guess_media_kind(name)
                if kind == "photo":
                    await sender.send_photo(data, caption=header_text, filename=name, message_thread_id=thread_id)
                elif kind == "video":
                    await sender.send_video(data, caption=header_text, filename=name, message_thread_id=thread_id)
                else:
                    await sender.send_document(data, caption=header_text, filename=name, message_thread_id=thread_id)
                return True
        size_str = f" ({_human_size(size)})" if size else ""
        await sender.send(f"{header_text}\n📎 <b>{escape(name)}</b>{size_str}", message_thread_id=thread_id)
        return True

    if atype == "AUDIO":
        url = attach.get("url")
        if url:
            data = await client.download_file(url)
            if data:
                await sender.send_voice(data, caption=header_text, message_thread_id=thread_id)
                return True
        await sender.send(f"{header_text}\n<i>[аудио]</i>", message_thread_id=thread_id)
        return True

    if atype == "STICKER":
        url = attach.get("url")
        if url:
            data = await client.download_file(url)
            if data:
                await sender.send_sticker(data, message_thread_id=thread_id)
                return True
        await sender.send(f"{header_text}\n<i>[стикер]</i>", message_thread_id=thread_id)
        return True

    if atype == "SHARE":
        share_url = attach.get("url", "")
        title = attach.get("title", "")
        desc = attach.get("description", "")
        parts = [header_text]
        if title:
            parts.append(f"🔗 <b>{escape(title)}</b>")
        if share_url:
            parts.append(escape(share_url))
        if desc:
            parts.append(f"<i>{escape(desc[:200])}</i>")
        await sender.send("\n".join(parts), message_thread_id=thread_id)
        return True

    if atype == "LOCATION":
        lat = attach.get("lat") or attach.get("latitude")
        lon = attach.get("lon") or attach.get("lng") or attach.get("longitude")
        if lat and lon:
            await sender.send(f"{header_text}\n📍 {lat}, {lon}", message_thread_id=thread_id)
        else:
            await sender.send(f"{header_text}\n<i>[геолокация]</i>", message_thread_id=thread_id)
        return True

    if atype == "CONTACT":
        name = attach.get("name", "")
        phone = attach.get("phone", "")
        text = f"{header_text}\n👤 {escape(name)}"
        if phone:
            text += f" — {escape(phone)}"
        await sender.send(text, message_thread_id=thread_id)
        return True

    log.info("Unknown attach type %s, sending as info", atype)
    await sender.send(f"{header_text}\n<i>[вложение: {escape(atype or 'unknown')}]</i>", message_thread_id=thread_id)
    return True


async def _handle_linked_message(
    link: dict,
    link_type: str,
    header_text: str,
    client: MaxClient,
    sender: TelegramSender,
    resolver: ContactResolver,
    thread_id: int | None = None,
    msg: MaxMessage | None = None,
    albums=None,
    key=None,
) -> None:
    """Handle FORWARD or REPLY link inside a message."""
    inner = link.get("message") or link
    fwd_sender_id = inner.get("sender") or link.get("sender")
    fwd_text = inner.get("text", "") or link.get("text", "")
    fwd_attaches = inner.get("attaches") or link.get("attaches") or []

    fwd_sender_label = ""
    if fwd_sender_id:
        fwd_sender_label = escape(await resolver.resolve_user(fwd_sender_id))

    if link_type == "FORWARD":
        prefix = "↩️ <b>Переслано</b>"
        if fwd_sender_label:
            prefix = f"↩️ <b>Переслано от {fwd_sender_label}</b>"
    else:
        prefix = "↩ <b>Ответ</b>"
        if fwd_sender_label:
            prefix = f"↩ <b>Ответ на {fwd_sender_label}</b>"

    full_header = f"{header_text}\n{prefix}"

    fwd_meaningful = [
        a for a in fwd_attaches
        if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
    ]

    if fwd_meaningful:
        text_sent = await _send_attaches(
            fwd_meaningful, client, sender, full_header, fwd_text,
            thread_id, msg, albums, key,
        )
        if fwd_text and not text_sent:
            await _flush_album(albums, key)
            await sender.send(f"{full_header}\n{escape(fwd_text)}", message_thread_id=thread_id)
    elif fwd_text:
        await _flush_album(albums, key)
        await sender.send(f"{full_header}\n{escape(fwd_text)}", message_thread_id=thread_id)
    else:
        await _flush_album(albums, key)
        await sender.send(f"{full_header}\n<i>[без содержимого]</i>", message_thread_id=thread_id)


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def create_max_client(
    max_token: str, max_device_id: str, sender: TelegramSender, max_chat_ids: str | None = None,
    debug: bool = False,
) -> MaxClient:
    client = MaxClient(token=max_token, device_id=max_device_id, debug=debug, chat_ids=max_chat_ids)
    resolver = ContactResolver(client=client)
    albums = _AlbumBuffer(sender)
    # Expose for tg_handler commands like /profile.
    client.resolver = resolver

    _first_connect = True
    _notif_count = 0
    _last_notif_time: datetime | None = None

    def _can_notify() -> bool:
        if _last_notif_time is None:
            return True
        elapsed = (datetime.now() - _last_notif_time).total_seconds()
        if _notif_count == 1:
            return elapsed >= 3600    # 2-е: через 1 час
        if _notif_count == 2:
            return elapsed >= 10800   # 3-е: через 3 часа
        return elapsed >= 86400       # 4-е и далее: раз в сутки

    @client.on_ready
    async def handle_ready(snapshot: dict):
        nonlocal _first_connect
        participant_ids = resolver.load_snapshot(snapshot)

        if participant_ids:
            log.info("Batch-resolving %d participants...", len(participant_ids))
            await resolver.resolve_users_batch(participant_ids)
            log.info("Resolved users: %s", resolver.users)

            log.info("Known chats: %s", resolver.chats)
            log.info("Known users: %s", resolver.users)

        if not _first_connect:
            await sender.send("✅ <b>Max:</b> соединение восстановлено")
        else:
            chat_count = len(resolver.chats)
            await sender.send(f"✅ <b>Max:</b> подключён | чатов: {chat_count}")
        _first_connect = False

    @client.on_disconnect
    async def handle_disconnect():
        nonlocal _notif_count, _last_notif_time
        if not _can_notify():
            log.info("Disconnect notification suppressed (throttle)")
            return
        _notif_count += 1
        _last_notif_time = datetime.now()
        await sender.send("⚠️ <b>Max:</b> соединение потеряно, переподключение...")

    @client.on_message
    async def handle_message(msg: MaxMessage):
        log.info(
            "New message: chat=%s sender=%s is_self=%s text=%r attaches=%d",
            msg.chat_id,
            msg.sender_id,
            msg.is_self,
            (msg.text[:80] + "…") if len(msg.text) > 80 else msg.text,
            len(msg.attaches),
        )

        if msg.is_self:
            return

        raw_sender = await resolver.resolve_user(msg.sender_id)
        is_dm = resolver.is_dm(msg.chat_id)
        raw_chat = resolver.chat_name(msg.chat_id)

        # One forum topic per Max chat. Prefer a human title:
        # - DMs → the peer's name
        # - Groups with a known title → the chat title
        # - Chats discovered at runtime (no known title yet) → the sender's name
        #   (better than the numeric chat ID; ensure_topic will rename later if a
        #   real chat title appears).
        chat_title_known = raw_chat != str(msg.chat_id) and not raw_chat.startswith("DM:")
        if is_dm or not chat_title_known:
            topic_title = raw_sender
        else:
            topic_title = raw_chat

        existing_thread = sender.topic_store.get_topic(msg.chat_id)
        thread_id = await sender.ensure_topic(msg.chat_id, topic_title)

        # First time we touch this chat → publish a pinned profile card so the
        # topic starts with context (avatar, name, etc.).
        if existing_thread is None and thread_id is not None:
            from app.tg_handler import post_topic_intro
            asyncio.create_task(post_topic_intro(
                sender.bot, sender.chat_id, client, msg.chat_id, thread_id,
            ))

        sender_label = escape(raw_sender)
        chat_label = escape(raw_chat)
        header_text = _header(msg, sender_label, chat_label, is_dm)

        # One album is one (chat, sender) run of media arriving back to back.
        album_key = (msg.chat_id, msg.sender_id)

        link = msg.link
        link_type = link.get("type") if isinstance(link, dict) else None

        if link_type in ("FORWARD", "REPLY"):
            await _handle_linked_message(link, link_type, header_text, client, sender,
                                         resolver, thread_id=thread_id, msg=msg,
                                         albums=albums, key=album_key)
            if msg.text:
                await _flush_album(albums, album_key)
                await sender.send(f"{header_text}\n{escape(msg.text)}", message_thread_id=thread_id)
            log.info("Forwarded link type=%s → TG", link_type)
            return

        meaningful_attaches = [
            a for a in msg.attaches
            if isinstance(a, dict) and a.get("_type") not in ("CONTROL", "WIDGET", "INLINE_KEYBOARD", None)
        ]

        if meaningful_attaches:
            text_sent = await _send_attaches(
                meaningful_attaches, client, sender, header_text, msg.text,
                thread_id, msg, albums, album_key,
            )
            if msg.text and not text_sent:
                await _flush_album(albums, album_key)
                await sender.send(f"{header_text}\n{escape(msg.text)}", message_thread_id=thread_id)
        else:
            # Plain text must not jump ahead of media still waiting in the buffer.
            await _flush_album(albums, album_key)
            body = escape(msg.text) if msg.text else "<i>[нетекстовое сообщение]</i>"
            await sender.send(f"{header_text}\n{body}", message_thread_id=thread_id)
            log.info("Forwarded text → TG")

    return client
