"""An MCP server exposing the bot over stdio, for use from opencode or any
other MCP client.

Run it directly:

    python3 -m luantibot.mcp_server

or configure it as a stdio MCP server. Connection details can come from the
environment (LUANTI_HOST, LUANTI_PORT, LUANTI_USER, LUANTI_PASSWORD) so the
`connect` tool can be called with no arguments.

Transport is newline-delimited JSON-RPC 2.0 on stdin/stdout, which is what the
MCP stdio transport specifies. Nothing outside the standard library is used.

The bot is blocking and must be pumped continuously to stay connected, so it
lives on a worker thread: tool calls are queued onto that thread, and whenever
no call is in flight the thread keeps stepping the bot.
"""
import json
import os
import queue
import sys
import threading
import traceback

from .bot import Bot, BotError, DamageInterrupt
from .env import load as _load_env


class ToolError(Exception):
    """An error already formatted for the client; reported verbatim."""

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "luantibot", "version": "0.1.0"}


# --------------------------------------------------------------------------
# bot worker thread
# --------------------------------------------------------------------------

class BotSession:
    """Owns the Bot and runs every action on a single thread."""

    def __init__(self):
        self.bot = None
        self.jobs = queue.Queue()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="luantibot-mcp")
        self.thread.start()

    def _run(self):
        while True:
            try:
                fn, holder = self.jobs.get(timeout=0.05)
            except queue.Empty:
                bot = self.bot
                if bot is not None:
                    try:
                        bot._interrupt_armed = False
                        bot.step()
                    except Exception:
                        pass            # a dead link is reported by the next call
                continue
            bot = self.bot
            if bot is not None:
                bot._interrupt_armed = True
            try:
                holder["result"] = fn()
            except DamageInterrupt as e:
                # The action was aborted because something attacked us; hand
                # the events back so the caller can decide what to do.
                holder["result"] = {"interrupted": "damage",
                                    "events": e.events,
                                    "hp": self.bot.hp if self.bot else None}
            except Exception as e:
                holder["error"] = f"{type(e).__name__}: {e}"
            finally:
                if bot is not None:
                    bot._interrupt_armed = False
                holder["done"].set()

    def submit(self, fn, timeout=600.0):
        holder = {"done": threading.Event()}
        self.jobs.put((fn, holder))
        if not holder["done"].wait(timeout):
            raise TimeoutError("bot action timed out")
        if "error" in holder:
            raise ToolError(holder["error"])
        result = holder.get("result")
        # Surface any damage taken while the action ran, even if it
        # finished normally, so the caller can react to being attacked.
        bot = self.bot
        if bot is not None:
            events = bot.take_damage_events()
            if events and isinstance(result, dict) and "events" not in result:
                result = dict(result, events=events)
        return result

    def require(self):
        if self.bot is None:
            raise RuntimeError("not connected -- call the connect tool first")
        return self.bot


SESSION = BotSession()


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def _pos(bot):
    return list(bot.node_pos)


def t_connect(host=None, port=None, username=None, password=None,
              wait_for_map=15):
    _load_env()                     # pick up .env edits without a restart
    host = host or os.environ.get("LUANTI_HOST")
    if not host:
        raise ToolError("no server host: pass host= or set LUANTI_HOST")
    raw_port = port or os.environ.get("LUANTI_PORT")
    if not raw_port:
        raise ToolError("no port: pass port= or set LUANTI_PORT")
    try:
        port = int(raw_port)
    except (TypeError, ValueError):
        raise ToolError(f"port must be a number, got {raw_port!r}")
    username = username or os.environ.get("LUANTI_USER")
    if not username:
        raise ToolError("no username: pass username= or set LUANTI_USER")
    password = password if password is not None else os.environ.get(
        "LUANTI_PASSWORD", "")

    def job():
        if SESSION.bot is not None:
            try:
                SESSION.bot.disconnect()
            except Exception:
                pass
            SESSION.bot = None
        bot = Bot(host, port, username, password)
        bot.connect(timeout=90)
        SESSION.bot = bot
        bot.wait(float(wait_for_map))
        return {"connected": True, "username": username, "position": _pos(bot),
                "hp": bot.hp, "blocks_loaded": len(bot.world.blocks),
                "privileges": sorted(bot.client.privileges)}
    return SESSION.submit(job, timeout=180)


def t_disconnect():
    def job():
        bot = SESSION.bot
        if bot is None:
            return {"connected": False}
        bot.disconnect()
        SESSION.bot = None
        return {"connected": False}
    return SESSION.submit(job, timeout=30)


