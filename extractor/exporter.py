#!/usr/bin/env python3
"""Export chat data from SQLite to ChatLab v0.0.2 format (JSON/JSONL)."""
import base64
import json
import mimetypes
import os
import re
import time
import urllib.parse

from extractor.models import get_db

# DB msg_type → ChatLab message type
CHATLAB_TYPE_MAP = {
    1: 0,   # text → TEXT
    2: 5,   # emoji → EMOJI
    3: 1,   # image → IMAGE
    4: 24,  # share → SHARE
    0: 99,  # other → OTHER
}


_STICKER_HEX_RE = re.compile(r"-ts-([0-9a-fA-F]{4,})(?:\.[a-zA-Z0-9]{1,5})?$")


def _decode_sticker_name(url: str) -> str | None:
    """Recover a sticker's human-readable name from a Douyin IM CDN URL.

    URLs look like .../im-resource/<digits>-ts-<utf8-hex>?...
    where <utf8-hex> is the UTF-8 bytes of e.g. "续火花.png" as hex.
    Returns the decoded name without extension, or None on no match.
    """
    if not url:
        return None
    try:
        path = urllib.parse.urlparse(url).path
        last = path.rsplit("/", 1)[-1]
        m = _STICKER_HEX_RE.search(last)
        if not m:
            return None
        raw = bytes.fromhex(m.group(1))
        name = raw.decode("utf-8")
        # Strip a trailing extension like .png/.webp/.gif
        base, sep, ext = name.rpartition(".")
        if sep and base and len(ext) <= 4 and ext.isalnum():
            return base
        return name
    except (UnicodeDecodeError, ValueError):
        return None


def _emoji_text_label(content: str | None, media_url: str | None) -> str:
    """Pick a text label '[name]' for an emoji message.
    Prefers the URL-decoded name (most reliable), then the existing content,
    finally a generic placeholder.
    """
    name = _decode_sticker_name(media_url or "")
    if name:
        return f"[{name}]"
    c = (content or "").strip()
    if c and c != "[表情]":
        if c.startswith("[") and c.endswith("]"):
            return c
        return f"[{c}]"
    return "[表情]"


_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{\{\d+\}\}")


def _render_template_tips(obj: dict) -> str | None:
    """渲染抖音系统消息模板。
    例：{"tips":"{{1}}赞了你分享的 {{2}}","template":[{"key":1,"name":"对方"},{"key":2,"name":"视频X"}]}
    → "对方赞了你分享的 视频X"
    """
    tips = obj.get("tips") or obj.get("hint") or obj.get("title")
    if not tips:
        return None
    names = {}
    for it in obj.get("template") or []:
        if isinstance(it, dict) and it.get("key") is not None:
            names[it["key"]] = (it.get("name") or "").strip()
    out = tips
    for k, name in names.items():
        out = out.replace(f"{{{{{k}}}}}", name)
    out = _TEMPLATE_PLACEHOLDER_RE.sub("", out).strip()
    return out or None


def _system_message_text(content: str | None, cj: dict | None = None) -> str:
    """msg_type=0 系统消息的可读文本。
    - 文本（已是 [语音 X秒]）原样返回
    - JSON 模板渲染成 [系统] 前缀的可读文字（优先用完整 cj，content 在 DB 里被截 200 字符）
    - 无法识别的兜底为 [系统消息]
    """
    c = (content or "").strip()
    if c and not c.startswith("{"):
        return c
    obj = cj if isinstance(cj, dict) else None
    if not obj and c.startswith("{"):
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            obj = None
    if not obj:
        return "[系统消息]"
    rendered = _render_template_tips(obj)
    if rendered:
        return f"[系统] {rendered}"
    if obj.get("aweType") == 193 or obj.get("tips") == "通话成功":
        return "[通话成功]"
    title = obj.get("title") or ""
    hint = obj.get("hint") or ""
    if "看视频" in title or "通话邀请" in hint:
        return "[视频通话邀请]"
    return "[系统消息]"


