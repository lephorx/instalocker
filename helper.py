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

# Only these origins may control this local service from a browser -- not
# a wildcard, since anything on localhost is otherwise reachable from any
# page the user happens to have open, not just this site.
ALLOWED_ORIGINS = {
    "https://val-skin-catch01.tail67576c.ts.net",
    "http://192.168.1.207:3000",
    "http://localhost:5173",  # frontend dev server
}


class AgentRequest(BaseModel):
    agent: Optional[str] = None


class State:
    def __init__(self):
        self.connected = False
        self.player_name: Optional[str] = None
        self.region: Optional[str] = None
        self.armed_agent: Optional[str] = None
        self.armed_agent_uuid: Optional[str] = None
        self.agents: dict[str, str] = {}  # lowercase display name -> uuid
        self.last_locked: Optional[str] = None
        self.seen_match_ids: set[str] = set()


state = State()


def detect_region() -> Optional[str]:
    try:
        with open(SHOOTER_LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "https://glz-" in line:
                    return line.split("https://glz-")[1].split("-")[0].lower()
    except FileNotFoundError:
        pass
    return None


def load_agents() -> dict[str, str]:
    res = requests.get(AGENTS_API_URL, timeout=10)
    res.raise_for_status()
    return {a["displayName"].lower(): a["uuid"] for a in res.json()["data"]}


async def lock_loop(client: Client) -> None:
    """Runs for as long as the Riot Client connection stays alive --
    auto-locks the armed agent for every match reached, not just the
    first one, and picks up agent changes made from the website mid-loop."""
    while True:
        presence = await asyncio.to_thread(client.fetch_presence, client.puuid)
        match_state = presence.get("matchPresenceData", {}).get("sessionLoopState")
        if match_state == "PREGAME" and state.armed_agent_uuid:
            match = await asyncio.to_thread(client.pregame_fetch_match)
            match_id = match.get("ID")
            if match_id and match_id not in state.seen_match_ids:
                state.seen_match_ids.add(match_id)
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

        try:
            await lock_loop(client)
        except Exception:
            pass  # connection dropped (match/session ended, client closed) -- reconnect
        finally:
            state.connected = False
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
        "last_locked": state.last_locked,
    }


@app.get("/agents")
async def agents():
    if not state.agents:
        state.agents = await asyncio.to_thread(load_agents)
    return {"agents": sorted(state.agents.keys())}


@app.post("/agent")
async def set_agent(body: AgentRequest):
    if body.agent is None:
        state.armed_agent = None
        state.armed_agent_uuid = None
        return {"armed_agent": None}

    if not state.agents:
        state.agents = await asyncio.to_thread(load_agents)

    uuid = state.agents.get(body.agent.strip().lower())
    if not uuid:
        raise HTTPException(status_code=400, detail=f"Unknown agent '{body.agent}'")

    state.armed_agent = body.agent.strip().lower()
    state.armed_agent_uuid = uuid
    return {"armed_agent": state.armed_agent}


@app.delete("/agent")
async def clear_agent():
    state.armed_agent = None
    state.armed_agent_uuid = None
    return {"armed_agent": None}
