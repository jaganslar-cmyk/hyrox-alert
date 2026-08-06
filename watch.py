#!/usr/bin/env python3
"""
HYROX Bengaluru 2027 registration watcher.

Polls the official HYROX registration pages and, the first time it detects that
registration/tickets have gone live, posts a one-time message to a Telegram group
via the Telegram Bot API. A state file prevents re-alerting.

Designed to run headlessly on a cron (e.g. GitHub Actions every ~15 min).
No third-party deps beyond `requests`.

Config via env vars:
  TELEGRAM_BOT_TOKEN  - the bot token from @BotFather
  TELEGRAM_CHAT_ID    - the target group's chat id (negative number for groups)

Helper: `python watch.py --get-chat-id` lists recent chats the bot can see, so
you can grab the group's chat id after adding the bot and sending one message.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"

# --------------------------------------------------------------------------- #
# Detection strategy
# --------------------------------------------------------------------------- #
# HYROX India sells all tickets through a vivenu storefront at india.hyrox.co.in.
# That page server-renders its live events into a Next.js __NEXT_DATA__ JSON blob,
# where each event is a structured object with a `name` and a `saleStatus`
# (e.g. "onSale"). This is a far more reliable signal than scraping button text
# off the marketing pages (which are cluttered with sibling-event CTAs and
# bundled UI strings like "Sold out" that appear regardless of real state).
#
# We consider registration OPEN when an event whose name matches Bengaluru/
# Bangalore appears with a buyable saleStatus. As a backup we also scan the two
# marketing event pages for a direct link to a live vivenu Bengaluru event.

STOREFRONT_URL = "https://india.hyrox.co.in/"

# Marketing pages used only as a backup signal (looking for a live store link).
BACKUP_URLS = [
    "https://hyrox.com/event/hyrox-bengaluru/",
    "https://hyrox.co.in/event/hyrox-bengaluru/",
]

# Event-name match for the race we care about.
CITY_PATTERN = re.compile(r"beng[au]luru|bangalore", re.IGNORECASE)

# saleStatus values (as seen in vivenu data) that mean tickets are purchasable.
BUYABLE_STATUSES = {"onSale", "on_sale", "available"}

# A live vivenu event-detail link on the storefront looks like /event/<slug>.
STORE_EVENT_LINK = re.compile(
    r"india\.hyrox\.co\.in/event/[a-z0-9\-]*(?:beng[au]luru|bangalore)[a-z0-9\-]*",
    re.IGNORECASE,
)

REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)

REGISTER_LINK = "https://hyrox.com/event/hyrox-bengaluru/"

MESSAGE_BODY = (
    "\U0001F3CB️ HYROX Bengaluru 2027 registration is OPEN!\n"
    "Race weekend: May 12-16, 2027 @ BIEC.\n"
    "Register: " + REGISTER_LINK
)


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def _get(url: str) -> requests.Response:
    return requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": USER_AGENT})


def _extract_events(html: str) -> list[dict]:
    """Deep-walk the Next.js __NEXT_DATA__ blob and return every event-like
    object (any dict carrying a `saleStatus`, or a `name` + `sellStart`)."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []

    events: list[dict] = []

    def walk(o):
        if isinstance(o, dict):
            if "saleStatus" in o or ("name" in o and "sellStart" in o):
                events.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return events


def check_storefront() -> tuple[bool, str]:
    """Primary signal: a Bengaluru event with a buyable saleStatus on the
    HYROX India vivenu storefront."""
    try:
        resp = _get(STOREFRONT_URL)
    except requests.RequestException as e:
        return False, f"[storefront] fetch error: {e}"
    if resp.status_code != 200:
        return False, f"[storefront] HTTP {resp.status_code}"

    events = _extract_events(resp.text)
    listing = [
        (e.get("name"), e.get("saleStatus"), e.get("sellStart"))
        for e in events
        if e.get("name")
    ]
    blr = [
        (name, status)
        for (name, status, _sell) in listing
        if name and CITY_PATTERN.search(name)
    ]
    is_open = any(status in BUYABLE_STATUSES for (_n, status) in blr)

    summary = "; ".join(f"{n!r}={s}" for (n, s, _sell) in listing) or "no events rendered"
    if blr:
        detail = "; ".join(f"{n!r}={s}" for (n, s) in blr)
        evidence = f"[storefront] BENGALURU event(s): {detail} => {'OPEN' if is_open else 'listed-but-not-buyable'}"
    else:
        evidence = f"[storefront] no Bengaluru event yet. Currently listed: {summary}"
    return is_open, evidence


