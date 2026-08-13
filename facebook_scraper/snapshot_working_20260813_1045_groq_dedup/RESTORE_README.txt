SNAPSHOT -- 2026-08-13 10:45  (Groq niche + budget/min + cross-group dedup)
===========================================================================
104/104 tests pass. Adds, on top of snapshot_working_20260813_1005:
  - Facebook Stories excluded (never leads)
  - Groq used ONLY to fill the Niche column (falls back to hashtags on any error)
  - Budget per minute ("$500 per minute", "400 per mint", "300 pr mint")
  - Cross-group dedup (same lead posted in several groups saved once)

ROLLBACK OPTIONS
  Safe/simplest  -> copy files from snapshot_working_20260813_1005  (pre-Groq)
  This version   -> copy the files in this folder back to the project root
Verify after any rollback:  python test_scraper_logic.py
