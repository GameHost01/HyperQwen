# HyperQwen Settings UI — Progress Tracker

## STATUS: Phase 4 COMPLETE (full VRAM coverage) — 2026-10-03

## Phase 4 (this session)
Goal: every setting that impacts VRAM is included in the calculation.

### Gaps found in audit + fixes
1. PREFIX_CACHE — was parsed but unused. Now shown as "Prefix cache (KV reserved)"
   line = 15% of the KV pool, SPLIT inside the pool line (no double count).
2. MAX_LEN — was display-only. Now feasibility-checked against KV token capacity:
   - cap_total  (full pool / bytes-per-token)  → server-start check (affects `fits`)
   - cap_tokens (after 15% prefix discount)    → runtime check (`max_len_ok`, red warning)
3. KV dtype (fp8/kvarn/int4pth) — now drives bytes-per-token:
   bf16=65536, fp8=32768, kvarn=16384, int4pth=16384 B/token
   (= 16 full-attn layers × 4 KV heads × 256 dim × 2 (K+V) × dtype bytes)
4. GPU_COUNT (advanced) — tensor-parallel sharding: weights/vision/drafter/KV/rec/act
   ÷ N; CUDA graphs + system overhead stay full per-GPU; tokenizer unsharded.
   Labels get "/ N GPUs" suffix.
5. Previously silent settings now surfaced as `notes` in the API + sidebar:
   EXTRA_ARGS (warning), INT8_ACT, PREFILL_ATTN, MTP_DRAFT_VOCAB, LOOKUP, SPEC_ATTN.
6. MAX_SEQS — already used for batch activations; also implied in capacity context.

### No VRAM impact (confirmed, no action)
TOOLS, TOOL_PARSER, DRAFT_SAMPLE, REQ_METRICS, SSE_KEEP_ALIVE, WARMUP, PORT, MODEL,
VLLM_API_KEY, HF_TOKEN, VLLM_WSL2_ENABLE_PIN_MEMORY.

### Files changed
- ui/serve.py: KV_BPT + PREFIX_KV_FRAC constants; compute_vram() new params
  (gpu_count, spec_attn, lookup, mtp_draft_vocab, int8_act, prefill_attn, extra_args),
  TP sharding, prefix split, capacity checks, notes; /api/vram handler passes
  all 20 params with .env fallback.
- ui/app.js: vramParams() sends 7 extra fields; renderVramSummary() shows
  KV capacity stat (ok/bad), GPU count, notes list; smart "KV CAPACITY SHORT" label.
- ui/index.html: .vram-note CSS.

### Verified (live server smoke tests)
- user env (mtp+long+prefix+MAX_LEN=150000): headroom +739 MiB, fits=True,
  cap=144,834 < 150,000 → max_len_ok=False (real runtime warning: tight!)
- prefix off: cap=170,393 → max_len_ok=True
- GPU_COUNT=2: 11.07 GiB headroom per card
- dflash2+kvarn+huge: cap=272,957 tok
- batch: KV auto + prefix split, fits=True
- node --check app.js OK; py_compile serve.py OK

## Phase 3 (previous)
Tab → fixed overlay sidebar, starts closed, results-only (pie, stats, table),
vramParams() reads state.values, 250ms debounce live update.