def t_status():
    def job():
        bot = SESSION.require()
        return {
            "position": _pos(bot),
            "exact_position": [round(v, 2) for v in bot.position],
            "hp": bot.hp, "dead": bot.dead,
            "standing_on": bot.block_at((bot.node_pos[0], bot.node_pos[1] - 1,
                                         bot.node_pos[2])),
            "blocks_loaded": len(bot.world.blocks),
            "lowest_y_known": bot.world.lowest_loaded_y(),
            "hostiles_nearby": [e.name for e in bot.hostiles(16)],
            "players_nearby": [e.name for e in bot.players_nearby()],
        }
    return SESSION.submit(job, timeout=30)


def t_inventory():
    def job():
        bot = SESSION.require()
        return {"items": [{"slot": i, "name": s.name, "count": s.count}
                          for i, s in bot.inventory.non_empty()],
                "wield_index": bot.client.wield_index}
    return SESSION.submit(job, timeout=30)


def t_goto(x, y, z, tolerance=1, timeout=180):
    def job():
        bot = SESSION.require()
        ok = bot.goto((int(x), int(y), int(z)), tolerance=int(tolerance),
                      timeout=float(timeout))
        return {"arrived": ok, "position": _pos(bot), "hp": bot.hp}
    return SESSION.submit(job, timeout=float(timeout) + 60)


def t_place(x, y, z, item=None):
    def job():
        bot = SESSION.require()
        pos = (int(x), int(y), int(z))
        # Stand where you can see the spot first; placing is aiming at
        # a node face, and you cannot aim at what you cannot see.
        if not bot.in_reach(pos) and not bot.approach(pos, timeout=60.0):
            return {"placed": False, "line_of_sight": None,
                    "reason": f"could not get within reach of {pos}"}
        if not bot.has_line_of_sight(pos):
            return {"placed": False, "line_of_sight": False,
                    "reason": f"no line of sight to {pos}"}
        ok = bot.place_block(pos, item=item)
        return {"placed": ok, "line_of_sight": True,
                "node": bot.block_at(pos)}
    return SESSION.submit(job, timeout=120)


def t_chat(message):
    def job():
        SESSION.require().chat(str(message))
        return {"sent": True}
    return SESSION.submit(job, timeout=30)


def t_find_blocks(name, limit=20, radius=None):
    def job():
        bot = SESSION.require()
        r = float(radius) if radius is not None else None
        hits = bot.scan(name, limit=int(limit) or None, visible_only=True,
                        radius=r)
        out = {"name": name, "count": len(hits),
               "note": "only blocks with a clear line of sight are reported",
               "positions": [list(p) for p in hits[:int(limit)]]}
        if r is not None:
            out["radius"] = r
        return out
    return SESSION.submit(job, timeout=120)


def t_node_at(x, y, z):
    def job():
        bot = SESSION.require()
        pos = (int(x), int(y), int(z))
        d = bot.world.get_def(pos)
        return {"name": bot.block_at(pos),
                "walkable": None if d is None else d.walkable,
                "diggable": None if d is None else d.diggable,
                "damage_per_second": None if d is None else d.damage_per_second,
                "metadata": {k: v.decode("utf-8", "replace")[:200]
                             for k, v in (bot.meta_at(pos) or {}).items()},
                "inventory": bot.node_items(pos)}
    return SESSION.submit(job, timeout=30)


def t_craft(item, times=1):
    def job():
        bot = SESSION.require()
        stack = bot.craft_item(item, times=int(times))
        return {"crafted": None if stack is None else
                {"name": stack.name, "count": stack.count}}
    return SESSION.submit(job, timeout=120)


def t_smelt(x, y, z, item, fuel, count=1, timeout=180):
    def job():
        bot = SESSION.require()
        pos = (int(x), int(y), int(z))
        # Opening a furnace is pointing at it: stand in range and in
        # sight of it before loading it.
        if not bot.in_reach(pos) and not bot.approach(pos, timeout=60.0):
            return {"output": None, "line_of_sight": None,
                    "reason": f"could not get within reach of the furnace "
                              f"at {pos}"}
        if not bot.has_line_of_sight(pos):
            return {"output": None, "line_of_sight": False,
                    "reason": f"no line of sight to the furnace at {pos}"}
        return {"output": bot.smelt(pos, item, fuel,
                                    count=int(count), timeout=float(timeout)),
                "line_of_sight": True}
    return SESSION.submit(job, timeout=float(timeout) + 120)


