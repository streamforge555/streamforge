# Streamforge Tiny2 User Camera Control - TEST v0.4.6
# Windows 10/11 + OBSBOT Center + OBS Studio + Tampermonkey
# Tiny2 control backend remains OBSBOT Center Global Hotkey by default.

# ============================================================
# 01. Imports / version / paths
# ============================================================
import argparse
import csv
import ctypes
import json
import os
import platform
import socket
import struct
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "0.4.6"
HOST = "127.0.0.1"
PORT = 8765
AUTH_TOKEN = "sf-tiny2-test-20260828"
BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(BASE_DIR, "tiny2_config.json")
LOG_PATH = os.path.join(BASE_DIR, "tiny2_camera_log.csv")


# ============================================================
# 02. Configuration
# ============================================================
DEFAULT_CONFIG = {
    "backend": "hotkey",
    "server_cooldown_ms": 700,
    "scene_key_poll_ms": 25,
    "hotkeys": {
        "left": ["CTRL", "ALT", "LEFT"],
        "right": ["CTRL", "ALT", "RIGHT"],
        "up": ["CTRL", "ALT", "UP"],
        "down": ["CTRL", "ALT", "DOWN"],
        "zoom_in": ["CTRL", "ALT", "I"],
        "zoom_out": ["CTRL", "ALT", "O"]
    },
    "obs_scene_hotkeys": {
        "scene_1": ["1"],
        "scene_2": ["2"],
        "scene_3": ["3"],
        "scene_4": ["4"],
        "control_on": ["4"],
        "control_off": ["1"]
    },
    "osc": {
        "host": "127.0.0.1",
        "port": 16284,
        "device_index": 0,
        "include_device_index": True,
        "move_speed": 35,
        "move_ms": 120,
        "zoom_start": 0,
        "zoom_step": 10
    }
}

COMMAND_LABELS = {
    "left": "c←",
    "right": "c→",
    "up": "c↑",
    "down": "c↓",
    "zoom_in": "c+",
    "zoom_out": "c-",
}
ALLOWED_COMMANDS = set(COMMAND_LABELS)
SCENE_CONTROL_STATE = {
    "1": False,  # Scene 1 = Tiny3
    "2": False,  # Scene 2 = Tiny2, no user control
    "3": False,  # Scene 3 = MEET
    "4": True,   # Scene 4 = Tiny2 + control guide + user control
}


