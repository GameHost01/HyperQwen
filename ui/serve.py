#!/usr/bin/env python3
"""
HyperQwen Settings UI — a tiny, zero-dependency local web server.

It lets you VIEW and EDIT the .env file (the one place HyperQwen keeps its
settings) from a normal web browser, plus a few safe one-click actions
(keygen, health check, docker compose start/stop/logs), and a VRAM
calculator that predicts GPU memory usage from actual model weights.

Design goals:
  * No third-party packages — only the Python standard library.
  * Bind to 127.0.0.1 only (your machine), never the network.
  * Never print or return secret values in full (key shown as  first4… + length).
  * Every save writes a timestamped backup  .env.bak-<YYYYmmdd-HHMMSS>  first.
  * Writes preserve your existing comments and any keys we don't know about.

Run:   python ui\\serve.py            (then a browser opens)
       python ui\\serve.py --no-browser --port 8790
Stop:  Ctrl-C in this window.
"""

import argparse
import http.server
import json
import os
import re
import secrets
import shutil
import socketserver
import struct
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime

# ---------------------------------------------------------------- paths
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # .../HyperQwen
ENV_PATH = os.path.join(REPO, ".env")
UI_DIR = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(UI_DIR, "schema.json")
INDEX_PATH = os.path.join(UI_DIR, "index.html")
APPJS_PATH = os.path.join(UI_DIR, "app.js")
COMPOSE_PATH = os.path.join(REPO, "docker-compose.yml")
MODELS_DIR = os.path.join(REPO, "models")

DEFAULT_PORT = 8790          # the settings UI's own port (not the model's 18020)
SECRET_KEYS = {"VLLM_API_KEY", "HF_TOKEN"}

# ---------------------------------------------------------------- helpers
def read_env_text():
    try:
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""

def parse_env(text):
    """Active KEY=VALUE pairs, in file order. Comments/blank/unknown lines ignored."""
    vals = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        k = k.strip()
        if k:
            vals[k] = v.strip()
    return vals

def mask(secret):
    """first 4 chars + length, never the full value."""
    if not secret:
        return ""
    return f"{secret[:4]}…({len(secret)} chars)"

def apply_env(text, targets):
    """
    Apply {KEY: value} to the .env text. value=="" means 'turn off'.
    Preserves comments and unknown keys. Idempotent.
      value set  -> edit the active KEY= line, else un-comment the '# KEY='
                    template line, else append KEY=value
      value ""   -> comment out the active line as '# KEY=' (keeps the doc line)
    """
    lines = text.splitlines()
    active, tpl = {}, {}
    for i, line in enumerate(lines):
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        t = re.match(r"^#\s*([A-Za-z_][A-Za-z0-9_]*)=", line)
        if m:
            active.setdefault(m.group(1), i)
        elif t:
            tpl.setdefault(t.group(1), i)

    out = list(lines)
    for k, v in targets.items():
        if v != "":
            if k in active:
                out[active[k]] = f"{k}={v}"
            elif k in tpl:
                out[tpl[k]] = f"{k}={v}"
            else:
                out.append(f"{k}={v}")
        else:  # disable
            if k in active:
                out[active[k]] = f"# {k}="
    return "\n".join(out) + "\n"

BACKUP_DIR = os.path.join(REPO, ".env-backups")

def make_backup():
    if not os.path.exists(ENV_PATH):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = os.path.join(BACKUP_DIR, f".env.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    dest, n = base, 1
    while os.path.exists(dest):   # two saves in the same second
        dest, n = f"{base}-{n}", n + 1
    shutil.copy2(ENV_PATH, dest)
    return os.path.basename(dest)

def list_backups():
    if not os.path.isdir(BACKUP_DIR):
        return []
    baks = []
    for name in os.listdir(BACKUP_DIR):
        if name.startswith(".env.bak-"):
            p = os.path.join(BACKUP_DIR, name)
            baks.append({"name": name,
                         "size": os.path.getsize(p),
                         "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M:%S")})
    baks.sort(key=lambda b: b["name"], reverse=True)
    return baks

