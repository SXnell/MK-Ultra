import os
import sys
import random
import json
import pygame

pygame.init()
pygame.joystick.init()

# =====================
# SPRITE FOOT-ANCHORING (optional per character)
# =====================
# Some sprite packs have different transparent padding/cropping.
# When that happens, scaling alone won't align feet to the stage.
# This helper finds the bottom-most opaque pixel and blits the image so
# that "feet" sit on the fighter rect's bottom edge.
_OPAQUE_BOTTOM_CACHE: dict[int, int] = {}

def _opaque_bottom_y(img: pygame.Surface) -> int:
    key = id(img)
    v = _OPAQUE_BOTTOM_CACHE.get(key)
    if v is not None:
        return v
    try:
        # Prefer alpha channel if present
        if img.get_masks()[3] != 0:
            import pygame.surfarray
            a = pygame.surfarray.array_alpha(img)  # (w,h)
            # find last y containing any alpha>0
            # axis 0 is x, axis 1 is y
            cols = (a > 0).any(axis=0)
            ys = cols.nonzero()[0]
            bottom = int(ys[-1]) if ys.size else (img.get_height() - 1)
        else:
            bottom = img.get_height() - 1
    except Exception:
        bottom = img.get_height() - 1
    _OPAQUE_BOTTOM_CACHE[key] = bottom
    return bottom


def _opaque_anchor_x_bottom(img: pygame.Surface, *, min_alpha: int = 1, window_px: int = 40) -> int:
    """Return a stable-ish horizontal anchor near the feet (bottom of the sprite).

    We look at a small band of pixels near the bottom-most opaque row and compute
    a weighted-average x of opaque pixels. This helps prevent left/right jitter
    when frames have different cropping (e.g., punches extending an arm).
    """
    try:
        w, h = img.get_width(), img.get_height()
        if w <= 0 or h <= 0:
            return 0

        import pygame.surfarray
        a = pygame.surfarray.array_alpha(img)  # (w,h)
        thr = max(1, int(min_alpha))
        m = (a >= thr)

        ys = m.any(axis=0).nonzero()[0]
        if ys.size == 0:
            return w // 2
        bottom = int(ys[-1])

        win = max(1, min(h, int(window_px)))
        y0 = max(0, bottom - win + 1)
        band = m[:, y0:bottom + 1]

        # Sum opaque counts per x within the band and take weighted average.
        counts = band.sum(axis=1)
        xs = counts.nonzero()[0]
        if xs.size == 0:
            return w // 2
        weights = counts[xs]
        denom = float(weights.sum())
        if denom <= 0:
            return w // 2
        ax = int(round(float((xs * weights).sum()) / denom))
        return max(0, min(w - 1, ax))
    except Exception:
        return img.get_width() // 2


# =====================
# SOUND (SFX) MANAGER
# =====================
# Loads and plays game sound effects with simple channel separation.
# If audio init fails (e.g., no device), the game will continue silently.

def _resolve_sound_root() -> str | None:
    """Return a filesystem path to the sounds folder, or None if not found."""
    # Prefer project-relative path (portable).
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = os.path.join(here, "sounds")
    if os.path.isdir(candidate):
        return candidate
    # Fallback to Blake's dev path (as provided).
    fallback = "/Users/blake/Documents/Mac_Code/MKUltra/sounds"
    if os.path.isdir(fallback):
        return fallback
    return None


class SoundManager:
    def __init__(self):
        self.enabled = False
        self.root = _resolve_sound_root()

        # Channels: keep announcer separate so it doesn't get cut off by frequent SFX.
        self._announcer_channel = None
        self._wind_channel = None
        self._hit_channel = None
        self._damage_channel = None

        # Background music (streamed via pygame.mixer.music)
        self._music_root = None
        self._menu_music_path = None
        self._fight_music_paths = []
        self._music_mode = None  # 'menu' or 'fight'
        self._music_volume = 0.25  # atmospheric; SFX should dominate
        self.MUSIC_END_EVENT = pygame.USEREVENT + 42
        # Preloaded sounds
        self._announcer_fight = None
        self._announcer_end = []
        self._damage_taken = []
        self._wind = []
        self._hit = []

        try:
            # pygame.init() often initializes mixer, but explicit init is safer.
            if not pygame.mixer.get_init():
                pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            # Make sure we have enough channels.
            pygame.mixer.set_num_channels(8)
            self._announcer_channel = pygame.mixer.Channel(0)
            self._wind_channel = pygame.mixer.Channel(1)
            self._hit_channel = pygame.mixer.Channel(2)
            self._damage_channel = pygame.mixer.Channel(3)
            self.enabled = True
            # Music end event (for fight playlist)
            try:
                pygame.mixer.music.set_endevent(self.MUSIC_END_EVENT)
                pygame.mixer.music.set_volume(self._music_volume)
            except Exception:
                pass
        except Exception as e:
            print(f"[Audio] Mixer init failed, running without sound: {e}")
            self.enabled = False

        if self.enabled and self.root:
            self._load_all()
        elif self.enabled:
            print("[Audio] Sounds folder not found; running without sound.")
            self.enabled = False

    def _load(self, rel_path: str):
        """Load a Sound from a path relative to sound root."""
        full = os.path.join(self.root, rel_path)
        try:
            return pygame.mixer.Sound(full)
        except Exception as e:
            print(f"[Audio] Failed to load sound: {full} ({e})")
            return None

    def _load_all(self):
        # Announcer
        self._announcer_fight = self._load(os.path.join("announcer", "fight.mp3"))
        for name in ["laugh.mp3", "excellent.mp3"]:
            s = self._load(os.path.join("announcer", name))
            if s:
                self._announcer_end.append(s)

        # Damage taken (mk1-00192..00200)
        for i in range(192, 201):
            fname = f"mk1-{i:05d}.mp3"
            s = self._load(os.path.join("damagetaken", fname))
            if s:
                self._damage_taken.append(s)

        # Hit sounds
        for i in range(59, 63):
            fname = f"wind-{i:05d}.mp3"
            s = self._load(os.path.join("hitsounds", fname))
            if s:
                self._wind.append(s)

        for i in range(48, 56):
            fname = f"hit-{i:05d}.mp3"
            s = self._load(os.path.join("hitsounds", fname))
            if s:
                self._hit.append(s)

        
        # Music (streamed) - keep paths; we don't load into Sound objects.
        self._music_root = os.path.join(self.root, "music")
        self._menu_music_path = os.path.join(self._music_root, "MainMenu.mp3")
        self._fight_music_paths = [os.path.join(self._music_root, f"Track{i}.mp3") for i in range(1, 5)]
# If anything critical is missing, disable gracefully (but keep game running).
        if not self._announcer_fight:
            print("[Audio] Missing announcer/fight.mp3; disabling sound.")
            self.enabled = False

    def _play_on(self, channel: pygame.mixer.Channel, sound: pygame.mixer.Sound | None):
        if not self.enabled or sound is None or channel is None:
            return
        try:
            channel.play(sound)
        except Exception:
            pass

    def play_round_start(self):
        # Always the same.
        self._play_on(self._announcer_channel, self._announcer_fight)

    def play_match_end(self):
        if not self.enabled or not self._announcer_end:
            return
        self._play_on(self._announcer_channel, random.choice(self._announcer_end))

    def play_wind(self):
        if not self.enabled or not self._wind:
            return
        self._play_on(self._wind_channel, random.choice(self._wind))

    def play_hit(self):
        if not self.enabled or not self._hit:
            return
        self._play_on(self._hit_channel, random.choice(self._hit))

    def play_damage_taken(self):
        if not self.enabled or not self._damage_taken:
            return
        self._play_on(self._damage_channel, random.choice(self._damage_taken))
    # -----------------
    # MUSIC CONTROL
    # -----------------
    def set_music_volume(self, vol: float):
        """Set background music volume (0.0 - 1.0)."""
        try:
            self._music_volume = max(0.0, min(1.0, float(vol)))
            pygame.mixer.music.set_volume(self._music_volume)
        except Exception:
            pass

    def stop_music(self):
        if not self.enabled:
            return
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
        self._music_mode = None

    def _play_music_file(self, path: str, loop: int = 0):
        if not self.enabled or not path:
            return
        try:
            if not os.path.isfile(path):
                return
            # Avoid restarting the same track if already playing it.
            # pygame doesn't expose the current path reliably, so we just load/play.
            pygame.mixer.music.load(path)
            pygame.mixer.music.set_volume(self._music_volume)
            pygame.mixer.music.play(loop)
        except Exception:
            pass

    def play_menu_music(self):
        """Play looping menu music (title/menu/select screens)."""
        if not self.enabled:
            return
        if self._music_mode == 'menu':
            return
        self._music_mode = 'menu'
        self._play_music_file(self._menu_music_path, loop=-1)

    def play_fight_music(self):
        """Start fight playlist: play one random track, then advance on end."""
        if not self.enabled:
            return
        if self._music_mode == 'fight':
            return
        self._music_mode = 'fight'
        self._play_music_file(random.choice(self._fight_music_paths), loop=0)

    def handle_music_end_event(self):
        """Call when MUSIC_END_EVENT is received."""
        if not self.enabled:
            return
        if self._music_mode == 'fight' and self._fight_music_paths:
            # Pick next track (allow repeats; classic feel)
            self._play_music_file(random.choice(self._fight_music_paths), loop=0)
        elif self._music_mode == 'menu':
            # Should be looping, but if it ended for any reason, restart it.
            self._play_music_file(self._menu_music_path, loop=-1)



SOUND_MGR = SoundManager()

# =====================
# HITBOX DATA (runtime)
# =====================
# Used by gameplay collision + push logic. Editor saves into this file.
HITBOX_JSON_PATH = 'hitboxes_nate.json'
HITBOX_DB = {}


def load_hitbox_db(path: str = HITBOX_JSON_PATH) -> dict:
    global HITBOX_DB
    try:
        import os, json
        if os.path.isfile(path):
            with open(path, 'r', encoding='utf-8') as f:
                HITBOX_DB = json.load(f)
        else:
            HITBOX_DB = {}
    except Exception:
        HITBOX_DB = {}
    return HITBOX_DB


# Load once at startup
load_hitbox_db()


# Runtime hitbox lookup helpers
def hb_mirror_local(fighter, r):
    x, y, w, h = r
    return [int(fighter.rect.width - (x + w)), int(y), int(w), int(h)]


def _render_y_offset(fighter, img: pygame.Surface | None = None) -> int:
    if not getattr(fighter, "anchor_feet", False):
        return 0
    if img is None:
        try:
            img = fighter.current_frame_info()[0]
        except Exception:
            img = None
    if img is None:
        return 0
    bottom = _opaque_bottom_y(img)
    return (img.get_height() - 1 - bottom) + int(getattr(fighter, "feet_y_nudge", 0))


def hb_local_to_world(fighter, r, *, y_offset: int = 0):
    rr = [int(r[0]), int(r[1]), int(r[2]), int(r[3])]
    if getattr(fighter, 'flip', False):
        rr = hb_mirror_local(fighter, rr)
    return pygame.Rect(fighter.rect.x + rr[0], fighter.rect.y + rr[1] + y_offset, rr[2], rr[3])


def hb_get_local_boxes(fighter):
    # Returns (push, hurt, hit) in LOCAL coords (stored facing-right).
    # If a specific frame has no data, falls back to the nearest defined frame (or frame 0 if present).
    name = getattr(fighter, 'name', 'fighter')
    try:
        _img, anim_key, frame_idx, _anim = fighter.current_frame_info()
        frame_idx = int(frame_idx)
    except Exception:
        return None, [], []

    try:
        anim_db = HITBOX_DB.get(name, {}).get(anim_key, {})
        if not anim_db:
            return None, [], []

        # Direct hit
        f = anim_db.get(str(frame_idx))

        # Prefer explicit frame 0 if present (common for idle/hurt templates)
        if f is None and '0' in anim_db:
            f = anim_db.get('0')

        # Otherwise choose nearest saved frame index
        if f is None:
            try:
                keys = sorted(int(k) for k in anim_db.keys() if str(k).isdigit())
            except Exception:
                keys = []
            if keys:
                # nearest <= frame_idx, else smallest
                k = max([k for k in keys if k <= frame_idx], default=keys[0])
                f = anim_db.get(str(k))

        if not f:
            return None, [], []
        push = f.get('push', None)
        hurt = (f.get('hurt', []) or [])
        hit = (f.get('hit', []) or [])

        # Crouch-walk pushbox: use low_idle pushbox to avoid transparent padding issues
        # on low_move frames while preserving hurt/hit boxes from low_move.
        if anim_key == "low_move":
            try:
                idle_db = HITBOX_DB.get(name, {}).get("low_idle", {})
                if idle_db:
                    idle_f = idle_db.get("0") or idle_db.get(0)
                    if not idle_f:
                        keys2 = []
                        try:
                            keys2 = sorted([int(k) for k in idle_db.keys() if str(k).isdigit()])
                        except Exception:
                            keys2 = []
                        if keys2:
                            idle_f = idle_db.get(str(keys2[0]))
                    if idle_f and idle_f.get("push", None) is not None:
                        push = idle_f.get("push", None)
            except Exception:
                pass

        return push, hurt, hit
    except Exception:
        return None, [], []


def hb_get_world_boxes(fighter):
    # Returns (push_rect_or_none, hurt_rects, hit_rects) in WORLD coords, mirrored with sprite.
    img = None
    try:
        img = fighter.current_frame_info()[0]
    except Exception:
        img = None
    y_offset = _render_y_offset(fighter, img)
    push, hurt, hit = hb_get_local_boxes(fighter)
    push_r = hb_local_to_world(fighter, push, y_offset=y_offset) if push is not None else None
    hurt_rs = [hb_local_to_world(fighter, r, y_offset=y_offset) for r in hurt]
    hit_rs = [hb_local_to_world(fighter, r, y_offset=y_offset) for r in hit]
    return push_r, hurt_rs, hit_rs


def hb_resolve_pushboxes(p1, p2):
    """Separate fighters horizontally using saved pushboxes (MK-style).
    If no pushbox is saved for a fighter/frame, falls back to fighter.rect.
    """
    r1, _, _ = hb_get_world_boxes(p1)
    r2, _, _ = hb_get_world_boxes(p2)
    if r1 is None:
        r1 = p1.rect
    if r2 is None:
        r2 = p2.rect
    if not r1.colliderect(r2):
        return
    overlap = min(r1.right, r2.right) - max(r1.left, r2.left)
    if overlap <= 0:
        return
    # Push apart equally. If one is at edge, push the other more.
    half = overlap // 2
    if p1.rect.centerx < p2.rect.centerx:
        p1.rect.x -= half
        p2.rect.x += overlap - half
    else:
        p1.rect.x += overlap - half
        p2.rect.x -= half
    # Clamp to screen
    p1.rect.x = max(0, min(WIDTH - p1.rect.width, p1.rect.x))
    p2.rect.x = max(0, min(WIDTH - p2.rect.width, p2.rect.x))

# =====================
# CONFIG
# =====================
WIDTH, HEIGHT = 1000, 600
FPS = 60


# =====================

# HITBOX EDITOR

# =====================
HITBOX_EDITOR_MODE = False  # toggled with F2 during fights




class HitboxEditor:
    """In-game per-frame hit/hurt/push box editor (MK-style).

    Stores boxes in fighter-local coords relative to fighter.rect.topleft.
    JSON schema:
      {
        "<fighter_name>": {
          "<anim_key>": {
            "<frame_index>": {"push": [x,y,w,h] | null, "hurt": [[...],...], "hit": [[...],...] }
          }
        }
      }
    """

    COLORS = {
        'push': (255, 255, 255),
        'hurt': (0, 255, 0),
        'hit':  (255, 0, 0),
        'sel':  (255, 255, 0),
    }

    def __init__(self, json_path: str = 'hitboxes_nate.json'):
        self.enabled = False
        self.json_path = json_path
        self.db = {}
        self.active_player = 0  # 0=p1, 1=p2
        self.mode = 'hurt'  # 'push'/'hurt'/'hit'
        self.selected_index = None
        self.dragging_new = False
        self.dragging_move = False
        self.drag_start = (0, 0)
        self.drag_rect = None
        self.move_offset = (0, 0)
        self._load()

    def _load(self):
        try:
            if os.path.isfile(self.json_path):
                with open(self.json_path, 'r', encoding='utf-8') as f:
                    self.db = json.load(f)
        except Exception:
            self.db = {}

    def save(self):
        """Write editor DB to disk and refresh runtime HITBOX_DB."""
        try:
            with open(self.json_path, 'w', encoding='utf-8') as f:
                json.dump(self.db, f, indent=2)
        except Exception:
            return
        # Refresh runtime DB so gameplay sees updates immediately
        try:
            load_hitbox_db(self.json_path)
        except Exception:
            pass

    def _fighter(self, p1, p2):
        return p1 if self.active_player == 0 else p2

    def _frame_key(self, fighter):
        """Return (fighter_name, anim_key, frame_index, anim_obj)."""
        name = getattr(fighter, 'name', 'fighter')
        info = fighter.current_frame_info()
        # info: (img, anim_key, frame_index, anim_obj)
        anim_key = info[1]
        frame_index = info[2]
        anim_obj = info[3]
        return name, anim_key, int(frame_index), anim_obj

    def _ensure_entry(self, fighter_name, anim_key, frame_idx):
        d = self.db.setdefault(fighter_name, {})
        a = d.setdefault(anim_key, {})
        f = a.setdefault(str(frame_idx), {'push': None, 'hurt': [], 'hit': []})
        # Backfill keys if older file
        if 'push' not in f:
            f['push'] = None
        if 'hurt' not in f:
            f['hurt'] = []
        if 'hit' not in f:
            f['hit'] = []
        return f

    def _get_boxes_local(self, fighter_name, anim_key, frame_idx):
        f = self._ensure_entry(fighter_name, anim_key, frame_idx)
        push = f.get('push')
        hurt = f.get('hurt', [])
        hit = f.get('hit', [])
        return push, hurt, hit

    def _set_boxes_local(self, fighter_name, anim_key, frame_idx, push, hurt, hit):
        f = self._ensure_entry(fighter_name, anim_key, frame_idx)
        f['push'] = push
        f['hurt'] = hurt
        f['hit'] = hit

    def _mirror_local(self, fighter, r):
        x, y, w, h = r
        # mirror within the fighter's sprite box width
        return [int(fighter.rect.width - (x + w)), int(y), int(w), int(h)]

    def _local_to_world(self, fighter, r):
        x, y, w, h = r
        rr = [int(x), int(y), int(w), int(h)]
        # If sprite is flipped, mirror boxes so they stay attached visually
        if getattr(fighter, 'flip', False):
            rr = self._mirror_local(fighter, rr)
        y_offset = _render_y_offset(fighter)
        return pygame.Rect(fighter.rect.x + rr[0], fighter.rect.y + rr[1] + y_offset, rr[2], rr[3])

    def _world_to_local(self, fighter, rect: pygame.Rect):
        # Convert world -> fighter local. If sprite is flipped, un-mirror so stored data is always facing-right.
        x = int(rect.x - fighter.rect.x)
        y = int(rect.y - fighter.rect.y)
        w = int(rect.w)
        h = int(rect.h)
        rr = [x, y, w, h]
        if getattr(fighter, 'flip', False):
            rr = self._mirror_local(fighter, rr)
        return rr

    def _all_world_rects(self, fighter, fighter_name, anim_key, frame_idx):
        push, hurt, hit = self._get_boxes_local(fighter_name, anim_key, frame_idx)
        out = {'push': [], 'hurt': [], 'hit': []}
        if push is not None:
            out['push'].append(self._local_to_world(fighter, push))
        out['hurt'] = [self._local_to_world(fighter, r) for r in hurt]
        out['hit'] = [self._local_to_world(fighter, r) for r in hit]
        return out

    def _copy_prev_frame(self, fighter_name, anim_key, frame_idx):
        if frame_idx <= 0:
            return
        prev = self._ensure_entry(fighter_name, anim_key, frame_idx - 1)
        cur = self._ensure_entry(fighter_name, anim_key, frame_idx)
        # Only copy if current frame has no boxes (to avoid accidental overwrite)
        empty = (cur.get('push') is None and not cur.get('hurt') and not cur.get('hit'))
        if empty:
            cur['push'] = prev.get('push')
            cur['hurt'] = [list(r) for r in prev.get('hurt', [])]
            cur['hit'] = [list(r) for r in prev.get('hit', [])]

    def handle_event(self, event, p1, p2):
        if not self.enabled:
            return False

        fighter = self._fighter(p1, p2)
        fighter_name, anim_key, frame_idx, anim_obj = self._frame_key(fighter)

        # Keybinds
        if event.type == pygame.KEYDOWN:
            # Exit editor (Esc)
            if event.key == pygame.K_ESCAPE:
                self.enabled = False
                return True

            if event.key == pygame.K_TAB:
                self.active_player = 1 - self.active_player
                self.selected_index = None
                return True

            if event.key == pygame.K_1:
                self.mode = 'push'; self.selected_index = None; return True
            if event.key == pygame.K_2:
                self.mode = 'hurt'; self.selected_index = None; return True
            if event.key == pygame.K_3:
                self.mode = 'hit'; self.selected_index = None; return True

            # Frame step
            if event.key in (pygame.K_COMMA, pygame.K_PERIOD) and anim_obj is not None and anim_obj.frames:
                step = -1 if event.key == pygame.K_COMMA else 1
                anim_obj.index = (anim_obj.index + step) % len(anim_obj.frames)
                self.selected_index = None
                # If new frame has no boxes, auto copy previous
                _, _, new_frame_idx, _ = self._frame_key(fighter)
                self._copy_prev_frame(fighter_name, anim_key, new_frame_idx)
                return True

            # Copy from previous frame
            if event.key == pygame.K_c:
                self._copy_prev_frame(fighter_name, anim_key, frame_idx)
                return True

            # Save
            if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                self.save()
                return True
            # Save (legacy)
            if event.key == pygame.K_s and (event.mod & pygame.KMOD_CTRL):
                self.save()
                return True

            # Delete selected
            if event.key in (pygame.K_DELETE, pygame.K_BACKSPACE):
                push, hurt, hit = self._get_boxes_local(fighter_name, anim_key, frame_idx)
                if self.mode == 'push':
                    push = None
                else:
                    lst = hurt if self.mode == 'hurt' else hit
                    if self.selected_index is not None and 0 <= self.selected_index < len(lst):
                        lst.pop(self.selected_index)
                    self.selected_index = None
                self._set_boxes_local(fighter_name, anim_key, frame_idx, push, hurt, hit)
                return True

            # Nudge selected rect
            if event.key in (pygame.K_LEFT, pygame.K_RIGHT, pygame.K_UP, pygame.K_DOWN):
                push, hurt, hit = self._get_boxes_local(fighter_name, anim_key, frame_idx)
                n = 1
                if event.mod & pygame.KMOD_SHIFT:
                    n = 5
                dx = dy = 0
                if event.key == pygame.K_LEFT: dx = -n
                if event.key == pygame.K_RIGHT: dx = n
                if event.key == pygame.K_UP: dy = -n
                if event.key == pygame.K_DOWN: dy = n

                if self.mode == 'push':
                    if push is not None:
                        push[0] += dx; push[1] += dy
                else:
                    lst = hurt if self.mode == 'hurt' else hit
                    if self.selected_index is not None and 0 <= self.selected_index < len(lst):
                        lst[self.selected_index][0] += dx
                        lst[self.selected_index][1] += dy
                self._set_boxes_local(fighter_name, anim_key, frame_idx, push, hurt, hit)
                return True

        # Mouse handling
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            mx, my = event.pos
            world = self._all_world_rects(fighter, fighter_name, anim_key, frame_idx)

            # Try select existing rect under mouse (in current mode)
            candidates = []
            if self.mode == 'push':
                candidates = world['push']
            else:
                candidates = world[self.mode]

            hit_i = None
            for i, r in enumerate(candidates):
                if r.collidepoint(mx, my):
                    hit_i = i
                    break

            if hit_i is not None:
                self.selected_index = hit_i
                self.dragging_move = True
                self.drag_start = (mx, my)
                sel_rect = candidates[hit_i]
                self.move_offset = (mx - sel_rect.x, my - sel_rect.y)
                return True

            # Otherwise start drawing a new rect
            self.selected_index = None
            self.dragging_new = True
            self.drag_start = (mx, my)
            self.drag_rect = pygame.Rect(mx, my, 1, 1)
            return True

        if event.type == pygame.MOUSEMOTION:
            if self.dragging_new and self.drag_rect is not None:
                mx, my = event.pos
                x0, y0 = self.drag_start
                x = min(x0, mx)
                y = min(y0, my)
                w = max(1, abs(mx - x0))
                h = max(1, abs(my - y0))
                self.drag_rect.update(x, y, w, h)
                return True

            if self.dragging_move and self.selected_index is not None:
                mx, my = event.pos
                ox, oy = self.move_offset
                new_x = mx - ox
                new_y = my - oy

                push, hurt, hit = self._get_boxes_local(fighter_name, anim_key, frame_idx)
                if self.mode == 'push':
                    if push is not None:
                        push = self._world_to_local(fighter, pygame.Rect(new_x, new_y, push[2], push[3]))
                else:
                    lst = hurt if self.mode == 'hurt' else hit
                    if 0 <= self.selected_index < len(lst):
                        w, h = lst[self.selected_index][2], lst[self.selected_index][3]
                        lst[self.selected_index] = self._world_to_local(fighter, pygame.Rect(new_x, new_y, w, h))

                self._set_boxes_local(fighter_name, anim_key, frame_idx, push, hurt, hit)
                return True

        if event.type == pygame.MOUSEBUTTONUP and event.button == 1:
            if self.dragging_new and self.drag_rect is not None:
                # Commit new rect
                local = self._world_to_local(fighter, self.drag_rect)
                push, hurt, hit = self._get_boxes_local(fighter_name, anim_key, frame_idx)
                if self.mode == 'push':
                    push = local
                elif self.mode == 'hurt':
                    hurt.append(local)
                else:
                    hit.append(local)
                self._set_boxes_local(fighter_name, anim_key, frame_idx, push, hurt, hit)

            self.dragging_new = False
            self.dragging_move = False
            self.drag_rect = None
            return True

        return False

    def draw_overlay(self, screen, p1, p2, font_small):
        if not self.enabled:
            return

        fighter = self._fighter(p1, p2)
        fighter_name, anim_key, frame_idx, _ = self._frame_key(fighter)
        world = self._all_world_rects(fighter, fighter_name, anim_key, frame_idx)

        # Draw existing boxes
        # push
        for r in world['push']:
            pygame.draw.rect(screen, self.COLORS['push'], r, 2)
        # hurt
        for i, r in enumerate(world['hurt']):
            col = self.COLORS['sel'] if (self.mode == 'hurt' and self.selected_index == i) else self.COLORS['hurt']
            pygame.draw.rect(screen, col, r, 2)
        # hit
        for i, r in enumerate(world['hit']):
            col = self.COLORS['sel'] if (self.mode == 'hit' and self.selected_index == i) else self.COLORS['hit']
            pygame.draw.rect(screen, col, r, 2)

        # selection highlight for pushbox
        if self.mode == 'push' and world['push']:
            pygame.draw.rect(screen, self.COLORS['sel'], world['push'][0], 2)

        # draw currently-being-drawn rect
        if self.dragging_new and self.drag_rect is not None:
            pygame.draw.rect(screen, self.COLORS[self.mode], self.drag_rect, 1)

        # HUD help
        help1 = f'EDITOR: {"P1" if self.active_player==0 else "P2"}  anim={anim_key}  frame={frame_idx}  mode={self.mode.upper()}'
        help2 = 'F2 toggle | TAB switch | 1 push 2 hurt 3 hit | , . step | drag to draw | drag box to move | arrows nudge (shift=5) | DEL delete | C copy prev | Ctrl+S save | Esc exit'
        s1 = font_small.render(help1, True, (255,255,0))
        s2 = font_small.render(help2, True, (255,255,0))
        screen.blit(s1, (10, HEIGHT - 60))
        screen.blit(s2, (10, HEIGHT - 30))


# =====================
# HITBOX RUNTIME HELPERS
# =====================

def _hb_get_frame_entry(fighter):
    # Returns (fighter_name, anim_key, frame_index)
    name = getattr(fighter, 'name', 'fighter')
    try:
        _, anim_key, frame_idx, _ = fighter.current_frame_info()
    except Exception:
        anim_key, frame_idx = 'unknown', 0
    return name, anim_key, int(frame_idx)


def get_hitbox_data_world(fighter, kind: str):
    # kind: 'push'|'hurt'|'hit'
    # Returns list of pygame.Rect in world coords. push returns 0 or 1 rect in a list.
    name, anim_key, frame_idx = _hb_get_frame_entry(fighter)
    entry = (((HITBOX_DB.get(name, {}) or {}).get(anim_key, {}) or {}).get(str(frame_idx), None))
    if not entry:
        return []
    y_offset = _render_y_offset(fighter)
    w = fighter.rect.width
    def mirror_local(r):
        x,y,ww,hh = r
        return [int(w - (x + ww)), int(y), int(ww), int(hh)]
    def to_world(r):
        rr = r
        if getattr(fighter, 'flip', False):
            rr = mirror_local(rr)
        return pygame.Rect(fighter.rect.x + rr[0], fighter.rect.y + rr[1] + y_offset, rr[2], rr[3])

    if kind == 'push':
        r = entry.get('push')
        return [to_world(r)] if r else []
    rects = entry.get(kind, []) or []
    return [to_world(r) for r in rects]


def get_pushbox_world(fighter):
    out = get_hitbox_data_world(fighter, 'push')
    return out[0] if out else fighter.rect.copy()

def get_hurtboxes_world(fighter):
    out = get_hitbox_data_world(fighter, 'hurt')
    return out if out else [fighter.rect.copy()]

def get_hitboxes_world(fighter):
    return get_hitbox_data_world(fighter, 'hit')


# =====================
# HUD LAYOUT
# =====================
# Score must be numbers-only (no label) in yellow above the health bars.
# These constants are used for BOTH intro + fight HUD so positions stay consistent.
HUD_SCORE_Y = 10
HUD_HEALTH_Y = 40
HUD_TIMER_Y = 28
HUD_ROMAN_Y = 68
HUD_ROUND_Y = 80

# =====================
# MENUS
# =====================
TITLESCREEN_BG_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/menu/TitleScreen.jpeg'
CHARSELECT_BG_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/menu/CharacterSelect.jpg'
NATE_SELECT_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/menu/NateSelect.png'
SCORPION_SELECT_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/menu/scorpionselect.png'
CONNOR_SELECT_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/menu/ConnorSelect.png'
BLAKE_SELECT_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/menu/BlakeSelect.png'

# Character indices that are currently implemented (box index -> character id)
CHAR_INDEX_TO_ID = {
    0: 'nate',
    1: 'scorpion',
    2: 'connor',
    3: 'blake',
}

STAGESELECT_BG_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/menu/StageSelect.jpeg'

MENU_ITEM_GAP = 18

# Character select boxes mapped in SOURCE space of CharacterSelect.jpg (585x328)
# (x, y, w, h) for 2 rows x 5 cols.
CHARSELECT_BOXES_SRC: list[tuple[int, int, int, int]] = [
    (10, 58, 103, 98),
    (124, 58, 104, 98),
    (238, 58, 104, 98),
    (353, 58, 106, 98),
    (469, 58, 110, 98),
    (8, 166, 108, 99),
    (125, 166, 104, 101),
    (238, 166, 104, 100),
    (353, 166, 109, 100),
    (469, 166, 110, 99),
]

# Gameplay box (hitbox/body box). Sprites are scaled to fit this box.
PLAYER_W, PLAYER_H = 300, 360

# Default walkway / floor Y (can be overridden per stage)
DEFAULT_GROUND_Y = HEIGHT - 80

