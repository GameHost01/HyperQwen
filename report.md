# Model verification report — `Swift-1.5-Qwen3.8-27B-W4A16-syv-fast`

- **Date:** 2026-10-13
- **Scope:** Verify that the new model dir works with (1) the settings UI and (2) the VRAM calculator,
  against the known-good reference `models/Qwen3.8-27B-W4A16-AutoRound-fast`.
- **Method (strictly read-only):** no file in the repo was edited, no server was started/stopped.
  - VRAM calculator: imported `ui/serve.py` in-process and called `compute_vram()` with the live `.env`
    values (identical code path to `GET /api/vram` on the running 8790 server).
  - Weights: byte-exact safetensors header scan (8-byte u64 length prefix + JSON tensor map,
    dtype-weighted shape products) over every non-`.bak` `*.safetensors` in both dirs.
  - Configs: full parse of both `config.json` (incl. `quantization_config.config_groups`, `ignore`,
    `text_config.layer_types`).
  - Draft vocab: `torch.load` of both `mtp_draft_vocab_ids.pt` + length of the `swift*` JSON id lists.
  - Previous launch: `docker logs hyperqwen-single-1` (2026-10-09, 2026-10-13 boots).
  - Server state (observed only, not touched): UI server `127.0.0.1:8790` **up**; vLLM server
    `0.0.0.0:18020` **up**.

## 1. Where the UI and the VRAM calculator live — and what they read

| Component | Location | What it reads |
|---|---|---|
| Settings UI | `ui/serve.py` — zero-dependency stdlib HTTP server on `127.0.0.1:8790`, serving `ui/index.html` + `ui/app.js`, schema in `ui/schema.json`; edits the repo `.env` (backs up before each save, masks secrets) | The `.env` file itself. **There is no model list or dropdown anywhere**: `schema.json` declares `MODEL` as `type: "text"` — a model is selected by *typing its path*. |
| VRAM calculator | Also inside `ui/serve.py`: `GET /api/vram` → `compute_vram(p)`; `ui/app.js` sends the live `.env` values (incl. `KV_MEM`, `OS_VRAM`) on a 250 ms debounce | (a) a **hardcoded map** `_w_mib(variant)`: `base` → `models/Qwen3.8-27B-W4A16-AutoRound`, `fast` → `...-AutoRound-fast`; (b) a **directory scan** `_scan_st()` summing header-declared bytes of every non-`.bak` `*.safetensors` (incl. `model_extra_tensors.safetensors`) in that dir; (c) **hardcoded constants**: `_FB` fallbacks (base 15968.8 / fast 15060.0 / dflash2 1221.3 MiB), `KV_BPT` (bf16 65536 / fp8 32768 / kvarn & int4pth 16384 bytes/token), `KV_PIN` (single fast/long 5583457484, huge 5261334938 bytes), `PREFIX_KV_FRAC=0.15`, `GPU_TOTAL=24576 MiB`. It does **not** read `MODEL`, `config.json`, or the model file list — only the `variant` key. |

## 2. Results