def validate(values, schema):
    """Fail-closed: reject enum values not in the allowed set. Returns list of errors."""
    errs = []
    opts = {}
    for g in schema.get("groups", []):
        for f in g.get("fields", []):
            if f.get("type") == "enum":
                opts[f["key"]] = f.get("options", [])
    for k, v in values.items():
        if k in opts and v != "" and v not in opts[k]:
            errs.append(f"{k}={v!r} is not allowed (choose from {opts[k]})")
    for k, v in values.items():
        if k in ("PORT", "MAX_LEN", "MAX_SEQS") and v != "":
            try:
                int(v)
            except ValueError:
                errs.append(f"{k}={v!r} is not a number")
    return errs

# ---------------------------------------------------------------- VRAM calculator
GPU_TOTAL_MIB = 24576  # RTX 3090, 24 GB

# Fallback weights in MiB (from safetensors header scan, 2026-10-03)
_FB = {
    "base":  {"total": 15968.8, "vision": 878.8},
    "fast":  {"total": 15060.0, "vision": 878.8},
    "dflash2": 1221.3,
}
_TOKENIZER_MIB = 19.1
_SYS_OVR_MIB = 410.0  # CUDA context, driver, torch runtime

# KV_MEM pins from launchers (bytes)
KV_PIN = {
    ("single", "fast"):  5_583_457_484,
    ("single", "long"):  5_583_457_484,
    ("single", "huge"):  5_261_334_938,
}

# KV bytes per token = 16 full-attn layers x 4 KV heads x 256 dim x 2 (K+V) x dtype bytes
KV_BPT = {
    "bf16":    65536,
    "fp8":     32768,
    "kvarn":   16384,
    "int4pth": 16384,
}
PREFIX_KV_FRAC = 0.15  # prefix cache reservation ≈ 15% of the KV pool

_SAFETENSORS_DT = {
    "F64": 8, "F32": 4, "F16": 2, "BF16": 2,
    "I64": 8, "I32": 4, "I16": 2, "I8": 1, "U8": 1, "BOOL": 1,
}
_scan_cache = {}

def _scan_st(model_dir):
    """Read safetensors headers → total bytes. Cached per directory."""
    if model_dir in _scan_cache:
        return _scan_cache[model_dir]
    total = 0
    try:
        for f in os.listdir(model_dir):
            if not f.endswith(".safetensors") or ".bak" in f:
                continue
            p = os.path.join(model_dir, f)
            with open(p, "rb") as fh:
                n = struct.unpack("<Q", fh.read(8))[0]
                hdr = json.loads(fh.read(n))
            for k, v in hdr.items():
                if k == "__metadata__":
                    continue
                sz = _SAFETENSORS_DT.get(v["dtype"], 0)
                for s in v["shape"]:
                    sz *= s
                total += sz
    except Exception:
        total = -1
    _scan_cache[model_dir] = total
    return total

def _w_mib(variant):
    """Model weights in MiB. Scans actual files; falls back to known values."""
    dirs = {
        "base": os.path.join(MODELS_DIR, "Qwen3.8-27B-W4A16-AutoRound"),
        "fast": os.path.join(MODELS_DIR, "Qwen3.8-27B-W4A16-AutoRound-fast"),
    }
    d = dirs.get(variant)
    if d and os.path.isdir(d):
        b = _scan_st(d)
        if b > 0:
            return b / (1024 * 1024)
    return _FB[variant]["total"]

def _dflash2_mib():
    d = os.path.join(MODELS_DIR, "Qwen3.8-27B-DFlash2-W4A16")
    if os.path.isdir(d):
        b = _scan_st(d)
        if b > 0:
            return b / (1024 * 1024)
    return _FB["dflash2"]

def _rec_mib(spec, k):
    """Recurrent (GDN/Mamba) state pool in MiB. Scales with draft/verify tokens."""
    if spec == "mtp":
        return max(100.0, k * 112.75)  # k=3→338, k=4→451, k=5→564
    if spec == "dflash2":
        return max(196.6, (k + 2) * 98.3)  # k=7→884.7, k=15→1769.4
    return 0.0

def _cg_mib(spec, k, mode):
    """CUDA graph memory in MiB. Scales with verify/draft tokens."""
    if spec == "dflash2":
        return 1485.0 + max(0, k - 7) * 45.0  # k=7→1485, k=15→1845
    if spec == "mtp":
        return 1024.0
    if mode == "batch":
        return 1229.0
    return 512.0

def _act_mib(mode):
    """Activation / workspace buffers in MiB."""
    return 1229.0 if mode == "batch" else 819.0