def check_backup() -> tuple[bool, str]:
    """Backup signal: a marketing event page now links directly to a live
    Bengaluru event on the vivenu store."""
    for url in BACKUP_URLS:
        try:
            resp = _get(url)
        except requests.RequestException as e:
            return False, f"[backup {url}] fetch error: {e}"
        if resp.status_code == 200 and STORE_EVENT_LINK.search(resp.text):
            link = STORE_EVENT_LINK.search(resp.text).group(0)
            return True, f"[backup {url}] live store link found: {link}"
        time.sleep(1)
    return False, "[backup] no live Bengaluru store link on marketing pages"


def check_all() -> tuple[bool, list[str]]:
    open_a, ev_a = check_storefront()
    open_b, ev_b = check_backup()
    return (open_a or open_b), [ev_a, ev_b]


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"notified": False, "history": []}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Telegram delivery
# --------------------------------------------------------------------------- #
# We post one message to a Telegram group via the official Bot API. The bot is
# created with @BotFather (gives a token) and added to the group; the group's
# chat id (a negative number) is the delivery target. No per-recipient setup,
# no account access, reliable, and free from the cloud cron.

def _telegram_api(token: str, method: str) -> str:
    return f"https://api.telegram.org/bot{token}/{method}"


def get_config() -> tuple[str, str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        raise SystemExit("Missing TELEGRAM_BOT_TOKEN env var.")
    return token, chat_id


def send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """Post a message to the target Telegram chat/group."""
    try:
        resp = requests.post(
            _telegram_api(token, "sendMessage"),
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": False},
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException as e:
        return False, f"request error: {e}"
    try:
        body = resp.json()
    except ValueError:
        return False, f"HTTP {resp.status_code} {resp.text[:160]}"
    ok = bool(body.get("ok"))
    detail = f"message_id={body['result']['message_id']}" if ok else body.get("description", "")
    return ok, f"HTTP {resp.status_code} {detail}"


def notify(text: str) -> list[str]:
    token, chat_id = get_config()
    if not chat_id:
        raise SystemExit("Missing TELEGRAM_CHAT_ID env var (run --get-chat-id to find it).")
    ok, detail = send_telegram(token, chat_id, text)
    return [f"telegram chat {chat_id}: {'OK' if ok else 'FAIL'} ({detail})"]


def print_chat_ids() -> int:
    """List chats the bot can currently see, to help find the group chat id.
    Add the bot to your group and send one message there first."""
    token, _ = get_config()
    try:
        resp = requests.get(_telegram_api(token, "getUpdates"), timeout=REQUEST_TIMEOUT)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        print(f"error calling getUpdates: {e}")
        return 1
    if not data.get("ok"):
        print(f"Telegram error: {data.get('description')}")
        return 1
    seen = {}
    for upd in data.get("result", []):
        for key in ("message", "channel_post", "my_chat_member"):
            chat = (upd.get(key) or {}).get("chat")
            if chat:
                seen[chat["id"]] = f"{chat.get('title') or chat.get('username') or chat.get('first_name')} ({chat.get('type')})"
    if not seen:
        print("No chats seen yet. Add the bot to your group, send a message there, then retry.")
        return 0
    print("Chats the bot can see:")
    for cid, label in seen.items():
        print(f"  chat_id={cid}  {label}")
    print("\nUse the negative id of your group as TELEGRAM_CHAT_ID.")
    return 0


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    if "--get-chat-id" in sys.argv:
        return print_chat_ids()

    now = datetime.now(timezone.utc).isoformat()
    force_test = "--test-send" in sys.argv
    dry_run = "--dry-run" in sys.argv

    state = load_state()

    if state.get("notified") and not force_test:
        print(f"[{now}] Already notified — nothing to do.")
        return 0

    is_open, evidence = check_all()
    for line in evidence:
        print(f"[{now}] {line}")

    if not is_open and not force_test:
        # Record the check but do not alert.
        state.setdefault("history", []).append({"ts": now, "open": False})
        state["history"] = state["history"][-50:]
        save_state(state)
        print(f"[{now}] Registration not open yet.")
        return 0

    reason = "FORCED TEST" if (force_test and not is_open) else "REGISTRATION OPEN"
    print(f"[{now}] {reason} — sending alerts.")

    if dry_run:
        print(f"[{now}] --dry-run set; would post to Telegram group. Message:\n{MESSAGE_BODY}")
        return 0

    results = notify(MESSAGE_BODY)
    for line in results:
        print(f"[{now}] {line}")

    # Only mark notified on a real open (so a test send doesn't disarm the watch).
    if is_open:
        state["notified"] = True
        state["notified_at"] = now
    state.setdefault("history", []).append({"ts": now, "open": is_open, "sent": True})
    state["history"] = state["history"][-50:]
    save_state(state)
    print(f"[{now}] Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
