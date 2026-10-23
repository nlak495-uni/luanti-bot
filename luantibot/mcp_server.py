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

from .bot import Bot, BotError
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
                        bot.step()
                    except Exception:
                        pass            # a dead link is reported by the next call
                continue
            try:
                holder["result"] = fn()
            except Exception as e:
                holder["error"] = f"{type(e).__name__}: {e}"
            finally:
                holder["done"].set()

    def submit(self, fn, timeout=600.0):
        holder = {"done": threading.Event()}
        self.jobs.put((fn, holder))
        if not holder["done"].wait(timeout):
            raise TimeoutError("bot action timed out")
        if "error" in holder:
            raise ToolError(holder["error"])
        return holder.get("result")

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


def t_dig(x, y, z, timeout=30):
    def job():
        bot = SESSION.require()
        pos = (int(x), int(y), int(z))
        before = bot.block_at(pos)
        slot, secs = bot.best_tool_for(pos)
        ok = bot.dig(pos, timeout=float(timeout))
        return {"dug": ok, "was": before, "now": bot.block_at(pos),
                "tool_slot": slot, "expected_seconds": secs}
    return SESSION.submit(job, timeout=float(timeout) + 120)


def t_place(x, y, z, item=None):
    def job():
        bot = SESSION.require()
        pos = (int(x), int(y), int(z))
        ok = bot.place_block(pos, item=item)
        return {"placed": ok, "node": bot.block_at(pos)}
    return SESSION.submit(job, timeout=120)


def t_chat(message):
    def job():
        SESSION.require().chat(str(message))
        return {"sent": True}
    return SESSION.submit(job, timeout=30)


def t_find_blocks(name, radius=32, limit=20):
    def job():
        bot = SESSION.require()
        hits = bot.scan(name, limit=int(limit) or None)
        return {"name": name, "count": len(hits),
                "positions": [list(p) for p in hits[:int(limit)]]}
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
        return {"output": bot.smelt((int(x), int(y), int(z)), item, fuel,
                                    count=int(count), timeout=float(timeout))}
    return SESSION.submit(job, timeout=float(timeout) + 120)


def t_mine_to_depth(target_y, timeout=900):
    def job():
        bot = SESSION.require()
        ok = bot.mine_to_depth(int(target_y), timeout=float(timeout))
        return {"reached": ok, "position": _pos(bot), "hp": bot.hp,
                "lowest_y_known": bot.world.lowest_loaded_y()}
    return SESSION.submit(job, timeout=float(timeout) + 120)


def t_defend(radius=8, timeout=60):
    def job():
        bot = SESSION.require()
        killed = bot.defend(radius=float(radius), timeout=float(timeout))
        return {"killed": killed, "hp": bot.hp,
                "remaining": [e.name for e in bot.hostiles(float(radius))]}
    return SESSION.submit(job, timeout=float(timeout) + 120)


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
    {"name": "dig", "fn": t_dig,
     "description": "Break the node at a position, choosing the best tool held "
                    "and walking into range first.",
     "schema": {"type": "object", "properties": {
         "x": _num("x"), "y": _num("y"), "z": _num("z"),
         "timeout": _num("seconds", 30)}, "required": ["x", "y", "z"]}},
    {"name": "place", "fn": t_place,
     "description": "Place a block at a position.",
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
                    "first. Use this to locate ore.",
     "schema": {"type": "object", "properties": {
         "name": _str("node name, e.g. mcl_core:stone_with_diamond"),
         "limit": _num("maximum positions to return", 20)},
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
                    "collect the output.",
     "schema": {"type": "object", "properties": {
         "x": _num("furnace x"), "y": _num("furnace y"), "z": _num("furnace z"),
         "item": _str("item to smelt"), "fuel": _str("fuel item"),
         "count": _num("how many to smelt", 1), "timeout": _num("seconds", 180)},
         "required": ["x", "y", "z", "item", "fuel"]}},
    {"name": "mine_to_depth", "fn": t_mine_to_depth,
     "description": "Dig a descending staircase to a target y level, fighting "
                    "off hostiles and avoiding lava.",
     "schema": {"type": "object", "properties": {
         "target_y": _num("y level to reach"), "timeout": _num("seconds", 900)},
         "required": ["target_y"]}},
    {"name": "defend", "fn": t_defend,
     "description": "Fight off hostile mobs near the bot.",
     "schema": {"type": "object", "properties": {
         "radius": _num("search radius in nodes", 8),
         "timeout": _num("seconds per target", 60)}}},
    {"name": "entities", "fn": t_entities,
     "description": "List nearby entities: players, mobs and item drops.",
     "schema": {"type": "object", "properties": {
         "radius": _num("search radius in nodes", 24)}}},
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
