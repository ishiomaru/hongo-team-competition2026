import json
import heapq
import math
import os
import subprocess
import tempfile
import tkinter as tk
from tkinter import messagebox

# ============================================================
# Hexa Udon AI / A* simulator
#
# match.json を読み込み、六角格子上で
#   - 巡回車
#   - 補給車
# を協調制御するシミュレータ。
#
# AI用.pdf の方針:
#   A* + 六方向移動
#   地形 / 道路状態 / 燃料 / 交通量
#   うどん候補の総コスト比較
#   未取得種類を優先
#   燃料不足車両を補給車が優先
#   巡回車の分散
#
# JSONの実際のキー名は公式の「ヘキサうどんのフォーマット」に
# 合わせて必要なら load_match() の normalize 部分だけ調整する。
# ============================================================

MATCH_FILE = "match.json"
SETTING_FILE = "setting.json"
SERVER_URL = "http://localhost:8080"
# コマンドリスト.txt に記載されている Procon-Token
PROCON_TOKEN = "hongo6420bb98d26dc0fc9cc5860d6caea25324a939b0b9ff9e66c3786e0e3e8"

# サーバー連携用ファイル
SUBMISSION_FILE = "submission.json"
DAY_MATCH_TEMPLATE = "match_day{day}.json"
DAY_SUBMISSION_TEMPLATE = "submission_day{day}.json"
INITIAL_MATCH_FILE = "initial_match.json"
NOW_FILE = "now.json"
SERVER_STATE_TEMPLATE = "server_day{day}.json"

# 初回の完全な問題データは、同じ階層の match.json を使用する。
# True: ローカルの match.json を初期データとして使う
# False: サーバーの / から初期データを取得する
USE_LOCAL_INITIAL_MATCH = True

# -----------------------------
# UI
# -----------------------------
HEX_SIZE = 29
MAP_X = 30
MAP_Y = 38
PANEL_W = 390
FPS_MS = 350

BG = "#111827"
PANEL = "#1f2937"
GRID = "#374151"
ROAD = "#9ca3af"
TEXT = "#f3f4f6"
MUTED = "#9ca3af"
PATH = "#38bdf8"
START = "#facc15"
PATROL = ["#22c55e", "#a78bfa", "#fb7185", "#f59e0b", "#2dd4bf"]
SUPPORT = "#f97316"
UDON_COLORS = {
    1: "#ef4444",
    2: "#eab308",
    3: "#3b82f6",
    4: "#a855f7",
    5: "#ec4899",
}

# コスト係数。大会ルールの具体値がJSONにあればそちらを優先する。
DEFAULT_TERRAIN_COST = {
    0: 1.0,
    1: 1.0,
    2: 2.0,
    3: 4.0,
    4: 999999.0,
}
DEFAULT_ROAD_COST = {
    0: 0.0,
    1: 0.0,
    2: 1.0,
    3: 3.0,
    4: 999999.0,
}


# ============================================================
# Server communication
# ============================================================

def _curl_get(path, output_file):
    """curl.exe でサーバーからJSONを取得してファイルに保存する。"""
    url = SERVER_URL.rstrip("/") + path
    try:
        result = subprocess.run(
            [
                "curl.exe",
                "-sS",
                "-H", f"Procon-Token: {PROCON_TOKEN}",
                url,
            ],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("curl.exe が見つかりません。Windowsのcurlを確認してください。")

    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"サーバーGETに失敗しました: {err or result.returncode}")

    raw = result.stdout
    if not raw.strip():
        raise RuntimeError(f"サーバーGETの応答が空です: {url}")

    # サーバーのJSONをそのまま保存する。
    with open(output_file, "wb") as f:
        f.write(raw)

    # UTF-8 BOM付きでも検証できるようにする。
    try:
        text = raw.decode("utf-8-sig")
        data = json.loads(text)
    except Exception as e:
        raise RuntimeError(
            f"サーバーから取得したデータがJSONとして読めません: {e}"
        )

    return data


def fetch_setting(filename=SETTING_FILE):
    """コマンドリストの /setting 相当。"""
    data = _curl_get("/setting", filename)
    return filename, data


def fetch_match(filename=MATCH_FILE):
    """
    コマンドリストの「現在情報の取得」相当。
    / のレスポンスを指定した名前で保存する。
    """
    data = _curl_get("/", filename)
    return filename, data


def deep_merge_state(base, current, preserve_keys=None):
    """
    初回に取得した完全なmatch情報を土台に、
    サーバーから返ってきた「現在状態」を重ねる。

    mapは本戦の初期マップとして固定参照するため、
    current側にmapが存在しても上書きしない。
    それ以外は現在情報を優先し、辞書は再帰的にマージする。
    """
    preserve_keys = set(preserve_keys or {"map"})

    if not isinstance(base, dict):
        base = {}
    if not isinstance(current, dict):
        return dict(base)

    merged = dict(base)
    for key, value in current.items():
        if key in preserve_keys:
            continue
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_state(merged[key], value, preserve_keys)
        else:
            merged[key] = value
    return merged


def create_now_from_initial(initial_data):
    """初期データからnow.jsonを作る。"""
    if not isinstance(initial_data, dict):
        raise ValueError("初期match情報がJSON objectではありません。")
    if not isinstance(initial_data.get("map"), dict):
        raise ValueError("初期match情報にmapがありません。")
    now = dict(initial_data)
    save_json(now, NOW_FILE)
    return now


def load_local_initial_match():
    """同じ階層のmatch.jsonを初期問題データとして読み込む。"""
    if not os.path.exists(MATCH_FILE):
        raise FileNotFoundError(
            f"初期データ {MATCH_FILE} が見つかりません。"
        )

    try:
        with open(MATCH_FILE, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"{MATCH_FILE} のJSONが不正です: {e}")

    if not isinstance(data, dict):
        raise ValueError(f"{MATCH_FILE} のルートはJSON objectである必要があります。")
    if not isinstance(data.get("map"), dict):
        raise ValueError(
            f"{MATCH_FILE} にmapがありません。\n"
            "初期マップを含む完全なmatch.jsonを配置してください。"
        )

    # ローカル初期データを初期保存用ファイルにも残す。
    save_json(data, INITIAL_MATCH_FILE)
    return data


