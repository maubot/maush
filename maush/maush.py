# maushbot - A maubot to execute shell commands in maush from Matrix.
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from typing import Any
import base64
import json
import re
import shlex

from maubot import MessageEvent, Plugin
from maubot.handlers import command, event
from mautrix.types import (
    ContentURI,
    EventType,
    FileInfo,
    MediaMessageEventContent,
    MessageType,
    RoomAvatarStateEventContent,
    RoomID,
    RoomNameStateEventContent,
    RoomTopicStateEventContent,
    ReactionEvent,
    StateEvent,
    UserID,
    EventID,
)
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper

from .ansitohtml import ansi_to_html


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("rooms")
        helper.copy("admins")
        helper.copy("server")
        helper.copy("untrusted")


LINE_LIMIT = 256
BYTE_LIMIT = 8192
ELLIPSIS = "[…]"


allowed_localpart_regex = re.compile(r"^[A-Za-z0-9._=+-]+$")


class MaushBot(Plugin):
    name_cache: dict[RoomID, str]
    topic_cache: dict[RoomID, str]
    avatar_cache: dict[RoomID, ContentURI]
    allow_redact: set[EventID]

    @classmethod
    def get_config_class(cls) -> type[BaseProxyConfig]:
        return Config

    async def start(self) -> None:
        self.name_cache = {}
        self.topic_cache = {}
        self.avatar_cache = {}
        self.allow_redact = set()
        self.on_external_config_update()

    async def get_cached_name(self, room_id: RoomID) -> str:
        if room_id not in self.name_cache:
            name_evt = await self.client.get_state_event(room_id, EventType.ROOM_NAME)
            self.name_cache[room_id] = (
                name_evt.name.strip()
                if (isinstance(name_evt, RoomNameStateEventContent) and name_evt.name)
                else ""
            )
        return self.name_cache[room_id]

    async def get_cached_topic(self, room_id: RoomID) -> str:
        if room_id not in self.topic_cache:
            topic_evt = await self.client.get_state_event(room_id, EventType.ROOM_TOPIC)
            self.topic_cache[room_id] = (
                topic_evt.topic.strip()
                if (
                    isinstance(topic_evt, RoomTopicStateEventContent)
                    and topic_evt.topic
                )
                else ""
            )
        return self.topic_cache[room_id]

    async def get_cached_avatar(self, room_id: RoomID) -> str:
        if room_id not in self.avatar_cache:
            avatar_evt = await self.client.get_state_event(
                room_id, EventType.ROOM_AVATAR
            )
            self.avatar_cache[room_id] = (
                avatar_evt.url
                if (
                    isinstance(avatar_evt, RoomAvatarStateEventContent)
                    and avatar_evt.url
                )
                else ContentURI("")
            )
        return self.avatar_cache[room_id]

    async def _get_reply_file(self, mxc: ContentURI) -> bytes | None:
        url = self.client.api.get_download_url(mxc)
        max_size = 8 * 1024 * 1024
        async with self.client.api.session.head(url) as response:
            if int(response.headers.get("Content-Length", "0")) > max_size:
                return None
        file = await self.client.download_media(mxc)
        if len(file) > max_size:
            return None
        return file

    async def _exec(self, evt: MessageEvent, **kwargs: Any) -> None:
        if not self._exec_ok(evt):
            self.log.debug(
                f"Ignoring exec {evt.event_id} from {evt.sender} in {evt.room_id}"
            )
            return

        http = self.client.api.session
        old_name = await self.get_cached_name(evt.room_id)
        old_topic = await self.get_cached_topic(evt.room_id)
        old_avatar = await self.get_cached_avatar(evt.room_id)
        devices = {}
        if old_name:
            devices["name"] = old_name
        if old_topic:
            devices["topic"] = old_topic
        if old_avatar:
            devices["avatar-mxc"] = old_avatar
        reply_to_id = evt.content.get_reply_to()
        if reply_to_id:
            reply_to_evt = await self.client.get_event(evt.room_id, reply_to_id)
            devices["reply"] = reply_to_evt.content.body
            if reply_to_evt.content.msgtype.is_media:
                devices["reply-mxc"] = reply_to_evt.content.url
                try:
                    file = await self._get_reply_file(reply_to_evt.content.url)
                    if file:
                        devices["reply-file"] = file
                except Exception:
                    self.log.warning(
                        "Failed to download media for shell", exc_info=True
                    )

        localpart, server = self.client.parse_user_id(evt.sender)
        if not allowed_localpart_regex.match(localpart):
            await evt.reply("User ID not supported")
            return
        req_data = {
            **kwargs,
            "user": evt.sender,
            "home": re.sub(r"//+", "/", f"/{server}/{localpart}"),
            "untrusted": evt.sender in self.config["untrusted"],
            "devices": {
                name: base64.b64encode(
                    file.encode("utf-8") if isinstance(file, str) else file
                ).decode("utf-8")
                for name, file in devices.items()
            },
        }
        try:
            resp = await http.post(self.config["server"], data=json.dumps(req_data))
        except Exception:
            await evt.reply("Failed to send request to maush")
            return
        if resp.status == 502:
            await evt.reply("maush is currently down")
            return
        data = await resp.json()
        self.log.debug("Execution response for %s: %s", evt.sender, data)
        if not data["ok"]:
            self.log.error("Exec failed: %s", data["error"])
            await evt.reply(data["error"])
            return

        dur = round(data["duration"] / 1_000_000, 1)
        ret = data["return"]
        if ret != 0:
            resp = f"Exited with code {ret} in {dur} ms. "
        else:
            resp = f"Completed in {dur} ms. "
        if data["timeout"]:
            resp += "**Execution timed out**. "
        if data["stdout"]:
            stdout = data["stdout"].strip()
            if stdout.count("\n") > LINE_LIMIT:
                stdout = "\n".join(stdout.split("\n")[:LINE_LIMIT] + [ELLIPSIS])
            if len(stdout) > BYTE_LIMIT:
                stdout = stdout[: BYTE_LIMIT - len(ELLIPSIS)] + ELLIPSIS
            resp += f"**stdout:**\n<pre><code>{ansi_to_html(stdout)}\n</code></pre>\n"
        if data["stderr"]:
            stderr = data["stderr"].strip()
            if stderr.count("\n") > LINE_LIMIT:
                stderr = "\n".join(stderr.split("\n")[:LINE_LIMIT] + [ELLIPSIS])
            if len(stderr) > BYTE_LIMIT:
                stderr = stderr[: BYTE_LIMIT - len(ELLIPSIS)] + ELLIPSIS
            resp += f"**stderr:**\n<pre><code>{ansi_to_html(stderr)}\n</code></pre>\n"

        resp = resp.strip()
        if resp:
            evt_id = await evt.reply(resp, allow_html=True)
            self.allow_redact.add(evt_id)
            await self.client.react(evt.room_id, evt_id, "delete")

        new_dev = data["devices"]
        new_name = new_dev.get("name") or ""
        if new_name:
            new_name = new_name.strip()
        new_topic = new_dev.get("topic") or ""
        if new_topic:
            new_topic = new_topic.strip()
        new_avatar = new_dev.get("avatar-mxc") or ""
        if new_avatar:
            new_avatar = new_avatar.strip()
        if (
            evt.sender in self.config["untrusted"]
            or (len(new_name) > 100 and new_name != old_name)
            or (len(new_topic) > 1000 and new_topic != old_topic)
            or (
                (not new_avatar.startswith("mxc://") or len(new_avatar) > 100)
                and new_avatar != old_avatar
                and new_avatar
            )
        ) and (
            new_name != old_name or new_topic != old_topic or new_avatar != old_avatar
        ):
            await evt.reply("3:<")
            return
        if new_name != old_name:
            await self.client.send_state_event(
                evt.room_id,
                EventType.ROOM_NAME,
                RoomNameStateEventContent(name=new_name),
            )
            self.name_cache[evt.room_id] = new_name
        if new_topic != old_topic:
            await self.client.send_state_event(
                evt.room_id,
                EventType.ROOM_TOPIC,
                RoomTopicStateEventContent(topic=new_topic),
            )
            self.topic_cache[evt.room_id] = new_topic
        if new_avatar != old_avatar:
            await self.client.send_state_event(
                evt.room_id,
                EventType.ROOM_AVATAR,
                RoomAvatarStateEventContent(url=new_avatar),
            )
            self.avatar_cache[evt.room_id] = new_avatar
        out_file = data.get("out_file")
        if out_file:
            data = base64.b64decode(out_file["content"])
            mime = out_file["mimetype"]
            filename = out_file["name"]
            uri = await self.client.upload_media(
                data=data,
                filename=filename,
                mime_type=mime,
            )
            msgtype = MessageType.FILE
            if mime.startswith("image/"):
                msgtype = MessageType.IMAGE
            elif mime.startswith("video/"):
                msgtype = MessageType.VIDEO
            elif mime.startswith("audio/"):
                msgtype = MessageType.AUDIO
            evt_id = await evt.reply(
                MediaMessageEventContent(
                    msgtype=msgtype,
                    body=filename,
                    url=uri,
                    info=FileInfo(
                        size=len(data),
                        mimetype=mime,
                    ),
                )
            )
            self.allow_redact.add(evt_id)
            await self.client.react(evt.room_id, evt_id, "delete")

    def _exec_ok(self, evt: MessageEvent) -> bool:
        return (
            evt.room_id in self.config["rooms"]
            and evt.content.msgtype == MessageType.TEXT
            and evt.sender != self.client.mxid
        )

    @event.on(EventType.REACTION)
    async def reaction(self, evt: ReactionEvent) -> None:
        if (
            evt.content.relates_to.event_id in self.allow_redact
            and evt.content.relates_to.key == "delete"
            and evt.sender != self.client.mxid
        ):
            self.allow_redact.remove(evt.content.relates_to.event_id)
            await self.client.redact(
                evt.room_id,
                evt.content.relates_to.event_id,
                f"Delete requested by {evt.sender}",
            )

    @event.on(EventType.ROOM_MESSAGE)
    async def arbitrary_cmd(self, evt: MessageEvent) -> None:
        if not self._exec_ok(evt) or (
            not evt.content.body.startswith("!!")
            and not evt.content.body.startswith("!?")
        ):
            return
        split = evt.content.body.split("\n\n", 1)
        stdin = split[1] if len(split) > 1 else ""
        split = split[0].split(" ", 1)
        prefix, cmd = split[0][:2], split[0][2:]
        args = [cmd]
        if len(split) > 1:
            args += shlex.split(split[1]) if prefix == "!?" else split[1].split(" ")
        await self._exec(evt, language=cmd, args=args, raw=True, script=stdin)

    @command.new("admin-sh", aliases=["su"])
    @command.argument("script", required=True, pass_raw=True)
    async def admin_shell(self, evt: MessageEvent, script: str) -> None:
        if evt.sender not in self.config["admins"]:
            await evt.reply(
                f"`{evt.sender}` is not in the sudoers file. This incident will be [reported](https://xkcd.com/838/)."
            )
            return
        await self._exec(evt, language="sh", script=script, admin=True)

    @command.new("sudo")
    @command.argument("user_id", required=True)
    @command.argument("script", required=True, pass_raw=True)
    async def sudo(self, evt: MessageEvent, user_id: UserID, script: str) -> None:
        if evt.sender not in self.config["admins"]:
            await evt.reply(
                f"`{evt.sender}` is not in the sudoers file. This incident will be [reported](https://xkcd.com/838/)."
            )
            return
        evt.sender = user_id
        await self._exec(evt, language="sh", script=script, admin=True)

    @command.new("sh", aliases=["shell"])
    @command.argument("script", required=True, pass_raw=True)
    async def shell(self, evt: MessageEvent, script: str) -> None:
        await self._exec(evt, language="sh", script=script)

    @command.new("py", aliases=["python"])
    @command.argument("script", required=True, pass_raw=True)
    async def python(self, evt: MessageEvent, script: str) -> None:
        await self._exec(evt, language="python", script=script)

    @command.new("js", aliases=["javascript", "node"])
    @command.argument("script", required=True, pass_raw=True)
    async def javascript(self, evt: MessageEvent, script: str) -> None:
        await self._exec(evt, language="node.js", script=script)

    @command.new("el", aliases=["execline"])
    @command.argument("script", required=True, pass_raw=True)
    async def execline(self, evt: MessageEvent, script: str) -> None:
        await self._exec(evt, language="execline", script=script)

    @event.on(EventType.ROOM_NAME)
    async def name_handler(self, evt: StateEvent) -> None:
        self.name_cache[evt.room_id] = (
            evt.content.name.strip() if evt.content.name else ""
        )

    @event.on(EventType.ROOM_TOPIC)
    async def topic_handler(self, evt: StateEvent) -> None:
        self.topic_cache[evt.room_id] = (
            evt.content.topic.strip() if evt.content.topic else ""
        )

    @event.on(EventType.ROOM_AVATAR)
    async def avatar_handler(self, evt: StateEvent) -> None:
        self.avatar_cache[evt.room_id] = (
            evt.content.url.strip() if evt.content.url else ""
        )
