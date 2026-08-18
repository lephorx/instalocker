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
MAPS_API_URL = "https://valorant-api.com/v1/maps"
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
    # Ordered fallback chain -- tried in order, first one not already
    # picked by a teammate wins. A single-item list is a plain instalock,
    # same as before. Empty/omitted disarms.
    agents: Optional[list[str]] = None
    mode: Optional[str] = None  # "lock" (instalock) or "select" (pick only, don't lock)


class MapProfileRequest(BaseModel):
    map_url: str
    agents: list[str]  # empty clears the profile for this map


class MapProfilesToggleRequest(BaseModel):
    enabled: bool


class State:
    def __init__(self):
        self.connected = False
        self.client: Optional[Client] = None
        self.player_name: Optional[str] = None
        self.region: Optional[str] = None
        self.agent_chain: list[str] = []  # ordered agent names, tried in priority order
        self.mode: str = "lock"
        self.agents: list[dict] = []  # [{name, uuid, portrait, base_content}, ...]
        self.maps: list[dict] = []  # [{name, url}, ...]
        # None = not fetched yet (e.g. not connected) -- distinct from an
        # empty set, which would mean "owns nothing". Treated as
        # "unknown, don't lock anyone out" rather than assuming nothing
        # is owned.
        self.owned_agent_uuids: Optional[set[str]] = None
        # lowercased map url -> ordered agent name chain for that map
        self.map_profiles: dict[str, list[str]] = {}
        self.map_profiles_enabled: bool = False
        self.current_map: Optional[str] = None
        self.in_pregame: bool = False
        self.last_locked: Optional[str] = None
        self.seen_match_ids: set[str] = set()


state = State()


def find_agent(name: str) -> Optional[dict]:
    target = name.strip().lower()
    return next((a for a in state.agents if a["name"].lower() == target), None)


def find_agent_by_uuid(uuid: str) -> Optional[dict]:
    return next((a for a in state.agents if a["uuid"] == uuid), None)


def find_map_name(map_url: str) -> Optional[str]:
    if not map_url:
        return None
    target = map_url.strip().lower()
    m = next((m for m in state.maps if m["url"].lower() == target), None)
    return m["name"] if m else None


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


def load_maps() -> list[dict]:
    res = requests.get(MAPS_API_URL, timeout=10)
    res.raise_for_status()
    maps = [{"name": m["displayName"], "url": m["mapUrl"]} for m in res.json()["data"] if m.get("mapUrl")]
    maps.sort(key=lambda m: m["name"])
    return maps


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


def taken_agent_uuids(match_data: dict) -> set[str]:
    """Best-effort -- if the response shape doesn't match what's expected
    here, returns an empty set (nobody considered "taken"), which just
    means fallback picks always use the first choice in the chain -- no
    worse than not having a fallback chain at all."""
    try:
        players = (match_data.get("AllyTeam") or {}).get("Players") or []
        return {p["CharacterID"] for p in players if p.get("CharacterID")}
    except Exception:
        return set()


def pick_agent_uuid(chain_names: list[str], taken: set[str]) -> Optional[str]:
    resolved = [a["uuid"] for a in (find_agent(name) for name in chain_names) if a]
    for uuid in resolved:
        if uuid not in taken:
            return uuid
    return resolved[0] if resolved else None  # everyone in the chain is taken -- try the first choice anyway


