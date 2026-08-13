WORKING SNAPSHOT -- 2026-08-13 10:05
====================================
State: fully working scraper. 80/80 tests pass.
  - Live-verified: 13 leads captured today, 28 leads written to Sheet yesterday
  - Includes: Stories excluded, typo-tolerant role matching, multi-language
    buyer detection, anti-throttling Chrome flags, virtualized-feed handling
  - Does NOT include: any Groq integration

TO ROLL BACK (copy these files back over the project root):
  copy /Y fb_group_url_collector.py ..
  copy /Y scrape_multi_groups.py ..
  copy /Y test_scraper_logic.py ..
  copy /Y run_daily.bat ..

Then verify:  python test_scraper_logic.py   (expect 80/80)