def update_now_with_server_state(current_data):
    """
    now.jsonを読み、サーバーの最新状態を反映して再生成する。
    mapは常に初回取得時のものを保持する。
    """
    if not os.path.exists(NOW_FILE):
        raise FileNotFoundError(
            f"{NOW_FILE} がありません。先に初回のmatch取得を行ってください。"
        )

    with open(NOW_FILE, "r", encoding="utf-8-sig") as f:
        previous = json.load(f)

    now = deep_merge_state(previous, current_data, preserve_keys={"map"})
    save_json(now, NOW_FILE)
    return now


def prepare_initial_match(data):
    """
    最初のGETで得た完全なmatchを初期マップとして保存し、
    AIが参照するnow.jsonを初期化する。
    """
    save_json(data, INITIAL_MATCH_FILE)
    return create_now_from_initial(data)


def fetch_initial_match():
    """
    初期matchを用意してnow.jsonを作る。

    通常は同じ階層のmatch.jsonを初期データとして使用する。
    USE_LOCAL_INITIAL_MATCH=False の場合だけサーバーの/から取得する。
    """
    if USE_LOCAL_INITIAL_MATCH:
        data = load_local_initial_match()
        now = prepare_initial_match(data)
        return data, now

    _, data = fetch_match(INITIAL_MATCH_FILE)

    # サーバーが試合終了等のエラーJSONを返した場合、
    # 「mapがない」という別のエラーにすり替えない。
    if isinstance(data, dict) and data.get("code"):
        code = data.get("code")
        message = data.get("message", "")
        raise RuntimeError(f"サーバーエラー: {code}\n{message}")

    now = prepare_initial_match(data)
    return data, now


def fetch_current_and_update_now(day_number):
    """
    2日目以降の現在情報をGETし、初期mapを保持したままnow.jsonへ反映する。

    サーバーがAccessTimeErrorなどを返した場合は例外にして、
    既存のnow.jsonを壊さない。
    """
    state_name = SERVER_STATE_TEMPLATE.format(day=day_number)
    _, current = fetch_match(state_name)

    if isinstance(current, dict) and current.get("code"):
        code = current.get("code")
        message = current.get("message", "")
        raise RuntimeError(f"サーバーエラー: {code}\n{message}")

    now = update_now_with_server_state(current)
    return current, now, state_name


def post_submission(filename=SUBMISSION_FILE):
    """コマンドリストのPOST相当。submission JSONをサーバーへ送る。"""
    if not os.path.exists(filename):
        raise FileNotFoundError(f"提出ファイルがありません: {filename}")

    url = SERVER_URL.rstrip("/") + "/"
    try:
        result = subprocess.run(
            [
                "curl.exe",
                "-sS",
                "-i",
                "-X", "POST",
                "-H", "Content-Type: application/json",
                "-H", f"Procon-Token: {PROCON_TOKEN}",
                "--data-binary", f"@{filename}",
                url,
            ],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        raise RuntimeError("curl.exe が見つかりません。Windowsのcurlを確認してください。")

    response = result.stdout.decode("utf-8", errors="replace")
    if result.returncode != 0:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"提出POSTに失敗しました: {err or result.returncode}")

    return response


def save_json(data, filename):
    """Windows PowerShellのBOM付きJSONも読めるが、出力は通常のUTF-8にする。"""
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    return filename


def get_current_day(data, fallback=0):
    """サーバーJSONに現在日が含まれていれば取得。なければローカル日を使う。"""
    if not isinstance(data, dict):
        return fallback

    for key in ("day", "currentDay", "dayIndex", "current_day"):
        if key in data:
            value = as_int(data[key], fallback)
            return max(0, value)

    # ネストされたstate/gameがある場合も確認
    for parent in ("state", "game", "status"):
        obj = data.get(parent)
        if isinstance(obj, dict):
            for key in ("day", "currentDay", "dayIndex", "current_day"):
                if key in obj:
                    return max(0, as_int(obj[key], fallback))

    return fallback


def _extract_day_steps(data):
    if not isinstance(data, dict):
        return []
    value = data.get("daySteps", [])
    return value if isinstance(value, list) else []


# ============================================================
# Utility
# ============================================================