def _kv_mib_single(ctx, kv_mem_bytes):
    """KV cache pool in MiB for single-user mode. Uses KV_MEM override if set."""
    if kv_mem_bytes > 0:
        return kv_mem_bytes / (1024 * 1024)
    b = KV_PIN.get(("single", ctx), 5_583_457_484)
    return b / (1024 * 1024)

def _kv_mib_batch(kv, gpu_util):
    """KV cache in batch mode is auto-sized: usable − fixed costs.
    We return 0 here and let the caller compute it as the remainder."""
    return 0.0

def compute_vram(p):
    """Compute the VRAM breakdown from full settings. Returns a dict for the API response."""
    variant   = p.get("variant", "fast")
    spec      = p.get("spec", "mtp")
    if spec in ("off", "none", ""):
        spec = "off"
    ctx       = p.get("ctx", "fast")
    vision    = p.get("vision", "0") == "1"
    vis_off   = p.get("vision_offload", "0") == "1"
    dflash_k  = max(1, int(p.get("dflash_tokens", 7) or 7))
    draft_k   = max(1, int(p.get("draft_tokens", 4) or 4))
    mode      = p.get("mode", "single")
    kv        = p.get("kv", "fp8")
    gpu_util  = float(p.get("gpu_util", 0) or 0) or (0.93 if mode == "single" else 0.95)
    prefix    = p.get("prefix_cache", "0") == "1"
    max_len   = int(p.get("max_len", 0) or 0)
    kv_mem_b  = int(p.get("kv_mem", 0) or 0)
    max_seqs  = int(p.get("max_seqs", 0) or 0)
    gpu_count = max(1, int(p.get("gpu_count", 1) or 1))
    div       = float(gpu_count)
    spec_attn   = p.get("spec_attn", "1") == "1"
    lookup      = p.get("lookup", "0") == "1"
    mtp_vocab   = p.get("mtp_draft_vocab", "0") == "1"
    int8_act    = (p.get("int8_act", "") or "").strip()
    prefill_att = (p.get("prefill_attn", "") or "").strip()
    extra_args  = (p.get("extra_args", "") or "").strip()
    os_vram     = float(p.get("os_vram", 512) or 512)

    comps = []

    tps = f" / {gpu_count} GPUs" if gpu_count > 1 else ""

    # 1 — model weights (scan total includes vision; subtract if off or offloaded)
    w = _w_mib(variant) / div
    vis_mib = _FB[variant]["vision"] / div
    if not vision or vis_off:
        w -= vis_mib
    comps.append(("Model weights (" + variant + ")" + tps, round(w, 1), "#4f8cff"))

    # 1b — vision tower (only on GPU if vision=on AND not offloaded)
    if vision and not vis_off:
        comps.append(("Vision tower" + tps, round(vis_mib, 1), "#8b5cf6"))

    # 2 — DFlash2 drafter
    if spec == "dflash2":
        comps.append(("DFlash2 drafter" + tps, round(_dflash2_mib() / div, 1), "#38c172"))

    # 3 — tokenizer
    comps.append(("Tokenizer", _TOKENIZER_MIB, "#9aa3b2"))

    # 4 — KV cache pool (prefix cache lives INSIDE the pool, so split the line)
    kv_total = None  # pool size across ALL GPUs (MiB); batch mode = remainder
    if mode == "single":
        kv_total = _kv_mib_single(ctx, kv_mem_b)
        kv_mib = kv_total / div
        pm = PREFIX_KV_FRAC * kv_mib if prefix else 0.0
        kv_label = "KV cache pool" + (" (custom)" if kv_mem_b > 0 else " (pinned)") + tps
        comps.append((kv_label, round(kv_mib - pm, 1), "#e5534b"))
        if pm > 0:
            comps.append(("Prefix cache (KV reserved)", round(pm, 1), "#f59e0b"))

    # 5 — recurrent state (GDN/Mamba)
    eff_k = dflash_k if spec == "dflash2" else draft_k
    rec = _rec_mib(spec, eff_k) / div
    if rec > 0:
        label = "Recurrent state"
        if spec == "dflash2":
            label += f" (k={dflash_k})"
        elif spec == "mtp":
            label += f" (MTP k={draft_k})"
        comps.append((label, round(rec, 1), "#a855f7"))

    # 6 — CUDA graphs
    cg = _cg_mib(spec, eff_k, mode)
    comps.append(("CUDA graphs", round(cg, 1), "#06b6d4"))

    # 7 — activations / workspace
    act = _act_mib(mode) / div
    if mode == "batch" and max_seqs > 0:
        act = 819.0 * max(0.25, max_seqs / 64.0) / div  # scale by seq count (64 = default)
    comps.append(("Activations / workspace" + tps, round(act, 1), "#f97316"))

    # 8 — system overhead (CUDA context, driver, torch runtime)
    comps.append(("System overhead", _SYS_OVR_MIB, "#6b7280"))

    # 9 — OS / display (Windows DWM, display buffers, WSL2 compositor)
    if os_vram > 0:
        comps.append(("OS / Display (Windows)", round(os_vram, 1), "#94a3b8"))

    # totals
    used = sum(c[1] for c in comps)
    usable = GPU_TOTAL_MIB * gpu_util

    if mode == "batch":
        kv_total = max(0.0, usable - used)
        pm = PREFIX_KV_FRAC * kv_total if prefix else 0.0
        comps.append(("KV cache pool (auto)", round(kv_total - pm, 1), "#e5534b"))
        if pm > 0:
            comps.append(("Prefix cache (KV reserved)", round(pm, 1), "#f59e0b"))
        used += kv_total

    # KV token capacity (all GPUs combined) vs MAX_LEN
    # cap_total  = what the pool holds (server-start check)
    # cap_tokens = what remains after prefix-cache reservation (runtime check)
    bpt = KV_BPT.get(kv, KV_BPT["fp8"])
    cap_total = 0
    if kv_total and kv_total > 0:
        cap_total = int(kv_total * 1024 * 1024 / bpt)
        cap_tokens = int(cap_total * (1 - PREFIX_KV_FRAC)) if prefix else cap_total
    else:
        cap_tokens = 0
    startup_ok = (max_len <= cap_total) if (max_len > 0 and cap_total > 0) else True
    max_len_ok = (max_len <= cap_tokens) if (max_len > 0 and cap_tokens > 0) else True

    headroom = usable - used
    fits = headroom >= 0 and startup_ok

    if headroom > 0:
        comps.append(("Headroom", round(headroom, 1), "#2a2f3a"))

    # determine effective max_len for display
    if max_len > 0:
        eff_max_len = max_len
    elif mode == "single":
        if spec == "dflash2":
            if ctx == "fast":
                eff_max_len = 57344 if dflash_k > 7 else 65536
            elif ctx == "long":
                eff_max_len = 131072
            else:
                eff_max_len = 221184 if dflash_k > 7 else 245760
        else:
            eff_max_len = {"fast": 65536, "long": 150000, "huge": 200000}.get(ctx, 65536)
    else:
        eff_max_len = {"fp8": 150000, "kvarn": 262144, "int4pth": 262144}.get(kv, 150000)

    notes = []
    if extra_args:
        notes.append("EXTRA_ARGS is set — raw vLLM flags can change VRAM (not modeled).")
    if int8_act:
        notes.append("INT8_ACT on — int8 GEMMs slightly reduce activation memory (not modeled).")
    if prefill_att:
        notes.append("PREFILL_ATTN on — small activation change (not modeled).")
    if mtp_vocab and spec == "mtp":
        notes.append("MTP_DRAFT_VOCAB=1 — 40k draft vocab slightly changes MTP head size (not modeled).")
    if lookup and spec == "dflash2":
        notes.append("LOOKUP=1 — prompt lookup table, negligible VRAM.")
    if spec_attn:
        notes.append("SPEC_ATTN=1 — split-KV verify attention, no separate VRAM pool modeled.")

    # sort by size descending, keep Headroom last
    headroom_entry = None
    if comps and comps[-1][0] == "Headroom":
        headroom_entry = comps.pop()
    comps.sort(key=lambda c: c[1], reverse=True)
    if headroom_entry:
        comps.append(headroom_entry)

    return {
        "ok": True,
        "gpu_total_mib": GPU_TOTAL_MIB,
        "gpu_util": gpu_util,
        "gpu_count": gpu_count,
        "gpu_usable_mib": round(usable, 1),
        "components": [
            {"name": n, "mib": m, "gib": round(m / 1024, 3), "color": c,
             "pct": round(m / usable * 100, 1) if usable else 0}
            for n, m, c in comps
        ],
        "total_used_mib": round(used, 1),
        "total_used_gib": round(used / 1024, 3),
        "headroom_mib": round(headroom, 1),
        "headroom_gib": round(headroom / 1024, 3),
        "headroom_pct": round(headroom / usable * 100, 1) if usable else 0,
        "fits": fits,
        "max_len": eff_max_len,
        "max_len_ok": max_len_ok,
        "kv_capacity_tokens": cap_tokens,
        "notes": notes,
        "config": {
            "variant": variant, "spec": spec, "ctx": ctx,
            "vision": vision, "vision_offload": vis_off,
            "dflash_tokens": dflash_k, "draft_tokens": draft_k,
            "mode": mode, "kv": kv, "gpu_util": gpu_util,
            "prefix_cache": prefix, "max_len": eff_max_len,
            "kv_mem": kv_mem_b, "max_seqs": max_seqs,
        },
    }

