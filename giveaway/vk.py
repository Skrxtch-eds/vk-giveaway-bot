"""Небольшой клиент VK API: HTTPS, тайм-ауты и ограничение частоты запросов."""
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


class VkError(Exception):
    def __init__(self, code: int):
        self.code = code
        # Ответ VK может содержать параметры запроса. Не сохраняем их и ключ доступа.
        super().__init__(f"Ошибка VK API, код {code}")


class TransportError(Exception):
    pass


class VkClient:
    def __init__(self, token: str, group_id: int, version: str = "5.199", partner_tokens: dict | None = None):
        if not token or group_id <= 0:
            raise ValueError("Нужны VK_GROUP_TOKEN и положительный VK_GROUP_ID")
        self.token, self.group_id, self.version = token, group_id, version
        self.partner_tokens = {key.casefold(): value for key, value in (partner_tokens or {}).items()}
        self._lock = threading.Lock()
        self._last_call = 0.0

    def call(self, method: str, *, token: str | None = None, **params):
        with self._lock:
            time.sleep(max(0, 0.4 - (time.monotonic() - self._last_call)))
            self._last_call = time.monotonic()
        payload = urlencode({**params, "access_token": token or self.token, "v": self.version}).encode()
        request = Request(f"https://api.vk.com/method/{method}", data=payload, method="POST")
        data = self._json(request, timeout=20)
        if "error" in data:
            raise VkError(int(data["error"].get("error_code", -1)))
        if "response" not in data:
            raise TransportError("VK вернул ответ без результата")
        return data["response"]

    @staticmethod
    def _json(request, timeout: int):
        try:
            with urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except (URLError, TimeoutError, OSError, ValueError):
            raise TransportError("Не удалось получить корректный ответ VK") from None

    def is_member(self, day, user_id: int) -> bool:
        result = self.call("groups.isMember", token=self.partner_tokens.get(day.partner_id.casefold()),
                           group_id=day.group_id, user_id=user_id)
        if isinstance(result, dict) and result.get("member") in (0, 1):
            return bool(result["member"])
        if type(result) is int and result in (0, 1):
            return bool(result)
        raise TransportError("VK вернул неизвестный формат проверки подписки")

    def messages_allowed(self, user_id: int) -> bool:
        result = self.call("messages.isMessagesFromGroupAllowed", group_id=self.group_id, user_id=user_id)
        if not isinstance(result, dict) or result.get("is_allowed") not in (0, 1):
            raise TransportError("VK вернул неизвестный формат разрешения сообщений")
        return bool(result["is_allowed"])

    def send(self, user_id: int, random_id: int, body: dict):
        params = {**body, "peer_id": user_id, "random_id": random_id}
        params["keyboard"] = json.dumps(params["keyboard"], ensure_ascii=False)
        return self.call("messages.send", **params)

    def long_poll_server(self) -> dict:
        result = self.call("groups.getLongPollServer", group_id=self.group_id)
        url = urlparse(result["server"])
        if url.scheme != "https" or not url.hostname:
            raise TransportError("Long Poll сервер должен использовать HTTPS")
        return result

    def poll(self, server: dict, ts: str) -> dict:
        query = urlencode({"act": "a_check", "key": server["key"], "ts": ts, "wait": 25})
        return self._json(f"{server['server']}?{query}", timeout=35)

    def upload_banner(self, path: str | Path, peer_id: int) -> str:
        image = Path(path)
        content = image.read_bytes()
        if len(content) > 10 * 1024 * 1024 or image.suffix.lower() not in (".png", ".jpg", ".jpeg"):
            raise ValueError("Для баннера нужен PNG/JPEG не более 10 МБ")
        upload = self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        url = urlparse(upload["upload_url"])
        if url.scheme != "https" or not url.hostname:
            raise TransportError("Загрузка баннера требует HTTPS")
        boundary = "----Giveaway" + secrets.token_hex(16)
        mime = "image/png" if image.suffix.lower() == ".png" else "image/jpeg"
        header = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; filename=\"banner{image.suffix.lower()}\"\r\n"
                  f"Content-Type: {mime}\r\n\r\n").encode()
        request = Request(upload["upload_url"], data=header + content + f"\r\n--{boundary}--\r\n".encode(),
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        uploaded = self._json(request, timeout=45)
        saved = self.call("photos.saveMessagesPhoto", photo=uploaded["photo"], server=uploaded["server"], hash=uploaded["hash"])
        if not isinstance(saved, list) or not saved:
            raise TransportError("Не удалось сохранить баннер")
        photo = saved[0]
        suffix = f"_{photo['access_key']}" if photo.get("access_key") else ""
        return f"photo{photo['owner_id']}_{photo['id']}{suffix}"

    def check_connection(self, campaign, admin_id: int) -> dict:
        settings = self.call("groups.getLongPollSettings", group_id=self.group_id)
        events = settings.get("events", {})
        if not settings.get("is_enabled", settings.get("enabled")) or not events.get("message_new"):
            raise ValueError("Включите Long Poll API и событие «Входящее сообщение» в настройках сообщества")
        self.long_poll_server()
        self.messages_allowed(admin_id)
        checked = []
        for day in campaign.days:
            self.is_member(day, admin_id)
            checked.append(day.partner_id)
        return {"long_poll": True, "partners_checked": checked}
