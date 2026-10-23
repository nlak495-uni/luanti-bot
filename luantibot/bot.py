"""A Mineflayer-style bot API for Luanti.

    bot = Bot("server", 30000, "name", "password")
    bot.connect()
    bot.goto((10, 12, -4))
    bot.dig(bot.find_blocks(["mcl_core:stone"], radius=8)[0])

Because the Luanti client is authoritative over its own position, movement is
performed by interpolating the player's coordinates along a path and streaming
TOSERVER_PLAYERPOS -- the server only sanity-checks the speed.
"""
import collections
import math
import time

from .client import (INTERACT_DIGGING_COMPLETED, INTERACT_PLACE,
                     INTERACT_START_DIGGING, INTERACT_STOP_DIGGING,
                     INTERACT_USE, LuantiClient)
from .pathfinder import Pathfinder

DEFAULT_WALK_SPEED = 4.0        # nodes/second, Luanti's movement_speed_walk
DEFAULT_REACH = 4.5             # nodes; servers reject interactions beyond this
DEFAULT_CLIMB_SPEED = 2.0
FALL_SPEED = 10.0
ARRIVE_EPS = 0.08
PHYSICS_DT = 0.05


def _walk_line(frm, end):
    """Yield the voxels crossed by the straight line from `frm` to `end`.

    Yields the voxel containing `end` last and never the voxel that
    contains `frm`, so callers decide what each endpoint means. Walks
    the grid one boundary crossing at a time (Amanatides & Woo).
    """
    o = [float(v) for v in frm]
    d = [end[i] - o[i] for i in range(3)]
    cur = [math.floor(v) for v in o]
    last = [math.floor(v) for v in end]
    step = [1 if v > 0 else -1 if v < 0 else 0 for v in d]
    t_max, t_delta = [], []
    for i in range(3):
        if d[i] == 0:
            t_max.append(math.inf)
            t_delta.append(math.inf)
        else:
            boundary = cur[i] + (1 if step[i] > 0 else 0)
            t_max.append((boundary - o[i]) / d[i])
            t_delta.append(abs(1.0 / d[i]))
    crossings = sum(abs(last[i] - cur[i]) for i in range(3))
    for _ in range(crossings + 3):          # exactly `crossings` + slack
        if cur == last:
            return
        axis = min(range(3), key=t_max.__getitem__)
        cur[axis] += step[axis]
        t_max[axis] += t_delta[axis]
        yield tuple(cur)


class BotError(Exception):
    pass