def deep_merge(base, override):
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as file:
            json.dump(DEFAULT_CONFIG, file, ensure_ascii=False, indent=2)
        return deep_merge({}, DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as file:
            user_config = json.load(file)
        return deep_merge(DEFAULT_CONFIG, user_config)
    except Exception as error:
        print("[CONFIG ERROR] using defaults:", error)
        return deep_merge({}, DEFAULT_CONFIG)


CONFIG = load_config()


# ============================================================
# 03. Shared state / locks
# ============================================================
state_lock = threading.Lock()
command_lock = threading.Lock()
server_enabled = False
last_command_time = 0.0
last_scene_key = None
last_state_source = "startup"
panel = None
instance_mutex_handle = None


# ============================================================
# 03.5 Single-instance guard
#     Auto-start is enabled in v0.4.6, so a second controller must not run.
# ============================================================
def acquire_single_instance():
    global instance_mutex_handle

    if platform.system() != "Windows":
        return True

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p

    handle = kernel32.CreateMutexW(
        None,
        False,
        "Local\\StreamforgeTiny2UserControl",
    )
    if not handle:
        return False

    ERROR_ALREADY_EXISTS = 183
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return False

    instance_mutex_handle = handle
    return True


# ============================================================
# 04. CSV logging
# ============================================================
def log_event(source, command, backend, result, detail=""):
    new_file = not os.path.exists(LOG_PATH)
    try:
        with open(LOG_PATH, "a", newline="", encoding="utf-8-sig") as file:
            writer = csv.writer(file)
            if new_file:
                writer.writerow(["timestamp", "source", "command", "backend", "result", "detail"])
            writer.writerow([
                datetime.now().astimezone().isoformat(timespec="milliseconds"),
                source,
                command,
                backend,
                result,
                detail,
            ])
    except Exception as error:
        print("[LOG ERROR]", error)


# ============================================================
# 05. Windows keyboard sender / reader
#     1/2/3/4 are normal top-row number keys. Numpad is not used.
# ============================================================
VK = {
    "CTRL": 0x11,
    "ALT": 0x12,
    "LEFT": 0x25,
    "UP": 0x26,
    "RIGHT": 0x27,
    "DOWN": 0x28,
    "1": 0x31,
    "2": 0x32,
    "3": 0x33,
    "4": 0x34,
    "I": 0x49,
    "O": 0x4F,
}
KEYEVENTF_KEYUP = 0x0002


def _user32():
    if platform.system() != "Windows":
        raise RuntimeError("Windows hotkey support requires Windows")
    return ctypes.windll.user32


def send_hotkey(keys):
    user32 = _user32()
    try:
        codes = [VK[key] for key in keys]
    except KeyError as error:
        raise ValueError(f"Unknown key mapping: {error}") from error

    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
        time.sleep(0.015)
    time.sleep(0.04)
    for code in reversed(codes):
        user32.keybd_event(code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.015)


def is_key_down(key_name):
    return bool(_user32().GetAsyncKeyState(VK[key_name]) & 0x8000)


# ============================================================
# 06. Tiny2 Global Hotkey backend
# ============================================================
def hotkey_command(command):
    keys = CONFIG["hotkeys"].get(command)
    if not keys:
        raise ValueError(f"No Tiny2 hotkey mapping: {command}")
    send_hotkey(keys)


# ============================================================
# 07. Tiny2 OSC backend (experimental / retained from v0.4.3)
# ============================================================
def osc_string(value):
    data = value.encode("utf-8") + b"\x00"
    return data + (b"\x00" * ((4 - len(data) % 4) % 4))


def osc_packet(address, args):
    tags = "," + ("i" * len(args))
    packet = osc_string(address) + osc_string(tags)
    for value in args:
        packet += struct.pack(">i", int(value))
    return packet


def osc_send(address, values):
    cfg = CONFIG["osc"]
    args = list(values)
    if cfg.get("include_device_index", True):
        args.insert(0, int(cfg.get("device_index", 0)))
    packet = osc_packet(address, args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(packet, (cfg.get("host", "127.0.0.1"), int(cfg.get("port", 16284))))
    finally:
        sock.close()


osc_zoom = int(CONFIG["osc"].get("zoom_start", 0))


def osc_move(direction):
    path = {
        "left": "/OBSBOT/WebCam/General/SetGimbalLeft",
        "right": "/OBSBOT/WebCam/General/SetGimbalRight",
        "up": "/OBSBOT/WebCam/General/SetGimbalUp",
        "down": "/OBSBOT/WebCam/General/SetGimbalDown",
    }[direction]
    speed = max(1, min(100, int(CONFIG["osc"].get("move_speed", 35))))
    move_ms = max(20, min(1000, int(CONFIG["osc"].get("move_ms", 120))))
    osc_send(path, [speed])
    time.sleep(move_ms / 1000.0)
    osc_send(path, [0])


def osc_zoom_command(command):
    global osc_zoom
    step = max(1, min(50, int(CONFIG["osc"].get("zoom_step", 10))))
    if command == "zoom_in":
        osc_zoom = min(100, osc_zoom + step)
    else:
        osc_zoom = max(0, osc_zoom - step)
    osc_send("/OBSBOT/WebCam/General/SetZoom", [osc_zoom])


def execute_tiny2_command(command, dry_run=False):
    backend = str(CONFIG.get("backend", "hotkey")).lower()
    if dry_run:
        print(f"[DRY RUN] Tiny2 {backend}: {command}")
        return backend

    if backend == "hotkey":
        hotkey_command(command)
    elif backend == "osc":
        if command in {"left", "right", "up", "down"}:
            osc_move(command)
        elif command in {"zoom_in", "zoom_out"}:
            osc_zoom_command(command)
        else:
            raise ValueError("Unsupported OSC command")
    else:
        raise ValueError(f"Unknown backend: {backend}")
    return backend


# ============================================================
# 08. OBS scene hotkeys / control state mapping
#     Scene 1 = Tiny3 / control OFF
#     Scene 2 = Tiny2 / control OFF
#     Scene 3 = MEET  / control OFF
#     Scene 4 = Tiny2 + control guide / control ON
# ============================================================
def send_obs_scene_key(scene_key, source="scene", dry_run=False):
    keys = CONFIG.get("obs_scene_hotkeys", {}).get(f"scene_{scene_key}")
    if not keys:
        log_event(source, f"SCENE_{scene_key}", "obs-hotkey", "ERROR", "scene hotkey missing")
        return False

    if dry_run:
        print(f"[DRY RUN] OBS SCENE_{scene_key}: {keys}")
        return True

    try:
        send_hotkey(keys)
        log_event(source, f"SCENE_{scene_key}", "obs-hotkey", "OK", "+".join(keys))
        print(f"[OBS] SCENE_{scene_key} via {'+'.join(keys)}")
        return True
    except Exception as error:
        log_event(source, f"SCENE_{scene_key}", "obs-hotkey", "ERROR", repr(error))
        print("[OBS ERROR]", error)
        return False


# ============================================================
# 09. OBS controller-style display window (Meiryo / exact lowercase commands)
# ============================================================
class ObsPanel:
    def __init__(self):
        self.root = tk.Tk()
        self.root.option_add("*Font", ("Meiryo", 10))
        self.root.withdraw()
        self.window = None

    def _button_box(self, parent, text, row, column, accent="#35c9ff", width=7):
        box = tk.Label(
            parent,
            text=text,
            fg=accent,
            bg="#151821",
            font=("Meiryo", 24, "bold"),
            width=width,
            height=2,
            relief="ridge",
            bd=3,
        )
        box.grid(row=row, column=column, padx=7, pady=7, sticky="nsew")
        return box

    def show(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.deiconify()
            self.window.lift()
            return

        self.window = tk.Toplevel(self.root)
        self.window.title("Tiny2 Camera Control - OBS")
        self.window.configure(bg="#080a10")
        self.window.resizable(False, False)

        outer = tk.Frame(self.window, bg="#080a10", padx=22, pady=18)
        outer.pack(fill="both", expand=True)

        tk.Label(
            outer,
            text="TINY2 CAMERA CONTROL",
            fg="white",
            bg="#080a10",
            font=("Meiryo", 28, "bold"),
        ).pack()
        tk.Label(
            outer,
            text="ユーザーTiny2コントロール中",
            fg="#ff70e8",
            bg="#080a10",
            font=("Meiryo", 15, "bold"),
            pady=3,
        ).pack()
        tk.Label(
            outer,
            text="チャットで下のコマンドを送信",
            fg="#e9e9ef",
            bg="#080a10",
            font=("Meiryo", 13, "bold"),
            pady=3,
        ).pack()

        controller = tk.Frame(
            outer,
            bg="#10131b",
            highlightbackground="#d94cff",
            highlightthickness=3,
            padx=18,
            pady=14,
        )
        controller.pack(pady=(12, 6))

        move = tk.Frame(controller, bg="#10131b")
        move.grid(row=0, column=0, padx=(0, 24))
        zoom = tk.Frame(controller, bg="#10131b")
        zoom.grid(row=0, column=1, padx=(24, 0))

        self._button_box(move, "c↑", 0, 1)
        self._button_box(move, "c←", 1, 0)
        self._button_box(move, "●", 1, 1, accent="#777b86", width=7)
        self._button_box(move, "c→", 1, 2)
        self._button_box(move, "c↓", 2, 1)

        self._button_box(zoom, "c+", 0, 0, accent="#65ea8b", width=8)
        self._button_box(zoom, "c-", 2, 0, accent="#65ea8b", width=8)

        tk.Label(
            outer,
            text="※ この表示が出るシーン4でユーザーコントロールを使用します",
            fg="#c9ccd6",
            bg="#080a10",
            font=("Meiryo", 11, "bold"),
            pady=5,
        ).pack()

        # Xで閉じた場合も安全側へ倒す。
        # 操作受付をOFFにして窓を隠し、OBSをScene 1へ戻す。
        self.window.protocol(
            "WM_DELETE_WINDOW",
            lambda: emergency_stop(source="controller-window-close"),
        )

    def hide(self):
        if self.window is not None and self.window.winfo_exists():
            self.window.withdraw()

    def run(self):
        self.root.mainloop()


# ============================================================
# 10. Control state / manual scene-key synchronization
# ============================================================
def current_status():
    with state_lock:
        return {
            "version": VERSION,
            "enabled": server_enabled,
            "backend": CONFIG.get("backend", "hotkey"),
            "host": HOST,
            "port": PORT,
            "commands": COMMAND_LABELS,
            "last_scene_key": last_scene_key,
            "last_state_source": last_state_source,
            "scene_map": {
                "1": "Tiny3 / control OFF",
                "2": "Tiny2 / control OFF",
                "3": "MEET / control OFF",
                "4": "Tiny2 + guide / control ON",
            },
        }


def apply_control_state(value, source="state", scene_key=None):
    global server_enabled, last_scene_key, last_state_source
    value = bool(value)

    with state_lock:
        changed = server_enabled != value
        server_enabled = value
        if scene_key in SCENE_CONTROL_STATE:
            last_scene_key = scene_key
        last_state_source = source

    if panel is not None:
        panel.root.after(0, panel.show if value else panel.hide)

    detail = f"scene_key={scene_key or ''} changed={changed}"
    log_event(source, "ON" if value else "OFF", CONFIG.get("backend", "hotkey"), "OK", detail)
    print("[Tiny2]", "ON" if value else "OFF", f"source={source}", f"scene={scene_key}")
    return value


def set_enabled(value, source="state", switch_scene=True):
    """UI/API state change.

    Permission changes first. Then, when requested, the OBS scene hotkey is sent:
    ON -> Scene 4, OFF -> Scene 1.
    """
    value = apply_control_state(bool(value), source=source)

    if switch_scene:
        target_scene = "4" if value else "1"
        send_obs_scene_key(target_scene, source=source)
    return current_status()


def emergency_stop(source="emergency"):
    apply_control_state(False, source=source)
    send_obs_scene_key("1", source=source)
    log_event(source, "EMERGENCY_STOP", CONFIG.get("backend", "hotkey"), "OK")
    print("[Tiny2] EMERGENCY STOP COMPLETE / explicit ON or top-row 4 can restart")
    return current_status()


def handle_scene_key(scene_key, source="top-row-key"):
    if scene_key not in SCENE_CONTROL_STATE:
        return
    apply_control_state(SCENE_CONTROL_STATE[scene_key], source=source, scene_key=scene_key)


def scene_key_watcher():
    if platform.system() != "Windows":
        return

    previous = {key: False for key in SCENE_CONTROL_STATE}
    delay = max(10, min(200, int(CONFIG.get("scene_key_poll_ms", 25)))) / 1000.0

    while True:
        try:
            for key in ("1", "2", "3", "4"):
                down = is_key_down(key)
                if down and not previous[key]:
                    handle_scene_key(key, source=f"top-row-{key}")
                previous[key] = down
        except Exception as error:
            log_event("scene-key-watcher", "WATCH", "keyboard", "ERROR", repr(error))
            time.sleep(0.5)
        time.sleep(delay)


# ============================================================
# 11. Local browser test page
# ============================================================
TEST_HTML = r'''<!doctype html><html lang="ja"><meta charset="utf-8">
<title>Streamforge Tiny2 Test</title>
<style>body{font-family:Meiryo,'メイリオ',sans-serif;background:#111;color:#eee;max-width:820px;margin:30px auto;padding:0 16px}button{font-family:Meiryo,'メイリオ',sans-serif;font-size:19px;margin:5px;padding:10px 15px;border-radius:10px;border:1px solid #777;background:#222;color:#fff}.on{border-color:#42e66f}.danger{background:#b71c1c;border:2px solid #fff}pre{background:#1b1b1b;padding:12px;border-radius:10px;white-space:pre-wrap}.row{text-align:center}.small{font-size:13px;color:#bbb}</style>
<h1>Tiny2 Camera Control TEST v0.4.6</h1>
<p>Scene 1=Tiny3/OFF、2=Tiny2/OFF、3=MEET/OFF、4=Tiny2+Guide/ON。</p>
<div class="row"><button id="toggle" onclick="toggle()">Tiny2 OFF</button><button class="danger" onclick="emergency()">■ 強制停止</button></div>
<div class="row"><button onclick="scene(1)">Scene 1</button><button onclick="scene(2)">Scene 2</button><button onclick="scene(3)">Scene 3</button><button onclick="scene(4)">Scene 4</button></div>
<div class="row"><button onclick="cmd('up')">↑</button></div>
<div class="row"><button onclick="cmd('left')">←</button><button onclick="cmd('down')">↓</button><button onclick="cmd('right')">→</button></div>
<div class="row"><button onclick="cmd('zoom_in')">＋</button><button onclick="cmd('zoom_out')">－</button></div>
<pre id="out">status...</pre><p class="small">上段1/2/3/4を手で押した時もWindows側が状態を追従します。テンキーは対象外。</p>
<script>
const token='__TOKEN__'; let enabled=false; const out=document.getElementById('out');
async function req(path){try{const r=await fetch(path); const t=await r.text(); out.textContent=`${r.status} ${t}`; try{const j=JSON.parse(t); if(typeof j.enabled==='boolean')enabled=j.enabled}catch(_){} render(); return r.ok}catch(e){out.textContent=String(e);return false}}
function render(){const b=document.getElementById('toggle'); b.textContent=enabled?'Tiny2 ON':'Tiny2 OFF'; b.className=enabled?'on':''}
function toggle(){req(`/state?enabled=${enabled?0:1}&switch_scene=1&source=test-panel&token=${token}`)}
function emergency(){req(`/emergency?source=test-panel&token=${token}`)}
function scene(n){req(`/scene?key=${n}&source=test-panel&token=${token}`)}
function cmd(c){req(`/cmd?name=${encodeURIComponent(c)}&source=test-panel&token=${token}`)}
setInterval(()=>req('/status?token='+token),700); req('/status?token='+token);
</script></html>'''.replace("__TOKEN__", AUTH_TOKEN)


# ============================================================
# 12. Localhost HTTP API
# ============================================================
class Handler(BaseHTTPRequestHandler):
    def send_text(self, code, text, content_type="text/plain; charset=utf-8"):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, code, payload):
        self.send_text(code, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")

    def params(self, parsed):
        return parse_qs(parsed.query)

    def authed(self, params):
        return params.get("token", [""])[0] == AUTH_TOKEN

    def do_GET(self):
        global last_command_time
        parsed = urlparse(self.path)
        params = self.params(parsed)

        if parsed.path == "/":
            self.send_text(200, f"Tiny2 Camera Controller {VERSION} READY\nOpen /test")
            return

        if parsed.path == "/test":
            self.send_text(200, TEST_HTML, "text/html; charset=utf-8")
            return

        if parsed.path == "/status":
            if not self.authed(params):
                self.send_text(403, "FORBIDDEN")
                return
            self.send_json(200, current_status())
            return

        if parsed.path == "/state":
            if not self.authed(params):
                self.send_text(403, "FORBIDDEN")
                return
            value = params.get("enabled", ["0"])[0] == "1"
            switch_scene = params.get("switch_scene", ["1"])[0] != "0"
            source = params.get("source", ["unknown"])[0][:80]
            self.send_json(200, set_enabled(value, source=source, switch_scene=switch_scene))
            return

        if parsed.path == "/emergency":
            if not self.authed(params):
                self.send_text(403, "FORBIDDEN")
                return
            source = params.get("source", ["unknown"])[0][:80]
            self.send_json(200, emergency_stop(source=source))
            return

        if parsed.path == "/scene":
            if not self.authed(params):
                self.send_text(403, "FORBIDDEN")
                return
            scene_key = params.get("key", [""])[0]
            source = params.get("source", ["unknown"])[0][:80]
            if scene_key not in SCENE_CONTROL_STATE:
                self.send_text(400, "BAD SCENE KEY")
                return
            # Permission state is applied immediately; OBS receives the same top-row key.
            handle_scene_key(scene_key, source=f"{source}-scene-{scene_key}")
            send_obs_scene_key(scene_key, source=source)
            self.send_json(200, current_status())
            return

        if parsed.path != "/cmd":
            self.send_text(404, "NOT FOUND")
            return

        if not self.authed(params):
            self.send_text(403, "FORBIDDEN")
            return

        command = params.get("name", [""])[0]
        source = params.get("source", ["unknown"])[0][:80]
        message_id = params.get("message_id", [""])[0][:80]
        backend = str(CONFIG.get("backend", "hotkey"))

        if command not in ALLOWED_COMMANDS:
            log_event(source, command, backend, "BLOCK", "bad command")
            self.send_text(400, "BAD COMMAND")
            return

        if not current_status()["enabled"]:
            log_event(source, command, backend, "BLOCK", f"control OFF message_id={message_id}")
            self.send_text(423, "TINY2 CONTROL OFF")
            return

        with command_lock:
            now = time.monotonic()
            cooldown = max(0, int(CONFIG.get("server_cooldown_ms", 700))) / 1000.0
            if now - last_command_time < cooldown:
                log_event(source, command, backend, "COOLDOWN", f"message_id={message_id}")
                self.send_text(429, "COOLDOWN")
                return

            last_command_time = now
            try:
                used_backend = execute_tiny2_command(command)
                log_event(source, command, used_backend, "OK", f"message_id={message_id}")
                print(f"[Tiny2] {command} ({COMMAND_LABELS[command]}) via {used_backend}")
                self.send_text(200, "OK")
            except Exception as error:
                log_event(source, command, backend, "ERROR", repr(error))
                print("[ERROR]", error)
                self.send_text(500, f"ERROR: {error}")

    def log_message(self, format, *args):
        return


class LocalOnlyHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def run_server():
    server = LocalOnlyHTTPServer((HOST, PORT), Handler)
    print("========================================")
    print(" Streamforge Tiny2 Camera Control TEST")
    print("========================================")
    print("Version :", VERSION)
    print("Backend :", CONFIG.get("backend", "hotkey"))
    print("HTTP    :", f"http://{HOST}:{PORT}/test")
    print("State   : OFF (safe default)")
    print("Scene 1 : Tiny3 / control OFF")
    print("Scene 2 : Tiny2 / control OFF")
    print("Scene 3 : MEET / control OFF")
    print("Scene 4 : Tiny2 + guide / control ON")
    print("Commands: c← c→ c↑ c↓ c+ c-")
    print("Log     :", LOG_PATH)
    print("========================================")
    server.serve_forever()


# ============================================================
# 13. Self-test / entry point
# ============================================================
def dry_run_tests():
    print("[SELFTEST] config:", json.dumps(CONFIG, ensure_ascii=False))
    for command in sorted(ALLOWED_COMMANDS):
        execute_tiny2_command(command, dry_run=True)

    for scene_key in ("1", "2", "3", "4"):
        send_obs_scene_key(scene_key, source="selftest", dry_run=True)

    assert SCENE_CONTROL_STATE == {"1": False, "2": False, "3": False, "4": True}
    assert CONFIG["obs_scene_hotkeys"]["scene_1"] == ["1"]
    assert CONFIG["obs_scene_hotkeys"]["scene_2"] == ["2"]
    assert CONFIG["obs_scene_hotkeys"]["scene_3"] == ["3"]
    assert CONFIG["obs_scene_hotkeys"]["scene_4"] == ["4"]
    assert VK["1"] == 0x31 and VK["2"] == 0x32 and VK["3"] == 0x33 and VK["4"] == 0x34

    packet = osc_packet("/OBSBOT/WebCam/General/SetGimbalLeft", [0, 35])
    assert len(packet) % 4 == 0
    print("[SELFTEST] scene map / top-row keys OK")
    print("[SELFTEST] OSC packet alignment OK")
    print("[SELFTEST] PASS")


def main():
    global panel
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true", help="No camera/OBS movement; static dry-run only")
    args = parser.parse_args()

    if args.selftest:
        dry_run_tests()
        return

    if platform.system() != "Windows" and str(CONFIG.get("backend", "hotkey")).lower() == "hotkey":
        print("[WARN] hotkey backend is for Windows. Use --selftest here.")

    if not acquire_single_instance():
        print("[INFO] Streamforge Tiny2 controller is already running. Second instance will exit.")
        return

    panel = ObsPanel()
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=scene_key_watcher, daemon=True).start()
    panel.run()


if __name__ == "__main__":
    main()
