# HYROX Bengaluru 2027 registration watcher

Polls the official HYROX India ticket store every ~15 minutes and, the **first
time** registration for **HYROX Bengaluru 2027** (race weekend May 12–16, 2027 @
BIEC) goes live, posts **one message to your Telegram group**. A state flag
prevents double-posting.

## Why Telegram

Free, official Bot API (rock-solid — doesn't flake), posts to a **group** so all
4 of you see it in one chat, needs **zero access to anyone's account**, and runs
on the free GitHub Actions cloud cron. No VM, no cost, no phone tethered.

## How detection works (and why it's reliable)

HYROX India sells all tickets through a vivenu storefront at
`india.hyrox.co.in`. That page server-renders its live events into a JSON blob,
where each event has a structured `name` and `saleStatus` (e.g. `onSale`). The
watcher fires when an event whose name matches *Bengaluru* shows a buyable
status — far more reliable than scraping button text off the marketing pages.

As of the last check, the store lists only *HYROX Mumbai* (`onSale`) and **no
Bengaluru event**, i.e. correctly "not open yet."

## Files

- `watch.py` — checker + Telegram sender. No deps beyond `requests`.
- `.github/workflows/watch.yml` — runs it on a 15-min cron in GitHub Actions.
- `state.json` — tracks whether the alert already fired.

---

## Setup (one-time, ~5 min)

### 1. Create the bot

1. In Telegram, message **@BotFather** → `/newbot` → follow prompts.
2. It gives you a **token** like `123456:ABC-DEF...`. Keep it secret.

### 2. Create the group + add the bot

1. Make a Telegram group and add your 3 friends.
2. Add your new bot to the group (search its @username, add as member).
3. Send any message in the group (so the bot has an update to read).

### 3. Get the group's chat id

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
TELEGRAM_BOT_TOKEN="123456:ABC-DEF..." ./.venv/bin/python watch.py --get-chat-id
```
It prints the chats the bot can see. Copy your group's **chat id** (a negative
number like `-1001234567890`).

> If nothing shows, send another message in the group and retry. Note: if you
> later "upgrade" the group to a supergroup, the chat id changes — just re-run.

### 4. Test it end-to-end

```bash
export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."
export TELEGRAM_CHAT_ID="-1001234567890"
./.venv/bin/python watch.py --dry-run      # detection only
./.venv/bin/python watch.py --test-send    # posts a real message to the group now
```
If the message lands in the group, you're done wiring it up.

### 5. Put it on GitHub Actions (24/7, free)

1. Create a repo and push this folder (public repo = unlimited free Actions
   minutes; nothing sensitive is committed — the token lives in a secret).
2. Repo → Settings → **Secrets and variables → Actions** → add:
   | Secret | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | your bot token |
   | `TELEGRAM_CHAT_ID` | your group chat id (the negative number) |
3. It runs every 15 min automatically. Test now: Actions → *HYROX Bengaluru 2027
   watcher* → **Run workflow** (leave `dry_run = true` for a no-send check).

When registration opens, the group gets one message, and `state.json` is
committed back so it won't fire again.

## Cost

Zero — Telegram is free, GitHub Actions is free for public repos, polling is free.
