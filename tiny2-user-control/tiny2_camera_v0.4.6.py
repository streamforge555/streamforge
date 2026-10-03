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
