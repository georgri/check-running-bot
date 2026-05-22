#!/usr/bin/env python3
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Optional

import requests


class UnrecoverableError(Exception):
    pass


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: Optional[str]
    runc_username: str
    runc_password: str
    check_url: str
    sold_out_marker: str
    poll_interval_seconds: int
    request_timeout_seconds: int
    state_file: str

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        username = os.getenv("RUNC_USERNAME", "").strip()
        password = os.getenv("RUNC_PASSWORD", "").strip()
        if not token:
            raise UnrecoverableError("TELEGRAM_BOT_TOKEN is not set")
        if not username or not password:
            raise UnrecoverableError("RUNC_USERNAME/RUNC_PASSWORD must be set")

        return cls(
            telegram_bot_token=token,
            telegram_chat_id=(os.getenv("TELEGRAM_CHAT_ID") or "").strip() or None,
            runc_username=username,
            runc_password=password,
            check_url=os.getenv("CHECK_URL", "https://runc.run/check-in/217/").strip(),
            sold_out_marker=os.getenv(
                "SOLD_OUT_MARKER", "Свободные места на 42,2 км закончились"
            ).strip(),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
            state_file=os.getenv("STATE_FILE", "/root/check-running-bot/state.json").strip(),
        )


class TelegramClient:
    def __init__(self, token: str, timeout_seconds: int) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.update_offset: Optional[int] = None

    def _request(self, method: str, params: dict) -> dict:
        resp = requests.post(
            f"{self.base_url}/{method}", json=params, timeout=self.timeout_seconds
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error for {method}: {data}")
        return data

    def send_message(self, chat_id: str, text: str) -> None:
        self._request(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )

    def discover_chat_id(self) -> Optional[str]:
        payload = {
            "timeout": 1,
            "allowed_updates": ["channel_post", "my_chat_member", "message"],
        }
        if self.update_offset is not None:
            payload["offset"] = self.update_offset

        data = self._request("getUpdates", payload)
        updates = data.get("result", [])
        chat_id: Optional[str] = None

        for item in updates:
            update_id = item.get("update_id")
            if update_id is not None:
                self.update_offset = int(update_id) + 1

            channel_post = item.get("channel_post") or {}
            chat = channel_post.get("chat") or {}
            if chat.get("id"):
                chat_id = str(chat["id"])

            my_chat_member = item.get("my_chat_member") or {}
            chat = my_chat_member.get("chat") or {}
            if chat.get("type") == "channel" and chat.get("id"):
                chat_id = str(chat["id"])

            message = item.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("type") in {"channel", "supergroup"} and chat.get("id"):
                chat_id = str(chat["id"])

        return chat_id


class SlotMonitor:
    LOGIN_URL = "https://runc.run/login/"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.telegram = TelegramClient(cfg.telegram_bot_token, cfg.request_timeout_seconds)
        self.stop_requested = False
        self.chat_id = cfg.telegram_chat_id
        self.last_available: Optional[bool] = None

    def _signal_handler(self, signum, _frame) -> None:
        logging.info("Received signal %s, stopping", signum)
        self.stop_requested = True

    def _load_state(self) -> None:
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.last_available = state.get("last_available")
            logging.info("Loaded state: last_available=%s", self.last_available)
        except FileNotFoundError:
            logging.info("No state file found, starting fresh")
        except Exception as exc:
            logging.warning("Failed to load state file: %s", exc)

    def _save_state(self) -> None:
        state = {"last_available": self.last_available, "updated_at": int(time.time())}
        with open(self.cfg.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f)

    def _notify(self, text: str) -> None:
        if not self.chat_id:
            logging.warning("No Telegram chat_id configured/discovered yet; message skipped: %s", text)
            return
        try:
            self.telegram.send_message(self.chat_id, text)
        except Exception as exc:
            logging.exception("Failed to send Telegram message: %s", exc)

    def _try_discover_chat_id(self) -> None:
        if self.chat_id:
            return
        try:
            discovered = self.telegram.discover_chat_id()
            if discovered:
                self.chat_id = discovered
                logging.info("Discovered Telegram chat_id: %s", self.chat_id)
                self._notify(
                    "Bot connected to this channel/chat. Slot monitor is now able to send alerts."
                )
        except Exception as exc:
            logging.warning("Failed to discover chat_id via getUpdates: %s", exc)

    def _check_slot_availability(self) -> bool:
        session = requests.Session()

        page_resp = session.get(self.cfg.check_url, timeout=self.cfg.request_timeout_seconds)
        page_resp.raise_for_status()

        csrf = session.cookies.get("csrftoken", "")
        headers = {
            "Referer": self.cfg.check_url,
            "X-CSRFToken": csrf,
            "X-Requested-With": "XMLHttpRequest",
        }
        login_resp = session.post(
            self.LOGIN_URL,
            data={"username": self.cfg.runc_username, "password": self.cfg.runc_password},
            headers=headers,
            timeout=self.cfg.request_timeout_seconds,
        )
        login_resp.raise_for_status()

        login_ok = False
        ctype = login_resp.headers.get("content-type", "")
        if "application/json" in ctype:
            try:
                login_ok = bool(login_resp.json().get("success"))
            except Exception:
                login_ok = False

        if not login_ok:
            raise UnrecoverableError("Authentication failed on runc.run (/login/ returned non-success)")

        check_resp = session.get(self.cfg.check_url, timeout=self.cfg.request_timeout_seconds)
        check_resp.raise_for_status()
        html = check_resp.text

        sold_out = self.cfg.sold_out_marker in html
        return not sold_out

    def run(self) -> int:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._load_state()
        self._try_discover_chat_id()
        self._notify("White Nights 42.2 monitor started. Check interval: 60 seconds.")

        while not self.stop_requested:
            try:
                self._try_discover_chat_id()
                available = self._check_slot_availability()
                logging.info("Slot check complete: available=%s", available)

                if available and self.last_available is not True:
                    self._notify(
                        "Registration for 42.2 km (White Nights Marathon) is available.\n"
                        f"Registration link: {self.cfg.check_url}"
                    )

                self.last_available = available
                self._save_state()

            except UnrecoverableError as exc:
                logging.exception("Unrecoverable error")
                self._notify(f"Unrecoverable error: {exc}. Service will stop.")
                return 1
            except Exception as exc:
                logging.exception("Recoverable check error: %s", exc)

            for _ in range(self.cfg.poll_interval_seconds):
                if self.stop_requested:
                    break
                time.sleep(1)

        self._notify("White Nights 42.2 monitor is shutting down.")
        return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        cfg = Config.from_env()
        return SlotMonitor(cfg).run()
    except Exception as exc:
        logging.exception("Fatal startup error: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