async def lock_loop(client: Client) -> None:
    """Runs for as long as the Riot Client connection stays alive --
    auto-locks (or just selects, in "select" mode) the armed agent chain
    for every match reached, not just the first one, and picks up
    agent/mode/map-profile changes made from the website mid-loop."""
    while True:
        presence = await asyncio.to_thread(client.fetch_presence, client.puuid)
        match_presence = presence.get("matchPresenceData") or {}
        match_state = match_presence.get("sessionLoopState")
        state.in_pregame = match_state == "PREGAME"

        map_url = match_presence.get("matchMap") or ""
        state.current_map = find_map_name(map_url)

        if state.in_pregame:
            chain = state.agent_chain
            if state.map_profiles_enabled and map_url:
                profile_chain = state.map_profiles.get(map_url.lower())
                if profile_chain:
                    chain = profile_chain

            if chain:
                match = await asyncio.to_thread(client.pregame_fetch_match)
                match_id = match.get("ID")
                if match_id and match_id not in state.seen_match_ids:
                    state.seen_match_ids.add(match_id)
                    agent_uuid = pick_agent_uuid(chain, taken_agent_uuids(match))
                    if agent_uuid:
                        if state.mode == "select":
                            await asyncio.to_thread(client.pregame_select_character, agent_uuid)
                        else:
                            await asyncio.to_thread(client.pregame_lock_character, agent_uuid)
                        locked = find_agent_by_uuid(agent_uuid)
                        state.last_locked = locked["name"] if locked else None
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
        if not state.maps:
            try:
                state.maps = await asyncio.to_thread(load_maps)
            except Exception:
                pass  # non-fatal -- map profiles just won't resolve names/matches yet

        client = Client(region=region or detect_region() or "eu")
        try:
            await asyncio.to_thread(client.activate)
        except HandshakeError:
            state.connected = False
            state.client = None
            await asyncio.sleep(RECONNECT_INTERVAL_SECONDS)
            continue

        state.connected = True
        state.client = client
        state.player_name = client.player_name
        state.region = client.region
        state.owned_agent_uuids = await asyncio.to_thread(fetch_owned_agent_uuids, client)

        try:
            await lock_loop(client)
        except Exception:
            pass  # connection dropped (match/session ended, client closed) -- reconnect
        finally:
            state.connected = False
            state.client = None
            state.owned_agent_uuids = None
            state.in_pregame = False
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
        "agent_chain": state.agent_chain,
        "mode": state.mode,
        "map_profiles_enabled": state.map_profiles_enabled,
        "current_map": state.current_map,
        "in_pregame": state.in_pregame,
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
    enriched = [{**a, "owned": is_owned(a, owned)} for a in state.agents]
    # Owned/unknown (pickable) first, definitely-locked last -- each group
    # alphabetical. Sorted per-request rather than once, since ownership
    # can change across reconnects.
    enriched.sort(key=lambda a: (a["owned"] is False, a["name"]))
    return {"agents": enriched}


@app.get("/maps")
async def maps():
    if not state.maps:
        state.maps = await asyncio.to_thread(load_maps)
    return {"maps": state.maps}


def _resolve_chain(names: list[str]) -> list[str]:
    """Validates names against the known agent list and ownership,
    returning their canonical display names. Raises 400 on the first
    unknown/unowned agent."""
    resolved = []
    for name in names:
        agent = find_agent(name)
        if not agent:
            raise HTTPException(status_code=400, detail=f"Unknown agent '{name}'")
        if is_owned(agent, state.owned_agent_uuids) is False:
            raise HTTPException(status_code=400, detail=f"You don't own '{agent['name']}'")
        resolved.append(agent["name"])
    return resolved


@app.post("/agent")
async def set_agent(body: AgentRequest):
    if body.mode is not None:
        if body.mode not in MODES:
            raise HTTPException(status_code=400, detail=f"mode must be one of {MODES}")
        state.mode = body.mode

    if not body.agents:
        state.agent_chain = []
        return {"agent_chain": [], "mode": state.mode}

    if not state.agents:
        state.agents = await asyncio.to_thread(load_agents)

    state.agent_chain = _resolve_chain(body.agents)
    return {"agent_chain": state.agent_chain, "mode": state.mode}


@app.delete("/agent")
async def clear_agent():
    state.agent_chain = []
    return {"agent_chain": [], "mode": state.mode}


@app.get("/map-profiles")
async def get_map_profiles():
    return {"map_profiles": state.map_profiles, "enabled": state.map_profiles_enabled}


@app.post("/map-profile")
async def set_map_profile(body: MapProfileRequest):
    if not state.agents:
        state.agents = await asyncio.to_thread(load_agents)

    map_key = body.map_url.strip().lower()
    if not body.agents:
        state.map_profiles.pop(map_key, None)
    else:
        state.map_profiles[map_key] = _resolve_chain(body.agents)
    return {"map_profiles": state.map_profiles}


@app.post("/map-profiles/toggle")
async def toggle_map_profiles(body: MapProfilesToggleRequest):
    state.map_profiles_enabled = body.enabled
    return {"enabled": state.map_profiles_enabled}


@app.post("/dodge")
async def dodge():
    if not state.connected or not state.client:
        raise HTTPException(status_code=400, detail="Not connected to the Riot Client")
    if not state.in_pregame:
        raise HTTPException(status_code=400, detail="Not currently in agent-select")
    try:
        await asyncio.to_thread(state.client.pregame_quit_match)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Couldn't dodge: {e}")
    return {"ok": True}
