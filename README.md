# Marktplaats Sniper

Watches Marktplaats for you and sends a message to your phone (Telegram) the
second a new ad appears that matches what you're looking for. Good deals go
fast. This way you're the first one to message the seller.

## Setup (10 minutes, only once)

### 1. Install Python
Download it from https://www.python.org/downloads/ and run the installer.
**On the first screen, tick "Add python.exe to PATH"**, then click Install.

### 2. Connect Telegram
You need Telegram on your phone. Then double-click **`setup_telegram.bat`** and
follow the steps on screen. It walks you through making your own bot (takes
1 minute) and sends you a test message when it works.

> Skip this if you don't want phone alerts. New ads will then only show up in
> the sniper's window on your PC.

### 3. Say what you're looking for
Open **`settings.toml`** with Notepad (right-click → Open with → Notepad).

- Fill in your postcode under `[general]`.
- Change the example search to what you want: `query`, prices, distance.
- Want to watch more than one thing? Copy a whole `[[search]]` block and change it.

Save the file.

### 4. Test your searches
Double-click **`test_searches.bat`**. It shows the newest ads for each search
and which ones would trigger an alert (`HIT`) or get skipped (and why).
Nothing is sent. Too much junk? Add words to `exclude_words`. Missing stuff?
Loosen your filters. Run it again until it looks right.

### 5. Start sniping
Double-click **`start_sniper.bat`**. Leave the window open: it checks every
minute. Close the window to stop.

The first check only remembers what's already online, so you won't get 50
messages at once. After that, only brand new ads come through.

## The search settings

| setting | what it does |
|---|---|
| `name` | Label shown in the alert. Anything you like. |
| `query` | What you'd type in the Marktplaats search bar. |
| `min_price` / `max_price` | In euros. `0` means no limit. A minimum skips the €1 "bieden" bait ads. |
| `distance_km` | How far from your postcode. `0` = all of the Netherlands. |
| `query_in_title` | `true` = all words of your query must be in the ad title. Cuts out a lot of junk. |
| `exclude_words` | Skip ads containing any of these words, like `["defect", "kapot"]`. |
| `must_include` | Only alert if the ad has at least one of these words. `[]` = no requirement. |
| `highlight_words` | Get a ⭐ in the alert when these show up. Nothing is skipped for these. |
| `skip_wanted_ads` | Skip "gezocht" ads (people who want to buy, not sell). |
| `skip_shops` | Skip ads from webshops and businesses. |

Words don't care about capitals, and they also match inside longer words
(`"kapot"` also catches `"kapotte"`).

## Extra: everything in a spreadsheet
Double-click **`scan_to_spreadsheet.bat`** to get EVERY ad that currently matches
your searches (not just new ones) in a file in the `scans` folder. Open it with
Excel to sort by price or distance. Handy to see what something normally costs.

## Problems?

- **"Python is not installed"** even though you installed it: you probably missed
  the "Add to PATH" box. Run the Python installer again → Modify → tick it.
- **"Problem with settings.toml"**: the message tells you what's wrong and in
  which search. Usually a missing `"` or a missing comma between words in a list.
- **No alerts:** did you run `test_searches.bat`? If it shows no `HIT`s, your
  filters are too strict. Remember only *new* ads trigger an alert.
- **"HTTP 403" or "429"**: you're checking too often. Raise `poll_interval_seconds`.
  The sniper slows down by itself when this happens.
- **Want to start over?** Delete `seen.json`. The next start remembers the current
  ads again without alerting on them.

## Be nice
This uses the same hidden search that marktplaats.nl itself uses, not an
official API. Keep `poll_interval_seconds` at 30 or higher. Checking every few
seconds won't get you deals faster, it just gets you blocked.
