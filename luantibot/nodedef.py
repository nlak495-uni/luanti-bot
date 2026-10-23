"""Parsing of TOCLIENT_NODEDEF -- the server's node (block) type registry.

Container layout (NodeDefManager::serialize):
    u8  version (1)
    u16 count
    u32 length + payload, payload = count * [ u16 content_id,
                                              u16 length + ContentFeatures blob ]

Each ContentFeatures blob is individually length-prefixed, which is what makes
the inner format safe to extend -- and lets this parser read the fields it
understands and skip the rest.
"""
from .buffer import Reader
from .compress import decompress_stream

# drawtype enum, src/nodedef.h
(NDT_NORMAL, NDT_AIRLIKE, NDT_LIQUID, NDT_FLOWINGLIQUID, NDT_GLASSLIKE,
 NDT_ALLFACES, NDT_ALLFACES_OPTIONAL, NDT_TORCHLIKE, NDT_SIGNLIKE,
 NDT_PLANTLIKE, NDT_FENCELIKE, NDT_RAILLIKE, NDT_NODEBOX,
 NDT_GLASSLIKE_FRAMED, NDT_FIRELIKE, NDT_GLASSLIKE_FRAMED_OPTIONAL,
 NDT_MESH, NDT_PLANTLIKE_ROOTED) = range(18)

# Drawtypes that never obstruct movement, used as a fallback when the exact
# `walkable` flag could not be located in the blob.
NON_SOLID_DRAWTYPES = {
    NDT_AIRLIKE, NDT_LIQUID, NDT_FLOWINGLIQUID, NDT_TORCHLIKE, NDT_SIGNLIKE,
    NDT_PLANTLIKE, NDT_RAILLIKE, NDT_FIRELIKE, NDT_PLANTLIKE_ROOTED,
}
LIQUID_DRAWTYPES = {NDT_LIQUID, NDT_FLOWINGLIQUID}

# Drawtypes thin or clear enough to see through, for line-of-sight checks.
# Everything else that is walkable (stone, wood, slabs, fences, meshes...)
# counts as opaque.
TRANSPARENT_DRAWTYPES = {
    NDT_AIRLIKE, NDT_LIQUID, NDT_FLOWINGLIQUID, NDT_GLASSLIKE,
    NDT_GLASSLIKE_FRAMED, NDT_GLASSLIKE_FRAMED_OPTIONAL, NDT_TORCHLIKE,
    NDT_SIGNLIKE, NDT_PLANTLIKE, NDT_RAILLIKE, NDT_FIRELIKE,
    NDT_PLANTLIKE_ROOTED,
}

CONTENT_UNKNOWN = 125
CONTENT_AIR = 126
CONTENT_IGNORE = 127


class NodeDef:
    __slots__ = ("id", "name", "groups", "param_type", "param_type_2",
                 "drawtype", "walkable", "climbable", "buildable_to",
                 "damage_per_second", "liquid_type", "diggable", "pointable",
                 "light_source", "raw", "exact")

    def __init__(self, id, name):
        self.id = id
        self.name = name
        self.groups = {}
        self.param_type = 0
        self.param_type_2 = 0
        self.drawtype = NDT_NORMAL
        self.walkable = True
        self.climbable = False
        self.buildable_to = False
        self.damage_per_second = 0
        self.liquid_type = 0
        self.diggable = True
        self.pointable = True
        self.light_source = 0
        self.raw = b""
        self.exact = False       # True if the full blob parsed cleanly

    @property
    def is_liquid(self):
        return self.drawtype in LIQUID_DRAWTYPES or self.liquid_type != 0

    @property
    def blocks_sight(self):
        """True when this node type hides what is behind it."""
        return self.walkable and self.drawtype not in TRANSPARENT_DRAWTYPES

    def __repr__(self):
        return (f"<NodeDef {self.id} {self.name!r} walkable={self.walkable} "
                f"climbable={self.climbable} liquid={self.is_liquid}>")


