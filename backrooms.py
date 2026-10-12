"""
HOLLOW FLOOR — infinite first-person explorer
================================================
Controls:
    W / S     - move forward / back
    A / D     - strafe left / right
    Mouse     - look around
    Space     - jump
    Shift     - sprint
    E         - pick up
    Tab / F1  - developer debug panel
    Esc       - release mouse / quit

The map is infinite: maze "chunks" are generated procedurally around the
player and unloaded once far away, so you can walk forever.

Run:  python backrooms.py
"""

from panda3d.core import loadPrcFileData, ColorBlendAttrib, WindowProperties

# Panda3D runs app+cull+draw on ONE thread by default, so the game pins a single
# CPU core while the other 15 and the GPU sit idle. Moving cull and draw onto
# their own threads pipelines the frame across cores: measured 100 -> 179 fps at
# this view distance. This MUST be set before the window is created, i.e. before
# ursina is imported/instantiated.
loadPrcFileData('', 'threading-model Cull/Draw')

from ursina import *
from ursina.prefabs.first_person_controller import FirstPersonController
import random
import math
import heapq
from functools import lru_cache

# ---- Window ----------------------------------------------------------------
WINDOW_SIZE = (1280, 720)   # windowed resolution (not fullscreen)
FOV = 110                   # field of view (FirstPersonController defaults to 90)
PAUSE_BLUR = 0.012          # how much the world blurs behind the pause menu.
                            # The shader mixes by blur_size*10, so .012 is subtle
                            # and .1 would be a full smear. 0 = perfectly sharp.
# UI sizes below are plain fractions of the screen: a child of camera.ui with
# scale_y 0.2 is 20% of the window height, and the full width is aspect (~1.78)
# wide. Verified against the running game (its ui lens film size is 1.0).

# ---- World tuning ----------------------------------------------------------
CELL = 4              # size (units) of one grid cell
CHUNK = 6             # cells per chunk edge  (chunk = CHUNK*CELL units square)
WALL_H = 11.0         # floor-to-ceiling height (tall, airy rooms)
PLAYER_HEIGHT = 3.2   # eye height of the player (a full head below the ceiling)
WALL_DENSITY = 0.13   # fraction of cells that become walls (lower = more open)
PROP_DENSITY = 0.025  # chance an open cell gets a random prop. Kept moderate on
                      # purpose: props are built during chunk streaming, so a high
                      # density makes every chunk load hitch. The newer props are
                      # made common through PROP_TABLE weights instead.
# Only load enough chunks to fill what the fog actually shows. The fog goes fully
# black at (FOG_END - FOG_START) units, so geometry past that is invisible anyway
# Must reach at least FOG_END units so the black hides the map edge. Nearest edge
# ~= VIEW_RADIUS * 24 units: r2>=48, r3>=72, r4>=96, r5>=120, r6>=144. FOG_END=115
# needs r5; r6 keeps a margin. Bigger radius = many more chunks/entities, so raise it no more than the
# fog requires (chunk count grows as (2r+1)^2 -- r6 is 169 chunks vs r2's 25).
VIEW_RADIUS = 6
CHUNKS_PER_FRAME = 1  # queued chunks built per frame while streaming. The rest of
                      # a newly-entered ring is spread over frames so no single
                      # frame stalls (a stall feels like hitting an invisible wall)
PROP_CHUNK_RADIUS = 3 # chunks around the player that actually get props built.
                      # Props are detailed now (10-17 cubes each), so building
                      # them across all 169 loaded chunks costs more than the
                      # whole rest of the world. Past ~72 units the fog has them
                      # nearly black anyway. Chunks are rebuilt WITH props when
                      # you come close, so you never see them pop in nearby.
COLLISION_RADIUS = 1  # chunks around the player that keep ACTIVE colliders. Every
                      # raycast walks the whole collidables set, and the player can
                      # only ever touch what's adjacent, so distant chunks keep
                      # their colliders stashed (~1300 active -> ~70).
# A real velocity + gravity integration, so the jump traces a smooth
# parabola. Ursina's stock jump animates upward on an ease curve and then
# starts a separate fall, which gives a fast rise and a flat hang.
JUMP_VELOCITY = 11.0  # initial upward speed
JUMP_GRAVITY = 36.0   # downward acceleration; lower = floatier
PLAYER_MAX_DT = 1 / 30  # longest step the controller will take in one frame
UNSTICK_SPEED = 9.0   # how fast you're pushed back out if you clip into a wall
SPAWN_CLEAR = 1       # cells around origin kept wall-free so you don't spawn stuck
SPAWN_ROOM = 2        # you start in a walled room this many cells each way from
                      # the origin (5x5 cells, 20x20 units), one doorway per side
SPAWN_LIGHTER_CELL = (0, 2)   # one lighter is always here, in plain sight of
                              # where you start, so a first run is never unarmed
SPAWN_DESK_POS = (0.0, 7.0)   # ...on the desk in the spawn room, straight ahead
LIGHTER_SIZE = 2.76           # well over world scale (eye height 3.2 is ~1.9x
                              # real life) -- it has to be spotted on the carpet
LIGHTER_EMIT = 0.35           # a faint self-glow on the lighter itself...
LIGHTER_HALO = 0.26           # ...and a soft warm pool of light around it
BACKPACK_SIZE = 1.8           # the same: a pack you can actually see from the door
PROP_SHOWCASE = False # TEST: put one of every prop in a labelled row at spawn.
                      # Set back to False for a normal game.
SHOWCASE_CLEAR = 4    # cells kept wall-free around spawn while showcasing, so
                      # generated walls don't stand in front of the props
SEED = 1337

CHUNK_UNITS = CHUNK * CELL

# ---- Sprint / stamina (Fortnite-style) -------------------------------------
WALK_SPEED = 10.0
SPRINT_SPEED = 15.0             # 1.5x walking
STAMINA_MAX = 100.0
SPRINT_SECONDS = 15.0       # how long a full stamina bar lasts while sprinting
STAMINA_DRAIN = STAMINA_MAX / SPRINT_SECONDS   # per second while sprinting
STAMINA_REGEN = 15.0        # per second while recovering
STAMINA_REGEN_DELAY = 0.5   # seconds after sprinting before it starts refilling
SPRINT_UNLOCK = 35.0        # after emptying, must refill to this before you can sprint
BAR_W = 0.5                 # sprint bar width (UI units)

# ---- Inventory -------------------------------------------------------------
INV_SLOTS = 5           # slots you start with
INV_BONUS_SLOTS = 5     # extra slots the backpack grants when you pick it up
PICKUP_RANGE = 4.5      # how close you must be to pick something up
PICKUP_DOT = 0.55       # and roughly how much you must be facing it
INV_SLOT = 0.062        # slot size, as a fraction of screen height
INV_GAP = 0.010
INV_Y = -0.40           # height of the bar on screen
INV_BAG_GAP = 0.020     # space between the bar and the backpack row above it

# kind -> (display name, icon colour). Only these props can be carried.
ITEMS = {
    'backpack':     ('Backpack', (74, 52, 44)),
    'paint_bucket': ('Paint Can', (196, 196, 205)),
    'cone':         ('Traffic Cone', (232, 110, 28)),
    'lighter':      ('Lighter', (226, 178, 62)),
}


class Inventory:
    """Fixed-size slot inventory. Deliberately free of any ursina types so the
    rules can be tested on their own."""

    def __init__(self, size):
        self.slots = [None] * size

    @property
    def size(self):
        return len(self.slots)

    @property
    def used(self):
        return sum(1 for s in self.slots if s is not None)

    @property
    def is_full(self):
        return self.used >= self.size

    @property
    def has_bag(self):
        """Carrying a backpack? It is what adds the second row."""
        return self.size > INV_SLOTS

    def add(self, kind):
        """Put an item in the first free slot. Returns its index, or None when
        there is no room."""
        for i, slot in enumerate(self.slots):
            if slot is None:
                self.slots[i] = kind
                return i
        return None

    def remove(self, kind):
        """Take one item of this kind back out. True if there was one."""
        for i, slot in enumerate(self.slots):
            if slot == kind:
                self.slots[i] = None
                return True
        return False

    def expand(self, extra):
        self.slots.extend([None] * extra)


# ---- The entity ------------------------------------------------------------
ENTITY_ENABLED = True
# Diamond's scripted entrance
INTRO_DELAY = 30.0          # seconds of play before it appears
INTRO_SPAWN_MIN = 12.0      # how far behind you it appears
INTRO_SPAWN_MAX = 24.0
INTRO_BEHIND_DOT = -0.35    # must be at least this far off your facing
ENTITY_CHASE_SPEED = 11.0   # units/sec once it is up to speed (walk 10, sprint 15)
ENTITY_CHASE_START_SPEED = 8.0  # slower than you walk, for the first moment
ENTITY_CHASE_RAMP = 1.0     # seconds it stays at the slower speed
ENTITY_CHASE_DELAY = 0.5    # beat after the cutscene before it moves at all
ENTITY_SCALE = 1.12         # it stands taller than the model is built
ENTITY_REPATH_INTERVAL = 0.4  # seconds between A* recalculations while chasing
ENTITY_PATH_CELLS = 40      # don't even try to route past this many cells away
ENTITY_SPAWN_MIN = 45.0     # how far away it (re)appears while it is still idle
ENTITY_SPAWN_MAX = 85.0
ENTITY_SEE_RANGE = 100.0    # you have to be within this to trigger it (inside the fog)
ENTITY_SEE_DOT = 0.866      # cone half-angle 30deg (was 33.5, originally 25.8)
                            # how centred in your view it must be to count as
                            # "looked at". 1.0 = dead centre, lower = wider cone.
ENTITY_LOSE_DIST = 150.0    # if it is idle and this far off, it relocates nearer
ENTITY_RADIUS = 0.45        # half the monster's width, for wall clearance
ENTITY_CATCH_DIST = 1.6     # how close it gets before it counts as reaching you
WIN_SHAKE_PIXELS = 8.0      # window wobble while it is chasing AND in sight
WIN_SHAKE_HOLD = 1.2        # frames each wobble position is held (1.0 = a new
                            # position every frame; 1.2 = 20% slower)
CHASE_MIN, CHASE_MAX = 20.0, 30.0   # a chase lasts this long, then it glitches out
RESPAWN_MIN, RESPAWN_MAX = 9.0, 16.0  # buried this long before it digs up again
DIG_TIME = 5.9              # length of the dig-out cutscene (seconds)
DIG_DEPTH = 6.8            # how far below the floor it starts, fully buried
CUTSCENE_DIST = 10.0       # how far ahead of you the ground breaks open
BAR_H = 0.084              # letterbox bar height during the cutscene
ENTITY_STEP_RATE = 4.2      # walk-cycle speed relative to ground covered
ENTITY_LIMB_SWING = 34      # degrees the limbs swing from their joints
DEATH_TIME = 2.2            # from it reaching you to the death card
DEATH_STOP = 2.0            # how close it ends up: any nearer and its head is
                            # off the top of the screen entirely
ENTITY_HEAD_Y = 5.6         # roughly where its head sits above its feet

# ---- Footsteps -------------------------------------------------------------
STEP_STRIDE = 3.3           # floor covered per footfall at a walk
STEP_STRIDE_RUN = 2.7       # shorter, faster steps while sprinting
ENTITY_STRIDE = 4.4         # its legs are a good deal longer than yours
ENTITY_STEP_RANGE = 55.0    # past this you cannot hear it coming at all
TICK_RANGE = 26.0           # how far a failing tube's strike carries
# These are ambience, not events: they should sit under the room, and you should
# notice them without ever being addressed by them.
STEP_VOLUME = 0.30          # your own feet, barely there
ENTITY_STEP_VOLUME = 0.50   # its feet still have to work as a warning
TICK_VOLUME = 0.20

# ---- Fire: the lighter is the only thing that fights back -------------------
THROW_SPEED = 17.0          # how hard the lighter leaves your hand
THROW_GRAVITY = 20.0        # and how quickly it drops
THROW_HIT_R = 1.5           # how near it must pass to set Diamond alight
FIRE_CATCH_R = 2.0          # ...or how near it must walk to a burning patch
FIRE_FLOOR_LIFE = (5.0, 8.0)   # how long a missed throw keeps burning
BURN_TIME = 4.4             # how long it takes to burn down
BURN_RESPAWN = (38.0, 60.0) # it stays buried far longer after a burning


# ---- Backrooms palette -----------------------------------------------------
# NOTE: this Ursina build wants 0-1 float channels; passing 0-255 clamps to
# white. col255() converts 8-bit values for us.
def col255(r, g, b):
    return color.rgb(r / 255, g / 255, b / 255)


WALL_COLOR = col255(198, 182, 128)   # mono-yellow wallpaper
FOG_COLOR = col255(7, 6, 3)           # murky near-black that everything sinks into
# Fog is drawn PER-PIXEL by the game's own shaders (see the LOOK section), which
# blend every pixel toward FOG_COLOR over (FOG_END - FOG_START) units. The curve
# is exponential, not linear: the dark closes in fast and then swallows the far
# distance completely. The inputs live on the scene root, so the debug panel's
# fog controls still drive it.
# Lower FOG_END = tighter, closer black (more oppressive); higher = see further.
# Keep FOG_END under the loaded distance (VIEW_RADIUS) so the black swallows the
# map edge instead of showing a hard cut.
FOG_START = 0.0
FOG_END = 115.0                       # everything is fully black this far out

# ---- Look: materials & lighting --------------------------------------------
# Nothing is flat-shaded. The room, the props and the monster are lit per-pixel
# by the ceiling troffers, which sit on a fixed grid so a shader can find the few
# that reach any pixel without being told about every light in the world.
LIGHT_EVERY = 3                     # one troffer every 3 cells in x and z
LIGHT_SPACING = LIGHT_EVERY * CELL  # = 12 units
CEIL_TILE = 1.2                     # drop-ceiling tile size; a troffer is 2 x 1 tiles
LIGHT_Z_OFFSET = CEIL_TILE / 2      # nudges the troffers onto the tile grid
LIGHT_DEAD_CHANCE = 0.24            # burnt-out tubes leave pools of real darkness
LIGHT_FLICKER_CHANCE = 0.06         # failing tubes that stutter on and off
LIGHT_GRID_N = 32                   # light-state texture covers N x N troffers
OCC_GRID_N = 64                     # wall texture covers N x N cells (power of 2!)
BROWNOUT_EVERY = (18.0, 55.0)       # seconds between whole-building power sags
ENTITY_FLICKER_RADIUS = 22.0        # lights near the monster fail while it hunts

# ============================================================================
#  PURE GENERATION LOGIC (no Ursina objects -> unit-testable)
# ============================================================================

@lru_cache(maxsize=1 << 17)
def cell_is_wall(cx, cz):
    """Deterministic: is world grid cell (cx, cz) a wall? Same every visit.

    Cached because it builds a random.Random per call, which is slow, and the
    pathfinder hammers it thousands of times per search. The result only depends
    on the cell and constants that never change at runtime, so caching is safe.
    """
    if PROP_SHOWCASE:
        if abs(cx) <= SHOWCASE_CLEAR and abs(cz) <= SHOWCASE_CLEAR:
            return False  # keep the showcase row open
    else:
        ring = max(abs(cx), abs(cz))
        if ring <= SPAWN_ROOM:
            return False                      # inside the spawn room
        if ring == SPAWN_ROOM + 1:
            return cx != 0 and cz != 0        # its walls, a doorway mid-side
        if ring == SPAWN_ROOM + 2 and (cx == 0 or cz == 0):
            return False                      # nothing blocking a doorway outside
    h = (cx * 73856093) ^ (cz * 19349663) ^ SEED
    return random.Random(h).random() < WALL_DENSITY


def world_cell(x, z):
    """Grid cell containing world position (x, z)."""
    return (round(x / CELL), round(z / CELL))


def has_line_of_sight(ax, az, bx, bz, radius=0.0, step_len=1.2):
    """Is the straight line between two world points clear of walls?

    Uses the grid rather than raycasts on purpose: cell_is_wall() is valid
    anywhere in the infinite world, while colliders only exist in the couple of
    chunks around the player, so a raycast would sail straight through distant
    walls and report a false clear.

    radius widens the test by checking a parallel line either side, for callers
    that have a body rather than being a point. step_len is how finely the line
    is sampled -- it must be well under a cell, because a line that just clips
    the corner of a wall crosses only a sliver of it and a coarse sample walks
    straight past.
    """
    dx, dz = bx - ax, bz - az
    dist = math.hypot(dx, dz)
    if dist < 1e-6:
        return not cell_is_wall(*world_cell(ax, az))
    steps = max(2, int(dist / step_len) + 1)
    px, pz = (-dz / dist * radius, dx / dist * radius) if radius else (0.0, 0.0)
    for i in range(steps + 1):
        t = i / steps
        x, z = ax + dx * t, az + dz * t
        if cell_is_wall(*world_cell(x, z)):
            return False
        if radius and (cell_is_wall(*world_cell(x + px, z + pz))
                       or cell_is_wall(*world_cell(x - px, z - pz))):
            return False
    return True


def find_path(start, goal, max_nodes=900):
    """A* over the cell grid. Returns the list of cells to walk (excluding the
    start), or [] if already there / too far / no route within max_nodes.

    max_nodes bounds the worst-case frame cost: an unreachable or very distant
    goal would otherwise explore until it gave up, stalling the frame long
    enough to feel like walking into an invisible wall.
    """
    if start == goal or cell_is_wall(*goal):
        return []
    if abs(start[0] - goal[0]) + abs(start[1] - goal[1]) > ENTITY_PATH_CELLS:
        return []          # far beyond sight; the caller just walks straight
    open_q = [(0, start)]
    came = {start: None}
    g_score = {start: 0}
    nodes = 0
    while open_q and nodes < max_nodes:
        _, cur = heapq.heappop(open_q)
        nodes += 1
        if cur == goal:
            path = []
            while cur != start:
                path.append(cur)
                cur = came[cur]
            path.reverse()
            return path
        cx, cz = cur
        for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt = (cx + dx, cz + dz)
            if cell_is_wall(*nxt):
                continue
            ng = g_score[cur] + 1
            if ng < g_score.get(nxt, 1 << 30):
                g_score[nxt] = ng
                came[nxt] = cur
                h = abs(nxt[0] - goal[0]) + abs(nxt[1] - goal[1])
                heapq.heappush(open_q, (ng + h, nxt))
    return []


def world_to_chunk(x, z):
    """Which chunk coordinate contains world position (x, z)?"""
    return (math.floor(x / CHUNK_UNITS), math.floor(z / CHUNK_UNITS))


def chunks_in_radius(pcx, pcz, r):
    """Set of chunk coords within Chebyshev radius r of (pcx, pcz)."""
    return {(pcx + dx, pcz + dz)
            for dx in range(-r, r + 1)
            for dz in range(-r, r + 1)}


# ============================================================================
#  URSINA WORLD
# ============================================================================

chunks = {}          # (chx, chz) -> list of Entity
player = None        # set in main()

# lighting state (see the LOOK section)
_light_tex = None          # luminance texture: brightness of each troffer near you
_light_buf = None          # CPU copy of it, edited every frame
_light_base = None         # each troffer's steady brightness, before any failure
_light_origin = None       # troffer index stored at texel (0, 0)
_light_flickers = []       # [texel, on, seconds left] for failing tubes in range
_light_dips = {}           # texel -> (value, seconds left): lights the monster is killing
_occ_tex = None            # luminance texture: 255 where a cell is a wall
_occ_origin = None         # cell stored at texel (0, 0)
_look_time = 0.0           # shader clock
_power = 1.0               # whole-building brightness (brownouts)
_brownout_t = 30.0         # countdown to the next power sag
_brownout_left = 0.0       # how long the current sag lasts
_fear = 0.0                # 0..1, how distorted the screen is while hunted

# sprint state
_stamina = STAMINA_MAX
_sprint_locked = False      # True after fully depleting, until refilled to SPRINT_UNLOCK
_regen_timer = 0.0
_bar_bg = None              # HUD entities
_bar_fill = None

# pause menu state
_paused = False
_crosshair = None
_inventory = Inventory(INV_SLOTS)
_inv_ui = None
_inv_icons = []
_prompt = None
DEBUG_NOCLIP = False
DEBUG_FREEZE_ENTITY = False
DEBUG_HIDE_ENTITY = False
DEBUG_FOG_ON = True
DEBUG_GLOW = False
DEBUG_PATH = False
DEBUG_PROP_INDEX = 0.0
DEBUG_SHAKE_ON = True
_dbg_ui = None
_dbg_text = None
_dbg_sel = 0
_dbg_open = False
_path_vis = None
_path_sig = None

# entity state
_entity = None
_entity_chasing = False
_intro_state = 'waiting'   # legacy; the real state machine is _diamond_phase
_intro_t = 0.0
_diamond_phase = 'buried'  # 'buried' -> 'cutscene' -> 'chasing' -> 'buried'
_spawn_t = INTRO_DELAY     # countdown while buried, until it digs up
_chase_t = 0.0             # countdown while chasing
_chase_delay = 0.0         # beat before it starts moving at all
_chase_elapsed = 0.0       # seconds it has actually been hunting
_chase_speed = 12.0        # its speed right now (ramps up after the start)
_cutscene = None           # dict of dig-out cutscene state, or None
_cutscene_active = False   # freezes player control while the cutscene plays
_glitch = 0.0              # 0..1 screen corruption during the chase
_glitch_spike = 0.0        # random extra corruption bursts
_black = 0.0               # 0..1 momentary blackout
_black_t = 3.0             # countdown to the next blackout
_puff_tex = None           # shared dust-puff texture, built once
_entity_path = []           # remaining A* waypoints (cell coords)
_entity_goal = None         # cell the current path was built toward
_entity_repath = 0.0        # countdown to the next A* recalculation
_win_offset = (0, 0)        # the shake offset we currently have applied
_win_shaking = False
_win_shake_clock = 0.0      # frames since the wobble last moved
_sfx = {}                   # name -> panda AudioSound, synthesised at startup


def distance_xz(a, b):
    """Flat (ignoring height) distance between two things with x/z."""
    return math.hypot(a.x - b.x, a.z - b.z)

# crosshair size, as fractions of the screen (arm length / bar thickness)
CROSSHAIR_LEN = 0.022
CROSSHAIR_THICK = 0.004


# ---- Prop builders (each returns a list of Entities) -----------------------

def _blob_mesh(radius, r, points=20, wobble=0.34, squash=1.0):
    """A flat, irregular disc mesh -- an organic outline instead of a rectangle.

    Radii around the rim are randomised then smoothed with their neighbours, so
    the edge waves gently rather than coming out spiky, and the whole thing is
    fanned into triangles from the centre. Each caller needs its OWN mesh:
    panda3d re-parents a shared mesh node, so reusing one makes every entity
    but the last disappear.
    """
    radii = [radius * (1 + r.uniform(-wobble, wobble)) for _ in range(points)]
    smooth = [(radii[i - 1] + 2 * radii[i] + radii[(i + 1) % points]) / 4
              for i in range(points)]
    verts = [Vec3(0, 0, 0)]
    for i in range(points):
        a = math.tau * i / points
        verts.append(Vec3(math.cos(a) * smooth[i], 0,
                          math.sin(a) * smooth[i] * squash))
    tris = [(0, 1 + i, 1 + ((i + 1) % points)) for i in range(points)]
    return Mesh(vertices=verts, triangles=tris, mode='triangle')


