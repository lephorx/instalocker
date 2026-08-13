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

## Setup

```bash
git clone git@github.com:lephorx/instalocker.git
cd instalocker
pip install -r requirements.txt
uvicorn helper:app --port 13337
```

Requires Python 3.9+. Leave it running in the background — it
reconnects automatically whenever Valorant is open, across as many
matches and game sessions as you like, so you only need to start it
once. Then open val-skin-catch's **Instalock** tab: it detects the
helper automatically, shows your connection status, and lets you pick
which agent to arm. Once armed, it locks that agent every time you reach
agent-select until you change or disarm it.

## Local API

For reference (the val-skin-catch website is the intended client, but
these are plain HTTP if you want to script against them yourself):

- `GET /status` → `{connected, player_name, region, armed_agent, last_locked}`
- `GET /agents` → `{agents: [...]}`
- `POST /agent {"agent": "jett"}` → arms an agent
- `DELETE /agent` → disarms

Only requests from val-skin-catch's known origins are allowed (see
`ALLOWED_ORIGINS` in `helper.py`) — not a wildcard, since anything
listening on localhost is otherwise reachable from any page you happen
to have open, not just that site.

## Disclaimer

This automates an official Riot Games API endpoint and carries some risk
of action against your account under Riot's Terms of Service. Use at your
own risk, on your own account only.
