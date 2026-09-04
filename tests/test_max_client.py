"""Tests for app/max_client.py — OpCode enum and _parse_message."""

import pytest
from app.max_client import MaxClient, MaxMessage, OpCode


# ---------------------------------------------------------------------------
# OpCode enum
# ---------------------------------------------------------------------------

class TestOpCode:
    """Validate that all expected opcodes exist with their correct integer values."""

    def test_heartbeat_ping(self):
        assert OpCode.HEARTBEAT_PING == 1

    def test_handshake(self):
        assert OpCode.HANDSHAKE == 6

    def test_auth_snapshot(self):
        assert OpCode.AUTH_SNAPSHOT == 19

    def test_logout(self):
        assert OpCode.LOGOUT == 20

    def test_sticker_store(self):
        assert OpCode.STICKER_STORE == 27

    def test_asset_get(self):
        assert OpCode.ASSET_GET == 28

    def test_favorite_sticker(self):
        assert OpCode.FAVORITE_STICKER == 29

    def test_contact_get(self):
        assert OpCode.CONTACT_GET == 32

    def test_contact_presence(self):
        assert OpCode.CONTACT_PRESENCE == 35

    def test_chat_get(self):
        assert OpCode.CHAT_GET == 48

    def test_send_message(self):
        assert OpCode.SEND_MESSAGE == 64

    def test_edit_message(self):
        assert OpCode.EDIT_MESSAGE == 67

    def test_dispatch(self):
        assert OpCode.DISPATCH == 128

    def test_all_values_are_ints(self):
        for member in OpCode:
            assert isinstance(member.value, int), f"{member.name} is not an int"

    def test_no_duplicate_values(self):
        values = [m.value for m in OpCode]
        assert len(values) == len(set(values)), "Duplicate opcode values found"

    def test_can_be_used_as_int(self):
        # IntEnum should compare equal to a plain int
        assert OpCode.HANDSHAKE == 6
        assert 6 == OpCode.HANDSHAKE


# ---------------------------------------------------------------------------
# MaxMessage dataclass defaults
# ---------------------------------------------------------------------------

class TestMaxMessageDefaults:
    def test_default_text_is_empty_string(self):
        msg = MaxMessage()
        assert msg.text == ""

    def test_default_is_self_is_false(self):
        msg = MaxMessage()
        assert msg.is_self is False

    def test_default_attaches_is_empty_list(self):
        msg = MaxMessage()
        assert msg.attaches == []

    def test_default_link_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.link == {}

    def test_default_raw_is_empty_dict(self):
        msg = MaxMessage()
        assert msg.raw == {}

    def test_attaches_are_independent_instances(self):
        # mutable default via field(default_factory=...) must not be shared
        m1 = MaxMessage()
        m2 = MaxMessage()
        m1.attaches.append("x")
        assert m2.attaches == []


# ---------------------------------------------------------------------------
# MaxClient._parse_message
# ---------------------------------------------------------------------------

def _make_client() -> MaxClient:
    return MaxClient(token="tok", device_id="dev")


