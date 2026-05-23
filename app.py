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


def env_or_default(env_name: str, default: str) -> str:
    raw = (os.getenv(env_name) or "").strip()
    return raw if raw else default


def _extract_time_tokens(value: str) -> list[str]:
    return re.findall(r"\d{1,2}:\d{2}", value)


def _normalize_text_token(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _strip_html(value: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", no_tags).strip()


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
    planned_time_labels_by_target: dict[str, list[str]]


@dataclass
class AccountProbeResult:
    status: str  # register|booked|paid
    payment_link: Optional[str] = None
    payment_window_remaining: Optional[str] = None


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
    primary_planned_time_labels_by_target: dict[str, list[str]]
    secondary_planned_time_labels_by_target: dict[str, list[str]]

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        username = os.getenv("RUNC_USERNAME", "").strip()
        password = os.getenv("RUNC_PASSWORD", "").strip()
        if not token:
            raise UnrecoverableError("TELEGRAM_BOT_TOKEN is not set")
        if not username or not password:
            raise UnrecoverableError("RUNC_USERNAME/RUNC_PASSWORD must be set")

        primary_marathon_label = env_or_default("BOOKING_PRIMARY_MARATHON_PACE_LABEL", "3:31-3:45")
        secondary_marathon_label = env_or_default("BOOKING_SECONDARY_MARATHON_PACE_LABEL", "3:56-4:05")
        pace_delta = int(env_or_default("PACE_INFERENCE_SECONDS_FASTER", "10"))

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

        primary_half_labels = parse_labels_env_optional("BOOKING_PRIMARY_HALF_TIME_LABELS")
        if not primary_half_labels:
            primary_half_labels = parse_labels_env_optional("BOOKING_PRIMARY_HALF_PACE_LABELS")
        if not primary_half_labels:
            primary_half_labels = [
                "1:33-1:40",
                "от 1:33 до 1:40",
            ]

        secondary_half_labels = parse_labels_env_optional("BOOKING_SECONDARY_HALF_TIME_LABELS")
        if not secondary_half_labels:
            secondary_half_labels = parse_labels_env_optional("BOOKING_SECONDARY_HALF_PACE_LABELS")
        if not secondary_half_labels:
            secondary_half_labels = [
                "1:40-1:55",
                "от 1:40 до 1:55",
            ]

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
            primary_planned_time_labels_by_target={
                "full": primary_full_labels,
                "half": primary_half_labels,
            },
            secondary_planned_time_labels_by_target={
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
    ORDERS_URL = "https://runc.run/orders/"

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
        self.consecutive_recoverable_errors = 0
        self.last_recoverable_error_signature: Optional[str] = None
        self.recoverable_error_notified_for_streak = False

    def _new_account_state(self) -> dict:
        return {
            "status": "register",
            "order_url": None,
            "payment_window_remaining": None,
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
                planned_time_labels_by_target=self.cfg.primary_planned_time_labels_by_target,
            )
        ]
        if self.cfg.secondary_username and self.cfg.secondary_password:
            accounts.append(
                BookingAccount(
                    account_id="secondary",
                    username=self.cfg.secondary_username,
                    password=self.cfg.secondary_password,
                    planned_time_labels_by_target=self.cfg.secondary_planned_time_labels_by_target,
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
                        "payment_window_remaining": existing.get("payment_window_remaining"),
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

    def _build_startup_runs_state_message(self) -> str:
        lines = ["Startup debug: monitored runs state"]
        for target in self.cfg.targets:
            for account in self.accounts:
                acc_state = self._get_account_state(target, account)
                status = str(acc_state.get("status") or "register")
                registration_link = target.check_url
                line = (
                    f"- {target.title} | account: {account.username} | status: {status}\n"
                    f"  Registration link: {registration_link}"
                )
                if status != "register":
                    payment_link = str(acc_state.get("order_url") or "not set")
                    time_remaining = str(acc_state.get("payment_window_remaining") or "not detected")
                    line += (
                        f"\n  Payment link: {payment_link}\n"
                        f"  Time remaining to pay: {time_remaining}"
                    )
                lines.append(line)
        return "\n".join(lines)

    def _refresh_missing_booked_payment_windows(self) -> None:
        updated = False
        for target in self.cfg.targets:
            for account in self.accounts:
                acc_state = self._get_account_state(target, account)
                if str(acc_state.get("status") or "register") != "booked":
                    continue
                if acc_state.get("payment_window_remaining"):
                    continue
                try:
                    remaining = self._probe_payment_window_remaining(account, target)
                except Exception as exc:
                    logging.info(
                        "Startup refresh could not resolve payment window for %s/%s: %s",
                        target.target_id,
                        account.account_id,
                        exc,
                    )
                    continue
                if remaining:
                    acc_state["payment_window_remaining"] = remaining
                    updated = True
        if updated:
            self._save_state()

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
        # Newer forms expose distance as a select, not as radio labels.
        distance_select = page.locator(
            "select#raceRegDistance, select[name='participant-distance_proxy']"
        ).first
        if distance_select.count() > 0:
            options = page.evaluate(
                """
() => Array.from(
  document.querySelectorAll("select#raceRegDistance option, select[name='participant-distance_proxy'] option")
).map(o => ({ value: o.value || "", text: (o.textContent || "").trim() }))
"""
            )
            for label in target.distance_labels:
                for option in options:
                    value = str(option.get("value") or "").strip()
                    text = str(option.get("text") or "").strip()
                    if not value:
                        continue
                    if label in text or _normalize_text_token(label) in _normalize_text_token(text):
                        distance_select.select_option(value=value)
                        page.evaluate(
                            """
() => {
  const sel = document.querySelector("select#raceRegDistance, select[name='participant-distance_proxy']");
  if (!sel) return;
  sel.dispatchEvent(new Event('input', { bubbles: true }));
  sel.dispatchEvent(new Event('change', { bubbles: true }));
}
"""
                        )
                        logging.info("Selected distance for %s by select option: %s", target.target_id, text)
                        return

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

    def _wait_for_dynamic_planned_time_options(self, page: Page, timeout_ms: int = 8000) -> list[dict]:
        try:
            page.wait_for_function(
                """
() => {
  const sel = document.querySelector("select#raceRegTime, select[name='participant-planned_time_proxy']");
  if (!sel) return false;
  const options = Array.from(sel.querySelectorAll('option'));
  return options.some(o => (o.value || '').trim() !== '');
}
""",
                timeout=timeout_ms,
            )
        except Exception:
            pass
        return page.evaluate(
            """
() => Array.from(
  document.querySelectorAll("select#raceRegTime option, select[name='participant-planned_time_proxy'] option")
).map(o => ({ value: o.value || "", text: (o.textContent || "").trim() }))
"""
        )

    def _select_target_planned_time(self, page: Page, planned_time_labels: list[str], target: RaceTarget) -> None:
        time_select = page.locator(
            "select#raceRegTime, select[name='participant-planned_time_proxy']"
        ).first
        selectable: list[dict] = []
        if time_select.count() > 0:
            for _ in range(4):
                options = self._wait_for_dynamic_planned_time_options(page, timeout_ms=3000)
                selectable = [o for o in options if str(o.get("value") or "").strip()]
                if selectable:
                    break
                page.wait_for_timeout(300)

            for wanted in planned_time_labels:
                wanted_times = _extract_time_tokens(wanted)
                wanted_norm = _normalize_text_token(wanted)
                for option in selectable:
                    value = str(option.get("value") or "").strip()
                    text = str(option.get("text") or "").strip()
                    option_times = _extract_time_tokens(text)
                    option_norm = _normalize_text_token(text)
                    if not value:
                        continue
                    # Match by exact time bounds first, then by normalized text containment.
                    if wanted_times and option_times and wanted_times == option_times:
                        time_select.select_option(value=value)
                        logging.info(
                            "Selected planned time for %s by select option: %s",
                            target.target_id,
                            text,
                        )
                        return
                    if wanted_norm in option_norm or option_norm in wanted_norm:
                        time_select.select_option(value=value)
                        logging.info(
                            "Selected planned time for %s by normalized text: %s",
                            target.target_id,
                            text,
                        )
                        return

        for label in planned_time_labels:
            clicked = self._click_first(
                page,
                [
                    f"label:has-text('{label}')",
                    f"text={label}",
                ],
                timeout_ms=2200,
            )
            if clicked:
                logging.info("Selected planned time for %s by label: %s", target.target_id, label)
                return

        logging.warning(
            "Could not select planned time for %s. Tried labels: %s.",
            target.target_id,
            ", ".join(planned_time_labels),
        )
        available_texts = [str(item.get("text") or "").strip() for item in selectable if str(item.get("value") or "").strip()]
        if available_texts:
            logging.warning(
                "Available planned-time options for %s: %s",
                target.target_id,
                "; ".join(available_texts),
            )
        raise RuntimeError(
            f"Could not select planned time for {target.target_id}. Tried: {', '.join(planned_time_labels)}"
        )

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
        # Fallback for custom-styled hidden checkbox (commonly used on runc forms).
        page.evaluate(
            """
() => {
  const names = ['additional-terms-flag-100-flag'];
  for (const name of names) {
    const el = document.querySelector(`input[name="${name}"]`);
    if (!el) continue;
    el.checked = true;
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }
}
"""
        )

    def _extract_form_error_text(self, page: Page) -> Optional[str]:
        selectors = [
            ".alert",
            ".alert-danger",
            ".error",
            ".errors",
            ".invalid-feedback",
            ".help-block",
            "[class*=error]",
            "[class*=invalid]",
            "[role=alert]",
        ]
        for selector in selectors:
            locator = page.locator(selector)
            count = min(locator.count(), 20)
            for idx in range(count):
                text = " ".join((locator.nth(idx).inner_text() or "").split())
                if text:
                    return text[:400]
        return None

    def _extract_payment_link(self, page: Page) -> Optional[str]:
        current_url = page.url
        if self._is_probable_payment_link(current_url):
            return current_url

        candidates = [
            "a:has-text('Оплатить')",
            "a[href*='/order/']",
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
            full_href = href if href.startswith("http://") or href.startswith("https://") else f"https://runc.run{href}"
            if self._is_probable_payment_link(full_href):
                return full_href
        return None

    def _is_probable_payment_link(self, link: str) -> bool:
        normalized = link.strip().lower().rstrip("/")
        if not normalized:
            return False
        if normalized.endswith("/orders"):
            return False
        if re.search(r"/orders?/[0-9]+($|[/?#])", normalized):
            return True
        if re.search(r"/orders?/[a-z0-9\\-]{6,}($|[/?#])", normalized):
            return True
        if "/payment/" in normalized or "payment?" in normalized:
            return True
        if "/pay/" in normalized or "pay?" in normalized:
            return True
        return False

    def _extract_payment_link_from_html(self, html: str) -> Optional[str]:
        href_match = re.search(
            r"""href=["'](?P<href>(https?://[^"']+|/[^"']*(order|payment|pay)[^"']*))["']""",
            html,
            flags=re.IGNORECASE,
        )
        if not href_match:
            return None
        href = href_match.group("href")
        full_href = href if href.startswith("http://") or href.startswith("https://") else f"https://runc.run{href}"
        if self._is_probable_payment_link(full_href):
            return full_href
        return None

    def _extract_payment_window_remaining(self, text: str) -> Optional[str]:
        normalized = _normalize_text_token(text)
        keyword_windows = [
            r"время для оплаты[^0-9]{0,20}(\d{1,2}:\d{2}(?::\d{2})?)",
            r"время на оплат[уы][^0-9]{0,20}(\d{1,2}:\d{2}(?::\d{2})?)",
            r"до конца оплаты[^0-9]{0,20}(\d{1,2}:\d{2}(?::\d{2})?)",
            r"до окончания оплаты[^0-9]{0,20}(\d{1,2}:\d{2}(?::\d{2})?)",
            r"до удаления заказа[^0-9]{0,20}(\d{1,2}:\d{2}(?::\d{2})?)",
            r"осталось[^0-9]{0,20}(\d{1,2}:\d{2}(?::\d{2})?)",
        ]
        for pattern in keyword_windows:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if match:
                return match.group(1)

        verbose_patterns = [
            r"осталось[^0-9]{0,20}((?:\d+\s*ч(?:ас(?:а|ов)?)?\s*)?(?:\d+\s*мин(?:ут(?:а|ы)?)?\s*)?(?:\d+\s*сек(?:унд(?:а|ы)?)?)?)",
            r"время для оплаты[^0-9]{0,20}((?:\d+\s*ч(?:ас(?:а|ов)?)?\s*)?(?:\d+\s*мин(?:ут(?:а|ы)?)?\s*)?(?:\d+\s*сек(?:унд(?:а|ы)?)?)?)",
            r"время на оплат[уы][^0-9]{0,20}((?:\d+\s*ч(?:ас(?:а|ов)?)?\s*)?(?:\d+\s*мин(?:ут(?:а|ы)?)?\s*)?(?:\d+\s*сек(?:унд(?:а|ы)?)?)?)",
            r"до удаления заказа[^0-9]{0,20}((?:\d+\s*ч(?:ас(?:а|ов)?)?\s*)?(?:\d+\s*мин(?:ут(?:а|ы)?)?\s*)?(?:\d+\s*сек(?:унд(?:а|ы)?)?)?)",
        ]
        for pattern in verbose_patterns:
            match = re.search(pattern, normalized, flags=re.IGNORECASE)
            if not match:
                continue
            candidate = (match.group(1) or "").strip()
            if any(ch.isdigit() for ch in candidate):
                return candidate
        return None

    def _extract_payment_window_remaining_from_html(self, html: str) -> Optional[str]:
        countdown_values = re.findall(r'data-time-countdown=["\'](\d{1,8})["\']', html, flags=re.IGNORECASE)
        if not countdown_values:
            return None

        seconds_candidates: list[int] = []
        for raw in countdown_values:
            try:
                value = int(raw)
            except ValueError:
                continue
            if value > 0:
                seconds_candidates.append(value)
        if not seconds_candidates:
            return None

        # Multiple orders can exist on the page; report the earliest expiry.
        total_seconds = min(seconds_candidates)
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _classify_account_page(
        self,
        target: RaceTarget,
        final_url: str,
        html: str,
        prior_status: str = "register",
    ) -> AccountProbeResult:
        lowered_url = final_url.lower()
        if "/races/" in lowered_url:
            return AccountProbeResult(status="register")

        if target.sold_out_marker and target.sold_out_marker in html:
            return AccountProbeResult(status="register")

        register_markers = [
            "Зарегистрироваться",
            "register",
            "participant-distance_proxy",
            "participant-planned_time_proxy",
            "raceRegDistance",
            "raceRegTime",
        ]
        if any(marker in html for marker in register_markers):
            return AccountProbeResult(status="register")

        payment_link = self._extract_payment_link_from_html(html)
        if payment_link:
            return AccountProbeResult(status="booked", payment_link=payment_link)

        return AccountProbeResult(status="register")

    def _target_order_keywords(self, target: RaceTarget) -> list[str]:
        if target.target_id == "half":
            return [
                "белые ночи",
                "white nights",
                "half marathon",
                "полумарафон",
                "21,1 км",
                "21.1 км",
                "21,1км",
                "21.1км",
            ]
        if target.target_id == "full":
            return ["белые ночи", "42,2 км", "42.2 км", "42 км"]
        return [target.title.lower()]

    def _probe_orders_state(
        self,
        account: BookingAccount,
        target: RaceTarget,
        session: Optional[requests.Session] = None,
    ) -> tuple[str, Optional[str], str, Optional[str]]:
        sess = session or self._create_authenticated_session(account.username, account.password, target.check_url)
        orders_resp = sess.get(self.ORDERS_URL, timeout=self.cfg.request_timeout_seconds)
        orders_resp.raise_for_status()
        text = _normalize_text_token(_strip_html(orders_resp.text))
        payment_window_remaining = self._extract_payment_window_remaining(text)
        if not payment_window_remaining:
            payment_window_remaining = self._extract_payment_window_remaining_from_html(orders_resp.text)

        target_keywords = self._target_order_keywords(target)
        has_target_reference = any(keyword in text for keyword in target_keywords)
        if not has_target_reference:
            return ("other", None, "orders page has no target keywords", None)

        pending_markers = [
            "ожидают оплаты",
            "время для оплаты",
            "оформление заказа",
            "регистрация на забег",
        ]
        paid_markers = [
            "оплачен",
            "оплачено",
            "успешно оплачен",
            "оплата прошла",
        ]
        cancelled_markers = [
            "отменен",
            "отменён",
            "отменена",
            "удален",
            "удалён",
            "автоматически удалены",
        ]

        if any(marker in text for marker in pending_markers):
            return ("booked", self.ORDERS_URL, "orders page indicates pending payment", payment_window_remaining)
        if any(marker in text for marker in paid_markers):
            return ("paid", None, "orders page indicates paid", None)
        if any(marker in text for marker in cancelled_markers):
            return ("cancelled", None, "orders page indicates cancelled/deleted", None)
        return ("other", None, "orders page state unknown", None)

    def _probe_payment_window_remaining(self, account: BookingAccount, target: RaceTarget) -> Optional[str]:
        try:
            order_state, _payment_link, _reason, payment_window_remaining = self._probe_orders_state(account, target)
            if order_state == "booked":
                return payment_window_remaining
        except Exception as exc:
            logging.info(
                "Could not resolve payment window remaining for %s/%s: %s",
                target.target_id,
                account.account_id,
                exc,
            )
        return None

    def _build_booked_notification(self, target: RaceTarget, account: BookingAccount, payment_link: str, payment_window_remaining: Optional[str]) -> str:
        remaining = payment_window_remaining or "not detected yet"
        return (
            f"[{target.title}] Automatic registration submitted for {account.username}.\n"
            f"Payment link: {payment_link}\n"
            f"Time remaining to pay: {remaining}"
        )

    def _probe_account_state(
        self,
        account: BookingAccount,
        target: RaceTarget,
        prior_status: str = "register",
    ) -> AccountProbeResult:
        session = self._create_authenticated_session(account.username, account.password, target.check_url)
        check_resp = session.get(target.check_url, timeout=self.cfg.request_timeout_seconds)
        check_resp.raise_for_status()
        if "/races/" in check_resp.url.lower():
            order_state, payment_link, reason, payment_window_remaining = self._probe_orders_state(account, target, session=session)
            logging.info(
                "Order-state probe for %s/%s: %s (%s)",
                target.target_id,
                account.account_id,
                order_state,
                reason,
            )
            if order_state == "booked":
                return AccountProbeResult(
                    status="booked",
                    payment_link=payment_link,
                    payment_window_remaining=payment_window_remaining,
                )
            if order_state == "paid":
                return AccountProbeResult(status="paid")
            # cancelled/other -> allow checks and booking attempts again.
            return AccountProbeResult(status="register")

        return self._classify_account_page(target, check_resp.url, check_resp.text, prior_status=prior_status)

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

        planned_time_labels = account.planned_time_labels_by_target.get(target.target_id) or []
        if not planned_time_labels:
            raise RuntimeError(
                f"No configured planned time labels for target={target.target_id} account={account.username}"
            )

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
                self._select_target_planned_time(page, planned_time_labels, target)
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
                    return AccountProbeResult(
                        status="booked",
                        payment_link=payment_link,
                        payment_window_remaining=self._probe_payment_window_remaining(account, target),
                    )

                page_html = page.content()
                fallback = self._classify_account_page(target, page.url, page_html, prior_status="register")
                if fallback.status in {"booked", "paid"}:
                    if fallback.status == "booked" and not fallback.payment_window_remaining:
                        fallback.payment_window_remaining = self._probe_payment_window_remaining(account, target)
                    return fallback

                form_error = self._extract_form_error_text(page)
                if form_error:
                    raise RuntimeError(
                        "Registration submitted, but booking/payment state was not detected. "
                        f"Form error: {form_error}"
                    )
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

                probe = self._probe_account_state(
                    account,
                    target,
                    prior_status=str(acc_state.get("status") or "register"),
                )
                acc_state["status"] = probe.status
                if probe.payment_link:
                    acc_state["order_url"] = probe.payment_link
                if probe.payment_window_remaining:
                    acc_state["payment_window_remaining"] = probe.payment_window_remaining

                if probe.status == "register":
                    # Booking expired and form is available again for this target.
                    acc_state["booked_notified"] = False
                    continue

                if probe.status == "booked" and not acc_state.get("booked_notified"):
                    payment_link = acc_state.get("order_url") or target.check_url
                    payment_window_remaining = probe.payment_window_remaining or acc_state.get("payment_window_remaining")
                    if not payment_window_remaining:
                        payment_window_remaining = self._probe_payment_window_remaining(account, target)
                        if payment_window_remaining:
                            acc_state["payment_window_remaining"] = payment_window_remaining
                    self._notify(
                        self._build_booked_notification(
                            target,
                            account,
                            payment_link,
                            payment_window_remaining,
                        )
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
            if result.payment_window_remaining:
                acc_state["payment_window_remaining"] = result.payment_window_remaining

            if result.status == "booked" and not acc_state.get("booked_notified"):
                payment_link = acc_state.get("order_url") or target.check_url
                payment_window_remaining = result.payment_window_remaining or acc_state.get("payment_window_remaining")
                if not payment_window_remaining:
                    payment_window_remaining = self._probe_payment_window_remaining(account, target)
                    if payment_window_remaining:
                        acc_state["payment_window_remaining"] = payment_window_remaining
                self._notify(
                    self._build_booked_notification(
                        target,
                        account,
                        payment_link,
                        payment_window_remaining,
                    )
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
        self._refresh_missing_booked_payment_windows()
        self._notify(self._build_startup_runs_state_message())

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

                # Successful monitor cycle resets recoverable error streak.
                self.consecutive_recoverable_errors = 0
                self.last_recoverable_error_signature = None
                self.recoverable_error_notified_for_streak = False

            except UnrecoverableError as exc:
                logging.exception("Unrecoverable error")
                self._notify(f"Unrecoverable error: {exc}. Service will stop.")
                return 1
            except Exception as exc:
                logging.exception("Recoverable check error: %s", exc)
                error_signature = f"{type(exc).__name__}: {exc}"
                if error_signature == self.last_recoverable_error_signature:
                    self.consecutive_recoverable_errors += 1
                else:
                    self.last_recoverable_error_signature = error_signature
                    self.consecutive_recoverable_errors = 1
                    self.recoverable_error_notified_for_streak = False

                if (
                    self.consecutive_recoverable_errors >= 3
                    and not self.recoverable_error_notified_for_streak
                ):
                    self._notify(
                        "Recoverable error during monitor cycle "
                        f"(consecutive: {self.consecutive_recoverable_errors}): {exc}"
                    )
                    self.recoverable_error_notified_for_streak = True

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