class Bot:
    def __init__(self, host, port=30000, username="bot", password="",
                 verbose=False, view_range=12, **pathfinder_opts):
        self.client = LuantiClient(host, port, username, password, verbose=verbose)
        self.client.wanted_range = view_range
        self.verbose = verbose
        self._pathfinder_opts = pathfinder_opts
        self.pathfinder = None
        self.listeners = {}
        self.walk_speed = DEFAULT_WALK_SPEED
        self.reach = DEFAULT_REACH
        # The server ignores any interact whose item index is outside the
        # hotbar, so tools must be moved into it before they can be used.
        self.hotbar_size = 9
        # Passive defence: while armed, every tick punches the nearest
        # hostile within reach and sight. It never moves the bot.
        self.auto_defend_radius = None
        self.auto_defend_kills = 0
        self._auto_punched = set()
        self._auto_next_punch = 0.0
        self._client_hooks()

    # ------------------------------------------------------------- plumbing

    def _client_hooks(self):
        self.client.on_chat.append(
            lambda sender, msg, kind: self.emit("chat", sender, msg, kind))
        self.client.on_block.append(lambda bp: self.emit("block", bp))
        self.client.on_death.append(lambda: self.emit("death"))

    def on(self, event, fn):
        """Register a callback: 'chat', 'block', 'move', 'dig', 'death'."""
        self.listeners.setdefault(event, []).append(fn)
        return self

    def emit(self, event, *args):
        for fn in self.listeners.get(event, ()):
            try:
                fn(*args)
            except Exception as e:
                print(f"[bot] listener for {event!r} raised: {e!r}")

    def connect(self, timeout=60):
        self.client.connect(timeout=timeout)
        self.pathfinder = Pathfinder(self.client.world, **self._pathfinder_opts)
        return self

    def disconnect(self):
        self.client.disconnect()

    # ------------------------------------------------------------- accessors

    @property
    def world(self):
        return self.client.world

    @property
    def position(self):
        """Feet position as floats."""
        return tuple(self.client.position)

    @property
    def node_pos(self):
        """The node the bot's feet occupy."""
        x, y, z = self.client.position
        return (math.floor(x + 0.5), math.floor(y + 0.5), math.floor(z + 0.5))

    @property
    def hp(self):
        return self.client.hp

    @property
    def dead(self):
        return self.client.dead

    def respawn(self):
        self.client.respawn()
        return self.wait_for(lambda: not self.client.dead, timeout=10)

    @property
    def name(self):
        return self.client.username

    @property
    def inventory(self):
        return self.client.inventory

    @property
    def entities(self):
        """Every tracked object: other players, mobs, item drops."""
        return self.client.entities.entities

    def players_nearby(self):
        return self.client.entities.players(exclude=self.name)

    def player(self, name):
        return self.client.entities.by_name(name)

    def nearest_entity(self, predicate=None):
        return self.client.entities.nearest(self.position, predicate)

    def follow(self, name, distance=2, timeout=60.0):
        """Walk toward a named player until within `distance` nodes."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            target = self.player(name)
            if target is None:
                self.wait(0.5)
                continue
            tp = target.node_pos
            here = self.node_pos
            if max(abs(tp[0]-here[0]), abs(tp[2]-here[2])) <= distance:
                self.wait(0.3)
                continue
            if not self.goto(tp, tolerance=distance, timeout=10, replan=False):
                self.wait(0.3)
        return True

    def block_at(self, pos):
        return self.world.get_name(pos)

    def meta_at(self, pos):
        """Server-provided node metadata at a position."""
        return self.world.get_meta(pos)

    def find_blocks(self, names, radius=16, origin=None):
        """Nearest matching nodes within `radius` of the bot."""
        if isinstance(names, str):
            names = [names]
        return self.world.find_nodes(names, origin or self.position, radius)

    def scan(self, names, y_range=None, limit=None):
        """Search the whole streamed map for node types, nearest first."""
        return self.world.find_all(names, origin=self.position,
                                   y_range=y_range, limit=limit)

    def reachable(self, max_expanded=40000):
        """Every standable node the bot can actually walk to from here."""
        return self.pathfinder.explore(self.node_pos, max_expanded)

    def chat(self, message):
        self.client.send_chat(message)

    # ----------------------------------------------------------- event loop

    def step(self, dt=PHYSICS_DT):
        """Advance one tick: process packets, apply gravity, report position."""
        if not self.client.pump(0.0):
            raise BotError(self.client.conn.disconnect_reason or "disconnected")
        self._apply_gravity(dt)
        self.client.send_playerpos()
        try:
            self._auto_defend_tick()
        except Exception as e:          # passive defence must never abort a task
            print(f"[bot] auto-defend tick failed: {e!r}")

    def wait(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.step()
            time.sleep(PHYSICS_DT)

    def wait_for(self, predicate, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.step()
            if predicate():
                return True
            time.sleep(PHYSICS_DT)
        return False

    def run_forever(self):
        while True:
            self.step()
            time.sleep(PHYSICS_DT)

    def _apply_gravity(self, dt):
        """Fall if unsupported, so the bot's claimed position stays plausible.

        The landing height must be derived from the ground being fallen towards,
        not from the node the bot currently occupies -- clamping to the latter
        pins the bot in mid-air, because that height is where it already is.
        """
        p = self.node_pos
        here = self.world.get_def(p)
        if here is not None and (here.climbable or here.is_liquid):
            return

        below = self.world.get_def((p[0], p[1] - 1, p[2]))
        if below is None:
            return                            # ground unknown: do not guess
        if below.walkable:
            surface = p[1] - 0.5              # resting on it
            if self.client.position[1] < surface:
                self.client.position[1] = surface
            return

        # nothing underfoot: fall, stopping on the first solid surface reached
        new_y = self.client.position[1] - FALL_SPEED * dt
        node_y = math.floor(new_y + 0.5)
        ground = self.world.get_def((p[0], node_y - 1, p[2]))
        if ground is not None and ground.walkable:
            new_y = max(new_y, node_y - 0.5)
        self.client.position[1] = new_y

    # ------------------------------------------------------------- movement

    def look_at(self, target):
        """Point the bot's head at a world position (affects nothing but looks right)."""
        x, y, z = self.client.position
        dx, dy, dz = target[0] - x, target[1] - (y + 1.5), target[2] - z
        self.client.yaw = (math.degrees(math.atan2(dz, dx)) - 90.0) % 360.0
        self.client.pitch = -math.degrees(math.atan2(dy, math.hypot(dx, dz)))

    def path_to(self, goal, tolerance=0):
        if self.pathfinder is None:
            raise BotError("not connected")
        return self.pathfinder.find_path(self.node_pos, goal, tolerance=tolerance)

    def goto(self, goal, tolerance=0, timeout=120.0, replan=True):
        """Walk to a node position. Returns True if the goal was reached."""
        goal = tuple(int(v) for v in goal)
        deadline = time.monotonic() + timeout
        attempts = 0
        stalled = 0
        last_progress = math.inf
        while time.monotonic() < deadline:
            result = self.path_to(goal, tolerance=tolerance)
            if not result.path:
                return False
            self.walk_path(result.path, deadline=deadline)
            here = self.node_pos
            if self.pathfinder.heuristic(here, goal) <= max(tolerance, 0):
                return True
            attempts += 1
            progress = self.pathfinder.heuristic(here, goal)
            if progress >= last_progress - 0.5:      # not getting closer
                stalled += 1
            else:
                stalled = 0
            last_progress = min(last_progress, progress)
            if not replan or attempts > 8 or stalled >= 2:
                return False
            # let more of the map stream in before trying again
            self.wait(0.4)
        return False

    def walk_path(self, path, deadline=None):
        """Walk along a list of Steps, one waypoint at a time."""
        for step in path[1:]:
            if deadline and time.monotonic() > deadline:
                return False
            if not self._move_to_node(step, deadline):
                return False
        return True

    def _move_to_node(self, step, deadline=None):
        target = step.pos
        # feet sit half a node below the centre of the node they occupy
        tx, ty, tz = target[0], target[1] - 0.5, target[2]
        speed = {"climb_up": DEFAULT_CLIMB_SPEED,
                 "climb_down": DEFAULT_CLIMB_SPEED}.get(step.action, self.walk_speed)

        self.look_at((tx, ty, tz))
        last = time.monotonic()
        stuck_for = 0.0
        while True:
            if deadline and time.monotonic() > deadline:
                return False
            now = time.monotonic()
            dt = min(now - last, 0.2)
            last = now

            x, y, z = self.client.position
            dx, dy, dz = tx - x, ty - y, tz - z
            dist = math.sqrt(dx*dx + dy*dy + dz*dz)
            if dist <= ARRIVE_EPS:
                self.client.position = [tx, ty, tz]
                self.client.velocity = [0.0, 0.0, 0.0]
                self.client.send_playerpos(force=True)
                self.emit("move", target)
                return True

            move = min(speed * dt, dist)
            scale = move / dist
            self.client.position = [x + dx*scale, y + dy*scale, z + dz*scale]
            self.client.velocity = [dx/dist*speed, dy/dist*speed, dz/dist*speed]

            if not self.client.pump(0.0):
                raise BotError("disconnected while moving")
            self.client.send_playerpos()

            stuck_for += dt
            if stuck_for > 8.0:            # never spin forever on one waypoint
                return False
            time.sleep(PHYSICS_DT)

    # -------------------------------------------------------------- actions

    def distance_to(self, pos):
        """Distance from the bot's eyes to the centre of a node."""
        x, y, z = self.client.position
        return math.dist((x, y + 1.5, z), pos)

    def in_reach(self, pos):
        return self.distance_to(pos) <= self.reach

    def eye_position(self):
        """The point rays start from: feet position plus eye height.

        Convention shared with `distance_to` and `look_at` (feet + 1.5).
        """
        x, y, z = self.position
        return (x, y + 1.5, z)

    def has_line_of_sight(self, pos, frm=None):
        """True when nothing opaque hides `pos` from the bot's eyes.

        The line runs from the bot's eye position (feet + 1.5, the same
        origin `distance_to` uses) to the centre of `pos`. The
        destination node itself never blocks, and nodes that are not
        loaded count as opaque. `frm` overrides the origin.
        """
        if frm is None:
            frm = self.eye_position()
        target = tuple(int(v) for v in pos)
        end = (target[0] + 0.5, target[1] + 0.5, target[2] + 0.5)
        for p in _walk_line(frm, end):
            if p == target:
                continue
            d = self.world.get_def(p)
            if d is None or d.blocks_sight:
                return False
        return True

    def _view_vector(self, yaw, pitch):
        """Unit direction for a yaw/pitch pair in degrees.

        Inverse of the angles `look_at` computes: yaw sweeps the
        horizontal plane (0 = +z, 90 = -x), pitch is positive looking
        down.
        """
        y, p = math.radians(yaw), math.radians(pitch)
        return (-math.sin(y) * math.cos(p), -math.sin(p),
                math.cos(y) * math.cos(p))

    def raycast(self, direction, max_distance=32.0, frm=None):
        """The first opaque node along `direction`, as seen from the eye.

        `direction` is a (dx, dy, dz) vector; only its direction
        matters. Returns a dict with `position` (node coords), `name`,
        `distance` and `point` (the exact hit point in the world), or
        None when nothing is hit within `max_distance`. A ray that
        runs off the loaded map stops at the first unloaded node and
        reports it with name None and `"unloaded": True`.
        """
        if frm is None:
            frm = self.eye_position()
        length = math.sqrt(sum(v * v for v in direction))
        if length == 0:
            raise BotError("raycast needs a non-zero direction")
        d = tuple(v / length for v in direction)
        span = float(max_distance)
        prev = tuple(math.floor(v) for v in frm)
        for p in _walk_line(frm, tuple(frm[i] + d[i] * span for i in range(3))):
            axis = next(i for i in range(3) if p[i] != prev[i])
            # t (0..1 along the ray) at which it crossed into this voxel,
            # measured on the face it entered through
            face = p[axis] if d[axis] > 0 else p[axis] + 1
            t = (face - frm[axis]) / (d[axis] * span)
            prev = p
            hit = {"position": p, "distance": t * span,
                   "point": tuple(frm[i] + d[i] * t * span for i in range(3))}
            node_def = self.world.get_def(p)
            if node_def is None:
                hit.update(name=None, unloaded=True)
                return hit
            if node_def.blocks_sight:
                hit["name"] = node_def.name
                return hit
        return None

    def look_grid(self, size=10, step_deg=5.0, max_distance=32.0,
                  yaw=None, pitch=None):
        """A grid of raycasts fanning out around the view direction.

        Casts size x size rays spaced step_deg apart, centred on where
        the bot is looking (`yaw`/`pitch` in degrees override its
        current head angles). Rows run top to bottom, columns sweep
        left to right; each cell is the node name a ray hit, None when
        it saw nothing within `max_distance`, or "?" when it ran off
        the loaded map.
        """
        size = max(1, min(int(size), 20))
        if yaw is None:
            yaw = self.client.yaw
        if pitch is None:
            pitch = self.client.pitch
        half = (size - 1) / 2.0
        rows, hits = [], []
        for r in range(size):
            p_ang = pitch + (r - half) * step_deg
            row = []
            for c in range(size):
                y_ang = yaw + (c - half) * step_deg
                hit = self.raycast(self._view_vector(y_ang, p_ang),
                                   max_distance=max_distance)
                if hit is None:
                    row.append(None)
                elif hit.get("unloaded"):
                    row.append("?")
                else:
                    row.append(hit["name"])
                    hits.append({"row": r, "col": c, "name": hit["name"],
                                 "position": hit["position"],
                                 "distance": round(hit["distance"], 1)})
            rows.append(row)
        return {"size": size, "step_deg": step_deg,
                "max_distance": float(max_distance),
                "yaw": yaw, "pitch": pitch, "rows": rows, "hits": hits}

    def approach(self, pos, timeout=120.0):
        """Walk until `pos` is close enough to interact with."""
        if self.in_reach(pos):
            return True
        pos = tuple(int(v) for v in pos)
        # try to stand on a neighbouring node, nearest first
        spots = []
        for dx in range(-2, 3):
            for dy in range(-1, 3):
                for dz in range(-2, 3):
                    cand = (pos[0] + dx, pos[1] + dy, pos[2] + dz)
                    if self.pathfinder.standable(cand):
                        spots.append((abs(dx) + abs(dy) + abs(dz), cand))
        spots.sort()
        for _d, spot in spots[:12]:
            if self.goto(spot, tolerance=1, timeout=timeout, replan=False):
                if self.in_reach(pos):
                    return True
        return self.in_reach(pos)

    # --------------------------------------------------------- tools and digging

    def hand_capabilities(self):
        """Capabilities of the bare hand.

        Games like MineClone2 give the hand its dig capabilities through the
        player's `hand` inventory list rather than the global empty-name item,
        so prefer that when present.
        """
        idefs = self.client.itemdefs
        if idefs is None:
            return None
        hand_list = self._inv_list("hand")
        if hand_list and not hand_list[0].empty:
            caps = idefs.capabilities(hand_list[0].name)
            if caps is not None:
                return caps
        return idefs.capabilities("")

    def dig_time_for(self, pos, item_name=None):
        """Seconds to dig the node at `pos` with an item, or None if it cannot."""
        from .itemdef import dig_time
        node = self.world.get_def(pos)
        idefs = self.client.itemdefs
        if node is None or idefs is None:
            return None
        caps = (self.hand_capabilities() if item_name in (None, "")
                else idefs.capabilities(item_name))
        return dig_time(node.groups, caps)

    def best_tool_for(self, pos):
        """Pick the fastest usable tool for a node.

        Returns (slot, seconds) where slot is None for the bare hand, or
        (None, None) if nothing in the inventory can break it.
        """
        best_slot, best_time = None, self.dig_time_for(pos, None)
        for slot, stack in (self.inventory.non_empty() if self.inventory else []):
            t = self.dig_time_for(pos, stack.name)
            if t is None:
                continue
            if best_time is None or t < best_time:
                best_slot, best_time = slot, t
        return best_slot, best_time

    def can_dig(self, pos):
        return self.best_tool_for(pos)[1] is not None

    def dig(self, pos, timeout=12.0, approach=True):
        """Break the node at `pos`. Returns True once it is gone.

        Walks into range first unless `approach=False`.
        """
        pos = tuple(int(v) for v in pos)
        d = self.world.get_def(pos)
        if d is None:
            raise BotError(f"node at {pos} is not loaded")
        if not d.diggable:
            return False
        if not self.in_reach(pos):
            if not approach:
                raise BotError(f"{pos} is {self.distance_to(pos):.1f} nodes away, "
                               f"beyond reach of {self.reach}")
            if not self.approach(pos, timeout=timeout):
                return False
        above = (pos[0], pos[1] + 1, pos[2])

        # Choose the fastest tool we are carrying, and work out how long the
        # server expects the dig to take -- a completion sent too early is
        # rejected, so this replaces blind retrying.
        slot, expected = self.best_tool_for(pos)
        if expected is None:
            return False                      # nothing we hold can break it
        self.equip(slot)

        self.look_at((pos[0], pos[1], pos[2]))
        self.client.interact(INTERACT_START_DIGGING, pos, above)

        end = time.monotonic() + max(timeout, expected * 2 + 2.0)
        ready_at = time.monotonic() + expected
        next_try = ready_at
        while time.monotonic() < end:
            now = time.monotonic()
            if now >= next_try:
                self.client.interact(INTERACT_DIGGING_COMPLETED, pos, above)
                next_try = now + max(0.25, expected * 0.5)
            self.step()
            cur = self.world.get_def(pos)
            if cur is None or not cur.walkable or cur.name == "air":
                self.emit("dig", pos)
                return True
            time.sleep(PHYSICS_DT)
        self.client.interact(INTERACT_STOP_DIGGING, pos, above)
        return False

    def place(self, against, face=(0, 1, 0), slot=None):
        """Place the held item against a node. `face` is the side to build on."""
        against = tuple(int(v) for v in against)
        above = (against[0] + face[0], against[1] + face[1], against[2] + face[2])
        if slot is not None:
            self.client.select_slot(slot)
        self.look_at(against)
        self.client.interact(INTERACT_PLACE, against, above)
        self.step()
        return above

    def _held_count(self, item, slot):
        """How many of the item we are about to place are still in hand."""
        if self.inventory is None:
            return 0
        if item:
            return self.inventory.count(item)
        main = self.inventory.main
        if slot is not None and slot < len(main) and not main[slot].empty:
            return main[slot].count
        return 0

    def _predict_node(self, pos, item, slot):
        """Optimistically record a node we just placed, as the real client does."""
        name = item
        if name is None and slot is not None:
            main = self.inventory.main if self.inventory else []
            if slot < len(main) and not main[slot].empty:
                name = main[slot].name
        if not name or self.world.nodedefs is None:
            return
        cid = self.world.nodedefs.id_of(name)
        if cid is not None:
            self.world.set_node(pos, cid)

    def find_placement_spot(self, radius=4, wait_for_map=8.0, relocate=True):
        """Find a nearby empty node with solid ground under it, within reach.

        Returns the nearest such position, or None. Nodes in blocks the server
        has not sent yet read as unknown and are skipped, so this waits briefly
        for the map to arrive; if the bot has boxed itself in it will, with
        `relocate`, step somewhere more open and look again.
        """
        spot = self._scan_for_spot(radius, wait_for_map)
        if spot is not None or not relocate:
            return spot

        # Boxed in -- walk a short way to open ground and try once more.
        reachable = self.pathfinder.explore(self.node_pos, max_expanded=4000)
        here = self.node_pos
        candidates = sorted(
            (p for p in reachable if 3 <= self.pathfinder.heuristic(p, here) <= 12),
            key=lambda p: self.pathfinder.heuristic(p, here))
        for target in candidates[:6]:
            if self.goto(target, tolerance=1, timeout=30, replan=False):
                spot = self._scan_for_spot(radius, wait_for_map=2.0)
                if spot is not None:
                    return spot
        return None

    def _scan_for_spot(self, radius, wait_for_map):
        deadline = time.monotonic() + wait_for_map
        while True:
            px, py, pz = self.node_pos
            best = None
            for dx in range(-radius, radius + 1):
                for dy in range(-2, 3):
                    for dz in range(-radius, radius + 1):
                        if not (dx or dy or dz):
                            continue
                        c = (px + dx, py + dy, pz + dz)
                        if not self.world.is_free(c):
                            continue
                        if not self.world.is_walkable((c[0], c[1] - 1, c[2])):
                            continue
                        if not self.in_reach(c):
                            continue
                        d = abs(dx) + abs(dy) + abs(dz)
                        if best is None or d < best[0]:
                            best = (d, c)
            if best:
                return best[1]
            if time.monotonic() > deadline:
                return None
            self.wait(1.0)

    def place_block(self, pos, slot=None, item=None):
        """Place a block *at* `pos` by clicking a suitable neighbouring node.

        Luanti builds against an existing node: it fills the node you point at
        if that node is replaceable (air, grass, water), otherwise the one on
        the pointed face. This picks a neighbour and face that land the block
        where you asked for it.
        """
        if pos is None:
            raise BotError("place_block() needs a position; "
                           "find_placement_spot() found nowhere suitable")
        pos = tuple(int(v) for v in pos)
        target_def = self.world.get_def(pos)
        if target_def is not None and target_def.walkable:
            raise BotError(f"{pos} is already occupied by {target_def.name}")

        if item is not None and self.inventory is not None:
            slot = self.inventory.first_slot(item)
            if slot is None:
                raise BotError(f"no {item} in inventory")
        if not self.in_reach(pos) and not self.approach(pos):
            raise BotError(f"{pos} is out of reach ({self.distance_to(pos):.1f} nodes)")
        if slot is not None:
            self.select_slot(slot)
            self.step()

        for face in ((0, -1, 0), (0, 1, 0), (1, 0, 0), (-1, 0, 0),
                     (0, 0, 1), (0, 0, -1)):
            against = (pos[0] - face[0], pos[1] - face[1], pos[2] - face[2])
            d = self.world.get_def(against)
            if d is not None and d.walkable and d.pointable:
                before = self._held_count(item, slot)
                self.look_at(pos)
                self.client.interact(INTERACT_PLACE, against, pos)
                if self.wait_for(
                        lambda: self.world.is_walkable(pos)
                        or self._held_count(item, slot) < before, timeout=3.0):
                    # The real client predicts placement locally, so the server
                    # does not always echo an ADDNODE back to whoever placed the
                    # node. Mirror that prediction so our map stays correct.
                    if not self.world.is_walkable(pos):
                        self._predict_node(pos, item, slot)
                    return True
        return self.world.is_walkable(pos)

    # -------------------------------------------------------------- combat

    def hostiles(self, radius=16.0):
        """Nearby hostile mobs, nearest first."""
        return self.client.entities.hostiles(self.position, radius)

    def _auto_defend_tick(self):
        """Passive defence: punch the nearest reachable, visible hostile.

        Runs from every `step()` while `auto_defend_radius` is armed, so it
        fires between tool calls and inside long-running actions alike. It
        never moves the bot: a hostile out of reach is ignored until it
        comes to us. Kills are counted by watching punched mobs vanish.
        """
        radius = self.auto_defend_radius
        if radius is None or self.dead:
            return
        for oid in tuple(self._auto_punched):
            if oid not in self.client.entities.entities:
                self._auto_punched.discard(oid)
                self.auto_defend_kills += 1
        if time.monotonic() < self._auto_next_punch:
            return
        for mob in self.hostiles(radius):
            if math.dist(mob.position, self.position) > self.reach - 0.5:
                continue                        # out of reach: we do not chase
            if not self.has_line_of_sight(
                    (mob.position[0], mob.position[1] + 1.0, mob.position[2])):
                continue                        # cannot punch through walls
            self.attack(mob.id)
            self._auto_punched.add(mob.id)
            self._auto_next_punch = time.monotonic() + self.punch_interval()
            return                              # one punch per tick

    def best_weapon(self):
        """Inventory slot dealing the most damage, or None for bare hands."""
        idefs = self.client.itemdefs
        if idefs is None or self.inventory is None:
            return None, 0
        hand_caps = self.hand_capabilities()
        best_slot = None
        best_dmg = hand_caps.damage_against() if hand_caps else 0
        for slot, stack in self.inventory.non_empty():
            caps = idefs.capabilities(stack.name)
            if caps is None:
                continue
            dmg = caps.damage_against()
            if dmg > best_dmg:
                best_slot, best_dmg = slot, dmg
        return best_slot, best_dmg

    def punch_interval(self):
        """Minimum seconds between full-strength punches with what we hold."""
        idefs = self.client.itemdefs
        if idefs is None:
            return 1.0
        held = None
        main = self.inventory.main if self.inventory else []
        idx = self.client.wield_index
        if idx < len(main) and not main[idx].empty:
            held = main[idx].name
        caps = idefs.capabilities(held) if held else self.hand_capabilities()
        return caps.full_punch_interval if caps else 1.0

    def attack(self, target):
        """Punch an entity once. `target` is an Entity or an object id."""
        oid = target.id if hasattr(target, "id") else int(target)
        ent = self.client.entities.entities.get(oid)
        if ent is not None:
            self.look_at((ent.position[0], ent.position[1] + 1.0, ent.position[2]))
        # Punching an object is START_DIGGING pointed at it, not a separate verb.
        self.client.interact(INTERACT_START_DIGGING, object_id=oid)
        self.emit("attack", ent or oid)

    def fight(self, target, timeout=30.0, equip=True, max_distance=32.0):
        """Attack an entity until it dies or disappears.

        Returns True if the target is gone by the end.
        """
        oid = target.id if hasattr(target, "id") else int(target)
        if equip:
            slot, _dmg = self.best_weapon()
            self.equip(slot)

        end = time.monotonic() + timeout
        next_punch = 0.0
        while time.monotonic() < end:
            ent = self.client.entities.entities.get(oid)
            if ent is None:
                return True                       # dead or despawned
            dist = math.dist(ent.position, self.position)
            if dist > max_distance:
                return False
            if dist > self.reach - 0.5:
                # step toward it rather than flailing at empty air
                self.goto(ent.node_pos, tolerance=1, timeout=8, replan=False)
            elif time.monotonic() >= next_punch:
                if self.has_line_of_sight(
                        (ent.position[0], ent.position[1] + 1.0,
                         ent.position[2])):
                    self.attack(oid)
                    next_punch = time.monotonic() + self.punch_interval()
                else:
                    # in reach but hidden: circle around for a shot
                    self.goto(ent.node_pos, tolerance=1, timeout=8,
                              replan=False)
            self.step()
            time.sleep(PHYSICS_DT)
        return self.client.entities.entities.get(oid) is None

    def defend(self, radius=5.0, timeout=30.0):
        """Fight off any hostile that has come close. Returns how many died."""
        killed = 0
        for mob in self.hostiles(radius):
            if self.hp <= 0:
                break
            if self.fight(mob, timeout=timeout):
                killed += 1
        return killed

    # --------------------------------------------------------------- mining

    #: horizontal headings a staircase may run along
    HEADINGS = ((1, 0), (0, 1), (-1, 0), (0, -1))

    def _opening_is_safe(self, node):
        """Refuse to breach into a liquid or anything that damages us.

        Lava is obviously fatal, but water is nearly as bad for a mine: it
        floods the shaft behind you and makes movement unreliable, so any
        liquid next to the face being cut is treated as a reason to dig
        elsewhere.
        """
        def hazardous(d):
            return d is not None and (d.damage_per_second > 0 or d.is_liquid)

        if hazardous(self.world.get_def(node)):
            return False
        for dx, dy, dz in ((1,0,0), (-1,0,0), (0,1,0), (0,-1,0), (0,0,1), (0,0,-1)):
            if hazardous(self.world.get_def((node[0]+dx, node[1]+dy, node[2]+dz))):
                return False
        return True

    def settle(self, timeout=20.0):
        """Wait until the map is known underfoot and the bot has landed.

        Returns True once there is solid ground below, False on timeout.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            p = self.node_pos
            below = self.world.get_def((p[0], p[1] - 1, p[2]))
            here = self.world.get_def(p)
            if below is not None and below.walkable:
                return True
            if here is not None and (here.climbable or here.is_liquid):
                return True
            self.step()                       # applies gravity and pumps packets
            time.sleep(PHYSICS_DT)
        return False

    def _dig_down_one(self, p):
        """Drop one level by digging the block underfoot.

        Only safe when the block below *that* is solid, so the bot lands
        immediately instead of falling into a cave or lava.
        """
        below = (p[0], p[1] - 1, p[2])
        landing = (p[0], p[1] - 2, p[2])
        ld = self.world.get_def(landing)
        if ld is None or not ld.walkable:
            return False
        if not (self._opening_is_safe(below) and self._opening_is_safe(landing)):
            return False
        if not self.world.is_walkable(below) or not self.can_dig(below):
            return False
        try:
            if not self.dig(below, approach=False):
                return False
        except BotError:
            return False
        # we now fall one node into the hole
        return self.wait_for(lambda: self.node_pos[1] <= p[1] - 1, timeout=6.0)

    def mine_to_depth(self, target_y, heading=None, timeout=900.0,
                      max_steps=400, defend=True, min_hp=8):
        """Dig a descending staircase until the bot reaches `target_y`.

        Cuts a two-node opening per step (feet and head) and walks into it, so
        the bot is never standing on the block it is breaking. Refuses to open
        a face adjacent to lava, turns a corner when blocked, and fights off
        anything that interrupts.

        Returns True if the depth was reached.
        """
        deadline = time.monotonic() + timeout
        headings = list(self.HEADINGS) if heading is None else [tuple(heading)]
        hi = 0
        steps = 0

        while time.monotonic() < deadline and steps < max_steps:
            if self.node_pos[1] <= target_y:
                return True
            if self.hp <= min_hp and defend:
                self.defend(radius=6, timeout=20)
                if self.hp <= min_hp:
                    self.emit("mining_aborted", "hp too low")
                    return False

            # Mining refuses to cut blind, so make sure the map has arrived and
            # that we are actually standing on something -- after a teleport the
            # bot is typically still in mid-air above unstreamed ground.
            self.settle(timeout=20.0)

            if self.dead:
                # Respawning teleports us; anything computed from the old
                # position is meaningless, so start the step over.
                self.wait_for(lambda: not self.dead, timeout=30)
                self.wait(3)
                self.emit("mining_respawned", self.node_pos)
                continue

            if defend:
                near = self.hostiles(7)
                if near:
                    self.emit("mining_threat", [e.name for e in near], self.hp)
                    self.defend(radius=7, timeout=25)

            p = self.node_pos
            advanced = False
            for attempt in range(len(headings)):
                dx, dz = headings[(hi + attempt) % len(headings)]
                feet = (p[0] + dx, p[1] - 1, p[2] + dz)
                head = (p[0] + dx, p[1], p[2] + dz)
                if not (self._opening_is_safe(feet) and self._opening_is_safe(head)):
                    continue
                floor = (feet[0], feet[1] - 1, feet[2])
                fd = self.world.get_def(floor)
                if fd is None or not fd.walkable:
                    continue                     # nothing to land on; try elsewhere

                blocked = False
                for node in (head, feet):
                    if self.world.is_walkable(node):
                        try:
                            if not self.can_dig(node) or not self.dig(node, approach=False):
                                blocked = True
                                break
                        except BotError:
                            blocked = True      # out of reach: we moved or died
                            break
                if blocked:
                    continue
                if self.goto(feet, tolerance=0, timeout=20, replan=False):
                    hi = (hi + attempt) % len(headings)
                    steps += 1
                    advanced = True
                    self.emit("mining_step", self.node_pos, self.hp,
                              [e.name for e in self.hostiles(8)])
                    break

            if not advanced and self._dig_down_one(p):
                steps += 1
                advanced = True
                self.emit("mining_step", self.node_pos, self.hp,
                          [e.name for e in self.hostiles(8)])

            if not advanced:
                # Nothing to cut into: this is usually the lip of a cave, where
                # walking down an existing passage beats mining. Fall back to
                # the deepest place we can actually reach.
                reach = self.pathfinder.explore(self.node_pos, max_expanded=8000)
                deepest = min(reach, key=lambda q: q[1]) if reach else None
                if deepest is not None and deepest[1] < p[1]:
                    if self.goto(deepest, tolerance=1, timeout=60):
                        steps += 1
                        advanced = True
                        self.emit("mining_descended", self.node_pos)
                if not advanced:
                    hi = (hi + 1) % len(headings)
                    if not self.goto((p[0] + headings[hi][0] * 2, p[1],
                                      p[2] + headings[hi][1] * 2),
                                     tolerance=1, timeout=15, replan=False):
                        self.emit("mining_aborted", "boxed in")
                        return False
            if defend and self.hostiles(6):
                self.defend(radius=6, timeout=20)

        return self.node_pos[1] <= target_y

    # ------------------------------------------------------------- crafting

    def _inv_list(self, name):
        return (self.inventory.lists.get(name, []) if self.inventory else [])

    def _grid_item(self, idx):
        grid = self._inv_list("craft")
        if idx >= len(grid) or grid[idx].empty:
            return None
        return grid[idx].name

    def _grid_matches(self, placements):
        grid = self._inv_list("craft")
        for i in range(len(grid)):
            want = placements.get(i)
            got = None if grid[i].empty else grid[i].name
            if want != got:
                return False
        return True

    def clear_craft_grid(self, timeout=4.0):
        """Return anything left in the craft grid to the main inventory."""
        moved = False
        for i, stack in enumerate(list(self._inv_list("craft"))):
            if not stack.empty:
                self.client.inv_move_somewhere(stack.count, "craft", i, "main")
                moved = True
        if moved:
            self.wait_for(lambda: all(s.empty for s in self._inv_list("craft")),
                          timeout=timeout)
        return all(s.empty for s in self._inv_list("craft"))

    def open_node(self, pos, timeout=4.0):
        """Right-click a node to open it (chest, crafting table, furnace).

        For a crafting table this is what makes the server resize the player's
        `craft` list from 2x2 to 3x3.
        """
        pos = tuple(int(v) for v in pos)
        if not self.in_reach(pos) and not self.approach(pos):
            raise BotError(f"{pos} is out of reach")
        before = len(self._inv_list("craft"))
        for _attempt in range(3):
            # clear first: a stale formspec from earlier (the death screen, say)
            # would otherwise satisfy the wait immediately
            self.client.current_formspec = None
            self.look_at(pos)
            self.client.interact(INTERACT_PLACE, pos, (pos[0], pos[1] + 1, pos[2]))
            if self.wait_for(lambda: len(self._inv_list("craft")) != before
                             or self.client.current_formspec is not None,
                             timeout=timeout):
                # the resize and the formspec can arrive in either order
                self.wait_for(lambda: len(self._inv_list("craft")) != before,
                              timeout=2.0)
                if len(self._inv_list("craft")) != before:
                    break
        return len(self._inv_list("craft"))

    def craft(self, recipe, times=1, timeout=8.0):
        """Craft from a shaped grid layout.

        `recipe` is either rows of item names -- None for an empty cell --

            bot.craft([["mcl_core:wood", "mcl_core:wood", "mcl_core:wood"],
                       [None, "mcl_core:stick", None],
                       [None, "mcl_core:stick", None]])

        or a flat list matching the grid. The 2x2 grid is the player's own; call
        `open_node()` on a crafting table first for 3x3 recipes.

        Returns the crafted ItemStack, or None if the recipe did not resolve.
        """
        if self.inventory is None:
            raise BotError("inventory not received yet")
        grid = self._inv_list("craft")
        if not grid:
            raise BotError("no craft grid in this inventory")
        width = self.inventory.width("craft")

        # normalise the recipe to grid slot indices
        placements = {}
        if recipe and isinstance(recipe[0], (list, tuple)):
            for r, row in enumerate(recipe):
                for c, item in enumerate(row):
                    if item:
                        idx = r * width + c
                        if idx >= len(grid):
                            raise BotError(
                                f"recipe needs a {len(recipe)}x{max(len(r) for r in recipe)} "
                                f"grid but this one is {width}x{len(grid)//width}")
                        placements[idx] = item
        else:
            for idx, item in enumerate(recipe):
                if item:
                    placements[idx] = item

        # Check the ingredients up front. Without this a short inventory
        # silently fills fewer cells and the server happily matches whatever
        # *other* recipe those cells form -- e.g. a hoe instead of a pickaxe.
        needed = collections.Counter(placements.values())
        for item, n in needed.items():
            have = self.inventory.count(item)
            if have < n * times:
                raise BotError(f"need {n * times} x {item} to craft, have {have}")

        self.clear_craft_grid()

        for idx, item in placements.items():
            slot = self.inventory.first_slot(item)
            if slot is None:
                self.clear_craft_grid()
                raise BotError(f"ran out of {item} while filling the craft grid")
            self.client.inv_move(times, "main", slot, "craft", idx)
            # wait for the server to confirm before choosing the next source
            # slot, otherwise we keep reading a stale inventory
            if not self.wait_for(
                    lambda idx=idx, item=item: self._grid_item(idx) == item,
                    timeout=3.0):
                self.clear_craft_grid()
                raise BotError(f"could not place {item} into craft slot {idx}")

        if not self._grid_matches(placements):
            self.clear_craft_grid()
            raise BotError("craft grid does not match the requested recipe")

        # let the server recompute the preview
        if not self.wait_for(
                lambda: any(not s.empty for s in self._inv_list("craftpreview")),
                timeout=timeout):
            self.clear_craft_grid()
            return None
        preview = self._inv_list("craftpreview")[0]

        self.client.inv_craft(times)
        if not self.wait_for(
                lambda: any(not s.empty for s in self._inv_list("craftresult")),
                timeout=timeout):
            self.clear_craft_grid()
            return None

        result = self._inv_list("craftresult")[0]
        self.client.inv_move_somewhere(result.count, "craftresult", 0, "main")
        self.wait_for(lambda: all(s.empty for s in self._inv_list("craftresult")),
                      timeout=timeout)
        self.clear_craft_grid()
        self.emit("craft", result)
        return result

    def craft_item(self, item, times=1):
        """Craft a known item by name, using the built-in recipe book."""
        from . import recipes
        entry = recipes.get(item)
        if entry is None:
            raise BotError(f"no recipe known for {item}; "
                           f"pass the grid to craft() instead")
        rows, needs_table = entry
        grid = len(self._inv_list("craft"))
        if needs_table and grid < 9:
            raise BotError(f"{item} needs a 3x3 grid; "
                           f"call open_node() on a crafting table first")
        return self.craft(rows, times=times)

    def item_counts(self):
        """Counter of item name -> total count in the main inventory."""
        counts = collections.Counter()
        for stack in (self.inventory.main if self.inventory else []):
            if not stack.empty:
                counts[stack.name] += stack.count
        return counts

    def node_inventory(self, pos):
        """A nearby node's inventory (chest, furnace), if the server sent it.

        Node metadata carries the inventory, so chest and furnace contents are
        visible without opening them -- though the copy is only as fresh as the
        last block or metadata update received.
        """
        return self.world.get_node_inventory(tuple(int(v) for v in pos))

    def node_items(self, pos, list_name=None):
        """Items in a node's inventory as {item name: count}."""
        inv = self.node_inventory(pos)
        if inv is None:
            return {}
        lists = [list_name] if list_name else list(inv.lists)
        out = collections.Counter()
        for name in lists:
            for stack in inv.lists.get(name, ()):
                if not stack.empty:
                    out[stack.name] += stack.count
        return dict(out)

    def take_from_node(self, pos, list_name="main", count=99, timeout=3.0):
        """Pull items out of a node's inventory into ours; return what arrived.

        Node inventories are never sent to clients, so there is no way to look
        inside a chest or furnace first -- the move is attempted and the result
        is read from our own inventory.
        """
        loc = self.client.nodemeta(pos)
        before = self.item_counts()
        self.client.inv_move_somewhere(count, list_name, 0, "main",
                                       from_inv=loc, to_inv="current_player")
        self.wait_for(lambda: self.item_counts() != before, timeout=timeout)
        after = self.item_counts()
        return {k: v - before.get(k, 0) for k, v in after.items()
                if v > before.get(k, 0)}

    def put_into_node(self, pos, item, list_name, count=1, slot=0, timeout=3.0):
        """Move items from our inventory into one of a node's lists."""
        if self.inventory is None:
            raise BotError("inventory not received yet")
        src = self.inventory.first_slot(item)
        if src is None:
            raise BotError(f"no {item} in inventory")
        have = self.inventory.count(item)
        loc = self.client.nodemeta(pos)
        self.client.inv_move(count, "main", src, list_name, slot,
                             from_inv="current_player", to_inv=loc)
        return self.wait_for(lambda: self.inventory.count(item) < have,
                             timeout=timeout)

    def smelt(self, furnace_pos, item, fuel, count=1, timeout=90.0):
        """Smelt `count` of `item` in a furnace, burning `fuel`, and collect it.

        Returns a dict of what came out, e.g. {"mcl_core:iron_ingot": 3}.

        A furnace's src/fuel/dst contents are not visible to clients, so
        progress cannot be watched directly -- the bot loads the furnace and
        then polls by trying to take from `dst` until the output stops growing
        or the timeout is reached.
        """
        furnace_pos = tuple(int(v) for v in furnace_pos)
        if not self.in_reach(furnace_pos) and not self.approach(furnace_pos):
            raise BotError(f"furnace at {furnace_pos} is out of reach")

        if not self.put_into_node(furnace_pos, item, "src", count):
            raise BotError(f"could not load {item} into the furnace")
        if not self.put_into_node(furnace_pos, fuel, "fuel", max(1, count // 8 + 1)):
            raise BotError(f"could not load {fuel} into the furnace")

        collected = collections.Counter()
        deadline = time.monotonic() + timeout
        idle = 0.0
        while time.monotonic() < deadline and sum(collected.values()) < count:
            self.wait(2.0)
            # The furnace's own `dst` list is visible through node metadata, so
            # prefer looking before reaching in; fall back to just trying, since
            # our copy is only as fresh as the last update the server pushed.
            ready = self.node_items(furnace_pos, "dst")
            if ready or idle >= 4.0:
                got = self.take_from_node(furnace_pos, "dst")
                if got:
                    collected.update(got)
                    idle = 0.0
                    continue
            idle += 2.0
            if idle > 30.0 and collected:
                break                  # furnace has gone quiet; keep what we have
        self.emit("smelt", dict(collected))
        return dict(collected)

    def move_item(self, from_list, from_i, to_list, to_i, count=1):
        self.client.inv_move(count, from_list, from_i, to_list, to_i)
        self.step()

    def drop_item(self, list_name, index, count=1):
        self.client.inv_drop(count, list_name, index)
        self.step()

    def use(self, pos=None):
        """Use the held item, optionally pointed at a node."""
        if pos is None:
            self.client.interact(INTERACT_USE)
        else:
            self.client.interact(INTERACT_USE, tuple(int(v) for v in pos),
                                 (pos[0], pos[1] + 1, pos[2]))
        self.step()

    def select_slot(self, index):
        self.client.select_slot(index)

    def equip(self, slot, timeout=3.0):
        """Make an inventory slot the wielded item, moving it into the hotbar.

        Returns the hotbar slot now held, or None for the bare hand. Items
        outside the hotbar cannot be used at all -- the server drops any
        interact referring to them -- so they are swapped in first.
        """
        if slot is None:
            return None
        if slot < self.hotbar_size:
            if slot != self.client.wield_index:
                self.select_slot(slot)
                self.step()
            return slot

        main = self.inventory.main if self.inventory else []
        name = main[slot].name if slot < len(main) and not main[slot].empty else None
        target = next((i for i in range(self.hotbar_size)
                       if i < len(main) and main[i].empty), self.hotbar_size - 1)
        self.client.inv_move(main[slot].count, "main", slot, "main", target)
        self.wait_for(lambda: (target < len(self.inventory.main)
                               and not self.inventory.main[target].empty
                               and (name is None
                                    or self.inventory.main[target].name == name)),
                      timeout=timeout)
        self.select_slot(target)
        self.step()
        return target