def _crack_mesh(r, length=1.8, width=0.075, segments=9):
    """A jagged fissure as one flat mesh in the XY plane.

    Built as a tapering strip that wanders left/right as it descends, plus a
    couple of branches. Being flat it can sit flush against a wall face instead
    of protruding out of it the way the old thin cubes did.
    """
    verts, tris = [], []

    def strip(sx, sy, ln, wd, segs, lean):
        base = len(verts)
        x, y = sx, sy
        for i in range(segs + 1):
            t = i / segs
            w = wd * math.sin(math.pi * t) ** 0.6 + 0.004   # fat middle, fine ends
            verts.append(Vec3(x - w / 2, y, 0))
            verts.append(Vec3(x + w / 2, y, 0))
            y -= ln / segs
            x += lean * (ln / segs) + r.uniform(-0.055, 0.055)
        for i in range(segs):
            a = base + i * 2
            tris.append((a, a + 1, a + 3))
            tris.append((a, a + 3, a + 2))

    lean = r.uniform(-0.35, 0.35)
    strip(0, 0, length, width, segments, lean)
    for _ in range(r.randint(1, 3)):            # branches off the main fissure
        t = r.uniform(0.15, 0.8)
        strip(lean * length * t + r.uniform(-0.1, 0.1), -length * t,
              length * r.uniform(0.2, 0.42), width * 0.55,
              max(3, segments // 3), r.uniform(-1.1, 1.1))
    return Mesh(vertices=verts, triangles=tris, mode='triangle')


def _blob(x, y, z, radius, colour, r, wobble=0.34, double_sided=False):
    """One organic flat patch (puddle layer, stain ring, paint spill)."""
    return Entity(model=_blob_mesh(radius, r, wobble=wobble,
                                   squash=r.uniform(0.72, 1.25)),
                  color=colour, position=(x, y, z),
                  rotation_y=r.uniform(0, 360), double_sided=double_sided)


def _spill(x, z, colour, r, size, blobs=2, y=0.02):
    """A paint spill: a couple of overlapping organic patches."""
    return [_blob(x + r.uniform(-size, size) * 0.35, y + i * 0.006,
                  z + r.uniform(-size, size) * 0.35,
                  size * r.uniform(0.6, 1.0), colour, r)
            for i in range(blobs)]


def _prop_paint_bucket(x, z, r, placed=False):
    """A standing or knocked-over paint bucket with a spill next to it. One you
    `placed` yourself is set down upright and doesn't spill."""
    ents = []
    paint = r.choice([col255(225, 225, 230), col255(180, 40, 40),
                      col255(40, 60, 170), col255(30, 120, 55), col255(230, 200, 40)])
    metal = col255(200, 200, 208)
    shade = col255(150, 150, 158)
    dark = col255(120, 120, 128)
    ry = r.uniform(0, 360)

    if placed or r.random() < 0.5:   # standing
        # tapered body: stacked bands, slightly wider toward the rim
        for i in range(5):
            w = 0.40 + i * 0.028
            ents.append(Entity(model='cube', texture='white_cube',
                               collider='box' if i == 0 else None,
                               color=metal if i % 2 == 0 else shade,
                               position=(x, 0.06 + i * 0.11, z), rotation_y=ry,
                               scale=(w, 0.11, w)))
        ents.append(Entity(model='cube', color=shade, position=(x, 0.585, z),
                           rotation_y=ry, scale=(0.56, 0.05, 0.56)))    # rim lip
        ents.append(Entity(model='cube', color=dark, position=(x, 0.60, z),
                           rotation_y=ry, scale=(0.48, 0.02, 0.48)))    # inner lip
        ents.append(Entity(model='cube', color=paint, position=(x, 0.575, z),
                           rotation_y=ry, scale=(0.42, 0.03, 0.42)))    # paint surface
        # wire handle arcing over the top
        a = math.radians(ry)
        hx, hz = math.cos(a) * 0.27, math.sin(a) * 0.27
        for sgn in (-1, 1):
            ents.append(Entity(model='cube', color=dark, rotation_y=ry,
                               position=(x + hx * sgn, 0.72, z + hz * sgn),
                               scale=(0.035, 0.30, 0.035),
                               rotation=(0, ry, 22 * sgn)))
        ents.append(Entity(model='cube', color=dark, rotation_y=ry,
                           position=(x, 0.88, z), scale=(0.42, 0.035, 0.035)))
        # drips running down one side
        for _ in range(r.randint(1, 3)):
            da = math.radians(r.uniform(0, 360))
            ents.append(Entity(model='cube', color=paint,
                               position=(x + math.cos(da) * 0.26,
                                         r.uniform(0.18, 0.42),
                                         z + math.sin(da) * 0.26),
                               scale=(0.05, r.uniform(0.12, 0.30), 0.05)))
        if not placed:
            ents += _spill(x + r.uniform(-0.5, 0.5), z + r.uniform(0.5, 0.9),
                           paint, r, r.uniform(0.5, 0.9), blobs=3)
    else:                  # fallen over, big spill
        for i in range(4):
            w = 0.40 + i * 0.03
            ents.append(Entity(model='cube', texture='white_cube',
                               collider='box' if i == 0 else None,
                               color=metal if i % 2 == 0 else shade,
                               position=(x, 0.22, z + (i - 1.5) * 0.11),
                               scale=(w, w, 0.11), rotation_y=ry))
        ents.append(Entity(model='cube', color=shade, rotation_y=ry,
                           position=(x, 0.22, z + 0.28), scale=(0.56, 0.56, 0.05)))
        ang = math.radians(r.uniform(0, 360))
        ents += _spill(x + math.cos(ang) * 0.7, z + math.sin(ang) * 0.7,
                       paint, r, r.uniform(1.1, 1.8), blobs=6)
    return ents


def _prop_cone(x, z, r):
    """A traffic cone: a smooth taper of stacked bands with two reflective
    stripes, on a scuffed square base."""
    orange = col255(232, 110, 28)
    orange_d = col255(196, 88, 20)
    white = col255(238, 238, 238)
    base_c = col255(30, 30, 32)
    ry = r.uniform(0, 360)
    ents = [
        Entity(model='cube', color=base_c, position=(x, 0.03, z),
               rotation_y=ry, scale=(0.62, 0.06, 0.62)),
        Entity(model='cube', color=col255(48, 48, 50), position=(x, 0.07, z),
               rotation_y=ry, scale=(0.50, 0.04, 0.50)),
    ]
    # 10 bands from base to tip -> a far smoother cone than 3 chunky blocks
    steps = 10
    y = 0.10
    for i in range(steps):
        t = i / (steps - 1)
        w = 0.40 * (1.0 - t) + 0.055
        h = 0.075
        if 0.28 < t < 0.42 or 0.58 < t < 0.72:
            c = white                      # the two reflective bands
        else:
            c = orange if i % 2 == 0 else orange_d
        ents.append(Entity(model='cube', color=c, rotation_y=ry,
                           collider='box' if i == 0 else None,
                           position=(x, y + h / 2, z), scale=(w, h, w)))
        y += h
    ents.append(Entity(model='cube', color=orange, rotation_y=ry,
                       position=(x, y + 0.03, z), scale=(0.05, 0.06, 0.05)))
    return ents


def _prop_puddle(x, z, r):
    """A damp stain: nested organic patches, dark water inside a paler wet rim.
    Comes in a few flavours so they don't all look like the same puddle."""
    kind = r.random()
    if kind < 0.45:            # grey water
        ramp = ((74, 80, 78), (38, 44, 48), (24, 29, 33))
    elif kind < 0.78:          # rusty / dirty brown
        ramp = ((100, 84, 60), (64, 50, 33), (40, 30, 19))
    else:                      # stagnant green
        ramp = ((74, 86, 64), (44, 57, 40), (27, 37, 26))
    s = r.uniform(0.7, 1.8)
    layers = r.randint(2, 3)
    ents = []
    for i in range(layers):
        cr, cg, cb = ramp[i]
        j = r.randint(-7, 7)
        ents.append(_blob(x + r.uniform(-0.12, 0.12), 0.020 + i * 0.008,
                          z + r.uniform(-0.12, 0.12),
                          s * (1.0 - i * 0.28),
                          col255(cr + j, cg + j, cb + j), r,
                          wobble=r.uniform(0.26, 0.44)))
    return ents


def _prop_crack(cx, cz, r, force_dir=None):
    """A thin branching crack on an adjacent wall (falls back to a stain).
    force_dir pins which side the wall is on, for the showcase where the spawn
    area is deliberately wall-free."""
    adj = [force_dir] if force_dir else \
          [(dx, dz) for (dx, dz) in ((1, 0), (-1, 0), (0, 1), (0, -1))
           if cell_is_wall(cx + dx, cz + dz)]
    if not adj:
        return _prop_puddle(cx * CELL, cz * CELL, r)
    dx, dz = r.choice(adj)
    # The wall cube fills cell (cx+dx, cz+dz); its face toward us is half a cell
    # back from its centre. The decal is a FLAT mesh sitting a hair proud of that
    # face, so it reads as a fissure in the surface -- the old thin cubes stuck
    # out in front of the wall, which is why they looked stuck on top of it.
    skin = 0.012
    if dx != 0:
        px = (cx + dx) * CELL - dx * (CELL / 2 + skin)
        pz = cz * CELL + r.uniform(-1.1, 1.1)
        ry = -90 * dx
    else:
        px = cx * CELL + r.uniform(-1.1, 1.1)
        pz = (cz + dz) * CELL - dz * (CELL / 2 + skin)
        ry = 180 if dz > 0 else 0

    top = r.uniform(1.6, WALL_H - 2.2)
    dark = col255(r.randint(16, 30), r.randint(14, 26), r.randint(12, 22))
    ents = [Entity(model=_crack_mesh(r, length=r.uniform(1.2, 2.6),
                                     width=r.uniform(0.05, 0.10)),
                   color=dark, position=(px, top, pz), rotation_y=ry,
                   double_sided=True)]
    # A faint wider halo so the fissure reads as depth rather than a drawn line.
    # It sits nearer the wall than the crack itself, so depth-testing keeps it
    # behind instead of painting over the dark centre.
    if r.random() < 0.7:
        hx = px + (dx * 0.006 if dx else 0)
        hz = pz + (dz * 0.006 if dz else 0)
        ents.append(Entity(model=_crack_mesh(r, length=r.uniform(1.0, 2.2),
                                             width=r.uniform(0.14, 0.24),
                                             segments=6),
                           color=col255(92, 84, 60), position=(hx, top, hz),
                           rotation_y=ry, double_sided=True, alpha=0.45))
    return ents


def _prop_ceiling_stain(x, z, r):
    """Brown water damage spreading across the ceiling tiles overhead."""
    # Concentric organic rings: pale halo outside, darkening to a soaked core,
    # so it reads as water spreading through the tiles. double_sided because you
    # only ever see the underside of the ceiling.
    spread = r.uniform(0.7, 1.9)
    rings = r.randint(3, 5)
    # vary the tint: some are yellow-brown water damage, some greyer/older
    warm = r.uniform(0.0, 1.0)
    base = (150 - 16 * (1 - warm), 132 - 26 * (1 - warm), 88 - 24 * (1 - warm))
    ents = []
    for i in range(rings):
        t = i / max(1, rings - 1)                 # 0 = outer halo, 1 = core
        f = 1.0 - t * 0.76
        j = r.randint(-8, 8)
        # translucent, so the layers soak into the lit tile instead of sitting
        # on it like a flat sticker
        c = color.rgba((base[0] - 58 * t + j) / 255, (base[1] - 56 * t + j) / 255,
                       (base[2] - 42 * t + j) / 255, 0.26 + 0.26 * t)
        ents.append(_blob(x + r.uniform(-0.22, 0.22),
                          WALL_H - 0.03 - i * 0.006,
                          z + r.uniform(-0.22, 0.22),
                          spread * f, c, r,
                          wobble=r.uniform(0.24, 0.46), double_sided=True))
    return ents


def _prop_backpack(x, z, r):
    """Someone's abandoned pack, slumped on the carpet. Built at unit size and
    scaled by BACKPACK_SIZE about its footprint."""
    k = BACKPACK_SIZE
    fabric = r.choice([col255(48, 56, 82), col255(74, 52, 44),
                       col255(44, 64, 50), col255(64, 60, 54)])
    strap = col255(30, 30, 34)
    ry = r.uniform(0, 360)
    a = math.radians(ry)

    def off(ox, oz):     # rotate a local offset into world space
        # ursina's rotation_y turns local +z toward world (sin a, cos a) and
        # local +x toward (cos a, -sin a); this must match it or the parts
        # swing round the opposite way from the body they belong to
        return (x + (ox * math.cos(a) + oz * math.sin(a)) * k,
                z + (-ox * math.sin(a) + oz * math.cos(a)) * k)

    def sc(w, h, d):
        return (w * k, h * k, d * k)

    dark = col255(int(fabric[0] * 180), int(fabric[1] * 180), int(fabric[2] * 180))
    buckle = col255(180, 176, 168)
    ents = []
    # main body in three stacked slabs so it bulges rather than being one box
    for i, (w, h, d) in enumerate(((0.48, 0.14, 0.28),
                                   (0.54, 0.16, 0.33),
                                   (0.50, 0.14, 0.30))):
        bx, bz = off(0, 0)
        ents.append(Entity(model='cube', texture='white_cube', rotation_y=ry,
                           collider='box' if i == 1 else None,
                           color=fabric if i != 1 else dark,
                           position=(bx, (0.08 + i * 0.145) * k, bz), scale=sc(w, h, d)))
    # rounded top flap
    tx, tz = off(0, 0)
    ents.append(Entity(model='cube', color=fabric, rotation_y=ry,
                       position=(tx, 0.44 * k, tz), scale=sc(0.44, 0.10, 0.26)))
    # front pocket with a zip line and buckle
    px, pz = off(0.0, -0.19)
    ents.append(Entity(model='cube', color=dark, rotation_y=ry,
                       position=(px, 0.17 * k, pz), scale=sc(0.36, 0.20, 0.09)))
    ents.append(Entity(model='cube', color=buckle, rotation_y=ry,
                       position=(px, 0.27 * k, pz), scale=sc(0.30, 0.025, 0.06)))
    ents.append(Entity(model='cube', color=buckle, rotation_y=ry,
                       position=(px, 0.13 * k, pz), scale=sc(0.08, 0.05, 0.06)))
    # carry handle on top
    hx, hz = off(0, 0.10)
    ents.append(Entity(model='cube', color=strap, rotation_y=ry,
                       position=(hx, 0.52 * k, hz), scale=sc(0.16, 0.04, 0.05)))
    # shoulder straps, each in two pieces so they bend over the body
    for sgn in (-1, 1):
        s1x, s1z = off(sgn * 0.15, 0.17)
        s2x, s2z = off(sgn * 0.17, 0.13)
        ents.append(Entity(model='cube', color=strap,
                           position=(s1x, 0.34 * k, s1z), scale=sc(0.07, 0.24, 0.05),
                           rotation=(r.uniform(-12, 12), ry, 0)))
        ents.append(Entity(model='cube', color=strap,
                           position=(s2x, 0.12 * k, s2z), scale=sc(0.06, 0.20, 0.05),
                           rotation=(r.uniform(-25, -8), ry, 0)))
    return ents


def _prop_lighter(x, z, r, y=0.0, size=None):
    """A disposable lighter, lying flat -- on the carpet, or on whatever `y`
    puts it on. Small enough to walk past, and the only thing down here that
    can hurt it."""
    k = LIGHTER_SIZE if size is None else size
    shell = r.choice([col255(168, 36, 40), col255(176, 138, 32),
                      col255(34, 74, 132), col255(46, 46, 52)])
    metal = col255(178, 174, 162)
    ry = r.uniform(0, 360)
    a = math.radians(ry)

    def off(ox, oz):     # same convention as ursina's rotation_y (see backpack)
        return (x + (ox * math.cos(a) + oz * math.sin(a)) * k,
                z + (-ox * math.sin(a) + oz * math.cos(a)) * k)

    ents = []
    bx, bz = off(0, 0)
    ents.append(Entity(model='cube', texture='white_cube', color=shell,
                       rotation_y=ry, position=(bx, y + 0.032 * k, bz),
                       scale=(0.062 * k, 0.055 * k, 0.115 * k)))
    mx, mz = off(0, 0.076)                       # the metal hood at one end
    ents.append(Entity(model='cube', color=metal, rotation_y=ry,
                       position=(mx, y + 0.034 * k, mz),
                       scale=(0.052 * k, 0.050 * k, 0.046 * k)))
    fx, fz = off(0, 0.098)                       # the flint wheel
    ents.append(Entity(model='cube', color=col255(118, 114, 106), rotation_y=ry,
                       position=(fx, y + 0.050 * k, fz),
                       scale=(0.030 * k, 0.020 * k, 0.018 * k)))
    for e in ents:
        e.emit = LIGHTER_EMIT          # applied by _lit, after the shader goes on
    # a barely-there warm pool under it: enough to catch the eye in the dark,
    # never enough to read as a light source
    halo = Entity(model='quad', texture=_get_puff_texture(), double_sided=True,
                  position=(x, y + 0.015, z), rotation_x=90, scale=1.4,
                  color=color.rgba(1.0, 0.66, 0.30, LIGHTER_HALO))
    halo.setAttrib(_flame_blend())
    halo.setDepthWrite(False)
    halo.setBin('transparent', 4)
    halo.unlit_fx = True               # _lit must leave it on its own shader
    ents.append(halo)
    return ents


def _prop_spawn_desk(x, z):
    """A cheap old office desk left in the spawn room: laminate top gone yellow,
    thin steel legs, one drawer. Returns (entities, height of its top)."""
    top_h = 1.55                                 # waist height at this world's scale
    w, d = 2.0, 1.05
    lam = col255(150, 140, 110)
    edge = col255(96, 88, 66)
    steel = col255(78, 77, 72)
    ents = [
        # One invisible box over the whole desk, floor to top, is what you bump
        # into. A collider on the top slab alone never blocked anything: the
        # player's collision rays sample at 0.5, 1.6 and 3.1 up, and a thin top
        # at 1.5 slips between them.
        Entity(model='cube', collider='box', visible=False,
               position=(x, top_h / 2, z), scale=(w, top_h, d)),
        Entity(model='cube', texture='white_cube', color=lam,
               position=(x, top_h - 0.04, z), scale=(w, 0.08, d)),
        Entity(model='cube', color=edge,               # the chipped front lip
               position=(x, top_h - 0.09, z - d / 2 + 0.02), scale=(w, 0.05, 0.04)),
        Entity(model='cube', color=edge,               # a drawer under one end
               position=(x + w * 0.27, top_h - 0.26, z - 0.04),
               scale=(w * 0.38, 0.28, d * 0.86)),
        Entity(model='cube', color=steel,              # its handle
               position=(x + w * 0.27, top_h - 0.25, z - d / 2 - 0.005),
               scale=(0.22, 0.03, 0.03)),
    ]
    for sx in (-1, 1):
        for sz in (-1, 1):
            ents.append(Entity(model='cube', color=steel,
                               position=(x + sx * (w / 2 - 0.08), (top_h - 0.08) / 2,
                                         z + sz * (d / 2 - 0.08)),
                               scale=(0.06, top_h - 0.08, 0.06)))
    ents.append(Entity(model='cube', color=steel,      # a rail between the back legs
                       position=(x, 0.32, z + d / 2 - 0.08), scale=(w - 0.2, 0.04, 0.04)))
    return ents, top_h


# Weighted spawn table. Higher weight = shows up more often. Total weight is what
# each entry is measured against, so adding a prop dilutes the others slightly.
# Weights add up to 100 here, so each number IS that prop's percentage.
PROP_TABLE = (
    ('ceiling_stain', 17),
    ('paint_bucket', 17),
    ('crack', 17),
    ('cone', 17),
    ('puddle', 17),
    ('backpack', 9),
    ('lighter', 6),       # the rarest thing in the building, and the only weapon
)
_PROP_KINDS = [k for k, _ in PROP_TABLE]
_PROP_WEIGHTS = [w for _, w in PROP_TABLE]


def spawn_prop(cx, cz):
    """Deterministically maybe spawn a random prop in open cell (cx, cz).

    Returns (entities, pickup) where pickup is None, or a dict describing a
    carryable item sitting at that spot. Same prop every visit.
    """
    if (cx, cz) == SPAWN_LIGHTER_CELL:
        # generated like any other prop, so it comes back when the chunk reloads.
        # Only the lighter goes in the pickup, so taking it leaves the desk.
        r = random.Random(SEED + 991)
        dx, dz = SPAWN_DESK_POS
        desk, top_h = _prop_spawn_desk(dx, dz)
        x, z = dx - 0.35, dz - 0.1
        lighter = _prop_lighter(x, z, r, y=top_h)
        return desk + lighter, {'kind': 'lighter', 'x': x, 'z': z, 'ents': lighter}
    if max(abs(cx), abs(cz)) <= SPAWN_ROOM:
        return [], None           # the spawn room is bare apart from the lighter
    r = random.Random((cx * 2654435761) ^ (cz * 40503) ^ (SEED + 7))
    if r.random() >= PROP_DENSITY:
        return [], None
    x = cx * CELL + r.uniform(-1.0, 1.0)
    z = cz * CELL + r.uniform(-1.0, 1.0)
    kind = r.choices(_PROP_KINDS, _PROP_WEIGHTS)[0]
    if kind == 'crack':
        ents = _prop_crack(cx, cz, r)
    elif kind == 'ceiling_stain':
        ents = _prop_ceiling_stain(x, z, r)
    elif kind == 'puddle':
        ents = _prop_puddle(x, z, r)
    elif kind == 'paint_bucket':
        ents = _prop_paint_bucket(x, z, r)
    elif kind == 'cone':
        ents = _prop_cone(x, z, r)
    elif kind == 'lighter':
        ents = _prop_lighter(x, z, r)
    else:
        ents = _prop_backpack(x, z, r)

    pickup = None
    if kind in ITEMS:
        pickup = {'kind': kind, 'x': x, 'z': z, 'ents': ents}
    return ents, pickup


# ============================================================================
#  LOOK: procedural textures, lighting and shaders
# ============================================================================
# Every surface is generated here at startup (no image files to ship), and all
# of it is lit per-pixel. The ceiling troffers sit on a fixed grid, so a shader
# can find the few that can reach any pixel by rounding its position -- it only
# needs a small texture saying how bright each troffer is right now. A second
# small texture marks which cells are walls, which is enough for walls to cast
# shadows and for corners to darken, without any real shadow maps.

MAT_FLOOR, MAT_CEILING, MAT_WALL, MAT_LIGHT = 0, 1, 2, 3

WORLD_SHADER = None     # room shell: floor, ceiling, walls, troffers
OBJ_SHADER = None       # props and the monster
POST_SHADER = None      # full-screen grade, grain, vignette, pause blur
_look_textures = {}     # name -> ursina Texture, kept alive for the session


def _np_noise(h, w, cy, cx, rng):
    """Seamless value noise: a random cy x cx lattice, smoothly interpolated
    with wraparound, so the result tiles perfectly in both directions."""
    import numpy as np
    g = rng.random((cy, cx))

    def axis(n, c):
        t = np.arange(n) * c / n
        i0 = np.floor(t).astype(int)
        f = t - i0
        return i0 % c, (i0 + 1) % c, f * f * (3 - 2 * f)

    y0, y1, fy = axis(h, cy)
    x0, x1, fx = axis(w, cx)
    fx, fy = fx[None, :], fy[:, None]
    top = g[y0][:, x0] * (1 - fx) + g[y0][:, x1] * fx
    bot = g[y1][:, x0] * (1 - fx) + g[y1][:, x1] * fx
    return top * (1 - fy) + bot * fy


def _np_fbm(h, w, cy, cx, octaves, rng, gain=0.5):
    """Several octaves of _np_noise, finer and fainter each time."""
    total, amp, norm = 0.0, 1.0, 0.0
    for o in range(octaves):
        m = 2 ** o
        if cy * m > h or cx * m > w:
            break
        total = total + _np_noise(h, w, cy * m, cx * m, rng) * amp
        norm += amp
        amp *= gain
    return total / norm


def _make_texture(name, rgb, height):
    """Pack colour + a height map (in alpha, used for bump shading) into a
    mipmapped, anisotropically filtered, repeating texture."""
    import numpy as np
    from PIL import Image
    from panda3d.core import SamplerState
    rgba = np.dstack([np.clip(rgb, 0, 1), np.clip(height, 0, 1)])
    img = Image.fromarray((rgba * 255 + 0.5).astype(np.uint8), 'RGBA')
    tex = Texture(img)
    pt = tex._texture
    pt.setMinfilter(SamplerState.FT_linear_mipmap_linear)
    pt.setMagfilter(SamplerState.FT_linear)
    pt.setAnisotropicDegree(8)
    pt.setWrapU(SamplerState.WM_repeat)
    pt.setWrapV(SamplerState.WM_repeat)
    _look_textures[name] = tex
    return tex


def _tex_wallpaper(rng):
    """Mono-yellow wallpaper for one cell-wide strip of wall, floor to ceiling:
    faint pinstripes and a diamond motif, paper grain, strip seams, water
    running down from the ceiling, tide-lined stains and grime at the bottom."""
    import numpy as np
    H, W = 1024, 384
    u = (np.arange(W)[None, :] + 0.5) / W
    hgt = 1.0 - (np.arange(H)[:, None] + 0.5) / H       # 0 = floor, 1 = ceiling
    wy = hgt * WALL_H                                     # height in world units

    su = (u * 3.0) % 1.0
    stripe = (np.exp(-((su - 0.18) / 0.011) ** 2)
              + np.exp(-((su - 0.25) / 0.011) ** 2))
    mu = ((u * 3.0 + 0.5) % 1.0) - 0.5
    mv = ((wy / 0.42) % 1.0) - 0.5
    diamond = np.clip((0.26 - (np.abs(mu) * 2.2 + np.abs(mv) * 1.1)) * 12, 0, 1)

    grain = 0.6 * _np_noise(H, W, 512, 192, rng) + 0.4 * _np_noise(H, W, 256, 96, rng)
    blot = _np_fbm(H, W, 6, 2, 5, rng)

    cols = np.clip((_np_noise(1, W, 1, 24, rng)[0] - 0.60) * 4.0, 0, 1)
    run = 0.20 + 0.55 * _np_noise(1, W, 1, 12, rng)[0]
    streak = cols[None, :] * np.clip(1.0 - (1.0 - hgt) / run[None, :], 0, 1) ** 1.6

    field = _np_fbm(H, W, 8, 3, 5, rng)
    zone = np.clip(np.clip((hgt - 0.70) * 4, 0, 1) + np.clip((0.24 - hgt) * 5, 0, 1), 0, 1)
    stain = np.clip((field - 0.57) * 6.0, 0, 1) * zone
    rim = np.exp(-((field - 0.57) / 0.012) ** 2) * zone

    grime = np.clip(1.0 - wy / 1.2, 0, 1) ** 2 * (0.6 + 0.4 * blot)
    yellowed = np.clip((wy - 8.0) / 3.0, 0, 1)
    dist_seam = np.minimum(u % 0.5, 0.5 - (u % 0.5))
    seam = np.exp(-(dist_seam / 0.0035) ** 2)

    shade = ((1 - 0.06 * stripe) * (1 - 0.05 * diamond)
             * (0.92 + 0.08 * blot) * (0.96 + 0.07 * grain)
             * (1 - 0.30 * grime) * (1 - 0.22 * streak)
             * (1 - 0.10 * stain - 0.24 * rim) * (1 - 0.20 * seam))
    col = np.array([201, 185, 122]) / 255 * shade[..., None]
    brown = np.array([0.62, 0.50, 0.30])
    amt = np.clip(0.55 * grime + 0.5 * stain + 0.6 * rim + 0.45 * streak, 0, 1)[..., None] * 0.4
    col = col * (1 - amt) + col * brown * amt
    col[..., 2] *= 1 - 0.14 * yellowed
    height = 0.5 + 0.20 * stripe + 0.18 * diamond + 0.16 * (grain - 0.5) - 0.3 * seam
    return _make_texture('wall', col, height)


def _tex_carpet(rng):
    """Low-pile office carpet, damp: tufted fibres and old water mottling."""
    import numpy as np
    n = 512
    fine = _np_noise(n, n, 256, 256, rng)
    fine2 = _np_noise(n, n, 128, 128, rng)
    y, x = (np.mgrid[0:n, 0:n] + 0.5) / n
    tuft = np.sin(x * math.tau * 72) * np.sin(y * math.tau * 72) * 0.5 + 0.5
    mottle = _np_fbm(n, n, 4, 4, 5, rng)
    pile = 0.55 * fine + 0.30 * fine2 + 0.15 * tuft
    col = np.array([150, 136, 84]) / 255 * ((0.78 + 0.32 * pile) * (0.86 + 0.18 * mottle))[..., None]
    damp = np.clip((0.45 - mottle) * 3, 0, 1)[..., None] * 0.3
    col = col * (1 - damp) + np.array([0.30, 0.29, 0.17]) * damp
    return _make_texture('carpet', col, pile)


def _tex_ceiling(rng):
    """One fissured acoustic ceiling tile inside its metal T-bar."""
    import numpy as np
    n = 512
    y, x = (np.mgrid[0:n, 0:n] + 0.5) / n
    edge = np.minimum(np.minimum(x, 1 - x), np.minimum(y, 1 - y))
    grid = np.clip(1 - edge / 0.018, 0, 1)
    bevel = np.clip(1 - (edge - 0.018) / 0.022, 0, 1) * (1 - grid)
    f = _np_fbm(n, n, 24, 24, 3, rng)
    fissure = np.exp(-((f - 0.5) / 0.02) ** 2)
    holes = np.clip((_np_noise(n, n, 160, 160, rng) - 0.80) * 8, 0, 1)
    speck = _np_noise(n, n, 256, 256, rng)
    shade = (0.92 + 0.09 * speck) * (1 - 0.22 * fissure) * (1 - 0.35 * holes) * (1 - 0.32 * bevel)
    col = np.array([212, 206, 182]) / 255 * shade[..., None]
    metal = np.array([0.78, 0.76, 0.70]) * (0.95 + 0.05 * speck[..., None])
    col = col * (1 - grid[..., None]) + metal * grid[..., None]
    height = 0.55 + 0.1 * speck - 0.25 * fissure - 0.35 * holes - 0.3 * bevel + 0.4 * grid
    return _make_texture('ceiling', col, height)


def _tex_grime(rng):
    """Large-scale dirt fields sampled at world scale over everything, so the
    small repeating textures never show their repeats.
    r = stains, g = the tide line at a stain's edge, b = wet, a = dirt."""
    import numpy as np
    n = 512
    big = _np_fbm(n, n, 3, 3, 6, rng)
    stain = np.clip((big - 0.56) * 5, 0, 1)
    rim = np.exp(-((big - 0.56) / 0.010) ** 2)
    wet = np.clip((_np_fbm(n, n, 4, 4, 5, rng) - 0.60) * 5, 0, 1)
    dirt = _np_fbm(n, n, 8, 8, 5, rng)
    return _make_texture('grime', np.dstack([stain, rim, wet]), dirt)


def _tex_skin(rng):
    """Bloodless grey hide: mottling, bruising, veins showing through."""
    import numpy as np
    n = 256
    mottle = _np_fbm(n, n, 6, 6, 5, rng)
    fine = _np_noise(n, n, 128, 128, rng)
    veins = np.exp(-((_np_fbm(n, n, 5, 5, 4, rng) - 0.5) / 0.013) ** 2)
    dirt = _np_fbm(n, n, 3, 3, 4, rng)
    col = np.array([138, 130, 121]) / 255 * ((0.62 + 0.52 * mottle) * (0.90 + 0.16 * fine)
                                             * (0.80 + 0.25 * dirt))[..., None]
    bruise = np.clip((0.42 - mottle) * 4, 0, 1)[..., None] * 0.45
    col = col * (1 - bruise) + np.array([0.30, 0.21, 0.24]) * bruise
    v = veins[..., None] * 0.32
    col = col * (1 - v) + np.array([0.26, 0.24, 0.31]) * v
    height = 0.5 + 0.52 * (mottle - 0.5) + 0.36 * (fine - 0.5) - 0.48 * veins
    return _make_texture('skin', col, height)


def _grid_texture(name, n):
    """An n x n single-channel data texture, read with texelFetch (no filtering)."""
    from panda3d.core import Texture as PandaTexture, SamplerState
    t = PandaTexture(name)
    t.setup2dTexture(n, n, PandaTexture.T_unsigned_byte, PandaTexture.F_luminance)
    t.setMinfilter(SamplerState.FT_nearest)
    t.setMagfilter(SamplerState.FT_nearest)
    t.setWrapU(SamplerState.WM_clamp)
    t.setWrapV(SamplerState.WM_clamp)
    t.setRamImage(bytes(n * n))
    return t


@lru_cache(maxsize=1 << 15)
def _light_kind(i, j):
    """What the troffer at grid index (i, j) is: (None, 0) when it would be
    buried in a wall, else ('on' | 'dead' | 'flicker', brightness). Same every
    visit. The few around spawn always work, so you don't start in the dark."""
    if cell_is_wall(i * LIGHT_EVERY, j * LIGHT_EVERY):
        return None, 0.0
    r = random.Random((i * 92821) ^ (j * 689287) ^ (SEED + 31))
    roll = r.random()
    bright = r.uniform(0.74, 1.0)
    if abs(i) <= 1 and abs(j) <= 1:
        return 'on', max(bright, 0.85)
    if roll < LIGHT_DEAD_CHANCE:
        return 'dead', 0.0
    if roll < LIGHT_DEAD_CHANCE + LIGHT_FLICKER_CHANCE:
        return 'flicker', bright
    return 'on', bright


# ---- GLSL -------------------------------------------------------------------

_GLSL_COMMON = '''
uniform sampler2D light_grid;
uniform sampler2D occ_grid;
uniform vec2 light_origin;
uniform vec2 occ_origin;
uniform float u_power;
uniform float u_time;
uniform vec3 camera_world_position;
uniform vec4 fog_color;
uniform float fog_start;
uniform float fog_end;

const float LIGHT_SPACING = @LIGHT_SPACING@;
const float LIGHT_Z_OFF = @LIGHT_Z_OFF@;
const float CEIL_Y = @CEIL_Y@;
const float CELL = @CELL@;
const float CEIL_TILE = @CEIL_TILE@;
const int LIGHT_N = @LIGHT_N@;
const int OCC_N = @OCC_N@;
const vec3 LIGHT_TINT = vec3(1.0, 0.93, 0.72);
const vec3 AMBIENT = vec3(0.030, 0.028, 0.020);

float troffer(ivec2 idx) {
    ivec2 t = idx - ivec2(floor(light_origin + 0.5));
    if (t.x < 0 || t.y < 0 || t.x >= LIGHT_N || t.y >= LIGHT_N) return 0.0;
    return texelFetch(light_grid, t, 0).r;
}

const ivec2 NO_CELL = ivec2(-1000000);

ivec2 cell_of(vec2 xz) {
    return ivec2(floor(xz / CELL + 0.5));
}

float cell_wall(ivec2 cell) {
    ivec2 c = cell - ivec2(floor(occ_origin + 0.5));
    if (c.x < 0 || c.y < 0 || c.x >= OCC_N || c.y >= OCC_N) return 0.0;
    return texelFetch(occ_grid, c, 0).r;
}

// Contact darkness from nearby walls, measured as the true distance to each
// wall block, so it fades off smoothly instead of taking the shape of the
// grid. skip = a cell to leave out (the wall a wall-face pixel belongs to).
float wall_ao(vec2 xz, float reach, ivec2 skip) {
    ivec2 c = cell_of(xz);
    float occ = 0.0;
    for (int j = -1; j <= 1; j++) {
        for (int i = -1; i <= 1; i++) {
            ivec2 q = c + ivec2(i, j);
            if (q == skip || cell_wall(q) < 0.5) continue;
            vec2 dq = abs(xz - vec2(q) * CELL) - vec2(CELL * 0.5);
            occ += exp(-length(max(dq, vec2(0.0))) / reach);
        }
    }
    return occ;
}

// 1.0 if the straight line from a to b (world xz) crosses no wall cell. An
// exact walk through the grid (Amanatides & Woo), so shadow edges run true
// from the tube past the wall's corner instead of stepping along the cells.
float ray_clear(vec2 a, vec2 b) {
    vec2 ga = a / CELL + 0.5;
    vec2 d = (b - a) / CELL;
    ivec2 cell = ivec2(floor(ga));
    ivec2 end = ivec2(floor(ga + d));
    ivec2 stp = ivec2(d.x >= 0.0 ? 1 : -1, d.y >= 0.0 ? 1 : -1);
    vec2 inv = 1.0 / max(abs(d), vec2(1e-5));
    vec2 fr = ga - vec2(cell);
    vec2 tmax = vec2(stp.x > 0 ? 1.0 - fr.x : fr.x, stp.y > 0 ? 1.0 - fr.y : fr.y) * inv;
    for (int i = 0; i < 16; i++) {
        if (cell == end || min(tmax.x, tmax.y) > 1.0) return 1.0;
        if (tmax.x < tmax.y) {
            cell.x += stp.x;
            tmax.x += inv.x;
        } else {
            cell.y += stp.y;
            tmax.y += inv.y;
        }
        if (cell_wall(cell) > 0.5) return 0.0;
    }
    return 1.0;
}

// bump from a height value without tangents (screen-space derivatives)
vec3 bump(vec3 N, vec3 P, float h, float strength) {
    if (strength <= 0.0) return N;
    vec3 dpdx = dFdx(P);
    vec3 dpdy = dFdy(P);
    float dhx = dFdx(h);
    float dhy = dFdy(h);
    vec3 r1 = cross(dpdy, N);
    vec3 r2 = cross(N, dpdx);
    float det = dot(dpdx, r1);
    vec3 grad = sign(det) * (dhx * r1 + dhy * r2);
    return normalize(abs(det) * N - strength * grad);
}

// light from the 3x3 troffers around P, each a short tube shining downward,
// shadowed by any wall standing between the pixel and the tube. bounce is the
// light reflected back off the floor and ceiling, which reaches a surface
// whichever way it faces -- it is what keeps walls from going black.
vec3 gather(vec3 P, vec3 N, vec3 V, float wrap, float shininess, float spec_amt, float bounce, out vec3 spec) {
    ivec2 c = ivec2(floor(P.x / LIGHT_SPACING + 0.5), floor((P.z - LIGHT_Z_OFF) / LIGHT_SPACING + 0.5));
    vec3 diff = vec3(0.0);
    spec = vec3(0.0);
    for (int dj = -1; dj <= 1; dj++) {
        for (int di = -1; di <= 1; di++) {
            ivec2 idx = c + ivec2(di, dj);
            float I = troffer(idx);
            if (I < 0.004) continue;
            vec3 Lc = vec3(float(idx.x) * LIGHT_SPACING, CEIL_Y - 0.14, float(idx.y) * LIGHT_SPACING + LIGHT_Z_OFF);
            vec3 Lp = vec3(Lc.x + clamp(P.x - Lc.x, -1.1, 1.1), Lc.y, Lc.z + clamp(P.z - Lc.z, -0.5, 0.5));
            vec3 L = Lp - P;
            float hd = length(L.xz);
            float cutoff = 1.0 - smoothstep(8.0, 17.0, hd);
            if (cutoff <= 0.0) continue;
            float d2 = dot(L, L);
            vec3 Ld = L / max(sqrt(d2), 1e-4);
            float ndl = max((dot(N, Ld) + wrap) / (1.0 + wrap), 0.0);
            if (ndl <= 0.0 && bounce <= 0.0) continue;
            float emit = 0.10 + 0.90 * pow(clamp(Ld.y, 0.0, 1.0), 0.7);
            float att = 1.0 / (1.0 + d2 / 64.0);
            // four rays spread across the tube: a wall hiding part of it
            // gives a soft penumbra rather than a hard cut
            vec2 a = P.xz + N.xz * 0.08;
            float shadow = 0.25 * (ray_clear(a, Lc.xz + vec2(-1.05, -0.3))
                                 + ray_clear(a, Lc.xz + vec2(-0.35, 0.3))
                                 + ray_clear(a, Lc.xz + vec2(0.35, -0.3))
                                 + ray_clear(a, Lc.xz + vec2(1.05, 0.3)));
            float k = I * u_power * att * emit * cutoff * shadow;
            // bounce arrives from the whole lit room, so a wall only dims it
            float kb = I * u_power * att * cutoff * mix(0.6, 1.0, shadow) * bounce;
            diff += LIGHT_TINT * (k * ndl + kb);
            if (spec_amt > 0.0) {
                vec3 H = normalize(Ld + V);
                spec += LIGHT_TINT * (k * spec_amt * pow(max(dot(N, H), 0.0), shininess));
            }
        }
    }
    return diff;
}

vec3 tonemap(vec3 c) {
    return 1.0 - exp(-c * 2.2);
}

float fog_amount(vec3 P) {
    float dist = length(P - camera_world_position);
    float t = clamp((dist - fog_start) / max(fog_end - fog_start, 0.001), 0.0, 1.0);
    return (1.0 - exp(-3.4 * t)) / (1.0 - exp(-3.4)) * fog_color.a;
}
'''


def _glsl_common():
    s = _GLSL_COMMON
    for k, v in (('LIGHT_SPACING', LIGHT_SPACING), ('LIGHT_Z_OFF', LIGHT_Z_OFFSET),
                 ('CEIL_Y', WALL_H), ('CELL', CELL), ('CEIL_TILE', CEIL_TILE)):
        s = s.replace(f'@{k}@', f'{float(v):.6f}')
    return s.replace('@LIGHT_N@', str(LIGHT_GRID_N)).replace('@OCC_N@', str(OCC_GRID_N))


_WORLD_VERT = '''
#version 150
uniform mat4 p3d_ModelViewProjectionMatrix;
uniform mat4 p3d_ModelMatrix;
in vec4 p3d_Vertex;
in vec3 p3d_Normal;
in vec2 p3d_MultiTexCoord0;
out vec3 wpos;
out vec3 wnorm;
flat out int mat;
void main() {
    gl_Position = p3d_ModelViewProjectionMatrix * p3d_Vertex;
    wpos = (p3d_ModelMatrix * p3d_Vertex).xyz;
    wnorm = mat3(p3d_ModelMatrix) * p3d_Normal;
    mat = int(p3d_MultiTexCoord0.x + 0.5);
}
'''

_WORLD_FRAG = '''
#version 150
in vec3 wpos;
in vec3 wnorm;
flat in int mat;
out vec4 color;
uniform sampler2D wall_tex;
uniform sampler2D carpet_tex;
uniform sampler2D ceil_tex;
uniform sampler2D grime_tex;
@COMMON@

float hash12(vec2 p) {
    vec3 p3 = fract(vec3(p.xyx) * 0.1031);
    p3 += dot(p3, p3.yzx + 33.33);
    return fract((p3.x + p3.y) * p3.z);
}

void main() {
    vec3 P = wpos;
    vec3 N = normalize(wnorm);
    vec3 V = normalize(camera_world_position - P);
    float dist = length(camera_world_position - P);
    float bumpfade = 1.0 - smoothstep(6.0, 45.0, dist);

    vec3 albedo = vec3(0.0);
    vec3 emissive = vec3(0.0);
    float h = 0.5;
    float bump_k = 0.0;
    float spec_amt = 0.0;
    float shin = 16.0;
    float wrap = 0.3;
    float bounce = 0.12;
    float ao = 1.0;

    if (mat == 0) {                                   // damp carpet
        vec4 t = texture(carpet_tex, P.xz / 3.0);
        vec4 g = texture(grime_tex, P.xz / 41.0);
        vec4 g2 = texture(grime_tex, P.xz / 13.0 + 0.37);
        albedo = t.rgb * (1.0 - 0.34 * g.r - 0.28 * g.g);
        albedo = mix(albedo, albedo * vec3(0.78, 0.76, 0.60), g2.a * 0.55);
        float wet = g.b;
        albedo *= 1.0 - 0.32 * wet;
        spec_amt = 0.04 + 0.6 * wet;
        shin = 22.0 + 50.0 * wet;
        h = t.a;
        bump_k = 0.02;
        float occ = wall_ao(P.xz, 1.1, NO_CELL);
        ao = 1.0 - 0.42 * min(occ, 1.0) - 0.14 * clamp(occ - 1.0, 0.0, 1.0);
        wrap = 0.0;
    } else if (mat == 1) {                            // drop ceiling
        vec2 tp = P.xz / CEIL_TILE;
        vec2 cell = floor(tp);
        vec2 f = tp - cell;
        float hsh = hash12(cell);
        int rot = int(hsh * 4.0);
        vec2 rf = f;
        if (rot == 1) rf = vec2(1.0 - f.y, f.x);
        else if (rot == 2) rf = 1.0 - f;
        else if (rot == 3) rf = vec2(f.y, 1.0 - f.x);
        vec4 t = textureGrad(ceil_tex, rf, dFdx(tp), dFdy(tp));
        albedo = t.rgb;
        h = t.a;
        bump_k = 0.012;
        // Water damage: irregular blotches ringed by darker tide lines, laid
        // out in world space so a stain spreads naturally over neighbouring
        // tiles. Each tile soaks it up a little differently, and the metal
        // T-bars aren't paper, so they stay clean.
        float edge = min(min(f.x, 1.0 - f.x), min(f.y, 1.0 - f.y));
        float paper = smoothstep(0.012, 0.026, edge);
        vec2 suv = P.xz / 19.0 + vec2(0.61, 0.23);
        vec2 fuv = P.xz / 7.3 + vec2(0.17, 0.52);
        vec4 big = texture(grime_tex, suv);
        vec4 fine = texture(grime_tex, fuv);
        // tide lines read from a blurrier mip: a soft brown edge, not a pen line
        float tide = clamp(texture(grime_tex, suv, 2.5).g * 1.6
                           + texture(grime_tex, fuv, 2.5).g * 0.7, 0.0, 1.0);
        float soak = (0.82 + 0.18 * fract(hsh * 7.1)) * paper;
        float water = clamp(big.r * 1.6 + fine.r * 0.7, 0.0, 1.0) * soak;
        albedo *= 1.0 - 0.10 * big.a;
        albedo = mix(albedo, albedo * vec3(0.84, 0.72, 0.50), water * 0.85);
        albedo = mix(albedo, albedo * vec3(0.66, 0.54, 0.36), tide * soak * 0.55);
        if (fract(hsh * 13.7) < 0.006 && edge > 0.02) { // a missing tile: black plenum
            albedo = vec3(0.012, 0.011, 0.009);
            bump_k = 0.0;
        }
        float occ = wall_ao(P.xz, 1.3, NO_CELL);
        ao = 1.0 - 0.28 * min(occ, 1.0) - 0.10 * clamp(occ - 1.0, 0.0, 1.0);
        wrap = 1.0;
        bounce = 0.46;
        spec_amt = 0.02;
    } else if (mat == 2) {                            // wallpaper
        vec2 tc = abs(N.x) > 0.5 ? vec2(P.z, P.y) : vec2(P.x, P.y);
        float u = (tc.x + CELL * 0.5) / CELL;
        float v = clamp(P.y / CEIL_Y, 0.001, 0.999);
        vec4 t = texture(wall_tex, vec2(u, v));
        vec4 g = texture(grime_tex, vec2(tc.x / 31.0, P.y / 17.0 + 0.2));
        albedo = t.rgb * (1.0 - 0.18 * g.r - 0.30 * g.g);
        albedo = mix(albedo, albedo * vec3(0.84, 0.79, 0.60), g.a * 0.38);
        h = t.a;
        bump_k = 0.010;
        float occ = wall_ao(P.xz + N.xz * 0.05, 0.9, cell_of(P.xz - N.xz * 0.5));
        ao = 1.0 - 0.40 * min(occ, 1.0);
        ao *= mix(0.42, 1.0, smoothstep(0.0, 1.8, P.y));
        ao *= mix(0.68, 1.0, smoothstep(0.0, 1.5, CEIL_Y - P.y));
        spec_amt = 0.05;
        shin = 18.0;
        wrap = 0.15;
        bounce = 0.36;
    } else {                                          // fluorescent troffer
        ivec2 idx = ivec2(floor(P.x / LIGHT_SPACING + 0.5), floor((P.z - LIGHT_Z_OFF) / LIGHT_SPACING + 0.5));
        float I = clamp(troffer(idx) * u_power, 0.0, 1.0);
        vec2 lp = vec2(P.x - float(idx.x) * LIGHT_SPACING, P.z - (float(idx.y) * LIGHT_SPACING + LIGHT_Z_OFF));
        float frame = clamp(step(1.08, abs(lp.x)) + step(0.49, abs(lp.y)), 0.0, 1.0);
        vec2 prism = abs(fract(lp * 16.0) - 0.5);
        float diffuser = 0.86 + 0.14 * smoothstep(0.32, 0.5, max(prism.x, prism.y));
        float tubes = 0.72 + 0.28 * (1.0 - smoothstep(0.0, 0.16, abs(abs(lp.y) - 0.22)));
        vec3 lit = vec3(1.35, 1.27, 1.02) * diffuser * tubes;
        vec3 dark = vec3(0.07, 0.07, 0.065) * diffuser;
        emissive = mix(mix(dark, lit, I * I), vec3(0.16, 0.155, 0.14) * (0.3 + 0.7 * I), frame);
    }

    N = bump(N, P, h, bump_k * bumpfade);
    vec3 spec = vec3(0.0);
    vec3 light = vec3(0.0);
    if (mat != 3) light = gather(P, N, V, wrap, shin, spec_amt, bounce, spec);
    vec3 col = albedo * (AMBIENT + light) * ao + spec * ao;
    col = tonemap(col) + emissive;
    col = mix(col, fog_color.rgb, fog_amount(P));
    color = vec4(col, 1.0);
}
'''

_OBJ_VERT = '''
#version 150
uniform mat4 p3d_ModelViewProjectionMatrix;
uniform mat4 p3d_ModelMatrix;
in vec4 p3d_Vertex;
in vec3 p3d_Normal;
in vec2 p3d_MultiTexCoord0;
in vec4 p3d_Color;
out vec3 wpos;
out vec3 wnorm;
out vec2 uv;
out vec4 vcol;
void main() {
    gl_Position = p3d_ModelViewProjectionMatrix * p3d_Vertex;
    wpos = (p3d_ModelMatrix * p3d_Vertex).xyz;
    wnorm = transpose(inverse(mat3(p3d_ModelMatrix))) * p3d_Normal;
    uv = p3d_MultiTexCoord0;
    vcol = p3d_Color;
}
'''

_OBJ_FRAG = '''
#version 150
in vec3 wpos;
in vec3 wnorm;
in vec2 uv;
in vec4 vcol;
out vec4 color;
uniform sampler2D p3d_Texture0;
uniform vec4 p3d_ColorScale;
uniform float obj_fade;     // 1 = solid. Set on a root, it fades everything under it
uniform float obj_bump;
uniform float obj_spec;
uniform float obj_rim;
uniform float obj_emit;
@COMMON@

void main() {
    vec4 t = texture(p3d_Texture0, uv);
    // t.a is a height map for bump(), NOT coverage -- opacity is the entity's
    vec3 base = t.rgb * p3d_ColorScale.rgb * vcol.rgb;
    float opacity = p3d_ColorScale.a * vcol.a * obj_fade;
    if (opacity < 0.02) discard;
    vec3 P = wpos;
    vec3 V = normalize(camera_world_position - P);
    float nl = length(wnorm);
    bool has_n = nl > 0.1;
    vec3 N = has_n ? wnorm / nl : vec3(0.0, 1.0, 0.0);
    float dist = length(camera_world_position - P);
    vec3 Ng = N;                      // geometric normal, before any bump
    if (has_n) N = bump(N, P, t.a, obj_bump * (1.0 - smoothstep(4.0, 30.0, dist)));

    vec3 spec;
    vec3 light = gather(P, N, V, has_n ? 0.25 : 1.0, 34.0, obj_spec, 0.30, spec);
    float ao = 1.0 - 0.35 * min(wall_ao(P.xz, 1.0, NO_CELL), 1.0);
    ao *= mix(0.55, 1.0, smoothstep(0.0, 1.3, P.y));
    vec3 col = base * (AMBIENT + light) * ao + spec;
    // a thin cold rim so a dark shape still separates from darker air
    float rim = pow(1.0 - clamp(abs(dot(Ng, V)), 0.0, 1.0), 3.0) * obj_rim;
    col += rim * vec3(0.30, 0.34, 0.44) * (0.22 + 0.7 * dot(light, vec3(0.333)));
    col = tonemap(col) + base * obj_emit;
    col = mix(col, fog_color.rgb, fog_amount(P) * (obj_emit > 0.0 ? 0.55 : 1.0));
    color = vec4(col, opacity);
}
'''

_POST_FRAG = '''
#version 430
uniform sampler2D tex;
in vec2 uv;
out vec4 color;
uniform float blur_size;
uniform float u_time;
uniform float u_fear;
uniform float u_glitch;   // 0..1 signal-corruption during the chase
uniform float u_black;    // 0..1 momentary blackout

float hash(vec2 p) {
    p = fract(p * vec2(443.897, 441.423));
    p += dot(p, p.yx + 19.19);
    return fract((p.x + p.y) * p.x);
}
float hash1(float x) { return fract(sin(x * 127.1) * 43758.5453); }

void main() {
    vec2 res = vec2(textureSize(tex, 0));
    float g = clamp(u_glitch, 0.0, 1.0);
    vec2 uv2 = uv;

    // --- horizontal tearing: shove bands of scanlines sideways, more as g rises
    float band = floor(uv.y * 26.0);
    float tt = floor(u_time * 14.0);
    float act = step(0.88 - 0.5 * g, hash(vec2(band * 1.7, tt * 0.5)));
    uv2.x += (hash(vec2(band, tt)) - 0.5) * act * (0.09 * g * g);
    // an occasional big slice rip
    float sy = hash1(tt);
    if (hash(vec2(tt, 3.3)) > 0.98 - 0.12 * g && uv.y > sy && uv.y < sy + 0.14)
        uv2.x += (hash1(tt * 2.0) - 0.5) * 0.35;

    vec2 c = uv - 0.5;
    vec2 ca_dir = c * vec2(res.x / res.y, 1.0);
    float r2 = dot(ca_dir, ca_dir);

    // shaky, chromatic vision -- worse as it nears and while glitching
    vec2 wob = u_fear * 0.0014 * vec2(sin(u_time * 7.3 + uv.y * 31.0), cos(u_time * 5.1 + uv.x * 23.0));
    // fx gates every distortion: at rest it is 0 and the picture is clean
    float fx = clamp(max(u_fear, g), 0.0, 1.0);
    float ca = (0.0015 * fx + 0.006 * u_fear + 0.02 * g) * (r2 * 3.0 + 0.35);
    vec3 col = vec3(texture(tex, uv2 + wob + c * ca).r,
                    texture(tex, uv2 + wob).g,
                    texture(tex, uv2 + wob - c * ca).b);

    // blocky channel-shift corruption
    if (g > 0.01) {
        float by = floor(uv.y * 42.0);
        if (hash(vec2(by, floor(u_time * 20.0))) < 0.12 * g) {
            col.r = texture(tex, uv2 + vec2(0.05 * g, 0.0)).r;
            col.b = texture(tex, uv2 - vec2(0.05 * g, 0.0)).b;
        }
    }

    // pause-menu blur: identical to the old camera_vertical_blur behaviour
    if (blur_size > 0.0) {
        vec3 acc = vec3(0.0);
        for (float i = 0.0; i < 10.0; i++) {
            acc += texture(tex, uv2 + vec2(0.0, (i / 9.0 - 0.5) * blur_size)).rgb;
        }
        acc = 1.0 - (1.0 - col) * (1.0 - acc / 10.0);
        col = mix(col, acc, clamp(blur_size * 10.0, 0.0, 1.0));
    }

    // grade: drain colour, sink the shadows toward green-black, yellow highlights
    float lum = dot(col, vec3(0.299, 0.587, 0.114));
    col = mix(vec3(lum), col, 0.86);
    col = mix(col * vec3(0.90, 1.0, 0.92), col * vec3(1.06, 1.0, 0.84), smoothstep(0.12, 0.7, lum));
    col = pow(max(col, vec3(0.0)), vec3(1.05));

    // rolling scanlines while glitching
    col *= 1.0 - g * 0.15 * step(0.5, fract(uv.y * res.y * 0.5 + u_time * 4.0));

    // vignette that tightens and breathes when it is near
    float vr = mix(1.30, 0.60, u_fear) + 0.05 * u_fear * sin(u_time * 5.5);
    float vig = 1.0 - smoothstep(vr - 0.78, vr, sqrt(r2));
    col *= mix(mix(1.0, 0.30, fx), 1.0, vig);

    // animated film grain, heavier while glitching
    col += (hash(uv * res + fract(u_time * 13.7) * 1000.0) - 0.5) * fx * (0.06 + 0.05 * u_fear + 0.11 * g);

    // momentary blackout: crush toward black with a burst of static
    float b = clamp(u_black, 0.0, 1.0);
    float stat = hash(uv * res + u_time * 97.0);
    col = mix(col, vec3(stat) * 0.12, b);
    col *= 1.0 - b * 0.94;

    color = vec4(col, 1.0);
}
'''


def _init_look():
    """Build every texture and shader and hang the shared inputs on the scene
    root, where panda3d passes them down to everything underneath. Must run
    before any geometry is created."""
    global WORLD_SHADER, OBJ_SHADER, POST_SHADER, _light_tex, _occ_tex
    import numpy as np
    rng = np.random.default_rng(SEED)
    scene.set_shader_input('wall_tex', _tex_wallpaper(rng)._texture)
    scene.set_shader_input('carpet_tex', _tex_carpet(rng)._texture)
    scene.set_shader_input('ceil_tex', _tex_ceiling(rng)._texture)
    scene.set_shader_input('grime_tex', _tex_grime(rng)._texture)
    _tex_skin(rng)

    _light_tex = _grid_texture('light_grid', LIGHT_GRID_N)
    _occ_tex = _grid_texture('occ_grid', OCC_GRID_N)
    scene.set_shader_input('light_grid', _light_tex)
    scene.set_shader_input('occ_grid', _occ_tex)
    scene.set_shader_input('light_origin', Vec2(0, 0))
    scene.set_shader_input('occ_origin', Vec2(0, 0))
    scene.set_shader_input('u_power', 1.0)
    scene.set_shader_input('obj_fade', 1.0)      # everything solid unless told
    scene.set_shader_input('u_time', 0.0)

    common = _glsl_common()
    # Only per-entity knobs go in default_input: ursina copies default_input onto
    # EVERY entity that uses the shader, which would shadow the scene-level ones.
    WORLD_SHADER = Shader(name='backrooms_world', language=Shader.GLSL,
                          vertex=_WORLD_VERT,
                          fragment=_WORLD_FRAG.replace('@COMMON@', common))
    OBJ_SHADER = Shader(name='backrooms_object', language=Shader.GLSL,
                        vertex=_OBJ_VERT,
                        fragment=_OBJ_FRAG.replace('@COMMON@', common),
                        default_input={'obj_bump': 0.0, 'obj_spec': 0.08,
                                       'obj_rim': 0.0, 'obj_emit': 0.0})
    POST_SHADER = Shader(name='backrooms_post', fragment=_POST_FRAG,
                         default_input={'blur_size': 0.0, 'u_time': 0.0, 'u_fear': 0.0,
                       'u_glitch': 0.0, 'u_black': 0.0})


def _synth_wav(samples, rate=44100):
    """Float samples, shape (n,) mono or (n, 2) stereo -> 16-bit WAV bytes."""
    import io as _io
    import wave
    import numpy as np
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype('<i2')
    buf = _io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1 if pcm.ndim == 1 else pcm.shape[1])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _sound_sting(rng, rate=44100):
    """A horror stinger: a falling sub-bass hit under a dissonant, wavering
    string cluster that swells in behind it."""
    import numpy as np
    n = int(rate * 2.6)
    t = np.arange(n) / rate
    freq = 30 + 34 * np.exp(-t * 3.0)
    boom = np.sin(2 * np.pi * np.cumsum(freq) / rate) * np.exp(-t * 2.2)
    noise = rng.standard_normal(n)
    hit = np.convolve(noise, np.ones(60) / 60, mode='same') * np.exp(-t * 9.0) * 6.0
    cluster = sum(np.sin(2 * np.pi * f * t + 0.6 * np.sin(2 * np.pi * (5.3 + k) * t))
                  for k, f in enumerate((622.3, 659.3, 698.5, 740.0)))
    swell = np.clip(t / 0.35, 0, 1) * np.exp(-np.maximum(t - 0.35, 0) * 1.4)
    screech = cluster * swell * 0.11 * (0.8 + 0.2 * np.sin(2 * np.pi * 31 * t))
    out = (boom * 0.9 + hit * 0.5 + screech) * np.clip((t[-1] - t) / 0.3, 0, 1)
    return out / max(1e-6, np.abs(out).max()) * 0.95


def _sound_rumble(rng, rate=44100):
    """The dig-out: a floor-deep rumble that builds while nothing is visible,
    a crack and a thump for each arm breaking through, heavy slaps as the hands
    land, dragging scrapes on every haul -- then it drops to near silence just
    before the head snaps round, so the sting lands in a hole."""
    import numpy as np
    n = int(rate * 5.9)
    t = np.arange(n) / rate
    out = np.zeros(n)
    sub = np.sin(2 * np.pi * np.cumsum(26 + 8 * np.exp(-t * 0.4)) / rate)
    low = np.convolve(rng.standard_normal(n), np.ones(240) / 240, mode='same')
    env = np.clip(t / 1.0, 0, 1) ** 1.5
    env *= 1.0 - np.clip((t - 4.4) / 0.4, 0, 1)          # silence before the snap
    out += (sub * 0.4 + low * 1.1) * env * (0.55 + 0.45 * np.clip((t - 1.0) / 2.5, 0, 1))

    def place(sig, at):
        i = int(at * rate)
        j = min(n, i + len(sig))
        out[i:j] += sig[:j - i]

    def thump(at, amp, f0=110, f1=34, decay=9.0, length=0.6):
        k = int(rate * length)
        tt = np.arange(k) / rate
        f = f1 + (f0 - f1) * np.exp(-tt * 26)
        body = np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(-tt * decay)
        place(body * amp, at)

    def crack(at, amp, width=3, decay=70, length=0.35):
        k = int(rate * length)
        tt = np.arange(k) / rate
        sig = np.convolve(rng.standard_normal(k), np.ones(width) / width, mode='same')
        place(sig * np.exp(-tt * decay) * amp, at)

    def scrape(at, amp, length=0.45):
        k = int(rate * length)
        tt = np.arange(k) / rate
        sig = np.convolve(rng.standard_normal(k), np.ones(18) / 18, mode='same')
        sig *= np.clip(tt / 0.05, 0, 1) * np.exp(-tt * 6.0)
        place(sig * amp * 2.2, at)

    for at in (0.35, 0.62, 0.84):                        # the floor splitting
        crack(at, 0.35, width=2, decay=110)
    for at in (1.0, 1.9):                                # an arm punches through
        crack(at, 1.2, width=3, decay=45)
        crack(at + 0.03, 0.8, width=6, decay=30)
        thump(at, 1.0, f0=140, f1=38)
    for at in (1.4, 2.3):                                # the hand slams down
        thump(at, 0.8, f0=90, f1=30, decay=11)
        crack(at, 0.4, width=8, decay=60)
    for at in (2.45, 2.95, 3.45):                        # each haul
        scrape(at, 0.7)
        thump(at + 0.2, 0.55, f0=80, f1=30, decay=12)
    thump(4.45, 0.7, f0=100, f1=32)                      # it stands up out of it
    scrape(4.2, 0.5)
    out *= np.clip((t[-1] - t) / 0.3, 0, 1)
    return out / max(1e-6, np.abs(out).max()) * 0.95


def _sound_chase(rng, rate=44100):
    """The chase cue: 160bpm, a hammering kick, a snarling semitone ostinato
    down in the basses, tritone brass stabs and a shrieking high cluster over a
    sub floor. Two things keep the loop seamless: every sustained pitch is
    snapped to a whole number of cycles per loop, and every hit is written in
    with wrap-around, so decay tails spill over the join instead of being cut.
    """
    import numpy as np
    dur, beat = 3.0, 0.375                  # eight beats at 160bpm
    n = int(rate * dur)
    t = np.arange(n) / rate
    out = np.zeros(n)

    def snap(f):
        """Nearest pitch that completes whole cycles in one loop."""
        return round(f * dur) / dur

    def place(sig, at):
        """Add a hit at time `at`, wrapping its tail around the loop point."""
        i = int(at * rate) % n
        out[(np.arange(len(sig)) + i) % n] += sig

    # --- kick: a hard pitch-dropping thump on every beat, doubled into the turn
    for b in (0, 1, 2, 3, 4, 5, 6, 7, 7.5):
        k = int(rate * 0.30)
        tt = np.arange(k) / rate
        f = 44 + 96 * np.exp(-tt * 34)
        sig = np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(-tt * 8.5)
        click = np.convolve(rng.standard_normal(k), np.ones(3) / 3, mode='same')
        sig += click * np.exp(-tt * 150) * 0.22                        # beater click
        place(sig * 0.95, b * beat)

    # --- ostinato: sixteenths snarling between D2 and the semitone above it
    d2, eb2 = snap(73.42), snap(77.78)
    for i in range(32):
        f = eb2 if i % 8 >= 6 else d2
        k = int(rate * 0.115)
        tt = np.arange(k) / rate
        saw = 2 * ((f * tt) % 1.0) - 1
        saw = np.convolve(np.tanh(saw * 1.8), np.ones(8) / 8, mode='same')   # growl, not fizz
        e = np.exp(-tt * 30) * (1 - np.exp(-tt * 900))
        place(saw * e * 0.38, i * beat / 4)

    # --- stabs: a detuned tritone stack -- the "it is coming" chord
    for b, amp in ((0.0, 1.0), (3.5, 0.85), (4.0, 1.0), (7.5, 0.9)):
        k = int(rate * 0.55)
        tt = np.arange(k) / rate
        stab = np.zeros(k)
        for f in (snap(146.83), snap(207.65), snap(220.0)):   # D3, the tritone, A3
            for det in (-0.7, 0.0, 0.7):
                stab += 2 * (((f + det) * tt) % 1.0) - 1
        e = np.exp(-tt * 6.5) * (1 - np.exp(-tt * 240))
        brass = np.convolve(np.tanh(stab * 0.45), np.ones(5) / 5, mode='same')
        place(brass * e * 0.20 * amp, b * beat)

    # --- backbeat: a cracking hit on two and four
    for b in (2, 6):
        k = int(rate * 0.24)
        tt = np.arange(k) / rate
        body = np.convolve(rng.standard_normal(k), np.ones(10) / 10, mode='same')
        sig = body * np.exp(-tt * 24) + np.sin(2 * np.pi * 190 * tt) * np.exp(-tt * 30) * 0.4
        place(sig * 0.45, b * beat)

    # --- fill: a drum roll tightening over the last beat, into the downbeat
    for j in range(8):
        k = int(rate * 0.09)
        tt = np.arange(k) / rate
        hit = np.convolve(rng.standard_normal(k), np.ones(8) / 8, mode='same')
        hit = hit * np.exp(-tt * 40) * (0.18 + 0.55 * (j / 7) ** 2)
        hit += np.sin(2 * np.pi * (150 + 45 * j) * tt) * np.exp(-tt * 38) * 0.18 * (j / 7)
        place(hit, 7 * beat + j * beat / 8)

    # --- sustained: shrieking semitone cluster, tremolo'd, over a sub floor
    shriek = np.sin(2 * np.pi * snap(1174.7) * t) + np.sin(2 * np.pi * snap(1244.5) * t)
    out += shriek * (0.55 + 0.45 * np.sin(2 * np.pi * snap(16.0) * t)) * 0.042
    out += np.sin(2 * np.pi * snap(36.71) * t) * 0.22
    out += np.tanh(np.sin(2 * np.pi * snap(73.42) * t) * 1.6) * 0.07

    out = np.tanh(out * 1.1)                # light glue: keep the transients
    return out / max(1e-6, np.abs(out).max()) * 0.93


def _sound_ui_move(rng, rate=44100):
    """A dry tick for moving down the list."""
    import numpy as np
    n = int(rate * 0.07)
    t = np.arange(n) / rate
    tone = np.sin(2 * np.pi * 420 * t) * 0.5 + np.sin(2 * np.pi * 631 * t) * 0.22
    click = np.convolve(rng.standard_normal(n), np.ones(6) / 6, mode='same') * 0.5
    out = (tone * 0.5 + click) * np.exp(-t * 52)
    return out / max(1e-6, np.abs(out).max()) * 0.55


def _sound_ui_select(rng, rate=44100):
    """A low thunk for choosing something."""
    import numpy as np
    n = int(rate * 0.35)
    t = np.arange(n) / rate
    f = 60 + 150 * np.exp(-t * 6)
    body = np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(-t * 7)
    air = np.convolve(rng.standard_normal(n), np.ones(30) / 30, mode='same')
    out = body + air * np.exp(-t * 12) * 0.5
    return out / max(1e-6, np.abs(out).max()) * 0.8


def _sound_step(rng, rate=44100):
    """A footfall on old carpet: a dull low thud with a little fibre rustle.
    Deliberately quiet -- it is heard a few times a second for the whole game."""
    import numpy as np
    n = int(rate * 0.22)
    t = np.arange(n) / rate
    noise = rng.standard_normal(n)
    thud = np.convolve(noise, np.ones(90) / 90, mode='same') * 2.6      # the floor
    rustle = np.convolve(noise, np.ones(12) / 12, mode='same') * 0.35   # the pile
    f = 78 + 46 * np.exp(-t * 34)
    body = np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(-t * 24) * 0.55
    out = (thud + rustle) * np.exp(-t * 27) + body
    return out / max(1e-6, np.abs(out).max()) * 0.5


def _sound_step_heavy(rng, rate=44100):
    """Diamond's footfall: far too much weight coming down, and something wet
    dragging after it. This is what tells you where it is when it is behind you."""
    import numpy as np
    n = int(rate * 0.52)
    t = np.arange(n) / rate
    noise = rng.standard_normal(n)
    f = 40 + 62 * np.exp(-t * 24)
    stomp = np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(-t * 9.0)
    floor = np.convolve(noise, np.ones(200) / 200, mode='same') * 3.2 * np.exp(-t * 12)
    # the drag starts just after the foot lands
    scrape = np.convolve(noise, np.ones(26) / 26, mode='same')
    scrape *= np.clip((t - 0.05) * 14, 0, 1) * np.exp(-np.maximum(t - 0.05, 0) * 8.0)
    out = stomp * 1.0 + floor + scrape * 0.30
    return out / max(1e-6, np.abs(out).max()) * 0.85


def _sound_tick(rng, rate=44100):
    """A failing tube striking: the starter's dry snap and a scrap of 120Hz
    ballast buzz that dies almost at once."""
    import numpy as np
    n = int(rate * 0.16)
    t = np.arange(n) / rate
    snap = np.convolve(rng.standard_normal(n), np.ones(2) / 2, mode='same')
    snap *= np.exp(-t * 230)
    buzz = np.sin(2 * np.pi * 120 * t) * 0.5 + np.sin(2 * np.pi * 240 * t) * 0.2
    buzz = np.tanh(buzz * 2.0) * np.exp(-t * 34) * 0.30
    glass = np.sin(2 * np.pi * 3100 * t) * np.exp(-t * 90) * 0.18
    out = snap * 0.9 + buzz + glass
    return out / max(1e-6, np.abs(out).max()) * 0.6


def _sound_caught(rng, rate=44100):
    """It reaches you: a lunge of air, a wet impact and something snapping,
    over a sub that drops away under the whole thing."""
    import numpy as np
    n = int(rate * 1.9)
    t = np.arange(n) / rate
    noise = rng.standard_normal(n)
    # the rush of it closing the last stride
    rush = np.convolve(noise, np.ones(40) / 40, mode='same')
    rush *= np.clip(t / 0.22, 0, 1) ** 2 * np.exp(-np.maximum(t - 0.22, 0) * 11)
    # the impact itself
    hit = np.convolve(noise, np.ones(8) / 8, mode='same') * np.exp(-t * 26) * 1.4
    f = 120 * np.exp(-t * 7.0) + 22
    sub = np.sin(2 * np.pi * np.cumsum(f) / rate) * np.exp(-t * 2.6)
    # three cracks, close together
    out = rush * 0.8 + hit + sub * 1.1
    for ct, amp in ((0.06, 1.0), (0.13, 0.7), (0.21, 0.5)):
        k = np.exp(-np.maximum(t - ct, 0) * 190) * (t >= ct)
        out += np.convolve(rng.standard_normal(n), np.ones(3) / 3, mode='same') * k * amp
    # and a low moan underneath it, sinking
    moan = np.sin(2 * np.pi * np.cumsum(96 * np.exp(-t * 1.4) + 48) / rate)
    out += np.tanh(moan * 1.5) * np.exp(-t * 1.5) * 0.35
    out *= np.clip((t[-1] - t) / 0.25, 0, 1)
    return out / max(1e-6, np.abs(out).max()) * 0.95


def _formant(x, freqs, bws, rate, win=2048):
    """Run a signal through a throat: a few resonant peaks (formants) that can
    glide over time -- `freqs` is a list of (start, end) Hz per formant.

    Done in the frequency domain, frame by frame with overlap-add: each frame's
    spectrum is multiplied by bumps at the formants for that moment. Plain numpy
    and near-instant (importing scipy's filters alone cost two seconds of load)."""
    import numpy as np
    hop = win // 2
    n = len(x)
    pad = np.concatenate([np.zeros(hop), x, np.zeros(win)])
    out = np.zeros_like(pad)
    norm = np.zeros_like(pad)
    w = np.hanning(win)
    f = np.fft.rfftfreq(win, 1.0 / rate)
    frames = range(0, n + hop, hop)
    last = max(1, len(frames) - 1)
    for k, a in enumerate(frames):
        u = k / last
        shape = np.zeros_like(f)
        for (fa, fb), bw, gain in zip(freqs, bws, (1.0, 0.7, 0.35, 0.3, 0.25, 0.2)):
            fc = fa + (fb - fa) * u
            shape += gain / (1.0 + ((f - fc) / (bw * 0.5)) ** 2)   # a resonance
        seg = pad[a:a + win] * w
        out[a:a + win] += np.fft.irfft(np.fft.rfft(seg) * shape, win) * w
        norm[a:a + win] += w * w
    out = out / np.maximum(norm, 1e-6)
    return out[hop:hop + n]


def _sound_burn(rng, rate=44100):
    """Diamond catching fire: two agonised, straining screams with a gasp
    between them -- rough, breaking, over a huge growl and something metallic
    and wrong -- then a wet death rattle, over roaring flame and popping, then
    its knees and its body hitting the floor.

    What makes it a scream rather than a squeal or a groan: not a clean tone
    (one climbing to 730 Hz sounded like a rat), and not a relaxed low voice
    with a falling pitch (that sounded like a man not trying). It is a strained
    register held high, deep fast flutter, pitch breaks, a harsh upper edge --
    with a growl underneath for size and a ring-modulated layer so it is not
    quite human."""
    import numpy as np
    n = int(rate * 4.4)
    t = np.arange(n) / rate

    # --- a scream that costs it something ---------------------------------------
    # Two screams with a ragged, gasping pull of air between them. Each one
    # launches up into a strained register and HOLDS there -- a falling pitch
    # reads as a sigh -- with the voice shaking itself apart: fast rough flutter
    # (the thing that makes a scream sound like a scream), pitch breaks and
    # period-doubling where the throat can't keep up, and a harsh upper edge.
    # Under it a huge distorted growl gives it size, and a ring-modulated layer
    # makes it wrong -- no human throat makes those sidebands.
    #   0.00-1.05  first scream, climbing and straining
    #   1.05-1.25  a wet, catching gasp
    #   1.25-2.10  second scream, higher, breaking up -- then it cracks
    #   2.10-      the voice is gone; the wet death rattle takes over

    def seg(t0, t1, attack=0.03, release=0.08):
        return (np.clip((t - t0) / attack, 0, 1)
                * np.clip((t1 - t) / release, 0, 1))

    s1 = seg(0.0, 1.05, 0.025, 0.07)
    s2 = seg(1.25, 2.10, 0.03, 0.18)
    on = s1 + s2

    # pitch: launch up, strain and hold, a little higher the second time
    def contour(t0, lo, hi, hold):
        k = np.clip((t - t0) / 0.14, 0, 1)
        up = lo + (hi - lo) * (1 - (1 - k) ** 3)
        return up * (1 + hold * np.clip((t - t0 - 0.14) / 0.7, 0, 1))
    f0 = np.where(t < 1.15, contour(0.0, 190, 310, 0.10), contour(1.25, 210, 345, 0.06))
    # it cracks at the end of the second one: the pitch gives way
    f0 *= 1.0 - 0.35 * np.clip((t - 1.85) / 0.25, 0, 1)
    # strain: fast jitter, a wide unsteady wobble, and sudden breaks
    jitter = np.convolve(rng.standard_normal(n), np.ones(90) / 90, mode='same') * 0.9
    wobble = 0.035 * np.sin(2 * np.pi * 6.7 * t + 2.0 * np.sin(2 * np.pi * 1.3 * t))
    breaks = np.ones(n)
    rb = np.random.default_rng(7331)
    for bt in np.sort(rb.uniform(0.15, 2.0, 9)):         # the voice jumps and snaps back
        breaks *= 1.0 + rb.choice((-1, 1)) * rb.uniform(0.12, 0.28) * \
            np.exp(-((t - bt) / 0.035) ** 2)
    f0 = f0 * (1 + wobble + 0.02 * jitter) * breaks
    phase = np.cumsum(f0) / rate

    # the folds: a bright saw, and period-doubling that flickers in and out
    saw = 2.0 * (phase % 1.0) - 1.0
    doubling = np.clip(np.convolve(rng.standard_normal(n), np.ones(2200) / 2200,
                                   mode='same') * 18 + 0.25, 0, 0.8)
    sub = np.sign(np.sin(2 * np.pi * phase * 0.5)) * doubling
    # roughness: fast amplitude flutter at 55-80 Hz -- the scream signature
    rough_f = 67 + 12 * np.sin(2 * np.pi * 0.9 * t)
    rough = 1.0 - 0.85 * (0.5 + 0.5 * np.sin(2 * np.pi * np.cumsum(rough_f) / rate))
    rasp = np.convolve(rng.standard_normal(n), np.ones(3) / 3, mode='same')
    src = saw + sub + rasp * 0.45

    # the throat: wide open, with a harsh presence peak on top
    scream = _formant(src, [(880, 820), (1320, 1180), (2700, 2500), (3400, 3300)],
                      [140, 190, 300, 420], rate)
    scream = np.tanh(scream * 5.0)                    # driven hard: it is straining
    # the flutter goes on AFTER the drive -- before it, the saturation flattened
    # it straight back out (measured: it had the least flutter of any version)
    scream *= rough

    # the size: a distorted growl an octave and a half down, same breaks
    g_phase = np.cumsum(f0 * 0.34) / rate
    growl_src = 2.0 * (g_phase % 1.0) - 1.0
    growl = np.tanh(_formant(growl_src, [(420, 380), (760, 700), (1900, 1800)],
                             [120, 160, 260], rate) * 6.0) * (0.4 + 0.6 * rough)

    # the wrongness: ring-modulate the scream against a low, drifting sine
    ring = scream * np.sin(2 * np.pi * np.cumsum(97 + 21 * np.sin(2 * np.pi * 0.6 * t)) / rate)

    voice = (scream * 1.0 + growl * 0.55 + ring * 0.35) * on

    # the gasp between them: a wet, catching pull of air
    gasp_env = np.exp(-((t - 1.15) / 0.06) ** 2)
    gasp = _formant(rng.standard_normal(n), [(1500, 1300), (2800, 2600), (4200, 4000)],
                    [500, 700, 900], rate)
    voice += gasp * gasp_env * 1.4

    v_env = np.ones(n)                                # levels live in `on` now
    # after the crack, the voice tears into a wet rattle
    rattle = np.convolve(rng.standard_normal(n), np.ones(24) / 24, mode='same')
    rattle *= 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 23 * t))    # clicking, gurgling
    voice = voice + rattle * 3.0 * np.clip((t - 1.95) / 0.3, 0, 1) \
        * np.exp(-np.maximum(t - 2.2, 0) * 1.4)

    # --- the fire: a broad roar that swells and then eats everything ---
    noise = rng.standard_normal(n)
    roar = np.convolve(noise, np.ones(55) / 55, mode='same') * 3.0
    hiss = np.convolve(noise, np.ones(7) / 7, mode='same') * 0.5
    fire_env = np.clip(t / 0.3, 0, 1) * np.exp(-np.maximum(t - 3.0, 0) * 1.3)

    # the roar leads; the fire sits under it, then swells as the voice gives out
    fire_env *= 0.5 + 0.5 * np.clip((t - 1.9) / 0.8, 0, 1)
    out = voice * v_env * 1.3 + (roar + hiss) * fire_env * 0.5

    # --- pops and cracks all through it, thinning out as it dies ---
    rr = np.random.default_rng(5150)
    for ct in np.sort(rr.uniform(0.05, 4.1, 90)):
        amp = rr.uniform(0.15, 0.8) * (1.0 - ct / 4.5)
        k = np.exp(-np.maximum(t - ct, 0) * rr.uniform(90, 260)) * (t >= ct)
        out += np.convolve(rng.standard_normal(n), np.ones(3) / 3, mode='same') * k * amp * 0.5

    # --- its knees hitting the floor, then the body -- in step with the fall ---
    for ct, amp in ((2.22, 0.45), (2.45, 1.0)):
        fall = 34 + 70 * np.exp(-np.maximum(t - ct, 0) * 5.0)
        thud = np.sin(2 * np.pi * np.cumsum(fall) / rate) * np.exp(-np.maximum(t - ct, 0) * 3.4)
        out += thud * (t >= ct) * 0.9 * amp

    out = np.tanh(out * 1.05) * np.clip((t[-1] - t) / 0.4, 0, 1)
    return out / max(1e-6, np.abs(out).max()) * 0.95


