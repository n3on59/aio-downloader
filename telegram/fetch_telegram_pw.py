#!/usr/bin/env python3
"""
Scrape public Telegram channels with Playwright.
- Scrolls to fetch ALL new messages (no gaps).
- Sorts by time across channels.
- Shows Hijri-Shamsi date & Iran/Tehran time.
Scroll limit:
  - First run (no last_ids.json): 15 scrolls
  - Subsequent runs: 50 scrolls (stops when reaching stored ID)
"""
import asyncio, json, time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
import jdatetime
from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).parent                # telegram/
CHANNELS_FILE = BASE_DIR / "channels.json"
STATE_FILE = BASE_DIR / "last_ids.json"
OUTPUT_FILE = BASE_DIR.parent / "telegram.md"   # repo root
CONTENT_DIR = BASE_DIR / "content"

IRAN_TZ = ZoneInfo("Asia/Tehran")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

# ---- helper functions ----
def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def load_existing_md():
    if OUTPUT_FILE.exists():
        return OUTPUT_FILE.read_text(encoding="utf-8")
    return ""

def save_md(content):
    OUTPUT_FILE.write_text(content, encoding="utf-8")

def download_media(url, channel_name, post_id):
    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    ext = ".jpg"
    if any(k in url.lower() for k in [".mp4", "video", "stream"]):
        ext = ".mp4"
    local_name = f"{channel_name}_{post_id}_{int(time.time())}{ext}"
    local_path = CONTENT_DIR / local_name
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        local_path.write_bytes(resp.content)
        return f"telegram/content/{local_name}"
    except Exception as e:
        print(f"    ⚠️ Media download failed: {e}")
        return None

def convert_to_jalali(utc_dt: datetime) -> str:
    """Convert UTC datetime to Iran timezone and format as Jalali string."""
    local_dt = utc_dt.astimezone(IRAN_TZ)
    jdate = jdatetime.datetime.fromgregorian(datetime=local_dt)
    return jdate.strftime("%Y/%m/%d %H:%M")

# ---- scraping with adaptive scroll limit ----
async def scrape_channel_all(page, channel_name, last_id, max_scrolls):
    """
    Keep scrolling until we reach a message with id <= last_id,
    or we hit max_scrolls (prevents insane history load).
    Returns list of message dicts (newest first) with id > last_id.
    """
    url = f"https://t.me/s/{channel_name}"
    print(f"  🌐 Loading {url} ...")
    await page.goto(url, wait_until="networkidle", timeout=30000)

    try:
        await page.wait_for_selector("[data-post]", timeout=15000)
    except:
        print("    ❌ No messages found on initial page.")
        return []

    all_messages = []
    seen_ids = set()

    for scroll_count in range(1, max_scrolls + 1):
        # Extract all visible messages
        current_msgs = await page.evaluate("""() => {
            const containers = document.querySelectorAll('[data-post]');
            const msgs = [];
            containers.forEach(el => {
                const dataPost = el.getAttribute('data-post');
                if (!dataPost) return;
                const parts = dataPost.split('/');
                if (parts.length < 2) return;
                const channel = parts[0];
                const postId = parseInt(parts[1]);
                if (isNaN(postId)) return;

                const timeEl = el.querySelector('time');
                const datetime = timeEl ? timeEl.getAttribute('datetime') : '';

                const textEl = el.querySelector('.tgme_widget_message_text');
                const text = textEl ? textEl.innerText : '';

                let mediaUrl = null, mediaType = null;
                const photoWrap = el.querySelector('.tgme_widget_message_photo_wrap');
                if (photoWrap) {
                    const style = photoWrap.getAttribute('style') || '';
                    const match = style.match(/url\\('(.*?)'\\)/);
                    if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
                }
                if (!mediaUrl) {
                    const videoTag = el.querySelector('video');
                    if (videoTag && videoTag.src) { mediaUrl = videoTag.src; mediaType = 'video'; }
                }
                if (!mediaUrl) {
                    const linkPhoto = el.querySelector('a.tgme_widget_message_photo_wrap');
                    if (linkPhoto) {
                        const style = linkPhoto.getAttribute('style') || '';
                        const match = style.match(/url\\('(.*?)'\\)/);
                        if (match) { mediaUrl = match[1]; mediaType = 'photo'; }
                    }
                }

                msgs.push({
                    id: postId,
                    datetime: datetime,
                    text: text,
                    media_url: mediaUrl,
                    media_type: mediaType
                });
            });
            return msgs;
        }""")

        new_added = 0
        for m in current_msgs:
            if m["id"] not in seen_ids:
                seen_ids.add(m["id"])
                all_messages.append(m)
                new_added += 1

        print(f"    Scroll {scroll_count}: total unique={len(all_messages)}, new this scroll={new_added}")

        # Stop condition: we have messages and the oldest ones are no longer new
        if all_messages:
            oldest_id = min(msg["id"] for msg in all_messages)
            if oldest_id <= last_id:
                print(f"    Reached last_id ({last_id}) – stopping scroll.")
                break

        if new_added == 0:
            print("    No new messages added – end of history.")
            break

        # Scroll down to load older messages
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

        try:
            await page.wait_for_function(
                f"document.querySelectorAll('[data-post]').length > {len(seen_ids)}",
                timeout=5000
            )
        except:
            print("    No further messages loaded after scroll.")
            break

    # Filter and sort newest first
    filtered = [m for m in all_messages if m["id"] > last_id]
    filtered.sort(key=lambda x: x["id"], reverse=True)
    return filtered

