@echo off
cd /d "C:\Users\GNG\Desktop\X ( twitter )\fb_group_url_collector"

echo. >> daily_run.log
echo ==== Daily run started %DATE% %TIME% ==== >> daily_run.log

rem Start (or reuse) the debug Chrome window with the persistent profile.
rem If a Facebook session is already logged into this profile, it stays
rem logged in across days, same as any normal browser profile -- this does
rem NOT log in for you.
start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="C:\chrome-fb-debug-profile" "https://www.facebook.com/"

rem Give Chrome time to finish launching before Selenium attaches.
timeout /t 15 /nobreak > nul

"C:\Users\GNG\AppData\Local\Programs\Python\Python312\python.exe" scrape_multi_groups.py >> daily_run.log 2>&1

echo ==== Daily run finished %DATE% %TIME% ==== >> daily_run.log
