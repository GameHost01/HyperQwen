# HyperQwen Settings — FULL INVENTORY (source of truth for the UI)
Repo: G:\Apps\HyperQwen  ·  Config = a .env file of env vars.
Launch = docker compose --profile single|batch  (one GPU, one mode at a time)
Current user .env: SPEC=dflash2, PREFIX_CACHE=1, CTX=long, VLLM_WSL2_ENABLE_PIN_MEMORY=1,
                  VLLM_API_KEY=<set>, HF_TOKEN=<set>
NOTE their CTX=long + SPEC=dflash2 is a non-standard combo (docs recommend SPEC=mtp CTX=long)
      -> surface a WARNING in UI. CTX=long also implies MAX_LEN=150000, GPU_UTIL=0.93.

## PRESETS (README "Which setup")
A batch      : no .env change -> docker compose --profile batch up -d   (KV=fp8 default)
B single-def : no .env change -> docker compose --profile single up -d  (SPEC=mtp CTX=fast)
C reproducing: DFLASH_TOKENS=15 (single)
D long-ctx   : SPEC=mtp, CTX=long (single)
E huge-ctx   : CTX=huge (single)

## KNOWN SETTINGS (key | type | options | default | mode | desc)
MODE CONTROLS (single):
  SPEC        enum  mtp|dflash2|off|none   mtp        single  drafter: mtp=head, dflash2=block drafter
  CTX         enum  fast|long|huge         fast       single  fast=64k bf16KV; long=150k fp8KV; huge=240k KVarN
  PREFIX_CACHE int  0|1                    0          both    reuse shared prompt prefix (20x follow-ups)
MODE CONTROL (batch):
  KV          enum  fp8|kvarn|int4pth      fp8        batch   KV cache dtype
CAPACITY:
  MAX_LEN     int   (tokens)               65536/150000 both  context ceiling
  MAX_SEQS    int   (slots)                8/64         both  admitted requests
  GPU_UTIL    float 0..1                   0.93/0.95    both  gpu memory fraction
  DFLASH_TOKENS int (verify block)         7            single 15=reproduction mode (4 slots,56k)
  DRAFT_TOKENS  int (depth)                4/3          single spec depth
  KV_MEM      int  (bytes, optional)       (5.2GiB)     single pin KV pool bytes; empty=size from GPU_UTIL
SPECUATION/DRAFT:
  SPEC_ATTN   int  0|1  1(fast)  single  split-KV verify attention
  DRAFT_SAMPLE enum probabilistic|greedy probabilistic single
  LOOKUP      int  0|1  1(dflash2) single draft from context
  MTP_DRAFT_VOCAB int 0|1 1  single  draft w/ 40k vocab (0 for non-EN/DA/code)
PERF/QUANT:
  INT8_ACT    enum int8|(empty)  (single empty, batch int8) both  int8 act on Marlin GEMMs
  INT8_LAYERS enum mlp|gate_up|.|regex  mlp  batch  which layers int8
  PREFILL_ATTN enum int8|(empty) empty  both  int8 prefill attention
FEATURES:
  TOOLS       int 0|1 1  both  tool/function calling
  TOOL_PARSER enum qwen3_coder|hermes qwen3_coder both
  VISION      int 0|1 0  both  keep vision tower
  VISION_OFFLOAD int 0|1 1  single  tower weights in host RAM
  REQ_METRICS int 0|1 0  both  per-request timing in responses
  SSE_KEEP_ALIVE int (secs) 30  both  SSE keep-alive interval
  FAST_VARIANT int 0|1 1  both  use faster-variant model download
  WARMUP      int 0|1 0  single  post-boot warmup (qwen-server.sh only)
NETWORK/IDENTITY:
  PORT        int 18020  both  api port
  MODEL       path models/Qwen3.8-27B-W4A16-AutoRound[-fast] both
  VLLM_API_KEY secret (empty)  both  REQUIRED if port exposed
  HF_TOKEN    secret (empty)  both  huggingface token
  VLLM_WSL2_ENABLE_PIN_MEMORY int 0|1 1  WSL2: 1 required or UVA abort
EXPERT/ADVANCED:
  EXTRA_ARGS  string (vllm flags)  empty  both  raw extra vllm args (wins over flags)
  CUDA_VISIBLE_DEVICES string  empty  both  pin cards
  GPU_COUNT   int|all 1  both  cards handed to container