class TestParseMessage:
    """Tests for _parse_message — the only complex pure-ish method."""

    def test_returns_none_when_no_message_key(self):
        client = _make_client()
        assert client._parse_message({}) is None

    def test_returns_none_when_message_is_not_dict(self):
        client = _make_client()
        assert client._parse_message({"message": "oops"}) is None
        assert client._parse_message({"message": 42}) is None
        assert client._parse_message({"message": None}) is None

    def test_basic_text_message(self):
        client = _make_client()
        payload = {
            "chatId": 100,
            "message": {
                "sender": 7,
                "text": "Hello",
                "time": 1700000000000,
                "id": "abc123",
            },
        }
        msg = client._parse_message(payload)
        assert msg is not None
        assert msg.chat_id == 100
        assert msg.sender_id == 7
        assert msg.text == "Hello"
        assert msg.timestamp == 1700000000000
        assert msg.message_id == "abc123"

    def test_message_id_is_always_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"id": 99999}}
        msg = client._parse_message(payload)
        assert isinstance(msg.message_id, str)
        assert msg.message_id == "99999"

    def test_missing_text_defaults_to_empty_string(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 1}}
        msg = client._parse_message(payload)
        assert msg.text == ""

    def test_attaches_populated(self):
        client = _make_client()
        attaches = [{"_type": "PHOTO", "url": "http://example.com/img.jpg"}]
        payload = {"chatId": 1, "message": {"attaches": attaches}}
        msg = client._parse_message(payload)
        assert msg.attaches == attaches

    def test_attaches_none_becomes_empty_list(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"attaches": None}}
        msg = client._parse_message(payload)
        assert msg.attaches == []

    def test_link_populated(self):
        client = _make_client()
        link = {"type": "FORWARD", "message": {"text": "original"}}
        payload = {"chatId": 1, "message": {"link": link}}
        msg = client._parse_message(payload)
        assert msg.link == link

    def test_link_none_becomes_empty_dict(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"link": None}}
        msg = client._parse_message(payload)
        assert msg.link == {}

    def test_raw_is_full_payload(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"text": "hi"}, "extra": "data"}
        msg = client._parse_message(payload)
        assert msg.raw is payload

    def test_is_self_false_when_my_id_not_set(self):
        client = _make_client()
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_false_when_sender_differs(self):
        client = _make_client()
        client._my_id = 1
        payload = {"chatId": 1, "message": {"sender": 99}}
        msg = client._parse_message(payload)
        assert msg.is_self is False

    def test_is_self_true_when_sender_matches_my_id(self):
        client = _make_client()
        client._my_id = 42
        payload = {"chatId": 1, "message": {"sender": 42}}
        msg = client._parse_message(payload)
        assert msg.is_self is True

    def test_chat_id_none_when_absent(self):
        client = _make_client()
        payload = {"message": {"text": "no chat id"}}
        msg = client._parse_message(payload)
        assert msg.chat_id is None

    def test_empty_message_dict_returns_none(self):
        # Empty dict is falsy in Python, so _parse_message treats it as absent
        client = _make_client()
        payload = {"chatId": 5, "message": {}}
        msg = client._parse_message(payload)
        assert msg is None


# ---------------------------------------------------------------------------
# MaxClient constructor / basic state
# ---------------------------------------------------------------------------