def _file_to_data_url(filepath: str) -> str | None:
    """Read a local file and return a data URL (base64 encoded)."""
    if not filepath or not os.path.isfile(filepath):
        return None

    ext = os.path.splitext(filepath)[1].lower()
    # 优先使用自定义映射（mimetypes 会把 .mpeg 识别为 video/mpeg）
    mime = {
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".mpeg": "audio/mpeg",
            ".mp3": "audio/mpeg",
            ".wav": "audio/wav",
    }.get(ext)
    if not mime:
        mime, _ = mimetypes.guess_type(filepath)
    if not mime:
        mime = "application/octet-stream"

    try:
        with open(filepath, "rb") as f:
            data = f.read()
        b64 = base64.b64encode(data).decode("ascii")
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _detect_owner(conn) -> tuple[str, str]:
    """从数据库推断 owner。

    策略：
    1. participant_uids 中第一个 uid（提取时 curLoginUserInfo 排第一）
    2. 回退：出现在最多不同会话中的 sender_uid
    """
    # 策略 1: 从 participant_uids 取第一个 uid
    row = conn.execute(
        "SELECT participant_uids FROM conversations WHERE participant_uids != '[]' LIMIT 1"
    ).fetchone()
    if row:
        try:
            uids = json.loads(row[0])
            if uids:
                owner_uid = uids[0]
                user = conn.execute(
                    "SELECT nickname FROM users WHERE uid = ?", (owner_uid,)
                ).fetchone()
                owner_name = user[0] if user and user[0] else "我"
                return owner_uid, owner_name
        except (json.JSONDecodeError, IndexError):
            pass

    # 策略 2: 出现在最多会话中的 sender_uid
    rows = conn.execute("""
        SELECT sender_uid, COUNT(DISTINCT conv_id) as conv_count
        FROM messages WHERE sender_uid != ''
        GROUP BY sender_uid ORDER BY conv_count DESC LIMIT 1
    """).fetchall()
    if rows:
        owner_uid = rows[0][0]
        user = conn.execute(
            "SELECT nickname FROM users WHERE uid = ?", (owner_uid,)
        ).fetchone()
        owner_name = user[0] if user and user[0] else "我"
        return owner_uid, owner_name

    return "", "我"


