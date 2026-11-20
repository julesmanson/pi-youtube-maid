"""
YouTube Maid - mass-unsubscribe from YouTube channels, with a review step first.
No API/OAuth - attaches to your real, already-logged-in Chrome via remote debugging
(see launch_chrome_debug.bat) instead of launching a separate browser.

Setup:
  pip install playwright

Workflow:
  1. Run launch_chrome_debug.bat (closes Chrome, reopens it debug-enabled,
     then opens this file automatically).
  Both Generate and Start onboard themselves before doing anything else:
  confirm this Chrome profile is logged into YouTube (aborts cleanly if not;
  proceeds with a warning if that can't be confirmed either way), open the
  configured URL, then click "All subscriptions" if that page offers one.
  2. Set "keep first N", click Generate. This scans every subscribed channel
     and writes unsubscribe_worklist.json listing every channel NAME past
     position N - the candidates for unsubscribing. Nothing is touched yet.
  3. Open unsubscribe_worklist.json and delete any names you want to keep,
     even though they're past position N. Save the file.
  4. Set the delay, then click Start. This only unsubscribes channels whose
     name is still present in unsubscribe_worklist.json, matched against
     the live page - not by position, so it's correct even if your
     subscriptions changed between Generate and Start.

Test Subscribe 10 (testing only, visually set apart from the above): skips
the URL-opening/"All subscriptions" onboarding and goes straight to the
YouTube home page, subscribing to up to 10 channels found there (full
videos only, Shorts skipped by their own video-link URL; the only duplicate
check is each channel's own live "already subscribed" state - it has no
knowledge of unsubscribe_worklist.json or anything else). Once that step
ends - 10 found, feed ran dry, or Stop - it hands off to a normal Generate
run using the same URL/keep-first-N fields, so disposable test channels end
up in the same worklist a real run would use.

Delay before each (sec): an interruptible countdown shown before every
unsubscribe click, so you have a real window to hit Stop before anything
commits. Defaults to 5; set to 0 to skip the wait and go as fast as
YouTube's own UI allows (explicit element waits still pace every click).

While running:
  Stop  - halts the whole run after the current action finishes.
  Skip  - leaves just the current channel in the worklist and moves to the
          next one. Caught as early as possible: during the countdown, or
          failing that, right before the first click, or failing that,
          right before the final confirm click (the only truly irreversible
          one) - whichever point it's still waiting at when you click it.
  Pause - freezes the countdown where it stands (not reset) and holds
          before any click; Resume picks up from the same point.
"""

import json
import time
import random
import threading
from pathlib import Path
import tkinter as tk
from tkinter import ttk
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "maid_settings.json"
WORKLIST_FILE = HERE / "unsubscribe_worklist.json"

DEFAULT_SETTINGS = {
    "url": "https://www.youtube.com/feed/channels",
    "keep_first_n": 24,
    "cdp_url": "http://localhost:9222",
    "delay_seconds": 5.0
}

YOUTUBE_HOME_URL = "https://www.youtube.com"
SIGNED_IN_SELECTOR = "button#avatar-btn"
SIGN_IN_LINK_SELECTOR = "a#sign-in-link, ytd-button-renderer a:has-text('Sign in')"
ALL_SUBSCRIPTIONS_SELECTOR = "a:has-text('All subscriptions')"

CHANNEL_ROW_SELECTOR = "ytd-channel-renderer"
UNSUB_BUTTON_SELECTOR = "button:has-text('Subscribed'), button[aria-label*='Unsubscribe']"
NAME_SELECTOR = "#text, #channel-title, yt-formatted-string#text"
CONFIRM_SELECTOR = (
    "tp-yt-paper-button:has-text('Unsubscribe'), "
    "yt-confirm-dialog-renderer button:has-text('Unsubscribe')"
)

QUEUE_PREVIEW_COUNT = 3  # "up next" rows shown below the current channel
QUEUE_LOOKAHEAD_SCAN_CAP = 60  # rows to scan forward for preview names before giving up

