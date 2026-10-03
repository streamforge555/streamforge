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
