#!/usr/bin/env python3
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


class UnrecoverableError(Exception):
    pass


def _normalize_dash(value: str) -> str:
    return value.replace("–", "-").replace("—", "-")


def _parse_mmss(value: str) -> int:
    minute_part, second_part = value.split(":", 1)
    minutes = int(minute_part)
    seconds = int(second_part)
    if minutes < 0 or seconds < 0 or seconds > 59:
        raise ValueError(f"Invalid pace value: {value}")
    return minutes * 60 + seconds


def _format_mmss(total_seconds: int) -> str:
    bounded = max(60, total_seconds)
    minutes = bounded // 60
    seconds = bounded % 60
    return f"{minutes}:{seconds:02d}"


def infer_half_marathon_pace_labels(marathon_label: str, faster_seconds: int) -> list[str]:
    normalized = _normalize_dash(marathon_label)
    match = re.search(r"(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})", normalized)
    if not match:
        return [marathon_label]

    start = _parse_mmss(match.group(1))
    end = _parse_mmss(match.group(2))
    if end < start:
        start, end = end, start

    target_start = _format_mmss(start - faster_seconds)
    target_end = _format_mmss(end - faster_seconds)
    return [
        f"{target_start}-{target_end}",
        f"{target_start}–{target_end}",
    ]


def parse_labels_env_optional(env_name: str) -> list[str]:
    raw = (os.getenv(env_name) or "").strip()
    if not raw:
        return []
    return [item.strip() for item in raw.split("|") if item.strip()]


def parse_labels_env_with_default(env_name: str, default: str) -> list[str]:
    raw = os.getenv(env_name, default).strip()
    labels = [item.strip() for item in raw.split("|") if item.strip()]
    return labels if labels else [default]


@dataclass
class RaceTarget:
    target_id: str
    title: str
    check_url: str
    sold_out_marker: str
    distance_labels: list[str]


@dataclass
class BookingAccount:
    account_id: str
    username: str
    password: str
    pace_labels_by_target: dict[str, list[str]]


@dataclass
class AccountProbeResult:
    status: str  # register|booked|paid
    payment_link: Optional[str] = None