class NodeDefs:
    def __init__(self):
        self.by_id = {}
        self.by_name = {}
        self.inexact = 0

    @classmethod
    def parse(cls, blob: bytes, ser_ver: int = 29):
        plain, _ = decompress_stream(blob, ser_ver)
        self = cls()
        r = Reader(plain)
        version = r.u8()
        if version != 1:
            raise ValueError(f"unsupported nodedef container version {version}")
        count = r.u16()
        body = Reader(r.long_string())
        for _ in range(count):
            cid = body.u16()
            raw = body.string()
            try:
                nd = parse_content_features_full(cid, raw)
            except Exception:
                nd = parse_content_features(cid, raw)   # drawtype-based fallback
                self.inexact += 1
            self.by_id[cid] = nd
            self.by_name[nd.name] = nd

        # air/ignore/unknown are never transmitted; synthesise them
        for cid, name, walk in ((CONTENT_AIR, "air", False),
                                (CONTENT_IGNORE, "ignore", True),
                                (CONTENT_UNKNOWN, "unknown", True)):
            if cid not in self.by_id:
                nd = NodeDef(cid, name)
                nd.walkable = walk
                nd.drawtype = NDT_AIRLIKE if not walk else NDT_NORMAL
                nd.buildable_to = not walk
                self.by_id[cid] = nd
                self.by_name[name] = nd
        return self

    def get(self, cid):
        return self.by_id.get(cid)

    def name_of(self, cid):
        nd = self.by_id.get(cid)
        return nd.name if nd else f"<unknown {cid}>"

    def id_of(self, name):
        nd = self.by_name.get(name)
        return nd.id if nd else None


def parse_content_features(cid: int, raw: bytes) -> NodeDef:
    """Read the prefix of a ContentFeatures blob that we can rely on.

    Everything up to and including `drawtype` sits at a fixed offset, so it is
    always safe to read. Later fields (walkable, climbable, ...) live behind
    variable-length tile definitions; those are recovered by
    `refine_content_features` where possible.
    """
    r = Reader(raw)
    r.u8()                                   # CONTENTFEATURES_VERSION
    nd = NodeDef(cid, r.text())
    nd.raw = raw
    ngroups = r.u16()
    for _ in range(ngroups):
        gname = r.text()
        nd.groups[gname] = r.s16()
    nd.param_type = r.u8()
    nd.param_type_2 = r.u8()
    nd.drawtype = r.u8()

    # sensible defaults from the drawtype, refined below if we can
    nd.walkable = nd.drawtype not in NON_SOLID_DRAWTYPES
    nd.buildable_to = nd.drawtype in (NDT_AIRLIKE,) or nd.drawtype in LIQUID_DRAWTYPES
    nd.climbable = False
    return nd

# ---------------------------------------------------------------------------
# Full ContentFeatures decoding
#
# The layout below is CONTENTFEATURES_VERSION 13, recovered by decoding a live
# server's nodedef and cross-checking known nodes against the game's own Lua
# definitions (e.g. mcl_core:ladder -> walkable/climbable/is_ground_content).
# `NodeDefs.parse` verifies it by requiring every blob to be consumed exactly;
# any blob that does not is kept with drawtype-derived defaults instead.
# ---------------------------------------------------------------------------

TILE_FLAG_HAS_COLOR = 1 << 3
TILE_FLAG_HAS_SCALE = 1 << 4
TILE_FLAG_HAS_ALIGN_STYLE = 1 << 5

NODEBOX_REGULAR = 0
NODEBOX_FIXED = 1
NODEBOX_WALLMOUNTED = 2
NODEBOX_LEVELED = 3
NODEBOX_CONNECTED = 4

LIQUID_NONE, LIQUID_FLOWING, LIQUID_SOURCE = 0, 1, 2


