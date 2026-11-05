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
import time
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timezone
import math


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

# ---------------------------------------------------------------- custom presets
# User-made presets: a named snapshot of the known .env fields (secrets
# excluded), stored in ui/.custom-presets.json — never in .env itself.

CUSTOM_PRESETS_PATH = os.path.join(UI_DIR, ".custom-presets.json")
_presets_lock = threading.Lock()

def _custom_presets_read():
    try:
        with open(CUSTOM_PRESETS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("presets"), list):
            return data["presets"]
    except Exception:
        pass
    return []

def _custom_presets_write(presets):
    with open(CUSTOM_PRESETS_PATH, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "presets": presets}, f, indent=1)

def list_custom_presets():
    """Custom presets, sanitized for the UI (secrets are never stored)."""
    with _presets_lock:
        presets = _custom_presets_read()
    out = []
    for p in presets:
        if not isinstance(p, dict) or not (p.get("name") or "").strip() or not p.get("id"):
            continue
        sets = p.get("sets")
        if not isinstance(sets, dict):
            continue
        out.append({"id": str(p["id"]),
                    "name": p["name"],
                    "sets": {str(k): ("" if v is None else str(v)) for k, v in sets.items()},
                    "created": str(p.get("created") or ""),
                    "updated": str(p.get("updated") or "")})
    out.sort(key=lambda x: x["created"], reverse=True)
    return out

