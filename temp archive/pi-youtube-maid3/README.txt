YOUTUBE MAID - SETUP

1. pip install playwright
2. Open launch_chrome_debug.bat - relaunches Chrome debug-enabled, then opens youtube_maid.pyw automatically.

IN THE WINDOW

3. Set the URL (pre-filled) and "Keep first N from top", then click Generate.
4. Generate scans every subscribed channel and writes unsubscribe_worklist.json - every channel name past position N. Nothing is touched yet.
5. Open unsubscribe_worklist.json and delete any names you want to keep, even if they're past N. Save it.
6. Set the delay, then click Start - unsubscribes only the names still in that file, matched against the live page by name, not position.
7. The window stays open after a run ends (Stop, finished, or ran out of worklist names) - adjust settings and Generate/Start again without closing anything.

ONBOARDING (runs automatically at the start of both Generate and Start)

8. Checks that this Chrome profile is logged into YouTube. Confirmed not logged in: aborts cleanly with a message, nothing touched. Can't tell either way (YouTube's markup may have changed): logs a warning but still proceeds, rather than blocking on a guess.
9. Opens the configured URL.
10. If that page offers an "All subscriptions" button/link, clicks it. If it doesn't (e.g. the URL already points straight at the channel list), this step is a silent no-op - not an error.

WHILE A RUN IS ACTIVE

11. Stop - halts the whole run after the current action finishes.
12. Skip - leaves just the current channel in the worklist (nothing happens to it) and moves to the next one. Caught as early as possible: during the countdown, or failing that right before the first click, or failing that right before the final confirm click - whichever point it's still waiting at when you click Skip. The confirm click is the only truly irreversible one, so it's the last-chance checkpoint.
13. Pause / Resume - Pause freezes the countdown exactly where it stands (not reset) and holds before any click; Resume picks back up from that same point. Only enabled during Start, not Generate.

DELAY AND QUEUE PREVIEW

14. "Delay before each (sec)" is an interruptible countdown shown before every unsubscribe click - time to hit Stop/Skip/Pause before anything commits. Defaults to 5. Accepts 0 (or any positive number) - at 0 it skips the wait and moves as fast as YouTube's own UI allows.
15. The box below the buttons shows up to 4 rows: the current channel (green) plus the next 3 queued to be unsubscribed (muted) - even if the worklist has hundreds or thousands more behind them. The stack pushes up as each one finishes. Above it, a progress line tracks unsubscribed-so-far vs. remaining vs. the original worklist total.
16. If something looks stuck (no new channels loading, or channel names unreadable for several rows in a row), that box temporarily shows a red error with a suggested cause instead of the queue - it reverts to the normal stack automatically once the run is reading channels fine again.

SETTINGS

17. maid_settings.json remembers the last-used URL / keep_first_n / delay between sessions - auto-created, you don't need to open it for normal use.
18. The one setting only in that file (not in the window) is cdp_url, the Chrome debug port - only matters if port 9222 is ever unavailable.

NOTES

19. Chrome must be relaunched via the .bat before every session - debugging mode doesn't survive a normal Chrome restart.
20. To stop a running session, use the Stop button in the window - do not re-run the .bat, since it force-closes Chrome first and would yank the browser out from under an active run instead of stopping it cleanly.
21. Only closes the tab it opened - never your other tabs, never the browser itself.
22. No credential entry, ever - it only checks whether your real, already-logged-in Chrome profile shows as signed in (step 8); it never prompts for or handles a password.
