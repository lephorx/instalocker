"""Persistent local instalocker service + tiny local HTTP API.

Runs on your own gaming PC (NOT on val-skin-catch's server -- see
../README.md for why). Connects to the Riot Client and stays connected,
auto-locking whichever agent is currently "armed" every time a match
reaches agent-select -- not just once. The val-skin-catch website talks
to this over localhost so you can pick/change the armed agent from the
browser, which is the only way "instalock" can be part of the website
itself: the website's own server has no path to your PC's Riot Client,
only your browser (running on the same machine as this helper) does.

Usage:
    pip install -r requirements.txt
    uvicorn helper:app --port 13337

Leave it running in the background; the website polls it automatically.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from typing import Optional

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from valclient.client import Client
from valclient.exceptions import HandshakeError

AGENTS_API_URL = "https://valorant-api.com/v1/agents?isPlayableCharacter=true"
SHOOTER_LOG_PATH = os.path.expandvars(r"%LocalAppData%\VALORANT\Saved\Logs\ShooterGame.log")
POLL_INTERVAL_SECONDS = 1
RECONNECT_INTERVAL_SECONDS = 5

# Riot's well-known entitlement item-type UUID for "agent" -- used to ask
# the store entitlements endpoint specifically which agents this account
# owns (new agents need unlocking via XP/contract or purchase, they're
# not all available from the start).
AGENT_ITEM_TYPE = "01bb38e1-da47-4e6a-9b3d-945fe4655707"

# Only these origins may control this local service from a browser -- not
# a wildcard, since anything on localhost is otherwise reachable from any
# page the user happens to have open, not just this site.
ALLOWED_ORIGINS = {
    "https://val-skin-catch01.tail67576c.ts.net",
    "http://192.168.1.207:3000",
    "http://localhost:5173",  # frontend dev server
}


MODES = ("lock", "select")


class AgentRequest(BaseModel):
    agent: Optional[str] = None
    mode: Optional[str] = None  # "lock" (instalock) or "select" (pick only, don't lock)


class State:
    def __init__(self):
        self.connected = False
        self.player_name: Optional[str] = None
        self.region: Optional[str] = None
        self.armed_agent: Optional[str] = None
        self.armed_agent_uuid: Optional[str] = None
        self.mode: str = "lock"
        self.agents: list[dict] = []  # [{name, uuid, portrait}, ...]
        # None = not fetched yet (e.g. not connected) -- distinct from an
        # empty set, which would mean "owns nothing". Frontend treats
        # None as "unknown, don't lock anyone out" rather than assuming
        # nothing is owned.
        self.owned_agent_uuids: Optional[set[str]] = None
        self.last_locked: Optional[str] = None
        self.seen_match_ids: set[str] = set()


state = State()


def find_agent(name: str) -> Optional[dict]:
    target = name.strip().lower()
    return next((a for a in state.agents if a["name"].lower() == target), None)


def detect_region() -> Optional[str]:
    try:
        with open(SHOOTER_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "https://glz-" in line:
                    return line.split("https://glz-")[1].split("-")[0].lower()
    except FileNotFoundError:
        pass
    return None


def load_agents() -> list[dict]:
    res = requests.get(AGENTS_API_URL, timeout=10)
    res.raise_for_status()
    agents = [
        {
            "name": a["displayName"],
            "uuid": a["uuid"],
            "portrait": a.get("fullPortraitV2") or a.get("bustPortrait") or a.get("displayIcon"),
            # The original launch roster (Brimstone/Jett/Phoenix/Sage/Sova)
            # is owned by every account from creation -- Riot never grants
            # an explicit store entitlement for these, so they'd otherwise
            # show up as locked despite always being playable.
            "base_content": bool(a.get("isBaseContent")),
        }
        for a in res.json()["data"]
    ]
    agents.sort(key=lambda a: a["name"])
    return agents


def fetch_owned_agent_uuids(client: Client) -> Optional[set[str]]:
    """Best-effort -- if Riot's response shape doesn't match what's
    expected here, fail open (return None, meaning "unknown") rather than
    risk incorrectly locking the player out of agents they actually own.

    Every real account owns several starting agents, so an empty result
    is never actually correct -- it means parsing missed the real shape
    (e.g. requesting a specific item_type in the URL may return the list
    at the top level instead of wrapped in EntitlementsByTypes, depending
    on the Riot API version), not that the account owns nothing. Treated
    the same as a hard failure: fail open instead of locking everything.
    """
    try:
        data = client.store_fetch_entitlements(item_type=AGENT_ITEM_TYPE)
        entitlements = None
        for entry in data.get("EntitlementsByTypes") or []:
            if entry.get("ItemTypeID") == AGENT_ITEM_TYPE:
                entitlements = entry.get("Entitlements")
                break
        if entitlements is None:
            entitlements = data.get("Entitlements")
        if not entitlements:
            return None
        owned = {e["ItemID"] for e in entitlements if e.get("ItemID")}
        return owned or None
    except Exception:
        return None


async def lock_loop(client: Client) -> None:
    """Runs for as long as the Riot Client connection stays alive --
    auto-locks (or just selects, in "select" mode) the armed agent for
    every match reached, not just the first one, and picks up agent/mode
    changes made from the website mid-loop."""
    while True:
        presence = await asyncio.to_thread(client.fetch_presence, client.puuid)
        match_state = presence.get("matchPresenceData", {}).get("sessionLoopState")
        if match_state == "PREGAME" and state.armed_agent_uuid:
            match = await asyncio.to_thread(client.pregame_fetch_match)
            match_id = match.get("ID")
            if match_id and match_id not in state.seen_match_ids:
                state.seen_match_ids.add(match_id)
                if state.mode == "select":
                    await asyncio.to_thread(client.pregame_select_character, state.armed_agent_uuid)
                else:
                    await asyncio.to_thread(client.pregame_lock_character, state.armed_agent_uuid)
                state.last_locked = state.armed_agent
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def connection_loop() -> None:
    """Keeps (re)connecting to the Riot Client indefinitely -- covers
    Valorant not being open yet, being closed and reopened, or a match
    ending -- so this helper only needs to be started once per session,
    not re-run before every match."""
    region = os.environ.get("INSTALOCK_REGION")
    while True:
        if not state.agents:
            try:
                state.agents = await asyncio.to_thread(load_agents)
            except Exception:
                await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)
                continue

        client = Client(region=region or detect_region() or "eu")
        try:
            await asyncio.to_thread(client.activate)
        except HandshakeError:
            state.connected = False
            await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)
            continue

        state.connected = True
        state.player_name = client.player_name
        state.region = client.region
        state.owned_agent_uuids = await asyncio.to_thread(fetch_owned_agent_uuids, client)

        try:
            await lock_loop(client)
        except Exception:
            pass  # connection dropped (match/session ended, client closed) -- reconnect
        finally:
            state.connected = False
            state.owned_agent_uuids = None
            await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(connection_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)


class PrivateNetworkCORSMiddleware(BaseHTTPMiddleware):
    """A public HTTPS page fetching a private-network address (localhost)
    needs more than plain CORS -- Chrome's Private Network Access check
    also requires Access-Control-Allow-Private-Network: true on the
    preflight response."""

    async def dispatch(self, request: Request, call_next):
        origin = request.headers.get("origin")
        response = JSONResponse({}) if request.method == "OPTIONS" else await call_next(request)
        if origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response


app.add_middleware(PrivateNetworkCORSMiddleware)


@app.get("/status")
async def status():
    return {
        "connected": state.connected,
        "player_name": state.player_name,
        "region": state.region,
        "armed_agent": state.armed_agent,
        "mode": state.mode,
        "last_locked": state.last_locked,
    }


def is_owned(agent: dict, owned_uuids: Optional[set[str]]) -> Optional[bool]:
    if agent["base_content"]:
        return True
    if owned_uuids is None:
        return None
    return agent["uuid"] in owned_uuids


@app.get("/agents")
async def agents():
    if not state.agents:
        state.agents = await asyncio.to_thread(load_agents)
    owned = state.owned_agent_uuids
    return {"agents": [{**a, "owned": is_owned(a, owned)} for a in state.agents]}


@app.post("/agent")
async def set_agent(body: AgentRequest):
    if body.agent is None:
        state.armed_agent = None
        state.armed_agent_uuid = None
        return {"armed_agent": None, "mode": state.mode}

    if body.mode is not None:
        if body.mode not in MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {MODES}")
        state.mode = body.mode

    if not state.agents:
        state.agents = await asyncio.to_thread(load_agents)

    agent = find_agent(body.agent)
    if not agent:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{body.agent}'")

    if is_owned(agent, state.owned_agent_uuids) is False:
        raise HTTPException(status_code=400, detail=f"You don't own '{agent['name']}'")

    state.armed_agent = agent["name"]
    state.armed_agent_uuid = agent["uuid"]
    return {"armed_agent": state.armed_agent, "mode": state.mode}


@app.delete("/agent")
async def clear_agent():
    state.armed_agent = None
    state.armed_agent_uuid = None
    return {"armed_agent": None, "mode": state.mode}