| # | Check | Expected | Found | P/F | Proposed fix |
|---|---|---|---|---|---|
| 1 | UI: new model appears in a "model list" | Swift visible in a list/dropdown | **No list exists** — `MODEL` is a free-text field (`ui/schema.json`); nothing in `ui/` enumerates `models/` | n/a (no list to fail) | — (optional: `models/` directory-scan dropdown) |
| 2 | UI: can be selected; `MODEL` → new dir with settings preserved | Type path → saved; other settings intact | **Yes, by typing** `models/Swift-1.5-…-syv-fast` (per-field `.env` save keeps the rest; `single-user/select_model.sh` preserves any non-empty `MODEL`). **But** the saved preset "Qwen-fast" (`ui/.custom-presets.json`) explicitly sets `MODEL:''` — applying it **clears** the selection; and every default (`select_model.sh`, `resolve_config.sh`, `verify.sh`, `batch/start_qwen.sh`, `docker/entrypoint.sh`) then falls back to the AutoRound names | ⚠ partial | → §4, fix B |
| 3 | Fixed paths that skip or break the new model | Any `MODEL` dir usable end-to-end | `docker/prepare.sh` hardcodes `BASE=/app/models/Qwen3.8-27B-W4A16-AutoRound` → on a fresh box the prepare/verify stage never fetches or validates Swift. No `"-fast"` name-matching exists anywhere; the only fixed strings are prepare's base dir and `_w_mib`'s two keys | ❌ fail | → §4, fix C |
| 4 | Calculator: weight estimate vs on-disk — **reference** | ≈ on-disk total, vision excluded when `VISION=0` | Live run: **14181.2 MiB** = 15060.0 − 878.8 (vision). On-disk scan = **15060.0 MiB** (15,791,521,680 B incl. `model_extra_tensors`) — reproduces `_FB['fast']` exactly, proving the scan method is sound | ✅ pass | — |
| 5 | Calculator: weight estimate for **Swift** | A model-aware estimate of 15122.2 MiB | **Cannot target Swift at all**: `_w_mib` maps only `base`/`fast` → the AutoRound dirs; any other variant crashes with `KeyError('swift')`. Even with `MODEL=<Swift path>` in `.env`, the panel would still display the reference's 14181.2 MiB | ❌ fail | → §4, fix A |
| 6 | "~1 GiB heavier" (asymmetric W4A16 g128 zero-points) | Swift ≈ 16 GiB vs ref ≈ 15 GiB | On-disk delta is only **+62.2 MiB** (15122.2 vs 15060.0 MiB, +0.41%). Config confirms the body *is* asymmetric (`group_0: symmetric=False, zp_dtype: int8` vs ref's `symmetric=True`) — the zero-points exist, but the net byte delta is far below 1 GiB (the ref also has a different 7-shard layout) | ❌ expectation not met | No code fix; budget the +62 MiB, discard the "~1 GiB heavier" assumption |
| 7 | "lm_head and MTP module are int4 GPTQ" | 4-bit GPTQ lm_head & MTP | 4-bit group-128 quant is confirmed — but the method is **compressed-tensors, not GPTQ** (no GPTQ config anywhere): `lm_head` = 248,320 rows packed I32; MTP layer + `fc` int4 g128. The separate `model-mtp-bf16.safetensors` (52.9 KB) holds **only the 7 bf16 MTP norms** — the file name is a red herring; the MTP layer itself is int4 | ⚠ label wrong | Docs/notes: "int4 compressed-tensors (GPTQ-style) lm_head + MTP; MTP norms bf16 in side file" |
| 8 | "embeddings int8" | 8-bit group quant | **Confirmed in both models**: `group_2 targets [re:.*embed_tokens$], num_bits: 8, group_size: 128, symmetric: True` | ✅ pass | — |
| 9 | Draft vocab 29,670 rows (not stock 40,960) | `mtp_draft_vocab_ids` has 29,670 | **Confirmed**: Swift `.pt` = 29,670 rows (== `swift15_union_draft_vocab_ids.json`; the other swift lists are 25,879 / 25,285); `mtp.draft_lm_head` shape `[29670,640]` vs ref `[40960,640]`; ref `.pt` = 40,960. But `ui/serve.py`'s note hardcodes **"40k draft vocab"** — wrong for Swift | ⚠ note stale | → §4, fix D |
| 10 | Extra tensors in `model_extra_tensors.safetensors` counted | Sum includes the extra file, not just numbered shards | Both dirs contain one (ref 327,162,544 B; Swift 297,303,984 B); `_scan_st` includes every non-`.bak` `*.safetensors` (the ref total reproducing `_FB` proves inclusion works) | ✅ pass | — |
| 11 | bf16 vision dropped when `VISION=0` | −878.8 MiB for both | Vision tensors are BF16 in both dirs (identical `vision_config`: hidden 1152, depth 27, heads 16), 878.8 MiB each; calculator subtracts exactly that when `vision=0`/offload | ✅ pass | — |
| 12 | KV-pool estimate vs last launch log | Calculator pool ≈ logged pool | Last boot (2026-10-13, **pre-`KV_MEM`-pin**) logged **210,193 tokens** (fp8). Calculator with the current `.env` pin `KV_MEM=5397100000` → pool **4375.0 MiB + prefix 772.1 MiB** (85/15 split) = **140,000 tokens**. A fresh boot with today's `.env` gets a *smaller* pool than the log — the pin, not the model, explains it. `KV_BPT` fp8 = 32768 is validated correct for Swift: `text_config` identical (64 layers = 48 linear + 16 full-attn, 4 KV heads, head_dim 256). Result: `fits=true`, headroom 231 MiB, `max_len_ok` (130,048 ≤ 140,000) | ✅ pass (with note) | → §4, fix E (only if the larger pool is wanted) |

## 3. Could NOT be verified without launching (re)starting the server

1. **vLLM actually loading the Swift dir** — whether the engine accepts the 302-entry `ignore`
   list (it adds whole-module `…linear_attn` and `.linear_attn.norm` entries beyond the ref's
   `in_proj_a/b`-only list), the 6-shard layout, and the `model-mtp-bf16` side file.
   `verify.sh`'s asymmetric zero-point check is designed for exactly this, but running it needs the
   container's Python/vLLM environment.
2. **Real GPU occupancy at load time** — weights 15122.2 MiB + MTP + KV vs the 0.94-utilization
   budget (calculator: the reference fits with 231 MiB headroom; Swift is only +62 MiB heavier, but
   this is unverified live; OOM / zero-point behavior under the asymmetric AWQ GEMM path untested).
3. **Actual KV pool size under Swift's geometry** — only the reference's pools are logged
   (210,193 pre-pin / 140,000 pinned); Swift's free-memory-dependent auto-size needs a real boot.
4. **The 29,670-row draft path at runtime** — which of `mtp_draft_vocab_ids.pt` (29,670) vs the
   `swift15_*` JSON lists (25,879 / 25,285) the speculative-decode path consumes at boot, and whether
   it errors on a non-40,960 size, is untested.
5. **Live UI behavior in a browser** — selection was verified by code reading (free-text field,
   per-field save) and by the in-process `compute_vram()` call; no POST was made to the running 8790
   server (that would write `.env`) and no page was clicked through.
6. **Fresh-container `prepare` stage** — that it builds only AutoRound + AutoRound-fast + DFlash2 and
   never touches Swift is established by reading `docker/prepare.sh`, not by running a fresh container.
7. **`served_model_name`** — hardcoded `qwen3.8-27b` for both models; whether clients care that two
   different weight dirs share one served name is a naming question, untested at the API.

## 4. Proposed fixes (diff only — NOT applied, per the read-only constraint)

**Fix A — `ui/serve.py`: make the calculator model-aware (fixes #5).**

```diff
- def _w_mib(variant):
-     dirs = {'base': MODELS_DIR + '/Qwen3.8-27B-W4A16-AutoRound',
-             'fast': MODELS_DIR + '/Qwen3.8-27B-W4A16-AutoRound-fast'}
-     d = dirs.get(variant)
-     if d and os.path.isdir(d):
-         b = _scan_st(d)
-         return b / 1048576 if b > 0 else _FB[variant]['total']
-     return _FB[variant]['total']
+ def _w_mib(variant, model_dir=None):
+     dirs = {'base': MODELS_DIR + '/Qwen3.8-27B-W4A16-AutoRound',
+             'fast': MODELS_DIR + '/Qwen3.8-27B-W4A16-AutoRound-fast',
+             'swift': MODELS_DIR + '/Swift-1.5-Qwen3.8-27B-W4A16-syv-fast'}
+     d = model_dir or dirs.get(variant)
+     if d and os.path.isdir(d):
+         b = _scan_st(d)
+         return b / 1048576 if b > 0 else None
+     fb = _FB.get(variant)
+     return fb['total'] if isinstance(fb, dict) else fb
```
plus a guard in `compute_vram`: when `_w_mib` returns `None`, emit a
`"model dir not found — weight line unavailable"` note instead of letting the `KeyError` 500 the
`/api/vram` endpoint.

**Fix B — preset handling: never let a preset blank `MODEL` (fixes #2).** In the preset-apply path of
`ui/serve.py`, skip empty-valued keys:

```diff
   for k, v in preset_sets.items():
+    if v == "":          # an empty preset entry must not clear a user-set field
+        continue
     changes[k] = v
```
(alternatively, remove the `"MODEL": ""` entry from the "Qwen-fast" preset in
`ui/.custom-presets.json`).

**Fix C — `docker/prepare.sh`: honor a pre-built `MODEL` dir (fixes #3).**

```diff
+ # If the operator points MODEL at a dir that already exists (e.g. the prebuilt
+ # Swift-1.5 ...-syv-fast), there is nothing to fetch or quantize — skip prep.
+ if [ -n "${MODEL:-}" ] && [ -d "$MODEL" ]; then
+   echo "prepare: MODEL=$MODEL already present, skipping fetch/quantize"
+   exit 0
+ fi
  BASE=${BASE_MODEL_DIR:-/app/models/Qwen3.8-27B-W4A16-AutoRound}
```
(`verify.sh` already honors a set `$MODEL`; only its default fallback needs the same treatment.)

**Fix D — `ui/serve.py`: correct the stale "40k draft vocab" note (fixes #9).**

```diff
- "MTP_DRAFT_VOCAB=1 — 40k draft vocab slightly changes MTP head size (not modeled)."
+ "MTP_DRAFT_VOCAB=1 — draft-vocab head size is model-specific (40,960 rows in the reference; 29,670 in Swift-1.5) — not modeled."
```

**Fix E (optional) — `.env`: match the KV pin to the 2026-10-13 log (relates to #12).** The logged
pool was 210,193 tokens fp8 = 6,887,303,040 B. With the 85/15 pool/prefix split:

```diff
- KV_MEM=5397100000
+ KV_MEM=8102709460      # 6.887 GB pool + 15% prefix reserve == logged 210,193-token pool
```
(leave as-is if the smaller 140,000-token pool is the intended trade-off for headroom.)

## 5. Appendix — measured values

### On-disk (header-declared bytes, all non-`.bak` `*.safetensors`)

| Dir | Shards | Extra file | MTP side file | Total | MiB |
|---|---|---|---|---|---|
| `Qwen3.8-27B-W4A16-AutoRound-fast` (ref) | 7 × `model-0000N-of-00007` (3,211,003,472 + 3,195,027,104 + 3,195,027,104 + 3,217,442,568 + 699,275,200 + 1,291,274,752 + 655,565,120) | `model_extra_tensors.safetensors` 327,162,544 | `mtp_draft_vocab_ids.pt` 329,341 B (40,960 rows) | 15,791,521,680 | **15,060.0** |
| `Swift-1.5-Qwen3.8-27B-W4A16-syv-fast` | 6 × `model-0000N-of-00006` (655,565,120 + 2,730,582,048 + 3,966,050,624 + 3,966,050,704 + 3,993,185,016 + 248,265,904) | `model_extra_tensors.safetensors` 297,303,984 | `model-mtp-bf16.safetensors` 52,936 (7 bf16 norms) + `mtp_draft_vocab_ids.pt` 238,973 B (29,670 rows) | 15,856,743,280 | **15,122.2** |

Delta vs reference: **+65,221,600 B = +62.2 MiB (+0.41 %)** — not the ~1 GiB the asymmetric
zero-points suggested (g128 zero-points are only 1 byte per 128 weights).

### Quantization (both `quant_method: compressed-tensors`; `architectures: Qwen3_5ForConditionalGeneration`)

| Group | Target | Ref | Swift |
|---|---|---|---|
| `group_0` | body `Linear` | int4 g128 **symmetric** (no zp) | int4 g128 **asymmetric, `zp_dtype: torch.int8`** |
| `group_1` | `re:.*lm_head$` | int4 g128 symmetric | int4 g128 symmetric |
| `group_2` | `re:.*embed_tokens$` | **int8** g128 symmetric | **int8** g128 symmetric |
| `group_3` | `re:^mtp\..*` | int4 g128 symmetric (+ bf16 norms) | int4 g128 symmetric (+ bf16 norms in side file) |

`ignore` lists: ref 206 entries (48 × `linear_attn.in_proj_a/b`); Swift 302 entries (adds the whole
`linear_attn` module and `linear_attn.norm`, 48 each). `text_config` identical: hidden 5120,
24 attn heads, 4 KV heads, head_dim 256, 64 layers (`48 linear + 16 full_attention`),
vocab 248,320, intermediate 17408 — hence the calculator's `KV_BPT` fp8 = 32,768 B/token
(16 × 4 × 256 × 2 × 1 B) is valid for both models.

### VRAM calculator (in-process `compute_vram`, live `.env`: single/mtp/long/fp8, `KV_MEM=5397100000`,
`MAX_LEN=130048` as logged, `GPU_UTIL=0.94`, `MAX_SEQS=8`, int8_act, spec_attn, lookup, mtp_draft_vocab,
prefix cache, vision off + offload)

| Line | Value | Note |
|---|---|---|
| GPU usable (0.94 × 24576) | 23101.4 MiB | |
| Model weights (fast) | 14181.2 MiB | = 15060.0 − 878.8 vision; this is the **reference** number for any `MODEL` value |
| KV cache pool (custom) | 4375.0 MiB | 85 % of the `KV_MEM` pin |
| Prefix cache (KV reserved) | 772.1 MiB | 15 % (`PREFIX_KV_FRAC`) |
| CUDA graphs (mtp) | 1024 MiB | |
| Activations (single, int8) | 819 MiB | |
| OS/display (`.env` `OS_VRAM`) | 819 MiB | |
| Recurrent state (MTP k=4) | 451.0 MiB | |
| System overhead / tokenizer | 410.0 / 19.1 MiB | |
| **Total / headroom** | **22870.4 / 231.0 MiB** | `fits=true`, `kv_capacity_tokens=140,000`, `max_len_ok=true` |

Constants that would be wrong for Swift if reused blindly: `_FB` totals (15968.8 / 15060.0 /
1221.3 MiB), `KV_PIN` (5,583,457,484 / 5,261,334,938 B), the "40k draft vocab" note, and the
`base`/`fast` → AutoRound dir map.

### Previous launch log (container `hyperqwen-single-1`)

- 2026-10-13 05:47 (and 2026-10-09, identical): effective config `MODE=single CTX=long SPEC=mtp`,
  `MODEL=/app/models/Qwen3.8-27B-W4A16-AutoRound-fast`, `MAX_LEN=130048`, `MAX_SEQS=8`,
  `gpu_memory_utilization=0.94`, `kv_cache_dtype=fp8`, `language_model_only=True`,
  `speculative_config={mtp, k=4, probabilistic}`, FlashInfer backend.
- `GPU KV cache size: 210,193 tokens` → 6,887.9 MB ≈ 6,574 MiB actual pool — **larger than both the
  current `KV_MEM` pin (5,147.5 MiB) and the calculator's `KV_PIN` (5,326 MiB)**; that boot pre-dates
  the `KV_MEM` pin in `.env`.

### Server state at check time (observed only)

`127.0.0.1:8790` (UI) LISTENING; `0.0.0.0:18020` + `[::]:18020` (vLLM) LISTENING. Neither was
started or stopped during this verification.
