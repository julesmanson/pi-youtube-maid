# Pi YouTube Maid ver 0.1.0 alpha

> **Not yet tested against a real YouTube account.** Every workflow here has only been exercised against a faked browser layer - treat behavior as unverified until you've run it yourself and confirmed the result.

## Setup

1. `pip install playwright`
2. Run `launch_chrome_debug.bat` - relaunches Chrome debug-enabled, then opens `youtube_maid.pyw` automatically.

## In the window

1. Set the URL (pre-filled) and **Keep first N from top**, click **Generate**.
2. Generate scans every subscribed channel and writes `unsubscribe_worklist.json` - every channel name past position N. Nothing is touched yet.
3. Open `unsubscribe_worklist.json`, delete any names you want to keep, save.
4. Set the delay, click **Start** - unsubscribes only names still in that file, matched against the live page by name, not position.
5. The window stays open after a run ends - adjust settings and Generate/Start again without closing anything.

## Onboarding

Runs automatically at the start of Generate, Start, and Test Subscribe 10.

1. Checks that this Chrome profile is logged into YouTube. Confirmed logged out: aborts cleanly, nothing touched. Can't tell either way: warns, proceeds anyway rather than blocking on a guess.
2. Generate/Start then open the configured URL and click "All subscriptions" if that page offers one (silent no-op if not). Test Subscribe 10 skips this part entirely.

## Testing: Test Subscribe 10

A separate button, visually set apart - exists purely to create disposable test subscriptions, not part of normal use.

1. After the login check, goes straight to the **Harvest from** URL (its own field, below the button - defaults to the YouTube home page, editable to any page with video tiles), looking for up to 10 channels to subscribe to.
2. Shorts skipped outright, judged by each item's own video-link URL - only full-video channels are candidates.
3. Only duplicate check: the channel's own live "Subscribed" state. No knowledge of `unsubscribe_worklist.json` - a channel already in that file gets checked and subscribed like any other. (Same channel appearing twice in the feed is only opened once, since re-checking it is just wasted work.)
4. Feed runs dry before reaching 10 (after a few scroll retries): stops with whatever it found.
5. Once the subscribe step ends - hit 10, ran dry, or Stop - everything else is a normal Generate run: same URL/keep-first-N fields, same worklist written at the end. Stop during the subscribe step skips that trailing scan entirely.

## While a run is active

- **Stop** - halts the whole run after the current action finishes.
- **Skip** - leaves the current channel in the worklist untouched, moves to the next. Caught as early as possible: during the countdown, else right before the first click, else right before the final confirm click - the last-chance checkpoint, since that one's the only truly irreversible action.
- **Pause / Resume** - Pause freezes the countdown exactly where it stands (not reset); Resume picks up from the same point. Start only, not Generate or Test Subscribe 10.

## Delay and queue preview

- **Delay before each (sec)** - interruptible countdown before every click. Default 5; accepts 0 to skip the wait entirely.
- The box below the buttons shows up to 4 rows: current channel (green) + next 3 queued (muted), regardless of worklist size. A progress line above tracks unsubscribed / remaining / total.
- Stuck detection (no new channels loading, names unreadable for several rows) shows a red error with a suggested cause instead of the queue - reverts automatically once reading channels normally again.

## Settings

- `maid_settings.json` - remembers last-used URL / keep_first_n / delay / harvest URL. Auto-created, gitignored.
- `cdp_url` - Chrome debug port, only in that file - only matters if 9222 is ever unavailable.

## Notes

- Chrome must be relaunched via the `.bat` before every session - debug mode doesn't survive a normal restart.
- Use the **Stop** button to end a session, not the `.bat` (it force-closes Chrome instead of stopping cleanly).
- Only closes the tabs it opens (Test Subscribe 10: one per channel checked) - never your other tabs, never the browser itself.
- No credential entry, ever - only checks whether Chrome shows as signed in, never handles a password.

## Disclaimer

Use at your own risk. This project automates real actions on your real YouTube account and is not affiliated with, endorsed by, or supported by YouTube or Google. Always review `unsubscribe_worklist.json` before clicking Start. The author assumes no responsibility for unintended unsubscribes, account restrictions, or any other consequence of running this tool.

---

Developed by Jules Manson and Claude Sonnet 5.