def _init_audio():
    """Synthesise the sounds and load them from an in-memory file system, so
    nothing is written to disk. If audio is unavailable they are just silent."""
    import numpy as np
    from panda3d.core import VirtualFileSystem, VirtualFileMountRamdisk
    try:
        rng = np.random.default_rng(SEED + 99)
        vfs = VirtualFileSystem.getGlobalPtr()
        vfs.mount(VirtualFileMountRamdisk(), '/backrooms_audio', 0)
        for name, samples in (('sting', _sound_sting(rng)),
                              ('rumble', _sound_rumble(rng)),
                              ('chase', _sound_chase(rng)),
                              ('ui_move', _sound_ui_move(rng)),
                              ('ui_select', _sound_ui_select(rng)),
                              ('step', _sound_step(rng)),
                              ('step_heavy', _sound_step_heavy(rng)),
                              ('tick', _sound_tick(rng)),
                              ('caught', _sound_caught(rng)),
                              ('burn', _sound_burn(rng))):
            path = f'/backrooms_audio/{name}.wav'
            vfs.writeFile(path, _synth_wav(samples), False)
            _sfx[name] = base.loader.loadSfx(path)     # noqa: F821 (panda3d builtin)
    except Exception as e:
        print(f'[audio] disabled: {e}')


def _play_sfx(name, volume=1.0, rate=None):
    snd = _sfx.get(name)
    if snd is not None:
        snd.setLoop(False)
        if rate is not None:
            snd.setPlayRate(rate)      # a little pitch variation per hit
        snd.setVolume(volume * _volume)
        snd.play()


def _play_loop(name, volume=1.0):
    snd = _sfx.get(name)
    if snd is not None:
        snd.setLoop(True)
        snd.setVolume(volume * _volume)
        snd.play()


def _stop_sfx(name):
    snd = _sfx.get(name)
    if snd is not None:
        snd.stop()

def _lit(ents):
    """Put props under the same lighting as the room. Entities flagged
    `unlit_fx` (glows, halos) keep their own shader; an `emit` value is applied
    after the shader, since assigning the shader resets its inputs."""
    for e in ents:
        if getattr(e, 'unlit_fx', False):
            continue
        e.shader = OBJ_SHADER
        emit = getattr(e, 'emit', 0.0)
        if emit:
            e.set_shader_input('obj_emit', emit)
    return ents