# Current stage walkway Y. All landing/collision uses this.
CURRENT_GROUND_Y = DEFAULT_GROUND_Y


def get_ground_y() -> int:
    return CURRENT_GROUND_Y

# =====================
# STAGES / BACKGROUNDS
# =====================
# You asked to use this path for stages:
#   /Users/blake/Documents/Mac_Code/MKUltra/stages
# For portability, we also fall back to a local 'stages' folder next to this script.
STAGES_DIR = '/Users/blake/Documents/Mac_Code/MKUltra/stages'
DEFAULT_STAGE_NAME = 'MirabookaBusStation2'

# Stage configuration. Add more stages here later.
# ground_y controls where the fighters' FEET touch for that stage.
STAGES = {
    'ThePit': {
        'bg': 'ThePit',
        'ground_y': 550,
    },
    'MirabookaBusStation': {
        'bg': 'MirabookaBusStation',
        'ground_y': DEFAULT_GROUND_Y,
    },
    'BusStation': {
        'bg': 'BusStation',
        'ground_y': 560,
    },
}
def discover_stage_images(stage_dir: str):
    """Return list of (stage_key, filepath) discovered in stage_dir."""
    out = []
    try:
        for fn in os.listdir(stage_dir):
            if fn.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
                key = os.path.splitext(fn)[0]
                out.append((key, os.path.join(stage_dir, fn)))
    except Exception:
        pass
    out.sort(key=lambda t: t[0].lower())
    return out

# Set True to draw a line where the current stage's walkway is (helps tuning ground_y).
STAGE_WALKWAY_DEBUG = False
# While tuning a stage, you can adjust the walkway live:
#   PageUp  : move fighters UP   (smaller y)
#   PageDown: move fighters DOWN (bigger y)
# Hold Shift for bigger steps.
STAGE_TUNING_KEYS = False

STAGE_BG_EXTS = ('.png', '.jpg', '.jpeg', '.webp')

MOVE_SPEED = 5

# Jump physics (MK1/2-style: commit direction at takeoff, no air steer)
GRAVITY = 1.1
JUMP_VY = -20  # initial vertical velocity
JUMP_HSPEED = 7  # horizontal speed during diagonal jump
KNOCKDOWN_MS = 1500  # landing stun after being hit in air
AIR_ATTACK_LAND_STUN_MS = 450  # brief recovery after landing from an air attack (flop)

# Animation playback
INTRO_FPS = 24
IDLE_FPS = 10
MOVE_FPS = 12
BLOCK_FPS = 14
ATTACK_FPS = 14
HIT_FPS = 22

# Combat
ATTACK_DAMAGE = 6
from dataclasses import dataclass

@dataclass(frozen=True)
class MoveData:
    damage: int
    height: str            # 'high' | 'mid' | 'low'
    hitstun_ms: int
    blockstun_ms: int
    knockback_px: int
    knockdown_ms: int = 0  # 0 = no knockdown

# MK-ish authored move data (damage is per-move, not per-height).
# Tune later as you add more sprites / moves.
MOVE_DB: dict[tuple[str, str], MoveData] = {
    # Medium stance (standing)
    ("medium", "r"): MoveData(damage=4,  height="mid",  hitstun_ms=160, blockstun_ms=120, knockback_px=10),
    ("medium", "e"): MoveData(damage=8,  height="high", hitstun_ms=220, blockstun_ms=150, knockback_px=16),
    ("medium", "t"): MoveData(damage=6,  height="mid",  hitstun_ms=180, blockstun_ms=130, knockback_px=14),
    ("medium", "y"): MoveData(damage=10, height="high", hitstun_ms=260, blockstun_ms=170, knockback_px=20),

    # Low stance (crouching)
    ("low", "r"):    MoveData(damage=11, height="low",  hitstun_ms=220, blockstun_ms=160, knockback_px=18, knockdown_ms=550),

    # Air (jump attack)
    ("air", "attack"): MoveData(damage=10, height="high", hitstun_ms=240, blockstun_ms=160, knockback_px=22),
}
ATTACK_RANGE_PAD = 20  # extra horizontal reach for attacks

# When a fighter is knocked down from an AIR hit, hold this specific frame during stun.
# If the file doesn't exist, we fall back to the last frame of the high hit animation.
AIR_KNOCKDOWN_HOLD_FILENAME = 'Kombat_0000000001_000000001_00121.png'

# Active hit frames (0-indexed within each attack's sorted frame list)
# Based on your exact file names:
# - Attack1 (E) 00006..00011, damage at 00010 -> index 4
# - Attack2 (T) 00012..00016, damage at 00016 -> index 4
# - Attack3 (Y) 00018..00027, damage at 00022 -> index 4
# - Attack4 (R) IMG_7628..IMG_7632, damage at IMG_7632 -> index 4
ATTACK_ACTIVE_FRAME_INDEX = {
    "e": 4,
    "t": 4,
    "y": 4,
    "r": 4,
    "low_r": 4,   # 00069
    "high_r": 1,  # 00109
}


# Pushbox resolution (MK-style spacing)
def resolve_pushboxes(a, b):
    try:
        ra = get_pushbox_world(a)
        rb = get_pushbox_world(b)
    except Exception:
        ra = a.rect
        rb = b.rect
    if not ra.colliderect(rb):
        return
    overlap = min(ra.right, rb.right) - max(ra.left, rb.left)
    if overlap <= 0:
        return
    # Push apart along X. Split the correction.
    half = overlap // 2 if overlap > 1 else 1
    if ra.centerx < rb.centerx:
        a.rect.x -= half
        b.rect.x += (overlap - half)
    else:
        a.rect.x += half
        b.rect.x -= (overlap - half)
    a.rect.x = max(0, min(WIDTH - a.rect.width, a.rect.x))
    b.rect.x = max(0, min(WIDTH - b.rect.width, b.rect.x))

def _case_insensitive_dir(path: str) -> str | None:
    """Try to resolve a directory path case-insensitively.

    Useful on macOS (default case-insensitive) when assets were created with
    different capitalization than the code expects (e.g., Attack vs attack).
    Returns the resolved path if it exists, else None.
    """
    if not path:
        return None
    # Normalize separators
    path = os.path.normpath(path)
    # If it's already a valid dir, return it
    if os.path.isdir(path):
        return path

    # Build from root, matching each component case-insensitively.
    parts = path.split(os.sep)
    if parts and parts[0] == '':
        cur = os.sep
        parts = parts[1:]
    else:
        # Relative path: start at cwd
        cur = os.getcwd()

    for p in parts:
        if p in ('', '.'):
            continue
        try:
            entries = os.listdir(cur)
        except Exception:
            return None
        match = None
        p_low = p.lower()
        for e in entries:
            if e.lower() == p_low:
                match = e
                break
        if match is None:
            return None
        cur = os.path.join(cur, match)

    return cur if os.path.isdir(cur) else None


def resolve_sprite_dir(preferred: str, fallback: str) -> str:
    """Return preferred if it exists (or can be resolved), else fallback.

    Resolution order:
      1) preferred (as-is) if it exists
      2) preferred resolved case-insensitively (helps when folder capitalization differs)
      3) fallback (relative path inside repo)
    """
    try:
        if preferred and os.path.isdir(preferred):
            return preferred
        if preferred:
            ci = _case_insensitive_dir(preferred)
            if ci:
                return ci
    except Exception:
        pass
    return fallback


# Paths (relative to this script)
NATE_START_DIR = os.path.join("sprites", "nate", "start")

NATE_MEDIUM_IDLE_DIR = os.path.join("sprites", "nate", "medium", "idle", "idle1")
NATE_MEDIUM_MOVE_FWD_DIR = os.path.join("sprites", "nate", "medium", "movement", "movement1")
NATE_MEDIUM_MOVE_BACK_DIR = os.path.join("sprites", "nate", "medium", "movement", "movement2")

NATE_MEDIUM_BLOCK1_DIR = os.path.join("sprites", "nate", "medium", "block", "block1")
NATE_MEDIUM_BLOCK2_DIR = os.path.join("sprites", "nate", "medium", "block", "block2")

NATE_MEDIUM_ATTACK_R_DIR = os.path.join("sprites", "nate", "medium", "attack", "attack4")
NATE_MEDIUM_ATTACK_E_DIR = os.path.join("sprites", "nate", "medium", "attack", "attack1")
NATE_MEDIUM_ATTACK_T_DIR = os.path.join("sprites", "nate", "medium", "attack", "attack2")
NATE_MEDIUM_ATTACK_Y_DIR = os.path.join("sprites", "nate", "medium", "attack", "attack3")

NATE_MEDIUM_HIT_DIR = os.path.join("sprites", "nate", "medium", "hit", "hit1")

# Low stance (crouch) paths
# Preferred: Blake's local absolute paths (Mac). Fallback: repo-relative paths.
NATE_LOW_IDLE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/low/idle/idle1',
    os.path.join('sprites', 'nate', 'low', 'idle', 'idle1'),
)
NATE_LOW_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/low/movement/movement1',
    os.path.join('sprites', 'nate', 'low', 'movement', 'movement1'),
)
NATE_LOW_BLOCK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/low/block/block1',
    os.path.join('sprites', 'nate', 'low', 'block', 'block1'),
)
# Low attack (only R is implemented in low stance for now)
NATE_LOW_ATTACK_R_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/low/attack/attack1',
    os.path.join('sprites', 'nate', 'low', 'attack', 'attack1'),
)
NATE_LOW_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/low/hit/hit1',
    os.path.join('sprites', 'nate', 'low', 'hit', 'hit1'),
)

# High stance (jump) paths
NATE_HIGH_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/high/movement/movement1',
    os.path.join('sprites', 'nate', 'high', 'movement', 'movement1'),
)
NATE_HIGH_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/high/hit/hit1',
    os.path.join('sprites', 'nate', 'high', 'hit', 'hit1'),
)
NATE_HIGH_ATTACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/high/attack/attack1',
    os.path.join('sprites', 'nate', 'high', 'attack', 'attack1'),
)

# End-of-match (win/lose) animations
NATE_END_WIN_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/end/win/win1',
    os.path.join('sprites', 'nate', 'end', 'win', 'win1'),
)
NATE_END_LOSE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/nate/end/lose/lose1',
    os.path.join('sprites', 'nate', 'end', 'lose', 'lose1'),
)


# =====================
# CONNOR SPRITE PATHS
# =====================
CONNOR_START_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/start',
    os.path.join('sprites', 'connor', 'start'),
)

CONNOR_FIT = 1.0
CONNOR_LOW_STANCE_BOOST = 1.0
CONNOR_HIGH_STANCE_BOOST = 1.0
CONNOR_ATTACK_Y_BOOST = 1.0
CONNOR_ATTACK_R_BOOST = 1.0
CONNOR_FAST_SCALE = True

CONNOR_MEDIUM_IDLE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/idle/idle1',
    os.path.join('sprites', 'connor', 'medium', 'idle', 'idle1'),
)
CONNOR_MEDIUM_MOVE_FWD_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/movement/movement1',
    os.path.join('sprites', 'connor', 'medium', 'movement', 'movement1'),
)
CONNOR_MEDIUM_MOVE_BACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/movement/movement2',
    os.path.join('sprites', 'connor', 'medium', 'movement', 'movement2'),
)

CONNOR_MEDIUM_BLOCK1_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/block/block1',
    os.path.join('sprites', 'connor', 'medium', 'block', 'block1'),
)
CONNOR_MEDIUM_BLOCK2_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/block/block2',
    os.path.join('sprites', 'connor', 'medium', 'block', 'block2'),
)
if not os.path.isdir(CONNOR_MEDIUM_BLOCK2_DIR):
    CONNOR_MEDIUM_BLOCK2_DIR = CONNOR_MEDIUM_BLOCK1_DIR

CONNOR_MEDIUM_ATTACK_E_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/attack/attack1',
    os.path.join('sprites', 'connor', 'medium', 'attack', 'attack1'),
)
CONNOR_MEDIUM_ATTACK_T_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/attack/attack2',
    os.path.join('sprites', 'connor', 'medium', 'attack', 'attack2'),
)
CONNOR_MEDIUM_ATTACK_Y_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/attack/attack3',
    os.path.join('sprites', 'connor', 'medium', 'attack', 'attack3'),
)
CONNOR_MEDIUM_ATTACK_R_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/attack/attack4',
    os.path.join('sprites', 'connor', 'medium', 'attack', 'attack4'),
)

CONNOR_MEDIUM_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/medium/hit/hit1',
    os.path.join('sprites', 'connor', 'medium', 'hit', 'hit1'),
)

# Low stance (crouch) paths
CONNOR_LOW_IDLE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/low/idle/idle1',
    os.path.join('sprites', 'connor', 'low', 'idle', 'idle1'),
)
CONNOR_LOW_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/low/movement/movement1',
    os.path.join('sprites', 'connor', 'low', 'movement', 'movement1'),
)
if not os.path.isdir(CONNOR_LOW_MOVE_DIR):
    CONNOR_LOW_MOVE_DIR = CONNOR_LOW_IDLE_DIR
CONNOR_LOW_BLOCK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/low/block/block1',
    os.path.join('sprites', 'connor', 'low', 'block', 'block1'),
)
CONNOR_LOW_ATTACK_R_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/low/attack/attack1',
    os.path.join('sprites', 'connor', 'low', 'attack', 'attack1'),
)
CONNOR_LOW_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/low/hit/hit1',
    os.path.join('sprites', 'connor', 'low', 'hit', 'hit1'),
)

# High stance (jump) paths
CONNOR_HIGH_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/high/movement/movement1',
    os.path.join('sprites', 'connor', 'high', 'movement', 'movement1'),
)
CONNOR_HIGH_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/high/hit/hit1',
    os.path.join('sprites', 'connor', 'high', 'hit', 'hit1'),
)
CONNOR_HIGH_ATTACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/high/attack/attack1',
    os.path.join('sprites', 'connor', 'high', 'attack', 'attack1'),
)

# End-of-match (win/lose) animations
CONNOR_END_WIN_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/end/win/win1',
    os.path.join('sprites', 'connor', 'end', 'win', 'win1'),
)
CONNOR_END_LOSE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/connor/end/lose/lose1',
    os.path.join('sprites', 'connor', 'end', 'lose', 'lose1'),
)


# =====================
# BLAKE SPRITE PATHS
# =====================
# Mirrors Connor/Nate folder structure one-for-one.
BLAKE_START_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/start',
    os.path.join('sprites', 'blake', 'start'),
)

BLAKE_FIT = 1.0
BLAKE_FAST_SCALE = True

BLAKE_MEDIUM_IDLE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/idle/idle1',
    os.path.join('sprites', 'blake', 'medium', 'idle', 'idle1'),
)
BLAKE_MEDIUM_MOVE_FWD_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/movement/movement1',
    os.path.join('sprites', 'blake', 'medium', 'movement', 'movement1'),
)
BLAKE_MEDIUM_MOVE_BACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/movement/movement2',
    os.path.join('sprites', 'blake', 'medium', 'movement', 'movement2'),
)

BLAKE_MEDIUM_BLOCK1_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/block/block1',
    os.path.join('sprites', 'blake', 'medium', 'block', 'block1'),
)
BLAKE_MEDIUM_BLOCK2_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/block/block2',
    os.path.join('sprites', 'blake', 'medium', 'block', 'block2'),
)
if not os.path.isdir(BLAKE_MEDIUM_BLOCK2_DIR):
    BLAKE_MEDIUM_BLOCK2_DIR = BLAKE_MEDIUM_BLOCK1_DIR

BLAKE_MEDIUM_ATTACK_E_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/attack/attack1',
    os.path.join('sprites', 'blake', 'medium', 'attack', 'attack1'),
)
BLAKE_MEDIUM_ATTACK_T_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/attack/attack2',
    os.path.join('sprites', 'blake', 'medium', 'attack', 'attack2'),
)
BLAKE_MEDIUM_ATTACK_Y_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/attack/attack3',
    os.path.join('sprites', 'blake', 'medium', 'attack', 'attack3'),
)
BLAKE_MEDIUM_ATTACK_R_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/attack/attack4',
    os.path.join('sprites', 'blake', 'medium', 'attack', 'attack4'),
)

BLAKE_MEDIUM_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/medium/hit/hit1',
    os.path.join('sprites', 'blake', 'medium', 'hit', 'hit1'),
)

# Low stance (crouch) paths
BLAKE_LOW_IDLE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/low/idle/idle1',
    os.path.join('sprites', 'blake', 'low', 'idle', 'idle1'),
)
BLAKE_LOW_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/low/movement/movement1',
    os.path.join('sprites', 'blake', 'low', 'movement', 'movement1'),
)
if not os.path.isdir(BLAKE_LOW_MOVE_DIR):
    BLAKE_LOW_MOVE_DIR = BLAKE_LOW_IDLE_DIR
BLAKE_LOW_BLOCK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/low/block/block1',
    os.path.join('sprites', 'blake', 'low', 'block', 'block1'),
)
BLAKE_LOW_ATTACK_R_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/low/attack/attack1',
    os.path.join('sprites', 'blake', 'low', 'attack', 'attack1'),
)
BLAKE_LOW_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/low/hit/hit1',
    os.path.join('sprites', 'blake', 'low', 'hit', 'hit1'),
)

# High stance (jump) paths
BLAKE_HIGH_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/high/movement/movement1',
    os.path.join('sprites', 'blake', 'high', 'movement', 'movement1'),
)
BLAKE_HIGH_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/high/hit/hit1',
    os.path.join('sprites', 'blake', 'high', 'hit', 'hit1'),
)
BLAKE_HIGH_ATTACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/high/attack/attack1',
    os.path.join('sprites', 'blake', 'high', 'attack', 'attack1'),
)

# End-of-match (win/lose) animations
BLAKE_END_WIN_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/end/win/win1',
    os.path.join('sprites', 'blake', 'end', 'win', 'win1'),
)
BLAKE_END_LOSE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/blake/end/lose/lose1',
    os.path.join('sprites', 'blake', 'end', 'lose', 'lose1'),
)


# =====================
# SCORPION SPRITE PATHS
# =====================
# Mirrors Nate's folder structure one-for-one.
SCORPION_START_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/start',
    os.path.join('sprites', 'scorpion', 'start'),
)

SCORPION_FIT = 0.62
LOW_STANCE_BOOST = 1.45  # boost low stance size (~45%)  # boost low stance size (~25%)  # art-fit inside the standard PLAYER_W/PLAYER_H canvas (tweak if needed)

# Medium stance paths (preferred: Blake's absolute path; fallback: repo-relative)
SCORPION_MEDIUM_IDLE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/idle/idle1',
    os.path.join('sprites', 'scorpion', 'medium', 'idle', 'idle1'),
)
SCORPION_MEDIUM_MOVE_FWD_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/movement/movement1',
    os.path.join('sprites', 'scorpion', 'medium', 'movement', 'movement1'),
)
SCORPION_MEDIUM_MOVE_BACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/movement/movement2',
    os.path.join('sprites', 'scorpion', 'medium', 'movement', 'movement2'),
)

SCORPION_MEDIUM_BLOCK1_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/block/block1',
    os.path.join('sprites', 'scorpion', 'medium', 'block', 'block1'),
)
# Optional: block2. If it doesn't exist, fall back to block1 so blocking always works.
SCORPION_MEDIUM_BLOCK2_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/block/block2',
    os.path.join('sprites', 'scorpion', 'medium', 'block', 'block2'),
)
if not os.path.isdir(SCORPION_MEDIUM_BLOCK2_DIR):
    SCORPION_MEDIUM_BLOCK2_DIR = SCORPION_MEDIUM_BLOCK1_DIR

SCORPION_MEDIUM_ATTACK_E_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/attack/attack1',
    os.path.join('sprites', 'scorpion', 'medium', 'attack', 'attack1'),
)
SCORPION_MEDIUM_ATTACK_T_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/attack/attack2',
    os.path.join('sprites', 'scorpion', 'medium', 'attack', 'attack2'),
)
SCORPION_MEDIUM_ATTACK_Y_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/attack/attack3',
    os.path.join('sprites', 'scorpion', 'medium', 'attack', 'attack3'),
)
SCORPION_MEDIUM_ATTACK_R_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/attack/attack4',
    os.path.join('sprites', 'scorpion', 'medium', 'attack', 'attack4'),
)

SCORPION_MEDIUM_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/medium/hit/hit1',
    os.path.join('sprites', 'scorpion', 'medium', 'hit', 'hit1'),
)

# Low stance (crouch) paths (preferred: Blake's absolute path; fallback: repo-relative)
SCORPION_LOW_IDLE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/low/idle/idle1',
    os.path.join('sprites', 'scorpion', 'low', 'idle', 'idle1'),
)
SCORPION_LOW_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/low/movement/movement1',
    os.path.join('sprites', 'scorpion', 'low', 'movement', 'movement1'),
)
SCORPION_LOW_BLOCK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/low/block/block1',
    os.path.join('sprites', 'scorpion', 'low', 'block', 'block1'),
)
SCORPION_LOW_ATTACK_R_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/low/attack/attack1',
    os.path.join('sprites', 'scorpion', 'low', 'attack', 'attack1'),
)
SCORPION_LOW_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/low/hit/hit1',
    os.path.join('sprites', 'scorpion', 'low', 'hit', 'hit1'),
)

# High stance (jump) paths
SCORPION_HIGH_MOVE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/high/movement/movement1',
    os.path.join('sprites', 'scorpion', 'high', 'movement', 'movement1'),
)
SCORPION_HIGH_MOVE_BACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/high/movement/movement2',
    os.path.join('sprites', 'scorpion', 'high', 'movement', 'movement2'),
)
SCORPION_HIGH_HIT_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/high/hit/hit1',
    os.path.join('sprites', 'scorpion', 'high', 'hit', 'hit1'),
)
SCORPION_HIGH_ATTACK_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/high/attack/attack1',
    os.path.join('sprites', 'scorpion', 'high', 'attack', 'attack1'),
)

# End-of-match (win/lose) animations
SCORPION_END_WIN_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/end/win/win1',
    os.path.join('sprites', 'scorpion', 'end', 'win', 'win1'),
)
SCORPION_END_LOSE_DIR = resolve_sprite_dir(
    '/Users/blake/Documents/Mac_Code/MKUltra/sprites/scorpion/end/lose/lose1',
    os.path.join('sprites', 'scorpion', 'end', 'lose', 'lose1'),
)


# Font (MK2-style). Prefer your absolute path, then fall back to ./mk2.ttf, then pygame default.
FONT_PATH = '/Users/blake/Documents/Mac_Code/MKUltra/mk2.ttf'

def load_game_font(size: int) -> pygame.font.Font:
    # Prefer the absolute path you provided (Mac)
    if os.path.isfile(FONT_PATH):
        try:
            return pygame.font.Font(FONT_PATH, size)
        except Exception as e:
            print(f'[WARN] Failed to load font at {FONT_PATH}: {e}')

    # Fall back to a mk2.ttf sitting next to the script
    local_font = os.path.join(os.path.dirname(__file__), 'mk2.ttf')
    if os.path.isfile(local_font):
        try:
            return pygame.font.Font(local_font, size)
        except Exception as e:
            print(f'[WARN] Failed to load local font at {local_font}: {e}')

    # Final fallback: default pygame font
    return pygame.font.SysFont(None, size)


def try_load_image(path: str, *, convert_alpha: bool = True) -> pygame.Surface | None:
    """Load an image if present; return None if missing/unloadable."""
    if not path:
        return None
    try:
        if not os.path.isfile(path):
            return None
        img = pygame.image.load(path)
        return img.convert_alpha() if convert_alpha else img.convert()
    except Exception as e:
        print(f"[WARN] Failed to load image {path}: {e}")
        return None


def blit_scaled_center(dst: pygame.Surface, src: pygame.Surface) -> tuple[int, int, float, float]:
    """Scale src to fit dst (letterbox) and blit centered.

    Returns (off_x, off_y, scale_x, scale_y) so you can map source-space rects.
    """
    dw, dh = dst.get_size()
    sw, sh = src.get_size()
    if sw <= 0 or sh <= 0:
        return (0, 0, 1.0, 1.0)

    scale = min(dw / sw, dh / sh)
    new_w, new_h = int(sw * scale), int(sh * scale)
    scaled = pygame.transform.smoothscale(src, (new_w, new_h))
    off_x = (dw - new_w) // 2
    off_y = (dh - new_h) // 2
    dst.blit(scaled, (off_x, off_y))
    return (off_x, off_y, scale, scale)



# =====================
# SETUP
# =====================
screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("MK Clone")
clock = pygame.time.Clock()

WHITE = (255, 255, 255)
TEXT_RED = (255, 0, 0)
BLACK = (0, 0, 0)
RED = (200, 0, 0)
BLUE = (0, 0, 200)
GRAY = (60, 60, 60)
DARK = (20, 20, 20)


def load_scaled_images(folder: str, size: tuple[int, int]) -> list[pygame.Surface]:
    """Load image frames in folder and scale to a fixed size.

    Accepts .png/.jpg/.jpeg in any case (e.g., IMG_7628.PNG).
    """
    frames: list[pygame.Surface] = []
    if not os.path.isdir(folder):
        print(f"[WARN] Missing folder: {folder}")
        return frames

    for fname in sorted(os.listdir(folder)):
        low = fname.lower()
        if not (low.endswith(".png") or low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".gif")):
            continue
        path = os.path.join(folder, fname)
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {e}")
            continue
        img = pygame.transform.smoothscale(img, size)
        frames.append(img)

    print(f"[INFO] Loaded {len(frames)} frames from {folder}")
    return frames



def load_scaled_images_fitted(folder: str, size: tuple[int, int], fit: float = 1.0, *, bottom_align: bool = True) -> list[pygame.Surface]:
    """Load frames from folder, scale to a fixed *canvas* size, but optionally 'fit' the art smaller inside the canvas.

    This is useful when different character packs have different amounts of transparent padding/cropping.
    - size: final surface size (canvas)
    - fit: 1.0 = fill canvas (same as load_scaled_images), 0.8 = 80% of canvas
    - bottom_align: keep feet on the ground (blit to bottom center)

    Important: This version preserves aspect ratio (no stretching/warping) by scaling each source
    image to fit inside the target box and letterboxing/padding on the canvas.
    """
    frames: list[pygame.Surface] = []
    if not os.path.isdir(folder):
        print(f"[WARN] Missing folder: {folder}")
        return frames

    cw, ch = size
    # Clamp fit to sane values
    fit = max(0.05, min(1.0, float(fit)))
    target_w, target_h = max(1, int(cw * fit)), max(1, int(ch * fit))

    for fname in sorted(os.listdir(folder)):
        low = fname.lower()
        if not (low.endswith(".png") or low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".gif")):
            continue
        path = os.path.join(folder, fname)
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {e}")
            continue

        iw, ih = img.get_width(), img.get_height()
        if iw <= 0 or ih <= 0:
            continue

        # Preserve aspect ratio: scale to fit inside (target_w, target_h)
        scale = min(target_w / iw, target_h / ih)
        scale = max(0.01, float(scale))

        new_w, new_h = max(1, int(iw * scale)), max(1, int(ih * scale))
        art = pygame.transform.smoothscale(img, (new_w, new_h))

        # Place onto fixed canvas so rect sizes, hitboxes, etc. stay consistent.
        canvas = pygame.Surface((cw, ch), pygame.SRCALPHA, 32)
        x = (cw - new_w) // 2
        y = (ch - new_h) if bottom_align else (ch - new_h) // 2
        canvas.blit(art, (x, y))
        frames.append(canvas)

    print(f"[INFO] Loaded {len(frames)} frames from {folder} (fit={fit:.2f})")
    return frames

def load_scaled_images_normalized(folder: str, size: tuple[int, int], ref_h: int, fit: float = 1.0, target_h_override: int | None = None, bottom_align: bool = True) -> list[pygame.Surface]:
    """Load frames from folder and normalize on-screen size across frames based on opaque-content height.

    Problem this solves:
    Some sprite packs contain frames with different transparent padding/cropping. If you scale based on the full
    image size, the character will 'pulse' (appear larger/smaller) between frames (e.g., kicks becoming tiny).
    This loader scales each frame based on its *opaque bounding box height* so every frame's visible content
    has a consistent height relative to a reference.

    Args:
        folder: animation folder
        size: final surface canvas size (cw, ch)
        ref_h: reference opaque-content height (in pixels) that should map to the fitted target height
        fit: 1.0 fills the canvas height; lower values make character smaller inside the canvas
        bottom_align: anchor to canvas bottom (feet)
    """
    frames: list[pygame.Surface] = []
    if not os.path.isdir(folder):
        print(f"[WARN] Missing folder: {folder}")
        return frames

    cw, ch = size
    fit = max(0.05, min(1.0, float(fit)))
    target_h = max(1, int(ch * fit)) if target_h_override is None else max(1, int(target_h_override))

    ref_h = max(1, int(ref_h))

    for fname in sorted(os.listdir(folder)):
        low = fname.lower()
        if not (low.endswith(".png") or low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".gif")):
            continue
        path = os.path.join(folder, fname)
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {e}")
            continue

        # Crop to opaque content (removes varying transparent padding)
        try:
            bbox = img.get_bounding_rect(min_alpha=1)
        except Exception:
            bbox = pygame.Rect(0, 0, img.get_width(), img.get_height())

        if bbox.width <= 0 or bbox.height <= 0:
            # Fallback to whole image
            bbox = pygame.Rect(0, 0, img.get_width(), img.get_height())

        crop = img.subsurface(bbox).copy()

        # Scale uniformly based on the stance reference height (prevents size changes between moves).
        # We still crop to opaque content to remove inconsistent transparent padding.
        base_scale = target_h / max(1, ref_h)
        base_scale = max(0.01, float(base_scale))
        new_w = max(1, int(crop.get_width() * base_scale))
        new_h = max(1, int(crop.get_height() * base_scale))

        # Safety: if a particular frame is unusually wide/tall after scaling, shrink to fit the target box.
        max_w = max(1, int(cw * fit))
        max_h = max(1, int(ch * fit))
        if new_w > max_w or new_h > max_h:
            shrink = min(max_w / new_w, max_h / new_h)
            shrink = max(0.01, float(shrink))
            new_w = max(1, int(new_w * shrink))
            new_h = max(1, int(new_h * shrink))

        art = pygame.transform.smoothscale(crop, (new_w, new_h))

        canvas = pygame.Surface((cw, ch), pygame.SRCALPHA, 32)
        x = (cw - new_w) // 2
        y = (ch - new_h) if bottom_align else (ch - new_h) // 2
        canvas.blit(art, (x, y))
        frames.append(canvas)

    print(f"[INFO] Loaded {len(frames)} frames from {folder} (normalized, fit={fit:.2f})")
    return frames