def t_break_node_at(x, y, z, timeout=30):
    def job():
        bot = SESSION.require()
        pos = (int(x), int(y), int(z))
        before = bot.block_at(pos)
        slot, secs = bot.best_tool_for(pos)
        # Walk in first so line of sight is judged from where the bot
        # will actually stand, then refuse to dig blind.
        if not bot.in_reach(pos) and not bot.approach(
                pos, timeout=min(float(timeout), 60.0)):
            return {"broken": False, "line_of_sight": None, "was": before,
                    "reason": f"could not get within reach of {pos}"}
        if not bot.has_line_of_sight(pos):
            return {"broken": False, "line_of_sight": False, "was": before,
                    "reason": f"no line of sight to {pos}"}
        ok = bot.dig(pos, timeout=float(timeout), approach=False)
        return {"broken": ok, "line_of_sight": True, "was": before,
                "now": bot.block_at(pos), "tool_slot": slot,
                "expected_seconds": secs}
    return SESSION.submit(job, timeout=float(timeout) + 120)


def t_defend(enable=None, radius=8):
    """Arm or disarm passive defence; returns status immediately."""
    def job():
        bot = SESSION.require()
        if enable is not None:
            if enable:
                bot.auto_defend_radius = float(radius)
            else:
                bot.auto_defend_radius = None
        active = bot.auto_defend_radius
        return {"active": active is not None, "radius": active,
                "kills": bot.auto_defend_kills, "hp": bot.hp,
                "hostiles_nearby": [e.name for e in bot.hostiles(
                    active if active is not None else float(radius))]}
    return SESSION.submit(job, timeout=30)


def t_entities(radius=24):
    def job():
        bot = SESSION.require()
        import math
        out = []
        for e in bot.entities.values():
            d = math.dist(e.position, bot.position)
            if d <= float(radius):
                out.append({"id": e.id, "name": e.name, "hp": e.hp,
                            "is_player": e.is_player,
                            "position": [round(v, 1) for v in e.position],
                            "distance": round(d, 1)})
        out.sort(key=lambda r: r["distance"])
        return {"entities": out[:40]}
    return SESSION.submit(job, timeout=30)


def t_look(size=10, step_deg=5, max_distance=32, yaw=None, pitch=None):
    def job():
        bot = SESSION.require()
        return bot.look_grid(
            size=int(size), step_deg=float(step_deg),
            max_distance=float(max_distance),
            yaw=None if yaw is None else float(yaw),
            pitch=None if pitch is None else float(pitch))
    return SESSION.submit(job, timeout=60)


def _num(desc, default=None):
    d = {"type": "number", "description": desc}
    if default is not None:
        d["default"] = default
    return d


def _str(desc, default=None):
    d = {"type": "string", "description": desc}
    if default is not None:
        d["default"] = default
    return d


