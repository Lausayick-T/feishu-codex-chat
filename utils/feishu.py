"""
feishu.py —— 飞书发消息的轻量封装（纯标准库 urllib，无第三方依赖）。

server.py 和后台任务发送逻辑共用它。故意不依赖 lark-oapi，
便于独立进程使用系统 Python 调用。
"""

from __future__ import annotations

import json
import mimetypes
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

from . import config

BASE = "https://open.feishu.cn/open-apis"
ROOT = Path(__file__).resolve().parent.parent  # chat-agent/

# tenant_access_token 进程内缓存： (token, 过期时间戳)
_token_cache: tuple[str, float] | None = None


def load_config() -> dict:
    return config.load()


_TRANSIENT_ERRORS = (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError)


def _request(
    method: str,
    url: str,
    payload: dict | None = None,
    headers: dict | None = None,
    retries: int = 3,
    timeout: int = 20,
) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    last: Exception | None = None
    for i in range(max(1, retries)):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json; charset=utf-8")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            try:
                return json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"HTTP {e.code}: {config.redact_text(body)}") from exc
        except _TRANSIENT_ERRORS as e:
            last = e
            if i < retries - 1:
                time.sleep(0.6 * (i + 1))
                continue
            raise
    raise RuntimeError(f"请求失败: {last}")


def _request_bytes(method: str, url: str, payload: bytes | None = None, headers: dict | None = None,
                   content_type: str | None = None, timeout: int = 30, retries: int = 3) -> tuple[bytes, str]:
    for i in range(max(1, retries)):
        req = urllib.request.Request(url, data=payload, method=method)
        if content_type:
            req.add_header("Content-Type", content_type)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {config.redact_text(body)}") from e
        except _TRANSIENT_ERRORS:
            if i < retries - 1:
                time.sleep(0.6 * (i + 1))
                continue
            raise
    raise RuntimeError("请求失败")


