# MAX2TG-Bridge — контекст для AI-ассистентов

Этот файл — короткая шпаргалка для будущих сессий ассистента: что такое проект, что уже сделано, какие подводные камни.

## Что это

Двусторонний мост MAX (`ws-api.oneme.ru`) ↔ Telegram через форум-топики супергруппы. Каждый MAX-чат = свой topic в Telegram. Userbot подключается WebSocket'ом к MAX по `__oneme_auth` токену, Telegram-side — обычный bot через python-telegram-bot polling.

Основан на [Aist/max2tg](https://github.com/Aist/max2tg), но переписан вокруг форум-топиков и расширен. Лицензия MIT.

## Структура

```
app/
  main.py          # entry: load .env → MaxClient + Telegram Application
  config.py        # Settings dataclass + load_settings()
  max_client.py    # WS клиент MAX. Opcodes, retry, reconnect, upload_*
  max_listener.py  # MAX → TG handler (incoming), auto-topic creation
  resolver.py      # кеш контактов / чатов (chats_raw, contacts_raw)
  tg_sender.py     # TelegramSender + ensure_topic (create/rename)
  tg_handler.py    # TG → MAX handler + команды /bind, /add, /profile, /intro, /del, /help
  topics.py        # TopicStore: JSON-карта max_chat_id ↔ thread_id
tests/             # 191 pytest, asyncio_mode=auto
docs/cover.jpg     # обложка README
state/             # runtime (топик-карта), gitignored
logs/              # логи, gitignored
```

## Протокол MAX (наши находки)

WebSocket: `wss://ws-api.oneme.ru/websocket`, `Origin: https://web.max.ru`.

Пакет: `{"ver":11,"cmd":0,"seq":N,"opcode":OP,"payload":{...}}`. Ответ: `cmd=1` (OK) или `cmd=3` (error).

| Opcode | Назначение |
|---|---|
| 1 | HEARTBEAT_PING (раз в 30 сек) |
| 6 | HANDSHAKE |
| 19 | AUTH_SNAPSHOT (логин + первый snapshot чатов) |
| 32 | CONTACT_GET — возвращает `{names, baseUrl, photoId}`, **НЕ возвращает phone/about** |
| 35 | CONTACT_PRESENCE |
| 48 | CHAT_GET |
| 57 | open by link — работает только для `/join/<token>` (group/channel); `/u/<token>` падает с `not.found / chat namespace` |
| 64 | SEND_MESSAGE (text, elements, attaches, link) |
| 65 | ATTACH_TYPING ("я загружаю PHOTO/AUDIO/...") |
| 67 | EDIT_MESSAGE |
| 80 | PHOTO_UPLOAD_URL → `{count:1}` → `{url}` → POST → `{photos:{...:{token}}}` → attach `{_type:PHOTO, photoToken}` |
| 82 | VIDEO_UPLOAD_URL |
| 83 | VIDEO_DOWNLOAD_URL (на видео из MAX) |
| 84 | **CALLS** `createJoinLink` — НЕ аудио (как мы предполагали) |
| 85 | **CALLS** `getOkCallData` — тоже не аудио |
| 86 | Что-то с upload, требует `count + show + chatId`, возвращает `{}` |
| 87 | FILE_UPLOAD_URL → `{count:1}` → `{info:[{url, fileId}]}` → POST → ждать DISPATCH `op=136` → attach `{_type:FILE, fileId}` |
| 88 | FILE_DOWNLOAD_URL |
| 128 | DISPATCH (incoming сообщения от MAX) |
| 136 | UPLOAD_READY (server подтверждает обработку загруженного файла) |

### Элементы форматирования в `SEND_MESSAGE.message.elements`

`{type, from, length}` — `from` это codepoint-индекс (НЕ UTF-16; Telegram-side юзает UTF-16, конвертация в `tg_handler._utf16_to_char_offset`).

| TG MessageEntityType | MAX element type |
|---|---|
| BOLD | `STRONG` |
| ITALIC | `EMPHASIZED` |
| STRIKETHROUGH | `STRIKETHROUGH` |
| UNDERLINE | `UNDERLINE` |
| CODE | `MONOSPACED` |
| PRE | `BLOCKQUOTE` (MAX не имеет boxed code-block; квоту юзер сам выбрал) |
| BLOCKQUOTE / EXPANDABLE_BLOCKQUOTE | `BLOCKQUOTE` |
| TEXT_LINK | `LINK` с `attributes.url` |

`CODE_BLOCK` отвергается валидацией (`No enum constant`). Имена `EMPHASIS`, `EM`, `ITALIC` тоже отвергались — пришли к `EMPHASIZED` (как в `max-botapi-python.enums.text_style`).

### Attach типы (входящие из MAX)

`PHOTO` (baseUrl/baseRawUrl + photoToken), `VIDEO` (thumbnail), `FILE` (url + name + size), `AUDIO` (url), `STICKER` (url), `SHARE` (url + title + description), `LOCATION` (lat/lon), `CONTACT` (name + phone), `UNSUPPORTED` (новый voice — `audioId + token + duration + wave`, опкод download неизвестен).

### Особенности WS

- `proto.payload` ошибки **закрывают WS** для некоторых опкодов (мост авто-реконнектится через 5 сек). Это мешает массовому пробингу: после первой неудачи остальные опкоды успевают только таймаут схватить.
- Токен MAX молча ротируется при логине в web.max.ru с другого устройства: handshake проходит, AUTH_SNAPSHOT не приходит → бот висит. Решение — освежить `MAX_TOKEN`.

## Telegram-side нюансы

- Бот должен быть **админом супергруппы с правом «Управление темами»** (`can_manage_topics: true`) — иначе `create_forum_topic` падает.
- Супергруппа должна быть **forum-enabled** (включены Topics).
- Доступные реакции в чате — по умолчанию ограничены, для 👀 нужно «Все эмодзи» в настройках.
- Bot API лимит загрузки файла — 20 МБ.
- Bot **не может** ставить custom-emoji реакции (нужен Premium).

## Команды (в супергруппе)

- `/bind <chat_id|URL> [title]` — ручная привязка топика к MAX-чату.
- `/add <https://max.ru/join/...>` — резолв инвайт-ссылки + создание топика. `/u/<token>` пока не поддерживается.
- `/profile` — в топике, профиль собеседника (имя/id/аватар).
- `/intro` — перепост закреплённой карточки.
- `/del` — удалить топик с подтверждением (inline-кнопки).
- `/help` — справка.

## Состояние / runtime

- `state/topics.json` — JSON-карта `max_chat_id ↔ {topic_id, title}`. Атомарно перезаписывается (tmpfile + os.replace). Critical для непересоздавания топиков. Том должен быть mounted в docker-compose.
- `logs/max2tg.log` — RotatingFileHandler 10MB × 5.

## Тесты

`pytest -q` → 191 passed. asyncio_mode=auto. Покрытие: TopicStore, config, listener helpers (форматирование размеров, throttle), tg_handler (роутинг команд, маршрутизация медиа), max_client опкоды.

## Деплой

Docker. `docker-compose.yml` биндит `./logs:/app/logs` и `./state:/app/state`. Алёрт о подключении/обрыве идёт в General-топик.

## Что осталось / known issues

- Голосовые TG → MAX: уходят как `.ogg` файл (FILE), не как voice bubble. Опкод нативной audio-upload неизвестен (issue в vkmax #14 — без ответа). Hunting requires browser-side network capture.
- Voice MAX → TG: для нового `_type=UNSUPPORTED` нет рабочего download-опкода. Опкод 84/85 — calls service. Probing блокирован WS-disconnect на proto.payload.
- `/u/<token>` (user share) — opcode 57 ищет в chat-namespace. Server hint «No link or token found» для `{token}` payload — обманчив, реально опкод хочет только `link` URL.
- Phone/about для контакта — `CONTACT_GET` не возвращает. Нужен другой опкод (предположительно тот же, что юзает web.max.ru при открытии профиля справа).

## Если нужно ребутнуть знание о репо

```bash
# Локально
cd /d/DevTools/Database/max2tg
git status
pytest -q

# Прод (VPS Kyonix)
ssh max2tg "cd /opt/max2tg && docker compose ps && docker compose logs --tail=50 max2tg"

# Структура развёртывания
# - Контейнер max2tg-max2tg-1, образ собран из ./Dockerfile (python:3.12-slim → alpine; работает несмотря на glibc→musl, потому что слои совместимы).
```

## Ссылки

- Upstream: [Aist/max2tg](https://github.com/Aist/max2tg)
- Reference opcode-doc: [nsdkinx/vkmax](https://github.com/nsdkinx/vkmax) (особенно [docs/opcodes.md](https://github.com/nsdkinx/vkmax/blob/main/docs/opcodes.md))
- Официальный бот-API MAX: [max-messenger/max-botapi-python](https://github.com/max-messenger/max-botapi-python) — там же `enums/text_style.py` с правильными именами стилей
- Альтернативный мост: [mimimiartartart/MaxToTelegramBridge](https://github.com/mimimiartartart/MaxToTelegramBridge) (one-topic-per-всё, аналогичные паттерны)
