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
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


class UnrecoverableError(Exception):
    pass


@dataclass
class BookingAccount:
    account_id: str
    username: str
    password: str
    pace_labels: list[str]


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
    auto_book_enabled: bool
    booking_distance_labels: list[str]
    primary_pace_labels: list[str]
    secondary_username: Optional[str]
    secondary_password: Optional[str]
    secondary_pace_labels: list[str]
    booking_retry_cooldown_seconds: int
    browser_timeout_ms: int

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        username = os.getenv("RUNC_USERNAME", "").strip()
        password = os.getenv("RUNC_PASSWORD", "").strip()
        if not token:
            raise UnrecoverableError("TELEGRAM_BOT_TOKEN is not set")
        if not username or not password:
            raise UnrecoverableError("RUNC_USERNAME/RUNC_PASSWORD must be set")

        def parse_labels(env_name: str, default: str) -> list[str]:
            raw = os.getenv(env_name, default).strip()
            labels = [item.strip() for item in raw.split("|") if item.strip()]
            return labels if labels else [default]

        labels = parse_labels("BOOKING_DISTANCE_LABELS", "42,2 км|42.2 км|42 км")
        primary_pace_labels = parse_labels("BOOKING_PRIMARY_PACE_LABELS", "3:31-3:45|3:30-3:45|3:31–3:45")
        secondary_pace_labels = parse_labels("BOOKING_SECONDARY_PACE_LABELS", "3:56-4:05|3:55-4:05|3:56–4:05")

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
            auto_book_enabled=os.getenv("AUTO_BOOK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            booking_distance_labels=labels,
            primary_pace_labels=primary_pace_labels,
            secondary_username=(os.getenv("BOOKING_SECONDARY_USERNAME") or "").strip() or None,
            secondary_password=(os.getenv("BOOKING_SECONDARY_PASSWORD") or "").strip() or None,
            secondary_pace_labels=secondary_pace_labels,
            booking_retry_cooldown_seconds=int(os.getenv("BOOKING_RETRY_COOLDOWN_SECONDS", "30")),
            browser_timeout_ms=int(os.getenv("BROWSER_TIMEOUT_MS", "30000")),
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
        self.accounts = self._build_accounts()
        self.account_state: dict[str, dict] = {
            account.account_id: {"completed": False, "order_url": None, "last_attempt_ts": 0}
            for account in self.accounts
        }

    def _build_accounts(self) -> list[BookingAccount]:
        accounts = [
            BookingAccount(
                account_id="primary",
                username=self.cfg.runc_username,
                password=self.cfg.runc_password,
                pace_labels=self.cfg.primary_pace_labels,
            )
        ]
        if self.cfg.secondary_username and self.cfg.secondary_password:
            accounts.append(
                BookingAccount(
                    account_id="secondary",
                    username=self.cfg.secondary_username,
                    password=self.cfg.secondary_password,
                    pace_labels=self.cfg.secondary_pace_labels,
                )
            )
        return accounts

    def _signal_handler(self, signum, _frame) -> None:
        logging.info("Received signal %s, stopping", signum)
        self.stop_requested = True

    def _load_state(self) -> None:
        try:
            with open(self.cfg.state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
            self.last_available = state.get("last_available")
            account_state = state.get("account_state", {})
            for account in self.accounts:
                existing = account_state.get(account.account_id, {})
                self.account_state[account.account_id] = {
                    "completed": bool(existing.get("completed", False)),
                    "order_url": existing.get("order_url"),
                    "last_attempt_ts": int(existing.get("last_attempt_ts", 0)),
                }
            logging.info(
                "Loaded state: last_available=%s account_state=%s",
                self.last_available,
                self.account_state,
            )
        except FileNotFoundError:
            logging.info("No state file found, starting fresh")
        except Exception as exc:
            logging.warning("Failed to load state file: %s", exc)

    def _save_state(self) -> None:
        state = {
            "last_available": self.last_available,
            "account_state": self.account_state,
            "updated_at": int(time.time()),
        }
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

    def _create_authenticated_session(self, username: str, password: str) -> requests.Session:
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
            data={"username": username, "password": password},
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

        return session

    def _check_slot_availability(self) -> bool:
        session = self._create_authenticated_session(self.cfg.runc_username, self.cfg.runc_password)

        check_resp = session.get(self.cfg.check_url, timeout=self.cfg.request_timeout_seconds)
        check_resp.raise_for_status()
        html = check_resp.text

        sold_out = self.cfg.sold_out_marker in html
        return not sold_out

    def _click_first(self, page: Page, selectors: list[str], timeout_ms: int = 4000) -> bool:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                locator.wait_for(state="visible", timeout=timeout_ms)
                locator.click()
                return True
            except Exception:
                continue
        return False

    def _select_target_distance(self, page: Page) -> None:
        for label in self.cfg.booking_distance_labels:
            clicked = self._click_first(
                page,
                [
                    f"label:has-text('{label}')",
                    f"text={label}",
                ],
                timeout_ms=2500,
            )
            if clicked:
                logging.info("Selected distance by label: %s", label)
                return
        raise RuntimeError(
            f"Could not select target distance. Tried labels: {', '.join(self.cfg.booking_distance_labels)}"
        )

    def _select_target_pace(self, page: Page, pace_labels: list[str]) -> None:
        for label in pace_labels:
            clicked = self._click_first(
                page,
                [
                    f"label:has-text('{label}')",
                    f"text={label}",
                ],
                timeout_ms=2200,
            )
            if clicked:
                logging.info("Selected pace by label: %s", label)
                return
        raise RuntimeError(f"Could not select target pace. Tried labels: {', '.join(pace_labels)}")

    def _accept_required_consents(self, page: Page) -> None:
        consent_phrases = [
            "Я прочитал и согласен с правилами соревнования",
            "офертой",
            "инструкцией по технике безопасности",
        ]
        for phrase in consent_phrases:
            self._click_first(
                page,
                [
                    f"label:has-text('{phrase}')",
                    f"text={phrase}",
                ],
                timeout_ms=1500,
            )

    def _extract_payment_link(self, page: Page) -> Optional[str]:
        current_url = page.url
        if any(part in current_url for part in ["/order", "/orders", "/payment", "pay"]):
            return current_url

        candidates = [
            "a:has-text('Оплатить')",
            "a[href*='order']",
            "a[href*='payment']",
            "a[href*='pay']",
        ]
        for selector in candidates:
            locator = page.locator(selector).first
            if locator.count() == 0:
                continue
            href = locator.get_attribute("href")
            if not href:
                continue
            if href.startswith("http://") or href.startswith("https://"):
                return href
            return f"https://runc.run{href}"
        return None

    def _book_slot_and_get_payment_link(self, account: BookingAccount) -> str:
        session = self._create_authenticated_session(account.username, account.password)
        cookies = []
        for cookie in session.cookies:
            cookies.append(
                {
                    "name": cookie.name,
                    "value": cookie.value,
                    "domain": cookie.domain or "runc.run",
                    "path": cookie.path or "/",
                }
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.cfg.browser_timeout_ms)

            try:
                if cookies:
                    context.add_cookies(cookies)
                page.goto(self.cfg.check_url, wait_until="domcontentloaded", timeout=self.cfg.browser_timeout_ms)
                self._select_target_distance(page)
                self._select_target_pace(page, account.pace_labels)
                self._accept_required_consents(page)

                if not self._click_first(
                    page,
                    [
                        "button:has-text('Зарегистрироваться')",
                        "input[type='submit'][value*='Зарегистрироваться']",
                    ],
                    timeout_ms=6000,
                ):
                    raise RuntimeError("Could not find/click 'Зарегистрироваться' button")

                page.wait_for_timeout(3000)
                payment_link = self._extract_payment_link(page)
                if not payment_link:
                    raise RuntimeError("Registration submitted, but payment link could not be detected")
                return payment_link
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(f"Browser timeout during booking flow: {exc}") from exc
            finally:
                context.close()
                browser.close()

    def _maybe_auto_book(self) -> None:
        if not self.cfg.auto_book_enabled:
            return

        all_completed = all(
            bool(self.account_state[account.account_id].get("completed")) for account in self.accounts
        )
        if all_completed:
            return

        now = int(time.time())
        cooldown = self.cfg.booking_retry_cooldown_seconds
        due_accounts = []
        for account in self.accounts:
            acc_state = self.account_state[account.account_id]
            if acc_state.get("completed"):
                continue
            last_attempt_ts = int(acc_state.get("last_attempt_ts", 0))
            if now - last_attempt_ts >= cooldown:
                due_accounts.append(account)

        if not due_accounts:
            return

        self._notify("Slot is available. Starting automatic booking attempts.")

        for account in due_accounts:
            acc_state = self.account_state[account.account_id]
            acc_state["last_attempt_ts"] = now
            self._save_state()
            self._notify(f"Trying booking for account {account.username}...")

            payment_link = self._book_slot_and_get_payment_link(account)
            acc_state["completed"] = True
            acc_state["order_url"] = payment_link
            self._save_state()
            self._notify(
                f"Automatic registration submitted for {account.username}.\n"
                f"Payment link: {payment_link}"
            )

    def run(self) -> int:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._load_state()
        self._try_discover_chat_id()
        self._notify(
            f"White Nights 42.2 monitor started. Check interval: {self.cfg.poll_interval_seconds} seconds."
        )
        if self.cfg.auto_book_enabled:
            account_list = ", ".join(account.username for account in self.accounts)
            self._notify(f"Automatic booking is enabled for accounts: {account_list}")

        while not self.stop_requested:
            try:
                self._try_discover_chat_id()
                available = self._check_slot_availability()
                logging.info("Slot check complete: available=%s", available)

                if available and self.last_available is not True:
                    self._notify(
                        "Registration for target distance is available.\n"
                        f"Registration link: {self.cfg.check_url}"
                    )

                self.last_available = available
                self._save_state()

                if available:
                    self._maybe_auto_book()

            except UnrecoverableError as exc:
                logging.exception("Unrecoverable error")
                self._notify(f"Unrecoverable error: {exc}. Service will stop.")
                return 1
            except Exception as exc:
                logging.exception("Recoverable check error: %s", exc)
                self._notify(f"Recoverable error during monitor cycle: {exc}")

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