def create_custom_preset(name, sets, overwrite=False):
    """Validate and store a snapshot of the current settings. (preset, error).

    A name clash (case-insensitive) is an error unless overwrite is set; with
    overwrite the existing preset is updated in place — same id, its original
    name casing kept, new sets, and a fresh 'updated' stamp."""
    name = (name or "").strip()
    if not name:
        return None, "give the preset a name"
    if len(name) > 60:
        return None, "preset name too long (max 60 characters)"
    schema = json.load(open(SCHEMA_PATH, encoding="utf-8"))
    known = {f["key"] for g in schema.get("groups", []) for f in g.get("fields", [])}
    clean = {}
    for k, v in (sets or {}).items():
        k = str(k)
        if k not in known or k in SECRET_KEYS:
            continue  # unknown knobs and secrets are never stored in a preset
        clean[k] = "" if v is None else str(v)
    errs = validate(clean, schema)
    if errs:
        return None, "current settings don't pass validation: " + "; ".join(errs)
    with _presets_lock:
        presets = _custom_presets_read()
        for i, p in enumerate(presets):
            if (p.get("name") or "").strip().lower() != name.lower():
                continue
            if not overwrite:
                return None, "a preset named '%s' already exists — pick another name" % name
            nid = p.get("id") or ("c" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2))
            presets[i] = {"id": nid,
                          "name": (p.get("name") or "").strip(),
                          "sets": clean,
                          "created": p.get("created") or "",
                          "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            _custom_presets_write(presets)
            return dict(presets[i]), None
        p = {"id": "c" + datetime.now().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2),
             "name": name,
             "sets": clean,
             "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        presets.append(p)
        _custom_presets_write(presets)
    return p, None

def delete_custom_preset(pid):
    pid = str(pid or "")
    if not pid:
        return False
    with _presets_lock:
        presets = _custom_presets_read()
        keep = [p for p in presets if (p.get("id") or "") != pid]
        if len(keep) == len(presets):
            return False
        _custom_presets_write(keep)
    return True

def validate(values, schema):
    """Fail-closed: reject enum values not in the allowed set, and any
    int/float field that declares min/max if the value is non-numeric,
    non-finite, or out of range. Returns a list of error strings."""
    errs = []
    opts = {}
    num = {}
    for g in schema.get("groups", []):
        for f in g.get("fields", []):
            t = f.get("type")
            if t == "enum":
                opts[f["key"]] = f.get("options", [])
            elif t in ("int", "float") and ("min" in f or "max" in f):
                num[f["key"]] = (f.get("min"), f.get("max"))
    for k, v in values.items():
        if k in opts and v != "" and v not in opts[k]:
            errs.append(f"{k}={v!r} is not allowed (choose from {opts[k]})")
    for k, v in values.items():
        if k in ("PORT", "MAX_LEN", "MAX_SEQS") and v != "":
            try:
                int(v)
            except ValueError:
                errs.append(f"{k}={v!r} is not a number")
    for k, v in values.items():
        if k in num and v != "":
            lo, hi = num[k]
            try:
                n = float(v)
            except ValueError:
                errs.append(f"{k}={v!r} is not a number")
                continue
            if not math.isfinite(n):
                errs.append(f"{k}={v!r} must be a finite number")
                continue
            if lo is not None and n < float(lo):
                errs.append(f"{k}={v!r} is below {lo}")
            elif hi is not None and n > float(hi):
                errs.append(f"{k}={v!r} is above {hi}")
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

# Model discovery for the settings dropdown: reads each model dir's
# safetensors headers and config.json — no torch needed.

def _scan_model_dir(model_dir):
    """Sum tensor bytes across every non-.bak *.safetensors in model_dir.

    Returns (total_bytes, visual_bytes, draft_rows, has_mtp), or None when
    the dir is missing or holds no readable safetensors file."""
    try:
        files = os.listdir(model_dir)
    except OSError:
        return None
    total = visual = 0
    draft_rows = None
    has_mtp = False
    for f in sorted(files):
        if not f.endswith(".safetensors") or ".bak" in f:
            continue
        try:
            with open(os.path.join(model_dir, f), "rb") as fh:
                n = struct.unpack("<Q", fh.read(8))[0]
                hdr = json.loads(fh.read(n))
        except (OSError, ValueError):
            continue
        for k, v in hdr.items():
            if not isinstance(v, dict) or "shape" not in v:
                continue
            sz = _SAFETENSORS_DT.get(v.get("dtype"), 0)
            for s in v.get("shape", []):
                sz *= s
            total += sz
            if "visual" in k:
                visual += sz
            if k.startswith("mtp."):
                has_mtp = True
            if k.endswith(".weight_packed") or k.endswith(".weight") or k.endswith(".bias"):
                k0 = k.rsplit(".", 1)[0]
            else:
                k0 = k
            if k0 == "mtp.draft_lm_head" and v.get("shape"):
                draft_rows = int(v["shape"][0])
    return (total, visual, draft_rows, has_mtp)


def _resolve_model_dir(model):
    """Map a MODEL value to an existing directory, or None.

    Empty mirrors single-user/select_model.sh: the fast variant when it
    exists, else the base model.  Relative values are tried repo-relative
    first (the .env convention), then verbatim."""
    m = (model or "").strip()
    if not m:
        fast = os.path.join(MODELS_DIR, "Qwen3.8-27B-W4A16-AutoRound-fast")
        base = os.path.join(MODELS_DIR, "Qwen3.8-27B-W4A16-AutoRound")
        pick = fast if os.path.isdir(fast) else base
        return pick if os.path.isdir(pick) else None
    cands = [m] if os.path.isabs(m) else [os.path.join(REPO, m), m]
    for c in cands:
        if os.path.isdir(c):
            return c
    return None


def _quant_summary(model_dir):
    """Summarize the quantization groups in the model's config.json."""
    try:
        with open(os.path.join(model_dir, "config.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    qc = cfg.get("quantization_config") or {}
    if qc.get("quant_method") != "compressed-tensors":
        return {"method": qc.get("quant_method") or ""}
    body = None
    bits = gsizes = []
    asymmetric = False
    for g in (qc.get("config_groups") or {}).values():
        w = (g or {}).get("weights") or {}
        if w.get("num_bits") is not None:
            bits.append(int(w["num_bits"]))
        if w.get("group_size") is not None:
            gsizes.append(int(w["group_size"]))
        if not w.get("symmetric", True):
            asymmetric = True
        if body is None and g.get("targets") == ["Linear"]:
            body = w
    out = {"method": "compressed-tensors"}
    if body is not None:
        out["bits"] = body.get("num_bits")
        out["group_size"] = body.get("group_size")
        out["symmetric"] = bool(body.get("symmetric", True))
    elif bits:
        out["bits"] = max(bits)
        out["group_size"] = min(gsizes) if gsizes else None
        out["symmetric"] = not asymmetric
    return out


def _list_models():
    """Models selectable in the settings UI: immediate subdirs of the models
    dir with a config.json and at least one non-.bak *.safetensors.
    Skips DFlash2 drafter dirs, tmp-* dirs, dot dirs and unreadable dirs."""
    out = []
    try:
        entries = sorted(os.listdir(MODELS_DIR))
    except OSError:
        return out
    for d in entries:
        if d.startswith(".") or d.startswith("tmp-") or "DFlash2" in d:
            continue
        p = os.path.join(MODELS_DIR, d)
        if not os.path.isdir(p):
            continue
        try:
            if not os.path.isfile(os.path.join(p, "config.json")):
                continue
            sts = [f for f in os.listdir(p)
                   if f.endswith(".safetensors") and ".bak" not in f]
            if not sts:
                continue
        except OSError:
            continue
        sc = _scan_model_dir(p)
        total, _vis, rows, has_mtp = sc if sc else (0, 0, None, False)
        out.append({
            "name": d,
            "model": "models/" + d,
            "size_mib": round(total / (1024 * 1024), 1) if total else None,
            "quant": _quant_summary(p),
            "mtp": has_mtp,
            "draft_rows": rows,
        })
    return out


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

def _cdiv(a, b):
    return -(-a // b)


def _hybrid_kv_capacity(model_dir, kv_dtype, pool_bytes, max_len, num_spec):
    """Exact vLLM 0.30.0 hybrid (GDN linear + full attention) KV capacity in
    tokens, derived from the model's config.json — no torch, no engine.

    Mirrors vllm/v1/core/kv_cache_utils.py + platforms/interface.py:
      * block      = 16 * cdiv(mamba_page, 16 * attn_bytes_per_token)
      * mamba page padded to block * attn_bytes_per_token
      * layers grouped in 17-layer buckets (min type size), groups padded
      * num_blocks = pool // (bucket * padded_page)
      * per-request = cdiv(max_len, block) [full-attn group]
                      + n_mamba_groups * (2 + num_spec)  [mamba 'align' mode]
      * capacity    = int(num_blocks / per-request * max_len)
    Returns (capacity, detail) or (None, None) when not derivable
    (config unreadable, non-hybrid model, or a kv dtype without a clean
    per-token cost) — callers then fall back to the KV_BPT table estimate.
    """
    p = os.path.join(model_dir, "config.json")
    if not os.path.isfile(p):
        return None, None
    try:
        cfg = json.load(open(p, encoding="utf-8"))
    except Exception:
        return None, None
    tc = cfg.get("text_config") or cfg
    lt = tc.get("layer_types") or []
    n_full = sum(1 for t in lt if t == "full_attention")
    n_mamba = sum(1 for t in lt if t == "linear_attention")
    if not lt or n_full == 0 or n_mamba == 0:
        return None, None
    kv_bytes = {"bf16": 2, "fp8": 1}.get(kv_dtype)
    if kv_bytes is None or max_len <= 0 or pool_bytes <= 0:
        return None, None
    h_kv = int(tc.get("num_key_value_heads") or 0)
    h_dim = int(tc.get("head_dim") or 0)
    if h_kv <= 0 or h_dim <= 0:
        return None, None
    attn1 = 2 * h_kv * h_dim * kv_bytes  # bytes/token per full-attn layer
    k_hd = int(tc.get("linear_key_head_dim") or 0)
    v_hd = int(tc.get("linear_value_head_dim") or 0)
    k_dim = int(tc.get("linear_num_key_heads") or 0) * k_hd
    v_dim = int(tc.get("linear_num_value_heads") or 0) * v_hd
    # GDN state per page: conv (2*kernel-1, 2*k+v) in model dtype (bf16 here)
    # + ssm (v_heads, v_dim, k_dim) in float16
    conv_rows = 2 * int(tc.get("linear_conv_kernel_dim") or 4) - 1
    real_page = conv_rows * (2 * k_dim + v_dim) * 2 + v_dim * k_hd * 2
    if real_page <= 0:
        return None, None
    block = 16 * _cdiv(real_page, 16 * attn1)
    page = block * attn1
    n_mtp = int(tc.get("mtp_num_hidden_layers") or 0) if num_spec > 0 else 0
    n_full += n_mtp
    mn, mx = min(n_full, n_mamba), max(n_full, n_mamba)
    gs = mx if mx < mn * 1.5 else mn

    def _pad(n):
        q = gs - n % gs
        return 0 if q == gs else q

    mg = (n_mamba + _pad(n_mamba)) // gs
    fg = (n_full + _pad(n_full)) // gs
    bpb = gs * page
    num_blocks = pool_bytes // bpb
    if num_blocks <= 0:
        return None, None
    nbr = _cdiv(max_len, block) + mg * (2 + num_spec)
    if nbr <= 0:
        return None, None
    cap = int(num_blocks / nbr * max_len)
    detail = {
        "n_full": n_full, "n_mtp": n_mtp, "n_mamba": n_mamba,
        "mamba_page_real": real_page, "mamba_page_padded": page,
        "mamba_pad_pct": round(100 * (page - real_page) / real_page, 2),
        "block": block, "group_size": gs, "n_groups": fg + mg,
        "num_blocks": num_blocks, "blocks_per_request": nbr,
        "concurrency": round(num_blocks / nbr, 4),
    }
    return cap, detail


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
    model       = (p.get("model", "") or "").strip()

    comps = []

    tps = f" / {gpu_count} GPUs" if gpu_count > 1 else ""

    # 1 — model weights (scan total includes vision; subtract if off or offloaded)
    # Source order: scan of the selected model dir, else the reference 'fast'
    # totals (an unknown model is never an error).
    weights_source = "fallback-fast"
    model_dir = _resolve_model_dir(model)
    vis_mib = _FB[variant]["vision"] / div
    draft_rows = None
    if model_dir is not None:
        sc = _scan_model_dir(model_dir)
        if sc and sc[0] > 0:
            weights_source = "scan"
            w = sc[0] / (1024 * 1024) / div
            if sc[1] > 0:
                vis_mib = sc[1] / (1024 * 1024) / div
            draft_rows = sc[2]
        else:
            model_dir = None
    if weights_source != "scan":
        w = _w_mib(variant) / div
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
    #
    # Hybrid models (GDN linear + full attention, e.g. Qwen3.5/3.8) have no
    # honest bytes-per-token: the recurrent state pages dominate and vLLM
    # equalizes/pads pages across layer groups, so the KV_BPT table below is
    # only an estimate. When the model's config.json is readable we replicate
    # vLLM's exact block/group math instead (verified against the live engine:
    # 137,846 tok for the 5,397,100,000-byte pin, vLLM 0.30.0, 2026-10-13).
    kv_capacity_detail = None
    if mode == "single" and kv_total and kv_total > 0 and model_dir is not None:
        pool_b = kv_mem_b if kv_mem_b > 0 else KV_PIN.get(("single", ctx))
        if pool_b and spec in ("off", "mtp"):
            n_spec = 0 if spec == "off" else draft_k
            cap_tokens, kv_capacity_detail = _hybrid_kv_capacity(
                model_dir, kv, int(pool_b), max_len, n_spec)
            if kv_capacity_detail is not None:
                cap_total = cap_tokens  # one block pool: pool capacity == runtime capacity
    if kv_capacity_detail is None:
        bpt = KV_BPT.get(kv, KV_BPT["fp8"])
        cap_total = 0
        if kv_total and kv_total > 0:
            cap_total = int(kv_total * 1024 * 1024 / bpt)
        cap_tokens = int(cap_total * (1 - PREFIX_KV_FRAC)) if prefix else cap_total
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
    if kv_capacity_detail:
        d = kv_capacity_detail
        notes.insert(0, (
            "KV capacity exact (vLLM hybrid math): %d GDN + %d full-attn layers (incl. %d MTP), %d-tok blocks, %d blocks → %.2f× at %s tok; mamba page padded %s%% to the attention page."
            % (d["n_mamba"], d["n_full"], d["n_mtp"], d["block"], d["num_blocks"],
               d["concurrency"], "{:,}".format(eff_max_len), d["mamba_pad_pct"])
        ))
    if extra_args:
        notes.append("EXTRA_ARGS is set — raw vLLM flags can change VRAM (not modeled).")
    if int8_act:
        notes.append("INT8_ACT on — int8 GEMMs slightly reduce activation memory (not modeled).")
    if prefill_att:
        notes.append("PREFILL_ATTN on — small activation change (not modeled).")
    if mtp_vocab and spec == "mtp":
        if draft_rows:
            notes.append("MTP_DRAFT_VOCAB=1 — draft head has {:,} rows (scanned from the selected model); head size impact not modeled.".format(draft_rows))
        else:
            notes.append("MTP_DRAFT_VOCAB=1 — custom draft vocab slightly changes MTP head size (not modeled).")
    if lookup and spec == "dflash2":
        notes.append("LOOKUP=1 — prompt lookup table, negligible VRAM.")
    if spec_attn:
        notes.append("SPEC_ATTN=1 — split-KV verify attention, no separate VRAM pool modeled.")
    if weights_source == "fallback-fast":
        notes.append("Weights estimated from the reference fast variant — the selected model dir could not be read.")

    # sort by size descending, keep Headroom last
    headroom_entry = None
    if comps and comps[-1][0] == "Headroom":
        headroom_entry = comps.pop()
    comps.sort(key=lambda c: c[1], reverse=True)
    if headroom_entry:
        comps.append(headroom_entry)

    return {
        "ok": True,
        "weights_source": weights_source,
        "model_dir": model_dir or "",
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
        "kv_capacity_detail": kv_capacity_detail,
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
    # down must list the profiles too: without them compose only stops the
    # non-profile service (prepare) and leaves single/batch/solo running
    "down":       ["docker", "compose", "--profile", "single", "--profile", "batch",
                  "--profile", "solo", "down"],
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

# ---------------------------------------------------------------- startup progress
#
# A time-based startup bar for the serve container (single/batch/solo).
# Instead of docker's "Container ... Starting/Started" (which only means the
# process is alive), the UI parses the container's timestamped log and tracks
# the boot as a sequence of phases, each delimited by marker lines:
#
#   06:59:31  entrypoint: first-boot stages: [1/3] prepare [2/3] verify [3/3] start
#   06:59:31  prepare: model ready at ...                     (0.2 s once prepared)
#   07:00:08  verify: OK (0 failures)                         (~37 s)
#   07:00:31  Using FLASHINFER attention backend ...          (~24 s)
#   07:00:32  Loading safetensors checkpoint shards:   0% | 0/8
#   07:02:26  ...                                           100% | 8/8   (114 s)
#   07:02:26  Loading weights took 113.96 seconds
#   07:02:30  (second pass: MTP/draft head)                    (8.3 s)
#   07:02:45  torch.compile took ...
#   07:02:45  GPU KV cache size: ...
#   07:02:48  Capturing CUDA graphs ... 100%
#   07:02:51  Started server process
#   07:02:52  Application startup complete.                  <- model loaded
#
# The bar at any moment is (elapsed in this run) / (estimated total for this
# run), where each phase's estimate is (a) its measured duration if it has
# already completed in this run, (b) a measurement from a previous boot still
# in the log tail or from a cached measurement under the same settings
# fingerprint (.bootcal.json) — this is the on-the-fly re-calibration when
# settings change, or (c) a built-in default. Weight/draft/CUDA phases
# additionally show the actual N% from the log. Every phase that completes
# during a boot replaces its estimate with the real number, so the bar
# converges on the truth for the current settings.

BOOT_PHASE_DEFAULTS = {
    "prepare": 3.0,     # 0.2 s once prepared; first boot (download) takes far longer
    "verify": 45.0,
    "engine": 25.0,
    "load": 120.0,
    "draft": 12.0,
    "compile": 12.0,
    "kv": 2.0,
    "cuda": 6.0,
    "server": 6.0,
    "ready": 3.0,
}
BOOT_PHASE_ORDER = ["prepare", "verify", "engine", "load", "draft",
                   "compile", "kv", "cuda", "server", "ready"]
PHASE_LABELS = {
    "prepare": "Preparing model",
    "verify": "Verifying",
    "engine": "Engine init",
    "load": "Loading weights",
    "draft": "Loading draft head",
    "compile": "torch.compile",
    "kv": "KV cache",
    "cuda": "CUDA graphs",
    "server": "Server start",
    "ready": "Model loaded",
}

# settings that change how long a boot takes -> fingerprint for the measurement cache
BOOT_CAL_KEYS = ["FAST_VARIANT", "SPEC", "CTX", "KV", "GPU_UTIL",
                "DFLASH_TOKENS", "DRAFT_TOKENS", "VISION", "VISION_OFFLOAD",
                "GPU_COUNT", "MAX_LEN", "INT8_ACT", "PREFILL_ATTN", "EXTRA_ARGS"]
BOOTCAL_PATH = os.path.join(UI_DIR, ".bootcal.json")
_bootcal_saved = None   # (fingerprint, signature) of the last cache write
_pct_state = {}         # container name -> {"boot": boot-marker epoch, "pct": last displayed %}

RE_LOGLINE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z")
RE_SHARDS = re.compile(r"Loading safetensors checkpoint shards:\s*(\d{1,3})%\s*\S+\s*\|\s*(\d+)/(\d+)")
RE_CUDAGRAPH = re.compile(r"Capturing (?:prefill )?CUDA graphs[^:]*:\s*(\d{1,3})%\|[^|]*\|\s*(\d+)/(\d+)")
RE_ATTN = re.compile(r"Using [A-Z0-9_]+ attention backend out of potential backends")
RE_FATAL = re.compile(
    r"UVA is not available|OutOfMemoryError|CUDA driver error|out of memory|"
    r"Traceback \(most recent call last\)|entrypoint: verify\.sh FAILED", re.I)


def _iter_log_lines(text):
    """Yield (epoch, body) per line of a `docker logs --timestamps` stream.

    The prefix is '<UTC ts> '; bodies may start with and contain CRs (tqdm
    progress updates inside one line) — left in the body for the regexes."""
    for raw in text.split("\n"):
        m = RE_LOGLINE.match(raw)
        if not m:
            continue
        g = m.groups()
        try:
            ts = datetime(int(g[0]), int(g[1]), int(g[2]), int(g[3]),
                           int(g[4]), int(g[5]), tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
        ts += int((g[6] or "0")[:6]) / 1e6
        yield ts, raw[m.end():].lstrip("\r\n \t")


def _scan_hits(events):
    """Marker hits in a (possibly partial) run. Each key -> list of epochs;
    loadPct/cudaPct -> (epoch, pct, i, n); fatal -> (epoch, body)."""
    hits = {k: [] for k in ("boot", "preReady", "verify", "attn", "load0", "loadPct",
                            "took", "compile", "kv", "cudaPct", "cuda100",
                            "server", "complete", "fatal")}
    for ts, body in events:
        if "entrypoint: first-boot stages" in body:
            hits["boot"].append(ts)
        if "prepare: model ready at" in body:
            hits["preReady"].append(ts)
        if re.search(r"verify: \d+ FAILURE", body):
            hits["verify"].append(ts)
            hits["fatal"].append((ts, body))
        elif "verify: OK" in body:
            hits["verify"].append(ts)
        if RE_ATTN.search(body):
            hits["attn"].append(ts)
        for pct, i, n in RE_SHARDS.findall(body):
            if pct == "0" and i == "0":
                hits["load0"].append(ts)
            else:
                hits["loadPct"].append((ts, int(pct), int(i), int(n)))
        if "Loading weights took" in body:
            hits["took"].append(ts)
        if "torch.compile took" in body:
            hits["compile"].append(ts)
        if "GPU KV cache size:" in body:
            hits["kv"].append(ts)
        for pct, i, n in RE_CUDAGRAPH.findall(body):
            hits["cudaPct"].append((ts, int(pct), int(i), int(n)))
            if pct == "100":
                hits["cuda100"].append(ts)
        if "Started server process" in body:
            hits["server"].append(ts)
        if "Application startup complete" in body:
            hits["complete"].append(ts)
        if RE_FATAL.search(body):
            hits["fatal"].append((ts, body))
    return hits


def _one(hits, k):
    v = hits.get(k) or []
    return v[0] if v else None


def _last_before(cands, before):
    v = [t for t in (cands or []) if before is None or t < before]
    return v[-1] if v else None


def _shard_prog(hits, which):
    """Latest shard progress of the given loading pass."""
    cands = hits.get("loadPct") or []
    if which == "draft" and len(hits.get("load0") or []) < 2:
        return None
    if which == "load" and len(hits.get("load0") or []) > 1:
        cands = [e for e in cands if e[0] < hits["load0"][1]]
    elif which == "draft":
        cands = [e for e in cands if e[0] >= hits["load0"][1]]
    if not cands:
        return None
    ts, pct, i, n = cands[-1]
    return {"kind": "shards", "pct": pct, "i": i, "n": n}


def _cuda_prog(hits, t0, t1):
    c = [e for e in (hits.get("cudaPct") or [])
         if (t0 is None or e[0] >= t0) and (t1 is None or e[0] <= t1)]
    if not c:
        return None
    ts, pct, i, n = c[-1]
    return {"kind": "steps", "pct": pct, "i": i, "n": n}


def _build_phases(hits, expect_draft=False):
    """Ordered phase timeline (t0/t1 epochs, measured durations) for one run
    window. A partial window (no boot marker, or a log whose tail no longer
    contains the early markers) still yields every phase whose two boundaries
    are both present.

    Phase boundaries are anchored on plain (non-progress) log lines wherever
    possible — tqdm progress lines can drop out of a long-lived log — with
    progress markers used as secondary anchors:

      prepare:  boot marker          -> 'prepare: model ready at'
      verify:   prepare end          -> 'verify: OK' / 'verify: N FAILURE(S)'
      engine:  verify end           -> first of attn-backend / 0% shard / first shard %
      load:     (load start)         -> first 'Loading weights took'
      draft:    1st 'took'           -> 2nd 'took'          (2nd shard pass = MTP head;
                                                             while in flight: 1st 'took' -> open)
      compile:  2nd 'took' (the 1st on single-pass boots) -> last 'torch.compile took'
      kv:       compile end          -> 'GPU KV cache size'
      cuda:     kv end               -> last 100% graph marker
      server:   cuda end (or kv end) -> 'Started server process'
      ready:    server end           -> 'Application startup complete'
    """
    ph = []

    def step(key, t0, t1, prog=None):
        # A phase is PENDING (t0 = None) until its own start marker shows up,
        # and its start marker is the phase before it CLOSING — never the phase
        # before it starting. So at most one phase is open at any moment and a
        # newly starting phase can never resize the bar's denominator.
        if t0 is None and t1 is None:
            return None
        if t0 is not None and t1 is not None and t1 < t0:
            t1 = t0
        ph.append({"key": key, "label": PHASE_LABELS[key], "t0": t0, "t1": t1,
                  "dur": round(t1 - t0, 2) if (t0 is not None and t1 is not None) else None,
                  "progress": prog})
        return t1

    t_boot = _one(hits, "boot")
    t_prep = _one(hits, "preReady")
    t_vfy = _one(hits, "verify")
    step("prepare", t_boot, t_prep)
    step("verify", t_prep, t_vfy)

    # Weight-load start: the attention-backend line (a plain, surviving line)
    # precedes the first 0% shard line by ~1 s; use whichever is available.
    load_start = _one(hits, "attn")
    if load_start is None and hits.get("load0"):
        load_start = hits["load0"][0]
    if load_start is None and hits.get("loadPct"):
        load_start = hits["loadPct"][0][0]
    step("engine", t_vfy, load_start)

    tooks = hits.get("took") or []
    t_load_end = tooks[0] if tooks else None
    step("load", load_start, t_load_end, _shard_prog(hits, "load"))

    n_bars = len(hits.get("load0") or [])
    if tooks and len(tooks) > 1:
        # 2nd load pass = MTP/draft head (only fast variants have one)
        step("draft", t_load_end, tooks[1], _shard_prog(hits, "draft"))
        c0 = tooks[1]
    elif t_load_end is not None and (n_bars >= 2 or expect_draft):
        # The 1st pass is done but its 'took' twin is still to come: the MTP/draft
        # pass is in flight. Keep it OPEN (visible pill with its real shard %), and
        # hold compile back until the draft closes - compile must never start from
        # the 1st 'took' while the 2nd pass is still loading.
        step("draft", t_load_end, None, _shard_prog(hits, "draft"))
        c0 = None
    else:
        # Single-pass boot (no MTP head): torch.compile follows the 1st 'took' directly.
        c0 = t_load_end

    kv_t = _one(hits, "kv")
    c1 = _last_before(hits.get("compile"), kv_t)
    if c1 is None:
        cands = hits.get("compile") or []
        c1 = cands[-1] if cands else None
    step("compile", c0, c1)
    step("kv", c1, kv_t)

    # CUDA graphs: the 100% marker can drop out of a truncated window; any late
    # graph-progress line is a good-enough end anchor.
    cuda_pcts = hits.get("cudaPct") or []
    cu_end = max(hits["cuda100"]) if hits.get("cuda100") else None
    if cu_end is None and cuda_pcts:
        last = cuda_pcts[-1]
        cu_end = last[0] if last[1] == 100 else None
    step("cuda", kv_t, cu_end, _cuda_prog(hits, kv_t, cu_end))

    t_srv = _one(hits, "server")
    step("server", cu_end or kv_t, t_srv)
    step("ready", t_srv, _one(hits, "complete"))
    return ph


def _fingerprint_from_env_list(env_list):
    d = {}
    for item in env_list or []:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        if k in BOOT_CAL_KEYS:
            d[k] = v
    return json.dumps(d, sort_keys=True)


def _env_fingerprint():
    return _fingerprint_from_env_list(
        ["%s=%s" % (k, v) for k, v in parse_env(read_env_text()).items()])


def _container_fingerprint(name):
    """Settings fingerprint from the container's own environment (what the
    running boot actually used), falling back to the current .env."""
    try:
        r = subprocess.run(["docker", "inspect", name, "--format", "{{json .Config.Env}}"],
                           cwd=REPO, capture_output=True, text=True, timeout=15)
        return _fingerprint_from_env_list(json.loads(r.stdout.strip() or "[]"))
    except Exception:
        return _env_fingerprint()


def _bootcal_load(fp):
    """Cached per-phase seconds: exact settings match first, else the most
    recent measurement under any settings (a fallback estimate)."""
    try:
        with open(BOOTCAL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if fp in data and data[fp].get("phases"):
        return data[fp]["phases"]
    best = None
    for rec in data.values():
        if not rec.get("phases"):
            continue
        if best is None or (rec.get("at") or "") > (best[0] or ""):
            best = (rec.get("at"), rec["phases"])
    return best[1] if best else {}


def _bootcal_save(fp, phases):
    global _bootcal_saved
    sig = (fp, tuple(sorted((k, round(v, 1)) for k, v in phases.items())))
    if sig == _bootcal_saved:
        return
    _bootcal_saved = sig
    try:
        with open(BOOTCAL_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data[fp] = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "phases": {k: round(v, 2) for k, v in phases.items()}}
    if len(data) > 12:
        for k in sorted(data, key=lambda k: data[k].get("at") or "")[: len(data) - 12]:
            del data[k]
    try:
        with open(BOOTCAL_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
    except Exception:
        pass


def find_serve_container():
    """The hyperqwen serve container (single/batch/solo): the running one if
    any, else the most recent stopped one (a failed boot)."""
    try:
        r = subprocess.run(["docker", "ps", "-a", "--format", "json"],
                           cwd=REPO, capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    running, stopped = [], []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        if "hyperqwen" not in (c.get("Image") or ""):
            continue
        labels = c.get("Labels")
        if not isinstance(labels, str):
            labels = json.dumps(labels or {})
        msvc = re.search(r"com\.docker\.compose\.service=([A-Za-z0-9_-]+)", labels)
        if not msvc or msvc.group(1) not in ("single", "batch", "solo"):
            continue
        row = {"name": c.get("Names") or "", "service": msvc.group(1),
               "state": c.get("State") or "", "health": c.get("HealthStatus") or "",
               "status": c.get("Status") or ""}
        (running if row["state"] == "running" else stopped).append(row)
    if running:
        return running[0]
    return stopped[-1] if stopped else None


def boot_progress(tail=3000):
    """One-shot startup-progress snapshot for the settings UI.

    The --tail window defaults to 3000 lines so a fully-timestamped boot (with
    its tqdm progress lines, which are many per second) stays inside the window
    even after the server has been running a while."""
    if not docker_available():
        return {"ok": True, "docker": False, "container": None,
                "run": {"state": "no-docker", "pct": 0.0}}
    cont = find_serve_container()
    if cont is None:
        return {"ok": True, "docker": True, "container": None,
                "run": {"state": "stopped", "pct": 0.0}}

    try:
        # NO text=True: it enables universal-newline translation, which converts the
        # CRs embedded in tqdm progress lines (one \n-record can hold many \r updates)
        # into \n — orphaning the progress text from its timestamp and breaking the
        # shard/CUDA-graph progress regexes. Capture bytes and decode without translation.
        # Also merge the container's stderr into the stream: the docker CLI forwards
        # the container's stderr on the client's stderr, and this image writes uvicorn's
        # "Started server process" / "Application startup complete" and the CUDA-graph
        # tqdm lines there — a stdout-only capture loses them (and the progress bars).
        r = subprocess.run(["docker", "logs", "--timestamps", "--tail", str(tail),
                            cont["name"]],
                           cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=30)
        text = (r.stdout or b"").decode("utf-8", "replace")
    except Exception as e:
        return {"ok": True, "docker": True, "container": cont,
                "run": {"state": "unknown", "pct": 0.0, "error": str(e)}}

    events = list(_iter_log_lines(text))
    if not events:
        st = "starting" if cont["state"] in ("running", "created") else "stopped"
        return {"ok": True, "docker": True, "container": cont,
                "run": {"state": st, "pct": 0.0,
                         "note": "no log lines yet (container %s)" % cont["state"]}}

    # current run = since the last boot marker; the run before it = calibration
    boot_idx = [i for i, (_, b) in enumerate(events)
                if "entrypoint: first-boot stages" in b]
    if boot_idx:
        cur_i = boot_idx[-1]
        prev_i = boot_idx[-2] if len(boot_idx) > 1 else 0
        cur = events[cur_i:]
        prev = events[prev_i:cur_i]
    else:
        cur, prev = events, []

    hits = _scan_hits(cur)

    # per-phase calibration: previous in-log run > cached measurement > default
    fp = _container_fingerprint(cont["name"])
    # SPEC=mtp boots run a 2nd, much shorter shard pass (the MTP/draft head) between
    # the main weights and torch.compile; the draft pill only exists once that pass is
    # visible (its 0% bar or its 'took' line), so ask for the draft phase up front.
    try:
        fpd = json.loads(fp) if isinstance(fp, str) and fp else {}
    except Exception:
        fpd = {}
    expect_draft = str(fpd.get("SPEC", "")).lower() == "mtp"
    phases = _build_phases(hits, expect_draft)
    prev_phases = _build_phases(_scan_hits(prev), expect_draft) if prev else []
    cached = _bootcal_load(fp)
    prev_meas = {p["key"]: p["dur"] for p in prev_phases if p["dur"] is not None}
    calmap, calsrc = {}, {}
    for k in BOOT_PHASE_ORDER:
        if k in prev_meas:
            calmap[k], calsrc[k] = prev_meas[k], "prev-run"
        elif k in cached:
            calmap[k], calsrc[k] = cached[k], "cache"
        else:
            calmap[k], calsrc[k] = BOOT_PHASE_DEFAULTS[k], "default"
    cal_summary = ("previous run in this log" if prev_meas
                  else "cached measurements" if cached else "built-in defaults")

    # run state — ground truth, in priority order:
    #   1) a fatal marker in this run's window                    -> failed
    #   2) 'Application startup complete' / 'Started server process' in window
    #   3) docker's own healthcheck healthy (curl /health 200)   -> ready
    #      (long-lived containers lose their boot lines from the log window,
    #       but the healthcheck is exactly the "model loaded and serving" verdict)
    #   4) exited/dead without a ready signal                     -> stopped (user stop)
    #   5) running/created, or healthcheck still warming up      -> starting
    fatal_ts = hits["fatal"][0][0] if hits["fatal"] else None
    complete_ts = hits["complete"][-1] if hits["complete"] else None
    server_ts = hits["server"][-1] if hits["server"] else None
    ready_ts = complete_ts if complete_ts is not None else server_ts
    now = time.time()
    health = (cont.get("health") or "").lower()
    if fatal_ts is not None and (ready_ts is None or fatal_ts > ready_ts):
        state = "failed"
    elif ready_ts is not None:
        state = "ready"
    elif health == "healthy":
        state = "ready"
        ready_ts = None  # exact moment unknown: the complete line left the log window
    elif cont["state"] in ("running", "created", "restarting"):
        state = "starting"
    else:
        state = "stopped"

    now_pct = fatal_ts if state == "failed" else now

    # Overall progress = the WHOLE boot, 0 -> 100% (from "Start" to "Model loaded"):
    #   total = the expected duration of every phase of the boot, INCLUDING the
    #           phases that have not started yet, so the denominator is fixed for
    #           the whole boot (a phase closing or a new one starting can no longer
    #           resize it, and the bar can never jump backwards because of it)
    #   base  = measured durations of finished phases + the in-flight fraction of
    #           the active one (its real N% from the log where available, else
    #           elapsed time over its estimate)
    n_bars = len(hits.get("load0") or [])
    total = 0.0
    for k in BOOT_PHASE_ORDER:
        if k == "draft" and not (n_bars > 1 or expect_draft):
            continue  # this boot has no 2nd shard pass (MTP/draft head) - drop its estimate
        total += max(1.0, calmap.get(k, 10.0))
    base = 0.0
    for p in phases:
        if p["t0"] is None:
            continue
        exp = max(1.0, calmap.get(p["key"], 10.0))
        if p["t1"] is not None:
            base += p["dur"] if p["dur"] is not None else exp
        else:
            if p.get("progress"):
                f = min(1.0, p["progress"]["pct"] / 100.0)
            else:
                f = min(1.0, max(0.0, (now_pct - p["t0"]) / exp))
            base += f * exp
    pct = 100.0 * base / total if total > 0 else (100.0 if state == "ready" else 0.0)

    # A stopped container that never became ready: non-zero exit = failed boot,
    # clean exit 0 = user stop (reset the bar for the next boot).
    last_ev = events[-1][0]
    boot0 = hits["boot"][0] if hits["boot"] else None
    if state == "stopped":
        m = re.search(r"Exited \((\d+)\)", cont.get("status") or "")
        code = m.group(1) if m else None
        if code == "0":
            pct = 0.0
        else:
            state = "failed"
            if not hits["fatal"]:
                hits["fatal"].append((last_ev, "container exited with code %s" % (code or "?")))

    # Monotonicity guard: the displayed % never moves backwards during a boot
    # (estimates get replaced by measurements as phases close; keep the max).
    pkey = cont.get("name") or "?"
    rec = _pct_state.get(pkey)
    if rec is None or rec.get("boot") != boot0:
        rec = {"boot": boot0, "pct": 0.0}
        _pct_state[pkey] = rec
    if state == "ready":
        pct = 100.0  # a ready server has, by definition, finished the bar
        rec["pct"] = 100.0
    elif state == "stopped" and pct == 0.0:
        _pct_state.pop(pkey, None)
    else:
        pct = max(pct, rec["pct"])
        rec["pct"] = pct
    if len(_pct_state) > 64:
        for k in list(_pct_state)[:len(_pct_state) - 64]:
            del _pct_state[k]
    out_phases = []
    for p in phases:
        exp = calmap.get(p["key"])
        d = {"key": p["key"], "label": p["label"], "t0": p["t0"], "t1": p["t1"],
             "dur": p["dur"], "expected": round(exp, 1), "src": calsrc.get(p["key"]),
             "progress": p.get("progress")}
        if p["t1"] is not None and p["t0"] is not None:
            d["status"] = "done"
        elif state == "ready":
            # ready established without this phase's end marker (truncated
            # window) — done, duration unknown
            d["status"] = "done"
        elif p["t0"] is not None:
            d["status"] = "active"
            end_ref = ready_ts or fatal_ts or last_ev
            d["elapsed"] = round(min(now_pct, end_ref) - p["t0"], 1)
            if state != "failed" and d["elapsed"] > 1.3 * exp:
                d["overrun"] = True
        else:
            d["status"] = "pending"
        out_phases.append(d)
    if state == "failed" and fatal_ts is not None:
        fail_phase = None
        for p in phases:
            if p["t0"] is not None and p["t1"] is not None and abs(p["t1"] - fatal_ts) <= 1.0:
                fail_phase = p
        if fail_phase is None:
            for p in phases:
                if p["t0"] is not None and p["t1"] is None and p["t0"] <= fatal_ts:
                    fail_phase = p
        if fail_phase is not None:
            for d in out_phases:
                if d["key"] == fail_phase["key"]:
                    d["status"] = "fail"

    run = {
        "state": state,
        "pct": pct,
        "started": boot0,
        "lastTs": last_ev,
        "readyAt": ready_ts,
        "bootDur": round(ready_ts - boot0, 1) if (state == "ready" and boot0 and ready_ts) else None,
        "elapsed": round(min(now_pct, (ready_ts or last_ev)) - boot0, 1)
                   if (state in ("starting", "failed") and boot0) else None,
        "totalEst": (round(total, 1) if state in ("starting", "failed") and total > 0 else None),
        "calSrc": cal_summary,
        "phases": out_phases,
        "errors": [b[:300] for _, b in hits["fatal"][-6:]] if state == "failed" else [],
    }
    if state == "ready" and ready_ts is None:
        run["note"] = ("docker healthcheck: healthy — model loaded and serving "
                       "(boot-complete lines no longer in the log window)")
    if state == "ready" and boot0:
        meas = {p["key"]: p["dur"] for p in phases if p["dur"] is not None}
        if meas:
            _bootcal_save(fp, meas)
    return {"ok": True, "docker": True, "container": cont, "run": run}


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
                "model":          qs.get("model", env.get("MODEL", "")),
            }
            try:
                self._json(compute_vram(params))
            except Exception as e:
                self._json({"ok": False, "error": f"vram calc: {type(e).__name__}: {e}"}, 500)
        elif p == "/api/models":
            try:
                self._json({"ok": True, "models": _list_models()})
            except Exception as e:
                self._json({"ok": False, "error": f"model list: {type(e).__name__}: {e}"}, 500)
        elif p == "/api/status":
            text = read_env_text()
            values = parse_env(text)
            port = values.get("PORT", "18020")
            self._json({"ok": True, "port": port,
                        "health": probe_health(port),
                        "docker": docker_available(),
                        "mode": "unknown"})
        elif p == "/api/bootlog":
            try:
                tail = max(500, min(4000, int(qs.get("tail", "3000") or 3000)))
            except (ValueError, TypeError):
                tail = 3000
            self._json(boot_progress(tail))
        elif p == "/api/backups":
            self._json({"ok": True, "backups": list_backups()})
        elif p == "/api/presets":
            self._json({"ok": True, "custom": list_custom_presets()})
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
            # Auto-apply reasoning effort to the chat template (vLLM 0.30.0
            # lacks --chat-template-kwargs, so we rewrite the template directly)
            template_note = ""
            effort = targets.get("REASONING_EFFORT", "")
            if effort in ("off", "low", "medium", "xhigh"):
                script = os.path.join(REPO, "prepare", "apply_reasoning_effort.py")
                if os.path.isfile(script):
                    try:
                        r = subprocess.run(
                            [sys.executable, script, "--effort", effort],
                            cwd=REPO, capture_output=True, text=True, timeout=30)
                        if r.returncode == 0:
                            template_note = " Chat template updated."
                        else:
                            template_note = f" (template warn: {r.stderr.strip()[:120]})"
                    except Exception as e:
                        template_note = f" (template error: {e})"
            self._json({"ok": True, "backup": bak,
                        "note": "Saved. Restart the server to apply." + template_note})
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
        elif p == "/api/presets":
            action = payload.get("action", "")
            if action == "create":
                p, err = create_custom_preset(payload.get("name", ""), payload.get("sets") or {},
                                              bool(payload.get("overwrite", False)))
                if err:
                    return self._json({"ok": False, "error": err}, 400)
                self._json({"ok": True, "preset": p})
            elif action == "delete":
                if not delete_custom_preset(payload.get("id", "")):
                    return self._json({"ok": False, "error": "no such preset"}, 404)
                self._json({"ok": True})
            else:
                self._json({"ok": False, "error": "unknown preset action"}, 400)
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