# ---------------------------------------------------------------- actions
def probe_health(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            return {"up": r.status == 200, "http": r.status}
    except Exception as e:
        return {"up": False, "error": type(e).__name__}

def docker_available():
    try:
        subprocess.run(["docker", "version"], capture_output=True, timeout=10)
        return True
    except Exception:
        return False

ACTION_ARGS = {
    "ps":         ["docker", "compose", "ps"],
    "logs":       ["docker", "compose", "logs", "--tail", "80"],
    "up-single":  ["docker", "compose", "--profile", "single", "up", "-d"],
    "up-batch":   ["docker", "compose", "--profile", "batch", "up", "-d"],
    "down":       ["docker", "compose", "down"],
}
DESTRUCTIVE = {"down"}

def run_action(name):
    if name not in ACTION_ARGS:
        return {"ok": False, "error": f"unknown action {name!r}"}
    try:
        r = subprocess.run(ACTION_ARGS[name], cwd=REPO, capture_output=True,
                           text=True, timeout=180)
        out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        return {"ok": r.returncode == 0, "code": r.returncode,
                "out": out[-8000:], "action": name}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out (180s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ---------------------------------------------------------------- http
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "HyperQwenUI/1.0"

    def log_message(self, *a):
        pass  # quiet

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        raw = self.path
        p = raw.split("?", 1)[0]
        qs = {}
        if "?" in raw:
            qs = dict(urllib.parse.parse_qs(raw.split("?", 1)[1]))
            # unwrap single-value lists
            qs = {k: v[0] for k, v in qs.items()}

        if p in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, f"missing index.html: {e}", "text/plain")
        elif p == "/app.js":
            try:
                with open(APPJS_PATH, "rb") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except Exception as e:
                self._send(500, f"missing app.js: {e}", "text/plain")
        elif p == "/api/settings":
            text = read_env_text()
            values = parse_env(text)
            # mask secrets in the returned view
            shown = dict(values)
            for s in SECRET_KEYS:
                if s in shown:
                    shown[s] = "__KEEP__"
                    shown[s + "_meta"] = mask(values[s])
            schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
            self._json({
                "ok": True,
                "schema": schema,
                "values": shown,
                "port": values.get("PORT", "18020"),
                "backups": list_backups(),
                "envExists": os.path.exists(ENV_PATH),
            })
        elif p == "/api/vram":
            # Build params from query string, falling back to .env values
            env = parse_env(read_env_text())
            fv = env.get("FAST_VARIANT", "1")
            params = {
                "variant":        qs.get("variant", "fast" if fv == "1" else "base"),
                "spec":           qs.get("spec", env.get("SPEC", "mtp")),
                "ctx":            qs.get("ctx", env.get("CTX", "fast")),
                "vision":         qs.get("vision", env.get("VISION", "0")),
                "vision_offload": qs.get("vision_offload", env.get("VISION_OFFLOAD", "0")),
                "dflash_tokens":  qs.get("dflash_tokens", env.get("DFLASH_TOKENS", "7")),
                "draft_tokens":   qs.get("draft_tokens", env.get("DRAFT_TOKENS", "4")),
                "mode":           qs.get("mode", "single"),
                "kv":             qs.get("kv", env.get("KV", "fp8")),
                "gpu_util":       qs.get("gpu_util", env.get("GPU_UTIL", "")),
                "prefix_cache":   qs.get("prefix_cache", env.get("PREFIX_CACHE", "0")),
                "max_len":        qs.get("max_len", env.get("MAX_LEN", "")),
                "kv_mem":         qs.get("kv_mem", env.get("KV_MEM", "")),
                "max_seqs":       qs.get("max_seqs", env.get("MAX_SEQS", "")),
                "gpu_count":      qs.get("gpu_count", env.get("GPU_COUNT", "1")),
                "spec_attn":      qs.get("spec_attn", env.get("SPEC_ATTN", "1")),
                "lookup":         qs.get("lookup", env.get("LOOKUP", "0")),
                "mtp_draft_vocab": qs.get("mtp_draft_vocab", env.get("MTP_DRAFT_VOCAB", "0")),
                "int8_act":       qs.get("int8_act", env.get("INT8_ACT", "")),
                "prefill_attn":   qs.get("prefill_attn", env.get("PREFILL_ATTN", "")),
                "extra_args":     qs.get("extra_args", env.get("EXTRA_ARGS", "")),
                "os_vram":        qs.get("os_vram", env.get("OS_VRAM", "512")),
            }
            try:
                self._json(compute_vram(params))
            except Exception as e:
                self._json({"ok": False, "error": f"vram calc: {type(e).__name__}: {e}"}, 500)
        elif p == "/api/status":
            text = read_env_text()
            values = parse_env(text)
            port = values.get("PORT", "18020")
            self._json({"ok": True, "port": port,
                        "health": probe_health(port),
                        "docker": docker_available(),
                        "mode": "unknown"})
        elif p == "/api/backups":
            self._json({"ok": True, "backups": list_backups()})
        elif p == "/api/raw":
            text = read_env_text()
            active = parse_env(text)
            for s in SECRET_KEYS:
                if s in active:
                    text = re.sub(r"^(%s)=.*$" % s, s + "=<hidden>", text, flags=re.M)
            self._json({"ok": True, "text": text})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        p = self.path.split("?", 1)[0]
        try:
            payload = json.loads(self._body().decode("utf-8") or "{}")
        except Exception:
            return self._json({"ok": False, "error": "bad JSON body"}, 400)

        try:
            self._post(p, payload)
        except Exception as e:
            self._json({"ok": False, "error": f"server error: {type(e).__name__}: {e}"}, 500)

    def _post(self, p, payload):
        if p == "/api/settings":
            values_in = payload.get("values", {})
            schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
            current = parse_env(read_env_text())
            # resolve __KEEP__ sentinels for secrets
            targets = {}
            for k, v in values_in.items():
                if v == "__KEEP__" and k in SECRET_KEYS:
                    v = current.get(k, "")
                targets[k] = v
            errs = validate(targets, schema)
            if errs:
                return self._json({"ok": False, "error": "; ".join(errs)}, 400)
            text = read_env_text()
            bak = make_backup()
            new_text = apply_env(text, targets)
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write(new_text)
            self._json({"ok": True, "backup": bak,
                        "note": "Saved. Restart the server to apply."})
        elif p == "/api/keygen":
            current = parse_env(read_env_text())
            key = secrets.token_hex(24)  # 48 hex chars, like `openssl rand -hex 24`
            bak = make_backup()
            new_text = apply_env(read_env_text(), {"VLLM_API_KEY": key})
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write(new_text)
            self._json({"ok": True, "backup": bak, "keyMeta": mask(key),
                        "note": "New API key generated and saved to .env."})
        elif p == "/api/restore":
            name = payload.get("name", "")
            if not name.startswith(".env.bak-") or "/" in name or "\\" in name:
                return self._json({"ok": False, "error": "bad backup name"}, 400)
            src = os.path.join(BACKUP_DIR, name)
            if not os.path.exists(src):
                return self._json({"ok": False, "error": "no such backup"}, 404)
            make_backup()
            shutil.copy2(src, ENV_PATH)
            self._json({"ok": True, "restored": name})
        elif p == "/api/action":
            name = payload.get("action", "")
            if name in DESTRUCTIVE and not payload.get("confirm"):
                return self._json({"ok": False, "error": "needs confirm:true"}, 400)
            self._json(run_action(name))
        else:
            self._json({"ok": False, "error": "not found"}, 404)


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    srv = ThreadingServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print("=" * 56)
    print("  HyperQwen Settings UI")
    print(f"  Open:  {url}")
    print(f"  Editing: {ENV_PATH}")
    print("  Stop:   press Ctrl-C in this window")
    print("=" * 56)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