def _get_content_json(msg) -> dict | None:
    """从 raw_data 中提取完整的 content_json。"""
    raw = msg["raw_data"]
    if not raw:
        return None
    try:
        raw_obj = json.loads(raw) if isinstance(raw, str) else raw
        cj_str = raw_obj.get("content_json", "")
        if cj_str:
            return json.loads(cj_str) if isinstance(cj_str, str) else cj_str
    except (json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


class ChatLabExporter:
    def __init__(self, conv_name: str = None, output_format: str = "jsonl"):
        self.conv_name = conv_name
        self.output_format = output_format  # "json" or "jsonl"

    def export(self, output_path: str):
        conn = get_db()

        # Detect owner
        owner_uid, owner_name = _detect_owner(conn)
        print(f"[*] 检测到 owner: {owner_name} ({owner_uid})")

        # Find conversation
        if self.conv_name:
            row = conn.execute(
                "SELECT conv_id, name FROM conversations WHERE name LIKE ?",
                (f"%{self.conv_name}%",),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT conv_id, name FROM conversations ORDER BY last_message_time DESC LIMIT 1"
            ).fetchone()

        if not row:
            print(f"[-] 未找到会话: {self.conv_name or '(any)'}")
            conn.close()
            return

        conv_id = row["conv_id"]
        conv_name = row["name"]
        print(f"[*] 导出会话: {conv_name} (ID: {conv_id})")

        # Load messages ordered by seq
        messages = conn.execute(
            "SELECT * FROM messages WHERE conv_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()

        print(f"[*] 共 {len(messages)} 条消息")

        # Build users map from DB
        users_map = {}
        users_rows = conn.execute("SELECT uid, nickname FROM users").fetchall()
        for u in users_rows:
            if u["uid"] and u["nickname"]:
                users_map[u["uid"]] = u["nickname"]

        # Collect members from messages
        members_map = {}
        for msg in messages:
            uid = msg["sender_uid"] or ""
            if uid and uid not in members_map:
                name = users_map.get(uid, "")
                if not name:
                    name = owner_name if uid == owner_uid else conv_name
                members_map[uid] = name

        # Media base dir
        media_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")

        # Build ChatLab structure
        header = {
            "chatlab": {
                "version": "0.0.2",
                "exportedAt": int(time.time()),
                "generator": "douyin-chat-export",
            },
            "meta": {
                "name": f"与{conv_name}的对话",
                "platform": "douyin",
                "type": "private",
                "ownerId": owner_uid,
            },
        }

        members = []
        for uid, name in members_map.items():
            member = {"platformId": uid, "accountName": name}
            members.append(member)

        chatlab_messages = []
        image_count = 0
        image_embedded = 0
        emoji_count = 0
        voice_count = 0
        system_count = 0
        share_normalized = 0
        ref_count = 0

        for msg in messages:
            chatlab_type = CHATLAB_TYPE_MAP.get(msg["msg_type"], 99)
            content = msg["content"]
            cj = _get_content_json(msg)

            # 发送方：从 users_map 获取昵称
            uid = msg["sender_uid"] or ""
            display_name = users_map.get(uid, "")
            if not display_name:
                display_name = owner_name if uid == owner_uid else conv_name

            # 语音消息：msg_type=0 但有 resource_url + duration
            # 不再嵌 base64 / CDN URL —— 对 LLM 无意义且每条几百 KB；用纯文字标签即可。
            is_voice = False
            if cj and cj.get("resource_url") and cj.get("duration"):
                is_voice = True
                chatlab_type = 0  # TEXT
                dur_sec = round(cj["duration"] / 1000)
                content = f"[语音 {dur_sec}秒]" if dur_sec else "[语音]"
                voice_count += 1

            # 表情：用文字标签代替 URL — CDN 早晚过期，URL 对 LLM 也没意义。
            # 从 URL 路径里反解出表情名（如 [续火花]）。
            if not is_voice and chatlab_type == 5:
                content = _emoji_text_label(content, msg["media_url"])
                emoji_count += 1
            # 图片：优先 CDN URL，本地文件 fallback 为 base64
            elif not is_voice and chatlab_type == 1:
                if msg["media_url"]:
                    content = msg["media_url"]
                    image_count += 1
                elif msg["media_local_path"] and os.path.isfile(
                    os.path.join(media_dir, msg["media_local_path"])
                ):
                    data_url = _file_to_data_url(
                        os.path.join(media_dir, msg["media_local_path"])
                    )
                    if data_url:
                        content = data_url
                        image_embedded += 1
                    else:
                        chatlab_type = 0
                    image_count += 1

            # 分享消息：以 cj 的形态判断（含 itemId），不依赖 msg_type ——
            # 实测有 ~2300 条 share 被错分类成 msg_type=1 (TEXT)，content 直接是 aweme JSON 漏出来。
            # 不要放宽到 aweType / content_title 等字段 —— 表情消息的 cj 也带这些。
            if not is_voice and cj and cj.get("itemId"):
                item_id = cj.get("itemId", "")
                title = (cj.get("content_title") or "").strip()
                author = (cj.get("content_name") or "").strip()
                parts = []
                if title:
                    parts.append(title)
                if author:
                    parts.append(f"@{author}")
                if item_id:
                    parts.append(f"https://www.douyin.com/video/{item_id}")
                content = "[分享视频] " + " | ".join(parts) if parts else "[分享视频]"
                chatlab_type = 24  # SHARE，统一类型
                share_normalized += 1

            # 系统消息（msg_type=0 但不是语音 / 不是 share）：模板 JSON 渲染成可读文字
            elif not is_voice and msg["msg_type"] == 0:
                content = _system_message_text(content, cj)
                if chatlab_type == 99:
                    chatlab_type = 0  # TEXT
                system_count += 1

            # 最终兜底：还是 JSON 的内容（极少数 awemeType 都缺的）统一收敛
            if isinstance(content, str) and content.startswith("{") and content.endswith("}"):
                content = "[分享内容]"

            chatlab_msg = {
                "sender": uid,
                "accountName": display_name,
                "timestamp": msg["timestamp"] or 0,
                "type": chatlab_type,
                "content": content,
                "platformMessageId": msg["msg_id"],
            }

            # 引用/回复消息
            if msg["ref_msg"]:
                try:
                    ref = json.loads(msg["ref_msg"]) if isinstance(msg["ref_msg"], str) else msg["ref_msg"]
                    ref_info = {}
                    if ref.get("server_id"):
                        ref_info["replyTo"] = f"srv_{ref['server_id']}"
                    if ref.get("nickname"):
                        ref_info["replyToAuthor"] = ref["nickname"]
                    if ref.get("content"):
                        ref_info["replyToContent"] = ref["content"]
                    if ref_info:
                        chatlab_msg["replyTo"] = ref_info
                        ref_count += 1
                except (json.JSONDecodeError, TypeError):
                    pass

            chatlab_messages.append(chatlab_msg)

        conn.close()

        # Write output
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        if self.output_format == "json":
            output = {**header, "members": members, "messages": chatlab_messages}
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False)
            print(f"[+] JSON 导出完成: {output_path}")
        else:
            # JSONL format
            with open(output_path, "w", encoding="utf-8") as f:
                # Header line
                header_line = {"_type": "header", **header}
                f.write(json.dumps(header_line, ensure_ascii=False) + "\n")
                # Member lines
                for member in members:
                    member_line = {"_type": "member", **member}
                    f.write(json.dumps(member_line, ensure_ascii=False) + "\n")
                # Message lines
                for msg in chatlab_messages:
                    msg_line = {"_type": "message", **msg}
                    f.write(json.dumps(msg_line, ensure_ascii=False) + "\n")
            print(f"[+] JSONL 导出完成: {output_path}")

        print(f"  消息: {len(chatlab_messages)}")
        print(f"  成员: {len(members)}")
        if image_count:
            print(f"  图片: {image_count} (嵌入 data URL: {image_embedded})")
        if emoji_count:
            print(f"  表情: {emoji_count} (转为文字标签)")
        if voice_count:
            print(f"  语音: {voice_count} (转为文字标签)")
        if system_count:
            print(f"  系统消息: {system_count} (模板渲染为文字)")
        if share_normalized:
            print(f"  分享视频: {share_normalized} (含 type=1 错分类的)")
        if ref_count:
            print(f"  引用/回复: {ref_count}")
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"  文件大小: {size_mb:.1f} MB")
