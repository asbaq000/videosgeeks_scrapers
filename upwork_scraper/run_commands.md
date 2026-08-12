important ones 
fetch 10 latest to 2 days old jobs with full data but without hire rate

python -m upwork_scraper --niche video --pages 1 --max-age-days 2 --limit 10 --enrich-clients --out jobs.json

fetch 10 latest to 2 days old jobs with full data with hire rate (full data)
python -m upwork_scraper --reset-login
python -m upwork_scraper --login
python -m upwork_scraper --niche video --pages 1 --max-age-days 2 --limit 3 --enrich-clients --logged-in --out jobs.json


The Standard Run

bash
python -m upwork_scraper --niche video --pages 1 --out jobs.json
Quickly scrapes the first page of jobs using your video.json keywords and saves to jobs.json.

The "Get Everything" Run

bash
python -m upwork_scraper --niche video --pages 1 --enrich-clients --out jobs.json
Scrapes jobs AND opens the background browser to fetch all the rich client details (country, hire rate, etc).


fetch 10 latest to 2 days old jobs with full data but without hire rate
python -m upwork_scraper --niche video --pages 1 --max-age-days 2 --limit 10 --enrich-clients --out jobs.json


The "Testing" Run

bash
python -m upwork_scraper --niche video --pages 1 --enrich-clients --enrich-limit 3 --out test.json
Scrapes jobs but only enriches the first 3 clients (perfect for making sure things work without waiting a long time).

The "Only New Jobs" Run

bash
python -m upwork_scraper --niche video --pages 1 --max-age 60 --enrich-clients --out new_jobs.json
Scrapes jobs but throws away anything older than 60 minutes, so you only get fresh postings.

The "Live Watcher" Run

bash
python -m upwork_scraper --niche video --watch --interval 120 --skip-backlog --out live_jobs.jsonl
Runs continuously, checking Upwork every 120 seconds and saving any brand new jobs it finds to a file.

The Custom Search Run

bash
python -m upwork_scraper --query "video editor" --query "youtube editor" --pages 1 --out custom.json
Ignores your niche file and just searches for these specific keywords right now.