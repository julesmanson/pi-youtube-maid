"""
Pi YouTube Maid - mass-unsubscribe from YouTube channels, with a review step first.
No API/OAuth - attaches to your real, already-logged-in Chrome via remote debugging
(see launch_chrome_debug.bat) instead of launching a separate browser.

Setup:
  pip install playwright

Workflow:
  1. Run launch_chrome_debug.bat (closes Chrome, reopens it debug-enabled,
     then opens this file automatically).
  Generate and Start both onboard themselves before doing anything else:
  confirm this Chrome profile is logged into YouTube (aborts cleanly if not;
  proceeds with a warning if that can't be confirmed either way), open the
  configured URL, then click "All subscriptions" if that page offers one.
  2. Set "keep first N", click Generate. This scans every subscribed channel
     and fills the list on the right with every channel NAME past position
     N - the candidates for unsubscribing. Nothing is touched yet.
  3. Edit that list directly - one name per line, free-form text, Undo/Redo
     supported. Save writes it to unsubscribe_worklist.txt; Reset clears it.
  4. Set the delay, then click Start. This only unsubscribes channels whose
     name is still present in that list, matched against the live page - not
     by position, so it's correct even if your subscriptions changed between
     Generate and Start. Each name is removed from the list live as its
     channel is actually unsubscribed.

Test Subscribe 10 (testing only, visually set apart from the above): skips
the URL-opening/"All subscriptions" onboarding and goes straight to the
Harvest-from page (defaults to the YouTube home page, editable to any page),
subscribing to up to 10 channels found there (full videos only, Shorts
skipped by their own video-link URL; the only duplicate check is each
channel's own live "already subscribed" state - it has no knowledge of the
unsubscribe list or anything else). The names it actually subscribes to get
appended to the list on the right - nothing else happens automatically.

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

# Theme - one constant per part, edit freely. Applies uniformly everywhere
# that part appears (skin covers the upper/middle/lower panels and the log,
# alike - there's no separate "top section" color anymore).
SKIN_BG = "#312a23"             # app surface background - every panel, top to bottom
SKIN_TEXT = "#c5c9d2"           # text sitting on the skin (labels, log, counters)
INPUT_FIELD_BG = "#617081"      # background inside editable areas (URL/N/delay/harvest entries)
INPUT_TEXT = "#ffffff"          # color of what you type into a field
BUTTON_BG = "#617081"           # button background, not pressed
BUTTON_BG_PRESSED = "#abbcc9"   # button background, pressed
BUTTON_TEXT = "#c5c9d2"         # text on buttons
ATTRIBUTION_TEXT = "#9fafc5"    # "Developed by..." footer text
STACK_BG = "#000000"            # background inside the stack display (current + up-next channels)
STACK_TEXT = "#888888"          # the 3 up-next channel names in the stack display
STACK_ACTIVE = "#00ff00"        # the currently staged (about to be unsubscribed) channel - always lime
ERROR_TEXT = "#ff6b6b"          # not one of the named parts above - the stack display's stuck/error state, kept distinct since it's a status signal, not a skin color

TEST_SUBSCRIBE_BATCH_SIZE = 10  # the target - how many channels to successfully subscribe to before stopping (the "10" in the button name), used as the loop's success condition
TEST_SUBSCRIBE_SCROLL_GIVEUP = 10  # a timeout - how many consecutive scroll attempts that load zero new items to tolerate before giving up early (even if fewer than 10 were found), used as the stall-detection limit

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "maid_settings.json"
WORKLIST_FILE = HERE / "unsubscribe_worklist.txt"  # plain text, one channel name per line - no JSON

DEFAULT_SETTINGS = {
    "url": "https://www.youtube.com/feed/subscriptions",
    "keep_first_n": 24,
    "cdp_url": "http://localhost:9222",
    "delay_seconds": 5.0,
    "harvest_url": "https://www.youtube.com"
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

QUEUE_LOOKAHEAD_SCAN_CAP = 60  # rows to scan forward for preview names before giving up

HOME_ITEM_SELECTOR = "ytd-rich-item-renderer"
HOME_VIDEO_LINK_SELECTOR = "a#thumbnail"
HOME_CHANNEL_LINK_SELECTOR = "ytd-channel-name a, #channel-name a"
CHANNEL_SUBSCRIBE_BUTTON_SELECTOR = "ytd-subscribe-button-renderer button, #subscribe-button button"


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


def save_last_used(url, keep_first_n, cdp_url, delay_seconds, harvest_url):
    SETTINGS_FILE.write_text(json.dumps(
        {
            "url": url,
            "keep_first_n": keep_first_n,
            "cdp_url": cdp_url,
            "delay_seconds": delay_seconds,
            "harvest_url": harvest_url
        },
        indent=2
    ))


def load_worklist_text():
    if WORKLIST_FILE.exists():
        try:
            return WORKLIST_FILE.read_text()
        except Exception:
            return ""
    return ""


class YouTubeMaid:
    def __init__(self, root, settings):
        self.root = root
        root.title("Pi YouTube Maid")
        root.geometry("900x580")
        root.minsize(700, 460)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        root.configure(background=SKIN_BG)
        style = ttk.Style(root)
        style.theme_use("clam")  # clam actually honors background/foreground overrides on Windows, unlike vista/winnative
        style.configure(".", background=SKIN_BG, foreground=SKIN_TEXT)
        style.configure("TFrame", background=SKIN_BG)
        style.configure("TLabel", background=SKIN_BG, foreground=SKIN_TEXT)
        style.configure("TButton", background=BUTTON_BG, foreground=BUTTON_TEXT)
        style.map("TButton", background=[("pressed", BUTTON_BG_PRESSED), ("active", BUTTON_BG_PRESSED)],
                  foreground=[])
        style.configure("TEntry", fieldbackground=INPUT_FIELD_BG, foreground=INPUT_TEXT, insertcolor=INPUT_TEXT)
        style.configure("TSpinbox", fieldbackground=INPUT_FIELD_BG, foreground=INPUT_TEXT,
                         insertcolor=INPUT_TEXT, arrowcolor=SKIN_TEXT)
        style.configure("TScrollbar", background=INPUT_FIELD_BG, troughcolor=SKIN_BG, arrowcolor=SKIN_TEXT)

        self.stop_event = threading.Event()
        self.skip_event = threading.Event()
        self.pause_event = threading.Event()
        self.worker_thread = None
        self.running = False

        footer = ttk.Frame(root, padding=(12, 2))
        footer.pack(side="bottom", fill="x")
        ttk.Label(footer, text="Developed by Jules Manson and Claude Sonnet 5.",
                  font=("", 8), foreground=ATTRIBUTION_TEXT).pack(anchor="center")

        columns_frame = ttk.Frame(root)
        columns_frame.pack(fill="both", expand=True)

        left_frame = ttk.Frame(columns_frame)
        left_frame.pack(side="left", fill="y")

        right_frame = ttk.Frame(columns_frame, padding=(4, 12, 12, 12))
        right_frame.pack(side="left", fill="both", expand=True)

        # Row 1: the stack display. One continuous STACK_BG fill, no skin-colored
        # gap anywhere inside it - progress/countdown/queue rows all share it.
        # This is a real stack: row 0 (top) is the active/current channel,
        # every row below is "up next," pushing up as each one finishes - and
        # the stack holds as many rows as actually fit the available height,
        # not a fixed count. It grows/shrinks live as the window is resized.
        queue_frame = ttk.Frame(left_frame, padding=(12, 8, 12, 10))
        queue_frame.pack(fill="both", expand=True)

        display_box = tk.Frame(queue_frame, background=STACK_BG)
        display_box.pack(fill="both", expand=True)

        self.progress_var = tk.StringVar(value="")
        tk.Label(display_box, textvariable=self.progress_var, font=("", 9), anchor="w",
                 background=STACK_BG, foreground=SKIN_TEXT).pack(fill="x", padx=8, pady=(6, 0))

        self.countdown_var = tk.StringVar(value="")
        tk.Label(display_box, textvariable=self.countdown_var, font=("", 16, "bold"), anchor="w",
                 background=STACK_BG, foreground=SKIN_TEXT).pack(fill="x", padx=8)

        self.queue_box = tk.Frame(display_box, background=STACK_BG)
        self.queue_box.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        # Without this, queue_box's own requested size ratchets up with its
        # children and never shrinks back down after the window does -
        # verified: without it, growing to many rows then shrinking back left
        # it stuck oversized (391px/17 rows instead of returning to 162px/7).
        self.queue_box.pack_propagate(False)

        self.queue_labels = []
        self._queue_row_height = None
        self._add_queue_row()  # row 0 (current) always exists, even before any resize event fires
        self.queue_box.bind("<Configure>", self._on_queue_box_resize)

        # Row 2: the 4 run-control buttons together, in this exact order,
        # centered to match the full-width display above them. Generate
        # lives in the top-right of the right panel instead - it fills the
        # list there, so it sits next to what it fills.
        btn_frame = ttk.Frame(left_frame, padding=(12, 0, 12, 8))
        btn_frame.pack(fill="x")
        btn_group = ttk.Frame(btn_frame)
        btn_group.pack(anchor="center")
        self.start_btn = ttk.Button(btn_group, text="Start", command=self.on_start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btn_group, text="Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.pause_btn = ttk.Button(btn_group, text="Pause", command=self.on_pause_toggle, state="disabled")
        self.pause_btn.pack(side="left", padx=4)
        self.skip_btn = ttk.Button(btn_group, text="Skip", command=self.on_skip, state="disabled")
        self.skip_btn.pack(side="left", padx=4)

        # Everything else - unchanged from before, just now sitting below the two rows above.
        form = ttk.Frame(left_frame, padding=12)
        form.pack(fill="x")

        ttk.Label(form, text="Channels page URL").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.url_var = tk.StringVar(value=settings["url"])
        ttk.Entry(form, textvariable=self.url_var, width=42).grid(row=0, column=1, sticky="we", pady=4)

        form.columnconfigure(1, weight=1)

        controls_frame = ttk.Frame(left_frame, padding=(12, 0, 12, 4))
        controls_frame.pack(fill="x")
        ttk.Label(controls_frame, text="Keep first N").pack(side="left")
        self.start_at_var = tk.IntVar(value=settings["keep_first_n"])
        self.start_at_spin = ttk.Spinbox(controls_frame, from_=0, to=100000, textvariable=self.start_at_var, width=8)
        self.start_at_spin.pack(side="left", padx=(8, 12))
        ttk.Label(controls_frame, text="Delay (sec)").pack(side="left")
        self.delay_var = tk.DoubleVar(value=settings["delay_seconds"])
        self.delay_spin = ttk.Spinbox(controls_frame, from_=0, to=300, increment=0.5, format="%.1f",
                                       textvariable=self.delay_var, width=6)
        self.delay_spin.pack(side="left", padx=(8, 12))

        ttk.Label(left_frame, text=f"Generate fills the list on the right - edit it there, then click Start.",
                  wraplength=440, padding=(12, 0)).pack(fill="x")

        # Separate row, visually set apart - a testing utility, not part of the
        # main Generate/Start workflow above it.
        test_frame = ttk.Frame(left_frame, padding=(12, 8, 12, 8))
        test_frame.pack(fill="x")
        ttk.Label(test_frame, text="Testing").pack(side="left", padx=(0, 8))
        self.test_subscribe_btn = ttk.Button(test_frame, text="Test Subscribe 10", command=self.on_test_subscribe)
        self.test_subscribe_btn.pack(side="left")
        ttk.Label(test_frame, text="subscribes to up to 10 new channels from the page below "
                  "(full videos only, skips Shorts and already-subscribed channels), then appends "
                  "those names to the list on the right.",
                  wraplength=320).pack(side="left", padx=(8, 0))

        harvest_frame = ttk.Frame(left_frame, padding=(12, 0, 12, 8))
        harvest_frame.pack(fill="x")
        ttk.Label(harvest_frame, text="Harvest from").pack(side="left", padx=(0, 8))
        self.harvest_url_var = tk.StringVar(value=settings["harvest_url"])
        self.harvest_url_entry = ttk.Entry(harvest_frame, textvariable=self.harvest_url_var, width=42)
        self.harvest_url_entry.pack(side="left", fill="x", expand=True)

        # Right column: the worklist itself - plain text, one channel name per
        # line, directly editable. This IS the worklist; WORKLIST_FILE is only
        # touched by Save (writing it out) and startup (reading it back in).
        worklist_btn_frame = ttk.Frame(right_frame, padding=(0, 0, 0, 6))
        worklist_btn_frame.pack(fill="x")
        self.worklist_save_btn = ttk.Button(worklist_btn_frame, text="Save", command=self.on_worklist_save)
        self.worklist_save_btn.pack(side="left", padx=(0, 4))
        self.worklist_undo_btn = ttk.Button(worklist_btn_frame, text="Undo", command=self.on_worklist_undo)
        self.worklist_undo_btn.pack(side="left", padx=4)
        self.worklist_redo_btn = ttk.Button(worklist_btn_frame, text="Redo", command=self.on_worklist_redo)
        self.worklist_redo_btn.pack(side="left", padx=4)
        self.worklist_reset_btn = ttk.Button(worklist_btn_frame, text="Reset", command=self.on_worklist_reset)
        self.worklist_reset_btn.pack(side="left", padx=4)
        self.generate_btn = ttk.Button(worklist_btn_frame, text="Generate", command=self.on_generate)
        self.generate_btn.pack(side="right", padx=(4, 0))

        worklist_text_frame = ttk.Frame(right_frame)
        worklist_text_frame.pack(fill="both", expand=True)
        self.worklist_text = tk.Text(worklist_text_frame, wrap="none", undo=True,
                                      background=INPUT_FIELD_BG, foreground=INPUT_TEXT,
                                      insertbackground=INPUT_TEXT)
        self.worklist_text.pack(side="left", fill="both", expand=True)
        worklist_scrollbar = ttk.Scrollbar(worklist_text_frame, command=self.worklist_text.yview)
        worklist_scrollbar.pack(side="right", fill="y")
        self.worklist_text.config(yscrollcommand=worklist_scrollbar.set)

        existing_worklist = load_worklist_text()
        if existing_worklist:
            self.worklist_text.insert("1.0", existing_worklist)
        self.worklist_text.edit_reset()  # the initial load isn't itself an undoable edit

        self.log("Ready. Set the number, click Generate, review the list on the right, then click Start.")

    def log(self, message):
        print(message)

    def _add_queue_row(self):
        i = len(self.queue_labels)
        lbl = tk.Label(self.queue_box, text="(idle)" if i == 0 else "", anchor="w",
                        padx=8, pady=2, background=STACK_BG,
                        font=("", 9, "bold" if i == 0 else "normal"),
                        foreground=STACK_ACTIVE if i == 0 else STACK_TEXT)
        lbl.pack(fill="x")
        self.queue_labels.append(lbl)

    def _on_queue_box_resize(self, event):
        # Measures the first row's real rendered height once, then fits as
        # many rows as the box's current height allows - row 0 (current) is
        # never removed even if space is tight.
        if self._queue_row_height is None:
            self.queue_box.update_idletasks()
            self._queue_row_height = max(self.queue_labels[0].winfo_reqheight(), 1)
        desired_count = max(1, event.height // self._queue_row_height)
        while len(self.queue_labels) < desired_count:
            self._add_queue_row()
        while len(self.queue_labels) > desired_count and len(self.queue_labels) > 1:
            self.queue_labels.pop().destroy()

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
                lbl.config(foreground=STACK_ACTIVE)  # also clears a prior error's red

    def set_error(self, message, suggestion):
        self.root.after(0, lambda: self._render_error(f"{message} - {suggestion}"))

    def _render_error(self, text):
        self.countdown_var.set("")
        self.queue_labels[0].config(text=text, foreground=ERROR_TEXT)
        for lbl in self.queue_labels[1:]:
            lbl.config(text="")

    def worklist_names(self):
        # Called from the worker thread - reading a Tk widget's current text
        # is safe without root.after, only mutating it needs marshaling.
        text = self.worklist_text.get("1.0", "end")
        return {line.strip() for line in text.splitlines() if line.strip()}

    def set_worklist(self, names):
        self.root.after(0, lambda: self._replace_worklist_text("\n".join(names)))

    def append_worklist(self, names):
        def _do():
            current = self.worklist_text.get("1.0", "end").strip()
            combined = (current + "\n" + "\n".join(names)) if current else "\n".join(names)
            self._replace_worklist_text(combined)
        self.root.after(0, _do)

    def remove_worklist_line(self, name):
        def _do():
            lines = [l for l in self.worklist_text.get("1.0", "end").splitlines() if l.strip() != name]
            self._replace_worklist_text("\n".join(lines))
        self.root.after(0, _do)

    def _replace_worklist_text(self, text):
        self.worklist_text.delete("1.0", "end")
        self.worklist_text.insert("1.0", text)

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
        # However many "up next" slots the stack display currently has (it
        # resizes live), minus 1 for the current/active row at the top.
        upcoming_slots = max(0, len(self.queue_labels) - 1)
        names = []
        count = rows.count()
        end = min(count, position + 1 + QUEUE_LOOKAHEAD_SCAN_CAP)
        i = position + 1
        while i < end and len(names) < upcoming_slots:
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
        harvest_url = self.harvest_url_var.get().strip()
        if not harvest_url:
            self.log("Harvest-from URL is empty.")
            return None
        return url, start_at, delay, harvest_url

    def _set_running_ui(self, running, unsubscribing=False):
        state = "disabled" if running else "normal"
        self.generate_btn.config(state=state)
        self.start_btn.config(state=state)
        self.test_subscribe_btn.config(state=state)
        self.start_at_spin.config(state=state)
        self.delay_spin.config(state=state)
        self.harvest_url_entry.config(state=state)
        self.stop_btn.config(state="normal" if running else "disabled")
        # Skip/Pause only mean anything mid-unsubscribe - Generate/Test Subscribe have no per-channel action to skip or pause.
        skip_pause_state = "normal" if (running and unsubscribing) else "disabled"
        self.skip_btn.config(state=skip_pause_state)
        self.pause_btn.config(state=skip_pause_state)
        # The worklist itself is locked while running - a worker thread may be
        # reading/mutating it (removing lines as it processes them).
        self.worklist_text.config(state=state)
        self.worklist_save_btn.config(state=state)
        self.worklist_undo_btn.config(state=state)
        self.worklist_redo_btn.config(state=state)
        self.worklist_reset_btn.config(state=state)

    def on_generate(self):
        if self.running:
            return
        form = self._read_form()
        if form is None:
            return
        url, start_at, delay, harvest_url = form

        self.running = True
        self.stop_event.clear()
        self._set_running_ui(True)
        save_last_used(url, start_at, DEFAULT_SETTINGS["cdp_url"], delay, harvest_url)

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
        url, start_at, delay, harvest_url = form

        self.running = True
        self.stop_event.clear()
        self._set_running_ui(True)
        save_last_used(url, start_at, DEFAULT_SETTINGS["cdp_url"], delay, harvest_url)

        self.worker_thread = threading.Thread(
            target=self.run_test_subscribe, args=(DEFAULT_SETTINGS["cdp_url"], harvest_url), daemon=True
        )
        self.worker_thread.start()

    def on_start(self):
        if self.running:
            return
        names = self.worklist_names()
        if not names:
            self.log("The list on the right is empty - nothing to unsubscribe.")
            return

        form = self._read_form()
        if form is None:
            return
        url, _start_at, delay, harvest_url = form

        self.running = True
        self.stop_event.clear()
        self.skip_event.clear()
        self.pause_event.clear()
        self.pause_btn.config(text="Pause")
        self._set_running_ui(True, unsubscribing=True)
        save_last_used(url, _start_at, DEFAULT_SETTINGS["cdp_url"], delay, harvest_url)

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

    def on_worklist_save(self):
        if self.running:
            return
        WORKLIST_FILE.write_text(self.worklist_text.get("1.0", "end"))
        self.log(f"Saved {WORKLIST_FILE.name}.")

    def on_worklist_undo(self):
        if self.running:
            return
        try:
            self.worklist_text.edit_undo()
        except tk.TclError:
            pass  # nothing left to undo

    def on_worklist_redo(self):
        if self.running:
            return
        try:
            self.worklist_text.edit_redo()
        except tk.TclError:
            pass  # nothing left to redo

    def on_worklist_reset(self):
        if self.running:
            return
        self.worklist_text.delete("1.0", "end")

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
                    self.log("Stopped before the scan finished - list not updated.")
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

                self.set_worklist(names)
                self.log(f"Found {total} channels total, {len(names)} past position {start_at}.")
                self.log("Filled the list on the right. Review/edit it, then click Start.")
                page.close()

        except Exception as e:
            self.log(f"Fatal error: {e}")
        finally:
            self.root.after(0, self.on_finished)

    def _subscribe_up_to_10_from_page(self, page, context, harvest_url):
        # Read-then-decide per item: only a full-video item (never Shorts, judged
        # by its own thumbnail link href rather than a fragile class/attribute
        # name) gets a real subscribe attempt. The only duplicate check is each
        # channel's own live "already subscribed" state - this method has no
        # knowledge of the unsubscribe worklist or anything outside this scan;
        # seen_names below is just this-run dedup (the same channel can appear
        # more than once in the feed) and is never seeded from the worklist.
        # harvest_url defaults to the YouTube home page but can be any page
        # that renders ytd-rich-item-renderer video tiles (search results,
        # a trending page, etc.).
        page.goto(harvest_url)
        page.wait_for_timeout(1500)

        seen_names = set()
        subscribed_names = []
        index = 0
        scroll_tries = 0

        while len(subscribed_names) < TEST_SUBSCRIBE_BATCH_SIZE and not self.stop_event.is_set():
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
                    subscribed_names.append(channel_name)
                    self.log(f"Subscribed #{len(subscribed_names)}: {channel_name}")
            except Exception as e:
                self.log(f"Couldn't process '{channel_name}': {e}")
            finally:
                chan_page.close()

        self.log(f"Test Subscribe 10: {len(subscribed_names)} new subscription(s) this run.")
        return subscribed_names

    def run_test_subscribe(self, cdp_url, harvest_url):
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

                subscribed_names = self._subscribe_up_to_10_from_page(page, context, harvest_url)
                page.close()

                if subscribed_names:
                    self.append_worklist(subscribed_names)
                    self.log(f"Appended {len(subscribed_names)} name(s) to the list on the right.")

        except Exception as e:
            self.log(f"Fatal error: {e}")
        finally:
            self.root.after(0, self.on_finished)

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
                                self.remove_worklist_line(channel_name)
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