def _skip_tiledef(r):
    r.u8()                                   # tiledef version (6)
    r.string()                               # texture name
    atype = r.u8()                           # animation type
    if atype == 1:                           # vertical frames
        r.u16(); r.u16(); r.f32()
    elif atype == 2:                         # 2D sheet
        r.u8(); r.u8(); r.f32()
    flags = r.u16()
    if flags & TILE_FLAG_HAS_COLOR:
        r.take(3)
    if flags & TILE_FLAG_HAS_SCALE:
        r.u8()
    if flags & TILE_FLAG_HAS_ALIGN_STYLE:
        r.u8()


def _skip_box_vector(r):
    for _ in range(r.u16()):
        r.take(24)                           # aabb3f: 6 floats


def _skip_nodebox(r):
    r.u8()                                   # nodebox version (6)
    t = r.u8()
    if t in (NODEBOX_FIXED, NODEBOX_LEVELED):
        _skip_box_vector(r)
    elif t == NODEBOX_WALLMOUNTED:
        r.take(72)                           # wall_top, wall_bottom, wall_side
    elif t == NODEBOX_CONNECTED:
        for _ in range(15):                  # fixed + 6 connect + 6 disconnect
            _skip_box_vector(r)              #   + disconnected + disconnected_sides
    return t


def _skip_sound(r):
    r.string()                               # sound name
    r.f32(); r.f32(); r.f32()                # gain, pitch, fade


def parse_content_features_full(cid: int, raw: bytes) -> NodeDef:
    """Decode a whole ContentFeatures blob. Raises on any mismatch."""
    r = Reader(raw)
    version = r.u8()
    if version != 13:
        raise ValueError(f"ContentFeatures version {version} not supported")
    nd = NodeDef(cid, r.text())
    nd.raw = raw
    for _ in range(r.u16()):
        gname = r.text()
        nd.groups[gname] = r.s16()
    nd.param_type = r.u8()
    nd.param_type_2 = r.u8()
    nd.drawtype = r.u8()
    r.string()                               # mesh
    r.f32()                                  # visual_scale

    for _ in range(r.u8()):                  # tiles
        _skip_tiledef(r)
    for _ in range(6):                       # overlay tiles
        _skip_tiledef(r)
    for _ in range(r.u8()):                  # special tiles
        _skip_tiledef(r)

    r.u8()                                   # alpha_for_legacy
    r.take(3)                                # color rgb
    r.string()                               # palette name
    r.u8()                                   # waving
    r.u8()                                   # connect_sides
    for _ in range(r.u16()):                 # connects_to ids
        r.u16()
    r.take(4)                                # post_effect_color argb
    r.u8()                                   # leveled
    r.u8()                                   # light_propagates
    r.u8()                                   # sunlight_propagates
    nd.light_source = r.u8()
    r.u8()                                   # is_ground_content
    nd.walkable = r.bool8()
    nd.pointable = r.bool8()
    nd.diggable = r.bool8()
    nd.climbable = r.bool8()
    nd.buildable_to = r.bool8()
    r.u8()                                   # rightclickable
    nd.damage_per_second = r.u32()
    nd.liquid_type = r.u8()
    r.string()                               # liquid_alternative_flowing
    r.string()                               # liquid_alternative_source
    r.u8()                                   # liquid_viscosity
    r.u8()                                   # liquid_renewable
    r.u8()                                   # liquid_range
    r.u8()                                   # drowning
    r.u8()                                   # floodable
    _skip_nodebox(r)                         # node_box
    _skip_nodebox(r)                         # selection_box
    _skip_nodebox(r)                         # collision_box
    _skip_sound(r); _skip_sound(r); _skip_sound(r)
    r.u8()                                   # legacy_facedir_simple
    r.u8()                                   # legacy_wallmounted
    r.string()                               # node_dig_prediction
    r.u8()                                   # leveled_max
    r.u8()                                   # alpha mode
    r.u8()                                   # move_resistance
    r.u8()                                   # liquid_move_physical
    if r.remaining():
        r.u8()                               # post_effect_color_shaded

    if r.remaining():
        raise ValueError(f"{r.remaining()} trailing bytes in {nd.name!r}")
    nd.exact = True
    return nd