async def main():
    channels = load_channels()
    state = load_state()
    is_first_run = not state

    # Set scroll limit: first run = 15, later runs = 50
    scroll_limit = 15 if is_first_run else 50

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        all_messages = []   # will hold dicts with _dt_utc, _channel, etc.

        for ch_name in channels:
            clean_name = ch_name.lstrip("@")
            last_id = state.get(ch_name, 0)

            msgs = await scrape_channel_all(page, clean_name, last_id, max_scrolls=scroll_limit)
            if not msgs:
                print(f"  ℹ️ No new messages for {ch_name}")
                continue

            # Attach parsed UTC datetime and channel name
            for m in msgs:
                dt_utc = datetime(2000,1,1, tzinfo=ZoneInfo("UTC"))
                if m["datetime"]:
                    try:
                        dt_utc = datetime.fromisoformat(m["datetime"]).astimezone(ZoneInfo("UTC"))
                    except:
                        pass
                m["_dt_utc"] = dt_utc
                m["_channel"] = clean_name

            all_messages.extend(msgs)
            print(f"  ✅ {ch_name}: fetched {len(msgs)} new messages (after filter)")

        await browser.close()

    if not all_messages:
        print("ℹ️ No new messages across all channels.")
        if not OUTPUT_FILE.exists():
            save_md("# Telegram Channel Archive\n\n")
        save_state(state)
        return

    # Sort all messages by UTC time, newest first
    all_messages.sort(key=lambda m: m["_dt_utc"], reverse=True)

    all_entries = []
    for msg in all_messages:
        ch = msg["_channel"]
        dt_utc = msg["_dt_utc"]
        media_md = None
        if msg["media_url"]:
            media_md = download_media(msg["media_url"], ch, msg["id"])

        jalali_str = convert_to_jalali(dt_utc)

        header = f"## {jalali_str} — {ch}\n"
        if media_md:
            if msg["media_type"] == "photo":
                header += f"![Photo]({media_md})\n\n"
            else:
                header += f"[🎬 Video]({media_md})\n\n"

        text = msg["text"] or ("📷 Photo" if msg["media_type"] == "photo" else "🎬 Video" if msg["media_type"] == "video" else "")
        lines = text.splitlines()
        quoted = "\n> ".join(lines)
        entry = f"{header}> {quoted}\n\n"
        all_entries.append(entry)

    # Update state per channel
    for ch_name in channels:
        clean_name = ch_name.lstrip("@")
        ch_msgs = [m for m in all_messages if m["_channel"] == clean_name]
        if ch_msgs:
            state[ch_name] = max(m["id"] for m in ch_msgs)

    if not OUTPUT_FILE.exists():
        save_md("# Telegram Channel Archive\n\n")

    if all_entries:
        existing = load_existing_md()
        combined = "".join(all_entries) + existing
        save_md(combined)
        print(f"✅ Added {len(all_entries)} new messages, sorted by time.")
    else:
        print("ℹ️ No new messages to write.")

    save_state(state)

if __name__ == "__main__":
    asyncio.run(main())