TOOLS = [
    {"name": "connect", "fn": t_connect,
     "description": "Connect the bot to a Luanti server. host, port and "
                    "username are required with no defaults: pass them as "
                    "arguments or set LUANTI_HOST, LUANTI_PORT and "
                    "LUANTI_USER (password via LUANTI_PASSWORD).",
     "schema": {"type": "object", "properties": {
         "host": _str("server hostname"), "port": _num("UDP port"),
         "username": _str("account name"), "password": _str("account password"),
         "wait_for_map": _num("seconds to wait for the map to stream", 15)}}},
    {"name": "disconnect", "fn": t_disconnect,
     "description": "Disconnect the bot.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "status", "fn": t_status,
     "description": "Position, health, surroundings and nearby players/hostiles.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "inventory", "fn": t_inventory,
     "description": "List the bot's inventory slots.",
     "schema": {"type": "object", "properties": {}}},
    {"name": "goto", "fn": t_goto,
     "description": "Pathfind and walk to a coordinate.",
     "schema": {"type": "object", "properties": {
         "x": _num("x"), "y": _num("y"), "z": _num("z"),
         "tolerance": _num("how close is close enough, in nodes", 1),
         "timeout": _num("seconds", 180)},
         "required": ["x", "y", "z"]}},
    {"name": "place", "fn": t_place,
     "description": "Place a block at a position. Requires the bot to see "
                    "the spot first; refuses when something opaque is in "
                    "the way.",
     "schema": {"type": "object", "properties": {
         "x": _num("x"), "y": _num("y"), "z": _num("z"),
         "item": _str("item name to place, e.g. mcl_core:cobble")},
         "required": ["x", "y", "z"]}},
    {"name": "chat", "fn": t_chat,
     "description": "Send a chat message or a server command such as /giveme.",
     "schema": {"type": "object", "properties": {"message": _str("text")},
                "required": ["message"]}},
    {"name": "find_blocks", "fn": t_find_blocks,
     "description": "Search every streamed map block for a node type, nearest "
                    "first, reporting only blocks the bot has a clear line of "
                    "sight to. Buried nodes are hidden behind the terrain in "
                    "front of them, so this finds what is actually visible. "
                    "Pass radius to cap the search distance from the bot. "
                    "Use this to locate ore.",
     "schema": {"type": "object", "properties": {
         "name": _str("node name, e.g. mcl_core:stone_with_diamond"),
         "limit": _num("maximum positions to return", 20),
         "radius": _num("only report blocks within this many nodes")},
         "required": ["name"]}},
    {"name": "node_at", "fn": t_node_at,
     "description": "Describe the node at a position, including any metadata "
                    "and container contents.",
     "schema": {"type": "object", "properties": {
         "x": _num("x"), "y": _num("y"), "z": _num("z")},
         "required": ["x", "y", "z"]}},
    {"name": "craft", "fn": t_craft,
     "description": "Craft a known item by name. 3x3 recipes need the bot to "
                    "have opened a crafting table first.",
     "schema": {"type": "object", "properties": {
         "item": _str("item to craft, e.g. mcl_tools:pick_stone"),
         "times": _num("how many times to run the recipe", 1)},
         "required": ["item"]}},
    {"name": "smelt", "fn": t_smelt,
     "description": "Load a furnace at a position with input and fuel, then "
                    "collect the output. Requires line of sight to the "
                    "furnace, as opening it means pointing at it.",
     "schema": {"type": "object", "properties": {
         "x": _num("furnace x"), "y": _num("furnace y"), "z": _num("furnace z"),
         "item": _str("item to smelt"), "fuel": _str("fuel item"),
         "count": _num("how many to smelt", 1), "timeout": _num("seconds", 180)},
         "required": ["x", "y", "z", "item", "fuel"]}},
    {"name": "break_node_at", "fn": t_break_node_at,
     "description": "Break the node at a position if it is visible: the bot "
                    "walks into range, checks line of sight from its eyes to "
                    "the node, and refuses when something opaque is in the "
                    "way.",
     "schema": {"type": "object", "properties": {
         "x": _num("x"), "y": _num("y"), "z": _num("z"),
         "timeout": _num("seconds", 30)}, "required": ["x", "y", "z"]}},
    {"name": "defend", "fn": t_defend,
     "description": "Toggle passive defence: while active the bot punches "
                    "hostile mobs within reach and line of sight as it goes "
                    "about its other work -- it never moves on its own. Call "
                    "without enable to just read status.",
     "schema": {"type": "object", "properties": {
         "enable": {"type": "boolean",
                    "description": "true to arm, false to disarm"},
         "radius": _num("nodes within which hostiles draw punches", 8)}}},
    {"name": "entities", "fn": t_entities,
     "description": "List nearby entities: players, mobs and item drops.",
     "schema": {"type": "object", "properties": {
         "radius": _num("search radius in nodes", 24)}}},
    {"name": "look", "fn": t_look,
     "description": "Look around by casting a grid of raycasts from the "
                    "bot's eyes, fanning out around its view direction. "
                    "Each row of the result is a line of sight cells "
                    "(node name, null for open air, '?' for unloaded "
                    "map), top row first.",
     "schema": {"type": "object", "properties": {
         "size": _num("grid size, e.g. 10 for a 10x10 grid", 10),
         "step_deg": _num("degrees between rays", 5),
         "max_distance": _num("how far each ray reaches", 32),
         "yaw": _num("override the bot's current yaw in degrees"),
         "pitch": _num("override the bot's current pitch in degrees")}}},
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

def _result(request_id, payload):
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def _error(request_id, code, message):
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message}}


def handle(message):
    """Handle one JSON-RPC message; return a response, or None for notifications."""
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        # Echo the client's protocol version when we can speak it.
        asked = params.get("protocolVersion", PROTOCOL_VERSION)
        return _result(request_id, {
            "protocolVersion": asked if isinstance(asked, str) else PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
        })

    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["schema"]} for t in TOOLS]})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            return _error(request_id, -32602, f"unknown tool: {name}")
        try:
            payload = tool["fn"](**args)
            text = json.dumps(payload, indent=2, default=str)
            return _result(request_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False})
        except ToolError as e:
            return _result(request_id, {
                "content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:
            return _result(request_id, {
                "content": [{"type": "text",
                             "text": f"{type(e).__name__}: {e}"}],
                "isError": True})

    if request_id is None:
        return None
    return _error(request_id, -32601, f"unknown method: {method}")


def main():
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            response = handle(message)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            response = _error(message.get("id"), -32603, "internal error")
        if response is not None:
            out.write(json.dumps(response) + "\n")
            out.flush()


if __name__ == "__main__":
    main()
