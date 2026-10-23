"""luantibot -- a Mineflayer-style bot library for Luanti (Minetest) servers.

    from luantibot import Bot

    bot = Bot("example.com", 30000, "name", "password")
    bot.connect()
    bot.goto((10, 12, -4))

This is a standalone protocol client, not a mod: it speaks Luanti's UDP
protocol directly, which is what allows it to move and act on its own. Luanti's
client-side mod API is deliberately read-only with respect to player input, so a
CSM cannot do this.

Requires nothing outside the standard library (libzstd is loaded via ctypes).
"""
from .env import load as _load_env
_load_env()

from .auth import translate_password
from .bot import Bot, BotError
from .client import LuantiClient, LoginError
from .connection import Connection, Disconnected
from .entities import Entity, EntityTracker
from .inventory import Inventory, ItemStack
from .itemdef import ItemDef, ItemDefs, ToolCapabilities, dig_time
from .nodedef import NodeDef, NodeDefs
from . import recipes
from .pathfinder import Pathfinder, PathResult, Step
from .world import World

__version__ = "0.1.0"
__all__ = [
    "Bot", "BotError", "LuantiClient", "LoginError", "Connection",
    "Disconnected", "World", "Pathfinder", "PathResult", "Step",
    "NodeDef", "NodeDefs", "Inventory", "ItemStack", "Entity",
    "EntityTracker", "translate_password", "recipes",
    "ItemDef", "ItemDefs", "ToolCapabilities", "dig_time",
]
