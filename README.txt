YOUTUBE MAID - SETUP

1. pip install playwright
2. Open launch_chrome_debug.bat - relaunches Chrome debug-enabled, then opens youtube_maid.pyw automatically.

IN THE WINDOW

3. Set the URL (pre-filled) and "Keep first N from top", then click Generate.
4. Generate scans every subscribed channel and writes unsubscribe_worklist.json - every channel name past position N. Nothing is touched yet.
5. Open unsubscribe_worklist.json and delete any names you want to keep, even if they're past N. Save it.
6. Set the delay, then click Start - unsubscribes only the names still in that file, matched against the live page by name, not position.
7. The window stays open after a run ends (Stop, finished, or ran out of worklist names) - adjust settings and Generate/Start again without closing anything.

ONBOARDING (runs automatically at the start of Generate, Start, and Test Subscribe 10)

8. Checks that this Chrome profile is logged into YouTube. Confirmed not logged in: aborts cleanly with a message, nothing touched. Can't tell either way (YouTube's markup may have changed): logs a warning but still proceeds, rather than blocking on a guess.
9. Generate and Start then open the configured URL, and if that page offers an "All subscriptions" button/link, click it. If it doesn't (e.g. the URL already points straight at the channel list), this step is a silent no-op - not an error. Test Subscribe 10 skips this part entirely - see below.

TESTING: TEST SUBSCRIBE 10

10. A separate button, visually set apart from the main workflow - exists purely to create disposable test subscriptions, not part of normal use.
11. After the login check, it goes straight to the YouTube home page (not the configured URL) and works through the home feed looking for up to 10 channels to subscribe to.
12. Shorts are skipped outright - judged by each item's own video-link URL, not a guess at styling. Only channels surfaced by a full video are candidates.
13. The only duplicate check is each candidate channel's own live page: already shows "Subscribed" there -> skipped, doesn't count toward the 10. This button has no knowledge of unsubscribe_worklist.json or anything else - a channel already sitting in that file gets checked and subscribed like any other. (The same channel appearing twice in the feed itself is only opened once, since that's just wasted work within the same run, not a worklist lookup.)
14. If the feed runs dry before finding 10 (retries scrolling a few times first), it stops with whatever it found - never blocks forever.
15. Once the subscribe step ends - whether it hit 10, ran dry, or you clicked Stop - everything else is a perfectly normal Generate run: same URL/keep-first-N fields, same worklist file written at the end. Stop during the subscribe step skips that trailing Generate scan entirely.

WHILE A RUN IS ACTIVE

16. Stop - halts the whole run after the current action finishes.
17. Skip - leaves just the current channel in the worklist (nothing happens to it) and moves to the next one. Caught as early as possible: during the countdown, or failing that right before the first click, or failing that right before the final confirm click - whichever point it's still waiting at when you click Skip. The confirm click is the only truly irreversible one, so it's the last-chance checkpoint.
18. Pause / Resume - Pause freezes the countdown exactly where it stands (not reset) and holds before any click; Resume picks back up from that same point. Only enabled during Start, not Generate or Test Subscribe 10.

DELAY AND QUEUE PREVIEW

19. "Delay before each (sec)" is an interruptible countdown shown before every unsubscribe click - time to hit Stop/Skip/Pause before anything commits. Defaults to 5. Accepts 0 (or any positive number) - at 0 it skips the wait and moves as fast as YouTube's own UI allows.
20. The box below the buttons shows up to 4 rows: the current channel (green) plus the next 3 queued to be unsubscribed (muted) - even if the worklist has hundreds or thousands more behind them. The stack pushes up as each one finishes. Above it, a progress line tracks unsubscribed-so-far vs. remaining vs. the original worklist total.
21. If something looks stuck (no new channels loading, or channel names unreadable for several rows in a row), that box temporarily shows a red error with a suggested cause instead of the queue - it reverts to the normal stack automatically once the run is reading channels fine again.

SETTINGS

22. maid_settings.json remembers the last-used URL / keep_first_n / delay between sessions - auto-created, you don't need to open it for normal use.
23. The one setting only in that file (not in the window) is cdp_url, the Chrome debug port - only matters if port 9222 is ever unavailable.

NOTES

24. Chrome must be relaunched via the .bat before every session - debugging mode doesn't survive a normal Chrome restart.
25. To stop a running session, use the Stop button in the window - do not re-run the .bat, since it force-closes Chrome first and would yank the browser out from under an active run instead of stopping it cleanly.
26. Only closes the tabs it opens (Test Subscribe 10 opens one per channel it checks) - never your other tabs, never the browser itself.
27. No credential entry, ever - it only checks whether your real, already-logged-in Chrome profile shows as signed in (step 8); it never prompts for or handles a password.

Developed by Jules Manson and Claude Sonnet 5.