def _refresh_look_grids(force=False):
    """Re-centre the light and wall textures on the player once they have
    walked far enough that the edge could come into view."""
    global _light_origin, _light_base, _light_buf, _light_flickers, _light_dips
    global _occ_origin
    px, pz = player.x, player.z

    n = LIGHT_GRID_N
    li = round(px / LIGHT_SPACING)
    lj = round((pz - LIGHT_Z_OFFSET) / LIGHT_SPACING)
    if (force or _light_origin is None
            or abs(li - (_light_origin[0] + n // 2)) > n // 5
            or abs(lj - (_light_origin[1] + n // 2)) > n // 5):
        oi, oj = li - n // 2, lj - n // 2
        base = bytearray(n * n)
        flickers = []
        for y in range(n):
            for x in range(n):
                kind, bright = _light_kind(oi + x, oj + y)
                if kind == 'on' or kind == 'flicker':
                    base[y * n + x] = int(bright * 255)
                    if kind == 'flicker':
                        flickers.append([y * n + x, True, random.uniform(0.2, 2.0)])
        _light_origin, _light_base = (oi, oj), base
        _light_buf, _light_flickers, _light_dips = bytearray(base), flickers, {}
        _light_tex.setRamImage(bytes(_light_buf))
        scene.set_shader_input('light_origin', Vec2(oi, oj))

    n = OCC_GRID_N
    cx, cz = world_cell(px, pz)
    if (force or _occ_origin is None
            or abs(cx - (_occ_origin[0] + n // 2)) > n // 6
            or abs(cz - (_occ_origin[1] + n // 2)) > n // 6):
        ox, oz = cx - n // 2, cz - n // 2
        buf = bytearray(n * n)
        for y in range(n):
            row = y * n
            for x in range(n):
                if cell_is_wall(ox + x, oz + y):
                    buf[row + x] = 255
        _occ_tex.setRamImage(bytes(buf))
        _occ_origin = (ox, oz)
        scene.set_shader_input('occ_origin', Vec2(ox, oz))


_tick_cool = 0.0            # rate limit, so a bank of bad tubes ticks not buzzes


def _light_tick(texel):
    """A failing tube striking somewhere near you."""
    global _tick_cool
    if _tick_cool > 0 or _light_origin is None or player is None:
        return
    n = LIGHT_GRID_N
    oi, oj = _light_origin
    x = ((texel % n) + oi) * LIGHT_SPACING
    z = ((texel // n) + oj) * LIGHT_SPACING + LIGHT_Z_OFFSET
    d = math.hypot(x - player.x, z - player.z)
    if d > TICK_RANGE:
        return
    _tick_cool = random.uniform(0.10, 0.30)
    _play_sfx('tick', volume=(1.0 - d / TICK_RANGE) ** 1.5 * TICK_VOLUME,
              rate=random.uniform(0.85, 1.25))


def _update_lights():
    """Drive every troffer's brightness: failing tubes stutter, the whole
    building browns out now and then, and lights fail around the monster while
    it hunts you -- you often see the lights die before you see it."""
    global _brownout_t, _brownout_left, _power, _light_dips, _tick_cool
    if _light_buf is None:
        return
    dt = time.dt
    buf, base = _light_buf, _light_base
    _tick_cool -= dt

    for fl in _light_flickers:
        fl[2] -= dt
        if fl[2] <= 0:
            fl[1] = not fl[1]
            if fl[1]:
                fl[2] = random.uniform(0.06, 0.45)
                _light_tick(fl[0])          # you hear it come back on
            else:   # usually a stutter, sometimes a long dead spell
                fl[2] = random.uniform(0.8, 3.2) if random.random() < 0.12 \
                    else random.uniform(0.04, 0.16)
        buf[fl[0]] = base[fl[0]] if fl[1] else 0

    dips = {}
    if _entity is not None and _entity.enabled and (_entity_chasing or _diamond_phase == 'cutscene'):
        n = LIGHT_GRID_N
        oi, oj = _light_origin
        ei = round(_entity.x / LIGHT_SPACING) - oi
        ej = round((_entity.z - LIGHT_Z_OFFSET) / LIGHT_SPACING) - oj
        reach = int(ENTITY_FLICKER_RADIUS // LIGHT_SPACING) + 1
        for y in range(max(0, ej - reach), min(n, ej + reach + 1)):
            for x in range(max(0, ei - reach), min(n, ei + reach + 1)):
                k = y * n + x
                if not base[k]:
                    continue
                d = math.hypot((x + oi) * LIGHT_SPACING - _entity.x,
                               (y + oj) * LIGHT_SPACING + LIGHT_Z_OFFSET - _entity.z)
                if d >= ENTITY_FLICKER_RADIUS:
                    continue
                near = 1.0 - d / ENTITY_FLICKER_RADIUS
                val, left = _light_dips.get(k, (base[k], 0.0))
                left -= dt
                if left <= 0:        # hold each state a moment: a stutter, not a strobe
                    if random.random() < 0.25 + 0.5 * near:
                        val, left = int(base[k] * random.uniform(0.0, 0.2)), random.uniform(0.04, 0.22)
                    else:
                        val, left = base[k], random.uniform(0.05, 0.5) * (1.2 - near)
                dips[k] = (val, left)
                buf[k] = val
    for k in _light_dips:
        if k not in dips:
            buf[k] = base[k]
    _light_dips = dips

    _brownout_t -= dt
    if _brownout_left > 0:
        _brownout_left -= dt
        target = 0.30 + 0.30 * math.sin(_look_time * 41.0) ** 2   # sagging, buzzing
    else:
        target = 1.0
        if _brownout_t <= 0:
            _brownout_left = random.uniform(0.25, 1.2)
            lo, hi = BROWNOUT_EVERY
            if _entity_chasing:
                lo, hi = lo * 0.3, hi * 0.3
            _brownout_t = random.uniform(lo, hi)
    _power += (target - _power) * min(1.0, dt * 18.0)
    scene.set_shader_input('u_power', _power)
    _light_tex.setRamImage(bytes(buf))


def _update_atmosphere():
    """Clock for the shaders, and how corrupted the screen should look. During a
    chase the picture is constantly torn and fringed, with random spikes and the
    occasional split-second blackout; the rest of the time it settles."""
    global _look_time, _fear, _glitch, _glitch_spike, _black, _black_t
    dt = time.dt
    _look_time += dt

    target = 0.0
    if _entity is not None and _entity.enabled and _entity_chasing and player is not None:
        target = max(0.0, 1.0 - distance_xz(_entity, player) / 45.0)
        if target > 0 and _entity_in_sight():
            target = min(1.0, target + 0.25)
    elif _diamond_phase == 'cutscene' and _cutscene is not None:
        target = _cutscene.get('fear', 0.0)
    _fear += (target - _fear) * min(1.0, dt * 2.5)

    if _entity_chasing and not _paused:
        _glitch_spike = max(0.0, _glitch_spike - dt * 1.6)
        if random.random() < dt * 0.9:                 # random corruption bursts
            _glitch_spike = max(_glitch_spike, random.uniform(0.3, 0.65))
        _black_t -= dt
        if _black_t <= 0:                              # a split-second blackout
            _black_t = random.uniform(1.8, 5.0)
            _black = 1.0
        _glitch = min(1.0, 0.16 + 0.24 * _fear + _glitch_spike)
    else:
        _glitch_spike = max(0.0, _glitch_spike - dt * 2.0)
        _glitch = max(_glitch_spike, max(0.0, _glitch - dt * 3.0))
    _black = max(0.0, _black - dt * 7.0)

    scene.set_shader_input('u_time', _look_time)
    camera.set_shader_input('u_time', _look_time)
    fx = 1.0 if _fx_on else 0.0          # SETTINGS > SCREEN EFFECTS
    camera.set_shader_input('u_fear', _fear * fx)
    camera.set_shader_input('u_glitch', _glitch * fx)
    camera.set_shader_input('u_black', _black * fx)

    chase = _sfx.get('chase')               # the music leans in as it closes
    if chase is not None and _entity_chasing:
        chase.setVolume((0.6 + 0.4 * _fear) * _volume)


def _add_box(verts, tris, norms, uvs, cx, cy, cz, sx, sy, sz, mat):
    """Append one cuboid to a mesh buffer.

    Each face gets its OWN four vertices so it can carry its own normal. The
    material id rides in the u coordinate: the world shader maps textures from
    world position, so the real UVs would go unused anyway.
    """
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    z0, z1 = cz - sz / 2, cz + sz / 2
    faces = (
        (((x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)), (0, 1, 0)),
        (((x0, y0, z1), (x1, y0, z1), (x1, y0, z0), (x0, y0, z0)), (0, -1, 0)),
        (((x1, y0, z0), (x0, y0, z0), (x0, y1, z0), (x1, y1, z0)), (0, 0, -1)),
        (((x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)), (0, 0, 1)),
        (((x0, y0, z0), (x0, y0, z1), (x0, y1, z1), (x0, y1, z0)), (-1, 0, 0)),
        (((x1, y0, z1), (x1, y0, z0), (x1, y1, z0), (x1, y1, z1)), (1, 0, 0)),
    )
    uv = Vec2(mat, 0)
    for corners, n in faces:
        base = len(verts)
        verts.extend(Vec3(*p) for p in corners)
        nv = Vec3(*n)
        norms.extend((nv, nv, nv, nv))
        uvs.extend((uv, uv, uv, uv))
        tris.append((base, base + 1, base + 2))
        tris.append((base, base + 2, base + 3))


def build_chunk(chx, chz):
    """Build a chunk as ONE batched mesh plus a few dynamic entities.

    The room shell -- floor, ceiling, every wall and every steady light -- is
    baked into a single Mesh with per-vertex colours, so a whole chunk costs one
    draw call instead of ~15. Rendering here is CPU/draw-call bound, so this is
    what makes a long view distance affordable. Only things that must change on
    their own stay as separate entities: flickering lights and props.

    Collision is NOT part of this mesh; invisible box colliders are created for
    the few chunks next to the player by _update_collision.
    """
    pc = _last_chunk or (0, 0)
    with_props = (abs(chx - pc[0]) <= PROP_CHUNK_RADIUS
                  and abs(chz - pc[1]) <= PROP_CHUNK_RADIUS)
    _chunk_has_props[(chx, chz)] = with_props
    ents = []
    pickups = []
    cx0, cz0 = chx * CHUNK, chz * CHUNK
    center_x = (cx0 + (CHUNK - 1) / 2) * CELL
    center_z = (cz0 + (CHUNK - 1) / 2) * CELL
    rng = random.Random((chx * 40503) ^ (chz * 104729) ^ SEED)  # stable per chunk

    verts, tris, norms, uvs = [], [], [], []
    _add_box(verts, tris, norms, uvs, center_x, -0.5, center_z,
             CHUNK_UNITS, 1, CHUNK_UNITS, MAT_FLOOR)
    _add_box(verts, tris, norms, uvs, center_x, WALL_H + 0.5, center_z,
             CHUNK_UNITS, 1, CHUNK_UNITS, MAT_CEILING)

    # Troffers sit on the fixed light grid (LIGHT_EVERY cells apart) so the
    # shaders can find them. Dead and flickering ones are still built -- how
    # bright each panel glows is read from the light texture, per pixel.
    for li in range(-(-cx0 // LIGHT_EVERY), (cx0 + CHUNK - 1) // LIGHT_EVERY + 1):
        for lj in range(-(-cz0 // LIGHT_EVERY), (cz0 + CHUNK - 1) // LIGHT_EVERY + 1):
            if _light_kind(li, lj)[0] is not None:
                _add_box(verts, tris, norms, uvs, li * LIGHT_SPACING, WALL_H - 0.06,
                         lj * LIGHT_SPACING + LIGHT_Z_OFFSET, 2.4, 0.12, 1.2, MAT_LIGHT)

    for i in range(CHUNK):
        for j in range(CHUNK):
            cx, cz = cx0 + i, cz0 + j
            if cell_is_wall(cx, cz):
                _add_box(verts, tris, norms, uvs, cx * CELL, WALL_H / 2, cz * CELL,
                         CELL, WALL_H, CELL, MAT_WALL)
            elif with_props:
                prop_ents, pickup = spawn_prop(cx, cz)
                ents.extend(_lit(prop_ents))
                if pickup:
                    pickup['chunk'] = (chx, chz)
                    pickups.append(pickup)
    if with_props:                       # whatever you put down here, where you left it
        for item in _dropped:
            if world_to_chunk(item['x'], item['z']) == (chx, chz):
                d_ents, d_pickup = _build_dropped(item, (chx, chz))
                ents.extend(d_ents)
                pickups.append(d_pickup)

    shell = Entity(model=Mesh(vertices=verts, triangles=tris, normals=norms,
                              uvs=uvs, mode='triangle'),
                   shader=WORLD_SHADER,
                   double_sided=True)   # winding varies per face; never cull
    ents.insert(0, shell)

    chunks[(chx, chz)] = ents
    _chunk_pickups[(chx, chz)] = pickups


def _build_chunk_colliders(chx, chz):
    """Invisible boxes matching this chunk's floor and walls. Only made for the
    handful of chunks beside the player, and thrown away when you leave."""
    ents = []
    cx0, cz0 = chx * CHUNK, chz * CHUNK
    center_x = (cx0 + (CHUNK - 1) / 2) * CELL
    center_z = (cz0 + (CHUNK - 1) / 2) * CELL
    ents.append(Entity(model='cube', collider='box', visible=False,
                       position=(center_x, -0.5, center_z),
                       scale=(CHUNK_UNITS, 1, CHUNK_UNITS)))
    for i in range(CHUNK):
        for j in range(CHUNK):
            cx, cz = cx0 + i, cz0 + j
            if cell_is_wall(cx, cz):
                ents.append(Entity(model='cube', collider='box', visible=False,
                                   position=(cx * CELL, WALL_H / 2, cz * CELL),
                                   scale=(CELL, WALL_H, CELL)))
    return ents


def destroy_chunk(key):
    for e in chunks.pop(key, []):
        destroy(e)
    _chunk_has_props.pop(key, None)
    _chunk_pickups.pop(key, None)


def _update_collision(pcx, pcz):
    """Keep invisible colliders only for the chunks right around the player."""
    near = chunks_in_radius(pcx, pcz, COLLISION_RADIUS)
    for key in [k for k in _chunk_colliders if k not in near]:
        for e in _chunk_colliders.pop(key):
            destroy(e)
    for key in near:
        if key not in _chunk_colliders:
            _chunk_colliders[key] = _build_chunk_colliders(*key)


_build_queue = []       # chunk keys waiting to be built, kept sorted nearest-first
_chunk_has_props = {}   # (chx,chz) -> was this chunk built with props?
_chunk_pickups = {}     # (chx,chz) -> carryable items in that chunk
_chunk_colliders = {}   # (chx,chz) -> invisible collision boxes
_last_chunk = None      # player's chunk last time streaming actually ran


def refresh_chunks(px, pz):
    """Unload chunks out of range and (re)queue the ones still missing. Building
    is spread across frames by _process_build_queue so a big streaming ring never
    stalls a single frame -- that stall is what felt like an invisible wall. The
    immediate 3x3 around the player is always built at once so you never stand on
    a hole or walk into a wall that hasn't appeared yet."""
    global _last_chunk
    pcx, pcz = world_to_chunk(px, pz)
    # Collision must be refreshed EVERY frame, before the early-out below. A
    # chunk finished by the build queue on the same frame the queue drained
    # would otherwise keep its colliders stashed until you next changed chunk --
    # leaving a hole you can sink into, which reads as an invisible wall along
    # the chunk border. _update_collision itself early-outs when nothing moved.
    _update_collision(pcx, pcz)
    _upgrade_prop_chunks(pcx, pcz)
    # Nothing else can have changed unless you crossed into a new chunk or there
    # is still queued work, so skip rebuilding the needed-set every frame.
    if (pcx, pcz) == _last_chunk and not _build_queue:
        return
    _last_chunk = (pcx, pcz)
    needed = chunks_in_radius(pcx, pcz, VIEW_RADIUS)

    for key in set(chunks) - needed:
        destroy_chunk(key)

    pending = ((set(_build_queue) | (needed - set(chunks)))) & needed
    _build_queue[:] = sorted(
        pending, key=lambda k: (k[0] - pcx) ** 2 + (k[1] - pcz) ** 2)

    # build anything right next to the player this instant (the 3x3 of chunks)
    while _build_queue and abs(_build_queue[0][0] - pcx) <= 1 \
            and abs(_build_queue[0][1] - pcz) <= 1:
        build_chunk(*_build_queue.pop(0))

    _update_collision(pcx, pcz)


def _upgrade_prop_chunks(pcx, pcz):
    """Rebuild one nearby chunk per frame that was built without props, so
    clutter appears well before you can make it out through the fog."""
    if _build_queue:
        return                       # don't compete with streaming
    for key in chunks_in_radius(pcx, pcz, PROP_CHUNK_RADIUS):
        if key in chunks and not _chunk_has_props.get(key, False):
            destroy_chunk(key)
            build_chunk(*key)
            return                   # at most one rebuild per frame


def _process_build_queue():
    """Build up to CHUNKS_PER_FRAME queued chunks (the far part of the ring)."""
    for _ in range(CHUNKS_PER_FRAME):
        if not _build_queue:
            return
        key = _build_queue.pop(0)
        if key not in chunks:
            build_chunk(*key)


# ---- Diamond: a modelled, rigged 3D creature --------------------------------
# Built from lofted meshes at startup: an emaciated, too-tall thing with a
# ribcage you can count, arms that hang past its knees into long hooked
# fingers, and an elongated diamond-shaped skull split by a vertical mouth.
# Lit by the same troffers as everything else, so it is a pale shape in the
# light pools and nearly invisible between them.

_SKIN = (0.035, 0.035, 0.04)  # near-black hide
_MOUTH = (0.015, 0.004, 0.004)
_SOCKET = (0.035, 0.03, 0.03)
_CLAW = (0.015, 0.015, 0.02)
_TOOTH = (0.52, 0.49, 0.40)   # bone in shadow, just visible against the black

THIGH_LEN, SHIN_LEN, ANKLE_H = 1.56, 1.46, 0.11
UPPER_ARM_LEN, FOREARM_LEN = 1.42, 1.32


def _v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _v_scale(a, s):
    return (a[0] * s, a[1] * s, a[2] * s)


def _v_dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _v_cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _v_norm(a, fallback=(0.0, 1.0, 0.0)):
    ln = math.sqrt(_v_dot(a, a))
    return _v_scale(a, 1.0 / ln) if ln > 1e-9 else fallback


def _loft(rings, colour=None, cap_start=None, cap_end=None):
    """Skin a grid of rings into a smooth closed surface (raw mesh data).

    rings[i] is a list of points going around the shape, the same count for
    every ring; consecutive rings are stitched into quads. Normals come from
    finite differences across the grid, so any shape -- a limb, a ribcage, a
    skull with a mouth pushed into it -- shades smoothly with no special cases.
    colour(i, j) -> rgb. cap_start / cap_end: a point that closes that end with
    a fan (a rounded tip when it sits a little beyond the ring).
    """
    n, S = len(rings), len(rings[0])
    centres = [_v_scale(tuple(sum(p[k] for p in ring) for k in range(3)), 1.0 / S)
               for ring in rings]
    verts, norms, uvs, cols, tris = [], [], [], [], []
    for i, ring in enumerate(rings):
        up = _v_norm(_v_sub(centres[min(i + 1, n - 1)], centres[max(i - 1, 0)]))
        for j in range(S + 1):                       # j == S repeats j == 0 for the UV seam
            jj = j % S
            p = ring[jj]
            du = _v_sub(ring[(jj + 1) % S], ring[(jj - 1) % S])
            dv = _v_sub(rings[min(i + 1, n - 1)][jj], rings[max(i - 1, 0)][jj])
            nrm = _v_cross(du, dv)
            if _v_dot(nrm, _v_sub(p, centres[i])) < 0:
                nrm = _v_scale(nrm, -1)
            if _v_dot(nrm, nrm) < 1e-14:            # collapsed ring (a pointed tip)
                nrm = up if i > n // 2 else _v_scale(up, -1)
            verts.append(p)
            norms.append(_v_norm(nrm))
            uvs.append((j / S, i / max(1, n - 1)))
            cols.append(colour(i, jj) if colour else _SKIN)
    for i in range(n - 1):
        for j in range(S):
            a = i * (S + 1) + j
            b, c = a + 1, a + S + 1
            tris.append((a, c, b))
            tris.append((b, c, c + 1))
    for cap, ring_i in ((cap_start, 0), (cap_end, n - 1)):
        if cap is None:
            continue
        ci = len(verts)
        verts.append(cap)
        norms.append(_v_norm(_v_sub(cap, centres[ring_i])))
        uvs.append((0.5, float(ring_i > 0)))
        cols.append(cols[ring_i * (S + 1)])
        for j in range(S):
            a = ring_i * (S + 1) + j
            tris.append((ci, a, a + 1))
    return verts, norms, uvs, cols, tris


def _tube(path, radii, sides=10, colour=None, shape=None, caps=(True, True)):
    """Loft along a centreline. radii[i] is a float or (across, depth).
    shape(i, direction) -> radial multiplier, for ribs, knuckles and tendons."""
    n = len(path)
    rings, prev = [], None
    for i, p in enumerate(path):
        t = _v_norm(_v_sub(path[min(i + 1, n - 1)], path[max(i - 1, 0)]))
        if prev is None:     # parallel transport, so the tube never twists
            ref = (1.0, 0.0, 0.0) if abs(t[0]) < 0.9 else (0.0, 0.0, 1.0)
            nrm = _v_norm(_v_sub(ref, _v_scale(t, _v_dot(ref, t))))
        else:
            nrm = _v_norm(_v_sub(prev, _v_scale(t, _v_dot(prev, t))), prev)
        prev = nrm
        bn = _v_cross(t, nrm)
        r = radii[i]
        rw, rd = (r, r) if isinstance(r, (int, float)) else r
        ring = []
        for j in range(sides):
            a = math.tau * j / sides
            d = _v_add(_v_scale(nrm, math.cos(a)), _v_scale(bn, math.sin(a)))
            m = shape(i, d) if shape else 1.0
            off = _v_add(_v_scale(nrm, math.cos(a) * rw * m), _v_scale(bn, math.sin(a) * rd * m))
            ring.append(_v_add(p, off))
        rings.append(ring)

    def tip(k, sign):
        t = _v_norm(_v_sub(path[k], path[k - sign]))
        r = radii[k]
        r = r if isinstance(r, (int, float)) else max(r)
        return _v_add(path[k], _v_scale(t, r * 0.65))

    return _loft(rings, colour,
                 tip(0, -1) if caps[0] else None,
                 tip(n - 1, 1) if caps[1] else None)


def _merge(*parts):
    verts, norms, uvs, cols, tris = [], [], [], [], []
    for v, nr, uv, c, tr in parts:
        base = len(verts)
        verts += v
        norms += nr
        uvs += uv
        cols += c
        tris += [(a + base, b + base, d + base) for a, b, d in tr]
    return verts, norms, uvs, cols, tris


def _to_mesh(data):
    v, nr, uv, c, tr = data
    return Mesh(vertices=[Vec3(*p) for p in v], normals=[Vec3(*p) for p in nr],
                uvs=[Vec2(*p) for p in uv], colors=[color.rgba(r, g, b, 1) for r, g, b in c],
                triangles=tr, mode='triangle')


def _lerp_profile(keys, t):
    """Piecewise-linear lookup in [(t, value)] keyframes."""
    for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
        if t <= t1:
            f = (t - t0) / max(t1 - t0, 1e-9)
            if isinstance(v0, tuple):
                return tuple(a + (b - a) * f for a, b in zip(v0, v1))
            return v0 + (v1 - v0) * f
    return keys[-1][1]


def _mesh_torso():
    """Waist to shoulders, with a concave belly, a ribcage, a knuckled spine
    and shoulder blades standing out of the back."""
    H = 2.18
    steps = 22
    width = [(0.0, (0.22, 0.16)), (0.35, (0.20, 0.13)), (0.85, (0.30, 0.22)),
             (1.40, (0.38, 0.27)), (1.80, (0.42, 0.26)), (2.02, (0.38, 0.21)),
             (H, (0.17, 0.13))]
    path = [(0.0, H * i / steps, -0.05 * math.sin(math.pi * i / steps)) for i in range(steps + 1)]
    radii = [_lerp_profile(width, p[1]) for p in path]

    def shape(i, d):
        y = path[i][1]
        m = 1.0
        if y < 0.8 and d[2] > 0.5:                              # hollow belly
            m *= 0.84
        if 0.85 < y < 1.95 and d[2] > -0.2:                     # ribs
            m *= 1.0 + 0.13 * max(0.0, math.sin((y - 0.85) / 1.1 * math.pi * 5.5)) ** 3
        if d[2] < -0.88:                                        # vertebrae
            m *= 1.0 + 0.16 * max(0.0, math.sin(y * math.pi * 9.0)) ** 2
        if 1.45 < y < 2.0 and d[2] < -0.4 and 0.35 < abs(d[0]) < 0.85:
            m *= 1.10                                           # shoulder blades
        if 1.92 < y < 2.12 and d[2] > 0.2:
            m *= 1.12                                           # collarbones
        return m

    return _tube(path, radii, sides=24, shape=shape)


def _mesh_pelvis():
    ys = (-0.36, -0.28, -0.16, -0.02, 0.10)
    widths = ((0.12, 0.09), (0.23, 0.15), (0.28, 0.17), (0.26, 0.17), (0.20, 0.15))
    return _tube([(0.0, y, 0.0) for y in ys], list(widths), sides=16)


def _mesh_neck():
    path = [(0.0, 0.0, 0.0), (0.0, 0.4, 0.02), (0.0, 0.8, 0.06)]

    def shape(i, d):     # tendons standing out down the front
        return 1.0 + 0.22 * max(0.0, math.cos((math.atan2(d[0], d[2]) - 0.45) * 3)) ** 8 \
            if d[2] > 0 else 1.0

    return _tube(path, [0.115, 0.095, 0.10], sides=12, shape=shape)


def _head_ring_point(t, a):
    """Surface of the diamond skull at height fraction t and angle a."""
    w = 0.41 * math.sin(math.pi * t ** 0.78) ** 0.85
    dp = w * 0.80
    c, s = math.cos(a), math.sin(a)
    p = 1.25                                   # superellipse: 1 = sharp diamond, 2 = round
    k = (abs(c) ** p + abs(s) ** p) ** (-1.0 / p)
    return c * k * w, s * k * dp, w


def _mesh_head():
    """An elongated diamond of a skull, chin to crown, with deep eye sockets and
    a vertical mouth split down the front, lined with teeth."""
    n, S, H = 24, 28, 1.46
    rings, tags = [], []
    for i in range(n):
        t = i / (n - 1)
        y = t * H - 0.22
        zc = 0.12 * t - 0.02
        ring, tag = [], []
        for j in range(S):
            a = math.tau * j / S
            x, z, w = _head_ring_point(t, a)
            c, s = math.cos(a), math.sin(a)
            kind = None
            if 0.03 < t < 0.47 and s > 0 and abs(c) < 0.46:     # the mouth
                z *= 0.14
                kind = 'mouth'
            elif 0.48 < t < 0.66 and s > 0.25 and 0.38 < abs(c) < 0.88:
                z *= 0.46                                       # sunken eye sockets
                kind = 'socket'
            ring.append((x, y, z + zc))
            tag.append(kind)
        rings.append(ring)
        tags.append(tag)

    def colour(i, j):
        kind = tags[i][j]
        return _MOUTH if kind == 'mouth' else _SOCKET if kind == 'socket' else _SKIN

    head = _loft(rings, colour)

    teeth = []
    for side in (-1, 1):
        for k in range(8):
            t = 0.06 + 0.38 * k / 7
            y = t * H - 0.22
            zc = 0.12 * t - 0.02
            x, z, w = _head_ring_point(t, math.pi / 2 - side * 0.42)
            root = (x * 0.98, y, z * 0.80 + zc)
            ln = 0.09 + 0.055 * math.sin(k * 2.3) ** 2
            tip = (root[0] * 0.15, y - ln * 0.5, root[2] + 0.015)
            teeth.append(_tube([root, _v_add(_v_scale(root, 0.5), _v_scale(tip, 0.5)), tip],
                               [0.022, 0.013, 0.001], sides=5,
                               colour=lambda i, j: _TOOTH, caps=(False, False)))
    return _merge(head, *teeth)


def _eye_positions():
    t = 0.57
    y = t * 1.46 - 0.22
    zc = 0.12 * t - 0.02
    out = []
    for side in (-1, 1):
        x, z, w = _head_ring_point(t, math.pi / 2 - side * 0.62)
        out.append((x, y, z * 0.70 + zc))
    return out


def _knob(r):
    """A bony lump for a joint: a squashed ball of bone under the skin."""
    ys = [r * k for k in (-0.9, -0.6, -0.2, 0.2, 0.6, 0.9)]
    rad = [r * k for k in (0.35, 0.8, 1.0, 1.0, 0.8, 0.35)]
    return _tube([(0.0, y, r * 0.15) for y in ys], rad, sides=10)


def _limb(length, radii_keys, sides=10, shape=None):
    steps = 9
    path = [(0.0, -length * i / steps, 0.0) for i in range(steps + 1)]
    radii = [_lerp_profile(radii_keys, i / steps) for i in range(steps + 1)]
    return _tube(path, radii, sides=sides, shape=shape)


def _finger(start, direction, lengths, radii, curl):
    """A jointed finger curling toward the palm, ending in a hooked claw."""
    pts = [start]
    ang = 0.0
    dy, dz = direction
    for seg_i, ln in enumerate(lengths):
        ang += curl[seg_i]
        cy = dy * math.cos(ang) - dz * math.sin(ang)
        cz = dy * math.sin(ang) + dz * math.cos(ang)
        last = pts[-1]
        pts.append((last[0], last[1] + cy * ln, last[2] + cz * ln))
    claw_from = len(pts) - 2
    return _tube(pts, radii, sides=6,
                 colour=lambda i, j: _CLAW if i >= claw_from else _SKIN)


def _mesh_hand(side):
    palm = _tube([(0.0, 0.0, 0.0), (0.0, -0.17, 0.02), (0.0, -0.34, 0.025)],
                 [(0.10, 0.045), (0.13, 0.040), (0.115, 0.034)], sides=10)
    fingers = []
    for k, fx in enumerate((-0.085, -0.029, 0.029, 0.085)):
        long_ = 1.0 - 0.12 * abs(k - 1.5)
        fingers.append(_finger((fx, -0.33, 0.025), (-1.0, 0.0),
                               [0.34 * long_, 0.29 * long_, 0.22 * long_, 0.19],
                               [0.032, 0.029, 0.024, 0.018, 0.002],
                               [0.10, 0.22, 0.30, 0.45]))
    thumb = _finger((side * -0.095, -0.10, 0.045), (-0.55, 0.83),
                    [0.21, 0.17, 0.13], [0.031, 0.026, 0.018, 0.002], [0.2, 0.3, 0.4])
    return _merge(palm, thumb, *fingers)


def _mesh_foot():
    sole = _tube([(0.0, 0.0, -0.06), (0.0, -0.05, 0.18), (0.0, -0.075, 0.44)],
                 [(0.055, 0.05), (0.075, 0.035), (0.07, 0.02)], sides=10)
    toes = []
    for tx in (-0.045, 0.0, 0.045):
        toes.append(_tube([(tx, -0.075, 0.43), (tx * 1.2, -0.08, 0.55), (tx * 1.3, -0.09, 0.64)],
                          [0.018, 0.013, 0.002], sides=6,
                          colour=lambda i, j: _CLAW if i >= 1 else _SKIN))
    return _merge(sole, *toes)


def _build_entity_model(root):
    """Assemble the rig: pivots for every joint, a lit mesh on each bone."""
    skin = _look_textures.get('skin')
    parts = []

    def part(parent, data):
        e = Entity(parent=parent, model=_to_mesh(data), texture=skin,
                   shader=OBJ_SHADER, double_sided=True)
        e.set_shader_input('obj_bump', 0.012)   # dry skin, not marbled stone
        e.set_shader_input('obj_spec', 0.02)   # matte: no gloss on it at all
        e.set_shader_input('obj_rim', 0.95)
        parts.append(e)
        return e

    def pivot(parent, pos=(0, 0, 0)):
        return Entity(parent=parent, position=pos)

    body = pivot(root, (0, THIGH_LEN + SHIN_LEN + ANKLE_H, 0))
    part(body, _mesh_pelvis())
    spine = pivot(body, (0, 0.02, 0))
    part(spine, _mesh_torso())
    chest = pivot(spine, (0, 1.70, -0.04))
    neck = pivot(chest, (0, 0.46, 0.05))
    part(neck, _mesh_neck())
    head = pivot(neck, (0, 0.78, 0.07))
    part(head, _mesh_head())
    eyes = []
    for pos in _eye_positions():
        eye = Entity(parent=head, model='sphere', position=pos, scale=0.036,
                     color=col255(255, 247, 220), shader=OBJ_SHADER)
        eye.set_shader_input('obj_emit', 4.5)   # hot pinpricks deep in the sockets
        eyes.append(eye)

    rig = {'body': body, 'spine': spine, 'chest': chest, 'neck': neck, 'head': head,
           'arms': {}, 'legs': {}}

    def sinew(i, d):
        # cords of muscle down the front and back, so limbs are not smooth pipes
        return 1.0 + 0.10 * abs(d[2]) ** 6 * math.sin(i / 9 * math.pi) ** 0.5
    for side in (-1, 1):
        shoulder = pivot(chest, (side * 0.48, 0.36, 0.06))
        part(shoulder, _limb(UPPER_ARM_LEN, [(0.0, 0.16), (0.22, 0.135), (0.78, 0.092), (1.0, 0.104)], shape=sinew))
        elbow = pivot(shoulder, (0, -UPPER_ARM_LEN, 0))
        part(elbow, _knob(0.125))
        part(elbow, _limb(FOREARM_LEN, [(0.0, 0.104), (0.25, 0.098), (1.0, 0.062)], shape=sinew))
        wrist = pivot(elbow, (0, -FOREARM_LEN, 0))
        part(wrist, _mesh_hand(side))
        rig['arms'][side] = {'shoulder': shoulder, 'elbow': elbow, 'wrist': wrist}

        hip = pivot(body, (side * 0.20, -0.16, 0))

        def calf(i, d):
            return 1.0 + (0.28 * math.sin(i / 9 * math.pi) if d[2] < -0.3 and i < 6 else 0.0)

        part(hip, _limb(THIGH_LEN, [(0.0, 0.19), (0.4, 0.15), (0.92, 0.11), (1.0, 0.122)], shape=sinew))
        knee = pivot(hip, (0, -THIGH_LEN, 0))
        part(knee, _knob(0.15))
        part(knee, _limb(SHIN_LEN, [(0.0, 0.122), (0.3, 0.104), (1.0, 0.066)], shape=calf))
        ankle = pivot(knee, (0, -SHIN_LEN, 0))
        part(ankle, _mesh_foot())
        rig['legs'][side] = {'hip': hip, 'knee': knee, 'ankle': ankle}

    root._rig = rig
    root._parts = parts
    root._eyes = eyes
    root._anim = {'phase': 0.0, 'move': 0.0, 'chase': 0.0, 'lx': None, 'lz': None,
                  'twitch_t': 1.0, 'head': [0.0, 0.0, 0.0], 'goal': (0.0, 0.0, 0.0)}


def _face_entity_towards(x, z, snap=False):
    """Turn the creature to look at a world point (instantly if snap)."""
    e = _entity
    if e is None:
        return
    target = math.degrees(math.atan2(x - e.x, z - e.z))
    if snap:
        e.rotation_y = target
        e._anim['lx'], e._anim['lz'] = e.x, e.z
    return target


def _animate_entity(moved):
    """Drive the rig: a long, loping walk that turns into a hunched lunge while
    hunting; when still it stands swaying, head snapping to new angles."""
    e = _entity
    rig = getattr(e, '_rig', None) if e is not None else None
    if rig is None:
        return
    st = e._anim
    dt = time.dt

    if st['lx'] is None:
        st['lx'], st['lz'] = e.x, e.z
    step_x, step_z = e.x - st['lx'], e.z - st['lz']
    st['lx'], st['lz'] = e.x, e.z
    if moved > 1e-4 and 1e-8 < step_x * step_x + step_z * step_z < 4.0:
        target = math.degrees(math.atan2(step_x, step_z))
    else:
        target = _face_entity_towards(camera.world_position.x, camera.world_position.z)
    diff = (target - e.rotation_y + 180.0) % 360.0 - 180.0
    turn = 260.0 * dt
    e.rotation_y += max(-turn, min(turn, diff))

    st['move'] += ((1.0 if moved > 1e-4 else 0.0) - st['move']) * min(1.0, dt * 6.0)
    st['chase'] += ((1.0 if _entity_chasing else 0.0) - st['chase']) * min(1.0, dt * 2.0)
    st['phase'] += moved * ENTITY_STEP_RATE * 0.42
    ph, mv, ch = st['phase'], st['move'], st['chase']
    swing = ENTITY_LIMB_SWING * (0.8 + 0.35 * ch)
    breathe = math.sin(_look_time * 1.3)

    rig['spine'].rotation_x = 20 + 12 * ch + 1.6 * breathe
    rig['chest'].rotation_x = 17 + 6 * ch + 1.0 * breathe
    rig['chest'].rotation_y = math.sin(ph) * 7 * mv
    rig['body'].rotation_z = math.sin(ph) * 3.5 * mv
    lean = rig['spine'].rotation_x + rig['chest'].rotation_x

    drops = []
    for side, off in ((-1, 0.0), (1, math.pi)):
        s, c = math.sin(ph + off), math.cos(ph + off)
        leg = rig['legs'][side]
        thigh = -s * swing * mv - 4
        knee = 10 + 50 * max(0.0, c) * mv
        leg['hip'].rotation_x = thigh
        leg['knee'].rotation_x = knee
        leg['ankle'].rotation_x = -(thigh + knee) * 0.75
        drops.append(THIGH_LEN * math.cos(math.radians(thigh))
                     + SHIN_LEN * math.cos(math.radians(thigh + knee)))

        arm = rig['arms'][side]
        arm['shoulder'].rotation_x = (-lean * 0.82 + s * swing * (0.5 - 0.3 * ch) * mv
                                      - 10 - 22 * ch + side * 5)
        arm['shoulder'].rotation_z = -side * (9 + 4 * ch) + 1.5 * breathe * side
        arm['elbow'].rotation_x = -(28 + 18 * max(0.0, -s) * mv + 14 * ch)
        arm['wrist'].rotation_x = -8 - 22 * ch
    rig['body'].y = max(drops) + ANKLE_H + 0.12 * abs(math.sin(ph)) * mv

    # the head: a slow drift punctuated by sudden, unnatural snaps
    st['twitch_t'] -= dt
    if st['twitch_t'] <= 0:
        calm = ch < 0.5
        st['twitch_t'] = random.uniform(1.4, 4.5) if calm else random.uniform(0.3, 1.1)
        st['goal'] = (random.uniform(-12, 10), random.uniform(-34, 34), random.uniform(-38, 38))
    rate = min(1.0, dt * 16.0)
    for k in range(3):
        st['head'][k] += (st['goal'][k] - st['head'][k]) * rate
    tremor = math.sin(_look_time * 31.0) * 0.9 + math.sin(_look_time * 17.0) * 0.6
    rig['neck'].rotation_x = 40 + 8 * ch
    rig['head'].rotation_x = -(lean + rig['neck'].rotation_x) + 12 + st['head'][0]
    rig['head'].rotation_y = st['head'][1]
    rig['head'].rotation_z = 11 + st['head'][2] + tremor


def _entity_place(near_x, near_z):
    """Drop the entity in an open cell somewhere around the given point."""
    rr = random.Random()
    for _ in range(60):
        ang = rr.uniform(0, math.tau)
        dist = rr.uniform(ENTITY_SPAWN_MIN, ENTITY_SPAWN_MAX)
        x = near_x + math.cos(ang) * dist
        z = near_z + math.sin(ang) * dist
        if not cell_is_wall(round(x / CELL), round(z / CELL)):
            _entity.position = (x, 0, z)
            return True
    return False


def build_entity():
    """Create the horror entity, idle and waiting to be noticed."""
    global _entity
    _entity = Entity()
    _build_entity_model(_entity)
    _entity.scale = ENTITY_SCALE       # it looms over you
    _entity_place(0, 0)
    _entity.enabled = False      # stays hidden until the intro spawns it


def _entity_place_behind():
    """Put Diamond somewhere around the player that is in plain sight but NOT
    in front of them, so they have to turn around to find it."""
    fwd = Vec3(camera.forward[0], 0, camera.forward[2])
    if fwd.length() < 1e-6:
        fwd = Vec3(0, 0, 1)
    fwd = fwd.normalized()
    for _ in range(400):
        ang = random.uniform(0, math.tau)
        d = random.uniform(INTRO_SPAWN_MIN, INTRO_SPAWN_MAX)
        x = player.x + math.cos(ang) * d
        z = player.z + math.sin(ang) * d
        to = Vec3(x - player.x, 0, z - player.z)
        if to.length() < 1e-6:
            continue
        if to.normalized().dot(fwd) > INTRO_BEHIND_DOT:
            continue                       # too close to where they're looking
        if _entity_body_blocked(x, z):
            continue
        if not has_line_of_sight(player.x, player.z, x, z,
                                 radius=ENTITY_RADIUS, step_len=0.35):
            continue                       # must be able to actually see it
        _entity.position = (x, 0, z)
        return True
    return False


def _smoother(x):
    """Ken Perlin's smootherstep on a value already in 0..1."""
    x = max(0.0, min(1.0, x))
    return x * x * x * (x * (x * 6 - 15) + 10)


def _lerp(a, b, t):
    return a + (b - a) * t


def _get_puff_texture():
    """A soft round dust puff: opaque-ish centre fading to nothing at the rim."""
    global _puff_tex
    if _puff_tex is not None:
        return _puff_tex
    from PIL import Image
    n = 96
    img = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    px = img.load()
    c = (n - 1) / 2
    for y in range(n):
        for x in range(n):
            d = math.hypot(x - c, y - c) / c
            if d < 1.0:
                a = int(200 * (1.0 - d) ** 1.6)
                px[x, y] = (150, 140, 120, a)
    _puff_tex = Texture(img)
    return _puff_tex


def _diamond_spawn_site():
    """A spot on open floor ahead of the player, in plain view, with room around
    it for the hole -- where the ground will break open. Falls back to closer or
    off-angle spots."""
    fwd = Vec3(camera.forward.x, 0, camera.forward.z)
    if fwd.length() < 1e-4:
        fwd = Vec3(0, 0, 1)
    fwd = fwd.normalized()

    def blocks_view(x, z):
        """Would the spawn-room desk sit on or in front of the hole?"""
        dx, dz = SPAWN_DESK_POS
        vx, vz = x - player.x, z - player.z
        L2 = vx * vx + vz * vz or 1.0
        k = max(0.0, min(1.0, ((dx - player.x) * vx + (dz - player.z) * vz) / L2))
        return math.hypot(player.x + vx * k - dx, player.z + vz * k - dz) < 2.4

    def clear(x, z):
        if cell_is_wall(round(x / CELL), round(z / CELL)) or blocks_view(x, z):
            return False
        return all(not cell_is_wall(*world_cell(x + dx, z + dz))
                   for dx, dz in ((1.8, 0), (-1.8, 0), (0, 1.8), (0, -1.8)))

    for ang in (0.0, 0.35, -0.35, 0.7, -0.7):
        ca, sa = math.cos(ang), math.sin(ang)
        dx, dz = fwd.x * ca - fwd.z * sa, fwd.x * sa + fwd.z * ca
        for dist in (CUTSCENE_DIST, 9.0, 8.0, 12.0, 7.0):
            x, z = player.x + dx * dist, player.z + dz * dist
            if clear(x, z) and has_line_of_sight(player.x, player.z, x, z):
                return (x, z)
    return (player.x + fwd.x * 8.0, player.z + fwd.z * 8.0)


# ---- the dig-out, as a timeline in seconds -------------------------------------
# 0.0  the floor starts to crack and shake; nothing visible yet
# 1.0  one arm punches up through the carpet
# 1.4  ...and slams down on the edge of the hole, claws in
# 1.9  the other arm punches up, 2.3 it slams down too
# 2.4  it hauls itself up in three lurches, head hanging, looking at the floor
# 3.9  lets go, steps up onto the rim and stands, head still down
# 4.9  the head snaps up to look straight at you
# 5.3  the bars go and it comes for you
DIG_PUNCH = (1.0, 1.9)      # right arm, left arm break through
DIG_SLAM = 0.40             # after punching up, how long until the hand lands
DIG_LURCHES = (2.45, 2.95, 3.45)
DIG_RELEASE = 3.85          # hands leave the rim, it climbs out
DIG_STOOD = 4.55
DIG_SNAP = 4.9              # head snaps round to you
_DIG_HEIGHTS = (-12.0, -7.4, -6.6, -6.0, -5.0, -3.9, -3.1, 0.0)
DIG_HOLE_R = 1.6            # the hole at its widest
DIG_GRIP = 2.1              # where the claws land: past the edge, on solid floor

_dig_scar = []              # hole, torn carpet and cracks left after it climbs out


def _dig_height(t):
    """Where the root sits: buried, a jolt up with each arm, three lurches, and
    the climb out. Each move is fast then still -- it never glides."""
    h = _DIG_HEIGHTS
    y = h[0]
    y += (h[1] - h[0]) * _smoother((t - DIG_PUNCH[0]) / 0.11)       # first arm
    y += (h[2] - h[1]) * _smoother((t - DIG_PUNCH[0] - 0.2) / 0.5)  # pulls on it
    y += (h[3] - h[2]) * _smoother((t - DIG_PUNCH[1]) / 0.12)       # second arm
    for i, lt in enumerate(DIG_LURCHES):                             # the hauls
        y += (h[4 + i] - h[3 + i]) * _smoother((t - lt) / 0.22)
    y += (h[7] - h[6]) * _smoother((t - DIG_RELEASE) / (DIG_STOOD - DIG_RELEASE))
    # each lurch overshoots and settles back a touch: weight, not an elevator
    for lt in DIG_LURCHES:
        k = (t - lt - 0.22) / 0.3
        if 0.0 < k < 1.0:
            y += 0.22 * math.sin(k * math.pi) * (1.0 - k)
    return y


def _arm_ik(side, target):
    """Two-bone IK for one arm in the plane it swings in: shoulder and elbow
    angles that put the wrist on `target` (a world point). Angles are solved in
    the chest's own frame, so however the torso leans the hand still lands where
    it is told. Returns (shoulder_x, elbow_x) in degrees."""
    rig = _entity._rig
    chest = rig['chest']
    sh = rig['arms'][side]['shoulder']
    S = sh.getPos()                                   # in the chest's frame
    T = chest.getRelativePoint(scene, Vec3(*target))
    vy, vz = T[1] - S[1], T[2] - S[2]
    U, F = UPPER_ARM_LEN, FOREARM_LEN
    D = max(abs(U - F) + 1e-3, min(U + F - 1e-3, math.hypot(vy, vz)))
    phi = math.atan2(vz, -vy)                         # from straight down, toward +z
    alpha = math.acos(max(-1.0, min(1.0, (U * U + D * D - F * F) / (2 * U * D))))
    beta = math.acos(max(-1.0, min(1.0, (U * U + F * F - D * D) / (2 * U * F))))
    upper = phi - alpha                               # the elbow bends the natural way
    bend = math.pi - beta
    return -math.degrees(upper), -math.degrees(bend)


def _pose_dig(t):
    """Pose the whole rig for time t of the dig-out."""
    e = _entity
    rig = getattr(e, '_rig', None)
    if rig is None:
        return
    cs = _cutscene
    sx, sz = cs['site']
    e.rotation_y = math.degrees(math.atan2(player.x - e.x, player.z - e.z))
    e.y = _dig_height(t)

    # torso: bent right over the rim while it hauls, straightening to stand
    haul = _smoother((t - DIG_PUNCH[0] - 0.3) / 0.7)
    stand = _smoother((t - DIG_RELEASE) / (DIG_STOOD - DIG_RELEASE))
    lurch = sum(max(0.0, 1.0 - abs(t - lt - 0.12) / 0.14) for lt in DIG_LURCHES)
    spine = _lerp(_lerp(10, 40, haul), 24, stand) + 6 * lurch
    chest_x = _lerp(_lerp(8, 28, haul), 18, stand) + 4 * lurch
    rig['spine'].rotation_x = spine
    rig['chest'].rotation_x = chest_x
    rig['chest'].rotation_y = math.sin(t * 2.7) * 6 * haul * (1 - stand)
    rig['body'].rotation_z = math.sin(t * 3.1) * 3 * haul * (1 - stand)
    lean = spine + chest_x

    # legs: dangling unseen until it climbs out, then one knee comes up onto
    # the rim and pushes it to standing
    step = _smoother((t - DIG_RELEASE) / 0.3) * (1 - _smoother((t - DIG_STOOD + 0.15) / 0.3))
    heights = []
    for side in (-1, 1):
        leg = rig['legs'][side]
        thigh = -4.0 + (-72.0 * step if side == 1 else 8.0 * step)
        knee = 10.0 + (92.0 * step if side == 1 else 14.0 * step)
        leg['hip'].rotation_x = thigh
        leg['knee'].rotation_x = knee
        leg['ankle'].rotation_x = -(thigh + knee) * 0.7
        heights.append(ANKLE_H + THIGH_LEN * math.cos(math.radians(thigh))
                       + SHIN_LEN * math.cos(math.radians(thigh + knee)))
    rig['body'].y = heights[0]                 # the left leg is the one it stands on

    # arms: hidden -> punch straight up -> slam onto the rim -> let go
    fwd = Vec3(player.x - sx, 0, player.z - sz)
    fwd = fwd.normalized() if fwd.length() > 1e-4 else Vec3(0, 0, 1)
    rgt = Vec3(fwd.z, 0, -fwd.x)
    for side in (-1, 1):
        arm = rig['arms'][side]
        punch = DIG_PUNCH[0] if side == 1 else DIG_PUNCH[1]
        up = _smoother((t - punch) / 0.12)
        slam = _smoother((t - punch - DIG_SLAM + 0.16) / 0.16)
        rel = _smoother((t - DIG_RELEASE) / 0.35)
        # the claw lands on solid floor past the hole's edge, reaching forward
        rim = Vec3(sx, 0.16, sz) + fwd * DIG_GRIP + rgt * (0.55 * side)
        ik_x, ik_e = _arm_ik(side, rim)
        hang_x, hang_e = -12.0, -20.0
        up_x, up_e = -172.0, -6.0
        stood_x, stood_e = -lean * 0.8 - 16 - 18 * (1 - stand), -34.0
        # before it lands, the raised claw rakes at the air
        rake = math.sin(t * 17.0 + side) * 16.0 * up * (1 - slam)
        s_x = _lerp(_lerp(_lerp(hang_x, up_x + rake, up), ik_x, slam), stood_x, rel)
        s_e = _lerp(_lerp(_lerp(hang_e, up_e, up), ik_e, slam), stood_e, rel)
        arm['shoulder'].rotation_x = s_x
        arm['shoulder'].rotation_z = -side * _lerp(_lerp(4, 0, slam), 12, rel)
        arm['elbow'].rotation_x = s_e
        # hand flat on the floor, fingers forward and dug in
        flat = -(78.0 + lean) - s_x - s_e
        arm['wrist'].rotation_x = _lerp(_lerp(-18, -40, up), flat, slam * (1 - rel)) \
            + (-18 * rel)

    # head: hanging, looking at the floor it came out of, twitching -- until the snap
    snap = _smoother((t - DIG_SNAP) / 0.10)
    neck = _lerp(52, 38, stand)
    rig['neck'].rotation_x = neck
    down_pitch = 78.0                          # world pitch while it stares down
    look_pitch = math.degrees(math.atan2(
        (e.y + ENTITY_HEAD_Y * ENTITY_SCALE) - camera.world_position.y,
        max(1.0, distance_xz(e, player))))     # down onto the camera
    world_pitch = _lerp(down_pitch, look_pitch, snap)
    twitch = 0.0
    if t < DIG_SNAP:                          # small, sharp jerks of the head
        twitch = 7.0 * math.sin(t * 23.0) * max(0.0, math.sin(t * 3.3)) ** 6
    rig['head'].rotation_x = world_pitch - (lean + neck) + twitch
    overshoot = 14.0 * math.sin(min(1.0, (t - DIG_SNAP) / 0.35) * math.pi) \
        if t > DIG_SNAP else 0.0
    rig['head'].rotation_y = math.sin(t * 1.7) * 18 * (1 - snap)
    rig['head'].rotation_z = _lerp(4 + math.sin(t * 29.0) * 3.0 * haul, 16, snap) + overshoot


def _begin_cutscene():
    """Take the camera, and start the floor breaking open."""
    global _diamond_phase, _cutscene, _cutscene_active, _fear
    _clear_dig_scar()
    _restore_entity_look()
    site = _diamond_spawn_site()
    _entity.position = (site[0], _DIG_HEIGHTS[0], site[1])
    _entity.enabled = not DEBUG_HIDE_ENTITY
    _face_entity_towards(player.x, player.z, snap=True)

    bars = []
    for sgn in (1, -1):
        bars.append(Entity(parent=camera.ui, model='quad', color=color.black,
                           scale=(2.6, 0.001), position=(0, sgn * 0.5, 0)))
    rr = random.Random(int(site[0] * 13 + site[1] * 7))
    hole = Entity(model=_blob_mesh(1.0, rr, points=22, wobble=0.42),
                  color=color.rgba(0.008, 0.007, 0.006, 1.0), rotation_x=90,
                  position=(site[0], 0.03, site[1]), double_sided=True,
                  scale=0.001)
    _cutscene = {
        't': 0.0, 'site': site, 'bars': bars, 'hole': hole,
        'debris': [], 'dust': [], 'rim': [], 'cracks': _make_floor_cracks(site, rr),
        'fear': 0.0, 'stung': False, 'hits': set(), 'dust_t': 0.0, 'kick': 0.0,
        'aim': None,
    }
    _cutscene_active = True
    _set_hud_visible(False)            # nothing on screen but the scene
    _diamond_phase = 'cutscene'
    _fear = max(_fear, 0.3)
    _play_sfx('rumble', volume=0.95)
    print('[diamond] digging up')


def _make_floor_cracks(site, rr):
    """Jagged cracks radiating from the spot, built hidden and revealed a
    segment at a time as the floor gives way."""
    x0, z0 = site
    cracks = []
    for k in range(7):
        a = k * math.tau / 7 + rr.uniform(-0.3, 0.3)
        x, z = x0 + math.cos(a) * 0.5, z0 + math.sin(a) * 0.5
        segs = []
        for i in range(rr.randint(5, 8)):
            a += rr.uniform(-0.55, 0.55)
            seg = rr.uniform(0.35, 0.7)
            nx, nz = x + math.cos(a) * seg, z + math.sin(a) * seg
            w = 0.07 * (1.0 - i / 9)
            e = Entity(model='cube', color=color.rgba(0.03, 0.025, 0.015, 1),
                       position=((x + nx) / 2, 0.018, (z + nz) / 2),
                       rotation_y=math.degrees(math.atan2(nx - x, nz - z)),
                       scale=(w, 0.01, seg + 0.04), enabled=False)
            segs.append(e)
            x, z = nx, nz
        cracks.append(segs)
    return cracks


def _spawn_rim(scale):
    """Torn carpet flaps peeled up around the hole's edge."""
    cs = _cutscene
    x0, z0 = cs['site']
    rr = random.Random()
    n = 9 if not cs['rim'] else 7
    for _ in range(n):
        a = rr.uniform(0, math.tau)
        r = scale * rr.uniform(0.92, 1.1)
        ca, sa = math.cos(a), math.sin(a)
        size = rr.uniform(0.45, 0.9) * (0.7 + 0.5 * scale)
        flap = Entity(model='cube', shader=OBJ_SHADER, double_sided=True,
                      color=col255(*rr.choice(((112, 100, 58), (98, 88, 50), (84, 74, 42)))),
                      position=(x0 + ca * r, 0.06, z0 + sa * r),
                      rotation=(rr.uniform(-60, -25), math.degrees(math.atan2(ca, sa)),
                                rr.uniform(-12, 12)),
                      scale=(size, 0.04, size * 0.6))
        cs['rim'].append([flap, 0.0])


def _spawn_debris(count=12, power=1.0):
    cs = _cutscene
    x, z = cs['site']
    r = random.Random()
    for _ in range(count):
        a = r.uniform(0, math.tau)
        rc = r.choice(((60, 54, 34), (44, 40, 26), (150, 136, 84), (112, 100, 58)))
        flat = r.random() < 0.4                        # scraps of carpet, not just chunks
        s = r.uniform(0.14, 0.38)
        e = Entity(model='cube', shader=OBJ_SHADER, double_sided=True,
                   color=color.rgb(rc[0] / 255, rc[1] / 255, rc[2] / 255),
                   position=(x + math.cos(a) * r.uniform(0.2, 1.1), 0.2,
                             z + math.sin(a) * r.uniform(0.2, 1.1)),
                   scale=(s * 1.6, 0.04, s) if flat else s,
                   rotation=(r.uniform(0, 360), r.uniform(0, 360), r.uniform(0, 360)))
        vel = Vec3(math.cos(a) * r.uniform(1.0, 3.2) * power, r.uniform(5.0, 10.0) * power,
                   math.sin(a) * r.uniform(1.0, 3.2) * power)
        spin = Vec3(*[r.uniform(-420, 420) for _ in range(3)])
        cs['debris'].append([e, vel, spin])


def _spawn_dust(count=3, burst=False):
    cs = _cutscene
    x, z = cs['site']
    r = random.Random()
    for _ in range(count):
        a = r.uniform(0, math.tau)
        rad = r.uniform(0.3, 1.6) if burst else r.uniform(0.1, 1.4)
        e = Entity(model='quad', texture=_get_puff_texture(), double_sided=True,
                   color=color.rgba(0.36, 0.33, 0.26, 0.0),
                   position=(x + math.cos(a) * rad, r.uniform(0.1, 0.8),
                             z + math.sin(a) * rad),
                   scale=r.uniform(1.4, 3.0) if burst else r.uniform(1.0, 2.4))
        out = 2.2 if burst else 0.4
        cs['dust'].append([e, 0.0, r.uniform(1.1, 2.0), Vec3(
            math.cos(a) * out, r.uniform(0.8, 1.8), math.sin(a) * out)])


def _dig_impact(power, debris, dust):
    """Something broke through or slammed down: throw the floor about and jolt
    the camera."""
    cs = _cutscene
    _spawn_debris(debris, power)
    _spawn_dust(dust, burst=True)
    _spawn_rim(max(0.5, cs['hole'].scale_x))
    cs['kick'] = max(cs['kick'], power)


def _update_cutscene():
    global _fear, _glitch_spike, _black
    cs = _cutscene
    dt = time.dt
    cs['t'] += dt
    t = cs['t']
    p = t / DIG_TIME

    # letterbox in, then out as it comes for you
    barh = _smoother(t / 0.6) * BAR_H * (1 - _smoother((t - 5.3) / 0.6))
    for i, bar in enumerate(cs['bars']):
        sgn = 1 if i == 0 else -1
        bar.scale_y = max(0.001, barh)
        bar.y = sgn * (0.5 - barh / 2)

    # the floor gives way: cracks spread out first, then the hole opens up in
    # steps as each part of it forces its way out
    grow = min(1.0, t / 1.0)
    for segs in cs['cracks']:
        shown = int(round(grow * len(segs)))
        for i, s in enumerate(segs):
            s.enabled = i < shown
    hole_r = DIG_HOLE_R * (0.42 * _smoother((t - DIG_PUNCH[0]) / 0.1)
                    + 0.26 * _smoother((t - DIG_PUNCH[1]) / 0.1)
                    + 0.32 * _smoother((t - DIG_LURCHES[0]) / 1.2))
    cs['hole'].scale = (max(0.001, hole_r), max(0.001, hole_r), 1)

    # moments that hit: both punches, both slams, every lurch
    events = [('p1', DIG_PUNCH[0], 1.0, 16, 6), ('p2', DIG_PUNCH[1], 0.9, 14, 5),
              ('s1', DIG_PUNCH[0] + DIG_SLAM, 0.55, 5, 4),
              ('s2', DIG_PUNCH[1] + DIG_SLAM, 0.55, 5, 4)]
    events += [(f'l{i}', lt, 0.4, 4, 3) for i, lt in enumerate(DIG_LURCHES)]
    events += [('out', DIG_STOOD - 0.1, 0.45, 6, 5)]
    for name, at, power, debris, dust in events:
        if t >= at and name not in cs['hits']:
            cs['hits'].add(name)
            _dig_impact(power, debris, dust)
            if name in ('p1', 'p2'):
                _glitch_spike = max(_glitch_spike, 0.55)

    # rim flaps pop up from the floor as they tear
    for f in cs['rim']:
        f[1] = min(1.0, f[1] + dt * 9.0)
        f[0].scale_y = 0.04 * _smoother(f[1])

    for d in cs['debris']:
        e, vel, spin = d
        vel.y -= 24.0 * dt
        e.position += vel * dt
        e.rotation += spin * dt
        if e.y < 0.05:
            e.y = 0.05
            vel.x *= 0.35
            vel.z *= 0.35
            vel.y = -vel.y * 0.2 if vel.y < -2 else 0.0
            spin *= 0.3
    cs['dust_t'] -= dt
    if 0.2 < t < 4.4 and cs['dust_t'] <= 0:
        cs['dust_t'] = 0.16
        _spawn_dust(2)
    for d in list(cs['dust']):
        e, age, life, vel = d[0], d[1] + dt, d[2], d[3]
        d[1] = age
        e.position += vel * dt
        vel *= (1.0 - dt * 1.2)
        e.look_at(camera.world_position)         # billboard toward the camera
        e.scale += Vec3(1, 1, 1) * dt * 1.6
        f = age / life
        e.color = color.rgba(0.36, 0.33, 0.26, max(0.0, 0.32 * min(1.0, f * 6) * (1.0 - f)))
        if f >= 1.0:
            destroy(e)
            cs['dust'].remove(d)

    _pose_dig(t)

    # the camera: turn to the break, then follow its head as it comes up,
    # pushing in so it fills the frame
    rig = _entity._rig
    head = rig['head'].world_position
    up = _smoother((t - DIG_PUNCH[0]) / (DIG_STOOD - DIG_PUNCH[0]))
    sx, sz = cs['site']
    aim = Vec3(_lerp(sx, head.x, up), _lerp(0.4, head.y - 0.8, up), _lerp(sz, head.z, up))
    cs['aim'] = aim if cs['aim'] is None else cs['aim'] + (aim - cs['aim']) * min(1.0, dt * 6.0)
    to = cs['aim'] - camera.world_position
    want_yaw = math.degrees(math.atan2(to.x, to.z))
    want_pitch = -math.degrees(math.atan2(to.y, math.hypot(to.x, to.z)))
    diff = (want_yaw - player.rotation_y + 180.0) % 360.0 - 180.0
    player.rotation_y += diff * min(1.0, dt * 5.0)
    cs['kick'] = max(0.0, cs['kick'] - dt * 3.5)
    tremor = (0.3 + 1.8 * min(1.0, t / 2.0)) * (1.0 - _smoother((t - DIG_SNAP) / 0.6))
    shake = (math.sin(_look_time * 47.0) * tremor * 0.35
             + math.sin(_look_time * 61.0) * cs['kick'] * 3.2)
    pv = player.camera_pivot
    pv.rotation_x += (max(-70.0, min(70.0, want_pitch)) + shake - pv.rotation_x) * min(1.0, dt * 8.0)
    player.rotation_y += math.sin(_look_time * 53.0) * cs['kick'] * 0.9

    # lens: a slow push in, a punch in on the snap, and back out as it ends
    fov = _lerp(FOV, 78.0, _smoother(t / DIG_STOOD))
    if t >= DIG_SNAP:
        fov -= 16.0 * math.exp(-(t - DIG_SNAP) * 5.0)
    fov = _lerp(fov, FOV, _smoother((t - 5.3) / 0.7))
    camera.fov = fov

    cs['fear'] = 0.2 + 0.45 * _smoother(min(1.0, t / 4.0))
    _fear = max(_fear, cs['fear'])

    if not cs['stung'] and t >= DIG_SNAP:          # the head-snap toward you
        cs['stung'] = True
        _play_sfx('sting', volume=1.0)
        _fear = 1.0
        _glitch_spike = 1.0
        _black = max(_black, 0.6)

    if t >= DIG_TIME:
        _end_cutscene()


def _cleanup_cutscene():
    """Tear down the cutscene and give the player back. What it did to the floor
    -- the hole, the torn carpet, the cracks and the rubble -- stays."""
    global _cutscene_active, _cutscene
    cs = _cutscene
    if cs is None:
        _cutscene_active = False
        return
    for d in cs['dust']:
        destroy(d[0])
    for bar in cs['bars']:
        destroy(bar)
    _dig_scar.append(cs['hole'])
    _dig_scar.extend(f[0] for f in cs['rim'])
    _dig_scar.extend(d[0] for d in cs['debris'])
    for segs in cs['cracks']:
        for s in segs:
            s.enabled = True
            _dig_scar.append(s)
    _cutscene = None
    _cutscene_active = False
    camera.fov = FOV
    if _game_state == 'playing' and not _paused:
        _set_hud_visible(True)         # the menu / death card handle it otherwise


def _clear_dig_scar():
    for e in _dig_scar:
        destroy(e)
    _dig_scar.clear()


def _end_cutscene():
    _cleanup_cutscene()
    if _entity is not None:
        _entity.y = 0
        _entity._anim['lx'] = None
    _begin_chase()


def _begin_chase():
    global _entity_chasing, _entity_path, _entity_repath, _chase_t, _diamond_phase, _fear, _glitch_spike
    global _chase_delay, _chase_elapsed, _chase_speed
    _entity_chasing = True
    _entity_path, _entity_repath = [], 0.0
    _chase_delay = ENTITY_CHASE_DELAY
    _chase_elapsed = 0.0
    _chase_speed = ENTITY_CHASE_START_SPEED
    _chase_t = random.uniform(CHASE_MIN, CHASE_MAX)
    _diamond_phase = 'chasing'
    _fear = max(_fear, 0.6)
    _glitch_spike = 1.0
    _play_loop('chase', volume=0.85)
    print(f'[diamond] the chase begins ({_chase_t:.1f}s)')


def _end_chase():
    """The chase times out: Diamond glitches out and sinks away to re-dig later."""
    global _entity_chasing, _diamond_phase, _spawn_t, _glitch_spike, _black
    _stop_sfx('chase')
    _play_sfx('sting', volume=0.7)
    _glitch_spike = 1.0
    _black = 1.0
    _entity_chasing = False
    if _entity is not None:
        _entity.enabled = False
    _diamond_phase = 'buried'
    _spawn_t = random.uniform(RESPAWN_MIN, RESPAWN_MAX)
    print(f'[diamond] gone -- re-digs in {_spawn_t:.1f}s')


def _update_diamond():
    """Drive the whole loop: buried -> dig-out cutscene -> chase -> buried."""
    global _spawn_t, _chase_t
    if _entity is None or DEBUG_FREEZE_ENTITY:
        return
    if _diamond_phase == 'buried':
        _spawn_t -= time.dt
        if _spawn_t <= 0:
            _begin_cutscene()
    elif _diamond_phase == 'cutscene':
        _update_cutscene()
    elif _diamond_phase == 'chasing':
        _chase_t -= time.dt
        if _chase_t <= 0:
            _end_chase()
    elif _diamond_phase == 'burning':
        _update_burning()


def _entity_in_sight():
    """Entity is within the player's view cone AND not behind a wall."""
    to_e = _entity.world_position + Vec3(0, 2.5, 0) - camera.world_position
    dist = to_e.length()
    if dist > ENTITY_SEE_RANGE or dist < 0.001:
        return False
    if to_e.normalized().dot(camera.forward) < ENTITY_SEE_DOT:
        return False
    return has_line_of_sight(camera.world_position.x, camera.world_position.z,
                             _entity.x, _entity.z)


def _entity_body_blocked(x, z):
    """Would the monster's body overlap a wall standing here? Checked at its
    centre and four points around it, so its shoulders can't sink into a wall."""
    r = ENTITY_RADIUS
    return (cell_is_wall(*world_cell(x, z))
            or cell_is_wall(*world_cell(x + r, z))
            or cell_is_wall(*world_cell(x - r, z))
            or cell_is_wall(*world_cell(x, z + r))
            or cell_is_wall(*world_cell(x, z - r)))


def _entity_step_towards(tx, tz, stop_dist=0.0):
    """Move the entity toward a world point, turning to face the way it walks.

    The move is vetoed if it would put its body inside a wall; each axis is then
    tried alone so it slides along the surface instead of stopping dead. This is
    the hard guarantee that it can never end up inside geometry, whatever the
    pathfinder or the straight-line shortcut decide.
    """
    dx, dz = tx - _entity.x, tz - _entity.z
    d = math.hypot(dx, dz)
    if d < 1e-6:
        return 0.0
    if d > stop_dist:
        step = min(_chase_speed * time.dt, d - stop_dist)
        nx = _entity.x + dx / d * step
        nz = _entity.z + dz / d * step
        if not _entity_body_blocked(nx, nz):
            _entity.x, _entity.z = nx, nz
        elif not _entity_body_blocked(nx, _entity.z):
            _entity.x = nx
        elif not _entity_body_blocked(_entity.x, nz):
            _entity.z = nz
    return d


def _update_entity():
    """Hunt the player around the walls while the chase phase is active."""
    global _entity_chasing, _entity_path, _entity_goal, _entity_repath
    global _chase_delay, _chase_elapsed, _chase_speed
    if _entity is None or DEBUG_FREEZE_ENTITY or _diamond_phase != 'chasing':
        return
    if _chase_delay > 0:            # it stands there a beat first -- run
        _chase_delay -= time.dt
        _animate_entity(0.0)
        return
    # it lopes rather than sprints for the first second, so you can break away
    _chase_elapsed += time.dt
    _chase_speed = (ENTITY_CHASE_START_SPEED if _chase_elapsed < ENTITY_CHASE_RAMP
                    else ENTITY_CHASE_SPEED)

    # --- chasing ---
    before = (_entity.x, _entity.z)

    # Clear shot? Then ignore the grid completely and walk straight at you.
    # A* routes centre-to-centre on a 4-way grid, so following it across open
    # floor produced a staircase of pointless right-angle turns.
    if has_line_of_sight(_entity.x, _entity.z, player.x, player.z,
                         radius=ENTITY_RADIUS, step_len=0.35):
        _entity_path = []
        _entity_step_towards(player.x, player.z, stop_dist=ENTITY_CATCH_DIST)
        moved = math.hypot(_entity.x - before[0], _entity.z - before[1])
        _animate_entity(moved)
        _entity_footsteps(moved)
        return

    goal = world_cell(player.x, player.z)
    _entity_repath -= time.dt
    if _entity_repath <= 0 or goal != _entity_goal:
        _entity_path = find_path(world_cell(_entity.x, _entity.z), goal)
        _entity_goal = goal
        _entity_repath = ENTITY_REPATH_INTERVAL

    while _entity_path and \
            math.hypot(_entity_path[0][0] * CELL - _entity.x,
                       _entity_path[0][1] * CELL - _entity.z) < 0.4:
        _entity_path.pop(0)          # reached this waypoint

    # String-pulling: skip ahead to the furthest waypoint still in plain sight,
    # so it cuts the corner instead of tracing every cell centre on the way.
    for _ in range(6):               # bounded so this stays cheap per frame
        if len(_entity_path) < 2:
            break
        nx, nz = _entity_path[1][0] * CELL, _entity_path[1][1] * CELL
        if not has_line_of_sight(_entity.x, _entity.z, nx, nz,
                                 radius=ENTITY_RADIUS, step_len=0.35):
            break
        _entity_path.pop(0)

    if _entity_path:
        cx, cz = _entity_path[0]
        _entity_step_towards(cx * CELL, cz * CELL)
    else:
        # same cell as the player (or nowhere to route): close in directly
        _entity_step_towards(player.x, player.z, stop_dist=ENTITY_CATCH_DIST)
    moved = math.hypot(_entity.x - before[0], _entity.z - before[1])
    _animate_entity(moved)
    _entity_footsteps(moved)


class Player(FirstPersonController):
    """FirstPersonController with per-axis (sliding) collision.

    Ursina's version casts one ray along your movement direction and, if it
    hits, skips the ENTIRE movement block -- including the per-axis clamps that
    were supposed to let you slide. So brushing a wall at an angle stops you
    dead, and in a corner almost every direction has some component into one of
    the two walls, which cancels everything: you can't even back out, only
    strafe along until a side happens to clear.

    This resolves each axis independently, so the blocked axis stops and the
    free one keeps moving -- you slide along walls and can always reverse.
    """

    SKIN = 0.35          # stop this far short of a surface
    vy = 0.0             # vertical velocity

    def _blocked(self, direction, distance):
        """Is something within `distance` along `direction`, at any body height?"""
        for h in (0.5, self.height * 0.5, self.height - 0.1):
            if raycast(self.position + Vec3(0, h, 0), direction,
                       distance=distance, traverse_target=self.traverse_target,
                       ignore=self.ignore_list).hit:
                return True
        return False

    def update(self):
        if _cutscene_active or _paused or _game_state != 'playing':
            return
        self.rotation_y += mouse.velocity[0] * self.mouse_sensitivity[1]
        self.camera_pivot.rotation_x -= mouse.velocity[1] * self.mouse_sensitivity[0]
        self.camera_pivot.rotation_x = clamp(self.camera_pivot.rotation_x, -90, 90)

        self.direction = Vec3(
            self.forward * (held_keys['w'] - held_keys['s'])
            + self.right * (held_keys['d'] - held_keys['a'])
        ).normalized()

        # Cap the step. One long frame (alt-tab, dragging the window, a
        # screenshot) otherwise integrates half a second of gravity at once: you
        # drop ~9 units straight through the floor in a single step, and the
        # fell-out-of-the-world rescue drops you back in from y=3 -- onto
        # whatever happens to be below.
        dt = min(time.dt, PLAYER_MAX_DT)
        move = self.direction * dt * self.speed
        # Resolve X and Z separately: only the blocked axis is cancelled.
        if move[0] and self._blocked(Vec3(1 if move[0] > 0 else -1, 0, 0),
                                     abs(move[0]) + self.SKIN):
            move[0] = 0
        if move[2] and self._blocked(Vec3(0, 0, 1 if move[2] > 0 else -1),
                                     abs(move[2]) + self.SKIN):
            move[2] = 0
        if DEBUG_NOCLIP:                    # fly, ignore geometry
            self.position += move * 2
            self.y += (held_keys['space'] - held_keys['left control']) * 9 * dt
            return
        self.position += move

        # --- vertical motion: velocity + constant gravity = a true parabola ---
        if self.gravity:
            self.vy -= JUMP_GRAVITY * dt
            self.y += self.vy * dt
            ray = raycast(self.world_position + (0, self.height, 0), self.down,
                          traverse_target=self.traverse_target,
                          ignore=self.ignore_list)
            if ray.hit and self.vy <= 0 and self.y <= ray.world_point[1] + 0.02:
                self.y = ray.world_point[1]       # landed
                self.vy = 0.0
                if not self.grounded:
                    self.land()
                self.grounded = True
            else:
                self.grounded = False

    def jump(self):
        if not self.grounded:
            return
        self.vy = JUMP_VELOCITY
        self.grounded = False


def _unstick_player():
    """Push the player out if they have clipped inside a wall.

    FirstPersonController is a single point guarded by a few half-unit rays, so
    scraping a wall corner at an angle can leave your centre inside the cube.
    Once inside, rays in EVERY direction hit that cube's faces, so the
    controller clamps both axes to zero and you cannot walk back out -- only
    strafe until a side happens to clear. This detects that and eases you toward
    the nearest open cell instead.
    """
    cx, cz = world_cell(player.x, player.z)
    stuck = cell_is_wall(cx, cz)
    if not stuck and hasattr(player, '_blocked'):
        # Being blocked in all four directions at once is only possible when the
        # player's centre is inside a collider -- catches clipping into a wall
        # whose cell we aren't technically standing in (corners, chunk seams).
        stuck = all(player._blocked(d, 0.06) for d in
                    (Vec3(1, 0, 0), Vec3(-1, 0, 0), Vec3(0, 0, 1), Vec3(0, 0, -1)))
    if not stuck:
        return
    best = None
    for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1),
                   (1, 1), (1, -1), (-1, 1), (-1, -1)):
        if cell_is_wall(cx + dx, cz + dz):
            continue
        tx, tz = (cx + dx) * CELL, (cz + dz) * CELL
        d = math.hypot(tx - player.x, tz - player.z)
        if best is None or d < best[0]:
            best = (d, tx, tz)
    if best is None:
        return                       # walled in on all sides; nothing sensible to do
    d, tx, tz = best
    if d < 1e-6:
        return
    step = UNSTICK_SPEED * time.dt
    player.x += (tx - player.x) / d * step
    player.z += (tz - player.z) / d * step


def _window_geometry():
    """Current OS window origin and size, or None if it cannot be read."""
    try:
        p = base.win.getProperties()          # noqa: F821 (panda3d builtin)
        return (p.getXOrigin(), p.getYOrigin(), p.getXSize(), p.getYSize())
    except Exception:
        return None


# ---- the window title --------------------------------------------------------
# "Hollow Floor" normally. While it digs out, the title bar starts flickering to
# RUN like a failing sign -- more and more often -- and once the chase is on it
# just says RUN until the chase is over.
GAME_TITLE = 'Hollow Floor'
RUN_TITLE = 'RUN'
_title_now = GAME_TITLE     # what the window currently says
_title_flick_t = 0.0        # time until the flickering title changes again
_title_flick_run = False


def _set_title(text):
    global _title_now
    if text == _title_now:
        return                             # only talk to the OS when it changes
    _title_now = text
    props = WindowProperties()
    props.setTitle(text)
    base.win.requestProperties(props)      # noqa: F821 (panda3d builtin)


def _update_title():
    global _title_flick_t, _title_flick_run
    if _cutscene is not None:
        t = _cutscene['t']
        if t >= DIG_SNAP:                  # it has seen you: no more flicker
            _set_title(RUN_TITLE)
            return
        _title_flick_t -= time.dt
        if _title_flick_t <= 0:
            urgency = max(0.0, min(1.0, (t - 0.6) / (DIG_SNAP - 0.6)))
            _title_flick_run = random.random() < 0.18 + 0.62 * urgency
            # RUN only ever flashes for a moment; the real title holds longer,
            # and less and less long as it climbs out
            _title_flick_t = (random.uniform(0.04, 0.16) if _title_flick_run
                              else random.uniform(0.08, 0.6) * (1.0 - 0.7 * urgency))
        _set_title(RUN_TITLE if _title_flick_run else GAME_TITLE)
    elif _entity_chasing:
        _set_title(RUN_TITLE)
    else:
        _set_title(GAME_TITLE)


_win_rest = None            # where the window sits, fixed when a shake starts
_win_rest_size = None


def _update_window_shake():
    """Physically wobble the OS window while the entity is chasing.

    The resting position is taken ONCE, when the shake starts, and the window is
    shaken around that fixed point and put back on it exactly at the end. It used
    to be re-derived every frame as (where the window is) - (our last nudge), and
    that drifted: Windows refuses to move a maximized window further up/left than
    its snapped position, so every refused up/left nudge was "subtracted" anyway
    and the resting point crept down and to the right, frame after frame.

    If the player drags or resizes the window mid-chase, the window lands far
    from anywhere a nudge could put it, and the rest re-anchors to follow them.
    """
    global _win_shaking, _win_offset, _win_shake_clock, _win_rest, _win_rest_size
    geo = _window_geometry()
    if geo is None:
        return

    # only rattle the window when it is actually visible -- not through a wall
    want = (DEBUG_SHAKE_ON and _entity_chasing and not _paused)
    if not want:
        if _win_shaking or _win_offset != (0, 0):   # settle back exactly
            if _win_rest is not None:
                _set_window_origin(*_win_rest)
            _win_offset = (0, 0)
        _win_shaking = False
        _win_rest = None
        return

    amp = WIN_SHAKE_PIXELS
    if not _win_shaking or _win_rest is None:
        _win_rest = (geo[0] - _win_offset[0], geo[1] - _win_offset[1])
        _win_rest_size = (geo[2], geo[3])
        _win_shake_clock = WIN_SHAKE_HOLD     # jolt immediately when it starts
    else:
        # moved or resized by the player? a nudge (or a refused nudge, or one
        # frame of lag) is never more than 2x the amplitude away
        ex = _win_rest[0] + _win_offset[0]
        ey = _win_rest[1] + _win_offset[1]
        far = 2 * amp + 24
        if ((geo[2], geo[3]) != _win_rest_size
                or abs(geo[0] - ex) > far or abs(geo[1] - ey) > far):
            # where the player put it IS the rest -- none of our nudge is in it
            _win_rest = (geo[0], geo[1])
            _win_rest_size = (geo[2], geo[3])
            _win_offset = (0, 0)
    _win_shaking = True
    # Hold each position for WIN_SHAKE_HOLD frames. A fractional hold averages
    # out (1.2 moves the window on 5 frames out of every 6).
    _win_shake_clock += 1.0
    if _win_shake_clock < WIN_SHAKE_HOLD:
        return
    _win_shake_clock -= WIN_SHAKE_HOLD
    # uniform(), not randint(): the debug panel tunes this in float steps, and
    # randint() raises TypeError the moment the value stops being a whole number.
    _win_offset = (int(random.uniform(-amp, amp)), int(random.uniform(-amp, amp)))
    _set_window_origin(_win_rest[0] + _win_offset[0], _win_rest[1] + _win_offset[1])


def _set_window_origin(x, y):
    props = WindowProperties()
    props.setOrigin(int(x), int(y))
    base.win.requestProperties(props)         # noqa: F821 (panda3d builtin)


def _sync_window_properties():
    """Stop ursina resizing and moving the window behind our backs.

    ursina's `window` IS a WindowProperties object, and requestProperties(window)
    re-submits EVERY field it holds -- both the origin from startup's
    center_on_screen() and the startup size. mouse.locked does exactly that (see
    ursina/mouse.py), so re-locking on start or resume yanks a window the player
    has since maximized back to 1280x720 in the corner.

    An unset field means "leave this alone", so the fix is to clear both rather
    than to keep guessing the right values. Falls back to writing the window's
    real geometry if this build has no clear methods.
    """
    try:
        window.clearOrigin()
        window.clearSize()
        window.clearTitle()            # the title is ours to set (see _update_title)
        return
    except Exception:
        pass
    geo = _window_geometry()
    if geo is None:
        return
    try:
        window.setOrigin(int(geo[0] - _win_offset[0]),
                         int(geo[1] - _win_offset[1]))
        window.setSize(int(geo[2]), int(geo[3]))
    except Exception:
        pass


def build_prop_showcase():
    """TEST MODE (PROP_SHOWCASE): line up one of every prop just in front of
    spawn so they can be inspected side by side. Turn PROP_SHOWCASE off for
    normal play -- this is not part of the world generation."""
    r = random.Random(5)
    label_col = col255(255, 255, 255)

    def label(text, x, z, y=2.6):
        # World-space text needs a far larger scale than UI text. Do NOT rotate
        # it: ursina's Text already faces back toward spawn, and rotating 180
        # shows the reverse side, which renders the words mirrored.
        Text(text=text, parent=scene, position=(x, y, z - 0.8), scale=28,
             origin=(0, 0), color=label_col, double_sided=True)

    z = 9
    _lit(_prop_paint_bucket(-9, z, r))
    label('paint bucket', -9, z)

    _lit(_prop_backpack(-4.5, z, r))
    label('backpack', -4.5, z)

    _lit(_prop_cone(0, z, r))
    label('traffic cone', 0, z)

    _lit(_prop_puddle(4.5, z, r))
    label('puddle', 4.5, z, y=1.4)

    # the crack needs a wall to sit on, so give it one
    wv, wt, wn, wu = [], [], [], []
    _add_box(wv, wt, wn, wu, 12, WALL_H / 2, 8, CELL, WALL_H, CELL, MAT_WALL)
    Entity(model=Mesh(vertices=wv, triangles=wt, normals=wn, uvs=wu),
           shader=WORLD_SHADER, double_sided=True)
    _lit(_prop_crack(2, 2, r, force_dir=(1, 0)))
    label('wall crack', 9.4, 8, y=3.4)

    # ceiling stain sits overhead; keep its label low so it doesn't collide with
    # the cone label in front of it
    _lit(_prop_ceiling_stain(0, z + 7, r))
    label('ceiling stain - LOOK UP', 0, z + 7, y=0.7)


# ============================================================================
#  DEVELOPER DEBUG PANEL   (F1)
#     up/down  - select a row        left/right - change value / toggle
#     enter    - toggle or run       F1         - close
# ============================================================================

# label, global name  -> on/off switches
_DBG_TOGGLES = (
    ('Glow monster (see thru walls)', 'DEBUG_GLOW'),
    ('Show monster path line', 'DEBUG_PATH'),
    ('Noclip (walk through walls)', 'DEBUG_NOCLIP'),
    ('Freeze entity', 'DEBUG_FREEZE_ENTITY'),
    ('Hide entity', 'DEBUG_HIDE_ENTITY'),
    ('Fog', 'DEBUG_FOG_ON'),
    ('Window shake', 'DEBUG_SHAKE_ON'),
)
# label, global name, step, min, max  -> live numeric tuning
_DBG_VALUES = (
    ('Entity speed', 'ENTITY_CHASE_SPEED', 0.2, 0.0, 40.0),
    ('Entity sight range', 'ENTITY_SEE_RANGE', 10.0, 5.0, 400.0),
    ('Entity step rate', 'ENTITY_STEP_RATE', 0.2, 0.4, 20.0),
    ('Entity limb swing', 'ENTITY_LIMB_SWING', 2.0, 0.0, 90.0),
    ('Walk speed', 'WALK_SPEED', 0.5, 0.5, 60.0),
    ('Sprint speed', 'SPRINT_SPEED', 0.5, 0.5, 80.0),
    ('Fog end', 'FOG_END', 4.0, 8.0, 400.0),
    ('FOV', 'FOV', 5.0, 60.0, 140.0),
    ('Shake pixels', 'WIN_SHAKE_PIXELS', 1.0, 0.0, 40.0),
    ('Shake hold (frames)', 'WIN_SHAKE_HOLD', 0.1, 1.0, 10.0),
    ('Prop to spawn', 'DEBUG_PROP_INDEX', 1.0, 0.0, 5.0),
)
# label, function name -> one-shot actions
_DBG_ACTIONS = (
    ('Run Diamond intro now', '_dbg_intro_now'),
    ('Spawn selected prop in front of me', '_dbg_spawn_prop'),
    ('Trigger chase now', '_dbg_trigger'),
    ('Teleport entity to me', '_dbg_teleport'),
    ('Reset entity (idle, far away)', '_dbg_reset_entity'),
    ('Give +5 inventory slots', '_dbg_expand_inv'),
    ('Fill inventory', '_dbg_fill_inv'),
)


def _dbg_rows():
    """Flat list of (kind, label, key) for everything shown in the panel."""
    return ([('toggle', lbl, g) for lbl, g in _DBG_TOGGLES]
            + [('value', lbl, g) for lbl, g, *_ in _DBG_VALUES]
            + [('action', lbl, f) for lbl, f in _DBG_ACTIONS])


def _dbg_apply(name):
    """Push a changed setting into whatever actually consumes it."""
    if name in ('FOG_END', 'DEBUG_FOG_ON'):
        end = FOG_END if DEBUG_FOG_ON else 1e6
        scene.set_shader_input('fog_end', end)
        camera.clip_plane_far = (FOG_END if DEBUG_FOG_ON else 900) + 25
    elif name == 'FOV':
        camera.fov = FOV
    elif name == 'DEBUG_GLOW':
        _dbg_glow_apply()
    elif name == 'DEBUG_PATH':
        _update_path_vis()
    elif name == 'DEBUG_HIDE_ENTITY' and _entity is not None:
        _entity.enabled = not DEBUG_HIDE_ENTITY


def _update_path_vis():
    """Draw the route the monster is actually following, straight through walls
    so you can see it from anywhere. Rebuilt only when the route changes."""
    global _path_vis, _path_sig
    if not DEBUG_PATH or _entity is None:
        if _path_vis is not None:
            _path_vis.enabled = False
        _path_sig = None
        return

    pts = [(_entity.x, _entity.z)]
    pts += [(c[0] * CELL, c[1] * CELL) for c in _entity_path]
    if len(pts) == 1:                       # straight-line chase, no waypoints
        pts.append((player.x, player.z))
    sig = (len(pts), round(pts[0][0], 1), round(pts[0][1], 1),
           round(pts[-1][0], 1), round(pts[-1][1], 1))
    if _path_vis is not None and sig == _path_sig:
        _path_vis.enabled = True
        return                              # unchanged; don't rebuild the mesh
    _path_sig = sig

    verts = [Vec3(x, 0.25, z) for x, z in pts]
    mesh = Mesh(vertices=verts, mode='line', thickness=4)
    if _path_vis is None:
        _path_vis = Entity(model=mesh, color=col255(90, 255, 140))
        _path_vis.setBin('fixed', 90)       # visible through walls, like the glow
        _path_vis.setDepthTest(False)
        _path_vis.setDepthWrite(False)
    else:
        _path_vis.model = mesh
    _path_vis.enabled = True


def _dbg_glow_apply():
    """Light the monster up and let it draw through walls, so you can always
    see where it is and what it is doing."""
    if _entity is None:
        return
    parts = getattr(_entity, '_parts', [])
    for p in parts:
        p.color = col255(90, 255, 140) if DEBUG_GLOW else color.white
        p.set_shader_input('obj_emit', 0.6 if DEBUG_GLOW else 0.0)
    if DEBUG_GLOW:
        _entity.setBin('fixed', 100)     # drawn after the world...
        _entity.setDepthTest(False)      # ...and ignoring anything in front
        _entity.setDepthWrite(False)
    else:
        _entity.clearBin()
        _entity.setDepthTest(True)
        _entity.setDepthWrite(True)


def _dbg_spawn_prop():
    """Drop the currently selected prop on the floor in front of the player."""
    kind = _PROP_KINDS[int(DEBUG_PROP_INDEX) % len(_PROP_KINDS)]
    a = math.radians(player.rotation_y)
    x = player.x + math.sin(a) * 3.5
    z = player.z + math.cos(a) * 3.5
    cx, cz = world_cell(x, z)
    r = random.Random()
    if kind == 'crack':
        ents = _prop_crack(cx, cz, r)          # becomes a stain if no wall here
    elif kind == 'ceiling_stain':
        ents = _prop_ceiling_stain(x, z, r)
    elif kind == 'puddle':
        ents = _prop_puddle(x, z, r)
    elif kind == 'paint_bucket':
        ents = _prop_paint_bucket(x, z, r)
    elif kind == 'cone':
        ents = _prop_cone(x, z, r)
    elif kind == 'lighter':
        ents = _prop_lighter(x, z, r)
    else:
        ents = _prop_backpack(x, z, r)
    # hand it to the chunk so it unloads normally, and make it pickable
    key = world_to_chunk(x, z)
    chunks.setdefault(key, []).extend(ents)
    if kind in ITEMS:
        _chunk_pickups.setdefault(key, []).append(
            {'kind': kind, 'x': x, 'z': z, 'ents': ents, 'chunk': key})
    print(f'[debug] spawned {kind} ({len(ents)} parts)')


def _dbg_intro_now():
    global _diamond_phase, _spawn_t, _entity_chasing, _cutscene, _cutscene_active
    _stop_sfx('chase')
    _entity_chasing = False
    _cutscene, _cutscene_active = None, False
    _diamond_phase = 'buried'
    _spawn_t = 0.05               # dig-out cutscene fires almost immediately


def _dbg_trigger():
    global _cutscene, _cutscene_active
    _cutscene, _cutscene_active = None, False
    if _entity is not None:
        _entity.enabled = True
        _entity.y = 0
        _entity_place(player.x, player.z)
    _begin_chase()


def _dbg_teleport():
    if _entity is not None:
        _entity.position = (player.x + 3, 0, player.z + 3)


def _dbg_reset_entity():
    global _entity_chasing, _diamond_phase, _spawn_t, _cutscene, _cutscene_active
    _stop_sfx('chase')
    _cutscene, _cutscene_active = None, False
    _entity_chasing = False
    _diamond_phase = 'buried'
    _spawn_t = random.uniform(RESPAWN_MIN, RESPAWN_MAX)
    if _entity is not None:
        _entity.enabled = False


def _dbg_expand_inv():
    if not _inventory.has_bag:
        _inventory.expand(INV_BONUS_SLOTS)
        build_inventory_ui()


def _dbg_fill_inv():
    while _inventory.add('paint_bucket') is not None:
        pass
    _refresh_inventory_ui()


def build_debug_panel():
    global _dbg_ui, _dbg_text
    _dbg_ui = Entity(parent=camera.ui, enabled=False)
    Entity(parent=_dbg_ui, model='quad', color=col255(10, 10, 12), alpha=0.86,
           position=(-0.46, 0.06), scale=(0.62, 0.76), z=0.1)
    _dbg_text = Text(parent=_dbg_ui, text='', origin=(-0.5, 0.5),
                     position=(-0.755, 0.42), scale=0.72,
                     color=col255(226, 224, 210))
    _dbg_refresh()


def _dbg_refresh():
    if _dbg_text is None:
        return
    lines = ['<orange>DEBUG  (Tab / F1)<default>   up/down select   '
             'left/right change   enter toggle', '']
    rows = _dbg_rows()
    section = None
    for i, (kind, label, key) in enumerate(rows):
        if kind != section:
            section = kind
            lines.append({'toggle': '-- switches --', 'value': '',
                          'action': ''}[kind] if kind == 'toggle' else '')
        cursor = '<orange>>' if i == _dbg_sel else ' '
        if kind == 'toggle':
            on = globals()[key]
            state = '<lime>ON <default>' if on else '<gray>off<default>'
            lines.append(f'{cursor} {label:<30}{state}')
        elif kind == 'value':
            if key == 'DEBUG_PROP_INDEX':
                v = _PROP_KINDS[int(DEBUG_PROP_INDEX) % len(_PROP_KINDS)]
            else:
                v = f'{globals()[key]:.2f}'
            lines.append(f'{cursor} {label:<30}{v}')
        else:
            lines.append(f'{cursor} {label:<30}<azure>[enter]<default>')
    lines.append('')
    lines.append(f'  entities {sum(len(v) for v in chunks.values())}   '
                 f'chasing {_entity_chasing}   '
                 f'inv {_inventory.used}/{_inventory.size}')
    _dbg_text.text = '\n'.join(lines)


def _dbg_input(key):
    """Handle a key while the panel is open. Returns True if it was consumed."""
    global _dbg_sel
    rows = _dbg_rows()
    if key in ('up arrow', 'up arrow hold'):
        _dbg_sel = (_dbg_sel - 1) % len(rows)
    elif key in ('down arrow', 'down arrow hold'):
        _dbg_sel = (_dbg_sel + 1) % len(rows)
    elif key in ('left arrow', 'right arrow',
                 'left arrow hold', 'right arrow hold', 'enter'):
        kind, _label, name = rows[_dbg_sel]
        if kind == 'toggle':
            globals()[name] = not globals()[name]
            _dbg_apply(name)
        elif kind == 'value':
            step = next(s for l, g, s, lo, hi in _DBG_VALUES if g == name)
            lo = next(lo for l, g, s, lo, hi in _DBG_VALUES if g == name)
            hi = next(hi for l, g, s, lo, hi in _DBG_VALUES if g == name)
            d = -step if key.startswith('left') else step
            globals()[name] = min(hi, max(lo, globals()[name] + d))
            _dbg_apply(name)
        elif key == 'enter':
            globals()[name]()
    else:
        return False
    _dbg_refresh()
    return True


def _icon_image(kind):
    """Paint one item icon. Drawn at 4x and shrunk, so the edges come out
    smooth instead of stair-stepped at slot size."""
    from PIL import Image, ImageDraw, ImageFilter
    S = 256
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1, fill, r=0):
        d.rounded_rectangle((x0 * 2, y0 * 2, x1 * 2, y1 * 2), radius=r * 2, fill=fill)

    def shade_cols(x0, x1, y0, y1, base, lo=0.62, hi=1.18, r=0):
        """Fill a rounded box with a left-to-right cylinder shade."""
        mask = Image.new('L', (S, S), 0)
        ImageDraw.Draw(mask).rounded_rectangle((x0 * 2, y0 * 2, x1 * 2, y1 * 2),
                                               radius=r * 2, fill=255)
        grad = Image.new('RGBA', (S, S))
        gp = grad.load()
        for x in range(int(x0 * 2), int(x1 * 2) + 1):
            t = (x - x0 * 2) / max(1, (x1 - x0) * 2)
            k = lo + (hi - lo) * max(0.0, 1.0 - abs(t - 0.36) / 0.64) ** 1.3
            c = tuple(max(0, min(255, int(v * k))) for v in base) + (255,)
            for y in range(int(y0 * 2), int(y1 * 2) + 1):
                if 0 <= x < S and 0 <= y < S:
                    gp[x, y] = c
        img.paste(grad, (0, 0), mask)

    # a soft contact shadow under everything, so icons sit IN the slot
    sh = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse((40, 206, 216, 236), fill=(0, 0, 0, 120))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(8)))

    if kind == 'lighter':
        # a small flame first, so the hood overlaps its root
        fl = Image.new('RGBA', (S, S), (0, 0, 0, 0))
        fd = ImageDraw.Draw(fl)
        fd.ellipse((100, 6, 140, 70), fill=(255, 140, 30, 200))
        fd.ellipse((108, 22, 134, 66), fill=(255, 214, 90, 235))
        fd.ellipse((114, 38, 128, 64), fill=(255, 250, 220, 255))
        img.alpha_composite(fl.filter(ImageFilter.GaussianBlur(2.5)))
        shade_cols(38, 90, 52, 116, (196, 46, 44), r=7)          # the shell
        box(44, 58, 48, 110, (255, 190, 180, 90), r=2)            # its sheen
        shade_cols(36, 92, 34, 56, (196, 192, 182), lo=0.55, r=3)  # the hood
        for hx in range(42, 88, 8):                               # vent holes
            box(hx, 40, hx + 4, 44, (60, 58, 54, 255), r=1)
            box(hx, 48, hx + 4, 52, (60, 58, 54, 255), r=1)
        shade_cols(52, 70, 26, 38, (104, 100, 94), lo=0.5, r=4)   # flint wheel
        for wy in (28, 32, 35):
            box(53, wy, 69, wy + 1, (60, 58, 54, 255))
        box(76, 30, 86, 36, (150, 146, 138, 255), r=1)            # the button

    elif kind == 'paint_bucket':
        d.arc((62, 18, 194, 118), 200, 340, fill=(150, 150, 156, 255), width=7)   # handle
        shade_cols(30, 98, 36, 112, (190, 190, 198), r=4)          # the can
        box(30, 60, 98, 90, (64, 94, 150, 255))                   # label band
        box(30, 60, 98, 64, (40, 60, 104, 255))
        box(30, 86, 98, 90, (40, 60, 104, 255))
        box(46, 68, 82, 82, (220, 200, 120, 255), r=2)            # label panel
        d.ellipse((60, 58, 196, 94), fill=(120, 120, 128, 255))   # the lid rim
        d.ellipse((72, 64, 184, 88), fill=(216, 196, 96, 255))    # paint inside
        box(74, 44, 80, 72, (216, 196, 96, 255), r=3)             # a drip
        d.ellipse((144, 132, 164, 152), fill=(216, 196, 96, 255))

    elif kind == 'cone':
        base = [(64, 14), (26, 104), (102, 104)]
        d.polygon([(x * 2, y * 2) for x, y in base], fill=(236, 112, 28, 255))
        d.polygon([(128, 28), (60, 204), (128, 204)], fill=(255, 150, 70, 255))  # lit side
        for y0, y1 in ((44, 56), (70, 82)):                       # reflective bands
            t0, t1 = (y0 - 14) / 90, (y1 - 14) / 90
            d.polygon([(2 * (64 - 38 * t0), 2 * y0), (2 * (64 + 38 * t0), 2 * y0),
                       (2 * (64 + 38 * t1), 2 * y1), (2 * (64 - 38 * t1), 2 * y1)],
                      fill=(244, 244, 236, 255))
        box(58, 10, 70, 18, (200, 90, 22, 255), r=3)              # the tip
        box(16, 102, 112, 116, (36, 34, 32, 255), r=3)            # rubber base

    elif kind == 'backpack':
        box(52, 14, 76, 30, (40, 40, 44, 255), r=6)               # carry loop
        box(58, 18, 70, 26, (0, 0, 0, 0))
        shade_cols(28, 100, 24, 116, (86, 62, 50), r=18)          # the body
        shade_cols(30, 98, 24, 58, (104, 76, 60), lo=0.7, r=16)   # top flap
        box(58, 50, 70, 60, (182, 178, 170, 255), r=2)            # buckle
        shade_cols(40, 88, 70, 110, (70, 50, 40), lo=0.7, r=8)    # front pocket
        box(42, 76, 86, 79, (182, 178, 170, 255))                 # zip
        box(78, 74, 84, 84, (182, 178, 170, 255), r=1)            # zip pull
        for sx in (30, 92):                                       # strap edges
            box(sx, 40, sx + 6, 108, (36, 34, 34, 255), r=2)

    img = img.resize((64, 64), Image.LANCZOS)
    return img


_icon_cache = {}


def _icon(kind):
    tex = _icon_cache.get(kind)
    if tex is None:
        tex = Texture(_icon_image(kind))
        try:
            tex.filtering = 'mipmap'       # they are drawn smaller than 64px
        except Exception:
            pass
        _icon_cache[kind] = tex
    return tex


# ---- the slot bar ------------------------------------------------------------
# Five slots are always on screen. A backpack adds a second row of five that
# folds out above them when you press E. Whatever slot is selected is what the
# mouse uses.

_sel = 0                    # selected slot index into _inventory.slots
_bag_open = False
_inv_frames = []            # per slot: (frame, well, icon, number)
_bag_row = None             # the second row, shown while the bag is open
_bag_badge = None           # small backpack mark beside the bar once you have one
_sel_label = None           # item name that shows briefly when you change slot
_sel_label_t = 0.0

_SLOT_EDGE = (126, 120, 92)
_SLOT_EDGE_SEL = (236, 206, 118)


def _slot_xy(i):
    """Screen position of slot i: 0-4 is the bar, 5-9 the row above it."""
    row, col = divmod(i, INV_SLOTS)
    span = INV_SLOTS * INV_SLOT + (INV_SLOTS - 1) * INV_GAP
    x = -span / 2 + INV_SLOT / 2 + col * (INV_SLOT + INV_GAP)
    return x, INV_Y + row * (INV_SLOT + INV_BAG_GAP)


def build_inventory_ui():
    """Draw (or redraw) both rows. Called again when the backpack adds one."""
    global _inv_ui, _inv_icons, _inv_frames, _bag_row, _bag_badge, _sel_label
    if _inv_ui:
        destroy(_inv_ui)
    _inv_ui = Entity(parent=camera.ui)
    _bag_row = Entity(parent=_inv_ui, enabled=False)
    _inv_frames, _inv_icons = [], []
    for i in range(_inventory.size):
        parent = _inv_ui if i < INV_SLOTS else _bag_row
        x, y = _slot_xy(i)
        frame = Entity(parent=parent, model='quad', color=col255(*_SLOT_EDGE),
                       position=(x, y), scale=INV_SLOT, alpha=0.55, z=0.2)
        well = Entity(parent=parent, model='quad', color=col255(14, 14, 16),
                      position=(x, y), scale=INV_SLOT * 0.90, alpha=0.82, z=0.1)
        # a faint lip along the top of the well, so it reads as recessed
        Entity(parent=parent, model='quad', color=col255(0, 0, 0), alpha=0.5,
               position=(x, y + INV_SLOT * 0.40), scale=(INV_SLOT * 0.90, INV_SLOT * 0.08),
               z=0.05)
        icon = Entity(parent=parent, model='quad', position=(x, y), z=0.0,
                      scale=INV_SLOT * 0.80, color=color.white, enabled=False)
        num = Text(parent=parent, text=str((i + 1) % 10), origin=(-0.5, 0.5),
                   x=x - INV_SLOT * 0.44, y=y + INV_SLOT * 0.46, z=-0.01,
                   scale=0.55, color=col255(150, 144, 116))
        _inv_frames.append((frame, well, icon, num))
        _inv_icons.append(icon)
    if _inventory.size > INV_SLOTS:
        x, y = _slot_xy(INV_SLOTS)
        Text(parent=_bag_row, text='BACKPACK', origin=(-0.5, -0.5),
             x=x - INV_SLOT / 2, y=y + INV_SLOT / 2 + 0.006, scale=0.7,
             color=col255(206, 178, 96))
        Text(parent=_bag_row, text='E  close', origin=(0.5, -0.5),
             x=-x + INV_SLOT / 2, y=y + INV_SLOT / 2 + 0.006, scale=0.6,
             color=col255(124, 118, 94))
        # beside the bar: you own a bag, and E opens it
        bx, by = _slot_xy(INV_SLOTS - 1)
        bx += INV_SLOT * 1.08
        _bag_badge = Entity(parent=_inv_ui)      # unscaled, so the text stays sane
        Entity(parent=_bag_badge, model='quad', texture=_icon('backpack'),
               position=(bx, by + INV_SLOT * 0.10), scale=INV_SLOT * 0.80,
               color=color.white)
        Text(parent=_bag_badge, text='E', origin=(0, 0), x=bx,
             y=by - INV_SLOT * 0.42, scale=0.7, color=col255(206, 178, 96))
    else:
        _bag_badge = None
    _sel_label = Text(parent=_inv_ui, text='', origin=(0, 0), y=INV_Y + 0.062,
                      scale=0.8, color=col255(238, 235, 215), alpha=0)
    _set_bag_open(_bag_open)
    _refresh_inventory_ui()


def _refresh_inventory_ui():
    """Icons for what is in each slot, and the highlight on the selected one."""
    for i, ((frame, well, icon, num), kind) in enumerate(
            zip(_inv_frames, _inventory.slots)):
        if kind is None:
            icon.enabled = False
        else:
            icon.enabled = True
            icon.texture = _icon(kind)
            icon.color = color.white
        chosen = (i == _sel)
        frame.color = col255(*(_SLOT_EDGE_SEL if chosen else _SLOT_EDGE))
        frame.alpha = 0.95 if chosen else 0.55
        frame.scale = INV_SLOT * (1.10 if chosen else 1.0)
        well.alpha = 0.70 if chosen else 0.82
        num.color = col255(*((236, 206, 118) if chosen else (150, 144, 116)))


def _visible_slots():
    return _inventory.size if (_bag_open and _inventory.size > INV_SLOTS) else INV_SLOTS


def _set_bag_open(value):
    global _bag_open, _sel
    _bag_open = bool(value) and _inventory.size > INV_SLOTS
    if _bag_row is not None:
        _bag_row.enabled = _bag_open
    if _bag_badge is not None:
        _bag_badge.enabled = not _bag_open
    if not _bag_open and _sel >= INV_SLOTS:
        _sel = 0                     # the bag closed under your selection
    # the selected-item name sits above whichever row is on top
    if _sel_label is not None:
        _sel_label.y = INV_Y + (INV_SLOT + INV_BAG_GAP if _bag_open else 0) + 0.062


def _select_slot(i, announce=True):
    global _sel, _sel_label_t
    if not 0 <= i < _visible_slots():
        return
    _sel = i
    _refresh_inventory_ui()
    if announce and _sel_label is not None:
        kind = _inventory.slots[i]
        _sel_label.text = ITEMS[kind][0] if kind else ''
        _sel_label.color = col255(238, 235, 215)   # not whatever the last message was
        _sel_label_t = 1.6
    _play_sfx('ui_move', volume=0.25)


def _flash_label(text, seconds=1.6, colour=(238, 235, 215)):
    global _sel_label_t
    if _sel_label is not None:
        _sel_label.text = text
        _sel_label.color = col255(*colour)
        _sel_label_t = seconds


def _update_inventory_ui():
    """Fade the item-name label out after a moment."""
    global _sel_label_t
    if _sel_label is None:
        return
    _sel_label_t = max(0.0, _sel_label_t - time.dt)
    _sel_label.alpha = min(1.0, _sel_label_t / 0.4)


def _inventory_input(key):
    """Number keys and the wheel pick a slot. Returns True if it used the key."""
    if key in '1234567890' and len(key) == 1:
        _select_slot((int(key) - 1) % 10)
        return True
    if key in ('scroll down', 'scroll up'):
        n = _visible_slots()
        _select_slot((_sel + (1 if key == 'scroll down' else -1)) % n)
        return True
    return False


def _use_selected():
    """Left mouse / F: use whatever is in the selected slot. A lighter is
    thrown; anything with no use of its own is put down on the floor."""
    kind = _inventory.slots[_sel] if _sel < _inventory.size else None
    if kind == 'lighter':
        _inventory.slots[_sel] = None
        _refresh_inventory_ui()
        _flash_label('', 0.0)            # the name would sit over an empty slot
        _throw_lighter()
    elif kind is None:
        _flash_label('empty hand', 1.0, (150, 144, 116))
    else:
        _drop_selected(kind)


# ---- putting things down -----------------------------------------------------
# A dropped item is remembered by kind, spot and a seed for its look, and is
# rebuilt with its chunk -- chunks are thrown away and rebuilt as you move, and
# anything that only lived in the scene would vanish the first time you left.
DROP_NEAR, DROP_FAR = 1.6, 4.4  # it goes where you are looking at the floor,
                              # within this range. Looking level it goes at the
                              # far end: at 16:9 you only see ~39deg below level,
                              # so anything nearer than ~4 is under the screen
_dropped = []                 # {'kind', 'x', 'z', 'seed'} for this run

_PROP_BUILDERS = None


def _prop_builder(kind):
    global _PROP_BUILDERS
    if _PROP_BUILDERS is None:
        _PROP_BUILDERS = {'paint_bucket': _prop_paint_bucket, 'cone': _prop_cone,
                          'backpack': _prop_backpack, 'lighter': _prop_lighter}
    return _PROP_BUILDERS[kind]


def _drop_spot():
    """A clear piece of floor in front of you -- where you are looking, if that
    is in reach -- or None. Checks with a ray at knee and waist height, so
    walls, the desk and other props all count."""
    fwd = Vec3(camera.forward.x, 0, camera.forward.z)
    if fwd.length() < 1e-4:
        return None
    fwd = fwd.normalized()
    pitch = player.camera_pivot.rotation_x              # positive = looking down
    eye = camera.world_position.y - player.y
    aim = eye / math.tan(math.radians(pitch)) if pitch > 1.0 else DROP_FAR
    aim = max(DROP_NEAR, min(DROP_FAR, aim))
    tries, d = [], aim
    while d >= 1.2:                    # step in until there is room -- e.g. in
        tries.append(d)                # front of the desk rather than past it
        d -= 0.6
    for d in tries:
        blocked = False
        for h in (0.4, 1.2):
            hit = raycast(player.world_position + Vec3(0, h, 0), fwd, distance=d + 0.6,
                          traverse_target=scene, ignore=player.ignore_list)
            if hit.hit:
                blocked = True
                break
        x, z = player.x + fwd.x * d, player.z + fwd.z * d
        if not blocked and not cell_is_wall(*world_cell(x, z)):
            return x, z
    return None


def _build_dropped(item, key):
    """Build a dropped item's props and its pickup record for chunk `key`."""
    r = random.Random(item['seed'])
    build = _prop_builder(item['kind'])
    if item['kind'] == 'paint_bucket':
        ents = build(item['x'], item['z'], r, placed=True)
    else:
        ents = build(item['x'], item['z'], r)
    ents = _lit(ents)
    pickup = {'kind': item['kind'], 'x': item['x'], 'z': item['z'],
              'ents': ents, 'chunk': key, 'dropped': item}
    return ents, pickup


def _drop_selected(kind):
    spot = _drop_spot()
    if spot is None:
        _flash_label('no room to put it down', 1.3, (210, 120, 100))
        return
    _inventory.slots[_sel] = None
    _refresh_inventory_ui()
    item = {'kind': kind, 'x': spot[0], 'z': spot[1], 'seed': random.randrange(1 << 30)}
    _dropped.append(item)
    key = world_to_chunk(*spot)
    if key in chunks:                    # otherwise it appears when the chunk loads
        ents, pickup = _build_dropped(item, key)
        chunks[key].extend(ents)
        _chunk_pickups.setdefault(key, []).append(pickup)
    _flash_label(f'{ITEMS[kind][0]}  -  put down', 1.2, (150, 144, 116))
    _play_sfx('step', volume=0.55, rate=0.8)          # a soft thud on the carpet


def _clear_dropped():
    """New run: take everything you put down back out of the world."""
    for key, items in _chunk_pickups.items():
        for pk in [q for q in items if 'dropped' in q]:
            owner = chunks.get(key)
            for e in pk['ents']:
                if owner and e in owner:
                    owner.remove(e)
                destroy(e)
            items.remove(pk)
    _dropped.clear()


def _nearest_pickup():
    """The carryable item you are closest to and roughly looking at."""
    best, best_d = None, PICKUP_RANGE
    for items in _chunk_pickups.values():
        for p in items:
            d = math.hypot(p['x'] - player.x, p['z'] - player.z)
            if d >= best_d:
                continue
            to = Vec3(p['x'] - camera.world_position.x, 0,
                      p['z'] - camera.world_position.z)
            if to.length() < 1e-4:
                best, best_d = p, d
                continue
            fwd = Vec3(camera.forward[0], 0, camera.forward[2]).normalized()
            if to.normalized().dot(fwd) >= PICKUP_DOT:
                best, best_d = p, d
    return best


def _update_pickup():
    """Keep the '[E] pick up' prompt in sync with what you're looking at."""
    if _prompt is None:
        return
    p = _nearest_pickup()
    if p is None:
        _prompt.enabled = False
        return
    _prompt.enabled = True
    name = ITEMS[p['kind']][0]
    if p['kind'] == 'backpack' and _inventory.has_bag:
        _prompt.text = 'You already have a backpack'
        _prompt.color = col255(150, 144, 116)
    elif p['kind'] != 'backpack' and _inventory.is_full:
        _prompt.text = 'Inventory full'
        _prompt.color = col255(210, 120, 100)
    else:
        _prompt.text = f'[E]  {name}'
        _prompt.color = col255(238, 235, 215)


def _try_pick_up():
    """Pick up whatever is in front of you. Returns True if something was
    actually taken. The backpack adds a second row instead of using a slot,
    and you can only ever carry one."""
    p = _nearest_pickup()
    if p is None:
        return False
    if p['kind'] == 'backpack':
        if _inventory.has_bag:
            _flash_label('you already have a backpack', 1.4, (150, 144, 116))
            return False
        _inventory.expand(INV_BONUS_SLOTS)
        build_inventory_ui()
        _flash_label('Backpack  -  E to open', 2.2, (236, 206, 118))
        print(f'[inventory] backpack: +{INV_BONUS_SLOTS} slots '
              f'({_inventory.size} total)')
    else:
        i = _inventory.add(p['kind'])
        if i is None:
            return False                 # full: leave it on the floor
        _refresh_inventory_ui()
        name = ITEMS[p['kind']][0]
        _flash_label(f'{name}  -  backpack' if i >= INV_SLOTS else name)
    _play_sfx('ui_select', volume=0.35)

    # remove it from the world
    owner = chunks.get(p['chunk'])
    for e in p['ents']:
        if owner and e in owner:
            owner.remove(e)
        destroy(e)
    holder = _chunk_pickups.get(p['chunk'])
    if holder and p in holder:
        holder.remove(p)
    if p.get('dropped') in _dropped:
        _dropped.remove(p['dropped'])
    return True


def _interact():
    """E: take what you're looking at; otherwise open or close the backpack."""
    if _try_pick_up():
        return
    if _nearest_pickup() is not None:
        return                           # looking at something you can't take
    if _inventory.has_bag:
        _set_bag_open(not _bag_open)
        _refresh_inventory_ui()
        _play_sfx('ui_move', volume=0.3, rate=0.8 if _bag_open else 0.65)


# ============================================================================
#  FOOTSTEPS
# ============================================================================
# Both sets of feet are driven by ground covered rather than by a timer, so
# they stay in step with the legs at any speed. Diamond's are mixed by distance:
# once you have heard them you can tell how close it is without looking.

_step_dist = 0.0            # metres walked since your last footfall
_step_px = 0.0              # where you were last frame
_step_pz = 0.0
_ent_step_dist = 0.0        # the same, for the monster


def _update_footsteps():
    """Your own footfalls, one every STEP_STRIDE of ground."""
    global _step_dist, _step_px, _step_pz
    d = math.hypot(player.x - _step_px, player.z - _step_pz)
    _step_px, _step_pz = player.x, player.z
    if d > 2.0:                      # teleported (respawn / unstick): don't fire
        return
    _step_dist += d
    stride = STEP_STRIDE_RUN if player.speed > WALK_SPEED + 0.1 else STEP_STRIDE
    if _step_dist < stride:
        return
    _step_dist = 0.0
    _play_sfx('step', volume=STEP_VOLUME * random.uniform(0.8, 1.0),
              rate=random.uniform(0.92, 1.10))


def _entity_footsteps(moved):
    """Diamond's footfalls, mixed down by how far away it is."""
    global _ent_step_dist
    if moved <= 0 or moved > 2.0:
        return
    _ent_step_dist += moved
    if _ent_step_dist < ENTITY_STRIDE:
        return
    _ent_step_dist = 0.0
    d = distance_xz(_entity, player)
    if d > ENTITY_STEP_RANGE:
        return
    fall = (1.0 - d / ENTITY_STEP_RANGE) ** 1.6
    _play_sfx('step_heavy', volume=fall * ENTITY_STEP_VOLUME,
              rate=random.uniform(0.94, 1.04))


# ============================================================================
#  FIRE: the lighter, and what it does to Diamond
# ============================================================================
# The one thing in the building that can hurt it. Flames are additive billboards
# on a soft procedural texture -- a white-hot core that cools through orange to
# smoke as each tongue rises and dies, with new ones spawned constantly so the
# fire as a whole keeps moving instead of looping.

MAX_FLAMES = 110        # hard ceiling; fire is the only thing that spawns a crowd
_flames = []            # [entity, age, life, velocity, base_scale]
_burn_spawn = 0.0       # spawn accumulator, so the fire is not framerate-bound
_fires = []             # burning patches on the floor: dicts with x, z, life
_thrown = []            # lighters in the air: dicts with entity, velocity
_flame_tex = None
_burn_t = 0.0           # how long Diamond has been alight
_ADD_BLEND = None       # additive blend attrib, made once


def _get_flame_texture():
    """A soft tongue of flame: bright core, falling away to nothing, taller
    than it is wide so the billboards read as flame rather than as dots."""
    global _flame_tex
    if _flame_tex is not None:
        return _flame_tex
    from PIL import Image
    n = 96
    img = Image.new('RGBA', (n, n), (0, 0, 0, 0))
    px = img.load()
    c = (n - 1) / 2
    for y in range(n):
        for x in range(n):
            dx = (x - c) / c
            dy = (y - c) / c
            # squeeze horizontally and let the top taper: a teardrop
            taper = 1.0 + 0.55 * max(0.0, dy)
            d = math.hypot(dx * 1.45 * taper, dy * 0.95)
            if d >= 1.0:
                continue
            k = (1.0 - d) ** 1.7
            px[x, y] = (255, 240, 210, int(235 * k))
    _flame_tex = Texture(img)
    return _flame_tex


def _flame_blend():
    global _ADD_BLEND
    if _ADD_BLEND is None:
        _ADD_BLEND = ColorBlendAttrib.make(ColorBlendAttrib.M_add,
                                           ColorBlendAttrib.O_incoming_alpha,
                                           ColorBlendAttrib.O_one)
    return _ADD_BLEND


def _spawn_flame(x, y, z, scale=1.0, life=0.6, rise=2.6, spread=0.35,
                 engulf=False):
    """One tongue of flame. Additive and depth-write-off, so overlapping tongues
    build up into a single glowing mass instead of cutting each other out.

    `engulf` also drops the depth TEST, so the flame paints over whatever it is
    burning. Without it the body occludes every tongue on its near side and the
    thing looks like it is standing in front of a fire rather than being in one.
    """
    if len(_flames) >= MAX_FLAMES:
        return
    r = random
    e = Entity(model='quad', texture=_get_flame_texture(), double_sided=True,
               position=(x + r.uniform(-spread, spread), y,
                         z + r.uniform(-spread, spread)),
               color=color.rgba(1.0, 0.95, 0.75, 1.0),
               scale=scale * r.uniform(0.7, 1.25))
    e.setAttrib(_flame_blend())
    e.setDepthWrite(False)
    if engulf:
        e.setDepthTest(False)
        e.setBin('fixed', 30)
    else:
        e.setBin('transparent', 20)
    vel = Vec3(r.uniform(-0.35, 0.35), rise * r.uniform(0.75, 1.3),
               r.uniform(-0.35, 0.35))
    _flames.append([e, 0.0, life * r.uniform(0.75, 1.3), vel, e.scale_x])


def _update_flames():
    """Age every tongue: it rises, swells, cools from white through orange to a
    dull red, and fades out."""
    dt = time.dt
    cam = camera.world_position
    for f in _flames[:]:
        f[1] += dt
        t = f[1] / f[2]
        if t >= 1.0:
            destroy(f[0])
            _flames.remove(f)
            continue
        e, vel = f[0], f[3]
        e.position += vel * dt
        f[3] = Vec3(vel.x * 0.94, vel.y * 0.97 + 1.2 * dt, vel.z * 0.94)
        # fat in the middle of its life, thin at both ends
        e.scale = f[4] * (0.55 + 1.5 * math.sin(math.pi * min(1.0, t ** 0.75)))
        if t < 0.30:                       # white-hot
            k = t / 0.30
            col = (1.0, 0.97 - 0.22 * k, 0.82 - 0.50 * k)
        elif t < 0.68:                     # orange
            k = (t - 0.30) / 0.38
            col = (1.0 - 0.10 * k, 0.75 - 0.42 * k, 0.32 - 0.26 * k)
        else:                              # falling to embers
            k = (t - 0.68) / 0.32
            col = (0.90 - 0.55 * k, 0.33 - 0.28 * k, 0.06)
        e.color = color.rgba(col[0], col[1], col[2], (1.0 - t) ** 1.3)
        e.look_at(cam)


def _start_fire(x, z, life=None):
    """Light a patch of floor. It burns for a few seconds and will catch Diamond
    if it walks through, so a missed throw is not always a wasted one."""
    _fires.append({'x': x, 'z': z, 'life': life if life is not None else
                   random.uniform(*FIRE_FLOOR_LIFE), 'spawn': 0.0})
    _play_sfx('tick', volume=0.35, rate=0.7)


def _update_fires():
    """Feed every floor fire new flame, and burn whatever stands in one."""
    dt = time.dt
    for fire in _fires[:]:
        fire['life'] -= dt
        if fire['life'] <= 0:
            _fires.remove(fire)
            continue
        fade = min(1.0, fire['life'] / 1.2)         # dies down at the end
        fire['spawn'] -= dt
        if fire['spawn'] <= 0:
            fire['spawn'] = random.uniform(0.03, 0.07)
            _spawn_flame(fire['x'], 0.15, fire['z'],
                         scale=fire.get('scale', 1.5) * fade, life=0.55, rise=3.0,
                         spread=fire.get('spread', 0.5))
        if (not fire.get('harmless') and _entity is not None and _entity.enabled
                and _diamond_phase == 'chasing'
                and math.hypot(_entity.x - fire['x'], _entity.z - fire['z']) < FIRE_CATCH_R):
            _ignite_diamond()


def _throw_lighter():
    """Throw a lit lighter along your view. _use_selected has already taken it
    out of the slot."""
    fwd = Vec3(*camera.forward).normalized()
    start = camera.world_position + fwd * 0.9 + Vec3(0, -0.25, 0)
    e = Entity(model='cube', shader=OBJ_SHADER, color=col255(206, 166, 58),
               position=start, scale=(0.17, 0.32, 0.12),
               rotation=(random.uniform(0, 360), random.uniform(0, 360), 0))
    _thrown.append({'e': e, 'v': fwd * THROW_SPEED + Vec3(0, 2.0, 0),
                    'spin': Vec3(*[random.uniform(-600, 600) for _ in range(3)]),
                    'age': 0.0})
    _play_sfx('step', volume=0.4, rate=1.8)
    print('[fire] lighter thrown')


def _update_thrown():
    """Fly the thrown lighters, and see what they land on."""
    dt = time.dt
    for th in _thrown[:]:
        e = th['e']
        th['age'] += dt
        th['v'] = Vec3(th['v'].x, th['v'].y - THROW_GRAVITY * dt, th['v'].z)
        e.position += th['v'] * dt
        e.rotation += th['spin'] * dt
        th['trail'] = th.get('trail', 0.0) + dt * 40.0
        while th['trail'] >= 1.0:                 # a lit flame tumbling through
            th['trail'] -= 1.0
            _spawn_flame(e.x, e.y, e.z, scale=0.30, life=0.20, rise=0.4,
                         spread=0.06, engulf=True)

        hit_entity = (_entity is not None and _entity.enabled
                      and _diamond_phase in ('chasing', 'cutscene')
                      and math.hypot(e.x - _entity.x, e.z - _entity.z) < THROW_HIT_R
                      and -0.5 < e.y < 6.5)
        landed = e.y <= 0.08 or cell_is_wall(*world_cell(e.x, e.z)) or th['age'] > 4.0
        if not (hit_entity or landed):
            continue
        _thrown.remove(th)
        x, z = e.x, e.z              # read it BEFORE the node goes away
        destroy(e)
        if hit_entity:
            _ignite_diamond()
        else:
            _start_fire(x, z)


def _ignite_diamond():
    """It burns. The chase stops dead, it thrashes, goes down, and stays down a
    good deal longer than usual before it digs back up."""
    global _diamond_phase, _entity_chasing, _burn_t, _glitch_spike, _fear, _burn
    if _entity is None or _diamond_phase == 'burning':
        return
    if _cutscene is not None:              # hit while still climbing out
        _cleanup_cutscene()
        _entity.y = 0.0
    _stop_sfx('chase')
    _entity_chasing = False
    _diamond_phase = 'burning'
    _burn_t = 0.0
    _glitch_spike = 1.0
    _fear = max(_fear, 0.7)
    away = Vec3(_entity.x - player.x, 0, _entity.z - player.z)
    away = away.normalized() if away.length() > 1e-4 else Vec3(0, 0, 1)
    _burn = {'from': (_entity.x, _entity.z), 'away': away,
             'yaw0': _entity.rotation_y, 'stumble': Vec3(0, 0, 0),
             'stumble_t': 0.5, 'hit': False, 'embers': False, 'smoke': 0.0,
             'scorch': None}
    _play_sfx('burn', volume=1.0)
    for _ in range(26):                    # the lighter bursts on impact
        _spawn_flame(_entity.x, 3.2, _entity.z, scale=0.28, life=0.35,
                     rise=random.uniform(3, 9), spread=0.9, engulf=True)
    print('[fire] Diamond is burning')


# ---- the burn, as a timeline in seconds -------------------------------------
# 0.00  knocked back a stride, arms thrown up, head back
# 0.45  thrashes: clawing at itself, head whipping, staggering in circles, the
#       fire climbing from its chest to its head
# 1.95  its knees go
# 2.25  it falls forward, reaching toward you, and hits the floor at 2.45
# 2.50  lies burning; one last spasm at 2.9; the light in its eyes goes out
# 3.40  burns down to embers and smoke, and is gone; a scorch mark stays
BURN_RECOIL = 0.45
BURN_BUCKLE = 1.95
BURN_FALL = 2.25
BURN_HIT = 2.45
BURN_FADE = (3.4, 4.2)
_burn = None


def _burn_points(rig):
    """Every bone as a (start, end, height-rank) segment in world space, so the
    fire sits ON the body -- and falls with it -- instead of hanging in a
    column where it used to stand."""
    segs = []
    for side in (-1, 1):
        lg, ar = rig['legs'][side], rig['arms'][side]
        segs += [(lg['hip'], lg['knee'], 0.30), (lg['knee'], lg['ankle'], 0.10),
                 (ar['shoulder'], ar['elbow'], 0.72), (ar['elbow'], ar['wrist'], 0.62)]
    segs += [(rig['spine'], rig['chest'], 0.50), (rig['chest'], rig['neck'], 0.70),
             (rig['neck'], rig['head'], 0.86)]
    out = []
    for a, b, h in segs:
        out.append((a.getPos(scene), b.getPos(scene), h))
    head = rig['head']
    out.append((head.getPos(scene), scene.getRelativePoint(head, Vec3(0, 0.75, 0)), 1.0))
    return out


def _pose_burn(t):
    """The whole rig, frame by frame, for time t of the burn."""
    e = _entity
    rig = e._rig
    b = _burn
    recoil = _smoother(t / BURN_RECOIL)
    thrash = _smoother((t - BURN_RECOIL) / 0.25) * (1 - _smoother((t - BURN_BUCKLE) / 0.3))
    buckle = _smoother((t - BURN_BUCKLE) / (BURN_FALL - BURN_BUCKLE))
    fallk = max(0.0, min(1.0, (t - BURN_FALL) / (BURN_HIT - BURN_FALL)))
    fall = fallk * fallk                                  # accelerates into the floor
    down = t >= BURN_HIT

    # --- where it stands: shoved back, then staggering about ---
    push = 1.4 * (1 - (1 - recoil) ** 3)
    base_x = b['from'][0] + b['away'].x * push
    base_z = b['from'][1] + b['away'].z * push
    b['stumble_t'] -= time.dt
    if thrash > 0.3 and b['stumble_t'] <= 0:              # lurch somewhere new
        b['stumble_t'] = random.uniform(0.3, 0.55)
        a = random.uniform(0, math.tau)
        tgt = b['stumble'] + Vec3(math.cos(a), 0, math.sin(a)) * random.uniform(0.3, 0.7)
        if tgt.length() > 1.4:
            tgt = tgt.normalized() * 1.4
        if not _entity_body_blocked(base_x + tgt.x, base_z + tgt.z):
            b['goal'] = tgt
    g = b.get('goal', b['stumble'])
    b['stumble'] += (g - b['stumble']) * min(1.0, time.dt * 7.0)
    nx, nz = base_x + b['stumble'].x, base_z + b['stumble'].z
    if not _entity_body_blocked(nx, nz):
        e.x, e.z = nx, nz
    e.y = 0.0

    # --- which way it faces: at you, then spinning, blind ---
    face = math.degrees(math.atan2(player.x - e.x, player.z - e.z))
    spin = (math.sin(t * 2.3) * 70 + math.sin(t * 5.9) * 25) * thrash
    want = face + spin
    diff = (want - e.rotation_y + 180.0) % 360.0 - 180.0
    e.rotation_y += diff * min(1.0, time.dt * (4.0 if thrash else 2.0))

    # --- the whole body: whipping side to side, then pitching over ---
    e.rotation_z = (math.sin(t * 11.0) * 7 + math.sin(t * 17.0) * 4) * thrash
    bounce = 5.0 * math.sin(min(1.0, (t - BURN_HIT) / 0.25) * math.pi) if down else 0.0
    e.rotation_x = 82.0 * fall - bounce

    # --- torso ---
    spine = _lerp(20, -8, recoil * (1 - thrash) * (1 - buckle))  # arched back on impact
    spine = _lerp(spine, 12 + 14 * math.sin(t * 6.1), thrash)    # doubling up
    spine = _lerp(spine, 36, buckle)                             # hunched on its knees
    chest = _lerp(17, -10, recoil * (1 - thrash)) + (math.sin(t * 7.3) * 12) * thrash
    chest = _lerp(chest, 24, buckle)
    rig['spine'].rotation_x = spine
    rig['chest'].rotation_x = chest
    rig['chest'].rotation_y = math.sin(t * 8.7) * 22 * thrash
    rig['body'].rotation_z = math.sin(t * 9.4) * 9 * thrash
    lean = spine + chest

    # --- legs: staggering, then folding to kneel, then trailing as it falls ---
    heights = []
    for side in (-1, 1):
        leg = rig['legs'][side]
        stagger = math.sin(t * 8.0 + (0 if side > 0 else math.pi))
        thigh = -4 + stagger * 26 * thrash
        knee = 10 + max(0.0, -stagger) * 50 * thrash
        thigh = _lerp(thigh, 2, buckle)
        knee = _lerp(knee, 88, buckle)
        knee = _lerp(knee, 34, fall)                   # legs trail out behind
        leg['hip'].rotation_x = thigh
        leg['knee'].rotation_x = knee
        leg['ankle'].rotation_x = -(thigh + knee) * 0.7
        heights.append(ANKLE_H + THIGH_LEN * math.cos(math.radians(thigh))
                       + SHIN_LEN * math.cos(math.radians(thigh + knee)))
    rig['body'].y = min(heights)

    # --- arms: flung up, clawing at its own head, then one reaches for you ---
    for side in (-1, 1):
        arm = rig['arms'][side]
        ph = t * (6.5 if side > 0 else 7.9) + (0 if side > 0 else 1.7)
        flail_x = -95 + math.sin(ph) * 75                # windmilling
        flail_z = -side * (35 + math.sin(ph * 1.3) * 30)
        flail_e = -60 + math.sin(ph * 1.7) * 45
        clutch = max(0.0, math.sin(t * 1.9 + side)) ** 3  # hands go to its head
        flail_x = _lerp(flail_x, -158, clutch)
        flail_e = _lerp(flail_e, -115, clutch)
        s_x = _lerp(-12, -150, recoil)
        s_z = -side * _lerp(8, 42, recoil)
        s_e = _lerp(-20, -25, recoil)
        s_x = _lerp(s_x, flail_x, thrash)
        s_z = _lerp(s_z, flail_z, thrash)
        s_e = _lerp(s_e, flail_e, thrash)
        if side == 1:                                    # the reaching arm
            reach_x, reach_z, reach_e = -lean - 70, -side * 6, -8
        else:                                            # the other clutches its chest
            reach_x, reach_z, reach_e = -40, -side * 4, -120
        s_x = _lerp(s_x, reach_x, buckle)
        s_z = _lerp(s_z, reach_z, buckle)
        s_e = _lerp(s_e, reach_e, buckle)
        if side == 1 and down:                           # one last spasm
            k = max(0.0, 1.0 - abs(t - 2.95) / 0.18)
            s_x -= 38 * k
            s_e -= 30 * k
        arm['shoulder'].rotation_x = s_x
        arm['shoulder'].rotation_z = s_z
        arm['elbow'].rotation_x = s_e
        arm['wrist'].rotation_x = -20 - 25 * thrash * math.sin(ph * 2.3)

    # --- head: thrown back, whipping, then hanging, then still ---
    whip = (math.sin(t * 9.1) * 45 + math.sin(t * 15.3) * 15) * thrash
    rig['neck'].rotation_x = _lerp(_lerp(40, 5, recoil * (1 - thrash)), 50, buckle)
    head_x = _lerp(10, -40, recoil * (1 - thrash)) + math.sin(t * 7.7) * 25 * thrash
    rig['head'].rotation_x = _lerp(head_x, 20, buckle)
    rig['head'].rotation_y = whip * (1 - buckle)
    rig['head'].rotation_z = 11 + math.sin(t * 13.0) * 20 * thrash

    # --- its eyes: flare with the pain, then go out ---
    glow = _lerp(4.5, 9.0, recoil) * (1 - _smoother((t - 2.8) / 0.5))
    for eye in getattr(e, '_eyes', ()):
        eye.set_shader_input('obj_emit', glow)


def _update_burning():
    """Drive the burn: pose, fire on the body, the fall, smoke, and the end."""
    global _burn_t, _diamond_phase, _spawn_t, _black, _burn_spawn, _burn
    _burn_t += time.dt
    t = _burn_t
    e = _entity
    b = _burn
    _pose_burn(t)

    # --- fire: on the bones, spreading out from where the lighter hit ---
    spread = 0.12 + 0.9 * _smoother(t / 1.4)
    alive = 1.0 - _smoother((t - BURN_FADE[0]) / (BURN_FADE[1] - BURN_FADE[0]))
    segs = [s for s in _burn_points(e._rig) if abs(s[2] - 0.55) <= spread]
    rate = 95.0 * (0.35 + 0.65 * alive)
    _burn_spawn += time.dt * rate
    while _burn_spawn >= 1.0 and segs:
        _burn_spawn -= 1.0
        a, c, _h = random.choice(segs)
        k = random.random()
        pt = a + (c - a) * k
        _spawn_flame(pt.x, max(0.08, pt.y), pt.z,
                     scale=(0.95 if alive > 0.5 else 0.6) * (0.45 + 0.55 * alive),
                     life=0.5 * (0.6 + 0.4 * alive), rise=3.2 * (0.5 + 0.5 * alive),
                     spread=0.22, engulf=True)

    # --- it hits the floor: a burst of sparks and the scorch mark ---
    if t >= BURN_HIT and not b['hit']:
        b['hit'] = True
        for _ in range(34):
            pts = _burn_points(e._rig)
            a, c, _h = random.choice(pts)
            pt = a + (c - a) * random.random()
            _spawn_flame(pt.x, max(0.1, pt.y), pt.z, scale=0.25, life=0.4,
                         rise=random.uniform(4, 10), spread=0.5, engulf=True)
        _black = max(_black, 0.35)
        b['scorch'] = _make_scorch(e)

    # --- smoke off it once it is down, thickening as the flames die ---
    if t > BURN_BUCKLE:
        b['smoke'] += time.dt * (6.0 + 10.0 * (1 - alive))
        pts = _burn_points(e._rig)
        while b['smoke'] >= 1.0:
            b['smoke'] -= 1.0
            a, c, _h = random.choice(pts)
            pt = a + (c - a) * random.random()
            _spawn_smoke(pt.x, max(0.3, pt.y + 0.4), pt.z)

    # --- burning down: the body goes, embers are left ---
    if t > BURN_FADE[0]:
        _set_entity_alpha(max(0.0, alive))
    if t > BURN_FADE[0] + 0.3 and not b['embers']:
        b['embers'] = True
        a, c, _h = _burn_points(e._rig)[8]               # the spine, where it lies
        mid = (a + c) * 0.5
        _fires.append({'x': mid.x, 'z': mid.z, 'life': 3.2, 'spawn': 0.0,
                       'scale': 0.55, 'spread': 1.4, 'harmless': True})

    if t >= BURN_TIME:
        _restore_entity_look()
        e.enabled = False
        e.y = 0.0
        _diamond_phase = 'buried'
        _spawn_t = random.uniform(*BURN_RESPAWN)
        _burn = None
        print(f'[fire] it is gone -- re-digs in {_spawn_t:.1f}s')


def _set_entity_alpha(a):
    """Fade the whole body. Goes through the shader's own obj_fade input: the
    colour scale never reached Diamond's parts, so fading it that way did
    nothing at all."""
    from panda3d.core import TransparencyAttrib
    _entity.setTransparency(TransparencyAttrib.M_alpha)
    _entity.set_shader_input('obj_fade', max(0.0, min(1.0, a)))


def _restore_entity_look():
    """Undo everything the burn did to the body, ready for its next dig-out."""
    from panda3d.core import TransparencyAttrib
    e = _entity
    if e is None:
        return
    e.clearColorScale()
    e.set_shader_input('obj_fade', 1.0)
    e.setTransparency(TransparencyAttrib.M_none)
    e.rotation_x = e.rotation_z = 0.0
    for eye in getattr(e, '_eyes', ()):
        eye.set_shader_input('obj_emit', 4.5)


# ---- smoke and scorch marks ----------------------------------------------------
MAX_SMOKE = 70
_smoke = []                 # [entity, age, life, velocity, base_scale]
_scorches = []              # [entity, age, life]


def _spawn_smoke(x, y, z):
    if len(_smoke) >= MAX_SMOKE:
        return
    r = random
    e = Entity(model='quad', texture=_get_puff_texture(), double_sided=True,
               position=(x + r.uniform(-0.3, 0.3), y, z + r.uniform(-0.3, 0.3)),
               color=color.rgba(0.08, 0.075, 0.07, 0.0), scale=r.uniform(0.9, 1.6))
    e.setDepthWrite(False)
    e.setBin('transparent', 15)
    _smoke.append([e, 0.0, r.uniform(2.2, 3.4),
                   Vec3(r.uniform(-0.3, 0.3), r.uniform(1.2, 2.2), r.uniform(-0.3, 0.3)),
                   e.scale_x])


def _update_smoke():
    dt = time.dt
    cam = camera.world_position
    for s in _smoke[:]:
        s[1] += dt
        f = s[1] / s[2]
        if f >= 1.0:
            destroy(s[0])
            _smoke.remove(s)
            continue
        e = s[0]
        e.position += s[3] * dt
        s[3] = Vec3(s[3].x * 0.99, s[3].y * 0.985, s[3].z * 0.99)
        e.scale = s[4] * (1.0 + 2.2 * f)                 # billows out as it rises
        e.color = color.rgba(0.08, 0.075, 0.07, 0.62 * min(1.0, f * 5) * (1.0 - f) ** 1.5)
        e.look_at(cam)


def _make_scorch(e):
    """A burnt shape on the carpet where it fell, running from its knees
    out along where its body lies."""
    rr = random.Random()
    yaw = math.radians(e.rotation_y)
    fx, fz = math.sin(yaw), math.cos(yaw)
    cx, cz = e.x + fx * 2.6, e.z + fz * 2.6
    s = Entity(model=_blob_mesh(1.0, rr, points=24, wobble=0.35),
               color=color.rgba(0.02, 0.016, 0.01, 0.0), double_sided=True,
               position=(cx, 0.02, cz), rotation=(90, e.rotation_y, 0),
               scale=(1.5, 3.4, 1))
    _scorches.append([s, 0.0, 30.0])
    return s


def _update_scorches():
    dt = time.dt
    for s in _scorches[:]:
        s[1] += dt
        a = min(1.0, s[1] / 1.2) * min(1.0, (s[2] - s[1]) / 6.0)
        if s[1] >= s[2]:
            destroy(s[0])
            _scorches.remove(s)
            continue
        s[0].alpha = 0.88 * a


def _clear_fire():
    """Throw away every flame, fire, lighter in flight, puff of smoke and scorch
    mark (new run / menu)."""
    global _burn_t, _burn_spawn, _burn
    _burn_spawn = 0.0
    for f in _flames:
        destroy(f[0])
    for th in _thrown:
        destroy(th['e'])
    for s in _smoke:
        destroy(s[0])
    for s in _scorches:
        destroy(s[0])
    _flames.clear()
    _thrown.clear()
    _fires.clear()
    _smoke.clear()
    _scorches.clear()
    _burn_t = 0.0
    _burn = None


# ============================================================================
#  DEATH
# ============================================================================
# Diamond reaching you finally means something. The camera is taken and dragged
# round onto it, it comes the rest of the way in, and the screen goes out.

_death_t = 0.0
_death_faded = False
_run_time = 0.0             # how long this run has lasted
_death_time_text = None
_death_best_text = None
_death_note = None

_DEATH_LINES = (
    'it was already under you',
    'you were never alone down here',
    'the floor was only ever a lid',
    'it heard you the whole time',
    'there was no exit to find',
)


def _fmt_time(seconds):
    return f'{int(seconds) // 60}:{int(seconds) % 60:02d}'


def _begin_death():
    """It has you. Freeze the player, take the camera, and start the sequence."""
    global _game_state, _death_t, _death_faded
    if _game_state != 'playing':
        return
    _game_state = 'dying'
    _death_t = 0.0
    _death_faded = False
    global _entity_chasing
    _stop_sfx('chase')
    _entity_chasing = False          # stop the chase blackouts hiding the moment
    _set_hud_visible(False)          # no crosshair over your own death
    _play_sfx('caught', volume=1.0)
    _play_sfx('sting', volume=0.8)
    print(f'[death] caught after {_run_time:.1f}s')


def _update_death():
    """Drag the view onto it while it closes the last stride, then cut to black."""
    global _death_t, _death_faded, _fear, _glitch_spike, _black
    _death_t += time.dt
    # a held, moderate distortion rather than the full chase corruption: at 1.0
    # the grade crushes the picture to black and you cannot see what killed you
    _fear = 0.5
    _glitch_spike = 0.3
    _black = 0.0

    if _entity is not None and _entity.enabled:
        # your head is turned onto it whether you like it or not
        to = Vec3(_entity.x - player.x, 0, _entity.z - player.z)
        if to.length() > 1e-4:
            want = math.degrees(math.atan2(to.x, to.z))
            diff = (want - player.rotation_y + 180.0) % 360.0 - 180.0
            player.rotation_y += diff * min(1.0, time.dt * 7.0)
        # aim at its head, not its shins: it stands nearly twice your height,
        # so at this range the pitch has to swing a long way up
        d = max(0.6, distance_xz(_entity, player))
        head_y = _entity.y + ENTITY_HEAD_Y * ENTITY_SCALE
        want = -math.degrees(math.atan2(head_y - camera.world_position.y, d))
        pivot = player.camera_pivot
        pivot.rotation_x += (max(-78.0, want) - pivot.rotation_x) * min(1.0, time.dt * 5.0)
        # and it comes the rest of the way in, filling the screen
        if d > DEATH_STOP:
            step = min(6.0 * time.dt, d - DEATH_STOP)
            _entity.x += (player.x - _entity.x) / d * step
            _entity.z += (player.z - _entity.z) / d * step
        _face_entity_towards(player.x, player.z)
        _animate_entity(0.02)
        rig = getattr(_entity, '_rig', None)
        if rig is not None:
            k = min(1.0, _death_t / 0.8)
            rig['spine'].rotation_x = 20 - 18 * k     # straightens up over you
            rig['neck'].rotation_x = 40 - 18 * k
            rig['head'].rotation_x += 34 * k          # and cranes its face down

    if not _death_faded and _death_t >= DEATH_TIME * 0.55:
        _death_faded = True
        _fade_to(1.0, then=_show_death_card, rate=1.5)


def _show_death_card():
    """Black, then the card: how long you lasted, and what to do about it."""
    global _game_state, _entity_chasing, _diamond_phase, _fear, _glitch, _black
    best = float(_settings.get('best_time', 0.0))
    if _run_time > best:
        best = _run_time
        _settings['best_time'] = float(best)
        _save_settings()
    _game_state = 'dead'
    _entity_chasing = False
    _diamond_phase = 'buried'
    if _entity is not None:
        _entity.enabled = False
    _clear_fire()
    _fear = _glitch = _black = 0.0
    _set_hud_visible(False)
    _set_mouse_locked(False)
    camera.set_shader_input('blur_size', 0.0)
    if _death_time_text is not None:
        _death_time_text.text = f'YOU LASTED  {_fmt_time(_run_time)}'
        _death_best_text.text = f'best  {_fmt_time(best)}'
        _death_note.text = random.choice(_DEATH_LINES)
    _show_screen('death')
    _menu_ui.enabled = True
    _fade_to(0.0, rate=1.7)


def build_crosshair():
    """A Minecraft-style '+' crosshair. It doesn't use a fixed colour: it blends
    as (1 - whatever is behind it), so it inverts the framebuffer and stays
    readable against both the pale walls and the black fog."""
    global _crosshair
    _crosshair = Entity(parent=camera.ui)
    invert = ColorBlendAttrib.make(ColorBlendAttrib.M_add,
                                   ColorBlendAttrib.O_one_minus_fbuffer_color,
                                   ColorBlendAttrib.O_zero)
    # one horizontal bar + one vertical bar. x and y units are the same size on
    # screen here, so these come out square-armed (~16x3 px at 720p).
    for sx, sy in ((CROSSHAIR_LEN, CROSSHAIR_THICK),
                   (CROSSHAIR_THICK, CROSSHAIR_LEN)):
        arm = Entity(parent=_crosshair, model='quad', scale=(sx, sy),
                     color=color.white)
        arm.setAttrib(invert)


# ============================================================================
#  FRONT END: title screen, settings, controls, pause
# ============================================================================
# One overlay drives all four screens, so they share a look and both the mouse
# and the keyboard work everywhere. Rows are data, not hand-placed widgets:
# each one says what it edits and the renderer draws it.

SETTINGS_FILE = 'settings.json'
MENU_VERSION = 'build 1.0'

_BONE = (232, 226, 198)        # the text you read
_DIM = (124, 118, 94)          # labels that should sit back
_HOT = (255, 247, 220)         # the row you are on
_ACCENT = (206, 178, 96)       # marker + rules, the sickly fluorescent yellow
_DANGER = (188, 96, 82)

_menu_ui = None                # root of the whole front end
_menu_screen = 'main'
_menu_index = 0
_menu_screens = {}             # name -> Entity
_menu_rows = {}                # name -> list of row dicts
_menu_title = []               # title texts, for the flicker
_menu_t = 0.0
_settings_back_to = 'main'     # where BACK returns from SETTINGS
_game_state = 'menu'           # 'menu' while the front end owns the world
_volume = 0.85
_fx_on = True

_DEFAULTS = {
    'sensitivity': 68, 'fov': 110, 'draw_distance': 115, 'volume': 85,
    'screen_effects': True, 'window_shake': True, 'hunt': 'NORMAL',
    'fullscreen': False, 'best_time': 0.0,
}
_settings = dict(_DEFAULTS)

# speed / chase length / time buried, per difficulty
HUNT_PRESETS = {
    'CALM':       (9.0, 15.0, 22.0, 14.0, 24.0),
    'NORMAL':     (11.0, 20.0, 30.0, 9.0, 16.0),
    'RELENTLESS': (15.0, 26.0, 38.0, 5.0, 9.0),
}


def _c(rgb, a=1.0):
    return color.rgba(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, a)


def _settings_path():
    import os
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), SETTINGS_FILE)


def _load_settings():
    """Read settings.json if it is there; anything missing keeps its default."""
    import json
    import os
    global _settings
    _settings = dict(_DEFAULTS)
    path = _settings_path()
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            for k, v in saved.items():
                if k in _settings and isinstance(v, type(_settings[k])):
                    _settings[k] = v
        except Exception as e:
            print(f'[settings] could not read {path}: {e}')


def _save_settings():
    import json
    try:
        with open(_settings_path(), 'w', encoding='utf-8') as f:
            json.dump(_settings, f, indent=2)
    except Exception as e:
        print(f'[settings] could not save: {e}')


def apply_settings():
    """Push every setting into the thing it actually controls."""
    global FOV, FOG_END, DEBUG_SHAKE_ON, _volume, _fx_on
    global ENTITY_CHASE_SPEED, CHASE_MIN, CHASE_MAX, RESPAWN_MIN, RESPAWN_MAX
    s = _settings
    if player is not None:
        player.mouse_sensitivity = Vec2(s['sensitivity'], s['sensitivity'])
    FOV = s['fov']
    camera.fov = FOV
    FOG_END = float(s['draw_distance'])
    scene.set_shader_input('fog_end', FOG_END if DEBUG_FOG_ON else 1e6)
    camera.clip_plane_far = FOG_END + 25
    _volume = s['volume'] / 100.0
    _fx_on = bool(s['screen_effects'])
    DEBUG_SHAKE_ON = bool(s['window_shake'])
    ENTITY_CHASE_SPEED, CHASE_MIN, CHASE_MAX, RESPAWN_MIN, RESPAWN_MAX = \
        HUNT_PRESETS.get(s['hunt'], HUNT_PRESETS['NORMAL'])
    for name in ('sting', 'rumble', 'chase'):
        snd = _sfx.get(name)
        if snd is not None and name != 'chase':
            snd.setVolume(_volume)
    if bool(s['fullscreen']) != bool(window.fullscreen):
        _sync_window_properties()
        window.fullscreen = bool(s['fullscreen'])


# ---- rows --------------------------------------------------------------------

def _row_value(row):
    if row['kind'] == 'bool':
        return 'ON' if _settings[row['key']] else 'OFF'
    if row['kind'] == 'choice':
        return str(_settings[row['key']])
    if row['kind'] == 'num':
        return f"{int(_settings[row['key']])}{row.get('suffix', '')}"
    return ''


def _row_adjust(row, delta):
    """Left/right on a row: nudge a number, flip a switch, cycle a choice."""
    k = row['kind']
    if k == 'num':
        v = _settings[row['key']] + delta * row['step']
        _settings[row['key']] = max(row['min'], min(row['max'], v))
    elif k == 'bool':
        _settings[row['key']] = not _settings[row['key']]
    elif k == 'choice':
        opts = row['options']
        _settings[row['key']] = opts[(opts.index(_settings[row['key']]) + delta) % len(opts)]
    else:
        return False
    apply_settings()
    _save_settings()
    _refresh_rows()
    _play_sfx('ui_move', volume=0.5)
    return True


def _row_activate(row):
    if row['kind'] == 'action':
        _play_sfx('ui_select', volume=0.8)
        row['do']()
    else:
        _row_adjust(row, 1)


def _row_click(screen, i):
    if _fading():
        return
    _menu_select(screen, i, quiet=True)
    _row_activate(_menu_rows[screen][i])


def _menu_select(screen, i, quiet=False):
    global _menu_index
    if screen != _menu_screen or i == _menu_index:
        return
    _menu_index = i
    _refresh_rows()
    if not quiet:
        _play_sfx('ui_move', volume=0.35)


def _build_rows(parent, screen, rows, y0, step=0.082):
    _menu_rows[screen] = rows
    for i, row in enumerate(rows):
        y = y0 - i * step
        centred = row.get('centre', row['kind'] == 'action')
        btn = Button(parent=parent, model='quad', scale=(0.98, step * 0.9), y=y, z=0.02,
                     color=_c(_HOT, 0.0), highlight_color=_c(_HOT, 0.07),
                     pressed_color=_c(_HOT, 0.14))
        btn.on_click = Func(_row_click, screen, i)
        btn.on_mouse_enter = Func(_menu_select, screen, i)
        if centred:
            lab = Text(parent=parent, text=row['label'], origin=(0, 0), y=y,
                       scale=1.5, color=_c(_BONE))
            val = None
            mx = -0.135
        else:
            lab = Text(parent=parent, text=row['label'], origin=(-0.5, 0), x=-0.40,
                       y=y, scale=1.15, color=_c(_BONE))
            val = Text(parent=parent, text=_row_value(row), origin=(0.5, 0), x=0.42,
                       y=y, scale=1.15, color=_c(_ACCENT))
            mx = -0.44
        mark = Text(parent=parent, text='|', origin=(0.5, 0), x=mx, y=y, scale=1.5,
                    color=_c(_ACCENT), enabled=False)
        row['_lab'], row['_val'], row['_mark'] = lab, val, mark


def _refresh_rows():
    rows = _menu_rows.get(_menu_screen, [])
    for i, row in enumerate(rows):
        on = (i == _menu_index)
        base = _DANGER if row.get('danger') else _BONE
        row['_lab'].color = _c(_HOT if on else base)
        row['_mark'].enabled = on
        if row['_val'] is not None:
            row['_val'].text = _row_value(row)
            row['_val'].color = _c(_HOT if on else _ACCENT)


# ---- screens -----------------------------------------------------------------

def _show_screen(name):
    global _menu_screen, _menu_index
    _menu_screen = name
    _menu_index = 0
    for key, ent in _menu_screens.items():
        ent.enabled = (key == name)
    _refresh_rows()


def _open_settings(back_to):
    global _settings_back_to
    _settings_back_to = back_to
    _show_screen('settings')


def _settings_back():
    _show_screen('pause' if _settings_back_to == 'pause' else 'main')


def _menu_escape():
    if _menu_screen == 'pause':
        set_paused(False)
    elif _menu_screen == 'settings':
        _settings_back()
    elif _menu_screen == 'controls':
        _show_screen('main')


def _menu_input(key):
    """Keyboard driving for every screen. Returns True if it swallowed the key."""
    rows = _menu_rows.get(_menu_screen, [])
    if not rows:
        return False
    if key in ('up arrow', 'w'):
        _menu_select(_menu_screen, (_menu_index - 1) % len(rows))
    elif key in ('down arrow', 's'):
        _menu_select(_menu_screen, (_menu_index + 1) % len(rows))
    elif key in ('left arrow', 'a'):
        _row_adjust(rows[_menu_index], -1)
    elif key in ('right arrow', 'd'):
        _row_adjust(rows[_menu_index], 1)
    elif key in ('enter', 'space'):
        _row_activate(rows[_menu_index])
    elif key == 'escape':
        _menu_escape()
    else:
        return False
    return True


def build_menus():
    """Build the title screen, settings, controls, pause and death card, all in
    one overlay."""
    global _menu_ui, _death_time_text, _death_best_text, _death_note
    _menu_ui = Entity(parent=camera.ui, enabled=False)
    Entity(parent=_menu_ui, model='quad', scale=(2.4, 1.2), z=0.2,
           color=_c((6, 6, 8), 0.62))
    for sgn in (1, -1):          # letterbox, so it reads as a title card
        Entity(parent=_menu_ui, model='quad', scale=(2.4, 0.14), y=sgn * 0.5,
               z=0.15, color=_c((0, 0, 0), 1.0))

    def screen(name):
        ent = Entity(parent=_menu_ui, enabled=False)
        _menu_screens[name] = ent
        return ent

    def heading(parent, text, y=0.34, scale=2.6):
        Text(parent=parent, text=text, origin=(0, 0), y=y, scale=scale, color=_c(_BONE))
        Entity(parent=parent, model='quad', scale=(0.46, 0.003), y=y - 0.06,
               color=_c(_ACCENT, 0.55))

    def hint(parent, text):
        Text(parent=parent, text=text, origin=(0, 0), y=-0.435, scale=0.78,
             color=_c(_DIM))

    # --- title ---------------------------------------------------------------
    s = screen('main')
    title = Text(parent=s, text='HOLLOW FLOOR', origin=(0, 0), y=0.29, scale=4.0,
                 color=_c(_BONE))
    _menu_title.append(title)
    Text(parent=s, text='i t   i s   u n d e r   t h e   f l o o r', origin=(0, 0),
         y=0.19, scale=0.95, color=_c(_DIM))
    Entity(parent=s, model='quad', scale=(0.52, 0.003), y=0.135, color=_c(_ACCENT, 0.5))
    _build_rows(s, 'main', [
        {'kind': 'action', 'label': 'START', 'do': start_game},
        {'kind': 'action', 'label': 'SETTINGS', 'do': lambda: _open_settings('main')},
        {'kind': 'action', 'label': 'CONTROLS', 'do': lambda: _show_screen('controls')},
        {'kind': 'action', 'label': 'QUIT', 'do': application.quit, 'danger': True},
    ], y0=0.04)
    Text(parent=s, text=f'HOLLOW FLOOR  -  {MENU_VERSION}', origin=(-0.5, 0),
         x=-0.86, y=-0.44, scale=0.72, color=_c(_DIM))

    # --- settings ------------------------------------------------------------
    s = screen('settings')
    heading(s, 'SETTINGS')
    _build_rows(s, 'settings', [
        {'kind': 'num', 'label': 'MOUSE SENSITIVITY', 'key': 'sensitivity',
         'min': 10, 'max': 160, 'step': 4},
        {'kind': 'num', 'label': 'FIELD OF VIEW', 'key': 'fov',
         'min': 70, 'max': 130, 'step': 2},
        {'kind': 'num', 'label': 'DRAW DISTANCE', 'key': 'draw_distance',
         'min': 60, 'max': 200, 'step': 5},
        {'kind': 'num', 'label': 'MASTER VOLUME', 'key': 'volume',
         'min': 0, 'max': 100, 'step': 5, 'suffix': '%'},
        {'kind': 'bool', 'label': 'SCREEN EFFECTS', 'key': 'screen_effects'},
        {'kind': 'bool', 'label': 'WINDOW SHAKE', 'key': 'window_shake'},
        {'kind': 'choice', 'label': 'THE HUNT', 'key': 'hunt',
         'options': ['CALM', 'NORMAL', 'RELENTLESS']},
        {'kind': 'bool', 'label': 'FULLSCREEN', 'key': 'fullscreen'},
        {'kind': 'action', 'label': 'BACK', 'do': _settings_back, 'centre': True},
    ], y0=0.20, step=0.067)
    hint(s, 'ARROWS  change     ENTER  select     ESC  back')

    # --- controls ------------------------------------------------------------
    s = screen('controls')
    heading(s, 'CONTROLS')
    for i, (k, what) in enumerate((
            ('W A S D', 'move'), ('MOUSE', 'look around'), ('SHIFT', 'sprint'),
            ('SPACE', 'jump'), ('E', 'pick up  /  open backpack'),
            ('1-5  /  WHEEL', 'choose a slot'), ('LMB / F', 'throw a lighter / put down'),
            ('ESC', 'pause'), ('F1 / TAB', 'developer panel'))):
        y = 0.20 - i * 0.056
        Text(parent=s, text=k, origin=(0.5, 0), x=-0.04, y=y, scale=1.1, color=_c(_ACCENT))
        Text(parent=s, text=what, origin=(-0.5, 0), x=0.06, y=y, scale=1.1, color=_c(_BONE))
    _build_rows(s, 'controls', [
        {'kind': 'action', 'label': 'BACK', 'do': lambda: _show_screen('main')},
    ], y0=-0.33)
    hint(s, 'it hunts by sight - break the line and it loses you')

    # --- pause ---------------------------------------------------------------
    s = screen('pause')
    heading(s, 'PAUSED', y=0.30, scale=2.8)
    _build_rows(s, 'pause', [
        {'kind': 'action', 'label': 'RESUME', 'do': resume_game},
        {'kind': 'action', 'label': 'SETTINGS', 'do': lambda: _open_settings('pause')},
        {'kind': 'action', 'label': 'MAIN MENU', 'do': open_menu},
        {'kind': 'action', 'label': 'QUIT', 'do': application.quit, 'danger': True},
    ], y0=0.10)
    hint(s, 'ESC  resume')

    # --- death ---------------------------------------------------------------
    s = screen('death')
    Text(parent=s, text='IT FOUND YOU', origin=(0, 0), y=0.31, scale=3.2,
         color=_c(_DANGER))
    _death_note = Text(parent=s, text='', origin=(0, 0), y=0.235, scale=0.92,
                       color=_c(_DIM))
    Entity(parent=s, model='quad', scale=(0.52, 0.003), y=0.185,
           color=_c(_DANGER, 0.5))
    _death_time_text = Text(parent=s, text='', origin=(0, 0), y=0.125, scale=1.4,
                            color=_c(_BONE))
    _death_best_text = Text(parent=s, text='', origin=(0, 0), y=0.065, scale=0.85,
                            color=_c(_DIM))
    _build_rows(s, 'death', [
        {'kind': 'action', 'label': 'TRY AGAIN', 'do': start_game},
        {'kind': 'action', 'label': 'MAIN MENU', 'do': open_menu},
        {'kind': 'action', 'label': 'QUIT', 'do': application.quit, 'danger': True},
    ], y0=-0.02)
    hint(s, 'fire is the only thing down here it is afraid of')

    _show_screen('main')


# ---- state -------------------------------------------------------------------

def _set_mouse_locked(value):
    # sync first: re-locking the mouse re-submits ursina's window properties, and
    # a stale origin in there teleports the window (see _sync_window_properties)
    _sync_window_properties()
    mouse.locked = value
    if player is not None:
        player.cursor.visible = False


def _set_hud_visible(value):
    for ent in (_crosshair, _inv_ui, _bar_bg, _bar_fill):
        if ent is not None:
            ent.enabled = value
    if _prompt is not None:
        _prompt.enabled = False


def _reset_run():
    """Put the world back to a fresh start: spawn, no monster, clean screen."""
    global _diamond_phase, _spawn_t, _chase_t, _entity_chasing
    global _fear, _glitch, _glitch_spike, _black, _stamina, _sprint_locked
    global _run_time, _step_dist, _ent_step_dist, _step_px, _step_pz, _inventory
    _stop_sfx('chase')
    _clear_fire()
    _clear_dropped()
    _cleanup_cutscene()              # quit to the menu while it was digging out
    _clear_dig_scar()
    _restore_entity_look()
    _run_time = 0.0
    _step_dist = _ent_step_dist = 0.0
    _inventory = Inventory(INV_SLOTS)          # you start every run empty-handed
    global _sel, _bag_open
    _sel, _bag_open = 0, False
    build_inventory_ui()
    _entity_chasing = False
    _diamond_phase = 'buried'
    _spawn_t = INTRO_DELAY
    _chase_t = 0.0
    if _entity is not None:
        _entity.enabled = False
    _fear = _glitch = _glitch_spike = _black = 0.0
    _stamina, _sprint_locked = STAMINA_MAX, False
    if _entity is not None:
        _entity.rotation_x = _entity.rotation_z = 0.0
        _entity.y = 0.0
    player.position = (0, 2, 0)
    player.rotation_y = 0
    player.camera_pivot.rotation_x = 0
    player.vy = 0.0
    _step_px, _step_pz = player.x, player.z


def start_game():
    """START pressed: dip to black, then drop into the world."""
    if _fading():
        return
    _fade_to(1.0, then=_begin_run)


def _begin_run():
    global _game_state, _paused
    _reset_run()
    _game_state = 'playing'
    _paused = False
    _menu_ui.enabled = False
    _set_hud_visible(True)
    _set_mouse_locked(True)
    camera.set_shader_input('blur_size', 0.0)
    _fade_to(0.0, rate=1.6)          # and back up, into the room


def open_menu():
    """Back to the title screen, through black."""
    if _fading():
        return
    _fade_to(1.0, then=_do_open_menu)


def _do_open_menu():
    global _game_state, _paused
    _reset_run()
    _game_state = 'menu'
    _paused = False
    _set_hud_visible(False)
    _set_mouse_locked(False)
    camera.set_shader_input('blur_size', 0.0)
    _show_screen('main')
    _menu_ui.enabled = True
    _fade_to(0.0, rate=1.8)


def _update_menu():
    """Title-screen life: the sign flickers and the room turns slowly behind it."""
    global _menu_t
    _menu_t += time.dt
    if _menu_title:
        dip = 1.0
        if random.random() < 0.035:                  # a failing tube
            dip = random.uniform(0.15, 0.55)
        elif random.random() < 0.10:
            dip = random.uniform(0.75, 0.95)
        for title in _menu_title:
            title.color = _c(_BONE, max(0.25, dip))
    if _game_state == 'menu' and player is not None:
        player.rotation_y += 3.2 * time.dt           # a slow drift across the rooms
        player.camera_pivot.rotation_x = 4.0 + 1.6 * math.sin(_menu_t * 0.35)


# ---- loading screen and transitions ------------------------------------------
# Everything heavy (textures, audio, the spawn chunks, the monster's meshes) is
# built before the main loop starts, so nothing would be on screen during it.
# app.step() renders one frame on demand, which lets the bar actually move while
# the work happens. A single black quad over the top does every fade.

_loading = True                # true until the title screen takes over
_load_ui = None
_load_bar = None
_load_label = None
_load_pct = None
_fade_quad = None              # the black curtain, above everything
_fade_a = 0.0                  # where the curtain is now
_fade_to_a = 0.0               # where it is heading
_fade_rate = 2.6               # alpha per second
_fade_then = None              # called once it is fully down

LOAD_BAR_W = 0.62


def build_loading_screen():
    """The card you see first. Built before any of the slow work starts."""
    global _load_ui, _load_bar, _load_label, _load_pct, _fade_quad
    _load_ui = Entity(parent=camera.ui)
    Entity(parent=_load_ui, model='quad', scale=(2.6, 1.4), z=0.3, color=_c((5, 5, 6)))
    _menu_title.append(Text(parent=_load_ui, text='HOLLOW FLOOR', origin=(0, 0),
                            y=0.11, scale=2.8, color=_c(_BONE)))
    Text(parent=_load_ui, text='i t   i s   u n d e r   t h e   f l o o r',
         origin=(0, 0), y=0.045, scale=0.85, color=_c(_DIM))
    Entity(parent=_load_ui, model='quad', scale=(LOAD_BAR_W, 0.005), y=-0.04,
           color=_c(_DIM, 0.3))
    _load_bar = Entity(parent=_load_ui, model='quad', scale=(0.0001, 0.005), y=-0.04,
                       x=-LOAD_BAR_W / 2, origin=(-0.5, 0), color=_c(_ACCENT))
    _load_label = Text(parent=_load_ui, text='', origin=(0, 0), y=-0.09, scale=0.8,
                       color=_c(_DIM))
    _load_pct = Text(parent=_load_ui, text='0%', origin=(0.5, 0), x=LOAD_BAR_W / 2,
                     y=-0.09, scale=0.8, color=_c(_ACCENT))
    _fade_quad = Entity(parent=camera.ui, model='quad', scale=(2.6, 1.4), z=-0.8,
                        color=color.black, alpha=0)


def _loading_step(label, frac, app, frames=1):
    """Move the bar to `frac` and actually draw it. Called between build stages."""
    if _load_ui is None:
        return
    _load_label.text = label
    _load_pct.text = f'{int(frac * 100)}%'
    _load_bar.scale_x = max(0.0001, LOAD_BAR_W * min(1.0, frac))
    if _menu_title:                              # the sign is already failing
        dip = random.uniform(0.25, 0.6) if random.random() < 0.18 else 1.0
        _menu_title[0].color = _c(_BONE, dip)
    for _ in range(frames):
        app.step()


def _fade_manual(app, a_from, a_to, seconds):
    """Run a fade on our own frames -- used before the main loop exists."""
    global _fade_a, _fade_to_a
    acc = 0.0
    while acc < seconds:
        app.step()
        acc += max(time.dt, 1.0 / 240.0)
        _fade_a = a_from + (a_to - a_from) * min(1.0, acc / seconds)
        _fade_quad.alpha = _fade_a
    _fade_a = _fade_to_a = a_to
    _fade_quad.alpha = a_to


def _finish_loading(app):
    """Dip to black, drop the loading card, and come up on the title screen."""
    global _loading
    _fade_manual(app, 0.0, 1.0, 0.45)
    _load_ui.enabled = False
    _loading = False
    _do_open_menu()                              # raises the curtain again


def _fade_to(target, then=None, rate=2.6):
    global _fade_to_a, _fade_then, _fade_rate
    _fade_to_a = target
    _fade_then = then
    _fade_rate = rate


def _fading():
    """True mid-transition, so input is ignored until it lands."""
    return _fade_then is not None or _fade_a != _fade_to_a


def _update_fade():
    """Drive the curtain. It sits at 0 the whole time you are playing."""
    global _fade_a, _fade_then
    if _fade_quad is None:
        return
    if _fade_a != _fade_to_a:
        step = _fade_rate * time.dt
        _fade_a += max(-step, min(step, _fade_to_a - _fade_a))
        if abs(_fade_a - _fade_to_a) < 0.01:
            _fade_a = _fade_to_a
        _fade_quad.alpha = _fade_a
    elif _fade_then is not None:
        callback, _fade_then = _fade_then, None
        callback()


def set_paused(value):
    """Enter/leave the pause menu (one of the front-end screens)."""
    global _paused
    if _game_state != 'playing':
        return
    _paused = value
    _set_mouse_locked(not value)
    _set_hud_visible(not value and not _cutscene_active)   # nor over a cutscene
    if value:
        _show_screen('pause')
    _menu_ui.enabled = value
    # The camera shader stays applied (ursina can't remove one -- camera.py's
    # shader_setter returns early on None), so blur is toggled by its strength.
    camera.set_shader_input('blur_size', PAUSE_BLUR if value else 0.0)


def resume_game():
    set_paused(False)


def build_hud():
    """Create the sprint/stamina bar (Fortnite-style) near the bottom center."""
    global _bar_bg, _bar_fill
    # Both halves hang off a root at x=0. Whenever the aspect ratio changes
    # (maximize, fullscreen) ursina rescales the x of every DIRECT child of
    # camera.ui (window.py, update_aspect_ratio) -- that anchors things to the
    # screen edges. The background sits at x=0 so it never moved, but the fill
    # is anchored at its left end, x=-BAR_W/2, and got pushed out past the
    # background's edge. Under a root at 0 neither of them is touched.
    root = Entity(parent=camera.ui)
    # Starts hidden: at full stamina there's nothing to show, and _update_sprint
    # switches it on the moment you start sprinting or drop below full.
    _bar_bg = Entity(parent=root, model='quad', color=color.black,
                     scale=(BAR_W + 0.012, 0.03),
                     position=(0, -0.458), alpha=0.55, enabled=False)
    _bar_fill = Entity(parent=root, model='quad', color=color.white,
                       origin=(-0.5, 0), scale=(BAR_W, 0.022),
                       position=(-BAR_W / 2, -0.458), z=-0.1, enabled=False)


def _update_sprint():
    """Fortnite-style sprint: hold Shift while moving to go faster and drain
    stamina; empty = forced walk until it recharges past SPRINT_UNLOCK."""
    global _stamina, _sprint_locked, _regen_timer
    dt = time.dt

    moving = held_keys['w'] or held_keys['a'] or held_keys['s'] or held_keys['d']
    want_sprint = bool(held_keys['left shift'] or held_keys['right shift']) and moving
    sprinting = want_sprint and not _sprint_locked and _stamina > 0

    if sprinting:
        player.speed = SPRINT_SPEED
        _stamina = max(0.0, _stamina - STAMINA_DRAIN * dt)
        _regen_timer = STAMINA_REGEN_DELAY
        if _stamina <= 0:
            _sprint_locked = True          # must recharge before sprinting again
    else:
        player.speed = WALK_SPEED
        if _regen_timer > 0:
            _regen_timer -= dt
        else:
            _stamina = min(STAMINA_MAX, _stamina + STAMINA_REGEN * dt)
        if _sprint_locked and _stamina >= SPRINT_UNLOCK:
            _sprint_locked = False

    # --- HUD ---
    if _bar_fill:
        frac = _stamina / STAMINA_MAX
        # A permanently drawn full bar just reads as a stray white line across the
        # screen, so only show it while it actually says something: when you're
        # sprinting or still recharging.
        show = sprinting or frac < 0.999
        _bar_bg.enabled = show
        _bar_fill.enabled = show
        _bar_fill.scale_x = max(0.0001, BAR_W * frac)
        if _sprint_locked:
            _bar_fill.color = col255(210, 60, 45)      # red = locked out
        elif frac < 0.3:
            _bar_fill.color = col255(235, 150, 40)     # orange = low
        else:
            _bar_fill.color = col255(240, 236, 205)    # pale = ready
        # fade the bar out when full and not sprinting
        target = 1.0 if (frac < 0.999 or want_sprint) else 0.0
        _bar_fill.alpha = lerp(_bar_fill.alpha, target, min(1, 6 * dt))
        _bar_bg.alpha = lerp(_bar_bg.alpha, target * 0.55, min(1, 6 * dt))


def update():
    """Called every frame by Ursina."""
    global _run_time
    if _loading or player is None:
        return
    _update_fade()
    _update_title()
    # The fog shader needs the camera position. Set it ONCE here on the scene root
    # (all entities inherit it) instead of letting Ursina push it into every entity
    # every frame -- that per-entity loop was costing ~2000 python calls a frame.
    scene.set_shader_input('camera_world_position', camera.world_position)

    _update_atmosphere()             # shader clock + fear, even while paused
    _update_window_shake()           # also settles the window when paused
    if _game_state == 'menu':        # title screen: the room lives on behind it
        _update_menu()
        refresh_chunks(player.x, player.z)
        _refresh_look_grids()
        _process_build_queue()
        _update_lights()
        return
    if _paused:                      # freeze the world while the menu is open
        return
    refresh_chunks(player.x, player.z)
    _refresh_look_grids()
    _process_build_queue()
    if time.dt > 0.25:
        return          # a native dialog blocked us; skip this frame
                        # rather than let everything jump at once
    if _game_state in ('dying', 'dead'):
        if _game_state == 'dying':
            _update_death()
        _update_lights()
        _update_flames()
        _update_smoke()
        _update_scorches()
        return
    _run_time += time.dt             # how long you have lasted this run
    if not _cutscene_active:
        _unstick_player()
        _update_pickup()
        _update_footsteps()
    _update_inventory_ui()
    _update_diamond()
    if not _cutscene_active:
        _update_sprint()
    _update_lights()
    _update_entity()
    _update_thrown()
    _update_fires()
    _update_flames()
    _update_smoke()
    _update_scorches()
    _update_path_vis()
    # it has reached you
    if (_entity is not None and _entity.enabled and not _cutscene_active
            and _diamond_phase == 'chasing'
            and distance_xz(_entity, player) <= ENTITY_CATCH_DIST + 0.25):
        _begin_death()
    # Never fall through the world if a chunk unloads under you.
    if player.y < -5:
        player.position = (player.x, 3, player.z)


def input(key):
    global _dbg_open
    if _loading or _fading():
        return
    if key in ('f1', 'tab') and _dbg_ui is not None:
        # track open/closed ourselves rather than reading entity.enabled back --
        # round-tripping through it did not reliably flip the panel shut
        _dbg_open = not _dbg_open
        _dbg_ui.enabled = _dbg_open
        if _dbg_open:
            _dbg_refresh()
        return
    if _dbg_open and _dbg_input(key):
        return
    if _menu_ui is not None and _menu_ui.enabled:
        _menu_input(key)             # the front end owns every key while it is up
        return
    if key == 'escape' and player is not None:
        set_paused(not _paused)
    elif player is None or _paused or _game_state != 'playing':
        return
    elif _inventory_input(key):          # 1-0 and the wheel pick a slot
        return
    elif key == 'e':
        _interact()                      # pick up, or open/close the backpack
    elif key in ('left mouse down', 'f') and not _cutscene_active:
        _use_selected()


def main():
    global player
    # development_mode=False skips ursina's debug bookkeeping (hot reloader, entity
    # + collider counters) which we want for speed -- but it ALSO force-fullscreens
    # the window and hides the editor ui (which owns the fps counter). So ask for
    # windowed mode and the editor ui back explicitly.
    app = Ursina(
        title=GAME_TITLE,
        borderless=False,
        development_mode=False,
        fullscreen=False,
        size=WINDOW_SIZE,
        editor_ui_enabled=True,     # keeps the fps counter visible
    )
    window.color = FOG_COLOR                 # black void where nothing has rendered
    window.exit_button.visible = False

    # Fog inputs live on the SCENE ROOT, not on each entity: panda3d inherits
    # shader inputs down the graph, so one assignment covers every chunk, present
    # and future. Clearing continuous_input stops ursina's per-frame loop that set
    # camera_world_position on every entity individually (~2000 python calls a
    # frame at this view distance); update() now sets it once on the scene.
    fog_shader = Entity.default_shader
    fog_shader.continuous_input.clear()
    for k in ('fog_color', 'fog_start', 'fog_end', 'camera_world_position'):
        fog_shader.default_input.pop(k, None)
    scene.set_shader_input('fog_color', FOG_COLOR)
    scene.set_shader_input('fog_start', FOG_START)
    scene.set_shader_input('fog_end', FOG_END)
    scene.set_shader_input('camera_world_position', Vec3(0, 0, 0))

    build_loading_screen()           # something on screen before the slow work
    _loading_step('WAKING THE BUILDING', 0.03, app)
    _init_look()                     # textures + shaders, before any geometry
    _loading_step('PRINTING THE WALLPAPER', 0.30, app)
    _init_audio()                    # synthesised sounds for the intro
    _loading_step('TUNING THE HUM', 0.44, app)

    player = Player(
        height=PLAYER_HEIGHT,          # taller player (eye height)
        y=2, speed=WALK_SPEED, mouse_sensitivity=Vec2(68, 68),  # +50% (was 45)
        position=(0, 2, 0),
    )
    player.cursor.visible = False
    player.jump_height = 1.8   # comfortable jump; ceiling is now well overhead
    # Draw distance must exceed the fog, or geometry is clipped to black before the
    # fog finishes fading -- a hard edge. Keep it a little past FOG_END.
    camera.clip_plane_far = FOG_END + 25
    camera.fov = FOV                # set after the controller, which forces 90

    # Post-process: colour grade, grain, vignette and the pause blur. It stays
    # applied for the whole session (ursina has no working way to detach a camera
    # shader); the pause blur simply sits at blur_size 0 while you're playing.
    camera.shader = POST_SHADER
    for k, v in POST_SHADER.default_input.items():
        camera.set_shader_input(k, v)
    _loading_step('FITTING THE LENS', 0.52, app)

    build_hud()
    build_inventory_ui()
    build_debug_panel()
    global _prompt
    _prompt = Text(parent=camera.ui, text='', origin=(0, 0),
                   y=-0.11, scale=0.9, enabled=False)
    build_crosshair()
    build_menus()
    _set_hud_visible(False)          # keep the HUD off the loading card
    _loading_step('HANGING THE LIGHTS', 0.60, app)
    _refresh_look_grids(force=True)
    refresh_chunks(player.x, player.z)
    queued = max(1, len(_build_queue))
    while _build_queue:                     # finish the whole spawn area up front
        build_chunk(*_build_queue.pop(0))   # so nothing pops in around you on load
        if len(_build_queue) % 10 == 0:
            _loading_step('BUILDING THE ROOMS',
                          0.60 + 0.30 * (1 - len(_build_queue) / queued), app)
    if PROP_SHOWCASE:
        build_prop_showcase()

    _loading_step('SOMETHING IS ALREADY DOWN THERE', 0.92, app)
    if ENTITY_ENABLED:
        build_entity()

    _load_settings()                 # whatever you chose last time
    apply_settings()
    _loading_step('READY', 1.0, app, frames=4)
    _finish_loading(app)             # fade out of the card, up on the title screen
    app.run()


if __name__ == '__main__':
    main()