HOME_ITEM_SELECTOR = "ytd-rich-item-renderer"
HOME_VIDEO_LINK_SELECTOR = "a#thumbnail"
HOME_CHANNEL_LINK_SELECTOR = "ytd-channel-name a, #channel-name a"
CHANNEL_SUBSCRIBE_BUTTON_SELECTOR = "ytd-subscribe-button-renderer button, #subscribe-button button"
TEST_SUBSCRIBE_BATCH_SIZE = 10
TEST_SUBSCRIBE_SCROLL_GIVEUP = 10


def load_settings():
    settings = dict(DEFAULT_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            settings.update(json.loads(SETTINGS_FILE.read_text()))
        except Exception:
            pass

    # A negative keep_first_n would make Playwright's .nth() count from the
    # end of the list instead of skipping the top N - the opposite of what
    # this setting exists to guarantee. A hand-edited bad value falls back
    # to the safe default rather than silently doing something dangerous.
    if not isinstance(settings.get("keep_first_n"), int) or settings["keep_first_n"] < 0:
        settings["keep_first_n"] = DEFAULT_SETTINGS["keep_first_n"]

    # Delay is a real number, 0 (no wait) and up - same "bad hand-edited
    # value falls back to default" guarantee as keep_first_n.
    delay = settings.get("delay_seconds")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
        settings["delay_seconds"] = DEFAULT_SETTINGS["delay_seconds"]

    SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    return settings


def save_last_used(url, keep_first_n, cdp_url, delay_seconds):
    SETTINGS_FILE.write_text(json.dumps(
        {
            "url": url,
            "keep_first_n": keep_first_n,
            "cdp_url": cdp_url,
            "delay_seconds": delay_seconds
        },
        indent=2
    ))


class YouTubeMaid:
    def __init__(self, root, settings):
        self.root = root
        root.title("YouTube Maid")
        root.geometry("560x580")
        root.minsize(500, 460)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.stop_event = threading.Event()
        self.skip_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker_thread = None
        self.running = False

        form = ttk.Frame(root, padding=12)
        form.pack(fill="x")

        ttk.Label(form, text="Channels page URL:").grid(row=0, column=0, sticky="w")
        self.url_var = tk.StringVar(value=settings["url"])
        ttk.Entry(form, textvariable=self.url_var, width=42).grid(row=0, column=1, sticky="we", pady=4)

        ttk.Label(form, text="Keep first N from top:").grid(row=1, column=0, sticky="w")
        self.start_at_var = tk.IntVar(value=settings["keep_first_n"])
        self.start_at_spin = ttk.Spinbox(form, from_=0, to=100000, textvariable=self.start_at_var, width=10)
        self.start_at_spin.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(form, text="Delay before each (sec):").grid(row=2, column=0, sticky="w")
        self.delay_var = tk.DoubleVar(value=settings["delay_seconds"])
        self.delay_spin = ttk.Spinbox(form, from_=0, to=300, increment=0.5, format="%.1f",
                                       textvariable=self.delay_var, width=10)
        self.delay_spin.grid(row=2, column=1, sticky="w", pady=4)

        form.columnconfigure(1, weight=1)

        ttk.Label(root, text=f"Generate writes {WORKLIST_FILE.name} - edit it to remove any names you want to keep, then click Start.",
                  wraplength=520, padding=(12, 0)).pack(fill="x")

        btn_frame = ttk.Frame(root, padding=(12, 8))
        btn_frame.pack(fill="x")
        self.generate_btn = ttk.Button(btn_frame, text="Generate", command=self.on_generate)
        self.generate_btn.pack(side="left", padx=4)
        self.start_btn = ttk.Button(btn_frame, text="Start", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_frame, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.skip_btn = ttk.Button(btn_frame, text="Skip", command=self.on_skip, state="disabled")
        self.skip_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(btn_frame, text="Pause", command=self.on_pause_toggle, state="disabled")
        self.pause_btn.pack(side="left", padx=4)

        # Separate row, visually set apart - a testing utility, not part of the
        # main Generate/Start workflow above it.
        test_frame = ttk.Frame(root, padding=(12, 0, 12, 8))
        test_frame.pack(fill="x")
        ttk.Label(test_frame, text="Testing:", foreground="#6b7280").pack(side="left", padx=(0, 6))
        self.test_subscribe_btn = ttk.Button(test_frame, text="Test Subscribe 10", command=self.on_test_subscribe)
        self.test_subscribe_btn.pack(side="left")
        ttk.Label(test_frame, text="subscribes to up to 10 new channels from the YouTube home page "
                  "(full videos only, skips Shorts and already-subscribed channels), then runs a normal "
                  "Generate scan - lets you test Start against disposable channels.",
                  foreground="#6b7280", wraplength=380).pack(side="left", padx=(8, 0))

        # Queue preview: row 0 is the current/in-progress channel (green), rows
        # 1..QUEUE_PREVIEW_COUNT are upcoming worklist matches, muted. The stack
        # pushes up as each one finishes - row 1 becomes row 0, and so on.
        queue_frame = ttk.Frame(root, padding=(12, 4))
        queue_frame.pack(fill="x")

        self.progress_var = tk.StringVar(value="")
        ttk.Label(queue_frame, textvariable=self.progress_var, font=("", 9)).pack(anchor="w")

        self.countdown_var = tk.StringVar(value="")
        ttk.Label(queue_frame, textvariable=self.countdown_var, font=("", 16, "bold")).pack(anchor="w")

        queue_box = tk.Frame(queue_frame, relief="solid", borderwidth=1, background="#ffffff")
        queue_box.pack(fill="x", pady=(2, 0))

        self.queue_labels = []
        for i in range(1 + QUEUE_PREVIEW_COUNT):
            lbl = tk.Label(queue_box, text="(idle)" if i == 0 else "", anchor="w",
                            padx=8, pady=2, background="#ffffff",
                            font=("", 9, "bold" if i == 0 else "normal"),
                            foreground="#1a7f37" if i == 0 else "#6b7280")
            lbl.pack(fill="x")
            self.queue_labels.append(lbl)

        footer = ttk.Frame(root, padding=(12, 2))
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, text="Developed by Jules Manson and Claude Sonnet 5.",
                  font=("", 8), foreground="#6b7280").pack(anchor="center")

        log_frame = ttk.Frame(root, padding=12)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, height=16, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, side="left")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scrollbar.set)

        self.log("Ready. Set the number, click Generate, review the file it writes, then click Start.")

    def log(self, message):
        self.root.after(0, lambda: self._append_log(message))

    def _append_log(self, message):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def set_progress(self, processed, remaining_count, total):
        text = f"{processed} unsubscribed - {remaining_count} of {total} remaining"
        self.root.after(0, lambda: self.progress_var.set(text))

    def set_status(self, current="", upcoming=None, countdown=""):
        self.root.after(0, lambda: self._render_status(current, upcoming or [], countdown))

    def _render_status(self, current, upcoming, countdown):
        self.countdown_var.set(countdown)
        rows = [current] + list(upcoming)
        for i, lbl in enumerate(self.queue_labels):
            if i < len(rows) and rows[i]:
                lbl.config(text=rows[i])
            else:
                lbl.config(text="(idle)" if i == 0 else "")
            if i == 0:
                lbl.config(foreground="#1a7f37")  # green - also clears a prior error's red

    def set_error(self, message, suggestion):
        self.root.after(0, lambda: self._render_error(f"{message} - {suggestion}"))

    def _render_error(self, text):
        self.countdown_var.set("")
        self.queue_labels[0].config(text=text, foreground="#b91c1c")  # red
        for lbl in self.queue_labels[1:]:
            lbl.config(text="")

    @staticmethod
    def _format_seconds(seconds):
        return f"{seconds:g}"

    def _countdown(self, channel_name, upcoming, seconds):
        # Returns "stop"/"skip"/"ready". Pause freezes `remaining` rather than
        # resetting it - ticking resumes from the same point once unpaused.
        remaining = seconds
        while remaining > 0:
            if self.pause_event.is_set():
                self.set_status(current=channel_name, upcoming=upcoming, countdown="paused")
                while self.pause_event.is_set() and not self.stop_event.is_set() and not self.skip_event.is_set():
                    time.sleep(0.2)
                if self.stop_event.is_set():
                    return "stop"
                if self.skip_event.is_set():
                    self.skip_event.clear()
                    return "skip"
                continue
            self.set_status(current=channel_name, upcoming=upcoming,
                             countdown=f"{self._format_seconds(remaining)}s")
            if self.stop_event.is_set():
                return "stop"
            if self.skip_event.is_set():
                self.skip_event.clear()
                return "skip"
            # Ticks every 0.1s regardless of the configured delay, so Stop/Skip
            # land fast and fractional delays (e.g. 2.5) count down smoothly.
            time.sleep(0.1)
            remaining = round(remaining - 0.1, 1)
        self.set_status(current=channel_name, upcoming=upcoming, countdown="")
        if self.stop_event.is_set():
            return "stop"
        if self.skip_event.is_set():
            self.skip_event.clear()
            return "skip"
        return "ready"

    def _lookahead_upcoming(self, rows, position, remaining, exclude):
        # Read-only preview scan - never clicks anything, just inner_text() on
        # rows past the current one, looking for the next worklist matches.
        names = []
        count = rows.count()
        end = min(count, position + 1 + QUEUE_LOOKAHEAD_SCAN_CAP)
        i = position + 1
        while i < end and len(names) < QUEUE_PREVIEW_COUNT:
            try:
                name = rows.nth(i).locator(NAME_SELECTOR).first.inner_text(timeout=800).strip()
            except Exception:
                name = ""
            if name and name != exclude and name in remaining:
                names.append(name)
            i += 1
        return names

    def _check_logged_in(self, page):
        # None means "uncertain" (markup may have changed) rather than a guess either
        # way - callers fail OPEN on None, since blocking the whole app on a stale
        # selector is worse than occasionally proceeding without 100% confirmation.
        if page.locator(SIGN_IN_LINK_SELECTOR).count() > 0:
            return False
        if page.locator(SIGNED_IN_SELECTOR).count() > 0:
            return True
        return None

    def _ensure_logged_in(self, page):
        # Returns False only on a confirmed not-logged-in state - callers abort
        # then. "Uncertain" (markup drift) logs a warning but returns True -
        # proceeding beats blocking the whole app on a guess.
        self.log("Checking YouTube login...")
        page.goto(YOUTUBE_HOME_URL)
        page.wait_for_timeout(1500)

        logged_in = self._check_logged_in(page)
        if logged_in is False:
            self.log("Not logged into YouTube in this Chrome profile - sign in, then try again.")
            return False
        elif logged_in is None:
            self.log("Couldn't confirm YouTube login status (page markup may have changed) - continuing anyway.")
        else:
            self.log("Logged in.")
        return True

    def _onboard_to_subscriptions(self, page, url):
        # Three onboarding steps: confirm login, open the subscriptions-management
        # URL, then best-effort click "All subscriptions" if that page offers one.
        if not self._ensure_logged_in(page):
            return False

        self.log(f"Opening {url} ...")
        page.goto(url)
        page.wait_for_timeout(2000)

        try:
            all_subs = page.locator(ALL_SUBSCRIPTIONS_SELECTOR).first
            if page.locator(ALL_SUBSCRIPTIONS_SELECTOR).count() > 0:
                all_subs.click()
                page.wait_for_timeout(1500)
                self.log("Clicked 'All subscriptions'.")
        except Exception:
            pass  # not on every page layout - absence here is normal, not an error

        return True

    def _read_form(self):
        try:
            start_at = int(self.start_at_var.get())
            if start_at < 0:
                raise ValueError
        except (ValueError, tk.TclError):
            self.log("Keep-first-N must be a whole number, 0 or higher.")
            return None
        url = self.url_var.get().strip()
        if not url:
            self.log("URL is empty.")
            return None
        try:
            delay = float(self.delay_var.get())
            if delay < 0:
                raise ValueError
        except (ValueError, tk.TclError):
            self.log("Delay must be a real number, 0 or higher.")
            return None
        return url, start_at, delay

    def _set_running_ui(self, running, unsubscribing=False):
        state = "disabled" if running else "normal"
        self.generate_btn.config(state=state)
        self.start_btn.config(state=state)
        self.test_subscribe_btn.config(state=state)
        self.start_at_spin.config(state=state)
        self.delay_spin.config(state=state)
        self.stop_btn.config(state="normal" if running else "disabled")
        # Skip/Pause only mean anything mid-unsubscribe - Generate/Test Subscribe have no per-channel action to skip or pause.
        skip_pause_state = "normal" if (running and unsubscribing) else "disabled"
        self.skip_btn.config(state=skip_pause_state)
        self.pause_btn.config(state=skip_pause_state)

    def on_generate(self):
        if self.running:
            return
        form = self._read_form()
        if form is None:
            return
        url, start_at, delay = form

        self.running = True
        self.stop_event.clear()
        self._set_running_ui(True)
        save_last_used(url, start_at, DEFAULT_SETTINGS["cdp_url"], delay)

        self.worker_thread = threading.Thread(
            target=self.run_generate, args=(url, start_at, DEFAULT_SETTINGS["cdp_url"]), daemon=True
        )
        self.worker_thread.start()

    def on_test_subscribe(self):
        if self.running:
            return
        form = self._read_form()
        if form is None:
            return
        url, start_at, delay = form

        self.running = True
        self.stop_event.clear()
        self._set_running_ui(True)
        save_last_used(url, start_at, DEFAULT_SETTINGS["cdp_url"], delay)

        self.worker_thread = threading.Thread(
            target=self.run_test_subscribe, args=(url, start_at, DEFAULT_SETTINGS["cdp_url"]), daemon=True
        )
        self.worker_thread.start()

    def on_start(self):
        if self.running:
            return
        if not WORKLIST_FILE.exists():
            self.log(f"{WORKLIST_FILE.name} doesn't exist yet - click Generate first, review it, then Start.")
            return
        try:
            worklist = json.loads(WORKLIST_FILE.read_text())
            if not isinstance(worklist, list):
                raise ValueError
        except Exception:
            self.log(f"{WORKLIST_FILE.name} isn't valid JSON (should be a list of names) - fix or regenerate it.")
            return
        names = {str(n).strip() for n in worklist if str(n).strip()}
        if not names:
            self.log(f"{WORKLIST_FILE.name} is empty - nothing to unsubscribe. Generate again if that's wrong.")
            return

        form = self._read_form()
        if form is None:
            return
        url, _start_at, delay = form

        self.running = True
        self.stop_event.clear()
        self.skip_event.clear()
        self.pause_event.clear()
        self.pause_btn.config(text="Pause")
        self._set_running_ui(True, unsubscribing=True)
        save_last_used(url, _start_at, DEFAULT_SETTINGS["cdp_url"], delay)

        self.worker_thread = threading.Thread(
            target=self.run_unsubscribe, args=(url, names, DEFAULT_SETTINGS["cdp_url"], delay), daemon=True
        )
        self.worker_thread.start()

    def on_stop(self):
        self.log("Stop requested - finishing current action, then halting...")
        self.stop_event.set()

    def on_skip(self):
        if not self.running:
            return
        self.log("Skip requested - moving to the next channel without unsubscribing this one...")
        self.skip_event.set()

    def on_pause_toggle(self):
        if not self.running:
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="Pause")
            self.log("Resumed.")
        else:
            self.pause_event.set()
            self.pause_btn.config(text="Resume")
            self.log("Paused - will hold before the next action.")

    def on_finished(self):
        self.running = False
        self._set_running_ui(False)
        self._render_status("", [], "")
        self.progress_var.set("")
        self.pause_event.clear()
        self.pause_btn.config(text="Pause")

    def on_close(self):
        self.stop_event.set()
        self.root.after(200, self.root.destroy)

    def run_generate(self, url, start_at, cdp_url):
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(cdp_url)
                except Exception:
                    self.log(f"Could not connect to Chrome at {cdp_url}.")
                    self.log("Run launch_chrome_debug.bat first, then try again.")
                    return

                if len(browser.contexts) > 1:
                    self.log(f"Note: Chrome has {len(browser.contexts)} profile contexts open - using the first one found.")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                if not self._onboard_to_subscriptions(page, url):
                    page.close()
                    return

                self.log("Scanning your subscriptions - scrolling to load the full list, this can take a bit for ~1000 channels...")
                rows = page.locator(CHANNEL_ROW_SELECTOR)
                prev_count = -1
                stable_iterations = 0
                while stable_iterations < 3 and not self.stop_event.is_set():
                    current_count = rows.count()
                    if current_count == prev_count:
                        stable_iterations += 1
                    else:
                        stable_iterations = 0
                        self.set_status(current=f"Loading... {current_count} channels found so far")
                    prev_count = current_count
                    page.mouse.wheel(0, 3000)
                    page.wait_for_timeout(1000)

                if self.stop_event.is_set():
                    self.log("Stopped before the scan finished - no file written.")
                    page.close()
                    return

                total = rows.count()
                if total == 0:
                    self.log("Found zero channels - this likely means YouTube's page layout changed and the "
                              "selectors need updating, not that your subscription list is actually empty.")
                    page.close()
                    return

                names = []
                for i in range(start_at, total):
                    name_el = rows.nth(i).locator(NAME_SELECTOR).first
                    try:
                        name = name_el.inner_text(timeout=1500).strip()
                    except Exception:
                        name = ""
                    names.append(name if name else f"(unnamed channel #{i + 1})")

                WORKLIST_FILE.write_text(json.dumps(names, indent=2, ensure_ascii=False))
                self.log(f"Found {total} channels total, {len(names)} past position {start_at}.")
                self.log(f"Wrote {WORKLIST_FILE.name}. Review it, delete any names you want to keep, then click Start.")
                page.close()

        except Exception as e:
            self.log(f"Fatal error: {e}")
        finally:
            self.root.after(0, self.on_finished)

    def _subscribe_up_to_10_from_home(self, page, context):
        # Read-then-decide per item: only a full-video item (never Shorts, judged
        # by its own thumbnail link href rather than a fragile class/attribute
        # name) gets a real subscribe attempt. The only duplicate check is each
        # channel's own live "already subscribed" state - this method has no
        # knowledge of unsubscribe_worklist.json or anything outside this scan;
        # seen_names below is just this-run dedup (the same channel can appear
        # more than once in the feed) and is never seeded from any file.
        page.goto(YOUTUBE_HOME_URL)
        page.wait_for_timeout(1500)

        seen_names = set()
        subscribed = 0
        index = 0
        scroll_tries = 0

        while subscribed < TEST_SUBSCRIBE_BATCH_SIZE and not self.stop_event.is_set():
            items = page.locator(HOME_ITEM_SELECTOR)
            count = items.count()

            if index >= count:
                if scroll_tries >= TEST_SUBSCRIBE_SCROLL_GIVEUP:
                    self.log("Ran out of home page items before finding 10 new channels to subscribe to.")
                    break
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(1200)
                scroll_tries = 0 if page.locator(HOME_ITEM_SELECTOR).count() != count else scroll_tries + 1
                continue

            item = items.nth(index)
            index += 1

            try:
                href = item.locator(HOME_VIDEO_LINK_SELECTOR).first.get_attribute("href") or ""
            except Exception:
                continue
            if "/shorts/" in href:
                continue  # Shorts - this feature only subscribes from full videos

            try:
                channel_name = item.locator(HOME_CHANNEL_LINK_SELECTOR).first.inner_text(timeout=1000).strip()
                channel_href = item.locator(HOME_CHANNEL_LINK_SELECTOR).first.get_attribute("href")
            except Exception:
                continue
            if not channel_name or not channel_href or channel_name in seen_names:
                continue
            seen_names.add(channel_name)

            channel_url = channel_href if channel_href.startswith("http") else f"https://www.youtube.com{channel_href}"
            self.log(f"Checking channel: {channel_name}")
            chan_page = context.new_page()
            try:
                chan_page.goto(channel_url)
                chan_page.wait_for_timeout(1500)
                btn = chan_page.locator(CHANNEL_SUBSCRIBE_BUTTON_SELECTOR).first
                try:
                    already = "subscribed" in btn.inner_text(timeout=1500).strip().lower()
                except Exception:
                    already = False
                if already:
                    self.log(f"Already subscribed to '{channel_name}' - skipping.")
                else:
                    btn.click()
                    chan_page.wait_for_timeout(1000)
                    subscribed += 1
                    self.log(f"Subscribed #{subscribed}: {channel_name}")
            except Exception as e:
                self.log(f"Couldn't process '{channel_name}': {e}")
            finally:
                chan_page.close()

        self.log(f"Test Subscribe 10: {subscribed} new subscription(s) this run.")
        return subscribed

    def run_test_subscribe(self, url, start_at, cdp_url):
        # Only the subscribe-10-from-home step is unique to this button - once
        # that's done (or skipped/stopped), it hands off to a plain run_generate
        # for everything else, including that method's own on_finished dispatch.
        completed = False
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(cdp_url)
                except Exception:
                    self.log(f"Could not connect to Chrome at {cdp_url}.")
                    self.log("Run launch_chrome_debug.bat first, then try again.")
                    return

                if len(browser.contexts) > 1:
                    self.log(f"Note: Chrome has {len(browser.contexts)} profile contexts open - using the first one found.")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()

                if not self._ensure_logged_in(page):
                    page.close()
                    return

                self._subscribe_up_to_10_from_home(page, context)
                page.close()

                if self.stop_event.is_set():
                    self.log("Stopped before the normal Generate scan started.")
                else:
                    self.log("Continuing with a normal Generate scan...")
                    completed = True

        except Exception as e:
            self.log(f"Fatal error: {e}")
        finally:
            if not completed:
                self.root.after(0, self.on_finished)

        if completed:
            self.run_generate(url, start_at, cdp_url)

    def run_unsubscribe(self, url, worklist_names, cdp_url, delay_seconds):
        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.connect_over_cdp(cdp_url)
                except Exception:
                    self.log(f"Could not connect to Chrome at {cdp_url}.")
                    self.log("Run launch_chrome_debug.bat first, then click Start again.")
                    return

                if len(browser.contexts) > 1:
                    self.log(f"Note: Chrome has {len(browser.contexts)} profile contexts open - using the first one found.")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                if not self._onboard_to_subscriptions(page, url):
                    page.close()
                    return

                remaining = set(worklist_names)
                total = len(remaining)
                self.log(f"{total} channels in the worklist to unsubscribe.")
                self.set_progress(0, total, total)

                processed = 0
                skipped_kept = 0
                position = 0
                stall_count = 0
                name_fail_streak = 0
                error_shown = False
                saw_any_row = False

                while not self.stop_event.is_set() and remaining:
                    # Safe boundary point for Pause - between channels, never mid-click.
                    while self.pause_event.is_set() and not self.stop_event.is_set():
                        self.set_status(current="(paused)")
                        time.sleep(0.2)
                    if self.stop_event.is_set():
                        break

                    rows = page.locator(CHANNEL_ROW_SELECTOR)
                    count = rows.count()
                    if count > 0:
                        saw_any_row = True

                    if position >= count:
                        page.mouse.wheel(0, 2500)
                        page.wait_for_timeout(1200)
                        if rows.count() == count:
                            stall_count += 1
                            if not error_shown:
                                self.set_error("Page isn't loading more channels after scrolling.",
                                                "will retry scrolling up to 3x before giving up for this run.")
                                error_shown = True
                            if stall_count >= 3:
                                break
                        else:
                            stall_count = 0
                            if error_shown:
                                self.log("More channels loaded - resuming.")
                                self.set_status()
                                error_shown = False
                        continue

                    stall_count = 0
                    row = rows.nth(position)
                    name_el = row.locator(NAME_SELECTOR).first
                    try:
                        channel_name = name_el.inner_text(timeout=1500).strip()
                    except Exception:
                        name_fail_streak += 1
                        self.log(f"Row #{position + 1}: channel name not found (selector drift?) - skipping.")
                        if name_fail_streak >= 3 and not error_shown:
                            self.set_error("Can't read channel names for several rows in a row.",
                                            "YouTube's page markup may have changed - NAME_SELECTOR likely needs updating.")
                            error_shown = True
                        position += 1
                        continue

                    if name_fail_streak > 0:
                        name_fail_streak = 0
                        if error_shown:
                            self.log("Channel names readable again - resuming.")
                            self.set_status()
                            error_shown = False

                    if channel_name not in remaining:
                        skipped_kept += 1
                        position += 1
                        continue

                    upcoming = self._lookahead_upcoming(rows, position, remaining, channel_name)
                    self.log(f"Working on: {channel_name}")

                    countdown_status = self._countdown(channel_name, upcoming, delay_seconds)
                    if countdown_status == "stop":
                        self.log(f"Stopped before touching '{channel_name}' - nothing clicked.")
                        break
                    if countdown_status == "skip":
                        self.log(f"Skipped '{channel_name}' before the first click - left in the worklist for next time.")
                        position += 1
                        continue

                    # Two more Skip checkpoints past the countdown, each as late as it can safely
                    # be caught: before the first (reversible, dialog-opening) click, and again
                    # right before the second (irreversible) confirm click inside that dialog.
                    try:
                        if self.skip_event.is_set():
                            self.skip_event.clear()
                            self.log(f"Skipped '{channel_name}' before the first click - left in the worklist for next time.")
                            position += 1
                        else:
                            btn = row.locator(UNSUB_BUTTON_SELECTOR).first
                            btn.scroll_into_view_if_needed()
                            btn.click()
                            page.wait_for_timeout(random.randint(800, 1500))

                            confirm = page.locator(CONFIRM_SELECTOR).first
                            confirm.wait_for(timeout=5000)

                            if self.skip_event.is_set():
                                self.skip_event.clear()
                                page.keyboard.press("Escape")  # best-effort dismiss - dialog hasn't committed anything yet
                                self.log(f"Skipped '{channel_name}' at the confirmation step - left in the worklist for next time.")
                                position += 1
                            else:
                                confirm.click()
                                processed += 1
                                remaining.discard(channel_name)
                                self.log(f"Unsubscribed #{processed}: {channel_name}")
                                self.set_progress(processed, len(remaining), total)
                                # position NOT advanced - this row is gone, the next channel shifted into it
                    except Exception as e:
                        self.log(f"Skipped '{channel_name}' due to a hiccup: {e}")
                        position += 1

                page.close()  # never browser.close() - that's your real Chrome, not ours to close

                if not remaining:
                    self.log(f"Finished. Unsubscribed {processed}, skipped {skipped_kept} already-kept channels. Worklist fully processed.")
                elif self.stop_event.is_set():
                    self.log(f"Stopped. Unsubscribed {processed} so far, {len(remaining)} still remain in the worklist for next time.")
                elif not saw_any_row:
                    self.log("Found zero channel rows the entire run - this likely means YouTube's page layout "
                              "changed and the selectors need updating, not that the list is actually empty.")
                else:
                    self.log(f"Reached the end of the page but {len(remaining)} worklist names were never found: "
                              f"{', '.join(sorted(remaining))}. Possibly already unsubscribed, a name mismatch, or skipped by request.")

        except Exception as e:
            self.log(f"Fatal error: {e}")
        finally:
            self.root.after(0, self.on_finished)


if __name__ == "__main__":
    settings = load_settings()
    root = tk.Tk()
    app = YouTubeMaid(root, settings)
    root.mainloop()
