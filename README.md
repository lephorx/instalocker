# Instalocker

Locks a chosen Valorant agent as soon as agent-select starts. Controlled
from [val-skin-catch](https://github.com/lephorx/val-skin-catch)'s
"Instalock" tab, which embeds this as a submodule at `instalocker/`.

**Runs locally on your own gaming PC, not on any server.** Agent locking
goes through the Riot Client's *local* API (`127.0.0.1`, authenticated
via a lockfile Riot Client writes to disk only while it's running) —
there's no remote/cloud equivalent for this. The val-skin-catch website
is still how you control it: this is a small local HTTP service that the
website's frontend (running in your own browser, on your own machine)
talks to directly over `localhost`.

Reference implementation this was adapted from:
[Valorant-instalocker-TUI](https://github.com/techcrism/Valorant-instalocker-TUI)
(full-featured TUI with map-based profiles, shortcuts, multi-language
support, etc.). This is a deliberately minimal port — just the core
lock-on-select mechanism, controlled via a tiny local API instead of a
terminal.

## Setup (Windows, one click)

Download `instalocker.exe` from the
[latest release](https://github.com/lephorx/instalocker/releases/latest)
and double-click it. No Python needed. A console window opens and stays
open while it runs — that's normal, just leave it be while you play.

Windows SmartScreen may warn about it since the .exe isn't code-signed
(no cert for that) — click "More info" → "Run anyway".

## Setup (from source)

For other platforms, or if you'd rather run it from source:

```bash
git clone git@github.com:lephorx/instalocker.git
cd instalocker

# set up environment. Example:
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate

pip install -r requirements.txt
python run.py
```

Requires Python 3.9+. On Windows you can also just double-click
`run.bat`, which does the venv/install steps above automatically on
first run.

Either way: leave it running in the background — it reconnects
automatically whenever Valorant is open, across as many matches and game
sessions as you like, so you only need to start it once. Then open
val-skin-catch's **Instalock** tab: it detects the helper automatically,
shows your connection status, and gives you an agent-select screen with
every agent's portrait. Choose **Instalock** (locks the instant
agent-select starts) or **Select only** (picks the agent but leaves it
unlocked, so you can still change your mind in-game) at the top, then
click agents to build a priority order — if your first pick is already
taken by a teammate, the next one in your list is tried instead. It
re-applies automatically every time you reach agent-select until you
change or disarm it.

**Map-based profiles**: tick "Use map-based profiles" and pick a map from
the dropdown to build a separate agent chain just for that map (e.g.
always try Jett→Reyna on Ascent, Sova→Cypher on Icebox). While enabled,
whichever map you're actually on takes priority over the global chain;
maps with no profile set fall back to it.

**Dodge**: a "Dodge this match" button appears whenever you're in
agent-select, in case you want out of a bad lobby without alt-tabbing.
Confirms before acting since it counts as a real dodge (queue penalty
and all) — this doesn't bypass that, just saves you leaving the game.

## Local API

For reference (the val-skin-catch website is the intended client, but
these are plain HTTP if you want to script against them yourself):

- `GET /status` → `{connected, player_name, region, agent_chain, mode, map_profiles_enabled, current_map, in_pregame, last_locked}`
- `GET /agents` → `{agents: [{name, uuid, portrait, base_content, owned}, ...]}` (`owned` is
  `true`/`false` once connected, `null` if not known yet -- agents you
  haven't unlocked on this account are unclickable in the website's grid)
- `GET /maps` → `{maps: [{name, url}, ...]}`
- `POST /agent {"agents": ["jett", "reyna"], "mode": "lock"}` → arms an ordered fallback
  chain (`mode` is `"lock"` or `"select"`; omit to keep the current mode). A
  single-item array is a plain instalock.
- `DELETE /agent` → disarms
- `GET /map-profiles` → `{map_profiles: {"<map url>": ["jett", ...]}, enabled}`
- `POST /map-profile {"map_url": "...", "agents": ["sova", "cypher"]}` → sets a map's
  chain (empty `agents` clears it)
- `POST /map-profiles/toggle {"enabled": true}` → turns map-based selection on/off
- `POST /dodge` → quits the current pregame match (only works while actually in agent-select)

Only requests from val-skin-catch's known origins are allowed (see
`ALLOWED_ORIGINS` in `helper.py`) — not a wildcard, since anything
listening on localhost is otherwise reachable from any page you happen
to have open, not just that site.

## Disclaimer

This automates an official Riot Games API endpoint and carries some risk
of action against your account under Riot's Terms of Service. Use at your
own risk, on your own account only.
