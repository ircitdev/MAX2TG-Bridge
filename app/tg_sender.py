import asyncio
import io
import logging

from telegram import Bot, InputFile
from telegram.constants import ParseMode
from telegram.error import RetryAfter, TimedOut
from telegram.request import HTTPXRequest

from app.topics import TopicStore

log = logging.getLogger(__name__)

TG_MAX_LENGTH = 4096
TG_CAPTION_MAX = 1024
TG_TOPIC_NAME_MAX = 128
MAX_RETRIES = 3


def _looks_numeric(title: str) -> bool:
    """A title is 'placeholder' when it carries no human-readable name yet."""
    title = (title or "").strip()
    return not title or title.isdigit() or title.startswith("DM:")


class TelegramSender:
    def __init__(self, token: str, chat_id: str, topic_store: TopicStore,
                 proxy_url: str | None = None):
        if proxy_url:
            request = HTTPXRequest(proxy=proxy_url)
            self._bot = Bot(token=token, request=request)
        else:
            self._bot = Bot(token=token)
        self._chat_id = chat_id
        self._topics = topic_store
        self._topic_lock = asyncio.Lock()

    @property
    def bot(self) -> Bot:
        return self._bot

    @property
    def chat_id(self) -> str:
        return self._chat_id

    @property
    def topic_store(self) -> TopicStore:
        return self._topics

    async def start(self):
        await self._bot.initialize()
        me = await self._bot.get_me()
        log.info("Telegram bot ready: @%s", me.username)

    async def stop(self):
        await self._bot.shutdown()

    # ── forum topics ───────────────────────────────────────────────

    async def ensure_topic(self, max_chat_id, title: str) -> int | None:
        """Return the Telegram forum topic (thread) ID for a Max chat.

        Creates the topic on first use. If a previously created topic still
        carries a placeholder (numeric) name and a real name is now known,
        the topic is renamed. Returns None if topic creation fails — callers
        then fall back to the General topic.
        """
        title = (title or str(max_chat_id)).strip()[:TG_TOPIC_NAME_MAX]

        existing = self._topics.get_topic(max_chat_id)
        if existing is not None:
            stored = self._topics.get_title(max_chat_id) or ""
            if title and title != stored and _looks_numeric(stored) and not _looks_numeric(title):
                try:
                    await self._bot.edit_forum_topic(
                        chat_id=self._chat_id, message_thread_id=existing, name=title
                    )
                    self._topics.update_title(max_chat_id, title)
                    log.info("Renamed forum topic %s → %r", existing, title)
                except Exception:
                    log.exception("Failed to rename forum topic %s", existing)
            return existing

        async with self._topic_lock:
            existing = self._topics.get_topic(max_chat_id)
            if existing is not None:
                return existing
            try:
                topic = await self._bot.create_forum_topic(
                    chat_id=self._chat_id, name=title
                )
            except Exception:
                log.exception(
                    "Failed to create forum topic for Max chat %s — is the supergroup "
                    "a forum and is the bot an admin with 'Manage Topics'?",
                    max_chat_id,
                )
                return None
            thread_id = topic.message_thread_id
            self._topics.set_topic(max_chat_id, thread_id, title)
            log.info("Created forum topic %r (thread=%s) for Max chat %s",
                     title, thread_id, max_chat_id)
            return thread_id

    # ── helpers ────────────────────────────────────────────────────

    def _truncate_caption(self, text: str) -> str:
        if len(text) > TG_CAPTION_MAX:
            return text[: TG_CAPTION_MAX - 20] + "\n\n[...усечено]"
        return text

    async def _retry(self, coro_factory):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await coro_factory()
            except RetryAfter as e:
                log.warning("Telegram rate limit, retry after %ss", e.retry_after)
                await asyncio.sleep(e.retry_after)
            except TimedOut:
                log.warning("Telegram timeout (attempt %d/%d)", attempt, MAX_RETRIES)
                await asyncio.sleep(2 * attempt)
            except Exception:
                log.exception("Failed to send to Telegram (attempt %d/%d)", attempt, MAX_RETRIES)
                if attempt == MAX_RETRIES:
                    return None
                await asyncio.sleep(2 * attempt)
        return None

    # ── send methods ───────────────────────────────────────────────

    async def send(self, text: str, message_thread_id: int | None = None) -> None:
        if not text:
            return

        if len(text) > TG_MAX_LENGTH:
            text = text[: TG_MAX_LENGTH - 20] + "\n\n[...усечено]"

        await self._retry(
            lambda: self._bot.send_message(
                chat_id=self._chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_photo(self, data: bytes, caption: str = "", filename: str = "photo.jpg",
                         message_thread_id: int | None = None) -> None:
        caption = self._truncate_caption(caption)
        await self._retry(
            lambda: self._bot.send_photo(
                chat_id=self._chat_id,
                photo=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_document(self, data: bytes, caption: str = "", filename: str = "file",
                            message_thread_id: int | None = None) -> None:
        caption = self._truncate_caption(caption)
        await self._retry(
            lambda: self._bot.send_document(
                chat_id=self._chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_video(self, data: bytes, caption: str = "", filename: str = "video.mp4",
                         message_thread_id: int | None = None) -> None:
        caption = self._truncate_caption(caption)
        await self._retry(
            lambda: self._bot.send_video(
                chat_id=self._chat_id,
                video=InputFile(io.BytesIO(data), filename=filename),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )

    async def send_voice(self, data: bytes, caption: str = "",
                         message_thread_id: int | None = None) -> None:
        caption = self._truncate_caption(caption)
        result = await self._retry(
            lambda: self._bot.send_voice(
                chat_id=self._chat_id,
                voice=InputFile(io.BytesIO(data), filename="voice.ogg"),
                caption=caption or None,
                parse_mode=ParseMode.HTML,
                message_thread_id=message_thread_id,
            )
        )
        if result is None:
            log.info("send_voice failed, falling back to send_audio")
            await self._retry(
                lambda: self._bot.send_audio(
                    chat_id=self._chat_id,
                    audio=InputFile(io.BytesIO(data), filename="audio.m4a"),
                    caption=caption or None,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=message_thread_id,
                )
            )

    async def send_sticker(self, data: bytes, message_thread_id: int | None = None) -> None:
        await self._retry(
            lambda: self._bot.send_sticker(
                chat_id=self._chat_id,
                sticker=InputFile(io.BytesIO(data), filename="sticker.webp"),
                message_thread_id=message_thread_id,
            )
        )
