Security Feed Bot for Bale  (ربات رصد امنیتی برای بله)   v1.3
==============================================================

FAST START
  Windows     : double-click  start_windows.bat
  Linux / Mac : ./start_linux_mac.sh
  Your browser opens the control panel (http://127.0.0.1:8765). Fill the fields, press "Test", then "Start".

WHAT'S NEW IN 1.3
  - CONTINUOUS sending: the queue is drained until it is empty (no per-cycle limit). The interval only
    controls how often the feeds are checked for new items.
  - A post whose translation is temporarily blocked (HTTP 429 ...) is put aside; the next posts continue and the
    bot comes back to it automatically. After "give-up minutes" (default 30) the original text is sent with a marker.
  - New free translation provider "Microsoft Edge" (default); Google, Lingva (several servers), MyMemory,
    LibreTranslate and Google Cloud (API key) as alternatives; Retry-After is honoured and spacing grows after each 429.
  - Feeds: 3 attempts with different User-Agents, broken connections are retried, failing feeds are retried
    later and later, and the "Live log" tab shows which sources fail (one click disables them).

FEATURES
  - Full article text + ALL images, in reading order (long posts are split into several messages)
  - Translation to Persian piece by piece. Order in the channel:
    header -> FULL original text (with photos) -> FULL Persian translation
  - Your own categories and sources (give a site URL, the RSS feed is found automatically)
  - 12 colour themes for the dashboard (button "تم" at the top)
  - Every sent post is deleted from the database right away (only a tiny dedup fingerprint stays)

Full step-by-step guide (Persian, with screenshots):  GUIDE_fa.pdf

Server mode (no panel, uses data/config.json):   python run.py --headless
Change panel port:                                python run.py --port 9000

Files
  run.py            entry point
  app/              source code (app/web = panel page + bundled Vazirmatn font, SIL OFL)
  data/config.json  your settings (contains the bot token / API keys - do not share it!)
  data/bot.db       small queue + dedup database