def _post(url: str, payload: dict, headers: dict | None = None) -> dict:
    return _request("POST", url, payload, headers)


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    """获取并缓存 tenant_access_token，过期前 5 分钟刷新。"""
    global _token_cache
    if not app_id or not app_secret:
        raise RuntimeError("缺少飞书凭证；请在 .env 中配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
    now = time.time()
    if _token_cache and _token_cache[1] - 300 > now:
        return _token_cache[0]
    res = _post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if res.get("code") != 0:
        raise RuntimeError(f"取 token 失败: {config.redact_text(res)}")
    token = res["tenant_access_token"]
    _token_cache = (token, now + res.get("expire", 7200))
    return token


def _send(chat_id: str, msg_type: str, content: str, app_id: str, app_secret: str) -> dict:
    if not app_id or not app_secret:
        cfg = load_config()["feishu"]
        app_id = config.require(cfg, "app_id")
        app_secret = config.require(cfg, "app_secret")
    token = get_tenant_access_token(app_id, app_secret)
    res = _post(
        f"{BASE}/im/v1/messages?receive_id_type=chat_id",
        {"receive_id": chat_id, "msg_type": msg_type, "content": content},
        headers={"Authorization": f"Bearer {token}"},
    )
    if res.get("code") != 0:
        raise RuntimeError(f"发消息失败: {config.redact_text(res)}")
    return res


def send_text(chat_id: str, text: str, *, app_id: str = "", app_secret: str = "") -> dict:
    """向某个会话（群 chat_id 或 p2p chat_id）发文本消息。"""
    return _send(chat_id, "text", json.dumps({"text": text}, ensure_ascii=False), app_id, app_secret)


def send_card(chat_id: str, card: dict, *, app_id: str = "", app_secret: str = "") -> dict:
    """发送交互式消息卡片。返回里 data.message_id 可用于后续就地更新/撤回。"""
    return _send(chat_id, "interactive", json.dumps(card, ensure_ascii=False), app_id, app_secret)


def send_image(chat_id: str, image_key: str) -> dict:
    """发送图片消息。"""
    return _send(chat_id, "image", json.dumps({"image_key": image_key}), "", "")


def _token() -> str:
    cfg = load_config()["feishu"]
    return get_tenant_access_token(config.require(cfg, "app_id"), config.require(cfg, "app_secret"))


_bot_open_id: str | None = None
_chat_human_count_cache: dict[str, tuple[int | None, float]] = {}
_chat_human_count_lock = threading.Lock()
_CHAT_HUMAN_COUNT_TTL = 60
_CHAT_HUMAN_COUNT_ERROR_TTL = 15


def get_bot_open_id() -> str:
    """获取机器人自身 open_id（用于判断消息是否 @了机器人），进程内缓存。"""
    global _bot_open_id
    if _bot_open_id:
        return _bot_open_id
    res = _request("GET", f"{BASE}/bot/v3/info", headers={"Authorization": f"Bearer {_token()}"})
    bot = res.get("bot") or {}
    _bot_open_id = bot.get("open_id", "")
    return _bot_open_id


def chat_human_member_count(chat_id: str) -> int | None:
    """返回群内真人数量（飞书接口不包含机器人）；失败时短暂缓存 None。"""
    now = time.time()
    with _chat_human_count_lock:
        cached = _chat_human_count_cache.get(chat_id)
        if cached and cached[1] > now:
            return cached[0]

    count = 0
    page_token = ""
    try:
        while True:
            query = {
                "member_id_type": "open_id",
                "page_size": "50",
            }
            if page_token:
                query["page_token"] = page_token
            encoded_chat_id = urllib.parse.quote(chat_id, safe="")
            url = f"{BASE}/im/v1/chats/{encoded_chat_id}/members?{urllib.parse.urlencode(query)}"
            res = _request(
                "GET",
                url,
                headers={"Authorization": f"Bearer {_token()}"},
                retries=1,
                timeout=5,
            )
            if res.get("code") != 0:
                raise RuntimeError(f"获取群成员失败: {config.redact_text(res)}")
            data = res.get("data") or {}
            count += len(data.get("items") or [])
            # 回复策略只区分 1 人和多人，无需继续拉取大群的剩余分页。
            if count > 1 or not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
    except Exception:
        with _chat_human_count_lock:
            _chat_human_count_cache[chat_id] = (None, now + _CHAT_HUMAN_COUNT_ERROR_TTL)
        return None

    with _chat_human_count_lock:
        _chat_human_count_cache[chat_id] = (count, now + _CHAT_HUMAN_COUNT_TTL)
    return count


def update_card(message_id: str, card: dict) -> dict:
    """就地更新一张已发出的卡片（同一条消息，内容替换，不新增）。"""
    res = _request(
        "PATCH",
        f"{BASE}/im/v1/messages/{message_id}",
        {"content": json.dumps(card, ensure_ascii=False)},
        headers={"Authorization": f"Bearer {_token()}"},
    )
    if res.get("code") != 0:
        raise RuntimeError(f"更新卡片失败: {config.redact_text(res)}")
    return res


def recall(message_id: str) -> dict:
    """撤回（删除）一条消息。"""
    res = _request(
        "DELETE",
        f"{BASE}/im/v1/messages/{message_id}",
        headers={"Authorization": f"Bearer {_token()}"},
    )
    if res.get("code") != 0:
        raise RuntimeError(f"撤回失败: {config.redact_text(res)}")
    return res


def download_message_resource(message_id: str, file_key: str, resource_type: str, dest) -> Path:
    """下载消息资源（file / image / media 等）到 dest。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE}/im/v1/messages/{message_id}/resources/{file_key}?type={resource_type}"
    data, _ = _request_bytes("GET", url, headers={"Authorization": f"Bearer {_token()}"})
    dest.write_bytes(data)
    return dest


def download_image(image_key: str, dest, image_type: str = "message") -> Path:
    """下载图片消息到 dest。"""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    url = f"{BASE}/im/v1/images/{image_key}?image_type={image_type}"
    data, _ = _request_bytes("GET", url, headers={"Authorization": f"Bearer {_token()}"})
    dest.write_bytes(data)
    return dest


def upload_file(path, file_type: str = "stream") -> str:
    """上传文件（multipart，纯标准库），返回 file_key。.md 等用 file_type=stream。"""
    import uuid as _uuid

    p = Path(path)
    name = p.name
    data = p.read_bytes()
    boundary = "----chatagent" + _uuid.uuid4().hex

    def _field(n: str, v: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{n}"\r\n\r\n{v}\r\n'
        ).encode("utf-8")

    body = b""
    body += _field("file_type", file_type)
    body += _field("file_name", name)
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    body += data + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(f"{BASE}/im/v1/files", data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    req.add_header("Authorization", f"Bearer {_token()}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        res = json.loads(resp.read().decode("utf-8"))
    if res.get("code") != 0:
        raise RuntimeError(f"上传文件失败: {config.redact_text(res)}")
    return res["data"]["file_key"]


def upload_image(path, image_type: str = "message") -> str:
    """上传图片，返回 image_key。"""
    import uuid as _uuid

    p = Path(path)
    boundary = "----chatagent" + _uuid.uuid4().hex
    mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"

    body = b""
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image_type"\r\n\r\n{image_type}\r\n'
    ).encode("utf-8")
    body += (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{p.name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    body += p.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    data, _ = _request_bytes(
        "POST",
        f"{BASE}/im/v1/images",
        payload=body,
        content_type=f"multipart/form-data; boundary={boundary}",
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=30,
    )
    res = json.loads(data.decode("utf-8"))
    if res.get("code") != 0:
        raise RuntimeError(f"上传图片失败: {config.redact_text(res)}")
    return res["data"]["image_key"]


def send_file(chat_id: str, file_key: str) -> dict:
    """发送文件消息。"""
    return _send(chat_id, "file", json.dumps({"file_key": file_key}), "", "")


if __name__ == "__main__":
    # 自测：python3 feishu.py <chat_id> <text>
    import sys

    if len(sys.argv) >= 3:
        print(send_text(sys.argv[1], sys.argv[2]))
    else:
        cfg = load_config()["feishu"]
        print("token ok:", bool(get_tenant_access_token(
            config.require(cfg, "app_id"), config.require(cfg, "app_secret")
        )))