def load_scaled_images_fixed_height(
    folder: str,
    size: tuple[int, int],
    target_h: int | None = None,
    *,
    fit: float = 1.0,
    bottom_align: bool = True,
    use_smooth: bool = True,
    ref_h: int | None = None,
    clamp_ratio: float = 0.4,
    min_alpha: int = 1,
    allow_overflow: bool = False,
) -> list[pygame.Surface]:
    """Load frames and lock their on-screen height to a fixed target.

    Unlike load_scaled_images_normalized, this scales each frame based on its own
    opaque bounding-box height so pose changes do not cause size "pulsing".
    """
    frames: list[pygame.Surface] = []
    if not os.path.isdir(folder):
        print(f"[WARN] Missing folder: {folder}")
        return frames

    cw, ch = size
    fit = max(0.05, min(1.0, float(fit)))
    if target_h is None:
        target_h = max(1, int(ch * fit))
    else:
        target_h = max(1, int(target_h))

    scale_fn = pygame.transform.smoothscale if use_smooth else pygame.transform.scale

    for fname in sorted(os.listdir(folder)):
        low = fname.lower()
        if not (low.endswith(".png") or low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".gif")):
            continue
        path = os.path.join(folder, fname)
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {e}")
            continue

        try:
            bbox = img.get_bounding_rect(min_alpha=max(1, int(min_alpha)))
        except Exception:
            bbox = pygame.Rect(0, 0, img.get_width(), img.get_height())

        if bbox.width <= 0 or bbox.height <= 0:
            bbox = pygame.Rect(0, 0, img.get_width(), img.get_height())

        crop = img.subsurface(bbox).copy()

        eff_h = int(bbox.height)
        if ref_h is not None:
            ref_h = max(1, int(ref_h))
            lo = max(1, int(ref_h * (1.0 - float(clamp_ratio))))
            hi = max(lo, int(ref_h * (1.0 + float(clamp_ratio))))
            if eff_h < lo:
                eff_h = lo
            elif eff_h > hi:
                eff_h = hi

        scale = target_h / max(1, eff_h)
        scale = max(0.01, float(scale))
        new_w = max(1, int(crop.get_width() * scale))
        new_h = max(1, int(crop.get_height() * scale))

        max_w = max(1, int(cw * fit))
        max_h = max(1, int(ch * fit))
        if allow_overflow:
            if new_h > max_h:
                shrink = max_h / new_h
                shrink = max(0.01, float(shrink))
                new_w = max(1, int(new_w * shrink))
                new_h = max(1, int(new_h * shrink))
        else:
            if new_w > max_w or new_h > max_h:
                shrink = min(max_w / new_w, max_h / new_h)
                shrink = max(0.01, float(shrink))
                new_w = max(1, int(new_w * shrink))
                new_h = max(1, int(new_h * shrink))

        art = scale_fn(crop, (new_w, new_h))

        canvas = pygame.Surface((cw, ch), pygame.SRCALPHA, 32)
        x = (cw - new_w) // 2
        y = (ch - new_h) if bottom_align else (ch - new_h) // 2
        canvas.blit(art, (x, y))
        frames.append(canvas)

    print(f"[INFO] Loaded {len(frames)} frames from {folder} (fixed height, fit={fit:.2f})")
    return frames


def load_scaled_images_by_image_height(
    folder: str,
    size: tuple[int, int],
    target_h: int | None = None,
    *,
    fit: float = 1.0,
    bottom_align: bool = True,
    use_smooth: bool = True,
    allow_overflow: bool = False,
) -> list[pygame.Surface]:
    """Load frames and scale by the full image height (ignores bbox changes).

    This avoids size pops when the opaque bbox height varies between frames.
    """
    frames: list[pygame.Surface] = []
    if not os.path.isdir(folder):
        print(f"[WARN] Missing folder: {folder}")
        return frames

    cw, ch = size
    fit = max(0.05, min(1.0, float(fit)))
    if target_h is None:
        target_h = max(1, int(ch * fit))
    else:
        target_h = max(1, int(target_h))

    scale_fn = pygame.transform.smoothscale if use_smooth else pygame.transform.scale

    for fname in sorted(os.listdir(folder)):
        low = fname.lower()
        if not (low.endswith(".png") or low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".gif")):
            continue
        path = os.path.join(folder, fname)
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {e}")
            continue

        ih = max(1, img.get_height())
        scale = target_h / float(ih)
        scale = max(0.01, float(scale))
        new_w = max(1, int(img.get_width() * scale))
        new_h = max(1, int(img.get_height() * scale))

        max_w = max(1, int(cw * fit))
        max_h = max(1, int(ch * fit))
        if allow_overflow:
            if new_h > max_h:
                shrink = max_h / new_h
                shrink = max(0.01, float(shrink))
                new_w = max(1, int(new_w * shrink))
                new_h = max(1, int(new_h * shrink))
        else:
            if new_w > max_w or new_h > max_h:
                shrink = min(max_w / new_w, max_h / new_h)
                shrink = max(0.01, float(shrink))
                new_w = max(1, int(new_w * shrink))
                new_h = max(1, int(new_h * shrink))

        art = scale_fn(img, (new_w, new_h))
        canvas = pygame.Surface((cw, ch), pygame.SRCALPHA, 32)
        x = (cw - new_w) // 2
        y = (ch - new_h) if bottom_align else (ch - new_h) // 2
        canvas.blit(art, (x, y))
        frames.append(canvas)

    print(f"[INFO] Loaded {len(frames)} frames from {folder} (image height, fit={fit:.2f})")
    return frames


_SPRITE_FRAME_CACHE: dict[tuple, list[pygame.Surface]] = {}
_NATE_TARGET_BBOX_H: tuple[int, int, int] | None = None


def _first_image_path(folder: str) -> str | None:
    """Return the first image file path in a folder (sorted), or None."""
    if not os.path.isdir(folder):
        return None
    for fname in sorted(os.listdir(folder)):
        low = fname.lower()
        if low.endswith((".png", ".jpg", ".jpeg", ".gif")):
            return os.path.join(folder, fname)
    return None


def _measure_scaled_bbox_h(path: str, out_size: tuple[int, int], *, min_alpha: int = 1) -> int:
    """Measure opaque bbox height after scaling an image to out_size."""
    try:
        img = pygame.image.load(path).convert_alpha()
    except Exception:
        return out_size[1]
    try:
        img = pygame.transform.smoothscale(img, out_size)
    except Exception:
        return out_size[1]
    try:
        bb = img.get_bounding_rect(min_alpha=max(1, int(min_alpha)))
        return int(bb.height) if bb.height > 0 else out_size[1]
    except Exception:
        return out_size[1]


def _get_nate_target_bbox_heights() -> tuple[int, int, int]:
    """Return (medium, low, high) target bbox heights measured from Nate's sprites (scaled to PLAYER_W/PLAYER_H)."""
    global _NATE_TARGET_BBOX_H
    if _NATE_TARGET_BBOX_H is not None:
        return _NATE_TARGET_BBOX_H

    out_size = (PLAYER_W, PLAYER_H)

    med_p = _first_image_path(NATE_MEDIUM_IDLE_DIR)
    low_p = _first_image_path(NATE_LOW_IDLE_DIR)
    high_p = _first_image_path(NATE_HIGH_MOVE_DIR)

    # Fall back to sensible defaults if anything is missing.
    med = _measure_scaled_bbox_h(med_p, out_size, min_alpha=1) if med_p else int(out_size[1] * 0.88)
    low = _measure_scaled_bbox_h(low_p, out_size, min_alpha=1) if low_p else int(med * 0.59)
    high = _measure_scaled_bbox_h(high_p, out_size, min_alpha=1) if high_p else int(med * 1.05)

    _NATE_TARGET_BBOX_H = (int(med), int(low), int(high))
    return _NATE_TARGET_BBOX_H