@dataclass
class Config:
    telegram_bot_token: str
    telegram_chat_id: Optional[str]
    runc_username: str
    runc_password: str
    poll_interval_seconds: int
    request_timeout_seconds: int
    state_file: str
    auto_book_enabled: bool
    secondary_username: Optional[str]
    secondary_password: Optional[str]
    booking_retry_cooldown_seconds: int
    browser_timeout_ms: int
    check_max_retries: int
    check_retry_delay_seconds: int
    targets: list[RaceTarget]
    primary_pace_labels_by_target: dict[str, list[str]]
    secondary_pace_labels_by_target: dict[str, list[str]]

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        username = os.getenv("RUNC_USERNAME", "").strip()
        password = os.getenv("RUNC_PASSWORD", "").strip()
        if not token:
            raise UnrecoverableError("TELEGRAM_BOT_TOKEN is not set")
        if not username or not password:
            raise UnrecoverableError("RUNC_USERNAME/RUNC_PASSWORD must be set")

        primary_marathon_label = os.getenv("BOOKING_PRIMARY_MARATHON_PACE_LABEL", "3:31-3:45").strip()
        secondary_marathon_label = os.getenv("BOOKING_SECONDARY_MARATHON_PACE_LABEL", "3:56-4:05").strip()
        pace_delta = int(os.getenv("PACE_INFERENCE_SECONDS_FASTER", "10"))

        default_primary_full_labels = [primary_marathon_label, _normalize_dash(primary_marathon_label).replace("-", "–")]
        default_secondary_full_labels = [secondary_marathon_label, _normalize_dash(secondary_marathon_label).replace("-", "–")]

        primary_full_labels = parse_labels_env_optional("BOOKING_PRIMARY_FULL_PACE_LABELS")
        if not primary_full_labels:
            primary_full_labels = parse_labels_env_optional("BOOKING_PRIMARY_MARATHON_PACE_LABELS")
        if not primary_full_labels:
            primary_full_labels = default_primary_full_labels

        secondary_full_labels = parse_labels_env_optional("BOOKING_SECONDARY_FULL_PACE_LABELS")
        if not secondary_full_labels:
            secondary_full_labels = parse_labels_env_optional("BOOKING_SECONDARY_MARATHON_PACE_LABELS")
        if not secondary_full_labels:
            secondary_full_labels = default_secondary_full_labels

        primary_half_labels = parse_labels_env_optional("BOOKING_PRIMARY_HALF_PACE_LABELS")
        if not primary_half_labels:
            # Backward-compatible override name.
            primary_half_labels = parse_labels_env_optional("BOOKING_PRIMARY_PACE_LABELS")
        if not primary_half_labels:
            primary_half_labels = infer_half_marathon_pace_labels(primary_marathon_label, pace_delta)

        secondary_half_labels = parse_labels_env_optional("BOOKING_SECONDARY_HALF_PACE_LABELS")
        if not secondary_half_labels:
            # Backward-compatible override name.
            secondary_half_labels = parse_labels_env_optional("BOOKING_SECONDARY_PACE_LABELS")
        if not secondary_half_labels:
            secondary_half_labels = infer_half_marathon_pace_labels(secondary_marathon_label, pace_delta)

        targets = [
            RaceTarget(
                target_id="full",
                title="Full marathon 42.2 km",
                check_url=os.getenv("FULL_CHECK_URL", "https://runc.run/check-in/217/").strip(),
                sold_out_marker=os.getenv(
                    "FULL_SOLD_OUT_MARKER",
                    "Свободные места на 42,2 км закончились",
                ).strip(),
                distance_labels=parse_labels_env_with_default(
                    "FULL_BOOKING_DISTANCE_LABELS",
                    "42,2 км|42.2 км|42 км",
                ),
            ),
            RaceTarget(
                target_id="half",
                title="Half marathon 21.1 km",
                check_url=os.getenv("HALF_CHECK_URL", "https://runc.run/check-in/228/").strip(),
                sold_out_marker=os.getenv(
                    "HALF_SOLD_OUT_MARKER",
                    "Свободные места на 21,1 км закончились",
                ).strip(),
                distance_labels=parse_labels_env_with_default(
                    "HALF_BOOKING_DISTANCE_LABELS",
                    "21,1 км|21.1 км|21 км",
                ),
            ),
        ]

        return cls(
            telegram_bot_token=token,
            telegram_chat_id=(os.getenv("TELEGRAM_CHAT_ID") or "").strip() or None,
            runc_username=username,
            runc_password=password,
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            request_timeout_seconds=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
            state_file=os.getenv("STATE_FILE", "/root/check-running-bot/state.json").strip(),
            auto_book_enabled=os.getenv("AUTO_BOOK_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
            secondary_username=(os.getenv("BOOKING_SECONDARY_USERNAME") or "").strip() or None,
            secondary_password=(os.getenv("BOOKING_SECONDARY_PASSWORD") or "").strip() or None,
            booking_retry_cooldown_seconds=int(os.getenv("BOOKING_RETRY_COOLDOWN_SECONDS", "30")),
            browser_timeout_ms=int(os.getenv("BROWSER_TIMEOUT_MS", "30000")),
            check_max_retries=int(os.getenv("CHECK_MAX_RETRIES", "3")),
            check_retry_delay_seconds=int(os.getenv("CHECK_RETRY_DELAY_SECONDS", "2")),
            targets=targets,
            primary_pace_labels_by_target={
                "full": primary_full_labels,
                "half": primary_half_labels,
            },
            secondary_pace_labels_by_target={
                "full": secondary_full_labels,
                "half": secondary_half_labels,
            },
        )


class TelegramClient:
    def __init__(self, token: str, timeout_seconds: int) -> None:
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.update_offset: Optional[int] = None
        self.bot_username: Optional[str] = None

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

    def get_bot_username(self) -> Optional[str]:
        if self.bot_username:
            return self.bot_username
        data = self._request("getMe", {})
        result = data.get("result") or {}
        username = result.get("username")
        if username:
            self.bot_username = str(username)
        return self.bot_username

    def get_updates(self, allowed_updates: Optional[list[str]] = None) -> list[dict]:
        payload = {
            "timeout": 1,
            "allowed_updates": allowed_updates or ["channel_post", "my_chat_member", "message"],
        }
        if self.update_offset is not None:
            payload["offset"] = self.update_offset

        data = self._request("getUpdates", payload)
        updates = data.get("result", [])
        for item in updates:
            update_id = item.get("update_id")
            if update_id is not None:
                self.update_offset = int(update_id) + 1
        return updates


class SlotMonitor:
    LOGIN_URL = "https://runc.run/login/"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.telegram = TelegramClient(cfg.telegram_bot_token, cfg.request_timeout_seconds)
        self.stop_requested = False
        self.chat_id = cfg.telegram_chat_id
        self.bot_username: Optional[str] = None
        self.target_by_id: dict[str, RaceTarget] = {target.target_id: target for target in cfg.targets}
        self.last_available_by_target: dict[str, Optional[bool]] = {target.target_id: None for target in cfg.targets}
        self.attempt_history: list[dict] = []
        self.accounts = self._build_accounts()
        self.account_state: dict[str, dict[str, dict]] = {
            target.target_id: {account.account_id: self._new_account_state() for account in self.accounts}
            for target in cfg.targets
        }

    def _new_account_state(self) -> dict:
        return {
            "status": "register",
            "order_url": None,
            "last_attempt_ts": 0,
            "booked_notified": False,
            "paid_notified": False,
        }

    def _build_accounts(self) -> list[BookingAccount]:
        accounts = [
            BookingAccount(
                account_id="primary",
                username=self.cfg.runc_username,
                password=self.cfg.runc_password,
                pace_labels_by_target=self.cfg.primary_pace_labels_by_target,
            )
        ]
        if self.cfg.secondary_username and self.cfg.secondary_password:
            accounts.append(
                BookingAccount(
                    account_id="secondary",
                    username=self.cfg.secondary_username,
                    password=self.cfg.secondary_password,
                    pace_labels_by_target=self.cfg.secondary_pace_labels_by_target,
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

            loaded_last = state.get("last_available_by_target")
            if isinstance(loaded_last, dict):
                for target in self.cfg.targets:
                    self.last_available_by_target[target.target_id] = loaded_last.get(target.target_id)
            else:
                legacy_last = state.get("last_available")
                # Legacy state was single-target (half). Preserve if present.
                if "half" in self.last_available_by_target:
                    self.last_available_by_target["half"] = legacy_last

            self.attempt_history = state.get("attempt_history", [])
            loaded_account_state = state.get("account_state", {})

            # Legacy compatibility: account_state used to be a flat dict keyed by account_id.
            if loaded_account_state and any(key in {"primary", "secondary"} for key in loaded_account_state.keys()):
                loaded_account_state = {"half": loaded_account_state}

            for target in self.cfg.targets:
                per_target = loaded_account_state.get(target.target_id, {})
                for account in self.accounts:
                    existing = per_target.get(account.account_id, {})
                    status = str(existing.get("status") or "").strip().lower()
                    if status not in {"register", "booked", "paid"}:
                        status = "booked" if bool(existing.get("completed", False)) else "register"
                    self.account_state[target.target_id][account.account_id] = {
                        "status": status,
                        "order_url": existing.get("order_url"),
                        "last_attempt_ts": int(existing.get("last_attempt_ts", 0)),
                        "booked_notified": bool(existing.get("booked_notified", False)),
                        "paid_notified": bool(existing.get("paid_notified", False)),
                    }

            logging.info(
                "Loaded state: last_available_by_target=%s account_state=%s",
                self.last_available_by_target,
                self.account_state,
            )
        except FileNotFoundError:
            logging.info("No state file found, starting fresh")
        except Exception as exc:
            logging.warning("Failed to load state file: %s", exc)

    def _save_state(self) -> None:
        state = {
            "last_available_by_target": self.last_available_by_target,
            "attempt_history": self.attempt_history[-100:],
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

    def _record_attempt(self, target: RaceTarget, outcome: str, details: str) -> None:
        self.attempt_history.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "target": target.target_id,
                "outcome": outcome,
                "details": details,
            }
        )
        self.attempt_history = self.attempt_history[-100:]

    def _format_last_attempts(self, limit: int = 8) -> str:
        items = self.attempt_history[-limit:]
        if not items:
            return "No attempts recorded yet."
        lines = ["Last attempts:"]
        for item in items:
            lines.append(
                f"{item.get('ts')} | {item.get('target')} | {item.get('outcome')} | {item.get('details')}"
            )
        return "\n".join(lines)

    def _discover_chat_id_from_updates(self, updates: list[dict]) -> None:
        if self.chat_id:
            return
        for item in updates:
            for key in ("channel_post", "my_chat_member", "message"):
                payload = item.get(key) or {}
                chat = payload.get("chat") or {}
                chat_id = chat.get("id")
                if chat_id:
                    self.chat_id = str(chat_id)
                    logging.info("Discovered Telegram chat_id: %s", self.chat_id)
                    self._notify("Bot connected to this channel/chat. Slot monitor is now able to send alerts.")
                    return

    def _handle_commands_from_updates(self, updates: list[dict]) -> None:
        if not self.bot_username:
            return
        mention = f"@{self.bot_username}".lower()
        for item in updates:
            payload = item.get("channel_post") or item.get("message") or {}
            text = str(payload.get("text") or "")
            if not text:
                continue
            lowered = text.lower()
            if mention not in lowered:
                continue
            if not any(word in lowered for word in ("status", "статус", "attempt", "попыт")):
                continue
            chat = payload.get("chat") or {}
            command_chat_id = chat.get("id")
            if command_chat_id:
                self.telegram.send_message(str(command_chat_id), self._format_last_attempts(8))

    def _poll_updates(self) -> None:
        try:
            updates = self.telegram.get_updates(["channel_post", "my_chat_member", "message"])
            if not self.bot_username:
                self.bot_username = self.telegram.get_bot_username()
            self._discover_chat_id_from_updates(updates)
            self._handle_commands_from_updates(updates)
        except Exception as exc:
            logging.warning("Failed to poll updates/commands: %s", exc)

    def _create_authenticated_session(self, username: str, password: str, check_url: str) -> requests.Session:
        session = requests.Session()

        page_resp = session.get(check_url, timeout=self.cfg.request_timeout_seconds)
        page_resp.raise_for_status()

        csrf = session.cookies.get("csrftoken", "")
        headers = {
            "Referer": check_url,
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

    def _get_account_state(self, target: RaceTarget, account: BookingAccount) -> dict:
        return self.account_state[target.target_id][account.account_id]

    def _get_first_registerable_account(self, target: RaceTarget) -> Optional[BookingAccount]:
        for account in self.accounts:
            if self._get_account_state(target, account).get("status") == "register":
                return account
        return None

    def _check_slot_availability(self, account: BookingAccount, target: RaceTarget) -> bool:
        session = self._create_authenticated_session(account.username, account.password, target.check_url)
        check_resp = session.get(target.check_url, timeout=self.cfg.request_timeout_seconds)
        check_resp.raise_for_status()
        html = check_resp.text
        sold_out = target.sold_out_marker in html
        return not sold_out

    def _check_slot_availability_with_retries(self, account: BookingAccount, target: RaceTarget) -> bool:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.cfg.check_max_retries + 1):
            try:
                available = self._check_slot_availability(account, target)
                self._record_attempt(target, "ok", f"attempt={attempt} available={available}")
                return available
            except UnrecoverableError:
                raise
            except Exception as exc:
                last_exc = exc
                self._record_attempt(
                    target,
                    "retry_error",
                    f"attempt={attempt} error={type(exc).__name__}: {exc}",
                )
                if attempt < self.cfg.check_max_retries:
                    time.sleep(self.cfg.check_retry_delay_seconds)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("Slot availability check failed without an error")

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

    def _select_target_distance(self, page: Page, target: RaceTarget) -> None:
        for label in target.distance_labels:
            clicked = self._click_first(
                page,
                [
                    f"label:has-text('{label}')",
                    f"text={label}",
                ],
                timeout_ms=2500,
            )
            if clicked:
                logging.info("Selected distance for %s by label: %s", target.target_id, label)
                return
        raise RuntimeError(
            f"Could not select distance for {target.target_id}. Tried: {', '.join(target.distance_labels)}"
        )

    def _select_target_pace(self, page: Page, pace_labels: list[str], target: RaceTarget) -> None:
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
                logging.info("Selected pace for %s by label: %s", target.target_id, label)
                return
        raise RuntimeError(f"Could not select pace for {target.target_id}. Tried: {', '.join(pace_labels)}")

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

    def _extract_payment_link_from_html(self, html: str) -> Optional[str]:
        href_match = re.search(
            r"""href=["'](?P<href>(https?://[^"']+|/[^"']*(order|payment|pay)[^"']*))["']""",
            html,
            flags=re.IGNORECASE,
        )
        if not href_match:
            return None
        href = href_match.group("href")
        if href.startswith("http://") or href.startswith("https://"):
            return href
        return f"https://runc.run{href}"

    def _classify_account_page(self, target: RaceTarget, final_url: str, html: str) -> AccountProbeResult:
        lowered_url = final_url.lower()
        if "/races/" in lowered_url:
            return AccountProbeResult(status="paid")

        if target.sold_out_marker and target.sold_out_marker in html:
            return AccountProbeResult(status="register")

        register_markers = [
            "Зарегистрироваться",
            "register",
            "name=\"distance\"",
            "name=\"pace\"",
        ]
        if any(marker in html for marker in register_markers):
            return AccountProbeResult(status="register")

        payment_link = self._extract_payment_link_from_html(html)
        booked_markers = [
            "забронир",
            "брон",
            "оплат",
            "payment",
            "order",
        ]
        lowered_html = html.lower()
        if payment_link or any(marker in lowered_html for marker in booked_markers):
            return AccountProbeResult(status="booked", payment_link=payment_link)

        return AccountProbeResult(status="register")

    def _probe_account_state(self, account: BookingAccount, target: RaceTarget) -> AccountProbeResult:
        session = self._create_authenticated_session(account.username, account.password, target.check_url)
        check_resp = session.get(target.check_url, timeout=self.cfg.request_timeout_seconds)
        check_resp.raise_for_status()
        return self._classify_account_page(target, check_resp.url, check_resp.text)

    def _book_slot_and_get_result(self, account: BookingAccount, target: RaceTarget) -> AccountProbeResult:
        session = self._create_authenticated_session(account.username, account.password, target.check_url)
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

        pace_labels = account.pace_labels_by_target.get(target.target_id) or []
        if not pace_labels:
            raise RuntimeError(f"No configured pace labels for target={target.target_id} account={account.username}")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(self.cfg.browser_timeout_ms)

            try:
                if cookies:
                    context.add_cookies(cookies)
                page.goto(target.check_url, wait_until="domcontentloaded", timeout=self.cfg.browser_timeout_ms)
                self._select_target_distance(page, target)
                self._select_target_pace(page, pace_labels, target)
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
                if "/races/" in page.url.lower():
                    return AccountProbeResult(status="paid")

                payment_link = self._extract_payment_link(page)
                if payment_link:
                    return AccountProbeResult(status="booked", payment_link=payment_link)

                page_html = page.content()
                fallback = self._classify_account_page(target, page.url, page_html)
                if fallback.status in {"booked", "paid"}:
                    return fallback

                raise RuntimeError("Registration submitted, but booking/payment state was not detected")
            except PlaywrightTimeoutError as exc:
                raise RuntimeError(f"Browser timeout during booking flow: {exc}") from exc
            finally:
                context.close()
                browser.close()

    def _all_targets_paid(self) -> bool:
        if not self.accounts:
            return False
        for target in self.cfg.targets:
            for account in self.accounts:
                if self._get_account_state(target, account).get("status") != "paid":
                    return False
        return True

    def _has_registerable_accounts(self, target: RaceTarget) -> bool:
        return any(self._get_account_state(target, account).get("status") == "register" for account in self.accounts)

    def _sync_account_states(self) -> None:
        for target in self.cfg.targets:
            for account in self.accounts:
                acc_state = self._get_account_state(target, account)
                if acc_state.get("status") == "paid":
                    continue

                probe = self._probe_account_state(account, target)
                acc_state["status"] = probe.status
                if probe.payment_link:
                    acc_state["order_url"] = probe.payment_link

                if probe.status == "register":
                    # Booking expired and form is available again for this target.
                    acc_state["booked_notified"] = False
                    continue

                if probe.status == "booked" and not acc_state.get("booked_notified"):
                    payment_link = acc_state.get("order_url") or target.check_url
                    self._notify(
                        f"[{target.title}] Automatic registration submitted for {account.username}.\n"
                        f"Payment link: {payment_link}"
                    )
                    acc_state["booked_notified"] = True
                    continue

                if probe.status == "paid" and not acc_state.get("paid_notified"):
                    self._notify(
                        f"[{target.title}] Race for {account.username} is paid (redirected to /races/). "
                        "Slot checking is now stopped for this target/account."
                    )
                    acc_state["paid_notified"] = True
                    acc_state["booked_notified"] = True

    def _maybe_auto_book(self, target: RaceTarget) -> None:
        if not self.cfg.auto_book_enabled:
            return

        now = int(time.time())
        cooldown = self.cfg.booking_retry_cooldown_seconds
        due_accounts = []
        for account in self.accounts:
            acc_state = self._get_account_state(target, account)
            if acc_state.get("status") != "register":
                continue
            last_attempt_ts = int(acc_state.get("last_attempt_ts", 0))
            if now - last_attempt_ts >= cooldown:
                due_accounts.append(account)

        for account in due_accounts:
            acc_state = self._get_account_state(target, account)
            acc_state["last_attempt_ts"] = now
            self._save_state()

            result = self._book_slot_and_get_result(account, target)
            acc_state["status"] = result.status
            if result.payment_link:
                acc_state["order_url"] = result.payment_link

            if result.status == "booked" and not acc_state.get("booked_notified"):
                payment_link = acc_state.get("order_url") or target.check_url
                self._notify(
                    f"[{target.title}] Automatic registration submitted for {account.username}.\n"
                    f"Payment link: {payment_link}"
                )
                acc_state["booked_notified"] = True
            elif result.status == "paid" and not acc_state.get("paid_notified"):
                self._notify(
                    f"[{target.title}] Race for {account.username} is paid (redirected to /races/). "
                    "Slot checking is now stopped for this target/account."
                )
                acc_state["paid_notified"] = True
                acc_state["booked_notified"] = True
            self._save_state()

    def run(self) -> int:
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        self._load_state()
        self._poll_updates()
        self._notify(
            "White Nights monitor started for full (42.2) and half (21.1). "
            f"Check interval: {self.cfg.poll_interval_seconds} seconds."
        )
        if self.cfg.auto_book_enabled:
            account_list = ", ".join(account.username for account in self.accounts)
            self._notify(f"Automatic booking is enabled for accounts: {account_list}")

        while not self.stop_requested:
            try:
                self._poll_updates()
                self._sync_account_states()

                if self._all_targets_paid():
                    self._save_state()
                    logging.info("All targets/accounts are paid. Stopping slot checks.")
                    return 0

                for target in self.cfg.targets:
                    if not self._has_registerable_accounts(target):
                        logging.info("No registerable accounts for %s (booked/pending payment or paid).", target.target_id)
                        continue

                    registerable_account = self._get_first_registerable_account(target)
                    if registerable_account is None:
                        continue

                    available = self._check_slot_availability_with_retries(registerable_account, target)
                    logging.info("Slot check complete for %s: available=%s", target.target_id, available)

                    if available and self.last_available_by_target[target.target_id] is not True:
                        self._notify(
                            f"[{target.title}] Registration is available.\n"
                            f"Registration link: {target.check_url}"
                        )

                    self.last_available_by_target[target.target_id] = available
                    self._save_state()

                    if available:
                        self._maybe_auto_book(target)

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

        self._notify("White Nights monitor is shutting down.")
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