class TestMaxClientInit:
    def test_token_stored(self):
        c = MaxClient(token="my_token", device_id="dev1")
        assert c.token == "my_token"

    def test_device_id_stored(self):
        c = MaxClient(token="tok", device_id="mydev")
        assert c.device_id == "mydev"

    def test_debug_default_false(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c.debug is False

    def test_debug_explicit_true(self):
        c = MaxClient(token="tok", device_id="dev", debug=True)
        assert c.debug is True

    def test_initial_seq_is_zero(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._seq == 0

    def test_initial_my_id_is_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._my_id is None

    def test_ws_url_constant(self):
        assert MaxClient.WS_URL == "wss://ws-api.oneme.ru/websocket"

    def test_heartbeat_sec_constant(self):
        assert MaxClient.HEARTBEAT_SEC == 30

    def test_reconnect_sec_constant(self):
        assert MaxClient.RECONNECT_SEC == 5

    def test_on_disconnect_cb_initial_none(self):
        c = MaxClient(token="tok", device_id="dev")
        assert c._on_disconnect_cb is None

    def test_on_disconnect_decorator_registers_callback(self):
        c = MaxClient(token="tok", device_id="dev")

        @c.on_disconnect
        async def my_handler():
            pass

        assert c._on_disconnect_cb is my_handler

    def test_on_disconnect_returns_function(self):
        c = MaxClient(token="tok", device_id="dev")

        async def my_handler():
            pass

        result = c.on_disconnect(my_handler)
        assert result is my_handler


# ---------------------------------------------------------------------------
# on_ready must not block the WebSocket read loop
# ---------------------------------------------------------------------------

class TestOnReadyDoesNotBlockReadLoop:
    """Regression: on_ready issues its own cmd() calls, and the responses to
    those calls can only be read by the same loop that dispatched on_ready.
    Awaiting the callback inline deadlocks until every such request hits its
    10s timeout, which silently empties the startup contact batch-resolve.

    Both tests use wait_for so the unfixed code fails fast instead of hanging.
    """

    async def test_handle_does_not_await_callback_to_completion(self):
        import asyncio
        from app.max_client import MaxClient

        client = MaxClient(token="t", device_id="d")
        released = asyncio.Event()
        finished = asyncio.Event()

        @client.on_ready
        async def slow_ready(snapshot):
            await released.wait()
            finished.set()

        snapshot_frame = {"opcode": 19, "cmd": 1, "seq": 0,
                          "payload": {"profile": {"id": 42}}}
        try:
            await asyncio.wait_for(client._handle(snapshot_frame), timeout=2)
        except asyncio.TimeoutError:
            pytest.fail(
                "_handle() blocked until on_ready finished: the WebSocket read "
                "loop is stalled, so replies to requests issued inside on_ready "
                "can never be delivered."
            )

        assert not finished.is_set(), "callback should still be running in the background"

        released.set()
        for _ in range(3):
            await asyncio.sleep(0)
        assert finished.is_set()

    async def test_request_issued_from_on_ready_receives_its_response(self):
        """End-to-end shape of the bug: fetch_contacts() called inside on_ready
        must resolve once the loop delivers the matching frame."""
        import asyncio
        from unittest.mock import AsyncMock
        from app.max_client import MaxClient

        client = MaxClient(token="t", device_id="d")
        client._ws = AsyncMock()
        client._ws.closed = False
        result = {}

        @client.on_ready
        async def ready(snapshot):
            result["contacts"] = await client.fetch_contacts([777])

        try:
            await asyncio.wait_for(
                client._handle({"opcode": 19, "cmd": 1, "seq": 0,
                                "payload": {"profile": {"id": 42}}}),
                timeout=2,
            )
        except asyncio.TimeoutError:
            pytest.fail("_handle() blocked on on_ready; see the test above.")

        await asyncio.sleep(0)
        assert client._pending, "on_ready's cmd() should be waiting for a response"

        seq = next(iter(client._pending))
        await client._handle({
            "opcode": 32, "cmd": 1, "seq": seq,
            "payload": {"contacts": [{"id": 777, "names": [{"name": "Elena"}]}]},
        })
        await asyncio.sleep(0)

        assert result["contacts"]["contacts"][0]["names"][0]["name"] == "Elena"


class TestOnReadySingleFlight:
    """A reconnect delivers a fresh snapshot that supersedes the previous run,
    so at most one on_ready may be in flight."""

    async def test_new_snapshot_cancels_previous_run(self):
        import asyncio
        from app.max_client import MaxClient

        client = MaxClient(token="t", device_id="d")
        started = []
        cancelled = []

        @client.on_ready
        async def slow_ready(snapshot):
            started.append(snapshot["profile"]["id"])
            try:
                await asyncio.Event().wait()   # never completes on its own
            except asyncio.CancelledError:
                cancelled.append(snapshot["profile"]["id"])
                raise

        frame = lambda uid: {"opcode": 19, "cmd": 1, "seq": 0,
                             "payload": {"profile": {"id": uid}}}

        await client._handle(frame(1))
        await asyncio.sleep(0)
        first = client._ready_task

        await client._handle(frame(2))          # reconnect: new snapshot
        for _ in range(3):
            await asyncio.sleep(0)

        assert started == [1, 2]
        assert cancelled == [1], "the previous on_ready should have been cancelled"
        assert first.cancelled()
        assert not client._ready_task.done(), "the new run must still be alive"

        client._ready_task.cancel()

    async def test_finished_run_is_not_cancelled_again(self):
        import asyncio
        from app.max_client import MaxClient

        client = MaxClient(token="t", device_id="d")
        runs = []

        @client.on_ready
        async def quick_ready(snapshot):
            runs.append(snapshot["profile"]["id"])

        frame = lambda uid: {"opcode": 19, "cmd": 1, "seq": 0,
                             "payload": {"profile": {"id": uid}}}

        await client._handle(frame(1))
        await asyncio.sleep(0)
        assert client._ready_task.done()

        await client._handle(frame(2))
        await asyncio.sleep(0)

        assert runs == [1, 2]
        assert not client._ready_task.cancelled()