def load_scaled_images_consistent(
    folder: str,
    size: tuple[int, int],
    target_bbox_h: int,
    *,
    min_alpha: int = 1,
    bottom_align: bool = True,
    use_smooth: bool = True,
    x_anchor: str = "center",  # 'center' | 'feet'
    x_anchor_window_px: int = 40,
) -> list[pygame.Surface]:
    """Load frames from folder with consistent *scale* across frames.

    Key fixes for messy sprite packs:
    - If frames in the same folder have mixed source resolutions, we normalize them to the most common size.
    - We crop each frame to its opaque bbox (removes inconsistent transparent padding).
    - We compute a single scale factor for the whole folder based on the folder's median bbox height,
      so wide/tall poses don't cause per-frame shrinking.

    Optional jitter fix:
    - x_anchor='feet' will align frames horizontally using a "feet" anchor measured near the bottom
      of the sprite, reducing left/right sliding caused by per-frame cropping changes.
    """
    key = (
        folder,
        size,
        int(target_bbox_h),
        int(min_alpha),
        bool(bottom_align),
        bool(use_smooth),
        str(x_anchor),
        int(x_anchor_window_px),
    )
    cached = _SPRITE_FRAME_CACHE.get(key)
    if cached is not None:
        return cached

    frames: list[pygame.Surface] = []
    if not os.path.isdir(folder):
        print(f"[WARN] Missing folder: {folder}")
        _SPRITE_FRAME_CACHE[key] = frames
        return frames

    cw, ch = size
    target_bbox_h = max(1, int(target_bbox_h))
    min_alpha = max(1, int(min_alpha))
    scale_fn = pygame.transform.smoothscale if use_smooth else pygame.transform.scale
    x_anchor = (x_anchor or "center").lower().strip()
    if x_anchor not in ("center", "feet"):
        x_anchor = "center"
    x_anchor_window_px = max(1, int(x_anchor_window_px))

    # Load all source frames first.
    src_frames: list[pygame.Surface] = []
    sizes: dict[tuple[int, int], int] = {}
    for fname in sorted(os.listdir(folder)):
        low = fname.lower()
        if not (low.endswith(".png") or low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".gif")):
            continue
        path = os.path.join(folder, fname)
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception as e:
            print(f"[WARN] Failed to load {path}: {e}")
            continue
        src_frames.append(img)
        sizes[img.get_size()] = sizes.get(img.get_size(), 0) + 1

    if not src_frames:
        _SPRITE_FRAME_CACHE[key] = frames
        return frames

    # Normalize mixed-resolution folders to their most common source size.
    mode_size = max(sizes.items(), key=lambda kv: kv[1])[0]
    norm_frames: list[pygame.Surface] = []
    for img in src_frames:
        if img.get_size() != mode_size:
            try:
                img = pygame.transform.smoothscale(img, mode_size)
            except Exception:
                try:
                    img = pygame.transform.scale(img, mode_size)
                except Exception:
                    pass
        norm_frames.append(img)

    # Crop to opaque bbox and collect bbox heights for a stable reference.
    crops: list[pygame.Surface] = []
    bbox_hs: list[int] = []
    for img in norm_frames:
        try:
            bb = img.get_bounding_rect(min_alpha=min_alpha)
        except Exception:
            bb = pygame.Rect(0, 0, img.get_width(), img.get_height())
        if bb.width <= 0 or bb.height <= 0:
            bb = pygame.Rect(0, 0, img.get_width(), img.get_height())
        crop = img.subsurface(bb).copy()
        crops.append(crop)
        bbox_hs.append(int(bb.height))

    bbox_hs.sort()
    ref_h = max(1, int(bbox_hs[len(bbox_hs) // 2]))
    base_scale = target_bbox_h / float(ref_h)
    base_scale = max(0.01, float(base_scale))

    # Compute a uniform fit scale for the whole folder so no frame triggers an extra shrink.
    max_w = 1
    max_h = 1
    for crop in crops:
        max_w = max(max_w, int(crop.get_width() * base_scale))
        max_h = max(max_h, int(crop.get_height() * base_scale))
    fit_scale = 1.0
    if max_w > cw or max_h > ch:
        fit_scale = min(cw / max_w, ch / max_h)
        fit_scale = max(0.01, float(fit_scale))

    final_scale = base_scale * fit_scale

    # Scale all crops first (small surfaces), then place them onto the fixed canvas.
    scaled_arts: list[pygame.Surface] = []
    dims: list[tuple[int, int]] = []
    anchors_x: list[int] = []
    for crop in crops:
        new_w = max(1, int(crop.get_width() * final_scale))
        new_h = max(1, int(crop.get_height() * final_scale))
        try:
            art = scale_fn(crop, (new_w, new_h))
        except Exception:
            art = pygame.transform.scale(crop, (new_w, new_h))
        scaled_arts.append(art)
        dims.append((new_w, new_h))

        if x_anchor == "feet":
            ax = _opaque_anchor_x_bottom(art, min_alpha=min_alpha, window_px=x_anchor_window_px)
        else:
            ax = new_w // 2
        anchors_x.append(int(ax))

    # Choose a single canvas x position for the anchor that keeps ALL frames inside the canvas.
    # This prevents per-frame clamping (which would reintroduce jitter) and avoids clipping.
    anchor_ref = cw // 2
    if x_anchor == "feet" and dims:
        lo = 0
        hi = cw - 1
        for (new_w, _new_h), ax in zip(dims, anchors_x):
            lo = max(lo, int(ax))
            hi = min(hi, int(cw - new_w + ax))
        if lo <= hi:
            anchor_ref = max(lo, min(hi, anchor_ref))

    for art, (new_w, new_h), ax in zip(scaled_arts, dims, anchors_x):
        canvas = pygame.Surface((cw, ch), pygame.SRCALPHA, 32)
        if x_anchor == "feet":
            x = int(anchor_ref - ax)
            # If the safe intersection range was empty (rare), clamp to avoid blitting off-canvas.
            x = max(0, min(cw - new_w, x))
        else:
            x = (cw - new_w) // 2
        y = (ch - new_h) if bottom_align else (ch - new_h) // 2
        canvas.blit(art, (x, y))
        frames.append(canvas)

    _SPRITE_FRAME_CACHE[key] = frames
    print(f"[INFO] Loaded {len(frames)} frames from {folder} (consistent, ref_h={ref_h}, target={target_bbox_h})")
    return frames


def _first_frame_ref_height(folder: str) -> int:
    """Compute a reference opaque-content height from the first frame in a folder."""
    if not os.path.isdir(folder):
        return PLAYER_H
    files = sorted(os.listdir(folder))
    for fname in files:
        low = fname.lower()
        if not (low.endswith(".png") or low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".gif")):
            continue
        path = os.path.join(folder, fname)
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception:
            continue
        try:
            bbox = img.get_bounding_rect(min_alpha=1)
        except Exception:
            bbox = pygame.Rect(0, 0, img.get_width(), img.get_height())
        if bbox.height > 0:
            return int(bbox.height)
        return int(img.get_height())
    return PLAYER_H

def _sample_bbox_height(folder: str, sample_count: int = 8) -> int:
    """Estimate a stable bbox height by sampling multiple frames (median)."""
    if not os.path.isdir(folder):
        return PLAYER_H
    files = [f for f in sorted(os.listdir(folder)) if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif"))]
    if not files:
        return PLAYER_H
    n = len(files)
    if n <= sample_count:
        idxs = list(range(n))
    else:
        step = (n - 1) / max(1, (sample_count - 1))
        idxs = [int(round(i * step)) for i in range(sample_count)]
        idxs = sorted(set(min(n - 1, max(0, i)) for i in idxs))

    heights: list[int] = []
    for i in idxs:
        path = os.path.join(folder, files[i])
        try:
            img = pygame.image.load(path).convert_alpha()
        except Exception:
            continue
        try:
            bbox = img.get_bounding_rect(min_alpha=1)
        except Exception:
            bbox = pygame.Rect(0, 0, img.get_width(), img.get_height())
        h = int(bbox.height) if bbox.height > 0 else int(img.get_height())
        heights.append(h)

    if not heights:
        return PLAYER_H
    heights.sort()
    return int(heights[len(heights) // 2])

def load_specific_scaled_image(folder: str, filename: str, size: tuple[int, int]) -> pygame.Surface | None:
    """Load a specific image from a folder and scale it.

    Returns None if the file can't be loaded.
    """
    if not os.path.isdir(folder):
        return None

    # Try exact match first, then case-insensitive match.
    exact_path = os.path.join(folder, filename)
    cand_path = exact_path
    if not os.path.isfile(cand_path):
        target_low = filename.lower()
        for f in os.listdir(folder):
            if f.lower() == target_low:
                cand_path = os.path.join(folder, f)
                break

    if not os.path.isfile(cand_path):
        return None

    try:
        img = pygame.image.load(cand_path).convert_alpha()
        return pygame.transform.smoothscale(img, size)
    except Exception as e:
        print(f"[WARN] Failed to load hold frame {cand_path}: {e}")
        return None


class FrameAnim:
    """Frame animation helper.

    - loop=True: cycles 0..N-1 repeatedly
    - loop=False: plays to end and holds last frame (done=True)

    update() returns (advanced, looped)
    """

    def __init__(self, frames: list[pygame.Surface], fps: int, loop: bool = True):
        self.frames = frames
        self.loop = loop
        self.index = 0
        self.done = False
        self.frame_delay_ms = int(1000 / max(1, fps))
        self.last_tick = pygame.time.get_ticks()

    def reset(self):
        self.index = 0
        self.done = False
        self.last_tick = pygame.time.get_ticks()

    def update(self) -> tuple[bool, bool]:
        """Advance animation.

        Returns:
          advanced: True if the frame index changed
          looped:   True if it wrapped from end -> start this update
        """
        if self.done or not self.frames:
            return (False, False)

        now = pygame.time.get_ticks()
        if now - self.last_tick < self.frame_delay_ms:
            return (False, False)

        self.last_tick = now
        self.index += 1

        looped = False
        if self.index >= len(self.frames):
            if self.loop:
                self.index = 0
                looped = True
            else:
                self.index = len(self.frames) - 1
                self.done = True

        return (True, looped)

    def current(self) -> pygame.Surface | None:
        if not self.frames:
            return None
        return self.frames[self.index]


class Fighter:
    def __init__(self, x: int, color: tuple[int, int, int], controls: dict[str, int], facing_right: bool):
        self.rect = pygame.Rect(x, get_ground_y() - PLAYER_H, PLAYER_W, PLAYER_H)
        self.color = color
        self.controls = controls
        self.health = 100

        # Scoring: +200 points for each 10% (10 health) knocked off the opponent.
        # Keep a per-fighter tally of how many 10-health "steps" they've lost this round.
        self.score = 0
        self._tens_lost = 0

        # Character name (used for end-of-match text)
        self.name = 'nate'
        # Visual anchoring (optional per character)
        self.anchor_feet = False
        self.feet_y_nudge = 0  # +down / -up fine-tune


        # End-of-match state (None / 'win' / 'lose')
        self.end_state: str | None = None
        self.end_win_anim = FrameAnim(load_scaled_images(NATE_END_WIN_DIR, (PLAYER_W, PLAYER_H)), INTRO_FPS, loop=False)
        self.end_lose_anim = FrameAnim(load_scaled_images(NATE_END_LOSE_DIR, (PLAYER_W, PLAYER_H)), INTRO_FPS, loop=False)

        # Facing is determined each frame (auto-face)
        self.facing_right = facing_right

        # Stances
        self.stance = "medium"  # medium/low/high


        # Medium animations
        self.medium_idle = FrameAnim(load_scaled_images(NATE_MEDIUM_IDLE_DIR, (PLAYER_W, PLAYER_H)), IDLE_FPS, loop=True)
        self.medium_move_fwd = FrameAnim(load_scaled_images(NATE_MEDIUM_MOVE_FWD_DIR, (PLAYER_W, PLAYER_H)), MOVE_FPS, loop=True)
        self.medium_move_back = FrameAnim(load_scaled_images(NATE_MEDIUM_MOVE_BACK_DIR, (PLAYER_W, PLAYER_H)), MOVE_FPS, loop=True)

        # Medium block (two variants; chosen on press)
        self.medium_block1 = FrameAnim(load_scaled_images(NATE_MEDIUM_BLOCK1_DIR, (PLAYER_W, PLAYER_H)), BLOCK_FPS, loop=False)
        self.medium_block2 = FrameAnim(load_scaled_images(NATE_MEDIUM_BLOCK2_DIR, (PLAYER_W, PLAYER_H)), BLOCK_FPS, loop=False)
        self._block_anim: FrameAnim | None = None

        # Medium attacks
        self.attack_r_anim = FrameAnim(load_scaled_images(NATE_MEDIUM_ATTACK_R_DIR, (PLAYER_W, PLAYER_H)), ATTACK_FPS, loop=False)
        self.attack_e_anim = FrameAnim(load_scaled_images(NATE_MEDIUM_ATTACK_E_DIR, (PLAYER_W, PLAYER_H)), ATTACK_FPS, loop=False)
        self.attack_t_anim = FrameAnim(load_scaled_images(NATE_MEDIUM_ATTACK_T_DIR, (PLAYER_W, PLAYER_H)), ATTACK_FPS, loop=False)
        self.attack_y_anim = FrameAnim(load_scaled_images(NATE_MEDIUM_ATTACK_Y_DIR, (PLAYER_W, PLAYER_H)), ATTACK_FPS, loop=False)

        # Medium hit reaction
        self.hit_anim = FrameAnim(load_scaled_images(NATE_MEDIUM_HIT_DIR, (PLAYER_W, PLAYER_H)), HIT_FPS, loop=False)
        # Low stance animations
        # - Movement uses the SAME sprite sequence for forward and backward (your note).
        # - Only R attack is implemented for low stance (per provided sprites).
        self.low_idle = FrameAnim(load_scaled_images(NATE_LOW_IDLE_DIR, (PLAYER_W, PLAYER_H)), IDLE_FPS, loop=True)
        self.low_move = FrameAnim(load_scaled_images(NATE_LOW_MOVE_DIR, (PLAYER_W, PLAYER_H)), MOVE_FPS, loop=True)

        self.low_block = FrameAnim(load_scaled_images(NATE_LOW_BLOCK_DIR, (PLAYER_W, PLAYER_H)), BLOCK_FPS, loop=False)
        self._low_block_anim: FrameAnim | None = None

        self.low_attack_r_anim = FrameAnim(load_scaled_images(NATE_LOW_ATTACK_R_DIR, (PLAYER_W, PLAYER_H)), ATTACK_FPS, loop=False)

        self.low_hit_anim = FrameAnim(load_scaled_images(NATE_LOW_HIT_DIR, (PLAYER_W, PLAYER_H)), HIT_FPS, loop=False)

        # Current medium sub-state
        self.medium_state = "idle"  # idle/move_fwd/move_back/block/hit/attack_* 

        # Current low (crouch) sub-state
        self.low_state = "idle"  # idle/move_fwd/move_back/block/hit/attack_*

        # State flags
        self.is_blocking = False
        self.is_attacking = False
        self.is_hit = False

        # Air / jump state
        self.in_air = False
        self.vy = 0.0
        self.jump_dx = 0  # locked at takeoff

        # High stance (jump) animations
        # - Movement uses one sequence (used for straight/diagonal jump visuals for now)
        # - Attack is a single flop (non-looping)
        # - Hit is non-looping (played while falling)
        self.high_move = FrameAnim(load_scaled_images(NATE_HIGH_MOVE_DIR, (PLAYER_W, PLAYER_H)), MOVE_FPS, loop=True)
        self.high_attack = FrameAnim(load_scaled_images(NATE_HIGH_ATTACK_DIR, (PLAYER_W, PLAYER_H)), ATTACK_FPS, loop=False)
        self.high_hit = FrameAnim(load_scaled_images(NATE_HIGH_HIT_DIR, (PLAYER_W, PLAYER_H)), HIT_FPS, loop=False)

        # During knockdown stun after an AIR hit, we want to hold a very specific frame.
        # Prefer the configured filename if present; otherwise fall back to the last frame
        # of the high hit animation.
        self.air_knockdown_hold_frame: pygame.Surface | None = load_specific_scaled_image(
            NATE_HIGH_HIT_DIR,
            AIR_KNOCKDOWN_HOLD_FILENAME,
            (PLAYER_W, PLAYER_H),
        )
        if (self.air_knockdown_hold_frame is None) and self.high_hit.frames:
            self.air_knockdown_hold_frame = self.high_hit.frames[-1]

        # Air substates: move/attack/hit
        self.air_state = 'move'
        self.air_attack_used = False
        self.air_attack_damage_done = False
        self.air_was_hit = False

        # Recovery after landing from an air attack (flop ends on the floor)
        self.air_land_recover_until = 0

        # Knockdown (after being hit in the air and landing)
        self.knockdown_until = 0  # pygame ticks (ms)
        # Hitstun / blockstun timers (ms). These gate inputs like classic MK.
        self.hitstun_until = 0
        self.blockstun_until = 0
        self.forced_block_until = 0  # prevents releasing block during blockstun

        # Attack state (tap = finish current cycle, hold = repeat)
        self._active_attack: str | None = None  # "r", "e", "t", or "y"
        self._release_pending = False
        self._damage_done_this_cycle = False

        # Edge detection
        self._prev_r = False
        self._prev_e = False
        self._prev_t = False
        self._prev_y = False
        self._prev_jump = False

    @property
    def flip(self) -> bool:
        return not self.facing_right

    def current_frame_info(self):
        """Return (img_surface, anim_key, frame_index, anim_obj) for editor/debug."""
        # End states
        if self.end_state == 'win':
            return (self.end_win_anim.current(), 'end_win', self.end_win_anim.index, self.end_win_anim)
        if self.end_state == 'lose':
            return (self.end_lose_anim.current(), 'end_lose', self.end_lose_anim.index, self.end_lose_anim)

        # Air/high
        if self.in_air or self.stance == 'high':
            if (not self.in_air) and self.is_knocked_down():
                # Knockdown hold after air hit
                if (not self.high_hit.done) and (self.air_state == 'hit'):
                    return (self.high_hit.current(), 'high_hit', self.high_hit.index, self.high_hit)
                return (self.air_knockdown_hold_frame or self.high_hit.current(), 'high_knockdown', self.high_hit.index, self.high_hit)

            if self.air_state == 'hit':
                return (self.high_hit.current(), 'high_hit', self.high_hit.index, self.high_hit)
            if self.air_state == 'attack':
                return (self.high_attack.current(), 'high_attack', self.high_attack.index, self.high_attack)
            return (self.high_move.current(), 'high_move', self.high_move.index, self.high_move)

        # Ground
        if self.stance == 'low':
            if self.low_state == 'idle':
                return (self.low_idle.current(), 'low_idle', self.low_idle.index, self.low_idle)
            if self.low_state in ('move_fwd','move_back'):
                return (self.low_move.current(), 'low_move', self.low_move.index, self.low_move)
            if self.low_state == 'block' and self._low_block_anim is not None:
                return (self._low_block_anim.current(), 'low_block', self._low_block_anim.index, self._low_block_anim)
            if self.low_state == 'hit':
                return (self.low_hit_anim.current(), 'low_hit', self.low_hit_anim.index, self.low_hit_anim)
            if self.low_state == 'attack_r':
                return (self.low_attack_r_anim.current(), 'low_attack_r', self.low_attack_r_anim.index, self.low_attack_r_anim)
            return (None, 'low_unknown', 0, None)

        # Medium
        if self.medium_state == 'idle':
            return (self.medium_idle.current(), 'med_idle', self.medium_idle.index, self.medium_idle)
        if self.medium_state == 'move_fwd':
            return (self.medium_move_fwd.current(), 'med_move_fwd', self.medium_move_fwd.index, self.medium_move_fwd)
        if self.medium_state == 'move_back':
            return (self.medium_move_back.current(), 'med_move_back', self.medium_move_back.index, self.medium_move_back)
        if self.medium_state == 'block' and self._block_anim is not None:
            return (self._block_anim.current(), 'med_block', self._block_anim.index, self._block_anim)
        if self.medium_state == 'hit':
            return (self.hit_anim.current(), 'med_hit', self.hit_anim.index, self.hit_anim)
        if self.medium_state == 'attack_r':
            return (self.attack_r_anim.current(), 'med_attack_r', self.attack_r_anim.index, self.attack_r_anim)
        if self.medium_state == 'attack_e':
            return (self.attack_e_anim.current(), 'med_attack_e', self.attack_e_anim.index, self.attack_e_anim)
        if self.medium_state == 'attack_t':
            return (self.attack_t_anim.current(), 'med_attack_t', self.attack_t_anim.index, self.attack_t_anim)
        if self.medium_state == 'attack_y':
            return (self.attack_y_anim.current(), 'med_attack_y', self.attack_y_anim.index, self.attack_y_anim)

        return (None, 'med_unknown', 0, None)

    def set_end_state(self, state: str | None):
        # state: None / 'win' / 'lose'
        self.end_state = state
        if state == 'win':
            self.end_win_anim.reset()
        elif state == 'lose':
            self.end_lose_anim.reset()


    def update_facing(self, opponent: "Fighter"):
        self.facing_right = self.rect.centerx <= opponent.rect.centerx


    def is_knocked_down(self) -> bool:
        return pygame.time.get_ticks() < getattr(self, "knockdown_until", 0)

    # -----------------------------------------------------------------------
    # Combat timing helpers (hitstun/blockstun).
    # -----------------------------------------------------------------------
    def _now(self) -> int:
        return pygame.time.get_ticks()

    def _in_hitstun(self) -> bool:
        return self._now() < getattr(self, "hitstun_until", 0)

    def _in_blockstun(self) -> bool:
        return self._now() < getattr(self, "blockstun_until", 0)

    def _stunned(self) -> bool:
        return self._in_hitstun() or self._in_blockstun()

    def _block_is_correct(self, incoming_height: str) -> bool:
        # MK-ish: standing block covers high/mid; crouch block covers low/mid.
        stance = getattr(self, "stance", "medium")
        if stance == "medium":
            return incoming_height in ("high", "mid")
        if stance == "low":
            return incoming_height in ("low", "mid")
        return False

    def _move_data_for_current_attack(self):
        # Returns MoveData or None.
        if getattr(self, "in_air", False):
            return MOVE_DB.get(("air", "attack"))
        which = getattr(self, "_active_attack", None)
        if which is None:
            return None
        stance = getattr(self, "stance", "medium")
        return MOVE_DB.get((stance, which))
    def _start_jump(self, keys: pygame.key.ScancodeWrapper):
        """Begin a jump from grounded state.

        MK-style constraints:
        - Direction is committed at takeoff (no air steering).
        - Jump is not allowed while blocking / attacking / stunned.
        """
        left_pressed = keys[self.controls["left"]]
        right_pressed = keys[self.controls["right"]]

        if left_pressed and not right_pressed:
            self.jump_dx = -JUMP_HSPEED
        elif right_pressed and not left_pressed:
            self.jump_dx = JUMP_HSPEED
        else:
            self.jump_dx = 0

        self.in_air = True
        self.vy = JUMP_VY

        # Reset air substates
        self.air_state = 'move'
        self.air_attack_used = False
        self.air_attack_damage_done = False
        self.air_was_hit = False
        self.air_land_recover_until = 0

        self._set_high_state("move")


    def update_jump(self, keys: pygame.key.ScancodeWrapper):
        """Handle jump start + airborne physics.

        Rules (MK 90s):
        - Jump direction is locked at takeoff.
        - No blocking in air.
        - One air attack per jump.
        - If hit in air, you land into a knockdown stun timer.
        """
        now = pygame.time.get_ticks()

        # If knocked down, freeze on ground until timer expires
        if self.is_knocked_down():
            self.in_air = False
            self.vy = 0
            self.jump_dx = 0
            return


        # Hitstun / blockstun gating (MK-style): no new inputs/actions while stunned.
        if self._stunned() and (not self.in_air):
            # Keep updating the current reaction anims (hit/block) and return.
            if self.stance == 'low':
                if self.low_state == 'hit':
                    self._update_hit_anim()
                elif self.low_state == 'block':
                    self._update_block_anim()
                else:
                    self.low_idle.update()
            else:
                if self.medium_state == 'hit':
                    self._update_hit_anim()
                elif self.medium_state == 'block':
                    self._update_block_anim()
                else:
                    self.medium_idle.update()
            return

        jump_held = keys[self.controls["jump"]]
        jump_pressed = jump_held and (not self._prev_jump)
        self._prev_jump = jump_held

        # Start jump (only on ground, and only if not in hit/block/attack)
        if (not self.in_air) and jump_pressed and (not self.is_hit) and (not self.is_blocking) and (not self.is_attacking):
            if self.rect.bottom >= get_ground_y():
                self._start_jump(keys)

        if not self.in_air:
            return

        # No air block
        if self.is_blocking:
            self._end_block()

        # Horizontal (locked)
        if self.jump_dx:
            self.rect.x += int(self.jump_dx)
            self.rect.x = max(0, min(WIDTH - self.rect.width, self.rect.x))

        # Vertical
        self.rect.y += int(self.vy)
        self.vy += GRAVITY

        # Land
        if self.rect.bottom >= get_ground_y():
            self.rect.bottom = get_ground_y()
            self.in_air = False
            self.vy = 0
            self.jump_dx = 0

            if self.air_was_hit:
                self.knockdown_until = now + KNOCKDOWN_MS
                self.air_was_hit = False
                # Keep the high hit animation playing while we slide into knockdown.
                # update_stance() will keep us in high stance during the stun so the
                # last frame can be held for the full duration.
                self.air_state = 'hit'
                return

            # If we landed from an air attack (flop), apply a short recovery
            if self.air_state == 'attack':
                self.air_land_recover_until = now + AIR_ATTACK_LAND_STUN_MS
                # Stay on last frame of the flop during recovery
                return

            self.air_state = 'move'

    def update_stance(self, keys: pygame.key.ScancodeWrapper):
        # No new actions while stunned (MK-style turn-taking)
        if self._stunned() or self.is_knocked_down():
            return
        """Update stance based on grounded inputs.

        - While airborne, stance is forced to 'high' (jump state).
        - While knocked down, stance is forced to 'medium' (placeholder).
        - While in hit reaction (ground), we don't swap stance mid-animation.
        """
        # Lock stance while in hit reaction so we don't swap animations mid-hit
        if self.is_hit:
            return

        # Post-air-attack recovery (on the ground): keep high stance so we can render the prone flop frame
        if (not self.in_air) and (pygame.time.get_ticks() < self.air_land_recover_until):
            self.stance = 'high'
            return

        # Airborne stance is handled by the jump system
        if self.in_air:
            self.stance = 'high'
            return


        # Knockdown after an air hit: keep high stance during stun so we can
        # hold the last frame of the high-hit sprite (no snap back to medium).
        if pygame.time.get_ticks() < self.knockdown_until:
            self.stance = 'high'
            return

        if keys[self.controls['crouch']]:
            self.stance = 'low'
        else:
            self.stance = 'medium'

    def _set_medium_state(self, new_state: str):
        if new_state == self.medium_state:
            return
        self.medium_state = new_state

        if new_state == "idle":
            self.medium_idle.reset()
        elif new_state == "move_fwd":
            self.medium_move_fwd.reset()
        elif new_state == "move_back":
            self.medium_move_back.reset()
        elif new_state.startswith("attack_"):
            # Ensure every medium-stance attack can be replayed even when animations are non-looping.
            # (Non-looping FrameAnim sets done=True at the end, so we must reset before reuse.)
            self.attack_r_anim.reset()
            self.attack_e_anim.reset()
            self.attack_t_anim.reset()
            self.attack_y_anim.reset()
        elif new_state == "hit":
            self.hit_anim.reset()


    def _set_low_state(self, new_state: str):
        if new_state == self.low_state:
            return
        self.low_state = new_state

        if new_state == "idle":
            self.low_idle.reset()
        elif new_state == "move_fwd":
            self.low_move.reset()
        elif new_state == "move_back":
            self.low_move.reset()
        elif new_state == "attack_r":
            self.low_attack_r_anim.reset()
        elif new_state == "hit":
            self.low_hit_anim.reset()


    def _set_high_state(self, new_state: str):
        """Set airborne/high-stance substate and reset matching animation."""
        current = getattr(self, "air_state", "move")
        if new_state == current:
            return
        self.air_state = new_state

        if new_state == "move":
            self.high_move.reset()
        elif new_state == "attack":
            self.high_attack.reset()
        elif new_state == "hit":
            self.high_hit.reset()

    def _active_state_setter(self):
        """Return the correct state setter based on current stance."""
        return self._set_low_state if self.stance == "low" else self._set_medium_state
    # --------------------
    # BLOCK
    # --------------------
    def _begin_block(self):
        """Begin blocking in the current stance (medium or low)."""
        if self.stance == "low":
            self._low_block_anim = self.low_block
            self._low_block_anim.reset()
        else:
            self._block_anim = random.choice([self.medium_block1, self.medium_block2])
            self._block_anim.reset()

        self.is_blocking = True

        # Cancel attacks when block starts
        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False

        (self._set_low_state if self.stance == "low" else self._set_medium_state)("block")

    def _end_block(self):
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None
        (self._set_low_state if self.stance == "low" else self._set_medium_state)("idle")

    def update_block(self, keys: pygame.key.ScancodeWrapper):
        block_held = keys[self.controls["block"]]

        if self.in_air or self.stance not in ('medium','low'):
            if self.is_blocking:
                self._end_block()
            return

        if block_held:
            if not self.is_blocking and not self.is_hit:
                self._begin_block()
        else:
            # If we're in blockstun, we cannot release block yet.
            if self._now() < self.forced_block_until:
                return
            if self.is_blocking:
                self._end_block()

    def _update_block_anim(self):
        if self.stance == "low":
            if self._low_block_anim:
                self._low_block_anim.update()
        else:
            if self._block_anim:
                self._block_anim.update()  # plays to end, then holds

    # --------------------
    # HIT REACTION
    # --------------------
    def trigger_hit(self):
        """Immediately enter hit state (used the moment damage is dealt)."""
        self.is_hit = True

        # If we were in high stance on the ground (post-air-attack recovery),
        # snap back to grounded stance so hit anim/state machines behave normally.
        if (not self.in_air) and (self.stance == "high"):
            self.stance = "medium"
            self.air_land_recover_until = 0
            self.air_state = "move"
        # Getting hit cancels block/attack
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None
        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False

        (self._set_low_state if self.stance == "low" else self._set_medium_state)("hit")


    def trigger_air_hit(self):
        """Enter air-hit state when damage is dealt while airborne.

        Air hits are handled via air_state + air_was_hit -> knockdown on landing.
        We intentionally do NOT set self.is_hit here, because grounded hit recovery
        clears that flag via grounded state updates.
        """
        # Mark that we were hit in the air so landing logic can apply knockdown.
        self.air_was_hit = True

        # Cancel block/attack immediately
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None

        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False

        # Switch to air hit animation/state
        self._set_high_state("hit")

    def _update_hit_anim(self):
        anim = self.low_hit_anim if self.stance == "low" else self.hit_anim
        anim.update()
        if anim.done and (not self._in_hitstun()):
            self.is_hit = False
            # return to idle; input processing will happen next frame
            (self._set_low_state if self.stance == "low" else self._set_medium_state)("idle")

    # --------------------
    # ATTACKS
    # --------------------
    def _begin_attack(self, which: str):
        # No attacks while stunned / knocked down
        if self._stunned() or self.is_knocked_down() or self.is_hit:
            return
        # which: "r", "e", "t", or "y"
        # Low stance currently only supports the R attack (per available sprites).
        if self.stance == "low" and which != "r":
            return

        self.is_attacking = True
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None

        self._active_attack = which
        self._release_pending = False
        self._damage_done_this_cycle = False

        setter = self._set_low_state if self.stance == "low" else self._set_medium_state
        if which == "r":
            setter("attack_r")
        elif which == "e":
            setter("attack_e")
        elif which == "t":
            setter("attack_t")
        else:
            setter("attack_y")

        # SFX: wind (swing) plays at attack start
        SOUND_MGR.play_wind()

    def _end_attack(self):
        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False
        (self._set_low_state if self.stance == 'low' else self._set_medium_state)('idle')

    def _attack_anim(self) -> FrameAnim | None:
        # Returns the correct stance-specific attack animation
        if self.stance == "low":
            # Low stance only supports the R attack (per available sprites)
            return self.low_attack_r_anim if self._active_attack == "r" else None

        # medium
        if self._active_attack == "r":
            return self.attack_r_anim
        if self._active_attack == "e":
            return self.attack_e_anim
        if self._active_attack == "t":
            return self.attack_t_anim
        if self._active_attack == "y":
            return self.attack_y_anim
        return None


    def update_attacks(self, keys: pygame.key.ScancodeWrapper):
        # Air attacks (one per jump, MK-style)
        if self.in_air:
            # Any attack button triggers the single high flop attack animation (once per jump).
            r_held = keys[self.controls['attack_r']]
            e_held = keys[self.controls['attack_e']]
            t_held = keys[self.controls['attack_t']]
            y_held = keys[self.controls['attack_y']]

            r_pressed = r_held and not self._prev_r
            e_pressed = e_held and not self._prev_e
            t_pressed = t_held and not self._prev_t
            y_pressed = y_held and not self._prev_y

            if (not self.air_attack_used) and (not self.air_was_hit) and (not self.is_hit):
                if r_pressed or e_pressed or t_pressed or y_pressed:
                    self.air_attack_used = True
                    self.air_state = 'attack'
                    self.air_attack_damage_done = False
                    self.high_attack.reset()

            self._prev_r = r_held
            self._prev_e = e_held
            self._prev_t = t_held
            self._prev_y = y_held
            return
        if self.stance not in ("medium", "low"):
            if self.is_attacking:
                self._end_attack()
            self._prev_r = keys[self.controls["attack_r"]]
            self._prev_e = keys[self.controls["attack_e"]]
            self._prev_t = keys[self.controls["attack_t"]]
            self._prev_y = keys[self.controls["attack_y"]]
            return

        r_held = keys[self.controls["attack_r"]]
        e_held = keys[self.controls["attack_e"]]
        t_held = keys[self.controls["attack_t"]]
        y_held = keys[self.controls["attack_y"]]

        r_pressed = r_held and not self._prev_r
        e_pressed = e_held and not self._prev_e
        t_pressed = t_held and not self._prev_t
        y_pressed = y_held and not self._prev_y

        # Low stance: only R attack is wired right now
        if self.stance == 'low':
            e_pressed = t_pressed = y_pressed = False
        # Start a new attack only if we're not already attacking/blocking/hit
        if not self.is_hit and not self.is_blocking and not self.is_attacking:
            if r_pressed:
                self._begin_attack("r")
            elif self.stance == "medium":
                # In low stance, only R exists; other attacks are ignored.
                if e_pressed:
                    self._begin_attack("e")
                elif t_pressed:
                    self._begin_attack("t")
                elif y_pressed:
                    self._begin_attack("y")

        # If currently attacking: decide whether to stop after this cycle
        if self.is_attacking and self._active_attack is not None:
            if self.stance == "low":
                held = r_held
            else:
                held = {
                    "r": r_held,
                    "e": e_held,
                    "t": t_held,
                    "y": y_held,
                }[self._active_attack]

            if not held:
                self._release_pending = True
            else:
                # If held again, keep looping
                self._release_pending = False

        self._prev_r = r_held
        self._prev_e = e_held
        self._prev_t = t_held
        self._prev_y = y_held

    def _attack_hits(self, opponent: "Fighter") -> bool:
        """Return True if this attack connects.

        Prefer MK-style per-frame hitbox-vs-hurtbox checks if boxes exist in HITBOX_DB.
        Fall back to legacy reach-rect vs opponent.rect when boxes are missing.
        """
        # Try per-frame box collision first
        try:
            _p_push, _p_hurt, p_hit = hb_get_world_boxes(self)
            _o_push, o_hurt, _o_hit = hb_get_world_boxes(opponent)
            if p_hit and o_hurt:
                for hr in p_hit:
                    for ur in o_hurt:
                        if hr.colliderect(ur):
                            return True
                return False
        except Exception:
            pass

        # Legacy fallback
        if self.facing_right:
            reach = pygame.Rect(self.rect.right, self.rect.y, ATTACK_RANGE_PAD, self.rect.height)
        else:
            reach = pygame.Rect(self.rect.left - ATTACK_RANGE_PAD, self.rect.y, ATTACK_RANGE_PAD, self.rect.height)
        return reach.colliderect(opponent.rect)

    def _deal_damage_now(self, opponent: "Fighter"):
        md = self._move_data_for_current_attack()
        if md is None:
            return

        # If attack would connect spatially
        if not self._attack_hits(opponent):
            return

        # High attacks can whiff over crouchers (very MK)
        if md.height == "high" and opponent.stance == "low" and (not opponent.in_air):
            return

        # Determine block correctness
        blocked = opponent.is_blocking and opponent._block_is_correct(md.height)

        if blocked:
            # Blockstun + small pushback; no HP loss (per your spec: damage taken only when HP decreases)
            now = pygame.time.get_ticks()
            opponent.blockstun_until = max(opponent.blockstun_until, now + md.blockstun_ms)
            opponent.forced_block_until = max(opponent.forced_block_until, now + md.blockstun_ms)

            # Keep opponent in block state (even if player releases during blockstun)
            opponent.is_blocking = True
            (opponent._set_low_state if opponent.stance == "low" else opponent._set_medium_state)("block")

            # Pushback (smaller than on hit)
            push = max(6, md.knockback_px // 2)
            if self.facing_right:
                opponent.rect.x += push
            else:
                opponent.rect.x -= push
            opponent.rect.x = max(0, min(WIDTH - opponent.rect.width, opponent.rect.x))
            return

        # HIT: apply HP damage + hitstun/knockback/knockdown
        pre_health = opponent.health
        opponent.health = max(0, opponent.health - md.damage)

        # Only play impact SFX if HP actually decreased
        if opponent.health < pre_health:
            SOUND_MGR.play_hit()
            SOUND_MGR.play_damage_taken()

            # Award score based on how many 10-health thresholds were crossed.
            # Example: 100 -> 85 crosses 90, so +200.
            # Fixed: scoring now triggers on ANY successful damage, not only knockdowns.
            old_tens_lost = (100 - pre_health) // 10
            new_tens_lost = (100 - opponent.health) // 10
            if new_tens_lost > opponent._tens_lost:
                self.score += (new_tens_lost - opponent._tens_lost) * 200
                opponent._tens_lost = new_tens_lost
            else:
                # Keep the counter in sync even if something external modified health.
                opponent._tens_lost = max(opponent._tens_lost, new_tens_lost)

        # Hitstun timer
        now = pygame.time.get_ticks()
        opponent.hitstun_until = max(opponent.hitstun_until, now + md.hitstun_ms)

        # Trigger hit animation immediately (but keep it held until hitstun expires)
        if opponent.in_air:
            opponent.trigger_air_hit()
        else:
            opponent.trigger_hit()

        # Knockback
        if self.facing_right:
            opponent.rect.x += md.knockback_px
        else:
            opponent.rect.x -= md.knockback_px
        opponent.rect.x = max(0, min(WIDTH - opponent.rect.width, opponent.rect.x))

        # Knockdown (e.g., sweep)
        if md.knockdown_ms > 0 and (not opponent.in_air):
            opponent.knockdown_until = max(opponent.knockdown_until, now + md.knockdown_ms)

    def _update_attack_anim_and_damage(self, opponent: "Fighter"):
        anim = self._attack_anim()
        if anim is None:
            self._end_attack()
            return

        # Advance animation
        advanced, looped = anim.update()

        # Deal damage exactly on the specified active frame (no end-of-cycle delay)
        if advanced and (not self._damage_done_this_cycle):
            active_idx = None
            if self._active_attack is not None:
                if self.stance == "low" and self._active_attack == "r":
                    # Low R: 00065..00070 (6 frames). Default hit on LAST frame (00070).
                    active_idx = max(0, len(anim.frames) - 1) if anim.frames else 0
                else:
                    active_idx = ATTACK_ACTIVE_FRAME_INDEX.get(self._active_attack, None)
                    if active_idx is not None and anim.frames:
                        # Clamp in case this character's attack has fewer frames.
                        active_idx = min(active_idx, len(anim.frames) - 1)

            if active_idx is not None and anim.index == active_idx:
                self._damage_done_this_cycle = True
                self._deal_damage_now(opponent)

        # End attack when animation completes (single-shot attacks)
        if anim.done:
            self._end_attack()
            self._damage_done_this_cycle = False
    # --------------------
    # MOVEMENT
    # --------------------
    def update_movement(self, keys: pygame.key.ScancodeWrapper):
        # No new actions while stunned (MK-style turn-taking)
        if self._stunned() or self.is_knocked_down():
            return
        left = keys[self.controls["left"]]
        right = keys[self.controls["right"]]

        # no movement during hit, block, or attack
        if self.is_hit or self.is_blocking or self.is_attacking:
            return

        # only support medium + low for now
        if self.stance not in ("medium", "low"):
            return

        dx = 0
        if left and not right:
            dx = -MOVE_SPEED
        elif right and not left:
            dx = MOVE_SPEED

        setter = self._set_low_state if self.stance == "low" else self._set_medium_state

        if dx == 0:
            setter("idle")
            return

        # Apply movement
        self.rect.x += dx
        self.rect.x = max(0, min(WIDTH - self.rect.width, self.rect.x))

        # Choose forward/back animation relative to facing
        moving_right = dx > 0
        if self.facing_right:
            setter("move_fwd" if moving_right else "move_back")
        else:
            setter("move_fwd" if not moving_right else "move_back")

    # --------------------
    # UPDATE / DRAW
    # --------------------
    def update(self, keys: pygame.key.ScancodeWrapper, opponent: "Fighter"):
        # If the match is over, only play win/lose animation.
        if self.end_state == 'win':
            self.end_win_anim.update()
            return
        if self.end_state == 'lose':
            self.end_lose_anim.update()
            return
        # Jump start + airborne physics first (may set stance to 'high')
        self.update_jump(keys)

        # Knockdown (after an air hit): no input/actions until timer expires.
        # IMPORTANT: do NOT force medium stance here, or the high-hit animation/hold
        # frame will snap back to medium the instant we touch the floor.
        if self.is_knocked_down():
            self.stance = 'high'
            # While stunned, finish the high-hit animation if it hasn't completed yet,
            # then hold the configured last frame in draw().
            if self.air_state == 'hit' and (not self.high_hit.done):
                self.high_hit.update()
            return

        # Safety: if we're on the ground and no longer in knockdown or post-air-attack recovery,
        # we must not remain in 'high' stance. This prevents rare cases where a high-hit/knockdown
        # state leaves the fighter visually "falling" until hit again.
        now = pygame.time.get_ticks()
        if (not self.in_air) and (self.stance == 'high') and (now >= self.knockdown_until) and (now >= self.air_land_recover_until):
            self.stance = 'medium'
            self.air_state = 'move'
            self.air_was_hit = False

        self.update_stance(keys)

        # High stance (jump / post-air-attack recovery): play high animations (move/attack/hit).
        # While in high stance we skip the ground stance state machines.
        if self.stance == 'high':
            now = pygame.time.get_ticks()

            # Post-air-attack recovery on the ground: hold the final prone frame briefly.
            if (not self.in_air) and (now < self.air_land_recover_until):
                # Ensure we are holding the attack anim's last frame
                self.air_state = 'attack'
                # Do not advance animation during the hold (keeps the last frame)
                return

            # If recovery just ended, return control to ground stance.
            if (not self.in_air) and (self.air_land_recover_until != 0) and (now >= self.air_land_recover_until):
                self.air_land_recover_until = 0
                self.air_state = 'move'
                self.air_attack_damage_done = False
                # Fall back to normal grounded stance selection next update
                return


            # Start air attack on R (one per jump), direction locked at takeoff (MK 90s)
            r_held = keys[self.controls["attack_r"]]
            r_pressed = r_held and (not self._prev_r)
            if self.in_air and (not self.air_attack_used) and r_pressed and (self.air_state == 'move'):
                self.air_state = 'attack'
                self.air_attack_used = True
                self.air_attack_damage_done = False
                self._active_attack = "high_r"
                self.high_attack.reset()
                return
            # Air hit: play hit anim while falling (non-looping)
            if self.air_state == 'hit':
                self.high_hit.update()
                return

            # Air attack: play flop anim while airborne; deal damage on active frame
            if self.air_state == 'attack':
                advanced, _ = self.high_attack.update()
                if advanced and (not self.air_attack_damage_done) and self.high_attack.frames:
                    # Hit on the configured active frame (frame-accurate contact)
                    active_idx = ATTACK_ACTIVE_FRAME_INDEX.get('high_r', None)
                    if (active_idx is not None) and (self.high_attack.index == active_idx):
                        self.air_attack_damage_done = True
                        if not (opponent.is_blocking and (not opponent.in_air)):
                            if self._attack_hits(opponent):
                                opponent.health = max(0, opponent.health - ATTACK_DAMAGE)
                                if opponent.in_air:
                                    opponent.trigger_air_hit()
                                else:
                                    opponent.trigger_hit()
                return

            # Default airborne movement anim
            self.high_move.update()
            return

        # Only medium + low ground stances have sprite state machines right now.

        # Only medium + low ground stances have sprite state machines right now.
        if self.stance not in ('medium', 'low'):
            return

        # If we're in hit reaction, play it and return (short hit-stun)
        if self.is_hit:
            self._update_hit_anim()
            return

        # Block first; if blocking, ignore attacks
        self.update_block(keys)
        if not self.is_blocking:
            self.update_attacks(keys)

        # Movement (only if not blocking/attacking)
        self.update_movement(keys)

        # Update animation for the active stance/state
        if self.stance == "low":
            if self.low_state == "idle":
                self.low_idle.update()
            elif self.low_state == "move_fwd":
                self.low_move.update()
            elif self.low_state == "move_back":
                self.low_move.update()
            elif self.low_state == "block":
                self._update_block_anim()
            elif self.low_state == "hit":
                self._update_hit_anim()
            elif self.low_state in ("attack_r",):
                self._update_attack_anim_and_damage(opponent)
        else:
            if self.medium_state == "idle":
                self.medium_idle.update()
            elif self.medium_state == "move_fwd":
                self.medium_move_fwd.update()
            elif self.medium_state == "move_back":
                self.medium_move_back.update()
            elif self.medium_state == "block":
                self._update_block_anim()
            elif self.medium_state == "hit":
                self._update_hit_anim()
            elif self.medium_state in ("attack_r", "attack_e", "attack_t", "attack_y"):
                self._update_attack_anim_and_damage(opponent)

    def draw(self, surf: pygame.Surface):
        """Draw the fighter's current animation frame.

        Note: Some characters (e.g. Scorpion) may have sprite sheets with different
        transparent padding/cropping. When `self.anchor_feet` is True, we align the
        bottom-most opaque pixel of the current frame to the fighter's ground line.
        """

        def _blit(img: pygame.Surface | None) -> None:
            if img is None:
                pygame.draw.rect(surf, self.color, self.rect, 2)
                return

            if self.flip:
                img = pygame.transform.flip(img, True, False)

            if getattr(self, "anchor_feet", False):
                bottom = _opaque_bottom_y(img)
                y_shift = (img.get_height() - 1 - bottom) + int(getattr(self, "feet_y_nudge", 0))
                surf.blit(img, (self.rect.left, self.rect.top + y_shift))
            else:
                surf.blit(img, self.rect.topleft)

        # End-of-match win/lose animations (loop)
        if self.end_state == "win":
            _blit(self.end_win_anim.current())
            return
        if self.end_state == "lose":
            _blit(self.end_lose_anim.current())
            return

        # Airborne / high stance drawing
        if self.in_air or self.stance == "high":
            img = None

            # If we were hit in the air and are currently in the landing stun,
            # keep showing the high-hit sequence; once it finishes, hold the
            # configured last-frame sprite for the full stun duration.
            if (not self.in_air) and self.is_knocked_down():
                if (not self.high_hit.done) and (self.air_state == "hit"):
                    img = self.high_hit.current()
                else:
                    img = self.air_knockdown_hold_frame or self.high_hit.current()
            elif self.air_state == "hit":
                img = self.high_hit.current()
            elif self.air_state == "attack":
                img = self.high_attack.current()
            else:
                img = self.high_move.current()

            _blit(img)
            return

        if self.stance not in ("medium", "low"):
            pygame.draw.rect(surf, self.color, self.rect, 2)
            return

        img = None

        if self.stance == "low":
            if self.low_state == "idle":
                img = self.low_idle.current()
            elif self.low_state in ("move_fwd", "move_back"):
                img = self.low_move.current()
            elif self.low_state == "block" and self._low_block_anim is not None:
                img = self._low_block_anim.current()
            elif self.low_state == "hit":
                img = self.low_hit_anim.current()
            elif self.low_state == "attack_r":
                img = self.low_attack_r_anim.current()
        else:
            if self.medium_state == "idle":
                img = self.medium_idle.current()
            elif self.medium_state == "move_fwd":
                img = self.medium_move_fwd.current()
            elif self.medium_state == "move_back":
                img = self.medium_move_back.current()
            elif self.medium_state == "block" and self._block_anim is not None:
                img = self._block_anim.current()
            elif self.medium_state == "hit":
                img = self.hit_anim.current()
            elif self.medium_state == "attack_r":
                img = self.attack_r_anim.current()
            elif self.medium_state == "attack_e":
                img = self.attack_e_anim.current()
            elif self.medium_state == "attack_t":
                img = self.attack_t_anim.current()
            elif self.medium_state == "attack_y":
                img = self.attack_y_anim.current()

        _blit(img)


class Connor(Fighter):
    def __init__(self, x: int, color: tuple[int, int, int], controls: dict[str, int], facing_right: bool):
        super().__init__(x, color, controls, facing_right)

        self.name = 'connor'
        # Frames are bottom-aligned onto a fixed canvas during load, so we don't need runtime foot anchoring.
        self.anchor_feet = False

        nate_med_h, nate_low_h, _nate_high_h = _get_nate_target_bbox_heights()
        # Match Nate: medium is baseline; low is crouch; high should not shrink vs medium.
        self._target_h_medium = int(nate_med_h)
        self._target_h_low = int(nate_low_h)
        self._target_h_high = int(nate_med_h)

        def _load(folder: str, target_h: int, fps: int, loop: bool) -> FrameAnim:
            frames = load_scaled_images_consistent(
                folder,
                (PLAYER_W, PLAYER_H),
                target_bbox_h=target_h,
                min_alpha=1,
                bottom_align=True,
                use_smooth=not CONNOR_FAST_SCALE,
                x_anchor="feet",
            )
            return FrameAnim(frames, fps, loop)

        # End-of-match animations
        self.end_win_anim = _load(CONNOR_END_WIN_DIR, self._target_h_medium, INTRO_FPS, loop=False)
        self.end_lose_anim = _load(CONNOR_END_LOSE_DIR, self._target_h_medium, INTRO_FPS, loop=False)

        # Medium animations
        self.medium_idle = _load(CONNOR_MEDIUM_IDLE_DIR, self._target_h_medium, IDLE_FPS, loop=True)
        self.medium_move_fwd = _load(CONNOR_MEDIUM_MOVE_FWD_DIR, self._target_h_medium, MOVE_FPS, loop=True)
        self.medium_move_back = _load(CONNOR_MEDIUM_MOVE_BACK_DIR, self._target_h_medium, MOVE_FPS, loop=True)

        self.medium_block1 = _load(CONNOR_MEDIUM_BLOCK1_DIR, self._target_h_medium, BLOCK_FPS, loop=False)
        self.medium_block2 = _load(CONNOR_MEDIUM_BLOCK2_DIR, self._target_h_medium, BLOCK_FPS, loop=False)
        self._block_anim = None

        self.attack_r_anim = _load(CONNOR_MEDIUM_ATTACK_R_DIR, self._target_h_medium, ATTACK_FPS, loop=False)
        self.attack_e_anim = _load(CONNOR_MEDIUM_ATTACK_E_DIR, self._target_h_medium, ATTACK_FPS, loop=False)
        self.attack_t_anim = _load(CONNOR_MEDIUM_ATTACK_T_DIR, self._target_h_medium, ATTACK_FPS, loop=False)
        self.attack_y_anim = _load(CONNOR_MEDIUM_ATTACK_Y_DIR, self._target_h_medium, ATTACK_FPS, loop=False)

        self.hit_anim = _load(CONNOR_MEDIUM_HIT_DIR, self._target_h_medium, HIT_FPS, loop=False)

        # Low stance animations
        self.low_idle = _load(CONNOR_LOW_IDLE_DIR, self._target_h_low, IDLE_FPS, loop=True)
        self.low_move = _load(CONNOR_LOW_MOVE_DIR, self._target_h_low, MOVE_FPS, loop=True)
        self.low_block = _load(CONNOR_LOW_BLOCK_DIR, self._target_h_low, BLOCK_FPS, loop=False)
        self._low_block_anim = None
        self.low_attack_r_anim = _load(CONNOR_LOW_ATTACK_R_DIR, self._target_h_low, ATTACK_FPS, loop=False)
        self.low_hit_anim = _load(CONNOR_LOW_HIT_DIR, self._target_h_low, HIT_FPS, loop=False)

        # High stance animations
        self.high_move = _load(CONNOR_HIGH_MOVE_DIR, self._target_h_high, MOVE_FPS, loop=True)
        self.high_attack = _load(CONNOR_HIGH_ATTACK_DIR, self._target_h_high, ATTACK_FPS, loop=False)
        self.high_hit = _load(CONNOR_HIGH_HIT_DIR, self._target_h_high, HIT_FPS, loop=False)

        self.air_knockdown_hold_frame = load_specific_scaled_image(
            CONNOR_HIGH_HIT_DIR,
            AIR_KNOCKDOWN_HOLD_FILENAME,
            (PLAYER_W, PLAYER_H),
        )
        if (self.air_knockdown_hold_frame is None) and self.high_hit.frames:
            self.air_knockdown_hold_frame = self.high_hit.frames[-1]


class Blake(Fighter):
    def __init__(self, x: int, color: tuple[int, int, int], controls: dict[str, int], facing_right: bool):
        super().__init__(x, color, controls, facing_right)

        self.name = 'blake'
        # Frames are bottom-aligned onto a fixed canvas during load, so we don't need runtime foot anchoring.
        self.anchor_feet = False

        nate_med_h, nate_low_h, _nate_high_h = _get_nate_target_bbox_heights()
        # Match Nate: medium is baseline; low is crouch; high should not shrink vs medium.
        self._target_h_medium = int(nate_med_h)
        self._target_h_low = int(nate_low_h)
        self._target_h_high = int(nate_med_h)

        def _load(folder: str, target_h: int, fps: int, loop: bool) -> FrameAnim:
            frames = load_scaled_images_consistent(
                folder,
                (PLAYER_W, PLAYER_H),
                target_bbox_h=target_h,
                min_alpha=1,
                bottom_align=True,
                use_smooth=not BLAKE_FAST_SCALE,
                x_anchor="feet",
            )
            return FrameAnim(frames, fps, loop)

        # End-of-match animations
        self.end_win_anim = _load(BLAKE_END_WIN_DIR, self._target_h_medium, INTRO_FPS, loop=False)
        self.end_lose_anim = _load(BLAKE_END_LOSE_DIR, self._target_h_medium, INTRO_FPS, loop=False)

        # Medium animations
        self.medium_idle = _load(BLAKE_MEDIUM_IDLE_DIR, self._target_h_medium, IDLE_FPS, loop=True)
        self.medium_move_fwd = _load(BLAKE_MEDIUM_MOVE_FWD_DIR, self._target_h_medium, MOVE_FPS, loop=True)
        self.medium_move_back = _load(BLAKE_MEDIUM_MOVE_BACK_DIR, self._target_h_medium, MOVE_FPS, loop=True)

        self.medium_block1 = _load(BLAKE_MEDIUM_BLOCK1_DIR, self._target_h_medium, BLOCK_FPS, loop=False)
        self.medium_block2 = _load(BLAKE_MEDIUM_BLOCK2_DIR, self._target_h_medium, BLOCK_FPS, loop=False)
        self._block_anim = None

        self.attack_r_anim = _load(BLAKE_MEDIUM_ATTACK_R_DIR, self._target_h_medium, ATTACK_FPS, loop=False)
        self.attack_e_anim = _load(BLAKE_MEDIUM_ATTACK_E_DIR, self._target_h_medium, ATTACK_FPS, loop=False)
        self.attack_t_anim = _load(BLAKE_MEDIUM_ATTACK_T_DIR, self._target_h_medium, ATTACK_FPS, loop=False)
        self.attack_y_anim = _load(BLAKE_MEDIUM_ATTACK_Y_DIR, self._target_h_medium, ATTACK_FPS, loop=False)

        self.hit_anim = _load(BLAKE_MEDIUM_HIT_DIR, self._target_h_medium, HIT_FPS, loop=False)

        # Low stance animations
        self.low_idle = _load(BLAKE_LOW_IDLE_DIR, self._target_h_low, IDLE_FPS, loop=True)
        self.low_move = _load(BLAKE_LOW_MOVE_DIR, self._target_h_low, MOVE_FPS, loop=True)
        self.low_block = _load(BLAKE_LOW_BLOCK_DIR, self._target_h_low, BLOCK_FPS, loop=False)
        self._low_block_anim = None
        self.low_attack_r_anim = _load(BLAKE_LOW_ATTACK_R_DIR, self._target_h_low, ATTACK_FPS, loop=False)
        self.low_hit_anim = _load(BLAKE_LOW_HIT_DIR, self._target_h_low, HIT_FPS, loop=False)

        # High stance animations
        self.high_move = _load(BLAKE_HIGH_MOVE_DIR, self._target_h_high, MOVE_FPS, loop=True)
        self.high_attack = _load(BLAKE_HIGH_ATTACK_DIR, self._target_h_high, ATTACK_FPS, loop=False)
        self.high_hit = _load(BLAKE_HIGH_HIT_DIR, self._target_h_high, HIT_FPS, loop=False)

        self.air_knockdown_hold_frame = load_specific_scaled_image(
            BLAKE_HIGH_HIT_DIR,
            AIR_KNOCKDOWN_HOLD_FILENAME,
            (PLAYER_W, PLAYER_H),
        )
        if (self.air_knockdown_hold_frame is None) and self.high_hit.frames:
            self.air_knockdown_hold_frame = self.high_hit.frames[-1]


class Scorpion:
    def __init__(self, x: int, color: tuple[int, int, int], controls: dict[str, int], facing_right: bool):
        self.rect = pygame.Rect(x, get_ground_y() - PLAYER_H, PLAYER_W, PLAYER_H)
        self.color = color
        self.controls = controls
        self.health = 100

        # Scoring: +200 points for each 10% (10 health) knocked off the opponent.
        # Keep a per-fighter tally of how many 10-health "steps" they've lost this round.
        self.score = 0
        self._tens_lost = 0

        # Character name (used for end-of-match text)
        self.name = 'scorpion'
        # Visual anchoring: Scorpion sprite pack is more tightly cropped than Nate.
        self.anchor_feet = True
        self.feet_y_nudge = -30  # adjust if needed

        # Combo helpers (hit-confirmed chaining to avoid unfair spam)
        self._queued_attack: str | None = None
        self._combo_chain = 0
        self._last_hit_ms = -100000
        self._combo_window_ms = 450
        self._cancel_from_frac = 0.60
        self._combo_cooldown_until = 0
        # Normalize Scorpion frames based on opaque content, keeping each stance's relative size.
        # Medium stance defines the baseline; low/high keep their natural proportion but stay consistent within the stance.
        self._ref_h_medium = _first_frame_ref_height(SCORPION_MEDIUM_IDLE_DIR)
        self._ref_h_low = _first_frame_ref_height(SCORPION_LOW_IDLE_DIR)
        self._ref_h_high = _first_frame_ref_height(SCORPION_HIGH_MOVE_DIR)
        self._target_h_medium = int(PLAYER_H * SCORPION_FIT)
        # Preserve relative stance size based on source pack proportions
        self._target_h_low = max(1, int(self._target_h_medium * (self._ref_h_low / max(1, self._ref_h_medium)) * LOW_STANCE_BOOST))
        self._target_h_high = max(1, int(self._target_h_medium * (self._ref_h_high / max(1, self._ref_h_medium))))



        # End-of-match state (None / 'win' / 'lose')
        self.end_state: str | None = None
        self.end_win_anim = FrameAnim(load_scaled_images_normalized(SCORPION_END_WIN_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), INTRO_FPS, loop=False)
        self.end_lose_anim = FrameAnim(load_scaled_images_normalized(SCORPION_END_LOSE_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), INTRO_FPS, loop=False)

        # Facing is determined each frame (auto-face)
        self.facing_right = facing_right

        # Stances
        self.stance = "medium"  # medium/low/high


        # Medium animations
        self.medium_idle = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_IDLE_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), IDLE_FPS, loop=True)
        self.medium_move_fwd = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_MOVE_FWD_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), MOVE_FPS, loop=True)
        self.medium_move_back = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_MOVE_BACK_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), MOVE_FPS, loop=True)

        # Medium block (two variants; chosen on press)
        self.medium_block1 = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_BLOCK1_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), BLOCK_FPS, loop=False)
        self.medium_block2 = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_BLOCK2_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), BLOCK_FPS, loop=False)
        self._block_anim: FrameAnim | None = None

        # Medium attacks
        self.attack_r_anim = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_ATTACK_R_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), ATTACK_FPS, loop=False)
        self.attack_e_anim = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_ATTACK_E_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), ATTACK_FPS, loop=False)
        self.attack_t_anim = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_ATTACK_T_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), ATTACK_FPS, loop=False)
        self.attack_y_anim = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_ATTACK_Y_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), ATTACK_FPS, loop=False)

        # Medium hit reaction
        self.hit_anim = FrameAnim(load_scaled_images_normalized(SCORPION_MEDIUM_HIT_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_medium), HIT_FPS, loop=False)
        # Low stance animations
        # - Movement uses the SAME sprite sequence for forward and backward (your note).
        # - Only R attack is implemented for low stance (per provided sprites).
        self.low_idle = FrameAnim(load_scaled_images_normalized(SCORPION_LOW_IDLE_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_low), IDLE_FPS, loop=True)
        self.low_move = FrameAnim(load_scaled_images_normalized(SCORPION_LOW_MOVE_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_low), MOVE_FPS, loop=True)

        self.low_block = FrameAnim(load_scaled_images_normalized(SCORPION_LOW_BLOCK_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_low), BLOCK_FPS, loop=False)
        self._low_block_anim: FrameAnim | None = None

        self.low_attack_r_anim = FrameAnim(load_scaled_images_normalized(SCORPION_LOW_ATTACK_R_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_low), ATTACK_FPS, loop=False)

        self.low_hit_anim = FrameAnim(load_scaled_images_normalized(SCORPION_LOW_HIT_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_low), HIT_FPS, loop=False)

        # Current medium sub-state
        self.medium_state = "idle"  # idle/move_fwd/move_back/block/hit/attack_* 

        # Current low (crouch) sub-state
        self.low_state = "idle"  # idle/move_fwd/move_back/block/hit/attack_*

        # State flags
        self.is_blocking = False
        self.is_attacking = False
        self.is_hit = False

        # Air / jump state
        self.in_air = False
        self.vy = 0.0
        self.jump_dx = 0  # locked at takeoff

        # High stance (jump) animations
        # - Movement uses one sequence (used for straight/diagonal jump visuals for now)
        # - Attack is a single flop (non-looping)
        # - Hit is non-looping (played while falling)
        self.high_move = FrameAnim(load_scaled_images_normalized(SCORPION_HIGH_MOVE_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_high), MOVE_FPS, loop=True)
        self.high_move_back = FrameAnim(load_scaled_images_normalized(SCORPION_HIGH_MOVE_BACK_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_high), MOVE_FPS, loop=True)
        self.high_attack = FrameAnim(load_scaled_images_normalized(SCORPION_HIGH_ATTACK_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_high), ATTACK_FPS, loop=False)
        self.high_hit = FrameAnim(load_scaled_images_normalized(SCORPION_HIGH_HIT_DIR, (PLAYER_W, PLAYER_H), ref_h=self._ref_h_medium, fit=SCORPION_FIT, target_h_override=self._target_h_high), HIT_FPS, loop=False)

        # During knockdown stun after an AIR hit, we want to hold a very specific frame.
        # Prefer the configured filename if present; otherwise fall back to the last frame
        # of the high hit animation.
        self.air_knockdown_hold_frame: pygame.Surface | None = load_specific_scaled_image(
            SCORPION_HIGH_HIT_DIR,
            AIR_KNOCKDOWN_HOLD_FILENAME,
            (PLAYER_W, PLAYER_H),
        )
        if (self.air_knockdown_hold_frame is None) and self.high_hit.frames:
            self.air_knockdown_hold_frame = self.high_hit.frames[-1]

        # Air substates: move/attack/hit
        self.air_state = 'move'
        self.air_attack_used = False
        self.air_attack_damage_done = False
        self.air_was_hit = False

        # Recovery after landing from an air attack (flop ends on the floor)
        self.air_land_recover_until = 0

        # Knockdown (after being hit in the air and landing)
        self.knockdown_until = 0  # pygame ticks (ms)
        # Hitstun / blockstun timers (ms). These gate inputs like classic MK.
        self.hitstun_until = 0
        self.blockstun_until = 0
        self.forced_block_until = 0  # prevents releasing block during blockstun

        # Attack state (tap = finish current cycle, hold = repeat)
        self._active_attack: str | None = None  # "r", "e", "t", or "y"
        self._release_pending = False
        self._damage_done_this_cycle = False

        # Edge detection
        self._prev_r = False
        self._prev_e = False
        self._prev_t = False
        self._prev_y = False
        self._prev_jump = False

    @property
    def flip(self) -> bool:
        return not self.facing_right

    def current_frame_info(self):
        """Return (img_surface, anim_key, frame_index, anim_obj) for editor/debug."""
        # End states
        if self.end_state == 'win':
            return (self.end_win_anim.current(), 'end_win', self.end_win_anim.index, self.end_win_anim)
        if self.end_state == 'lose':
            return (self.end_lose_anim.current(), 'end_lose', self.end_lose_anim.index, self.end_lose_anim)

        # Air/high
        if self.in_air or self.stance == 'high':
            if (not self.in_air) and self.is_knocked_down():
                # Knockdown hold after air hit
                if (not self.high_hit.done) and (self.air_state == 'hit'):
                    return (self.high_hit.current(), 'high_hit', self.high_hit.index, self.high_hit)
                return (self.air_knockdown_hold_frame or self.high_hit.current(), 'high_knockdown', self.high_hit.index, self.high_hit)

            if self.air_state == 'hit':
                return (self.high_hit.current(), 'high_hit', self.high_hit.index, self.high_hit)
            if self.air_state == 'attack':
                return (self.high_attack.current(), 'high_attack', self.high_attack.index, self.high_attack)
            return (self.high_move.current(), 'high_move', self.high_move.index, self.high_move)

        # Ground
        if self.stance == 'low':
            if self.low_state == 'idle':
                return (self.low_idle.current(), 'low_idle', self.low_idle.index, self.low_idle)
            if self.low_state in ('move_fwd','move_back'):
                return (self.low_move.current(), 'low_move', self.low_move.index, self.low_move)
            if self.low_state == 'block' and self._low_block_anim is not None:
                return (self._low_block_anim.current(), 'low_block', self._low_block_anim.index, self._low_block_anim)
            if self.low_state == 'hit':
                return (self.low_hit_anim.current(), 'low_hit', self.low_hit_anim.index, self.low_hit_anim)
            if self.low_state == 'attack_r':
                return (self.low_attack_r_anim.current(), 'low_attack_r', self.low_attack_r_anim.index, self.low_attack_r_anim)
            return (None, 'low_unknown', 0, None)

        # Medium
        if self.medium_state == 'idle':
            return (self.medium_idle.current(), 'med_idle', self.medium_idle.index, self.medium_idle)
        if self.medium_state == 'move_fwd':
            return (self.medium_move_fwd.current(), 'med_move_fwd', self.medium_move_fwd.index, self.medium_move_fwd)
        if self.medium_state == 'move_back':
            return (self.medium_move_back.current(), 'med_move_back', self.medium_move_back.index, self.medium_move_back)
        if self.medium_state == 'block' and self._block_anim is not None:
            return (self._block_anim.current(), 'med_block', self._block_anim.index, self._block_anim)
        if self.medium_state == 'hit':
            return (self.hit_anim.current(), 'med_hit', self.hit_anim.index, self.hit_anim)
        if self.medium_state == 'attack_r':
            return (self.attack_r_anim.current(), 'med_attack_r', self.attack_r_anim.index, self.attack_r_anim)
        if self.medium_state == 'attack_e':
            return (self.attack_e_anim.current(), 'med_attack_e', self.attack_e_anim.index, self.attack_e_anim)
        if self.medium_state == 'attack_t':
            return (self.attack_t_anim.current(), 'med_attack_t', self.attack_t_anim.index, self.attack_t_anim)
        if self.medium_state == 'attack_y':
            return (self.attack_y_anim.current(), 'med_attack_y', self.attack_y_anim.index, self.attack_y_anim)

        return (None, 'med_unknown', 0, None)

    def set_end_state(self, state: str | None):
        # state: None / 'win' / 'lose'
        self.end_state = state
        if state == 'win':
            self.end_win_anim.reset()
        elif state == 'lose':
            self.end_lose_anim.reset()


    def update_facing(self, opponent: "Fighter"):
        self.facing_right = self.rect.centerx <= opponent.rect.centerx


    def is_knocked_down(self) -> bool:
        return pygame.time.get_ticks() < getattr(self, "knockdown_until", 0)

    # -----------------------------------------------------------------------
    # Combat timing helpers (hitstun/blockstun).
    # -----------------------------------------------------------------------
    def _now(self) -> int:
        return pygame.time.get_ticks()

    def _in_hitstun(self) -> bool:
        return self._now() < getattr(self, "hitstun_until", 0)

    def _in_blockstun(self) -> bool:
        return self._now() < getattr(self, "blockstun_until", 0)

    def _stunned(self) -> bool:
        return self._in_hitstun() or self._in_blockstun()

    def _block_is_correct(self, incoming_height: str) -> bool:
        # MK-ish: standing block covers high/mid; crouch block covers low/mid.
        stance = getattr(self, "stance", "medium")
        if stance == "medium":
            return incoming_height in ("high", "mid")
        if stance == "low":
            return incoming_height in ("low", "mid")
        return False

    def _move_data_for_current_attack(self):
        # Returns MoveData or None.
        if getattr(self, "in_air", False):
            return MOVE_DB.get(("air", "attack"))
        which = getattr(self, "_active_attack", None)
        if which is None:
            return None
        stance = getattr(self, "stance", "medium")
        return MOVE_DB.get((stance, which))
    def _start_jump(self, keys: pygame.key.ScancodeWrapper):
        """Begin a jump from grounded state.

        MK-style constraints:
        - Direction is committed at takeoff (no air steering).
        - Jump is not allowed while blocking / attacking / stunned.
        """
        left_pressed = keys[self.controls["left"]]
        right_pressed = keys[self.controls["right"]]

        if left_pressed and not right_pressed:
            self.jump_dx = -JUMP_HSPEED
        elif right_pressed and not left_pressed:
            self.jump_dx = JUMP_HSPEED
        else:
            self.jump_dx = 0

        self.in_air = True
        self.vy = JUMP_VY

        # Reset air substates
        self.air_state = 'move'
        self.air_attack_used = False
        self.air_attack_damage_done = False
        self.air_was_hit = False
        self.air_land_recover_until = 0

        self._set_high_state("move")


    def update_jump(self, keys: pygame.key.ScancodeWrapper):
        """Handle jump start + airborne physics.

        Rules (MK 90s):
        - Jump direction is locked at takeoff.
        - No blocking in air.
        - One air attack per jump.
        - If hit in air, you land into a knockdown stun timer.
        """
        now = pygame.time.get_ticks()

        # If knocked down, freeze on ground until timer expires
        if self.is_knocked_down():
            self.in_air = False
            self.vy = 0
            self.jump_dx = 0
            return


        # Hitstun / blockstun gating (MK-style): no new inputs/actions while stunned.
        if self._stunned() and (not self.in_air):
            # Keep updating the current reaction anims (hit/block) and return.
            if self.stance == 'low':
                if self.low_state == 'hit':
                    self._update_hit_anim()
                elif self.low_state == 'block':
                    self._update_block_anim()
                else:
                    self.low_idle.update()
            else:
                if self.medium_state == 'hit':
                    self._update_hit_anim()
                elif self.medium_state == 'block':
                    self._update_block_anim()
                else:
                    self.medium_idle.update()
            return

        jump_held = keys[self.controls["jump"]]
        jump_pressed = jump_held and (not self._prev_jump)
        self._prev_jump = jump_held

        # Start jump (only on ground, and only if not in hit/block/attack)
        if (not self.in_air) and jump_pressed and (not self.is_hit) and (not self.is_blocking) and (not self.is_attacking):
            if self.rect.bottom >= get_ground_y():
                self._start_jump(keys)

        if not self.in_air:
            return

        # No air block
        if self.is_blocking:
            self._end_block()

        # Horizontal (locked)
        if self.jump_dx:
            self.rect.x += int(self.jump_dx)
            self.rect.x = max(0, min(WIDTH - self.rect.width, self.rect.x))

        # Vertical
        self.rect.y += int(self.vy)
        self.vy += GRAVITY

        # Land
        if self.rect.bottom >= get_ground_y():
            self.rect.bottom = get_ground_y()
            self.in_air = False
            self.vy = 0
            self.jump_dx = 0

            if self.air_was_hit:
                self.knockdown_until = now + KNOCKDOWN_MS
                self.air_was_hit = False
                # Keep the high hit animation playing while we slide into knockdown.
                # update_stance() will keep us in high stance during the stun so the
                # last frame can be held for the full duration.
                self.air_state = 'hit'
                return

            # If we landed from an air attack (flop), apply a short recovery
            if self.air_state == 'attack':
                self.air_land_recover_until = now + AIR_ATTACK_LAND_STUN_MS
                # Stay on last frame of the flop during recovery
                return

            self.air_state = 'move'

    def update_stance(self, keys: pygame.key.ScancodeWrapper):
        # No new actions while stunned (MK-style turn-taking)
        if self._stunned() or self.is_knocked_down():
            return
        """Update stance based on grounded inputs.

        - While airborne, stance is forced to 'high' (jump state).
        - While knocked down, stance is forced to 'medium' (placeholder).
        - While in hit reaction (ground), we don't swap stance mid-animation.
        """
        # Lock stance while in hit reaction so we don't swap animations mid-hit
        if self.is_hit:
            return

        # Post-air-attack recovery (on the ground): keep high stance so we can render the prone flop frame
        if (not self.in_air) and (pygame.time.get_ticks() < self.air_land_recover_until):
            self.stance = 'high'
            return

        # Airborne stance is handled by the jump system
        if self.in_air:
            self.stance = 'high'
            return


        # Knockdown after an air hit: keep high stance during stun so we can
        # hold the last frame of the high-hit sprite (no snap back to medium).
        if pygame.time.get_ticks() < self.knockdown_until:
            self.stance = 'high'
            return

        if keys[self.controls['crouch']]:
            self.stance = 'low'
        else:
            self.stance = 'medium'

    def _set_medium_state(self, new_state: str):
        if new_state == self.medium_state:
            return
        self.medium_state = new_state

        if new_state == "idle":
            self.medium_idle.reset()
        elif new_state == "move_fwd":
            self.medium_move_fwd.reset()
        elif new_state == "move_back":
            self.medium_move_back.reset()
        elif new_state.startswith("attack_"):
            # Ensure every medium-stance attack can be replayed even when animations are non-looping.
            # (Non-looping FrameAnim sets done=True at the end, so we must reset before reuse.)
            self.attack_r_anim.reset()
            self.attack_e_anim.reset()
            self.attack_t_anim.reset()
            self.attack_y_anim.reset()
        elif new_state == "hit":
            self.hit_anim.reset()


    def _set_low_state(self, new_state: str):
        if new_state == self.low_state:
            return
        self.low_state = new_state

        if new_state == "idle":
            self.low_idle.reset()
        elif new_state == "move_fwd":
            self.low_move.reset()
        elif new_state == "move_back":
            self.low_move.reset()
        elif new_state == "attack_r":
            self.low_attack_r_anim.reset()
        elif new_state == "hit":
            self.low_hit_anim.reset()


    def _set_high_state(self, new_state: str):
        """Set airborne/high-stance substate and reset matching animation."""
        current = getattr(self, "air_state", "move")
        if new_state == current:
            return
        self.air_state = new_state

        if new_state == "move":
            self.high_move.reset()
            self.high_move_back.reset()
        elif new_state == "attack":
            self.high_attack.reset()
        elif new_state == "hit":
            self.high_hit.reset()

    def _active_state_setter(self):
        """Return the correct state setter based on current stance."""
        return self._set_low_state if self.stance == "low" else self._set_medium_state
    # --------------------
    # BLOCK
    # --------------------
    def _begin_block(self):
        """Begin blocking in the current stance (medium or low)."""
        if self.stance == "low":
            self._low_block_anim = self.low_block
            self._low_block_anim.reset()
        else:
            self._block_anim = random.choice([self.medium_block1, self.medium_block2])
            self._block_anim.reset()

        self.is_blocking = True

        # Cancel attacks when block starts
        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False

        (self._set_low_state if self.stance == "low" else self._set_medium_state)("block")

    def _end_block(self):
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None
        (self._set_low_state if self.stance == "low" else self._set_medium_state)("idle")

    def update_block(self, keys: pygame.key.ScancodeWrapper):
        block_held = keys[self.controls["block"]]

        if self.in_air or self.stance not in ('medium','low'):
            if self.is_blocking:
                self._end_block()
            return

        if block_held:
            if not self.is_blocking and not self.is_hit:
                self._begin_block()
        else:
            # If we're in blockstun, we cannot release block yet.
            if self._now() < self.forced_block_until:
                return
            if self.is_blocking:
                self._end_block()

    def _update_block_anim(self):
        if self.stance == "low":
            if self._low_block_anim:
                self._low_block_anim.update()
        else:
            if self._block_anim:
                self._block_anim.update()  # plays to end, then holds

    # --------------------
    # HIT REACTION
    # --------------------
    def trigger_hit(self):
        """Immediately enter hit state (used the moment damage is dealt)."""
        self.is_hit = True

        # If we were in high stance on the ground (post-air-attack recovery),
        # snap back to grounded stance so hit anim/state machines behave normally.
        if (not self.in_air) and (self.stance == "high"):
            self.stance = "medium"
            self.air_land_recover_until = 0
            self.air_state = "move"
        # Getting hit cancels block/attack
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None
        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False

        (self._set_low_state if self.stance == "low" else self._set_medium_state)("hit")


    def trigger_air_hit(self):
        """Enter air-hit state when damage is dealt while airborne.

        Air hits are handled via air_state + air_was_hit -> knockdown on landing.
        We intentionally do NOT set self.is_hit here, because grounded hit recovery
        clears that flag via grounded state updates.
        """
        # Mark that we were hit in the air so landing logic can apply knockdown.
        self.air_was_hit = True

        # Cancel block/attack immediately
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None

        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False

        # Switch to air hit animation/state
        self._set_high_state("hit")

    def _update_hit_anim(self):
        anim = self.low_hit_anim if self.stance == "low" else self.hit_anim
        anim.update()
        if anim.done and (not self._in_hitstun()):
            self.is_hit = False
            # return to idle; input processing will happen next frame
            (self._set_low_state if self.stance == "low" else self._set_medium_state)("idle")

    # --------------------
    # ATTACKS
    # --------------------
    def _begin_attack(self, which: str):
        # No attacks while stunned / knocked down
        if self._stunned() or self.is_knocked_down() or self.is_hit:
            return
        # which: "r", "e", "t", or "y"
        # Low stance currently only supports the R attack (per available sprites).
        if self.stance == "low" and which != "r":
            return

        self.is_attacking = True
        self.is_blocking = False
        self._block_anim = None
        self._low_block_anim = None

        self._active_attack = which
        self._release_pending = False
        self._damage_done_this_cycle = False

        setter = self._set_low_state if self.stance == "low" else self._set_medium_state
        if which == "r":
            setter("attack_r")
        elif which == "e":
            setter("attack_e")
        elif which == "t":
            setter("attack_t")
        else:
            setter("attack_y")

        # SFX: wind (swing) plays at attack start
        SOUND_MGR.play_wind()

    def _end_attack(self):
        self.is_attacking = False
        self._active_attack = None
        self._release_pending = False
        self._damage_done_this_cycle = False
        (self._set_low_state if self.stance == 'low' else self._set_medium_state)('idle')

    def _attack_anim(self) -> FrameAnim | None:
        # Returns the correct stance-specific attack animation
        if self.stance == "low":
            # Low stance only supports the R attack (per available sprites)
            return self.low_attack_r_anim if self._active_attack == "r" else None

        # medium
        if self._active_attack == "r":
            return self.attack_r_anim
        if self._active_attack == "e":
            return self.attack_e_anim
        if self._active_attack == "t":
            return self.attack_t_anim
        if self._active_attack == "y":
            return self.attack_y_anim
        return None


    def update_attacks(self, keys: pygame.key.ScancodeWrapper):
        # Air attacks (one per jump, MK-style)
        if self.in_air:
            # Any attack button triggers the single high flop attack animation (once per jump).
            r_held = keys[self.controls['attack_r']]
            e_held = keys[self.controls['attack_e']]
            t_held = keys[self.controls['attack_t']]
            y_held = keys[self.controls['attack_y']]

            r_pressed = r_held and not self._prev_r
            e_pressed = e_held and not self._prev_e
            t_pressed = t_held and not self._prev_t
            y_pressed = y_held and not self._prev_y

            if (not self.air_attack_used) and (not self.air_was_hit) and (not self.is_hit):
                if r_pressed or e_pressed or t_pressed or y_pressed:
                    self.air_attack_used = True
                    self.air_state = 'attack'
                    self.air_attack_damage_done = False
                    self.high_attack.reset()

            self._prev_r = r_held
            self._prev_e = e_held
            self._prev_t = t_held
            self._prev_y = y_held
            return
        if self.stance not in ("medium", "low"):
            if self.is_attacking:
                self._end_attack()
            self._prev_r = keys[self.controls["attack_r"]]
            self._prev_e = keys[self.controls["attack_e"]]
            self._prev_t = keys[self.controls["attack_t"]]
            self._prev_y = keys[self.controls["attack_y"]]
            return

        r_held = keys[self.controls["attack_r"]]
        e_held = keys[self.controls["attack_e"]]
        t_held = keys[self.controls["attack_t"]]
        y_held = keys[self.controls["attack_y"]]

        r_pressed = r_held and not self._prev_r
        e_pressed = e_held and not self._prev_e
        t_pressed = t_held and not self._prev_t
        y_pressed = y_held and not self._prev_y

        # Low stance: only R attack is wired right now
        if self.stance == 'low':
            e_pressed = t_pressed = y_pressed = False


        # Queue follow-up attacks for smooth combos (medium stance only).
        # - requires a recent hit (hit-confirm window)
        # - allows input buffering near the end of the current attack
        if self.is_attacking and (self.stance == "medium") and (self._active_attack is not None):
            now = pygame.time.get_ticks()
            if now >= self._combo_cooldown_until:
                can_chain = (now - self._last_hit_ms) <= self._combo_window_ms and self._combo_chain < 3
                anim = self._attack_anim()
                in_cancel = False
                if anim is not None and anim.frames:
                    cancel_from = int(len(anim.frames) * float(self._cancel_from_frac))
                    in_cancel = anim.index >= cancel_from
                if can_chain and in_cancel:
                    desired = None
                    if r_pressed and self._active_attack != "r":
                        desired = "r"
                    elif e_pressed and self._active_attack != "e":
                        desired = "e"
                    elif t_pressed and self._active_attack != "t":
                        desired = "t"
                    elif y_pressed and self._active_attack != "y":
                        desired = "y"
                    if desired is not None:
                        self._queued_attack = desired

        # Anti-spam: a tiny cooldown after a short combo chain
        if (not self.is_attacking) and (pygame.time.get_ticks() < self._combo_cooldown_until):
            r_pressed = e_pressed = t_pressed = y_pressed = False

        
        # Start a new attack only if we're not already attacking/blocking/hit
        if not self.is_hit and not self.is_blocking and not self.is_attacking:
            if r_pressed:
                self._begin_attack("r")
            elif self.stance == "medium":
                # In low stance, only R exists; other attacks are ignored.
                if e_pressed:
                    self._begin_attack("e")
                elif t_pressed:
                    self._begin_attack("t")
                elif y_pressed:
                    self._begin_attack("y")

        # If currently attacking: decide whether to stop after this cycle
        if self.is_attacking and self._active_attack is not None:
            if self.stance == "low":
                held = r_held
            else:
                held = {
                    "r": r_held,
                    "e": e_held,
                    "t": t_held,
                    "y": y_held,
                }[self._active_attack]

            if not held:
                self._release_pending = True
            else:
                # If held again, keep looping
                self._release_pending = False

        self._prev_r = r_held
        self._prev_e = e_held
        self._prev_t = t_held
        self._prev_y = y_held

    def _attack_hits(self, opponent: "Fighter") -> bool:
        """Return True if this attack connects.

        Prefer MK-style per-frame hitbox-vs-hurtbox checks if boxes exist in HITBOX_DB.
        Fall back to legacy reach-rect vs opponent.rect when boxes are missing.
        """
        # Try per-frame box collision first
        try:
            _p_push, _p_hurt, p_hit = hb_get_world_boxes(self)
            _o_push, o_hurt, _o_hit = hb_get_world_boxes(opponent)
            if p_hit and o_hurt:
                for hr in p_hit:
                    for ur in o_hurt:
                        if hr.colliderect(ur):
                            return True
                return False
        except Exception:
            pass

        # Legacy fallback
        if self.facing_right:
            reach = pygame.Rect(self.rect.right, self.rect.y, ATTACK_RANGE_PAD, self.rect.height)
        else:
            reach = pygame.Rect(self.rect.left - ATTACK_RANGE_PAD, self.rect.y, ATTACK_RANGE_PAD, self.rect.height)
        return reach.colliderect(opponent.rect)

    def _deal_damage_now(self, opponent: "Fighter"):
        md = self._move_data_for_current_attack()
        if md is None:
            return

        # If attack would connect spatially
        if not self._attack_hits(opponent):
            return

        # High attacks can whiff over crouchers (very MK)
        if md.height == "high" and opponent.stance == "low" and (not opponent.in_air):
            return

        # Determine block correctness
        blocked = opponent.is_blocking and opponent._block_is_correct(md.height)

        if blocked:
            # Blockstun + small pushback; no HP loss (per your spec: damage taken only when HP decreases)
            now = pygame.time.get_ticks()
            opponent.blockstun_until = max(opponent.blockstun_until, now + md.blockstun_ms)
            opponent.forced_block_until = max(opponent.forced_block_until, now + md.blockstun_ms)

            # Keep opponent in block state (even if player releases during blockstun)
            opponent.is_blocking = True
            (opponent._set_low_state if opponent.stance == "low" else opponent._set_medium_state)("block")

            # Pushback (smaller than on hit)
            push = max(6, md.knockback_px // 2)
            if self.facing_right:
                opponent.rect.x += push
            else:
                opponent.rect.x -= push
            opponent.rect.x = max(0, min(WIDTH - opponent.rect.width, opponent.rect.x))
            return

        # HIT: apply HP damage + hitstun/knockback/knockdown
        pre_health = opponent.health
        opponent.health = max(0, opponent.health - md.damage)

        # Only play impact SFX if HP actually decreased
        if opponent.health < pre_health:
            SOUND_MGR.play_hit()
            SOUND_MGR.play_damage_taken()

            # Hit-confirm window for chaining attacks (combos)
            now_hit = pygame.time.get_ticks()
            if (now_hit - self._last_hit_ms) > 800:
                self._combo_chain = 0
            self._last_hit_ms = now_hit
            self._combo_chain += 1

            # Award score based on how many 10-health thresholds were crossed.
            # Example: 100 -> 85 crosses 90, so +200.
            # Fixed: scoring now triggers on ANY successful damage, not only knockdowns.
            old_tens_lost = (100 - pre_health) // 10
            new_tens_lost = (100 - opponent.health) // 10
            if new_tens_lost > opponent._tens_lost:
                self.score += (new_tens_lost - opponent._tens_lost) * 200
                opponent._tens_lost = new_tens_lost
            else:
                # Keep the counter in sync even if something external modified health.
                opponent._tens_lost = max(opponent._tens_lost, new_tens_lost)

        # Hitstun timer
        now = pygame.time.get_ticks()
        opponent.hitstun_until = max(opponent.hitstun_until, now + md.hitstun_ms)

        # Trigger hit animation immediately (but keep it held until hitstun expires)
        if opponent.in_air:
            opponent.trigger_air_hit()
        else:
            opponent.trigger_hit()

        # Knockback
        if self.facing_right:
            opponent.rect.x += md.knockback_px
        else:
            opponent.rect.x -= md.knockback_px
        opponent.rect.x = max(0, min(WIDTH - opponent.rect.width, opponent.rect.x))

        # Knockdown (e.g., sweep)
        if md.knockdown_ms > 0 and (not opponent.in_air):
            opponent.knockdown_until = max(opponent.knockdown_until, now + md.knockdown_ms)

    def _update_attack_anim_and_damage(self, opponent: "Fighter"):
        anim = self._attack_anim()
        if anim is None:
            self._end_attack()
            return

        # Advance animation
        advanced, looped = anim.update()

        # Deal damage exactly on the specified active frame (no end-of-cycle delay)
        if advanced and (not self._damage_done_this_cycle):
            active_idx = None
            if self._active_attack is not None:
                if self.stance == "low" and self._active_attack == "r":
                    # Low R: 00065..00070 (6 frames). Default hit on LAST frame (00070).
                    active_idx = max(0, len(anim.frames) - 1) if anim.frames else 0
                else:
                    active_idx = ATTACK_ACTIVE_FRAME_INDEX.get(self._active_attack, None)
                    if active_idx is not None and anim.frames:
                        # Clamp in case this character's attack has fewer frames.
                        active_idx = min(active_idx, len(anim.frames) - 1)

            if active_idx is not None and anim.index == active_idx:
                self._damage_done_this_cycle = True
                self._deal_damage_now(opponent)

        # End attack when animation completes (single-shot attacks)
        if anim.done:
            now = pygame.time.get_ticks()
            queued = self._queued_attack
            self._queued_attack = None
            self._end_attack()
            self._damage_done_this_cycle = False

            # Seamless chaining into a queued follow-up (hit-confirmed)
            if (queued is not None) and (self.stance == 'medium') and ((now - self._last_hit_ms) <= self._combo_window_ms):
                # Small cooldown after a couple of connected hits to prevent infinite spam
                if self._combo_chain >= 2:
                    self._combo_cooldown_until = max(self._combo_cooldown_until, now + 220)
                self._begin_attack(queued)
    # --------------------
    # MOVEMENT
    # --------------------
    def update_movement(self, keys: pygame.key.ScancodeWrapper):
        # No new actions while stunned (MK-style turn-taking)
        if self._stunned() or self.is_knocked_down():
            return
        left = keys[self.controls["left"]]
        right = keys[self.controls["right"]]

        # no movement during hit, block, or attack
        if self.is_hit or self.is_blocking or self.is_attacking:
            return

        # only support medium + low for now
        if self.stance not in ("medium", "low"):
            return

        # Scorpion: no left/right movement while crouching (low stance)
        if self.stance == "low":
            self._set_low_state("idle")
            return

        dx = 0
        if left and not right:
            dx = -MOVE_SPEED
        elif right and not left:
            dx = MOVE_SPEED

        setter = self._set_low_state if self.stance == "low" else self._set_medium_state

        if dx == 0:
            setter("idle")
            return

        # Apply movement
        self.rect.x += dx
        self.rect.x = max(0, min(WIDTH - self.rect.width, self.rect.x))

        # Choose forward/back animation relative to facing
        moving_right = dx > 0
        if self.facing_right:
            setter("move_fwd" if moving_right else "move_back")
        else:
            setter("move_fwd" if not moving_right else "move_back")

    # --------------------
    # UPDATE / DRAW
    # --------------------
    def update(self, keys: pygame.key.ScancodeWrapper, opponent: "Fighter"):
        # If the match is over, only play win/lose animation.
        if self.end_state == 'win':
            self.end_win_anim.update()
            return
        if self.end_state == 'lose':
            self.end_lose_anim.update()
            return
        # Jump start + airborne physics first (may set stance to 'high')
        self.update_jump(keys)

        # Knockdown (after an air hit): no input/actions until timer expires.
        # IMPORTANT: do NOT force medium stance here, or the high-hit animation/hold
        # frame will snap back to medium the instant we touch the floor.
        if self.is_knocked_down():
            self.stance = 'high'
            # While stunned, finish the high-hit animation if it hasn't completed yet,
            # then hold the configured last frame in draw().
            if self.air_state == 'hit' and (not self.high_hit.done):
                self.high_hit.update()
            return

        # Safety: if we're on the ground and no longer in knockdown or post-air-attack recovery,
        # we must not remain in 'high' stance. This prevents rare cases where a high-hit/knockdown
        # state leaves the fighter visually "falling" until hit again.
        now = pygame.time.get_ticks()
        if (not self.in_air) and (self.stance == 'high') and (now >= self.knockdown_until) and (now >= self.air_land_recover_until):
            self.stance = 'medium'
            self.air_state = 'move'
            self.air_was_hit = False

        self.update_stance(keys)

        # High stance (jump / post-air-attack recovery): play high animations (move/attack/hit).
        # While in high stance we skip the ground stance state machines.
        if self.stance == 'high':
            now = pygame.time.get_ticks()

            # Post-air-attack recovery on the ground: hold the final prone frame briefly.
            if (not self.in_air) and (now < self.air_land_recover_until):
                # Ensure we are holding the attack anim's last frame
                self.air_state = 'attack'
                # Do not advance animation during the hold (keeps the last frame)
                return

            # If recovery just ended, return control to ground stance.
            if (not self.in_air) and (self.air_land_recover_until != 0) and (now >= self.air_land_recover_until):
                self.air_land_recover_until = 0
                self.air_state = 'move'
                self.air_attack_damage_done = False
                # Fall back to normal grounded stance selection next update
                return


            # Start air attack on R (one per jump), direction locked at takeoff (MK 90s)
            r_held = keys[self.controls["attack_r"]]
            r_pressed = r_held and (not self._prev_r)
            if self.in_air and (not self.air_attack_used) and r_pressed and (self.air_state == 'move'):
                self.air_state = 'attack'
                self.air_attack_used = True
                self.air_attack_damage_done = False
                self._active_attack = "high_r"
                self.high_attack.reset()
                return
            # Air hit: play hit anim while falling (non-looping)
            if self.air_state == 'hit':
                self.high_hit.update()
                return

            # Air attack: play flop anim while airborne; deal damage on active frame
            if self.air_state == 'attack':
                advanced, _ = self.high_attack.update()
                if advanced and (not self.air_attack_damage_done) and self.high_attack.frames:
                    # Hit on the configured active frame (frame-accurate contact)
                    active_idx = ATTACK_ACTIVE_FRAME_INDEX.get('high_r', None)
                    if (active_idx is not None) and (self.high_attack.index == active_idx):
                        self.air_attack_damage_done = True
                        if not (opponent.is_blocking and (not opponent.in_air)):
                            if self._attack_hits(opponent):
                                opponent.health = max(0, opponent.health - ATTACK_DAMAGE)
                                if opponent.in_air:
                                    opponent.trigger_air_hit()
                                else:
                                    opponent.trigger_hit()
                return

            # Default airborne movement anim (use backward jump sprites when moving away from opponent)
            if getattr(self, '_air_move_back', False):
                self.high_move_back.update()
            else:
                self.high_move.update()
            return

        # Only medium + low ground stances have sprite state machines right now.

        # Reset stale combo state if player pauses too long between hits
        if (now - self._last_hit_ms) > 900 and (not self.is_attacking):
            self._combo_chain = 0
            self._queued_attack = None

        # Only medium + low ground stances have sprite state machines right now.
        if self.stance not in ('medium', 'low'):
            return

        # If we're in hit reaction, play it and return (short hit-stun)
        if self.is_hit:
            self._update_hit_anim()
            return

        # Block first; if blocking, ignore attacks
        self.update_block(keys)
        if not self.is_blocking:
            self.update_attacks(keys)

        # Movement (only if not blocking/attacking)
        self.update_movement(keys)

        # Update animation for the active stance/state
        if self.stance == "low":
            if self.low_state == "idle":
                self.low_idle.update()
            elif self.low_state == "move_fwd":
                self.low_move.update()
            elif self.low_state == "move_back":
                self.low_move.update()
            elif self.low_state == "block":
                self._update_block_anim()
            elif self.low_state == "hit":
                self._update_hit_anim()
            elif self.low_state in ("attack_r",):
                self._update_attack_anim_and_damage(opponent)
        else:
            if self.medium_state == "idle":
                self.medium_idle.update()
            elif self.medium_state == "move_fwd":
                self.medium_move_fwd.update()
            elif self.medium_state == "move_back":
                self.medium_move_back.update()
            elif self.medium_state == "block":
                self._update_block_anim()
            elif self.medium_state == "hit":
                self._update_hit_anim()
            elif self.medium_state in ("attack_r", "attack_e", "attack_t", "attack_y"):
                self._update_attack_anim_and_damage(opponent)
    def draw(self, surf: pygame.Surface):
        """Draw Scorpion's current frame (same logic as Fighter).

        Scorpion can optionally use foot anchoring (`self.anchor_feet=True`) to
        align the bottom-most opaque pixel to the ground line, compensating for
        different sprite padding/cropping.
        """

        def _blit(img: pygame.Surface | None) -> None:
            if img is None:
                pygame.draw.rect(surf, self.color, self.rect, 2)
                return

            if self.flip:
                img = pygame.transform.flip(img, True, False)

            if getattr(self, "anchor_feet", False):
                bottom = _opaque_bottom_y(img)
                y_shift = (img.get_height() - 1 - bottom) + int(getattr(self, "feet_y_nudge", 0))
                surf.blit(img, (self.rect.left, self.rect.top + y_shift))
            else:
                surf.blit(img, self.rect.topleft)

        if self.end_state == "win":
            _blit(self.end_win_anim.current())
            return
        if self.end_state == "lose":
            _blit(self.end_lose_anim.current())
            return

        if self.in_air or self.stance == "high":
            img = None
            if (not self.in_air) and self.is_knocked_down():
                if (not self.high_hit.done) and (self.air_state == "hit"):
                    img = self.high_hit.current()
                else:
                    img = self.air_knockdown_hold_frame or self.high_hit.current()
            elif self.air_state == "hit":
                img = self.high_hit.current()
            elif self.air_state == "attack":
                img = self.high_attack.current()
            else:
                img = (self.high_move_back.current() if getattr(self, '_air_move_back', False) else self.high_move.current())

            _blit(img)
            return

        if self.stance not in ("medium", "low"):
            pygame.draw.rect(surf, self.color, self.rect, 2)
            return

        img = None
        if self.stance == "low":
            if self.low_state == "idle":
                img = self.low_idle.current()
            elif self.low_state in ("move_fwd", "move_back"):
                img = self.low_move.current()
            elif self.low_state == "block" and self._low_block_anim is not None:
                img = self._low_block_anim.current()
            elif self.low_state == "hit":
                img = self.low_hit_anim.current()
            elif self.low_state == "attack_r":
                img = self.low_attack_r_anim.current()
        else:
            if self.medium_state == "idle":
                img = self.medium_idle.current()
            elif self.medium_state == "move_fwd":
                img = self.medium_move_fwd.current()
            elif self.medium_state == "move_back":
                img = self.medium_move_back.current()
            elif self.medium_state == "block" and self._block_anim is not None:
                img = self._block_anim.current()
            elif self.medium_state == "hit":
                img = self.hit_anim.current()
            elif self.medium_state == "attack_r":
                img = self.attack_r_anim.current()
            elif self.medium_state == "attack_e":
                img = self.attack_e_anim.current()
            elif self.medium_state == "attack_t":
                img = self.attack_t_anim.current()
            elif self.medium_state == "attack_y":
                img = self.attack_y_anim.current()

        _blit(img)

class _VirtualKeys:
    # Mimics pygame.key.get_pressed() lookup.
    def __init__(self):
        self.state = {}

    def reset(self):
        self.state.clear()

    def hold(self, key, down=True):
        self.state[key] = bool(down)

    def __getitem__(self, key):
        return self.state.get(key, False)


def _get_connected_joysticks() -> list[pygame.joystick.Joystick]:
    """Return initialized joystick objects for all currently connected devices."""
    sticks: list[pygame.joystick.Joystick] = []
    try:
        count = pygame.joystick.get_count()
    except Exception:
        return sticks
    for i in range(count):
        try:
            js = pygame.joystick.Joystick(i)
            if not js.get_init():
                js.init()
            sticks.append(js)
        except Exception:
            continue
    return sticks


class ControllerProvider:
    """Maps a game controller to the fighter's existing keyboard controls.

    Mapping (as requested):
      - D-pad and left stick: movement (left/right/up/down)
      - A/B/X/Y (or Cross/Circle/Square/Triangle): the 4 attacks
      - RB / R1: block

    Notes:
      - We map A/B/X/Y onto the fighter's attack_e/t/r/y controls in a stable order.
      - Sticks use a deadzone to avoid drift.
    """

    def __init__(self, joystick: pygame.joystick.Joystick, controls: dict[str, int], deadzone: float = 0.28):
        self.js = joystick
        self.c = controls
        self.deadzone = float(deadzone)
        self.keys = _VirtualKeys()

        # Common button indices (Xbox layout is typical):
        #   0=A, 1=B, 2=X, 3=Y, 5=RB. (PS controllers usually map similarly via SDL.)
        self.btn_a = 0
        self.btn_b = 1
        self.btn_x = 2
        self.btn_y = 3
        # Shoulder mapping varies a bit across drivers; combat buttons (0-3) are
        # consistent enough, but R1/RB can shift (especially on DualSense).
        # Keep a small whitelist of likely indices.
        self.block_buttons = [5, 10, 11, 9, 7]

    def _axis_dir(self) -> tuple[int, int]:
        """Return (dx, dy) from sticks.

        Uses left stick first (axes 0/1). If it's neutral, falls back to right stick
        (commonly axes 2/3) so "joysticks also double as d-pad".
        """
        def _dir_from_axes(ax: float, ay: float) -> tuple[int, int]:
            dx = 0
            dy = 0
            if ax < -self.deadzone:
                dx = -1
            elif ax > self.deadzone:
                dx = 1
            if ay < -self.deadzone:
                dy = 1
            elif ay > self.deadzone:
                dy = -1
            return dx, dy

        try:
            ax0 = self.js.get_axis(0)
            ay0 = self.js.get_axis(1)
            dx, dy = _dir_from_axes(ax0, ay0)
            if dx != 0 or dy != 0:
                return dx, dy

            # Right stick fallback (best-effort)
            if self.js.get_numaxes() >= 4:
                ax1 = self.js.get_axis(2)
                ay1 = self.js.get_axis(3)
                return _dir_from_axes(ax1, ay1)
        except Exception:
            return 0, 0
        return 0, 0

    def _hat_dir(self) -> tuple[int, int]:
        """Return (x,y) from the controller d-pad, robust across devices.

        Priority:
          1) Hat 0 (standard)
          2) D-pad axes (commonly axes 6/7)
          3) D-pad buttons (common SDL mapping 11-14)

        We normalize to: x=-1 left, +1 right; y=+1 up, -1 down.
        """
        # 1) Hat
        try:
            if self.js.get_numhats() > 0:
                hx, hy = self.js.get_hat(0)
                if hx != 0 or hy != 0:
                    return hx, hy
        except Exception:
            pass

        # 2) Axes (best-effort; many controllers expose d-pad on axes 6/7)
        try:
            if self.js.get_numaxes() >= 8:
                ax = float(self.js.get_axis(6))
                ay = float(self.js.get_axis(7))
                hx = -1 if ax < -0.5 else (1 if ax > 0.5 else 0)
                # On many devices: up=-1, down=+1 on axis 7; normalize to up=+1
                hy = 1 if ay < -0.5 else (-1 if ay > 0.5 else 0)
                if hx != 0 or hy != 0:
                    return hx, hy
        except Exception:
            pass

        # 3) Buttons (common SDL indices)
        try:
            n = self.js.get_numbuttons()
            if n >= 15:
                up = self.js.get_button(11)
                down = self.js.get_button(12)
                left = self.js.get_button(13)
                right = self.js.get_button(14)
                hx = (-1 if left else 0) + (1 if right else 0)
                hy = (1 if up else 0) + (-1 if down else 0)
                if hx != 0 or hy != 0:
                    return hx, hy
        except Exception:
            pass
        return 0, 0

    def get_keys(self) -> _VirtualKeys:
        self.keys.reset()

        # Movement: combine D-pad + left stick
        hat_x, hat_y = self._hat_dir()
        stick_x, stick_y = self._axis_dir()
        mx = hat_x if hat_x != 0 else stick_x
        my = hat_y if hat_y != 0 else stick_y

        if mx < 0:
            self.keys.hold(self.c['left'], True)
        elif mx > 0:
            self.keys.hold(self.c['right'], True)

        # Up is +1 (hat or stick), Down is -1
        if my > 0:
            self.keys.hold(self.c['jump'], True)
        elif my < 0:
            self.keys.hold(self.c['crouch'], True)

        # Block (R1/RB)
        try:
            nb = self.js.get_numbuttons()
            for b in self.block_buttons:
                if 0 <= b < nb and self.js.get_button(b):
                    self.keys.hold(self.c['block'], True)
                    break
        except Exception:
            pass

        # Attacks
        # A -> attack_e, B -> attack_t, X -> attack_r, Y -> attack_y
        try:
            if self.js.get_button(self.btn_a):
                self.keys.hold(self.c['attack_e'], True)
            if self.js.get_button(self.btn_b):
                self.keys.hold(self.c['attack_t'], True)
            if self.js.get_button(self.btn_x):
                self.keys.hold(self.c['attack_r'], True)
            if self.js.get_button(self.btn_y):
                self.keys.hold(self.c['attack_y'], True)
        except Exception:
            pass

        return self.keys


class NPCController:
    def __init__(self, controls: dict, base_difficulty: float = 0.55):
        self.c = controls
        self.base = max(0.0, min(1.0, float(base_difficulty)))
        self.keys = _VirtualKeys()
        self.next_think = 0
        self.next_attack = 0
        self.next_block = 0
        self.move_hold_until = 0
        self.hold_dir = 0  # -1 left, +1 right

        self.last_attack = None
        self.combo_until = 0
        self.last_opp_health = 100
    def set_base(self, base_difficulty: float):
        self.base = max(0.0, min(1.0, float(base_difficulty)))

    def get_keys(self, me: 'Fighter', opp: 'Fighter', now_ms: int, round_no: int, p1_wins: int, p2_wins: int, match_state: str):
        # Keep held movement between think ticks so movement anim/speed stays normal.
        self.keys.reset()

        # Only drive inputs during active fighting. During intros / round-over / match-over
        # we return an all-false key state so the CPU can't move/attack.
        if match_state != 'fighting':
            return self.keys
        if now_ms < self.move_hold_until:
            if self.hold_dir < 0:
                self.keys.hold(self.c['left'], True)
            elif self.hold_dir > 0:
                self.keys.hold(self.c['right'], True)

        # Difficulty adapts: baseline slider + gentle ramp per round + rubberband
        # ---- Difficulty scaling (MK-style) ----
        # base comes from the character select slider (0..1). We apply a curve so the
        # upper end of the slider feels much more punishing.
        base = max(0.0, min(1.0, float(self.base)))
        base = base ** 0.70  # boosts high difficulties more than low ones

        # Adaptive "rubber band" like classic arcade fighters:
        # - later rounds get harder
        # - if the human is ahead, CPU ramps up a bit
        adapt = 0.0
        adapt += (round_no - 1) * 0.08
        adapt += (p1_wins - p2_wins) * 0.10
        adapt += random.uniform(-0.03, 0.03)

        diff = max(0.05, min(0.98, base + adapt))

        # Error rate / hesitation: low diff makes more mistakes, high diff plays tighter.
        error = (1.0 - diff) ** 2  # 0..1

        # Think interval: harder -> reacts faster (but keep a floor so it isn't frame-perfect)
        think_ms = int(260 - 210 * diff)
        think_ms = max(55, think_ms)

        # Track recent damage to trigger "combo pressure"
        if opp.health < self.last_opp_health:
            # If we successfully landed damage, keep pressure briefly.
            self.combo_until = now_ms + int(650 + 550 * diff)
        self.last_opp_health = opp.health

        if now_ms < self.next_think:
            return self.keys
        self.next_think = now_ms + think_ms

        dx = (opp.rect.centerx - me.rect.centerx)
        dist = abs(dx)

                # ---- Movement intent ----
        # Spacing-aware movement: the CPU should *hover* at an effective strike range
        # instead of walking all the way into point-blank distance.
        self.hold_dir = 0

        in_combo_window = now_ms < self.combo_until

        # "Strike band" (roughly where your medium attacks connect in this game).
        # We intentionally keep a band, not a single distance, so it feels like MK footsies.
        optimal_min = 95 + int(20 * (1.0 - diff))    # easier levels give more space / less precision
        optimal_max = 150 + int(10 * (1.0 - diff))

        # During pressure (after we landed damage), creep closer to keep combos going.
        if in_combo_window:
            optimal_min = max(70, optimal_min - 15)
            optimal_max = max(optimal_min + 25, optimal_max - 15)

        # If we're *about* to be able to attack, try to be in-range; if we're on cooldown,
        # don't keep walking forward—hover / bait.
        attack_ready_soon = (now_ms + 120) >= self.next_attack

        far_gap = optimal_max + (95 if diff < 0.45 else 75)

        if dist > far_gap:
            # Close distance (longer commit when far)
            self.hold_dir = 1 if dx > 0 else -1
            self.move_hold_until = now_ms + int(200 + 160 * error)
        elif dist > optimal_max:
            # Step into range (short commit so we don't overshoot)
            self.hold_dir = 1 if dx > 0 else -1
            step = 110 if attack_ready_soon else 85
            self.move_hold_until = now_ms + int(step + 80 * error)
        elif dist < optimal_min:
            # Too close: create space (even on hard, otherwise it face-hugs forever)
            self.hold_dir = -1 if dx > 0 else 1
            self.move_hold_until = now_ms + int(95 + 90 * error)
        else:
            # We're in the strike band: mostly hold position.
            # On higher diffs, occasionally micro-adjust to maintain the band.
            if diff > 0.70 and random.random() < 0.22:
                # If we're waiting on cooldown, backstep sometimes to bait whiffs.
                if not attack_ready_soon and random.random() < (0.35 + 0.25 * diff):
                    self.hold_dir = -1 if dx > 0 else 1
                    self.move_hold_until = now_ms + int(70 + 60 * error)
                # Or tiny step forward if opponent is drifting out and we can swing soon.
                elif attack_ready_soon and dist > (optimal_min + optimal_max) // 2 and random.random() < 0.55:
                    self.hold_dir = 1 if dx > 0 else -1
                    self.move_hold_until = now_ms + int(60 + 50 * error)

        # Apply movement hold immediately
        if self.hold_dir < 0:
            self.keys.hold(self.c['left'], True)
        elif self.hold_dir > 0:
            self.keys.hold(self.c['right'], True)

        # ---- Defense / blocking ----
        # MK-ish: higher difficulty blocks more often, and blocks "tighter" (less random).
        # If opponent is attacking and close, strongly favor block.
        if now_ms >= self.next_block and dist < 190:
            opp_attacking = getattr(opp, 'is_attacking', False)
            opp_hitting = (getattr(opp, 'hitstun_until', 0) > now_ms)  # opponent stunned -> they aren't the threat
            me_stunned = (getattr(me, 'hitstun_until', 0) > now_ms)

            if not me_stunned and opp_attacking and not opp_hitting:
                block_chance = 0.18 + 0.78 * diff
                # Make some mistakes on easier levels
                block_chance *= (1.0 - 0.85 * error)
                if random.random() < block_chance:
                    self.keys.hold(self.c['block'], True)

                    # On high diff, hold block a bit longer so it doesn't "flicker"
                    if diff > 0.75:
                        self.next_block = now_ms + int(220 - 120 * diff)
                    else:
                        self.next_block = now_ms + int(260 - 110 * diff)
            else:
                self.next_block = now_ms + int(300 - 110 * diff)

        # ---- Offense (attacks + pressure) ----
        # If opponent is in hitstun, increase aggression and shorten cooldowns (combo pressure).
        in_combo_window = now_ms < self.combo_until
        opp_in_hitstun = getattr(opp, 'hitstun_until', 0) > now_ms
        opp_in_blockstun = getattr(opp, 'blockstun_until', 0) > now_ms

        # Base willingness to press buttons
        aggression = 0.20 + 0.55 * diff
        if in_combo_window or opp_in_hitstun:
            aggression += 0.20 + 0.15 * diff

        # Don't mash into a blocking opponent forever on high diff (slight hesitation / spacing)
        if opp_in_blockstun and random.random() < (0.20 + 0.30 * diff):
            aggression *= 0.65

        if now_ms >= self.next_attack and dist < 175 and not self.keys[self.c['block']]:
            # mistakes on easy: sometimes fail to capitalize
            if random.random() < aggression * (1.0 - 0.55 * error):
                # Build weighted move list (varies by distance).
                # High diff prefers heavier punishes at the right spacing.
                if dist < 90:
                    weighted = [("attack_r", 4), ("attack_e", 3), ("attack_t", 2), ("attack_y", 1)]
                elif dist < 130:
                    weighted = [("attack_t", 5 if diff > 0.70 else 4), ("attack_r", 2), ("attack_e", 2), ("attack_y", 2)]
                else:
                    weighted = [("attack_y", 6 if diff > 0.70 else 5), ("attack_t", 3), ("attack_r", 1), ("attack_e", 1)]

                candidates = [(name, w) for (name, w) in weighted if name in self.c]
                if candidates:
                    names = [n for (n, _) in candidates]
                    weights = [w for (_, w) in candidates]

                    which = random.choices(names, weights=weights, k=1)[0]

                    # Reduce repeats, but allow intentional repetition at high diff during combos
                    if (not in_combo_window) and self.last_attack == which and len(names) > 1 and random.random() < (0.70 - 0.35 * diff):
                        which = random.choices(names, weights=weights, k=1)[0]

                    self.keys.hold(self.c[which], True)
                    self.last_attack = which

                    # Per-move cooldowns: high diff recovers faster, combos recover much faster
                    base_cd = {"attack_r": 520, "attack_e": 560, "attack_t": 650, "attack_y": 760}.get(which, 620)

                    # Combo pressure shortens recovery a lot
                    combo_bonus = 0
                    if in_combo_window or opp_in_hitstun:
                        combo_bonus = 180 + int(120 * diff)

                    cd = base_cd - int(260 * diff) - combo_bonus + random.randint(-60, 60)
                    cd = max(120, cd)  # floor to prevent "infinite" spam
                    self.next_attack = now_ms + cd

        return self.keys
class IntroPlayer:
    def __init__(
        self,
        folder: str,
        fighter_rect: pygame.Rect,
        flip: bool,
        *,
        normalize: bool = False,
        ref_h: int | None = None,
        fit: float = 1.0,
        target_h_override: int | None = None,
        y_nudge: int = 0,
    ):
        self.rect = fighter_rect
        self.flip = flip
        self.y_nudge = int(y_nudge)

        if normalize and ref_h is not None:
            frames = load_scaled_images_normalized(
                folder,
                (PLAYER_W, PLAYER_H),
                ref_h=int(ref_h),
                fit=float(fit),
                target_h_override=target_h_override,
                bottom_align=True,
            )
        else:
            frames = load_scaled_images(folder, (PLAYER_W, PLAYER_H))

        self.anim = FrameAnim(frames, INTRO_FPS, loop=False)

    @property
    def done(self) -> bool:
        return self.anim.done

    def update(self):
        self.anim.update()

    def draw(self, surf: pygame.Surface):
        img = self.anim.current()
        if img is None:
            return
        if self.flip:
            img = pygame.transform.flip(img, True, False)
        surf.blit(img, (self.rect.x, self.rect.y + self.y_nudge))


def draw_health_bar(x: int, y: int, health: int, color: tuple[int, int, int]) -> None:
    BAR_W = 280
    BAR_H = 22
    BORDER = 3
    MARGIN = 50

    # Clamp health visually (no logic change)
    health = max(0, min(100, health))

    # Auto-correct positioning so bars never go off-screen and stay MK-symmetrical.
    # If caller passes an "old" x (tuned for 200px bars), this will snap it cleanly.
    if x <= MARGIN + 5:
        x = MARGIN
    elif x >= WIDTH - (MARGIN + BAR_W + 5):
        x = WIDTH - MARGIN - BAR_W
    else:
        x = max(0, min(WIDTH - BAR_W, x))

    # Border
    pygame.draw.rect(
        screen,
        WHITE,
        (x - BORDER, y - BORDER, BAR_W + BORDER * 2, BAR_H + BORDER * 2)
    )

    # Fill
    pygame.draw.rect(
        screen,
        color,
        (x, y, int(BAR_W * (health / 100)), BAR_H)
    )


def wins_to_roman(wins: int) -> str:
    """Convert round-win counts to MK-style roman numerals."""
    if wins <= 0:
        return ''
    if wins == 1:
        return 'I'
    if wins == 2:
        return 'II'
    # Shouldn't happen (best-of-3), but keep it safe.
    return 'I' * wins


def _resolve_stages_dir() -> str:
    """Return a usable stages directory (prefers STAGES_DIR, falls back to ./stages)."""
    if os.path.isdir(STAGES_DIR):
        return STAGES_DIR
    return os.path.join(os.path.dirname(__file__), 'stages')


def load_stage_background(stage_name: str, target_size: tuple[int, int]) -> pygame.Surface | None:
    """Load and scale a stage background by name from the stages directory.

    Supports common image extensions. Returns None if not found.
    """
    stages_dir = _resolve_stages_dir()
    candidates = [os.path.join(stages_dir, stage_name)]
    for ext in STAGE_BG_EXTS:
        candidates.append(os.path.join(stages_dir, stage_name + ext))

    for p in candidates:
        if os.path.isfile(p):
            try:
                img = pygame.image.load(p).convert()
                if img.get_size() != target_size:
                    img = pygame.transform.smoothscale(img, target_size)
                return img
            except Exception as e:
                print(f'[WARN] Failed to load stage background {p}: {e}')
                return None
    return None


def draw_stage(surf: pygame.Surface, bg: pygame.Surface | None = None) -> None:
    """Draw the stage background (full photo).

    We intentionally do NOT draw the grey floor bar anymore.
    """
    if bg is not None:
        surf.blit(bg, (0, 0))
    else:
        surf.fill(DARK)


def blit_scaled_fill(dst: pygame.Surface, img: pygame.Surface) -> tuple[float, float]:
    """Stretch an image to fully fill the destination surface.

    Returns scale factors (sx, sy) from source -> destination.
    """
    iw, ih = img.get_size()
    if iw <= 0 or ih <= 0:
        return (1.0, 1.0)
    sx = dst.get_width() / iw
    sy = dst.get_height() / ih
    scaled = pygame.transform.smoothscale(img, (dst.get_width(), dst.get_height()))
    dst.blit(scaled, (0, 0))
    return (sx, sy)

def _assert_engine_contract():
    """Fail fast with a clear message if core class contracts are broken."""
    required = [
        "update",
        "draw",
        "update_jump",
        "update_stance",
        "update_block",
        "update_attacks",
        "update_movement",
        "_attack_hits",
        "_deal_damage_now",
    ]
    missing = [name for name in required if not hasattr(Fighter, name)]
    if missing:
        raise RuntimeError(f"Engine contract broken: Fighter missing methods: {missing}")



# =====================
# CHARACTER REGISTRY
# =====================
# Add future characters here (box index -> id is in CHAR_INDEX_TO_ID above).
CHAR_ID_TO_CLASS = {
    'nate': Fighter,
    'scorpion': Scorpion,
    'connor': Connor,
    'blake': Blake,
}

CHAR_ID_TO_START_DIR = {
    'nate': NATE_START_DIR,
    'scorpion': SCORPION_START_DIR,
    'connor': CONNOR_START_DIR,
    'blake': BLAKE_START_DIR,
}


def main() -> None:
    _assert_engine_contract()
    global HITBOX_EDITOR_MODE
    # --- Fonts (init once for full game flow) ---
    font_small = load_game_font(24)
    font_mid   = load_game_font(48)
    font_big   = load_game_font(72)

    # Stage (background). For now we load a single stage; later this can be swapped
    # based on user selection.
    stage_name = DEFAULT_STAGE_NAME
    stage_cfg = STAGES.get(stage_name, {'bg': stage_name, 'ground_y': DEFAULT_GROUND_Y})

    global CURRENT_GROUND_Y
    CURRENT_GROUND_Y = int(stage_cfg.get('ground_y', DEFAULT_GROUND_Y))

    stage_bg = load_stage_background(stage_cfg.get('bg', stage_name), (WIDTH, HEIGHT))

    npc_difficulty = 0.5

    # -----------------
    while True:
        goto_title = False
        SOUND_MGR.play_menu_music()
        # TITLE MENU
        # -----------------
        font_title = load_game_font(92)
        font_menu = load_game_font(52)
        font_hint = load_game_font(28)
        # Slightly smaller hint font for multi-line bottom prompts (prevents overflowing off screen).
        font_hint_small = load_game_font(22)

        title_bg = try_load_image(TITLESCREEN_BG_PATH, convert_alpha=False)
        # Fallback to the uploaded screenshot if the absolute path isn't available on this machine.
        char_bg = try_load_image(CHARSELECT_BG_PATH, convert_alpha=False) or try_load_image(
            os.path.join(os.path.dirname(__file__), '46tooe.jpg'), convert_alpha=False
        )
        nate_thumb = try_load_image(NATE_SELECT_PATH, convert_alpha=True)
        scorpion_thumb = try_load_image(SCORPION_SELECT_PATH, convert_alpha=True)
        connor_thumb = try_load_image(CONNOR_SELECT_PATH, convert_alpha=True)
        blake_thumb = try_load_image(BLAKE_SELECT_PATH, convert_alpha=True)

        # Menu selection
        menu_items = ['Single', 'Double', 'Quit']
        menu_index = 0
        game_mode = 'single'

        def _js_instance_id(js: pygame.joystick.Joystick) -> int:
            # pygame 2 uses instance ids; fall back safely.
            try:
                return js.get_instance_id()
            except Exception:
                try:
                    return js.get_id()
                except Exception:
                    return -1

        # Helpers for menu/controller polling
        def _poll_dpad(js: pygame.joystick.Joystick):
            # Normalize to x=-1 left/+1 right; y=+1 up/-1 down
            try:
                if js.get_numhats() > 0:
                    hx, hy = js.get_hat(0)
                    if hx or hy:
                        return hx, hy
            except Exception:
                pass
            try:
                # Some pads expose dpad on axes 6/7
                if js.get_numaxes() >= 8:
                    ax = float(js.get_axis(6))
                    ay = float(js.get_axis(7))
                    hx = -1 if ax < -0.5 else (1 if ax > 0.5 else 0)
                    hy = 1 if ay < -0.5 else (-1 if ay > 0.5 else 0)
                    if hx or hy:
                        return hx, hy
            except Exception:
                pass
            try:
                # Common SDL button mapping
                if js.get_numbuttons() >= 15:
                    up = js.get_button(11)
                    down = js.get_button(12)
                    left = js.get_button(13)
                    right = js.get_button(14)
                    hx = (-1 if left else 0) + (1 if right else 0)
                    hy = (1 if up else 0) + (-1 if down else 0)
                    if hx or hy:
                        return hx, hy
            except Exception:
                pass
            # Stick fallback
            try:
                ax0 = float(js.get_axis(0))
                ay0 = float(js.get_axis(1))
                hx = -1 if ax0 < -0.45 else (1 if ax0 > 0.45 else 0)
                hy = 1 if ay0 < -0.45 else (-1 if ay0 > 0.45 else 0)
                if hx or hy:
                    return hx, hy
            except Exception:
                pass
            return 0, 0

        def _button_pressed(js: pygame.joystick.Joystick, idx: int, prev: dict[int, int]):
            # Edge-detect a button press
            try:
                v = 1 if js.get_button(idx) else 0
            except Exception:
                v = 0
            was = prev.get(idx, 0)
            prev[idx] = v
            return v == 1 and was == 0

        menu_next_nav_ms = 0
        menu_btn_prev: dict[int, int] = {}

        while True:
            clock.tick(FPS)
            if title_bg is not None:
                blit_scaled_fill(screen, title_bg)
            else:
                screen.fill(BLACK)

            # Title
            title_surf = font_title.render('MK Ultra', True, TEXT_RED)
            screen.blit(title_surf, (WIDTH // 2 - title_surf.get_width() // 2, 60))

            # Menu items
            start_y = 240
            for i, label in enumerate(menu_items):
                is_sel = (i == menu_index)
                col = (255, 255, 0) if is_sel else WHITE
                surf = font_menu.render(label, True, col)
                screen.blit(surf, (WIDTH // 2 - surf.get_width() // 2, start_y + i * (surf.get_height() + MENU_ITEM_GAP)))

            hint = font_hint.render('ENTER / A to select  |  ESC / B to back', True, WHITE)
                    # hint blit removed per request

            pygame.display.flip()

            selected = False
            # Keyboard events
            for event in pygame.event.get():
                if event.type == SOUND_MGR.MUSIC_END_EVENT:
                    SOUND_MGR.handle_music_end_event()
                    continue
                if event.type == pygame.QUIT:
                    return
                if event.type == pygame.KEYDOWN:
                    if event.key == pygame.K_ESCAPE:
                        return
                    if event.key in (pygame.K_w, pygame.K_UP):
                        menu_index = (menu_index - 1) % len(menu_items)
                    if event.key in (pygame.K_s, pygame.K_DOWN):
                        menu_index = (menu_index + 1) % len(menu_items)
                    if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        sel = menu_items[menu_index].lower()
                        if sel == 'quit':
                            return
                        game_mode = 'double' if sel == 'double' else 'single'
                        selected = True

            # Controller polling (works even when hat events don't fire)
            sticks = _get_connected_joysticks()
            if sticks and not selected:
                js = sticks[0]
                now_ms = pygame.time.get_ticks()
                hx, hy = _poll_dpad(js)
                if now_ms >= menu_next_nav_ms:
                    if hy > 0:
                        menu_index = (menu_index - 1) % len(menu_items)
                        menu_next_nav_ms = now_ms + 160
                    elif hy < 0:
                        menu_index = (menu_index + 1) % len(menu_items)
                        menu_next_nav_ms = now_ms + 160

                if _button_pressed(js, 0, menu_btn_prev):  # A / Cross
                    sel = menu_items[menu_index].lower()
                    if sel == 'quit':
                        return
                    game_mode = 'double' if sel == 'double' else 'single'
                    selected = True

                if _button_pressed(js, 1, menu_btn_prev):  # B / Circle
                    return

            if selected:
                break
        # -----------------
        # CHARACTER SELECT
        # -----------------
        def run_character_select(player_label: str, allow_cpu_difficulty: bool) -> int:
            nonlocal npc_difficulty
            sel_index = 0
            cols = 5
            next_nav_ms = 0
            next_diff_ms = 0
            # Arm controller UI inputs only after the pad is in a neutral state.
            # This prevents "auto-confirm" when a controller is plugged in or
            # a face button is held from the previous screen.
            ui_armed = False
            # Edge-detect controller buttons so we don't auto-confirm if a button
            # is reported as held when a controller is plugged in / focused.
            btn_prev: dict[int, int] = {}
            # Small debounce window on entering the screen to ignore any
            # residual/held inputs.
            ignore_confirm_until = pygame.time.get_ticks() + 300
            while True:
                clock.tick(FPS)
                if char_bg is not None:
                    sx, sy = blit_scaled_fill(screen, char_bg)
                    off_x, off_y = 0, 0
                else:
                    screen.fill(GRAY)
                    off_x, off_y, sx, sy = (0, 0, 1.0, 1.0)

                # Draw Nate portrait in the first box if available
                if nate_thumb is not None and len(CHARSELECT_BOXES_SRC) > 0:
                    bx, by, bw, bh = CHARSELECT_BOXES_SRC[0]
                    rx = int(off_x + bx * sx)
                    ry = int(off_y + by * sy)
                    rw = int(bw * sx)
                    rh = int(bh * sy)
                    pad = max(2, int(6 * min(sx, sy)))
                    thumb = pygame.transform.smoothscale(nate_thumb, (max(1, rw - 2 * pad), max(1, rh - 2 * pad)))
                    screen.blit(thumb, (rx + pad, ry + pad))

                # Draw Scorpion portrait in the second box if available
                if 'scorpion' in CHAR_ID_TO_CLASS and scorpion_thumb is not None and len(CHARSELECT_BOXES_SRC) > 1:
                    bx, by, bw, bh = CHARSELECT_BOXES_SRC[1]
                    rx = int(off_x + bx * sx)
                    ry = int(off_y + by * sy)
                    rw = int(bw * sx)
                    rh = int(bh * sy)
                    pad = max(2, int(6 * min(sx, sy)))
                    thumb = pygame.transform.smoothscale(scorpion_thumb, (max(1, rw - 2 * pad), max(1, rh - 2 * pad)))
                    screen.blit(thumb, (rx + pad, ry + pad))

                # Draw Connor portrait in the third box if available
                if 'connor' in CHAR_ID_TO_CLASS and connor_thumb is not None and len(CHARSELECT_BOXES_SRC) > 2:
                    bx, by, bw, bh = CHARSELECT_BOXES_SRC[2]
                    rx = int(off_x + bx * sx)
                    ry = int(off_y + by * sy)
                    rw = int(bw * sx)
                    rh = int(bh * sy)
                    pad = max(2, int(6 * min(sx, sy)))
                    thumb = pygame.transform.smoothscale(connor_thumb, (max(1, rw - 2 * pad), max(1, rh - 2 * pad)))
                    screen.blit(thumb, (rx + pad, ry + pad))

                # Draw Blake portrait in the fourth box if available
                if 'blake' in CHAR_ID_TO_CLASS and blake_thumb is not None and len(CHARSELECT_BOXES_SRC) > 3:
                    bx, by, bw, bh = CHARSELECT_BOXES_SRC[3]
                    rx = int(off_x + bx * sx)
                    ry = int(off_y + by * sy)
                    rw = int(bw * sx)
                    rh = int(bh * sy)
                    pad = max(2, int(6 * min(sx, sy)))
                    thumb = pygame.transform.smoothscale(blake_thumb, (max(1, rw - 2 * pad), max(1, rh - 2 * pad)))
                    screen.blit(thumb, (rx + pad, ry + pad))

                # Highlight selected box
                if 0 <= sel_index < len(CHARSELECT_BOXES_SRC):
                    bx, by, bw, bh = CHARSELECT_BOXES_SRC[sel_index]
                    rx = int(off_x + bx * sx)
                    ry = int(off_y + by * sy)
                    rw = int(bw * sx)
                    rh = int(bh * sy)
                    pygame.draw.rect(screen, BLACK, (rx - 3, ry - 3, rw + 6, rh + 6), 8)

                # Bottom prompts (TWO LINES so it never runs off-screen)
                if allow_cpu_difficulty:
                    pct = int(round(npc_difficulty * 100))
                    bar_len = 10
                    filled = int(round(npc_difficulty * bar_len))
                    bar = '[' + '=' * filled + '-' * (bar_len - filled) + ']'
                    line1 = f'{player_label} SELECT  |  CPU {bar} {pct}%'
                    line2 = 'ENTER/A confirm  |  ESC/B back  |  L1 decrease CPU  |  R1 increase CPU  |  B/N (kb) adjust'
                else:
                    line1 = f'{player_label} SELECT'
                    line2 = 'ENTER / A confirm  |  ESC / B back'

                s1 = font_hint.render(line1, True, WHITE)
                s2 = font_hint_small.render(line2, True, WHITE)
                strip_h = int(max(s1.get_height() + s2.get_height() + 22, 72))
                strip = pygame.Surface((WIDTH, strip_h), pygame.SRCALPHA)
                strip.fill((0, 0, 0, 190))
                screen.blit(strip, (0, HEIGHT - strip_h))
                y2 = HEIGHT - s2.get_height() - 10
                y1 = y2 - s1.get_height() - 6
                screen.blit(s1, (WIDTH // 2 - s1.get_width() // 2, y1))
                screen.blit(s2, (WIDTH // 2 - s2.get_width() // 2, y2))

                pygame.display.flip()

                # --- Controller polling (navigation works even if d-pad doesn't emit hat events) ---
                sticks = _get_connected_joysticks()
                js_poll = None
                if sticks:
                    if player_label == 'P2' and len(sticks) > 1:
                        js_poll = sticks[1]
                    else:
                        js_poll = sticks[0]
                now_ms = pygame.time.get_ticks()
                # Determine whether the controller is "neutral".
                if js_poll is not None and not ui_armed:
                    try:
                        # No d-pad direction
                        hx0, hy0 = _poll_dpad(js_poll)
                        # Sticks near center
                        ax0 = float(js_poll.get_axis(0)) if js_poll.get_numaxes() > 0 else 0.0
                        ay0 = float(js_poll.get_axis(1)) if js_poll.get_numaxes() > 1 else 0.0
                        neutral_sticks = (abs(ax0) < 0.25 and abs(ay0) < 0.25)
                        # No buttons held
                        neutral_buttons = True
                        for bi in range(js_poll.get_numbuttons()):
                            if js_poll.get_button(bi):
                                neutral_buttons = False
                                break
                        if hx0 == 0 and hy0 == 0 and neutral_sticks and neutral_buttons:
                            ui_armed = True
                            # Initialize previous button states at arm-time so held buttons
                            # don't register as an immediate edge-press.
                            try:
                                btn_prev[0] = 1 if js_poll.get_button(0) else 0
                                btn_prev[1] = 1 if js_poll.get_button(1) else 0
                                # Also extend the confirm debounce a bit after arming.
                                ignore_confirm_until = max(ignore_confirm_until, pygame.time.get_ticks() + 250)
                            except Exception:
                                pass
                    except Exception:
                        ui_armed = True
                if js_poll is not None and now_ms >= next_nav_ms:
                    # d-pad via hat / axes / buttons
                    try:
                        hx, hy = (js_poll.get_hat(0) if js_poll.get_numhats() > 0 else (0, 0))
                    except Exception:
                        hx, hy = (0, 0)
                    if hx == 0 and hy == 0:
                        try:
                            if js_poll.get_numaxes() >= 8:
                                ax = float(js_poll.get_axis(6))
                                ay = float(js_poll.get_axis(7))
                                hx = -1 if ax < -0.5 else (1 if ax > 0.5 else 0)
                                hy = 1 if ay < -0.5 else (-1 if ay > 0.5 else 0)
                        except Exception:
                            pass
                    if hx == 0 and hy == 0:
                        try:
                            if js_poll.get_numbuttons() >= 15:
                                up = js_poll.get_button(11)
                                down = js_poll.get_button(12)
                                left = js_poll.get_button(13)
                                right = js_poll.get_button(14)
                                hx = (-1 if left else 0) + (1 if right else 0)
                                hy = (1 if up else 0) + (-1 if down else 0)
                        except Exception:
                            pass
                    # also allow left stick navigation
                    if hx == 0 and hy == 0:
                        try:
                            ax0 = float(js_poll.get_axis(0))
                            ay0 = float(js_poll.get_axis(1))
                            dead = 0.55
                            if ax0 < -dead:
                                hx = -1
                            elif ax0 > dead:
                                hx = 1
                            if ay0 < -dead:
                                hy = 1
                            elif ay0 > dead:
                                hy = -1
                        except Exception:
                            pass
                    if hx < 0:
                        sel_index = (sel_index - 1) % len(CHARSELECT_BOXES_SRC)
                        next_nav_ms = now_ms + 160
                    elif hx > 0:
                        sel_index = (sel_index + 1) % len(CHARSELECT_BOXES_SRC)
                        next_nav_ms = now_ms + 160
                    elif hy > 0:
                        sel_index = (sel_index - cols) % len(CHARSELECT_BOXES_SRC)
                        next_nav_ms = now_ms + 180
                    elif hy < 0:
                        sel_index = (sel_index + cols) % len(CHARSELECT_BOXES_SRC)
                        next_nav_ms = now_ms + 180

                # confirm/back + CPU diff polling (controller)
                if js_poll is not None and ui_armed:
                    try:
                        # Confirm/back should be edge-triggered and debounced.
                        if now_ms >= ignore_confirm_until:
                            if _button_pressed(js_poll, 1, btn_prev):  # B / Circle
                                return -2
                            if _button_pressed(js_poll, 0, btn_prev):  # A / Cross
                                if sel_index in CHAR_INDEX_TO_ID:
                                    return sel_index
                        else:
                            # Still update prev state during debounce.
                            _button_pressed(js_poll, 0, btn_prev)
                            _button_pressed(js_poll, 1, btn_prev)

                        # CPU difficulty adjustment uses L1/R1.
                        if allow_cpu_difficulty and now_ms >= next_diff_ms:
                            # DualSense mappings can vary across drivers, so use a small whitelist.
                            l1_btns = (4, 9)
                            r1_btns = (5, 10, 11, 7)
                            dec = False
                            inc = False
                            nb = 0
                            try:
                                nb = js_poll.get_numbuttons()
                            except Exception:
                                nb = 0
                            for b in l1_btns:
                                if 0 <= b < nb and js_poll.get_button(b):
                                    dec = True
                                    break
                            for b in r1_btns:
                                if 0 <= b < nb and js_poll.get_button(b):
                                    inc = True
                                    break
                            if dec:
                                npc_difficulty = max(0.0, npc_difficulty - 0.05)
                                next_diff_ms = now_ms + 160
                            elif inc:
                                npc_difficulty = min(1.0, npc_difficulty + 0.05)
                                next_diff_ms = now_ms + 160
                    except Exception:
                        pass

                for event in pygame.event.get():
                    if event.type == SOUND_MGR.MUSIC_END_EVENT:
                        SOUND_MGR.handle_music_end_event()
                        continue
                    if event.type == pygame.QUIT:
                        return -1
                    if event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_ESCAPE:
                            return -2

                        # CPU difficulty adjust (Single only) — Mac friendly.
                        # Use B/N so it doesn't conflict with character navigation (LEFT/RIGHT).
                        if allow_cpu_difficulty and event.key in (pygame.K_b, pygame.K_n):
                            step = 0.05
                            if event.key == pygame.K_b:
                                npc_difficulty = max(0.0, npc_difficulty - step)
                            else:
                                npc_difficulty = min(1.0, npc_difficulty + step)
                            continue

                        if event.key in (pygame.K_a, pygame.K_LEFT):
                            sel_index = (sel_index - 1) % len(CHARSELECT_BOXES_SRC)
                        if event.key in (pygame.K_d, pygame.K_RIGHT):
                            sel_index = (sel_index + 1) % len(CHARSELECT_BOXES_SRC)
                        if event.key in (pygame.K_w, pygame.K_UP):
                            sel_index = (sel_index - cols) % len(CHARSELECT_BOXES_SRC)
                        if event.key in (pygame.K_s, pygame.K_DOWN):
                            sel_index = (sel_index + cols) % len(CHARSELECT_BOXES_SRC)
                        if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                            if sel_index in CHAR_INDEX_TO_ID:
                                return sel_index

                    # Controller support
                    if event.type in (pygame.JOYBUTTONDOWN, pygame.JOYHATMOTION, pygame.JOYAXISMOTION):
                        sticks = _get_connected_joysticks()
                        js = None
                        if sticks:
                            if player_label == 'P2' and len(sticks) > 1:
                                js = sticks[1]
                            else:
                                js = sticks[0]
                        if js is None:
                            continue
                        jid = _js_instance_id(js)
                        e_jid = getattr(event, 'instance_id', getattr(event, 'joy', -999))
                        if e_jid != jid:
                            continue

                        now_ms = pygame.time.get_ticks()

                        # D-pad navigation (hat)
                        if event.type == pygame.JOYHATMOTION and now_ms >= next_nav_ms:
                            hx, hy = event.value
                            if hx < 0:
                                sel_index = (sel_index - 1) % len(CHARSELECT_BOXES_SRC)
                                next_nav_ms = now_ms + 140
                            elif hx > 0:
                                sel_index = (sel_index + 1) % len(CHARSELECT_BOXES_SRC)
                                next_nav_ms = now_ms + 140
                            elif hy > 0:
                                sel_index = (sel_index - cols) % len(CHARSELECT_BOXES_SRC)
                                next_nav_ms = now_ms + 160
                            elif hy < 0:
                                sel_index = (sel_index + cols) % len(CHARSELECT_BOXES_SRC)
                                next_nav_ms = now_ms + 160

                        # Left stick navigation (axes 0/1)
                        if event.type == pygame.JOYAXISMOTION and now_ms >= next_nav_ms:
                            if event.axis in (0, 1):
                                v = float(event.value)
                                dead = 0.45
                                if event.axis == 0:
                                    if v < -dead:
                                        sel_index = (sel_index - 1) % len(CHARSELECT_BOXES_SRC)
                                        next_nav_ms = now_ms + 140
                                    elif v > dead:
                                        sel_index = (sel_index + 1) % len(CHARSELECT_BOXES_SRC)
                                        next_nav_ms = now_ms + 140
                                else:
                                    if v < -dead:
                                        sel_index = (sel_index - cols) % len(CHARSELECT_BOXES_SRC)
                                        next_nav_ms = now_ms + 160
                                    elif v > dead:
                                        sel_index = (sel_index + cols) % len(CHARSELECT_BOXES_SRC)
                                        next_nav_ms = now_ms + 160

                        # Buttons: A confirm, B back
                        if event.type == pygame.JOYBUTTONDOWN:
                            # Debounce/arm so plugging in a controller or
                            # held inputs from the previous screen don't auto-confirm.
                            if not ui_armed or now_ms < ignore_confirm_until:
                                # Still update edge state.
                                _button_pressed(js, 0, btn_prev)
                                _button_pressed(js, 1, btn_prev)
                                continue
                            if event.button == 1:
                                return -2
                            if event.button == 0:
                                if sel_index in CHAR_INDEX_TO_ID:
                                    return sel_index

                            # CPU difficulty (NPC select only): controller L1/R1 adjust.
                            if allow_cpu_difficulty and now_ms >= next_diff_ms:
                                l1_btns = (4, 9)
                                r1_btns = (5, 10, 11, 7)
                                if event.button in l1_btns:
                                    npc_difficulty = max(0.0, npc_difficulty - 0.05)
                                    next_diff_ms = now_ms + 120
                                elif event.button in r1_btns:
                                    npc_difficulty = min(1.0, npc_difficulty + 0.05)
                                    next_diff_ms = now_ms + 120

                # --- Controller polling fallback (handles PS5 d-pad as buttons/axes, even if events don't fire)
                sticks = _get_connected_joysticks()
                js = None
                if sticks:
                    if player_label == 'P2' and len(sticks) > 1:
                        js = sticks[1]
                    else:
                        js = sticks[0]
                if js is not None:
                    now_ms = pygame.time.get_ticks()
                    hx, hy = _poll_dpad(js)
                    if now_ms >= next_nav_ms:
                        if hx < 0:
                            sel_index = (sel_index - 1) % len(CHARSELECT_BOXES_SRC)
                            next_nav_ms = now_ms + 140
                        elif hx > 0:
                            sel_index = (sel_index + 1) % len(CHARSELECT_BOXES_SRC)
                            next_nav_ms = now_ms + 140
                        elif hy > 0:
                            sel_index = (sel_index - cols) % len(CHARSELECT_BOXES_SRC)
                            next_nav_ms = now_ms + 160
                        elif hy < 0:
                            sel_index = (sel_index + cols) % len(CHARSELECT_BOXES_SRC)
                            next_nav_ms = now_ms + 160

                    # Confirm/back (only when armed + past debounce)
                    if ui_armed and now_ms >= ignore_confirm_until:
                        if _button_pressed(js, 0, btn_prev):
                            if sel_index in CHAR_INDEX_TO_ID:
                                return sel_index
                        if _button_pressed(js, 1, btn_prev):
                            return -2

                        # CPU difficulty via L1/R1 during NPC select
                        if allow_cpu_difficulty and now_ms >= next_diff_ms:
                            l1_btns = (4, 9)
                            r1_btns = (5, 10, 11, 7)
                            dec = any(_button_pressed(js, b, btn_prev) for b in l1_btns)
                            inc = any(_button_pressed(js, b, btn_prev) for b in r1_btns)
                            if dec:
                                npc_difficulty = max(0.0, npc_difficulty - 0.05)
                                next_diff_ms = now_ms + 120
                            elif inc:
                                npc_difficulty = min(1.0, npc_difficulty + 0.05)
                                next_diff_ms = now_ms + 120
                    else:
                        # During debounce/unarmed, keep prev state updated so we don't
                        # create a synthetic edge when arming completes.
                        _button_pressed(js, 0, btn_prev)
                        _button_pressed(js, 1, btn_prev)
                        if allow_cpu_difficulty:
                            for b in (4, 5, 6, 7, 8, 9, 10, 11):
                                _button_pressed(js, b, btn_prev)

        
        # -----------------
        # STAGE SELECT
        # -----------------
        def run_stage_select(font_mid):
            """Return selected stage key or None to go back."""
            stage_dir = '/Users/blake/Documents/Mac_Code/MKUltra/stages'
            discovered = discover_stage_images(stage_dir)
            stage_keys = []
            stage_paths = {}

            # Include known stages first
            for k, cfg in STAGES.items():
                stage_keys.append(k)
                bgname = cfg.get('bg', k)
                for ext in ('.png', '.jpg', '.jpeg', '.gif'):
                    p = os.path.join(stage_dir, bgname + ext)
                    if os.path.exists(p):
                        stage_paths[k] = p
                        break

            # Add discovered images (auto-register)
            for k, p in discovered:
                if k not in stage_keys:
                    stage_keys.append(k)
                    stage_paths[k] = p
                if k not in STAGES:
                    STAGES[k] = {'bg': k, 'ground_y': DEFAULT_GROUND_Y}

            if not stage_keys:
                return DEFAULT_STAGE_NAME

            bg_img = try_load_image(STAGESELECT_BG_PATH, convert_alpha=False) or try_load_image(
                os.path.join(os.path.dirname(__file__), 'StageSelect.jpeg'), convert_alpha=False
            )
            bg_img = pygame.transform.scale(bg_img, (WIDTH, HEIGHT))

            # Template is 600x600 with borders near x=116,295,484 and y=115,301,484
            tW, tH = 600, 600
            pad = 6
            xL, xM, xR = 116, 295, 484
            yT, yM, yB = 115, 301, 484
            rects_t = [
                pygame.Rect(xL+pad, yT+pad, (xM-xL)-2*pad, (yM-yT)-2*pad),
                pygame.Rect(xM+pad, yT+pad, (xR-xM)-2*pad, (yM-yT)-2*pad),
                pygame.Rect(xL+pad, yM+pad, (xM-xL)-2*pad, (yB-yM)-2*pad),
                pygame.Rect(xM+pad, yM+pad, (xR-xM)-2*pad, (yB-yM)-2*pad),
            ]
            sx = WIDTH / tW
            sy = HEIGHT / tH
            rects = [pygame.Rect(int(r.x*sx), int(r.y*sy), int(r.w*sx), int(r.h*sy)) for r in rects_t]

            previews = {}
            for k in stage_keys[:4]:
                p = stage_paths.get(k)
                surf = try_load_image(p, convert_alpha=False) if p else None
                if surf is None:
                    # fallback to a flat surface if missing
                    surf = pygame.Surface((600, 600))
                    surf.fill((60, 60, 60))
                previews[k] = surf

            sel = 0
            # Controller navigation (single-screen UI)
            next_nav_ms = 0
            ignore_confirm_until = pygame.time.get_ticks() + 250
            btn_prev = {0: False, 1: False}  # A=0, B=1 edge tracking
            while True:
                clock.tick(FPS)
                for event in pygame.event.get():
                    if event.type == SOUND_MGR.MUSIC_END_EVENT:
                        SOUND_MGR.handle_music_end_event()
                        continue
                    if event.type == pygame.QUIT:
                        pygame.quit()
                        sys.exit(0)
                    if event.type == pygame.KEYDOWN:
                        if event.key in (pygame.K_ESCAPE, pygame.K_BACKSPACE):
                            return None
                        if event.key == pygame.K_RETURN:
                            return stage_keys[sel]
                        if event.key in (pygame.K_LEFT, pygame.K_a):
                            sel = (sel - 1) % min(4, len(stage_keys))
                        if event.key in (pygame.K_RIGHT, pygame.K_d):
                            sel = (sel + 1) % min(4, len(stage_keys))
                        if event.key in (pygame.K_UP, pygame.K_w):
                            sel = (sel - 2) % min(4, len(stage_keys))
                        if event.key in (pygame.K_DOWN, pygame.K_s):
                            sel = (sel + 2) % min(4, len(stage_keys))
                    # Controller support (D-pad / left stick / A confirm / B back)
                    if event.type in (pygame.JOYBUTTONDOWN, pygame.JOYHATMOTION, pygame.JOYAXISMOTION):
                        sticks = _get_connected_joysticks()
                        js = sticks[0] if sticks else None
                        if js is not None:
                            jid = _js_instance_id(js)
                            e_jid = getattr(event, 'instance_id', getattr(event, 'joy', -999))
                            if e_jid == jid:
                                now_ms = pygame.time.get_ticks()

                                # D-pad navigation (hat)
                                if event.type == pygame.JOYHATMOTION and now_ms >= next_nav_ms:
                                    hx, hy = event.value
                                    if hx < 0:
                                        sel = (sel - 1) % min(4, len(stage_keys))
                                        next_nav_ms = now_ms + 140
                                    elif hx > 0:
                                        sel = (sel + 1) % min(4, len(stage_keys))
                                        next_nav_ms = now_ms + 140
                                    elif hy > 0:
                                        sel = (sel - 2) % min(4, len(stage_keys))
                                        next_nav_ms = now_ms + 160
                                    elif hy < 0:
                                        sel = (sel + 2) % min(4, len(stage_keys))
                                        next_nav_ms = now_ms + 160

                                # Left stick navigation (axes 0/1)
                                if event.type == pygame.JOYAXISMOTION and now_ms >= next_nav_ms:
                                    if event.axis in (0, 1):
                                        v = float(event.value)
                                        dead = 0.45
                                        if event.axis == 0:
                                            if v < -dead:
                                                sel = (sel - 1) % min(4, len(stage_keys))
                                                next_nav_ms = now_ms + 140
                                            elif v > dead:
                                                sel = (sel + 1) % min(4, len(stage_keys))
                                                next_nav_ms = now_ms + 140
                                        else:
                                            if v < -dead:
                                                sel = (sel - 2) % min(4, len(stage_keys))
                                                next_nav_ms = now_ms + 160
                                            elif v > dead:
                                                sel = (sel + 2) % min(4, len(stage_keys))
                                                next_nav_ms = now_ms + 160

                                # Buttons: B back, A confirm (edge + debounce)
                                if event.type == pygame.JOYBUTTONDOWN:
                                    # update edge state
                                    a_edge = _button_pressed(js, 0, btn_prev)
                                    b_edge = _button_pressed(js, 1, btn_prev)

                                    if now_ms < ignore_confirm_until:
                                        continue

                                    if event.button == 1 and b_edge:
                                        return None
                                    if event.button == 0 and a_edge:
                                        return stage_keys[sel]

                    # Controller polling fallback (some mappings don't emit hat events reliably)
                    sticks = _get_connected_joysticks()
                    js = sticks[0] if sticks else None
                    if js is not None:
                        now_ms = pygame.time.get_ticks()
                        hx, hy = _poll_dpad(js)
                        if now_ms >= next_nav_ms:
                            if hx < 0:
                                sel = (sel - 1) % min(4, len(stage_keys))
                                next_nav_ms = now_ms + 140
                            elif hx > 0:
                                sel = (sel + 1) % min(4, len(stage_keys))
                                next_nav_ms = now_ms + 140
                            elif hy > 0:
                                sel = (sel - 2) % min(4, len(stage_keys))
                                next_nav_ms = now_ms + 160
                            elif hy < 0:
                                sel = (sel + 2) % min(4, len(stage_keys))
                                next_nav_ms = now_ms + 160
                    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        mx, my = event.pos
                        for i, r in enumerate(rects):
                            if i < len(stage_keys) and r.collidepoint(mx, my):
                                sel = i
                                return stage_keys[sel]

                screen.blit(bg_img, (0, 0))
                for i, r in enumerate(rects):
                    if i >= len(stage_keys):
                        continue
                    k = stage_keys[i]
                    pv = previews.get(k)
                    if pv is not None:
                        screen.blit(pygame.transform.scale(pv, (r.w, r.h)), r.topleft)
                    if i == sel:
                        pygame.draw.rect(screen, (255, 215, 0), r, 4)

                label = stage_keys[sel]
                txt = font_mid.render(label, True, (255, 255, 255))
                screen.blit(txt, (WIDTH//2 - txt.get_width()//2, int(HEIGHT*0.85)))
                pygame.display.flip()

# P1 always chooses first (same flow for both Single + Double).
        p1_sel = run_character_select('P1', allow_cpu_difficulty=False)
        if p1_sel == -1:
            return
        if p1_sel == -2:
            continue

        p2_sel = 0
        if game_mode == 'double':
            # Two-player mode: P2 picks after P1.
            p2_sel = run_character_select('P2', allow_cpu_difficulty=False)
            if p2_sel == -1:
                return
            if p2_sel == -2:
                continue
        else:
            # Single-player mode: after P1 picks, P1 also selects the NPC's fighter.
            p2_sel = run_character_select('NPC', allow_cpu_difficulty=True)
            if p2_sel == -1:
                return
            if p2_sel == -2:
                continue

        # Stage selection (after fighters are chosen)
        # Ensure fonts exist before stage select
        stage_choice = run_stage_select(font_mid)
        if stage_choice is None:
            continue
        stage_name = stage_choice
        # Switch to fight music as soon as we leave menus and load the stage
        # (so the intro sequence doesn't keep using MainMenu.mp3)
        SOUND_MGR.play_fight_music()
        stage_cfg = STAGES.get(stage_name, {'bg': stage_name, 'ground_y': DEFAULT_GROUND_Y})
        CURRENT_GROUND_Y = int(stage_cfg.get('ground_y', DEFAULT_GROUND_Y))
        stage_bg = load_stage_background(stage_cfg.get('bg', stage_name), (WIDTH, HEIGHT))

        # Resolve selected characters (box index -> character id -> class)
        p1_char_id = CHAR_INDEX_TO_ID.get(p1_sel, 'nate')
        p2_char_id = CHAR_INDEX_TO_ID.get(p2_sel, 'nate')
        p1_cls = CHAR_ID_TO_CLASS.get(p1_char_id, Fighter)
        p2_cls = CHAR_ID_TO_CLASS.get(p2_char_id, Fighter)


        p1 = p1_cls(

            x=40,
            color=BLUE,
            controls={
                "left": pygame.K_a,
                "right": pygame.K_d,
                "jump": pygame.K_w,
                "crouch": pygame.K_s,
                "block": pygame.K_f,
                "attack_r": pygame.K_r,
                "attack_e": pygame.K_e,
                "attack_t": pygame.K_t,
                "attack_y": pygame.K_y,
            },
            facing_right=True,
        )

        # Convenience mapping for p2 while testing
        p2 = p2_cls(
            x=WIDTH - 40 - PLAYER_W,
            color=RED,
            controls={
                "left": pygame.K_LEFT,
                "right": pygame.K_RIGHT,
                "jump": pygame.K_UP,
                "crouch": pygame.K_DOWN,
                "block": pygame.K_RCTRL,
                "attack_r": pygame.K_KP1,
                "attack_e": pygame.K_KP2,
                "attack_t": pygame.K_KP3,
                "attack_y": pygame.K_KP4,
            },
            facing_right=False,
        )

        npc_controller = None
        if game_mode == 'single':
            npc_controller = NPCController(p2.controls, base_difficulty=npc_difficulty)

        # Controller assignments (auto):
        # - If at least 1 controller is connected, it drives P1.
        # - If 2+ controllers are connected AND game_mode=='double', controller #2 drives P2.
        sticks = _get_connected_joysticks()
        p1_controller = ControllerProvider(sticks[0], p1.controls) if len(sticks) >= 1 else None
        p2_controller = ControllerProvider(sticks[1], p2.controls) if (game_mode == 'double' and len(sticks) >= 2) else None

        # Intro animations (character-specific normalization for tightly-cropped sprite packs)
        if p1_char_id == 'scorpion':
            _ref_h = _first_frame_ref_height(SCORPION_MEDIUM_IDLE_DIR)
            _target_h = int(PLAYER_H * SCORPION_FIT)
            intro1 = IntroPlayer(
                CHAR_ID_TO_START_DIR.get(p1_char_id, NATE_START_DIR),
                p1.rect,
                flip=False,
                normalize=True,
                ref_h=_ref_h,
                fit=SCORPION_FIT,
                target_h_override=_target_h,
                y_nudge=-30,
            )
        elif p1_char_id == 'connor':
            _ref_h = _first_frame_ref_height(CONNOR_MEDIUM_IDLE_DIR)
            _target_h = int(PLAYER_H * CONNOR_FIT)
            intro1 = IntroPlayer(
                CHAR_ID_TO_START_DIR.get(p1_char_id, NATE_START_DIR),
                p1.rect,
                flip=False,
                normalize=True,
                ref_h=_ref_h,
                fit=CONNOR_FIT,
                target_h_override=_target_h,
            )
        elif p1_char_id == 'blake':
            _ref_h = _first_frame_ref_height(BLAKE_MEDIUM_IDLE_DIR)
            _target_h = int(PLAYER_H * BLAKE_FIT)
            intro1 = IntroPlayer(
                CHAR_ID_TO_START_DIR.get(p1_char_id, NATE_START_DIR),
                p1.rect,
                flip=False,
                normalize=True,
                ref_h=_ref_h,
                fit=BLAKE_FIT,
                target_h_override=_target_h,
            )
        else:
            intro1 = IntroPlayer(CHAR_ID_TO_START_DIR.get(p1_char_id, NATE_START_DIR), p1.rect, flip=False)

        if p2_char_id == 'scorpion':
            _ref_h2 = _first_frame_ref_height(SCORPION_MEDIUM_IDLE_DIR)
            _target_h2 = int(PLAYER_H * SCORPION_FIT)
            intro2 = IntroPlayer(
                CHAR_ID_TO_START_DIR.get(p2_char_id, NATE_START_DIR),
                p2.rect,
                flip=True,
                normalize=True,
                ref_h=_ref_h2,
                fit=SCORPION_FIT,
                target_h_override=_target_h2,
                y_nudge=-30,
            )
        elif p2_char_id == 'connor':
            _ref_h2 = _first_frame_ref_height(CONNOR_MEDIUM_IDLE_DIR)
            _target_h2 = int(PLAYER_H * CONNOR_FIT)
            intro2 = IntroPlayer(
                CHAR_ID_TO_START_DIR.get(p2_char_id, NATE_START_DIR),
                p2.rect,
                flip=True,
                normalize=True,
                ref_h=_ref_h2,
                fit=CONNOR_FIT,
                target_h_override=_target_h2,
            )
        elif p2_char_id == 'blake':
            _ref_h2 = _first_frame_ref_height(BLAKE_MEDIUM_IDLE_DIR)
            _target_h2 = int(PLAYER_H * BLAKE_FIT)
            intro2 = IntroPlayer(
                CHAR_ID_TO_START_DIR.get(p2_char_id, NATE_START_DIR),
                p2.rect,
                flip=True,
                normalize=True,
                ref_h=_ref_h2,
                fit=BLAKE_FIT,
                target_h_override=_target_h2,
            )
        else:
            intro2 = IntroPlayer(CHAR_ID_TO_START_DIR.get(p2_char_id, NATE_START_DIR), p2.rect, flip=True)
        intro_active = True

        # UI fonts

        # Hitbox editor (F2)
        editor = HitboxEditor(json_path='hitboxes_nate.json')
        editor_pause_started = 0

        # =====================
        # ROUNDS / TIMER (MK-style)
        # =====================
        ROUND_SECONDS = 90
        ROUND_OVER_PAUSE_MS = 2500  # brief pause between rounds

        # Match state
        match_state = 'intro'  # intro / fighting / paused / round_over / match_over
        current_round = 1
        p1_round_wins = 0
        p2_round_wins = 0

        round_start_ticks = pygame.time.get_ticks()
        round_over_started = 0
        round_winner: Fighter | None = None
        round_loser: Fighter | None = None
        round_result_reason = ''  # 'ko' / 'time' / 'draw'

        # =====================
        # PAUSE MENU
        # =====================
        pause_menu_items = ['CONTINUE', 'OPTIONS', 'QUIT']
        pause_menu_index = 0
        pause_view = 'main'  # 'main' or 'options'
        pause_overlay = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA)
        pause_overlay.fill((60, 60, 60, 160))  # transparent grey
        pause_started_ticks = 0
        pause_nav_next_ms = 0
        pause_ignore_confirm_until = 0

        def _enter_pause(now_ms: int):
            nonlocal match_state, pause_menu_index, pause_started_ticks, pause_nav_next_ms, pause_ignore_confirm_until, pause_view
            if match_state != 'fighting':
                return
            match_state = 'paused'
            pause_menu_index = 0
            pause_view = 'main'
            pause_started_ticks = now_ms
            pause_nav_next_ms = now_ms + 180
            pause_ignore_confirm_until = now_ms + 250

        def _resume_from_pause(now_ms: int):
            nonlocal match_state, round_start_ticks, pause_started_ticks
            if match_state != 'paused':
                return
            # Freeze the round timer while paused by shifting the start tick forward
            if pause_started_ticks:
                round_start_ticks += (now_ms - pause_started_ticks)
            pause_started_ticks = 0
            match_state = 'fighting'

        def reset_positions_and_state():
            # Keep positions consistent with the intro spacing (as far apart as possible).
            p1.rect.x = 40
            p2.rect.x = WIDTH - 40 - PLAYER_W
            p1.rect.bottom = get_ground_y()
            p2.rect.bottom = get_ground_y()
            p1.set_end_state(None)
            p2.set_end_state(None)

        def start_round(round_no: int):
            nonlocal round_start_ticks, match_state, current_round, round_winner, round_loser, round_result_reason
            current_round = round_no
            p1.health = 100
            p2.health = 100
            p1._tens_lost = 0
            p2._tens_lost = 0
            reset_positions_and_state()
            round_winner = None
            round_loser = None
            round_result_reason = ''
            round_start_ticks = pygame.time.get_ticks()
            match_state = 'fighting'
            SOUND_MGR.play_fight_music()
            SOUND_MGR.play_round_start()

        def end_round(winner: Fighter | None, loser: Fighter | None, reason: str):
            nonlocal match_state, round_over_started, round_winner, round_loser, round_result_reason, p1_round_wins, p2_round_wins
            round_over_started = pygame.time.get_ticks()
            round_winner = winner
            round_loser = loser
            round_result_reason = reason

            if winner is not None and loser is not None:
                winner.set_end_state('win')
                loser.set_end_state('lose')
                if winner is p1:
                    p1_round_wins += 1
                elif winner is p2:
                    p2_round_wins += 1
            else:
                # draw: no score changes
                p1.set_end_state(None)
                p2.set_end_state(None)

            match_state = 'round_over'

        def finalize_match_if_needed():
            nonlocal match_state
            # Best of 3: first to 2 rounds wins
            if p1_round_wins >= 2 or p2_round_wins >= 2:
                if match_state != 'match_over':
                    match_state = 'match_over'
                    SOUND_MGR.play_match_end()
                return True
            return False

        running = True
        while running:
            clock.tick(FPS)
            draw_stage(screen, stage_bg)

            for event in pygame.event.get():
                if event.type == SOUND_MGR.MUSIC_END_EVENT:
                    SOUND_MGR.handle_music_end_event()
                    continue
                if event.type == pygame.QUIT:
                    running = False

                # Hitbox editor consumes input while enabled (but does not stop rendering).
                if HITBOX_EDITOR_MODE and match_state == 'fighting':
                    # Allow F2 handling in KEYDOWN block below to still run; for other events, let editor eat them.
                    if event.type in (pygame.MOUSEBUTTONDOWN, pygame.MOUSEBUTTONUP, pygame.MOUSEMOTION, pygame.KEYDOWN):
                        consumed = editor.handle_event(event, p1, p2)
                        # If editor handled it (or it's mouse), don't let gameplay/pause logic also react.
                        if consumed and not (event.type == pygame.KEYDOWN and event.key == pygame.K_F2):
                            continue


                # -----------------
                # KEYBOARD INPUT
                # -----------------
                if event.type == pygame.KEYDOWN:
                    now_ms = pygame.time.get_ticks()

                    # Hitbox editor toggle (F2)
                    if event.key == pygame.K_F2 and match_state == 'fighting':
                        HITBOX_EDITOR_MODE = not HITBOX_EDITOR_MODE
                        editor.enabled = HITBOX_EDITOR_MODE
                        # freeze round timer while in editor
                        if HITBOX_EDITOR_MODE:
                            editor_pause_started = now_ms
                        else:
                            if editor_pause_started:
                                round_start_ticks += (now_ms - editor_pause_started)
                            editor_pause_started = 0
                        # Prevent time-jumps when leaving editor
                        try:
                            for a in [p1.medium_idle,p1.medium_move_fwd,p1.medium_move_back,p1.medium_block1,p1.medium_block2,p1.attack_r_anim,p1.attack_e_anim,p1.attack_t_anim,p1.attack_y_anim,p1.hit_anim,
                                      p1.low_idle,p1.low_move,p1.low_block,p1.low_attack_r_anim,p1.low_hit_anim,
                                      p1.high_move,p1.high_attack,p1.high_hit,p1.end_win_anim,p1.end_lose_anim,
                                      p2.medium_idle,p2.medium_move_fwd,p2.medium_move_back,p2.medium_block1,p2.medium_block2,p2.attack_r_anim,p2.attack_e_anim,p2.attack_t_anim,p2.attack_y_anim,p2.hit_anim,
                                      p2.low_idle,p2.low_move,p2.low_block,p2.low_attack_r_anim,p2.low_hit_anim,
                                      p2.high_move,p2.high_attack,p2.high_hit,p2.end_win_anim,p2.end_lose_anim]:
                                a.last_tick = pygame.time.get_ticks()
                        except Exception:
                            pass
                        continue

                    if event.key == pygame.K_ESCAPE:
                        if match_state == 'fighting' and not HITBOX_EDITOR_MODE:
                            _enter_pause(now_ms)
                        elif match_state == 'paused':
                            if pause_view == 'options':
                                pause_view = 'main'
                            else:
                                _resume_from_pause(now_ms)
                        elif match_state in ('round_over', 'match_over'):
                            goto_title = True
                            running = False
                        else:
                            running = False

                    # Toggle pause (keyboard)
                    if event.key == pygame.K_p:
                        if match_state == 'fighting' and not HITBOX_EDITOR_MODE:
                            _enter_pause(now_ms)
                        elif match_state == 'paused':
                            _resume_from_pause(now_ms)

                    # Skip intro / start fight
                    if event.key == pygame.K_SPACE and match_state == 'intro':
                        intro_active = False
                        match_state = 'fighting'
                        start_round(1)

                    # Round-over: advance (skip) like classic MK
                    if event.key == pygame.K_RETURN and match_state == 'round_over':
                        round_over_started = 0

                    # Match over: restart match
                    if event.key == pygame.K_RETURN and match_state == 'match_over':
                        p1_round_wins = 0
                        p2_round_wins = 0
                        intro_active = False
                        match_state = 'fighting'
                        start_round(1)

                    # Pause menu: confirm selection (keyboard)
                    if event.key == pygame.K_RETURN and match_state == 'paused' and now_ms >= pause_ignore_confirm_until:
                        if pause_view == 'options':
                            pause_view = 'main'
                        else:
                            if pause_menu_index == 0:
                                _resume_from_pause(now_ms)
                            elif pause_menu_index == 1:
                                pause_view = 'options'
                            elif pause_menu_index == 2:
                                goto_title = True
                                running = False

                # Controller buttons
                if event.type == pygame.JOYBUTTONDOWN:
                    now_ms = pygame.time.get_ticks()

                    # Pause toggle during a fight (controller)
                    # IMPORTANT: On some DualSense (PS5) mappings on macOS, L1/R1 can report as 9/10.
                    # We intentionally avoid 9/10 so bumpers never pause. Buttons 6/8 are common
                    # candidates for "Start/Options" across many SDL mappings.
                    if event.button in (6, 8):
                        if match_state == 'fighting' and not HITBOX_EDITOR_MODE:
                            _enter_pause(now_ms)
                            continue
                        if match_state == 'paused':
                            _resume_from_pause(now_ms)
                            continue

                    # Any button skips intro
                    if match_state == 'intro':
                        intro_active = False
                        match_state = 'fighting'
                        start_round(1)

                    # After a round/match is over:
                    #  - A/Cross confirms (like Enter)
                    #  - B/Circle returns to title (like Esc)
                    if match_state == 'round_over':
                        if event.button == 0:  # A/Cross
                            round_over_started = 0
                        elif event.button == 1:  # B/Circle
                            goto_title = True
                            running = False

                    if match_state == 'match_over':
                        if event.button == 0:  # A/Cross
                            p1_round_wins = 0
                            p2_round_wins = 0
                            intro_active = False
                            match_state = 'fighting'
                            start_round(1)
                        elif event.button == 1:  # B/Circle
                            goto_title = True
                            running = False

                    # Pause menu selection
                    if match_state == 'paused' and now_ms >= pause_ignore_confirm_until:
                        if event.button == 0:  # A/Cross confirm
                            if pause_view == 'options':
                                pause_view = 'main'
                            else:
                                if pause_menu_index == 0:
                                    _resume_from_pause(now_ms)
                                elif pause_menu_index == 1:
                                    pause_view = 'options'
                                elif pause_menu_index == 2:
                                    goto_title = True
                                    running = False
                        elif event.button == 1:  # B/Circle back
                            if pause_view == 'options':
                                pause_view = 'main'
                            else:
                                _resume_from_pause(now_ms)

            kb_keys = pygame.key.get_pressed()

            # Per-player input (keyboard or controller). Fighters still read their existing
            # keyboard control keycodes; controller providers simply "press" those keys.
            p1_keys = p1_controller.get_keys() if p1_controller is not None else kb_keys
            p2_keys = p2_controller.get_keys() if p2_controller is not None else kb_keys

            # A keys wrapper that reports no input (used to freeze fighters between rounds)
            class _NoKeys:
                def __getitem__(self, _key):
                    return False

            no_keys = _NoKeys()

            # -----------------
            # PAUSE MENU NAV (polling)
            # -----------------
            if match_state == 'paused':
                now_ms = pygame.time.get_ticks()
                if pause_view == 'main' and now_ms >= pause_nav_next_ms:
                    move_up = kb_keys[pygame.K_UP] or kb_keys[pygame.K_w] or p1_keys[p1.controls['jump']]
                    move_down = kb_keys[pygame.K_DOWN] or kb_keys[pygame.K_s] or p1_keys[p1.controls['crouch']]
                    if move_up:
                        pause_menu_index = (pause_menu_index - 1) % len(pause_menu_items)
                        pause_nav_next_ms = now_ms + 180
                    elif move_down:
                        pause_menu_index = (pause_menu_index + 1) % len(pause_menu_items)
                        pause_nav_next_ms = now_ms + 180

            # Auto-facing
            p1.update_facing(p2)
            p2.update_facing(p1)

            # =====================
            # INTRO
            # =====================
            if match_state == 'intro':
                intro1.update()
                intro2.update()
                intro1.draw(screen)
                intro2.draw(screen)

                # HUD should be visible during the intro (score, timer, round, health, roman tally)
                remaining = ROUND_SECONDS                # Score (yellow, numbers only) centered above each health bar
                BAR_W = 280
                MARGIN = 50
                score_yellow = (255, 255, 0)

                # Left score (centered over left health bar)
                p1_score_surf = font_small.render(str(p1.score).zfill(4), True, score_yellow)
                p1_score_x = MARGIN + (BAR_W // 2) - (p1_score_surf.get_width() // 2)
                screen.blit(p1_score_surf, (p1_score_x, HUD_SCORE_Y))

                # Right score (centered over right health bar)
                p2_score_surf = font_small.render(str(p2.score).zfill(4), True, score_yellow)
                p2_health_x = WIDTH - MARGIN - BAR_W
                p2_score_x = p2_health_x + (BAR_W // 2) - (p2_score_surf.get_width() // 2)
                screen.blit(p2_score_surf, (p2_score_x, HUD_SCORE_Y))
                draw_health_bar(50, HUD_HEALTH_Y, p1.health, BLUE)
                draw_health_bar(WIDTH - 250, HUD_HEALTH_Y, p2.health, RED)

                # Timer
                timer_txt = font_mid.render(str(remaining).zfill(2), True, WHITE)
                screen.blit(timer_txt, (WIDTH // 2 - timer_txt.get_width() // 2, HUD_HEALTH_Y - 10))

                # Round label
                round_txt = font_small.render(f'ROUND {current_round}', True, WHITE)
                screen.blit(round_txt, (WIDTH // 2 - round_txt.get_width() // 2, HUD_ROUND_Y))

                # Roman round win tallies
                p1_roman = font_small.render(wins_to_roman(p1_round_wins), True, WHITE)
                p2_roman = font_small.render(wins_to_roman(p2_round_wins), True, WHITE)
                screen.blit(p1_roman, (50 + 100 - p1_roman.get_width() // 2, HUD_ROMAN_Y))
                screen.blit(p2_roman, (WIDTH - 250 + 100 - p2_roman.get_width() // 2, HUD_ROMAN_Y))

                pygame.display.flip()

                # If intros finish, allow fight to start (Space still skips)
                if intro1.done and intro2.done:
                    intro_active = False
                    match_state = 'fighting'
                    start_round(1)

            else:
                # =====================
                # TIMER
                # =====================
                now_ms = pygame.time.get_ticks()
                # Freeze the round timer while paused by pegging elapsed time at pause start.
                if match_state == 'paused' and pause_started_ticks:
                    elapsed_ms = pause_started_ticks - round_start_ticks
                else:
                    elapsed_ms = now_ms - round_start_ticks
                remaining = max(0, ROUND_SECONDS - int(elapsed_ms / 1000))

                # =====================
                # UPDATE FIGHTERS
                # =====================
                if match_state == 'fighting' and not HITBOX_EDITOR_MODE:
                    p1.update(p1_keys, p2)
                    if game_mode == 'single' and npc_controller is not None:
                        ai_keys = npc_controller.get_keys(p2, p1, pygame.time.get_ticks(), current_round, p1_round_wins, p2_round_wins, match_state)
                        p2.update(ai_keys, p1)
                    else:
                        p2.update(p2_keys, p1)

                    # Resolve pushbox overlap (spacing)
                    hb_resolve_pushboxes(p1, p2)

                    # KO ends round
                    if p1.health <= 0 or p2.health <= 0:
                        if p1.health <= 0 and p2.health <= 0:
                            # Double KO: pick winner by remaining health (equal -> draw)
                            end_round(None, None, 'draw')
                        elif p2.health <= 0:
                            end_round(p1, p2, 'ko')
                        else:
                            end_round(p2, p1, 'ko')

                    # Time over ends round
                    elif remaining <= 0:
                        if p1.health > p2.health:
                            end_round(p1, p2, 'time')
                        elif p2.health > p1.health:
                            end_round(p2, p1, 'time')
                        else:
                            # Tie on time: treat as draw (replay same round)
                            end_round(None, None, 'draw')

                elif match_state == 'paused':
                    # Freeze fighters while paused (no input).
                    p1.update(no_keys, p2)
                    p2.update(no_keys, p1)
                elif match_state in ('round_over', 'match_over'):
                    # During round-over or match-over, fighters only advance end anims
                    p1.update(no_keys, p2)
                    p2.update(no_keys, p1)

                # =====================
                # DRAW FIGHTERS + HUD
                # =====================
                p1.draw(screen)
                p2.draw(screen)

                if HITBOX_EDITOR_MODE and match_state == 'fighting':
                    editor.draw_overlay(screen, p1, p2, font_small)                # Score (yellow, numbers only) centered above each health bar
                BAR_W = 280
                MARGIN = 50
                score_yellow = (255, 255, 0)

                # Left score (centered over left health bar)
                p1_score_surf = font_small.render(str(p1.score).zfill(4), True, score_yellow)
                p1_score_x = MARGIN + (BAR_W // 2) - (p1_score_surf.get_width() // 2)
                screen.blit(p1_score_surf, (p1_score_x, HUD_SCORE_Y))

                # Right score (centered over right health bar)
                p2_score_surf = font_small.render(str(p2.score).zfill(4), True, score_yellow)
                p2_health_x = WIDTH - MARGIN - BAR_W
                p2_score_x = p2_health_x + (BAR_W // 2) - (p2_score_surf.get_width() // 2)
                screen.blit(p2_score_surf, (p2_score_x, HUD_SCORE_Y))
                draw_health_bar(50, HUD_HEALTH_Y, p1.health, BLUE)
                draw_health_bar(WIDTH - 250, HUD_HEALTH_Y, p2.health, RED)

                # Timer (top-center)
                timer_surf = font_mid.render(str(remaining).rjust(2, '0'), True, TEXT_RED)
                screen.blit(timer_surf, (WIDTH // 2 - timer_surf.get_width() // 2, HUD_TIMER_Y))

                # Round label (centered under the timer)
                round_text = f'ROUND {current_round}'
                round_surf = font_small.render(round_text, True, TEXT_RED)
                screen.blit(round_surf, (WIDTH // 2 - round_surf.get_width() // 2, HUD_ROUND_Y))

                # MK-style round win indicators (roman numerals) under each health bar
                # Bars are 200px wide starting at x=50 and x=WIDTH-250.
                left_roman = wins_to_roman(p1_round_wins)
                right_roman = wins_to_roman(p2_round_wins)

                if left_roman:
                    l_surf = font_small.render(left_roman, True, TEXT_RED)
                    screen.blit(l_surf, (50 + 100 - l_surf.get_width() // 2, HUD_ROMAN_Y))
                if right_roman:
                    r_surf = font_small.render(right_roman, True, TEXT_RED)
                    screen.blit(r_surf, (WIDTH - 250 + 100 - r_surf.get_width() // 2, HUD_ROMAN_Y))

                # =====================
                # PAUSE OVERLAY
                # =====================
                if match_state == 'paused':
                    screen.blit(pause_overlay, (0, 0))
                    if pause_view == 'options':
                        title_surf = font_mid.render('OPTIONS', True, WHITE)
                        screen.blit(title_surf, (WIDTH//2 - title_surf.get_width()//2, 140))
                        msg = font_small.render('Coming soon...', True, WHITE)
                        screen.blit(msg, (WIDTH//2 - msg.get_width()//2, 230))
                    # hint removed per request
                    # hint blit removed per request
                    else:
                        title_surf = font_mid.render('PAUSED', True, WHITE)
                        screen.blit(title_surf, (WIDTH//2 - title_surf.get_width()//2, 140))

                        base_y = 220
                        for i, item in enumerate(pause_menu_items):
                            color = (255, 255, 0) if i == pause_menu_index else WHITE
                            s = font_small.render(item, True, color)
                            screen.blit(s, (WIDTH//2 - s.get_width()//2, base_y + i * 40))

                    # hint removed per request
                    # hint blit removed per request

                # =====================
                # ROUND OVER TRANSITION
                # =====================
                if match_state == 'round_over':
                    # Overlay round result
                    if round_result_reason == 'draw':
                        title = 'draw'
                        sub = 'replaying round'
                    else:
                        winner_name = getattr(round_winner, 'name', 'nate') if round_winner is not None else 'nate'
                        title = f'{winner_name} wins'
                        sub = 'time over' if round_result_reason == 'time' else ''

                    title_s = font_big.render(title, True, TEXT_RED)
                    sub_s = font_small.render(sub, True, TEXT_RED)
                    screen.blit(title_s, (WIDTH//2 - title_s.get_width()//2, 120))
                    screen.blit(sub_s, (WIDTH//2 - sub_s.get_width()//2, 190))

                    # hint removed per request
                    # hint blit removed per request

                    # After pause, start next round or end match
                    now = pygame.time.get_ticks()
                    if round_over_started == 0:
                        # Enter was pressed to skip
                        round_over_started = now - ROUND_OVER_PAUSE_MS - 1

                    if now - round_over_started >= ROUND_OVER_PAUSE_MS:
                        # If someone reached 2 wins, match ends.
                        if finalize_match_if_needed():
                            # Keep the last round's winner/loser end states on screen
                            pass
                        else:
                            # Next round number rules:
                            # - Default: increment round
                            # - If draw: replay same round number
                            next_round = current_round + 1
                            if round_result_reason == 'draw':
                                next_round = current_round

                            # Hard cap: if we somehow exceed 3, force match end by score
                            if next_round > 3:
                                match_state = 'match_over'
                            else:
                                start_round(next_round)

                # =====================
                # MATCH OVER
                # =====================
                if match_state == 'match_over':
                    # Determine final winner by round wins
                    if p1_round_wins > p2_round_wins:
                        final_winner, final_loser = p1, p2
                    elif p2_round_wins > p1_round_wins:
                        final_winner, final_loser = p2, p1
                    else:
                        final_winner, final_loser = None, None

                    if final_winner is not None and final_loser is not None:
                        winner_name = getattr(final_winner, 'name', 'nate')
                        loser_name = getattr(final_loser, 'name', 'nate')
                        win_text = f'{winner_name} wins'
                        lose_text = f'{loser_name} loses'

                        win_surf = font_big.render(win_text, True, TEXT_RED)
                        lose_surf = font_mid.render(lose_text, True, TEXT_RED)
                        screen.blit(win_surf, (WIDTH//2 - win_surf.get_width()//2, 120))
                        screen.blit(lose_surf, (WIDTH//2 - lose_surf.get_width()//2, 190))
                    else:
                        draw_surf = font_big.render('draw', True, TEXT_RED)
                        screen.blit(draw_surf, (WIDTH//2 - draw_surf.get_width()//2, 140))

                    # hint removed per request
                    # hint blit removed per request

            pygame.display.flip()


        if goto_title:
            continue
        break

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()



def load_specific_frame(folder: str, filename: str, size: tuple[int, int] | None = None):
    """Load a single image by filename from folder. Returns None if missing."""
    try:
        path = os.path.join(folder, filename)
        if not os.path.isfile(path):
            return None
        img = pygame.image.load(path).convert_alpha()
        if size is not None:
            img = pygame.transform.scale(img, size)
        return img
    except Exception:
        return None