def as_int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def as_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pick(d, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d:
            return d[k]
    return default


def cell_value(layer, x, y, default=0):
    """2次元List / dict座標の両方を許容する。"""
    if layer is None:
        return default

    if isinstance(layer, list):
        if 0 <= y < len(layer):
            row = layer[y]
            if isinstance(row, list) and 0 <= x < len(row):
                return row[x]
        return default

    if isinstance(layer, dict):
        v = layer.get(f"{x},{y}")
        if v is not None:
            return v
        v = layer.get(f"{x}:{y}")
        if v is not None:
            return v
        v = layer.get(str(y))
        if isinstance(v, dict):
            return v.get(str(x), default)
    return default


def find_layer(data, *names):
    if not isinstance(data, dict):
        return None
    for name in names:
        if name in data:
            return data[name]
    layers = data.get("maps")
    if isinstance(layers, dict):
        for name in names:
            if name in layers:
                return layers[name]
    return None


# ============================================================
# Hex coordinates: odd-row offset
# ============================================================

def neighbors(x, y, width, height):
    # horizontal layout / odd-r offset
    if y & 1:
        dirs = [
            (-1, 0), (1, 0),
            (0, -1), (1, -1),
            (0, 1), (1, 1),
        ]
    else:
        dirs = [
            (-1, 0), (1, 0),
            (-1, -1), (0, -1),
            (-1, 1), (0, 1),
        ]

    for dx, dy in dirs:
        nx, ny = x + dx, y + dy
        if 0 <= nx < width and 0 <= ny < height:
            yield nx, ny


def offset_to_cube(x, y):
    # odd-r -> cube
    q = x - (y - (y & 1)) // 2
    r = y
    return q, -q-r, r


def hex_distance(a, b):
    ax, ay, az = offset_to_cube(*a)
    bx, by, bz = offset_to_cube(*b)
    return max(abs(ax-bx), abs(ay-by), abs(az-bz))


# ============================================================
# Data
# ============================================================

class Vehicle:
    def __init__(self, raw, kind, index):
        raw = raw if isinstance(raw, dict) else {}

        self.id = str(pick(raw, "id", "name", default=f"{kind}{index+1}"))
        self.kind = kind
        self.index = index

        pos = pick(raw, "position", "pos", default=None)
        if isinstance(pos, dict):
            self.x = as_int(pick(pos, "x", default=0))
            self.y = as_int(pick(pos, "y", default=0))
        elif isinstance(pos, list) and len(pos) >= 2:
            self.x, self.y = as_int(pos[0]), as_int(pos[1])
        else:
            self.x = as_int(pick(raw, "x", "pos_x", default=0))
            self.y = as_int(pick(raw, "y", "pos_y", default=0))

        self.max_fuel = as_float(
            pick(raw, "max_fuel", "fuel_max", "fuel_capacity", default=100)
        )
        self.fuel = as_float(
            pick(raw, "fuel", "current_fuel", default=self.max_fuel)
        )

        self.speed = max(1, as_int(pick(raw, "speed", "move", default=1)))
        self.collected = set()
        self.collected_count = 0
        self.path = []
        self.last_path_cost = 0.0
        self.target = None
        self.status = "待機"
        self.total_distance = 0

        inv = pick(raw, "inventory", "udon_inventory", "collected", default=[])
        if isinstance(inv, list):
            for item in inv:
                if isinstance(item, dict):
                    t = pick(item, "type", "id", "kind", default=None)
                else:
                    t = item
                if t is not None:
                    self.collected.add(as_int(t, t))
        elif isinstance(inv, dict):
            for k, v in inv.items():
                if as_int(v, 0) > 0:
                    self.collected.add(as_int(k, k))

    def fuel_ratio(self):
        if self.max_fuel <= 0:
            return 0.0
        return max(0.0, self.fuel / self.max_fuel)


class HexMap:
    def __init__(self, data):
        self.data = data

        raw_map_obj = data.get("map") if isinstance(data, dict) else None
        if isinstance(raw_map_obj, dict) and isinstance(raw_map_obj.get("cells"), list):
            raw_map = raw_map_obj["cells"]
            self.height = as_int(raw_map_obj.get("height"), len(raw_map))
            self.width = as_int(raw_map_obj.get("width"), max(len(r) for r in raw_map if isinstance(r, list)))
        else:
            raw_map = find_layer(data, "terrain", "field", "grid")
            if raw_map is None and isinstance(data, list):
                raw_map = data
            if not isinstance(raw_map, list) or not raw_map:
                raise ValueError("match.json に map.cells がありません。")
            self.height = len(raw_map)
            self.width = max(len(r) for r in raw_map if isinstance(r, list))

        self.terrain = [[0] * self.width for _ in range(self.height)]
        for y, row in enumerate(raw_map):
            if not isinstance(row, list):
                continue
            for x, value in enumerate(row):
                if x >= self.width:
                    continue
                if isinstance(value, dict):
                    self.terrain[y][x] = as_int(
                        pick(value, "terrain", "type", "tile", default=0)
                    )
                else:
                    self.terrain[y][x] = as_int(value)

        self.road = find_layer(data, "road", "roads", "road_state", "road_map")
        self.traffic = find_layer(
            data, "traffic", "traffic_map", "congestion", "traffic_volume"
        )
        # 本戦 match.json は spots が {brand, pos, stocks}。
        # UI/AI内部では brand+1 をうどん種類として扱い、brand=0 も消えないようにする。
        self.udon = [[0] * self.width for _ in range(self.height)]
        self.udon_stock = [[0] * self.width for _ in range(self.height)]
        spots = data.get("spots", []) if isinstance(data, dict) else []
        if isinstance(spots, list):
            for spot in spots:
                if not isinstance(spot, dict):
                    continue
                pos = as_int(pick(spot, "pos", default=-1), -1)
                if 0 <= pos < self.width * self.height:
                    x, y = pos % self.width, pos // self.width
                    brand = as_int(pick(spot, "brand", default=0), 0)
                    stock = as_int(pick(spot, "stocks", "stock", default=0), 0)
                    if stock > 0:
                        self.udon[y][x] = brand + 1
                        self.udon_stock[y][x] = stock
        else:
            legacy = find_layer(data, "udon", "udon_map", "stock", "inventory_map")
            self.udon = [[as_int(cell_value(legacy, x, y, 0)) for x in range(self.width)] for y in range(self.height)]
            self.udon_stock = [[1 if self.udon[y][x] else 0 for x in range(self.width)] for y in range(self.height)]

        self.terrain_cost = dict(DEFAULT_TERRAIN_COST)
        self.road_cost = dict(DEFAULT_ROAD_COST)

        costs = data.get("costs", {}) if isinstance(data, dict) else {}
        if isinstance(costs, dict):
            tc = pick(costs, "terrain", "terrain_cost", default={})
            rc = pick(costs, "road", "road_cost", default={})
            if isinstance(tc, dict):
                for k, v in tc.items():
                    self.terrain_cost[as_int(k, k)] = as_float(v, 1.0)
            if isinstance(rc, dict):
                for k, v in rc.items():
                    self.road_cost[as_int(k, k)] = as_float(v, 0.0)

        self.blocked = set()
        for y in range(self.height):
            for x in range(self.width):
                t = self.terrain[y][x]
                if self.terrain_cost.get(t, 1.0) >= 999999:
                    self.blocked.add((x, y))

    def in_bounds(self, p):
        x, y = p
        return 0 <= x < self.width and 0 <= y < self.height

    def terrain_at(self, p):
        x, y = p
        return self.terrain[y][x]

    def road_at(self, p):
        return as_int(cell_value(self.road, p[0], p[1], 0))

    def traffic_at(self, p):
        return as_float(cell_value(self.traffic, p[0], p[1], 0))

    def udon_at(self, p):
        x, y = p
        if not self.in_bounds(p):
            return 0
        return self.udon[y][x] if self.udon_stock[y][x] > 0 else 0

    def consume_udon(self, p):
        x, y = p
        if not self.in_bounds(p) or self.udon_stock[y][x] <= 0:
            return 0
        self.udon_stock[y][x] -= 1
        if self.udon_stock[y][x] <= 0:
            self.udon[y][x] = 0
        return 1


def load_match(filename=MATCH_FILE):
    if not os.path.exists(filename):
        raise FileNotFoundError(f"{filename} が見つかりません。")

    with open(filename, "r", encoding="utf-8-sig") as f:
        data = json.load(f)

    if not isinstance(data, dict):
        raise ValueError("match.json のルートはJSON objectである必要があります。")

    field = HexMap(data)

    # 本戦形式: agents は開始位置を 0..width*height-1 で表す。
    agents = data.get("agents", [])
    if not isinstance(agents, list) or not agents:
        raise ValueError("match.json に agents がありません。")

    patrols = []
    for i, pos in enumerate(agents):
        pos = as_int(pos, -1)
        if not 0 <= pos < field.width * field.height:
            raise ValueError(f"agents[{i}]={pos} はマップ範囲外です。")
        x, y = pos % field.width, pos // field.width
        patrols.append(Vehicle({
            "id": f"agent{i}",
            "position": [x, y],
            "max_fuel": data.get("fuelLimits", 20),
            "fuel": data.get("fuelLimits", 20),
            "speed": 1,
        }, "patrol", i))

    # 追加形式として supportAgents があれば補給車として扱う。
    supports = []
    for i, pos in enumerate(data.get("supportAgents", []) if isinstance(data.get("supportAgents", []), list) else []):
        pos = as_int(pos, -1)
        if 0 <= pos < field.width * field.height:
            x, y = pos % field.width, pos // field.width
            supports.append(Vehicle({
                "id": f"support{i}", "position": [x, y],
                "max_fuel": data.get("fuelLimits", 20),
                "fuel": data.get("fuelLimits", 20), "speed": 1,
            }, "support", i))

    settings = {
        "day_seconds": data.get("daySeconds", []),
        "day_steps": data.get("daySteps", []),
        "fuel_limit": data.get("fuelLimits", 20),
        "players": data.get("players", 1),
        "busy_threshold": data.get("busyThreshold", 2),
        "jammed_threshold": data.get("jammedThreshold", 4),
        "starts_at": data.get("startsAt"),
    }
    return field, patrols, supports, settings


# ============================================================
# A*
# ============================================================

class Planner:
    def __init__(self, field, patrols):
        self.field = field
        self.patrols = patrols

        # AI用.pdf の「車両分散」をコスト化する。
        self.separation_weight = 1.25
        self.traffic_weight = 0.75
        self.terrain_weight = 1.0
        self.road_weight = 1.0
        self.fuel_weight = 0.15

    def separation_penalty(self, p, vehicle):
        penalty = 0.0
        for other in self.patrols:
            if other is vehicle:
                continue
            d = hex_distance(p, (other.x, other.y))
            if d == 0:
                penalty += 8.0
            elif d == 1:
                penalty += 4.0
            elif d == 2:
                penalty += 1.0
        return penalty * self.separation_weight

    def move_cost(self, p, vehicle):
        terrain = self.field.terrain_at(p)
        road = self.field.road_at(p)
        traffic = self.field.traffic_at(p)

        tc = self.field.terrain_cost.get(terrain, 1.0)
        rc = self.field.road_cost.get(road, 0.0)

        # 移動1回の燃料消費。
        fuel_use = max(0.1, tc + rc)

        if tc >= 999999 or rc >= 999999:
            return math.inf, math.inf

        cost = (
            self.terrain_weight * tc
            + self.road_weight * rc
            + self.traffic_weight * traffic
            + self.fuel_weight * fuel_use
            + self.separation_penalty(p, vehicle)
        )
        return cost, fuel_use

    def astar(self, vehicle, goal):
        start = (vehicle.x, vehicle.y)
        if not self.field.in_bounds(goal):
            return [], math.inf, math.inf
        if start == goal:
            return [], 0.0, 0.0

        # (f, g, fuel_used, x, y)
        open_heap = []
        heapq.heappush(open_heap, (0.0, 0.0, 0.0, start[0], start[1]))

        g_cost = {start: 0.0}
        fuel_cost = {start: 0.0}
        parent = {}
        closed = set()

        while open_heap:
            f, g, fuel, x, y = heapq.heappop(open_heap)
            p = (x, y)

            if p in closed:
                continue
            closed.add(p)

            if p == goal:
                path = []
                cur = goal
                while cur != start:
                    path.append(cur)
                    cur = parent[cur]
                path.reverse()
                return path, g, fuel

            for nx, ny in neighbors(x, y, self.field.width, self.field.height):
                np = (nx, ny)

                if np in self.field.blocked or np in closed:
                    continue

                step_cost, step_fuel = self.move_cost(np, vehicle)
                if not math.isfinite(step_cost):
                    continue

                new_fuel = fuel + step_fuel

                # 現在燃料だけで到達可能な経路に限定
                if new_fuel > vehicle.fuel + 1e-9:
                    continue

                new_g = g + step_cost
                if new_g >= g_cost.get(np, math.inf):
                    continue

                g_cost[np] = new_g
                fuel_cost[np] = new_fuel
                parent[np] = p

                h = hex_distance(np, goal)
                heapq.heappush(
                    open_heap,
                    (new_g + h, new_g, new_fuel, nx, ny)
                )

        return [], math.inf, math.inf

    def candidates(self, vehicle):
        """すべての未取得うどんを候補としてA*し、総合コスト最小を返す。"""
        candidates = []

        for y in range(self.field.height):
            for x in range(self.field.width):
                p = (x, y)
                utype = self.field.udon_at(p)
                if not utype:
                    continue

                # すでに取得した種類は候補から除外
                if utype in vehicle.collected:
                    continue

                path, cost, fuel = self.astar(vehicle, p)
                if not path and p != (vehicle.x, vehicle.y):
                    continue

                # 遠すぎるうどんより、未取得種類を優先。
                # 種類数を増やすことを第一目的として少しだけボーナス。
                variety_bonus = -12.0

                score = cost + variety_bonus
                candidates.append(
                    (score, cost, fuel, p, path, utype)
                )

        candidates.sort(key=lambda z: z[0])
        return candidates

    def choose_patrol_target(self, vehicle):
        candidates = self.candidates(vehicle)
        if not candidates:
            return None

        best = candidates[0]

        # 同じ種類を狙う候補だけなら、交通量と距離を含めた
        # A*総コストが低いものを選ぶ。
        return best

    def choose_support_target(self, support):
        if not self.patrols:
            return None

        # 燃料率が低い巡回車ほど優先。
        # 同率付近では近い車を優先。
        ranked = []
        for p in self.patrols:
            dist = hex_distance((support.x, support.y), (p.x, p.y))
            urgency = 1.0 - p.fuel_ratio()
            score = urgency * 100.0 - dist
            ranked.append((score, p))

        ranked.sort(key=lambda z: z[0], reverse=True)
        target = ranked[0][1]

        path, cost, fuel = self.astar(support, (target.x, target.y))
        if not path and (support.x, support.y) != (target.x, target.y):
            return None

        return target, path, cost, fuel


# ============================================================
# Simulation
# ============================================================

class Simulation:
    def __init__(self, field, patrols, supports, settings):
        self.field = field
        self.patrols = patrols
        self.supports = supports
        self.settings = settings
        self.planner = Planner(field, patrols)

        self.day = 0
        self.step_in_day = 0
        self.total_step = 0
        self.score = 0
        self.action_history = []
        self.running = False
        self.finished = False
        self.log_lines = []
        self.selected = 0

        self.day_steps = list(settings.get("day_steps", []))
        self.day_seconds = list(settings.get("day_seconds", []))
        self.time_limit = len(self.day_steps) if self.day_steps else as_int(
            pick(settings, "days", "time_limit", "max_days", default=200), 200
        )

    def log(self, text):
        self.log_lines.append(text)
        if len(self.log_lines) > 8:
            self.log_lines.pop(0)

    def collect(self, vehicle):
        utype = self.field.udon_at((vehicle.x, vehicle.y))
        if not utype:
            return

        if utype in vehicle.collected:
            return

        vehicle.collected.add(utype)
        vehicle.collected_count += 1
        self.score += 1

        # match.json の stocks を1つ消費する。
        self.field.consume_udon((vehicle.x, vehicle.y))

        self.log(
            f"{vehicle.id}: うどん種類 {utype} を取得 "
            f"(score={self.score})"
        )

    def refill(self, patrol, support):
        if hex_distance((patrol.x, patrol.y), (support.x, support.y)) == 0:
            amount = patrol.max_fuel * 0.50
            patrol.fuel = min(patrol.max_fuel, patrol.fuel + amount)
            patrol.status = "補給完了"
            support.status = f"{patrol.id}を補給"
            self.log(f"{support.id}: {patrol.id} を補給")

    def plan_patrol(self, vehicle):
        result = self.planner.choose_patrol_target(vehicle)
        if result is None:
            vehicle.path = []
            vehicle.target = None
            vehicle.status = "候補なし"
            return

        score, cost, fuel, target, path, utype = result
        vehicle.path = path
        vehicle.target = target
        vehicle.last_path_cost = cost
        vehicle.status = f"うどん{utype}へ"
        self.log(
            f"{vehicle.id}: target={target} type={utype} "
            f"cost={cost:.1f} fuel={fuel:.1f}"
        )

    def plan_support(self, vehicle):
        result = self.planner.choose_support_target(vehicle)
        if result is None:
            vehicle.path = []
            vehicle.target = None
            vehicle.status = "補給先なし"
            return

        target, path, cost, fuel = result
        vehicle.path = path
        vehicle.target = (target.x, target.y)
        vehicle.last_path_cost = cost
        vehicle.status = f"{target.id}へ補給"

    def move_one(self, vehicle):
        if not vehicle.path:
            return False

        nx, ny = vehicle.path.pop(0)
        old = (vehicle.x, vehicle.y)

        # 実移動時にも燃料を再計算。
        cost, fuel = self.planner.move_cost((nx, ny), vehicle)
        if not math.isfinite(cost) or fuel > vehicle.fuel + 1e-9:
            vehicle.path = []
            vehicle.status = "燃料不足"
            return False

        vehicle.x, vehicle.y = nx, ny
        vehicle.fuel = max(0.0, vehicle.fuel - fuel)
        vehicle.total_distance += 1

        if old != (nx, ny):
            return True
        return False

    def step(self):
        if self.finished:
            return

        if self.finished:
            return

        self.total_step += 1
        current_day_steps = self.day_steps[self.day] if self.day < len(self.day_steps) else 1
        self.step_in_day += 1

        # 1. 巡回車の目標決定
        for v in self.patrols:
            # 燃料が低いときは目標を再評価
            if not v.path or v.fuel_ratio() < 0.20:
                self.plan_patrol(v)

        # 2. 補給車の目標決定
        for s in self.supports:
            if not s.path:
                self.plan_support(s)

        # 3. 1日分の移動
        step_actions = []
        for v in self.patrols:
            if not v.path:
                self.plan_patrol(v)
            self.move_one(v)
            self.collect(v)
            step_actions.append({"agent": v.index, "pos": v.y * self.field.width + v.x})

        for s in self.supports:
            if not s.path:
                self.plan_support(s)
            self.move_one(s)
            step_actions.append({"agent": f"support{s.index}", "pos": s.y * self.field.width + s.x})

        self.action_history.append(step_actions)

        # 4. 補給
        for s in self.supports:
            for p in self.patrols:
                self.refill(p, s)

        # 5. 終了判定
        remaining = 0
        for y in range(self.field.height):
            for x in range(self.field.width):
                if self.field.udon_at((x, y)):
                    remaining += 1

        if remaining == 0:
            self.finished = True
            self.running = False
            self.log("すべてのうどん候補を回収。")

        if self.step_in_day >= current_day_steps:
            self.day += 1
            self.step_in_day = 0
            self.log(f"DAY {self.day} 開始")

        if self.day >= self.time_limit:
            self.finished = True
            self.running = False
            self.log("全日程終了。")

    def build_submission(self):
        """シミュレーション中の各ステップの行動をJSON化する。

        注意: 公式の回答サーバーが要求する最終POSTスキーマは、
        別紙「フォーマットについて」の内容に合わせて最終調整する。
        ここでは match.json と同じ agent/pos 概念で、再現可能な行動列を出力する。
        """
        return {
            "actions": self.action_history
        }

    def save_submission(self, filename="submission.json"):
        return save_json(self.build_submission(), filename)

    def run_steps(self, count):
        """現在読み込んでいるmatchについて指定ステップだけ計算する。"""
        count = max(0, as_int(count, 0))
        self.running = False
        for _ in range(count):
            if self.finished:
                break
            self.step()

    def save_day_submission(self, filename):
        return self.save_submission(filename)

    def reset(self):
        # サーバー連携時はnow.jsonをAI入力として再初期化する。
        source = NOW_FILE if os.path.exists(NOW_FILE) else MATCH_FILE
        field, patrols, supports, settings = load_match(source)
        self.__init__(field, patrols, supports, settings)


# ============================================================
# UI
# ============================================================

class App:
    def __init__(self, root, sim):
        self.root = root
        self.sim = sim
        self._server_day = 0

        root.title("HEXA UDON AI  /  A* COOPERATIVE ROUTING")
        root.configure(bg=BG)
        root.geometry("1280x850")
        root.minsize(1050, 720)

        self.canvas = tk.Canvas(
            root,
            bg="#0b1220",
            highlightthickness=0
        )
        self.canvas.pack(side="left", fill="both", expand=True)

        self.panel = tk.Frame(root, bg=PANEL, width=PANEL_W)
        self.panel.pack(side="right", fill="y")
        self.panel.pack_propagate(False)

        self.title = tk.Label(
            self.panel,
            text="HEXA UDON AI",
            fg=TEXT, bg=PANEL,
            font=("Consolas", 20, "bold")
        )
        self.title.pack(anchor="w", padx=18, pady=(18, 2))

        tk.Label(
            self.panel,
            text="A* / 6-DIRECTION / COOPERATIVE",
            fg=MUTED, bg=PANEL,
            font=("Consolas", 9)
        ).pack(anchor="w", padx=19, pady=(0, 14))

        self.info = tk.Label(
            self.panel,
            justify="left",
            anchor="nw",
            fg=TEXT, bg=PANEL,
            font=("Consolas", 10),
        )
        self.info.pack(fill="x", padx=18)

        btn_frame = tk.Frame(self.panel, bg=PANEL)
        btn_frame.pack(fill="x", padx=18, pady=12)

        self.start_btn = tk.Button(
            btn_frame, text="START / PAUSE",
            command=self.toggle,
            bg="#374151", fg=TEXT,
            activebackground="#4b5563",
            relief="flat"
        )
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))

        tk.Button(
            btn_frame, text="STEP",
            command=self.single_step,
            bg="#374151", fg=TEXT,
            activebackground="#4b5563",
            relief="flat"
        ).pack(side="left", expand=True, fill="x", padx=4)

        tk.Button(
            btn_frame, text="RESET",
            command=self.reset,
            bg="#374151", fg=TEXT,
            activebackground="#4b5563",
            relief="flat"
        ).pack(side="left", expand=True, fill="x", padx=(4, 0))

        tk.Button(
            self.panel, text="EXPORT submission.json",
            command=self.export_submission,
            bg="#0f766e", fg=TEXT,
            activebackground="#115e59",
            relief="flat"
        ).pack(fill="x", padx=18, pady=(0, 4))

        tk.Button(
            self.panel, text="GET match.json",
            command=self.fetch_only_match,
            bg="#374151", fg=TEXT,
            activebackground="#4b5563",
            relief="flat"
        ).pack(fill="x", padx=18, pady=2)

        tk.Button(
            self.panel, text="GET → A* → POST (NEXT DAY)",
            command=self.fetch_and_calculate_next_day,
            bg="#7c3aed", fg=TEXT,
            activebackground="#6d28d9",
            relief="flat"
        ).pack(fill="x", padx=18, pady=(2, 8))

        self.vehicle_label = tk.Label(
            self.panel,
            text="VEHICLES",
            fg="#93c5fd", bg=PANEL,
            font=("Consolas", 12, "bold")
        )
        self.vehicle_label.pack(anchor="w", padx=18, pady=(4, 4))

        self.vehicle_info = tk.Label(
            self.panel,
            justify="left",
            anchor="nw",
            fg=TEXT, bg=PANEL,
            font=("Consolas", 9),
        )
        self.vehicle_info.pack(fill="x", padx=18)

        self.log_label = tk.Label(
            self.panel,
            text="AI LOG",
            fg="#93c5fd", bg=PANEL,
            font=("Consolas", 12, "bold")
        )
        self.log_label.pack(anchor="w", padx=18, pady=(14, 4))

        self.log = tk.Label(
            self.panel,
            justify="left",
            anchor="nw",
            fg=MUTED, bg=PANEL,
            font=("Consolas", 8),
        )
        self.log.pack(fill="x", padx=18)

        self.help = tk.Label(
            self.panel,
            text=(
                "\nHEX MAP\n"
                "・左クリック: 車両選択\n"
                "・水色: 選択車両のA*経路\n"
                "・黄色: 車両\n"
                "・赤系: うどんスポット\n\n"
                "INPUT\n"
                "initial_match.json = 初期map\n"
                "now.json = 毎日のAI入力\n"
            ),
            justify="left",
            anchor="nw",
            fg=MUTED, bg=PANEL,
            font=("Consolas", 8)
        )
        self.help.pack(fill="x", padx=18, pady=10)

        self.canvas.bind("<Button-1>", self.click_map)
        self.root.bind("<space>", lambda e: self.toggle())
        self.root.bind("<Return>", lambda e: self.single_step())
        self.root.after(FPS_MS, self.tick)

        self.draw()

    def hex_points(self, x, y):
        cx = MAP_X + x * HEX_SIZE * 1.5
        cy = MAP_Y + y * HEX_SIZE * math.sqrt(3)
        if y & 1:
            cx += HEX_SIZE * 0.75

        points = []
        for i in range(6):
            a = math.radians(60 * i)
            points.extend([
                cx + HEX_SIZE * math.cos(a),
                cy + HEX_SIZE * math.sin(a)
            ])
        return points

    def hex_center(self, x, y):
        cx = MAP_X + x * HEX_SIZE * 1.5
        cy = MAP_Y + y * HEX_SIZE * math.sqrt(3)
        if y & 1:
            cx += HEX_SIZE * 0.75
        return cx, cy

    def draw_hex(self, x, y, fill, outline=GRID, width=1):
        self.canvas.create_polygon(
            self.hex_points(x, y),
            fill=fill,
            outline=outline,
            width=width
        )

    def terrain_color(self, value):
        return {
            0: "#263244",
            1: "#30465c",
            2: "#4b5563",
            3: "#713f12",
            4: "#111111",
        }.get(value, "#263244")

    def draw(self):
        self.canvas.delete("all")

        f = self.sim.field

        # map
        for y in range(f.height):
            for x in range(f.width):
                p = (x, y)
                fill = self.terrain_color(f.terrain_at(p))

                if p in f.blocked:
                    fill = "#111111"

                self.draw_hex(x, y, fill)

                # road indicator
                road = f.road_at(p)
                if road:
                    cx, cy = self.hex_center(x, y)
                    r = max(2, 3 + road * 2)
                    self.canvas.create_oval(
                        cx-r, cy-r, cx+r, cy+r,
                        fill=ROAD, outline=""
                    )

                # traffic ring
                traffic = f.traffic_at(p)
                if traffic > 0:
                    cx, cy = self.hex_center(x, y)
                    r = 9 + min(8, traffic * 2)
                    self.canvas.create_oval(
                        cx-r, cy-r, cx+r, cy+r,
                        outline="#64748b",
                        width=1
                    )

                # udon
                utype = f.udon_at(p)
                if utype:
                    cx, cy = self.hex_center(x, y)
                    r = 9
                    self.canvas.create_oval(
                        cx-r, cy-r, cx+r, cy+r,
                        fill=UDON_COLORS.get(utype, "#ffffff"),
                        outline="#ffffff",
                        width=1
                    )
                    self.canvas.create_text(
                        cx, cy,
                        text=str(utype),
                        fill="#111827",
                        font=("Consolas", 8, "bold")
                    )

        # selected patrol path
        if self.sim.patrols:
            selected = self.sim.patrols[
                min(self.sim.selected, len(self.sim.patrols)-1)
            ]
            for i, p in enumerate(selected.path):
                cx, cy = self.hex_center(*p)
                r = 5 if i else 7
                self.canvas.create_oval(
                    cx-r, cy-r, cx+r, cy+r,
                    fill=PATH,
                    outline=""
                )

        # target markers
        for v in self.sim.patrols:
            if v.target:
                cx, cy = self.hex_center(*v.target)
                self.canvas.create_oval(
                    cx-13, cy-13, cx+13, cy+13,
                    outline=PATROL[v.index % len(PATROL)],
                    width=2
                )

        for s in self.sim.supports:
            if s.target:
                cx, cy = self.hex_center(*s.target)
                self.canvas.create_rectangle(
                    cx-11, cy-11, cx+11, cy+11,
                    outline=SUPPORT,
                    width=2
                )

        # patrol cars
        for i, v in enumerate(self.sim.patrols):
            cx, cy = self.hex_center(v.x, v.y)
            rr = 12

            outline = "#ffffff" if i == self.sim.selected else PATROL[i % len(PATROL)]
            self.canvas.create_oval(
                cx-rr, cy-rr, cx+rr, cy+rr,
                fill=PATROL[i % len(PATROL)],
                outline=outline,
                width=3
            )
            self.canvas.create_text(
                cx, cy,
                text=str(i+1),
                fill="#111827",
                font=("Consolas", 9, "bold")
            )

        # support cars
        for i, s in enumerate(self.sim.supports):
            cx, cy = self.hex_center(s.x, s.y)
            self.canvas.create_rectangle(
                cx-11, cy-11, cx+11, cy+11,
                fill=SUPPORT,
                outline="#ffffff",
                width=2
            )
            self.canvas.create_text(
                cx, cy,
                text="S",
                fill="#111827",
                font=("Consolas", 9, "bold")
            )

        self.update_panel()

    def update_panel(self):
        s = self.sim

        state = "RUNNING" if s.running else ("FINISHED" if s.finished else "PAUSED")
        self.info.config(
            text=(
                f"STATE  : {state}\n"
                f"DAY    : {s.day:02d} / {s.time_limit:02d}\n"
                f"STEP   : {s.step_in_day:02d} / {(s.day_steps[s.day] if s.day < len(s.day_steps) else 0):02d}\n"
                f"SCORE  : {s.score:03d}\n"
                f"PATROL : {len(s.patrols)}\n"
                f"SUPPORT: {len(s.supports)}\n"
                f"MAP    : {s.field.width} x {s.field.height}\n"
            )
        )

        lines = []
        for i, v in enumerate(s.patrols):
            mark = ">" if i == s.selected else " "
            target = "-" if not v.target else f"{v.target[0]},{v.target[1]}"
            lines.append(
                f"{mark} P{i+1} "
                f"({v.x:2},{v.y:2}) "
                f"F:{v.fuel:5.1f}/{v.max_fuel:5.1f} "
                f"U:{len(v.collected):2d}\n"
                f"   {v.status:<16} T:{target}"
            )

        for i, v in enumerate(s.supports):
            lines.append(
                f"  S{i+1} "
                f"({v.x:2},{v.y:2}) "
                f"F:{v.fuel:5.1f}/{v.max_fuel:5.1f}\n"
                f"   {v.status}"
            )

        self.vehicle_info.config(text="\n".join(lines) if lines else "-")
        self.log.config(text="\n".join(s.log_lines) if s.log_lines else "-")

    def click_map(self, event):
        # 近い六角中心を選択
        best = None
        best_d = float("inf")

        for i, v in enumerate(self.sim.patrols):
            cx, cy = self.hex_center(v.x, v.y)
            d = (event.x-cx)**2 + (event.y-cy)**2
            if d < best_d and d < HEX_SIZE**2 * 2.5:
                best = i
                best_d = d

        if best is not None:
            self.sim.selected = best
            self.draw()

    def fetch_and_calculate_next_day(self):
        """
        1日目:
            同じ階層のmatch.jsonを初期データとして使用 → initial_match.json → now.json
        2日目以降:
            / から現在状態だけ取得 → 初期mapを保持してnow.jsonを更新

        その後、必ずnow.jsonをAIの入力としてA*計算し、
        その日の行動予定をPOSTする。
        """
        try:
            day = getattr(self, "_server_day", 0)

            if day == 0 and not os.path.exists(NOW_FILE):
                # 初回は同じ階層のmatch.jsonを完全な初期データとして使う。
                _, now_data = fetch_initial_match()
                source_name = MATCH_FILE if USE_LOCAL_INITIAL_MATCH else INITIAL_MATCH_FILE
                log_prefix = "ローカル初期match" if USE_LOCAL_INITIAL_MATCH else "初回match取得"
            elif day == 0 and os.path.exists(NOW_FILE):
                # now.jsonが残っていても、DAY 1開始時はmatch.jsonから初期化できる。
                _, now_data = fetch_initial_match()
                source_name = MATCH_FILE if USE_LOCAL_INITIAL_MATCH else INITIAL_MATCH_FILE
                log_prefix = "ローカル初期match再初期化" if USE_LOCAL_INITIAL_MATCH else "初回match再取得"
            else:
                # 2日目以降はmapを再取得せず、現在情報だけnow.jsonへ反映。
                _, now_data, source_name = fetch_current_and_update_now(day + 1)
                log_prefix = "現在情報取得 + now更新"

            # now.jsonには必ず初期mapが残っているので、毎日これをAI入力にする。
            field, patrols, supports, settings = load_match(NOW_FILE)
            sim = Simulation(field, patrols, supports, settings)

            # この日の日数ではなく、今回取得したmatchのdayStepsを使用。
            day_steps = settings.get("day_steps", [])
            if day < len(day_steps):
                steps = as_int(day_steps[day], 0)
            else:
                steps = as_int(day_steps[-1], 0) if day_steps else 0

            self.sim = sim
            sim.run_steps(steps)

            submission_name = DAY_SUBMISSION_TEMPLATE.format(day=day + 1)
            sim.save_day_submission(submission_name)

            # サーバーへ提出
            response = post_submission(submission_name)

            self.log_lines = [
                f"DAY {day + 1}: {log_prefix}",
                f"入力: {NOW_FILE} (map=initial)",
                f"状態取得: {source_name}",
                f"A*計算: {steps} steps",
                f"提出: {submission_name}",
                "SERVER RESPONSE:",
                *response.splitlines()[-3:],
            ]
            self.draw()

            self._server_day = day + 1

            messagebox.showinfo(
                "SERVER DAY",
                f"DAY {day + 1} 完了\n"
                f"状態取得: {source_name}\n"
                f"AI入力: {NOW_FILE}\n"
                f"A*: {steps} steps\n"
                f"提出: {submission_name}"
            )

        except Exception as e:
            messagebox.showerror("SERVER ERROR", str(e))

    def fetch_only_match(self):
        """
        同じ階層のmatch.jsonを初期matchとして読み込み、initial_match.jsonとnow.jsonを作る。
        """
        try:
            _, data = fetch_initial_match()
            self._server_day = 0
            field, patrols, supports, settings = load_match(NOW_FILE)
            self.sim = Simulation(field, patrols, supports, settings)
            self.draw()
            messagebox.showinfo(
                "MATCH GET",
                f"初期matchを取得しました。\n"
                f"初期データ: {INITIAL_MATCH_FILE}\n"
                f"AI参照: {NOW_FILE}\n"
                "mapは以後この初期mapを保持します。"
            )
        except Exception as e:
            messagebox.showerror("MATCH GET ERROR", str(e))

    def export_submission(self):
        try:
            path = self.sim.save_submission("submission.json")
            messagebox.showinfo("EXPORT", f"出力しました: {path}")
        except Exception as e:
            messagebox.showerror("EXPORT ERROR", str(e))

    def toggle(self):
        if not self.sim.finished:
            self.sim.running = not self.sim.running
            self.draw()

    def single_step(self):
        if self.sim.finished:
            return
        self.sim.running = False
        self.sim.step()
        self.draw()

    def reset(self):
        try:
            self.sim.reset()
            self.draw()
        except Exception as e:
            messagebox.showerror("RESET ERROR", str(e))

    def tick(self):
        if self.sim.running:
            self.sim.step()
            self.draw()
        self.root.after(FPS_MS, self.tick)


def main():
    try:
        # サーバー連携済みならnow.jsonを優先。
        source = NOW_FILE if os.path.exists(NOW_FILE) else MATCH_FILE
        field, patrols, supports, settings = load_match(source)
    except Exception as e:
        # GUIが起動できる環境ではエラーをダイアログ表示。
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("match.json ERROR", str(e))
            root.destroy()
        except Exception:
            print(str(e))
        return

    root = tk.Tk()
    sim = Simulation(field, patrols, supports, settings)
    App(root, sim)
    root.mainloop()


if __name__ == "__main__":
    main()
