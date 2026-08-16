"""Scriptable fakes for the opencode-discord-bot test suite.

These stand in for the network-bound classes (``OpencodeClient``,
``DiscordRest``, ``ComulyticClient``) and Pycord gateway objects
(``discord.TextChannel``, ``discord.Message``, ``discord.Attachment``,
``discord.ApplicationContext``, ``discord.Guild``) so the bot's features
can be exercised end-to-end with NO real Discord gateway, NO real
opencode server, NO real Comulytic API, and NO real Discord REST.

The three ``Scripted*Client`` fakes accept queued response sequences per
method via ``.script(method, *returns)`` so chain tests are deterministic.
Every call is recorded on ``self.calls`` (a list of ``(method, args,
kwargs)`` tuples) for assertions. ``.reset()`` clears the queues + call
log so a fake can be reused across tests.

The Pycord-free stand-ins (``FakeChannel``, ``FakeMessage``,
``FakeAttachment``, ``FakeInteraction``, ``FakeGuild``) implement just
the attributes/methods the bot's code paths read. They are intentionally
lightweight — they are NOT ``unittest.mock`` objects (those are heavy and
opaque); they are plain dataclasses-ish objects with recorded side
effects.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any


# ---------------------------------------------------------------------------
# Scriptable clients
# ---------------------------------------------------------------------------


class _ScriptedClient:
    """Base for the three scriptable fakes.

    Subclasses set ``_methods`` to the set of async methods they expose.
    ``.script(method, *returns)`` queues return values; the next call to
    that method pops the front. A method with an empty queue returns its
    ``_default_return`` (subclasses override per-method defaults via
    ``_default_for(method)``). Every call is recorded on ``self.calls``.
    """

    _methods: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self._queues: dict[str, deque] = defaultdict(deque)
        self.calls: list[tuple[str, tuple, dict]] = []
        # Side-effect callbacks: method -> list of callables(queue_state).
        # Used by tests that need to trigger stop_events on the Nth call.
        self._side_effects: dict[str, list] = defaultdict(list)

    def script(self, method: str, *returns: Any) -> None:
        """Queue return values for ``method`` (consumed in FIFO order)."""
        if method not in self._methods:
            raise ValueError(f"unknown method {method!r} for {type(self).__name__}")
        self._queues[method].extend(returns)

    def script_exc(self, method: str, *errors: BaseException) -> None:
        """Queue exceptions to raise for ``method`` (consumed in FIFO order)."""
        if method not in self._methods:
            raise ValueError(f"unknown method {method!r} for {type(self).__name__}")
        self._queues[method].extend(errors)

    def on_call(self, method: str, callback) -> None:
        """Register a side-effect callback fired BEFORE the method returns."""
        self._side_effects[method].append(callback)

    def reset(self) -> None:
        self._queues.clear()
        self.calls.clear()
        self._side_effects.clear()

    def _next(self, method: str) -> Any:
        queue = self._queues.get(method)
        if queue:
            value = queue.popleft()
            if isinstance(value, BaseException):
                raise value
            return value
        return self._default_for(method)

    def _default_for(self, method: str) -> Any:
        return None

    def _record(self, method: str, args: tuple, kwargs: dict) -> None:
        self.calls.append((method, args, kwargs))

    def _fire_side_effects(self, method: str) -> None:
        for cb in self._side_effects.get(method, []):
            cb(self._queues.get(method))


class ScriptedOpencodeClient(_ScriptedClient):
    """Duck-typed stand-in for ``opencode_client.OpencodeClient``.

    Every REST method is async and pops from the scripted queue (or
    returns a safe default). ``send_prompt_async`` stashes the sid on
    ``self.last_sid`` so tests can assert which session was driven.
    """

    _methods = frozenset({
        "health", "list_sessions", "create_session", "get_session",
        "delete_session", "abort_session", "revert_session",
        "get_session_status", "list_messages", "send_message",
        "send_prompt_async", "list_questions", "reply_question",
        "reject_question", "list_permissions", "reply_permission",
        "list_agents", "aclose",
    })

    def __init__(self) -> None:
        super().__init__()
        self.last_sid: str | None = None
        self.last_prompt_parts: list[dict] | None = None
        self.last_agent: str | None = None
        self._next_session_id = 1000

    def _default_for(self, method: str) -> Any:
        if method == "create_session":
            sid = f"sid-{self._next_session_id}"
            self._next_session_id += 1
            return {"id": sid}
        if method == "get_session_status":
            return {}
        if method == "list_questions":
            return []
        if method == "list_permissions":
            return []
        if method == "list_messages":
            return []
        if method == "list_sessions":
            return []
        if method == "list_agents":
            return []
        if method in (
            "delete_session", "abort_session", "reply_question",
            "reject_question", "reply_permission", "send_message",
        ):
            return True
        if method == "send_prompt_async":
            return None
        if method == "revert_session":
            return {}
        if method == "get_session":
            return {}
        if method == "health":
            return {"healthy": True}
        return None

    async def health(self):
        self._record("health", (), {})
        self._fire_side_effects("health")
        return self._next("health")

    async def list_sessions(self):
        self._record("list_sessions", (), {})
        self._fire_side_effects("list_sessions")
        return self._next("list_sessions")

    async def create_session(self, title=None):
        self._record("create_session", (), {"title": title})
        self._fire_side_effects("create_session")
        result = self._next("create_session")
        if isinstance(result, dict) and "id" in result:
            self.last_sid = result["id"]
        return result

    async def get_session(self, sid):
        self._record("get_session", (sid,), {})
        self._fire_side_effects("get_session")
        return self._next("get_session")

    async def delete_session(self, sid):
        self._record("delete_session", (sid,), {})
        self._fire_side_effects("delete_session")
        return self._next("delete_session")

    async def abort_session(self, sid):
        self._record("abort_session", (sid,), {})
        self._fire_side_effects("abort_session")
        return self._next("abort_session")

    async def revert_session(self, sid, message_id):
        self._record("revert_session", (sid, message_id), {})
        self._fire_side_effects("revert_session")
        return self._next("revert_session")

    async def get_session_status(self):
        self._record("get_session_status", (), {})
        self._fire_side_effects("get_session_status")
        return self._next("get_session_status")

    async def list_messages(self, sid, limit=None):
        self._record("list_messages", (sid,), {"limit": limit})
        self._fire_side_effects("list_messages")
        return self._next("list_messages")

    async def send_message(self, sid, parts, agent=None, model=None):
        self._record("send_message", (sid, parts), {"agent": agent, "model": model})
        self._fire_side_effects("send_message")
        return self._next("send_message")

    async def send_prompt_async(self, sid, parts, agent=None, model=None):
        self._record("send_prompt_async", (sid, parts), {"agent": agent, "model": model})
        self.last_sid = sid
        self.last_prompt_parts = parts
        self.last_agent = agent
        self._fire_side_effects("send_prompt_async")
        return self._next("send_prompt_async")

    async def list_questions(self):
        self._record("list_questions", (), {})
        self._fire_side_effects("list_questions")
        return self._next("list_questions")

    async def reply_question(self, request_id, answers):
        self._record("reply_question", (request_id, answers), {})
        self._fire_side_effects("reply_question")
        return self._next("reply_question")

    async def reject_question(self, request_id):
        self._record("reject_question", (request_id,), {})
        self._fire_side_effects("reject_question")
        return self._next("reject_question")

    async def list_permissions(self):
        self._record("list_permissions", (), {})
        self._fire_side_effects("list_permissions")
        return self._next("list_permissions")

    async def reply_permission(self, request_id, reply, message=None):
        self._record("reply_permission", (request_id, reply), {"message": message})
        self._fire_side_effects("reply_permission")
        return self._next("reply_permission")

    async def list_agents(self):
        self._record("list_agents", (), {})
        self._fire_side_effects("list_agents")
        return self._next("list_agents")

    async def aclose(self):
        self._record("aclose", (), {})
        self._fire_side_effects("aclose")
        return self._next("aclose")

    def _resolve_model(self, agent):
        # Mirror OpencodeClient._resolve_model — pure config read.
        return None


class ScriptedDiscordRest(_ScriptedClient):
    """Duck-typed stand-in for ``discord_rest.DiscordRest``.

    Methods return scripted dicts or auto-incrementing ids. ``create_message``
    records the posted content on ``self.posted`` (list of
    ``(channel_id, content)`` tuples) for assertions. ``list_messages`` pops
    from the queue (default ``[]``).
    """

    _methods = frozenset({
        "create_text_channel", "edit_channel", "create_message",
        "edit_message", "list_messages", "aclose",
    })

    def __init__(self) -> None:
        super().__init__()
        self._next_id = 1000
        self.posted: list[tuple[int, str]] = []
        self.channel_names: dict[int, str] = {}
        self.created_channels: list[tuple[int, str]] = []

    def _new_id(self) -> int:
        mid = self._next_id
        self._next_id += 1
        return mid

    def _default_for(self, method: str) -> Any:
        if method == "create_text_channel":
            return {"id": self._new_id()}
        if method == "create_message":
            return {"id": self._new_id()}
        if method == "edit_channel":
            return {}
        if method == "edit_message":
            return {}
        if method == "list_messages":
            return []
        return None

    async def create_text_channel(self, guild_id, name, *, parent_id=0, topic="", reason=""):
        self._record(
            "create_text_channel", (guild_id, name),
            {"parent_id": parent_id, "topic": topic, "reason": reason},
        )
        self._fire_side_effects("create_text_channel")
        result = self._next("create_text_channel")
        if isinstance(result, dict) and "id" in result:
            cid = int(result["id"])
            self.channel_names[cid] = name
            self.created_channels.append((cid, name))
        return result

    async def edit_channel(self, channel_id, name, *, reason=""):
        self._record("edit_channel", (channel_id, name), {"reason": reason})
        self._fire_side_effects("edit_channel")
        if isinstance(self.channel_names, dict) and channel_id in self.channel_names:
            self.channel_names[channel_id] = name
        return self._next("edit_channel")

    async def create_message(self, channel_id, content):
        self._record("create_message", (channel_id, content), {})
        self._fire_side_effects("create_message")
        result = self._next("create_message")
        if isinstance(result, dict) and "id" in result:
            self.posted.append((int(channel_id), content))
        return result

    async def edit_message(self, channel_id, message_id, content):
        self._record("edit_message", (channel_id, message_id, content), {})
        self._fire_side_effects("edit_message")
        return self._next("edit_message")

    async def list_messages(self, channel_id, *, after=0, limit=50):
        self._record("list_messages", (channel_id,), {"after": after, "limit": limit})
        self._fire_side_effects("list_messages")
        return self._next("list_messages")

    async def aclose(self):
        self._record("aclose", (), {})
        self._fire_side_effects("aclose")
        return self._next("aclose")


class ScriptedComulyticClient(_ScriptedClient):
    """Duck-typed stand-in for ``comulytic.ComulyticClient``.

    ``download_audio_proxy`` / ``download_audio_presigned`` default to
    returning ``self.default_audio_bytes`` (set by the test, e.g. a real
    sample clip) so chain tests can drive real ffmpeg + STT. The probe
    endpoint ``_request`` returns ``{"data": {"visible": False}}`` so the
    bridge's openapi probe no-ops.
    """

    _methods = frozenset({
        "list_recordings", "probe_newest", "get_note_detail",
        "download_audio_proxy", "download_audio_presigned", "close",
        "_request",
    })

    def __init__(self, default_audio_bytes: bytes = b"") -> None:
        super().__init__()
        self.default_audio_bytes = default_audio_bytes
        self._next_note_id = 5000

    def _default_for(self, method: str) -> Any:
        if method == "probe_newest":
            return (0, None)
        if method == "list_recordings":
            return {"data": {"data": [], "total": 0, "page": 1, "pageSize": 20}, "success": True}
        if method == "get_note_detail":
            return {"hasCloudAudio": True, "audioAccess": "public",
                    "audioUrlVO": {"rawAudio": "https://example.com/raw.mp3"}}
        if method in ("download_audio_proxy", "download_audio_presigned"):
            return self.default_audio_bytes
        if method == "_request":
            return {"data": {"visible": False}, "success": True}
        return None

    async def list_recordings(self, page=1, page_size=20, *, trash=False, dir_id="", trans_success=None):
        self._record(
            "list_recordings", (page, page_size),
            {"trash": trash, "dir_id": dir_id, "trans_success": trans_success},
        )
        self._fire_side_effects("list_recordings")
        return self._next("list_recordings")

    async def probe_newest(self):
        self._record("probe_newest", (), {})
        self._fire_side_effects("probe_newest")
        return self._next("probe_newest")

    async def get_note_detail(self, note_id):
        self._record("get_note_detail", (note_id,), {})
        self._fire_side_effects("get_note_detail")
        return self._next("get_note_detail")

    async def download_audio_proxy(self, note_id, *, max_bytes=120 * 1024 * 1024):
        self._record("download_audio_proxy", (note_id,), {"max_bytes": max_bytes})
        self._fire_side_effects("download_audio_proxy")
        return self._next("download_audio_proxy")

    async def download_audio_presigned(self, url, *, max_bytes=120 * 1024 * 1024):
        self._record("download_audio_presigned", (url,), {"max_bytes": max_bytes})
        self._fire_side_effects("download_audio_presigned")
        return self._next("download_audio_presigned")

    async def _request(self, method, path, *, params=None, json_body=None, extra_headers=None):
        self._record("_request", (method, path), {"params": params, "json_body": json_body})
        self._fire_side_effects("_request")
        return self._next("_request")

    async def close(self):
        self._record("close", (), {})
        self._fire_side_effects("close")
        return self._next("close")

    # The static helpers are tested directly against the real class, but
    # expose them here for completeness so duck-typed call sites compile.
    @staticmethod
    def get_audio_url(detail):
        from opencode_discord_bot.comulytic import ComulyticClient
        return ComulyticClient.get_audio_url(detail)


# ---------------------------------------------------------------------------
# Pycord-free stand-ins (FakeChannel, FakeMessage, etc.)
# ---------------------------------------------------------------------------


class FakeAuthor:
    def __init__(self, *, bot=False, id=2000, name="testuser", guild_permissions=None):
        self.bot = bot
        self.id = id
        self.name = name
        self.guild_permissions = guild_permissions or _FakePermissions()


class _FakePermissions:
    def __init__(self, manage_channels=False):
        self.manage_channels = manage_channels


class FakeAttachment:
    """Stand-in for ``discord.Attachment`` with readable bytes."""

    def __init__(self, *, data=b"", content_type="audio/mpeg", filename="clip.mp3"):
        self._data = data
        self.content_type = content_type
        self.filename = filename
        self.size = len(data)

    async def read(self) -> bytes:
        return self._data


class FakeChannel:
    """Stand-in for ``discord.TextChannel`` / ``discord.abc.Messageable``.

    Records every ``send`` call (content + view) and returns a
    ``FakeMessage`` with an auto-incrementing id. ``edit`` updates
    ``self.last_content`` (the most recent edit content).
    """

    def __init__(self, *, id=100, name="session-channel", guild=None,
                 category=None, topic="", mentioned_name=None):
        self.id = id
        self.name = name
        self.guild = guild
        self.category = category
        self.topic = topic
        self.mention = f"#{mentioned_name or name}"
        self.sent: list[tuple[str, dict]] = []
        self.edits: list[str] = []
        self.last_content: str | None = None
        self._next_msg_id = id * 1000 + 1

    async def send(self, content=None, *, view=None, embed=None, file=None, **kw):
        self.sent.append((content, {"view": view, "embed": embed, "file": file, **kw}))
        msg = FakeMessage(
            id=self._next_msg_id, channel=self, content=content, author=FakeAuthor(bot=True, name="opencode-bot")
        )
        self._next_msg_id += 1
        return msg

    async def edit(self, *, content=None, name=None, topic=None, **kw):
        if content is not None:
            self.edits.append(content)
            self.last_content = content
        if name is not None:
            self.name = name
        return self


class FakeMessage:
    """Stand-in for ``discord.Message``."""

    def __init__(self, *, id=1, channel=None, content="", author=None,
                 attachments=None, guild=None, edited_timestamp=None):
        self.id = id
        self.channel = channel if channel is not None else FakeChannel()
        self.content = content
        self.author = author if author is not None else FakeAuthor()
        self.attachments = attachments or []
        self.guild = guild or self.channel.guild
        self.edited_timestamp = edited_timestamp

    async def edit(self, content=None, **kw):
        if content is not None:
            self.content = content
            self.channel.last_content = content
            self.channel.edits.append(content)
        return self

    async def delete(self):
        return None


class FakeInteractionResponse:
    """Stand-in for ``discord.Interaction.response``."""

    def __init__(self):
        self.deferred = False
        self.responded = False
        self.sent_modal = None
        self.sent_content: list[str] = []

    async def defer(self, *, ephemeral=False):
        self.deferred = True

    async def send_message(self, content=None, *, ephemeral=False, **kw):
        self.responded = True
        self.sent_content.append(content)

    async def send_modal(self, modal):
        self.sent_modal = modal

    async def edit_message(self, *, content=None, view=None, **kw):
        pass


class FakeInteraction:
    """Stand-in for ``discord.Interaction`` (for button callbacks)."""

    def __init__(self, *, user=None, channel=None):
        self.user = user or FakeAuthor()
        self.channel = channel or FakeChannel()
        self.response = FakeInteractionResponse()
        # Pycord sets `interaction.data["values"]` on select-menu picks.
        self.data: dict = {}

    async def response_send_modal(self, modal):
        self.response.sent_modal = modal


class FakeApplicationContext:
    """Stand-in for ``discord.ApplicationContext`` (slash-command invocations).

    Implements just the surface the bot's command handlers read:
    ``channel_id``, ``guild``, ``author``, ``user``, ``defer``,
    ``followup.send``, ``respond``, ``response`` (for ``_deny``).
    """

    def __init__(self, *, channel=None, guild=None, author=None,
                 channel_id=None, guild_id=999):
        self.channel = channel or FakeChannel()
        self.channel_id = channel_id if channel_id is not None else self.channel.id
        self.guild = guild or FakeGuild()
        self.guild_id = guild_id
        self.author = author or FakeAuthor()
        self.user = self.author
        self._followup = self.channel
        self._responded: list[str] = []
        self._deferred = False
        self.response = FakeInteractionResponse()

    async def defer(self, *, ephemeral=False):
        self._deferred = True

    async def respond(self, content=None, **kw):
        self._responded.append(content)
        return await self.channel.send(content, **kw)

    class _Followup:
        def __init__(self, channel):
            self._channel = channel

        async def send(self, content=None, **kw):
            return await self._channel.send(content, **kw)

    @property
    def followup(self):
        return self._Followup(self.channel)


class FakeGuild:
    """Stand-in for ``discord.Guild``.

    ``create_text_channel`` returns a fresh ``FakeChannel`` (recorded on
    ``self.created_channels``); ``create_category`` returns a fake category
    with an id. ``get_channel`` returns a configured channel if its id is in
    ``self._channels_by_id``.
    """

    def __init__(self, *, id=999, name="test-guild", channels_by_id=None):
        self.id = id
        self.name = name
        self.created_channels: list[FakeChannel] = []
        self.created_categories: list["FakeCategoryChannel"] = []
        self._channels_by_id = channels_by_id or {}
        self._next_channel_id = 10000

    async def create_text_channel(self, name, *, category=None, topic="", reason=""):
        cid = self._next_channel_id
        self._next_channel_id += 1
        ch = FakeChannel(id=cid, name=name, guild=self, category=category, topic=topic)
        self.created_channels.append(ch)
        self._channels_by_id[cid] = ch
        return ch

    async def create_category(self, name, *, reason=""):
        cid = self._next_channel_id
        self._next_channel_id += 1
        cat = FakeCategoryChannel(id=cid, name=name, guild=self)
        self.created_categories.append(cat)
        return cat

    def get_channel(self, channel_id):
        return self._channels_by_id.get(channel_id)


class FakeCategoryChannel:
    """Stand-in for ``discord.CategoryChannel`` (parent of session channels)."""

    def __init__(self, *, id=200, name="OpenCode Sessions", guild=None):
        self.id = id
        self.name = name
        self.guild = guild
        self.text_channels: list[FakeChannel] = []


class FakeVoiceChannel:
    """Stand-in for ``discord.VoiceChannel`` (only what /oc_voice needs)."""

    def __init__(self, *, id=300, name="General", guild=None):
        self.id = id
        self.name = name
        self.guild = guild


class FakeVoiceState:
    """Stand-in for ``member.voice`` (so /oc_voice can find the channel)."""

    def __init__(self, channel=None):
        self.channel = channel


# ---------------------------------------------------------------------------
# Helpers for building opencode message lists
# ---------------------------------------------------------------------------


def assistant_message(text: str, *, mid: str = "m-asst") -> dict:
    """Build a list_messages entry for an assistant role with text parts."""
    return {
        "info": {"role": "assistant", "id": mid},
        "parts": [{"type": "text", "text": text}],
    }


def user_message(text: str, *, mid: str = "m-user") -> dict:
    """Build a list_messages entry for a user role with text parts."""
    return {
        "info": {"role": "user", "id": mid},
        "parts": [{"type": "text", "text": text}],
    }


def question_request(*, rid="q-1", sid="sid-1", questions=None) -> dict:
    """Build a list_questions entry for one question request."""
    if questions is None:
        questions = [{
            "question": "Which file?",
            "header": "Pick a file",
            "options": [
                {"label": "main.py", "description": "the entry point"},
                {"label": "utils.py", "description": "helpers"},
            ],
            "multiple": False,
            "custom": True,
        }]
    return {"id": rid, "sessionID": sid, "questions": questions}


def permission_request(*, rid="p-1", sid="sid-1", permission="edit", patterns=None) -> dict:
    """Build a list_permissions entry for one permission request."""
    return {
        "id": rid,
        "sessionID": sid,
        "permission": permission,
        "patterns": patterns or ["src/main.py"],
        "metadata": {},
        "always": False,
    }


def discord_message_dict(*, mid=9001, content="user reply", author_id="9999", bot=False) -> dict:
    """Build a list_messages entry for Discord REST (author + content)."""
    return {
        "id": str(mid),
        "content": content,
        "author": {"id": author_id, "bot": bot, "username": "user"},
    }