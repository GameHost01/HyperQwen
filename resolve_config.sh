#!/bin/bash
# resolve_config.sh — one validated effective-configuration resolver for the
# launchers (automation backlog item 6, finding F13).
#
# Sourced by batch/start_qwen.sh and single-user/start_qwen.sh right after
# REPO is set:
#   source "$REPO/resolve_config.sh"
#   resolve_effective_config batch|single
#
# What it does, in order:
#   1. Refuses (exit 1) unknown values for the enumerated controls — CTX and
#      SPEC in single-user mode, KV in batch mode. A typo'd profile must never
#      boot the wrong geometry.
#   2. Warns (stderr, continues) about controls that are set but silently
#      ignored: CTX/SPEC in batch mode, KV in single-user mode, and launcher
#      flags shadowed by EXTRA_ARGS (EXTRA_ARGS expands last, so it wins
#      without a word — the warning makes that visible).
#   3. Prints the redacted effective configuration to stderr: every control
#      with its resolved value, secrets as presence-only (VLLM_API_KEY is
#      never printed, only set/unset + length; api_key.txt as present/absent).
#
# Fail-closed: refusal paths call exit, which terminates a sourcing launcher
# even without `set -e`. This file needs nothing but bash and REPO set.
# Run standalone for tests/CI: `bash resolve_config.sh single`.

# _refuse prints the reason and stops the launcher. Separate function so the
# refusal shape (stderr + nonzero exit) has exactly one definition.
_refuse() {
  echo "resolve_config: refusing: $1" >&2
  exit 1
}

# _warn prints a non-fatal notice. Ignored/shadowed controls warn rather than
# refuse: EXTRA_ARGS shadowing is a documented override door (e.g. TP size),
# and cross-mode knobs come along for the ride in a shared .env.
_warn() {
  echo "resolve_config: WARNING: $1" >&2
}

resolve_effective_config() {
  local mode=${1:?usage: resolve_effective_config batch|single}
  [ "$mode" = "batch" ] || [ "$mode" = "single" ] \
    || _refuse "unknown mode $mode (want batch|single)"
  [ -n "${REPO:-}" ] || _refuse "REPO is unset"

  # 1. Validate the enumerated controls. Defaults mirror the launchers.
  local ctx=${CTX:-} spec=${SPEC:-} kv=${KV:-}
  if [ "$mode" = "single" ]; then
    ctx=${ctx:-fast}; spec=${spec:-mtp}
    case "$ctx" in fast|long|huge) ;;
      *) _refuse "unknown CTX=$ctx (want fast|long|huge)" ;;
    esac
    case "$spec" in mtp|dflash2|off|none) ;;
      *) _refuse "unknown SPEC=$spec (want mtp|dflash2|off|none)" ;;
    esac
    [ -z "${KV:-}" ] || _warn "KV=$KV is set but single-user mode ignores it (KV is a batch-mode control)"
  else
    kv=${kv:-fp8}
    case "$kv" in fp8|kvarn|int4pth) ;;
      *) _refuse "unknown KV=$kv (want fp8|kvarn|int4pth)" ;;
    esac
    [ -z "${CTX:-}" ] || _warn "CTX=$CTX is set but batch mode ignores it (CTX is a single-user control)"
    [ -z "${SPEC:-}" ] || _warn "SPEC=$SPEC is set but batch mode ignores it (SPEC is a single-user control)"
  fi

  # 2. EXTRA_ARGS shadow warnings. The launchers expand EXTRA_ARGS after their
  # own flags, so any of these in EXTRA_ARGS silently wins; say so.
  local _shadow="--port | --host | --max-model-len | --max-num-seqs | --gpu-memory-utilization | --kv-cache-dtype | --attention-backend | --served-model-name | --api-server-count | --block-size | --mamba-ssm-cache-dtype "
  local _f
  for _f in $_shadow; do
    case " ${EXTRA_ARGS:-} " in
      *" $_f"*|*" ${_f%= }="*) _warn "EXTRA_ARGS carries ${_f% } which overrides the launcher's own flag (EXTRA_ARGS expands last)" ;;
    esac
  done
  case " ${EXTRA_ARGS:-} " in
    *" --api-key "*|*" --api-key="*)
      _warn "EXTRA_ARGS appears to carry --api-key inline; prefer VLLM_API_KEY or api_key.txt (value not shown)" ;;
  esac

  # 3. Redacted effective configuration. Secrets are presence-only: the key
  # value and api_key.txt contents never reach any output.
  local _model=${MODEL:-$REPO/models/Qwen3.8-27B-W4A16-AutoRound}
  local _port=${PORT:-18020}
  local _key_status="unset"
  [ -n "${VLLM_API_KEY:-}" ] && _key_status="set(${#VLLM_API_KEY} chars)"
  local _keyfile="absent"
  [ -f "$REPO/api_key.txt" ] && _keyfile="present"
  {
    echo "[effective-config] MODE=$mode"
    [ "$mode" = "single" ] \
      && echo "[effective-config] CTX=$ctx SPEC=$spec" \
      || echo "[effective-config] KV=$kv"
    echo "[effective-config] MODEL=$_model PORT=$_port"
    echo "[effective-config] MAX_LEN=${MAX_LEN:-<profile default>} MAX_SEQS=${MAX_SEQS:-<profile default>}"
    echo "[effective-config] REASONING_EFFORT=${REASONING_EFFORT:-<blank: model default xhigh>} REASONING_BUDGET=${REASONING_BUDGET:-<blank: no cap>}"
    local _inf

    for _inf in TEMPERATURE TOP_P TOP_K MIN_P PRESENCE_PENALTY REPETITION_PENALTY MAX_NEW_TOKENS; do

      if [ -n "${!_inf:-}" ]; then

        echo "[effective-config] sampling: $_inf=${!_inf}"

      fi

    done

    echo "[effective-config] VLLM_API_KEY=$_key_status api_key.txt=$_keyfile"
  } >&2
}

# build_sampling_args: TEMPERATURE / TOP_P / TOP_K / MIN_P / PRESENCE_PENALTY /

# REPETITION_PENALTY / MAX_NEW_TOKENS from .env -> ONE --override-generation-config

# JSON flag for `vllm serve`. Blank value = key omitted (the model's own generation_config

# default applies: Qwen3.8 = 1.0 / 0.95 / 20 / 0 / no penalties). Per-request values

# still win over these defaults, except the max_new_tokens cap, which also bounds a

# per-request max_tokens; EXTRA_ARGS still wins over this flag; bad values are dropped with a note.

build_sampling_args() {

  SAMPLING_ARGS=()

  local _k _v _j _json="" _probe_out

  for _k in TEMPERATURE TOP_P TOP_K MIN_P PRESENCE_PENALTY REPETITION_PENALTY MAX_NEW_TOKENS; do

    _v=${!_k:-}

    [ -n "$_v" ] || continue

    if [ "$_k" = TOP_K ] || [ "$_k" = MAX_NEW_TOKENS ]; then

      printf '%s' "$_v" | grep -Eq '^[0-9]+$' || { echo "resolve_config: sampling: $_k=$_v ignored (need a non-negative integer)" >&2; continue; }

      _v=$((10#$_v))

      if [ "$_k" = TOP_K ]; then

        [ "$_v" -le 99999 ] || { echo "resolve_config: sampling: $_k=$_v ignored (max 99999)" >&2; continue; }

      else

        [ "$_v" -ge 1 ] || { echo "resolve_config: sampling: $_k=$_v ignored (min 1)" >&2; continue; }

        [ "$_v" -le 262144 ] || { echo "resolve_config: sampling: $_k=$_v ignored (max 262144)" >&2; continue; }

      fi

    else

      printf '%s' "$_v" | grep -Eq '^-?([0-9]+([.][0-9]+)?|[.][0-9]+)([eE][-+]?[0-9]+)?$' || { echo "resolve_config: sampling: $_k=$_v ignored (not a number)" >&2; continue; }

      case "$_k" in

        TEMPERATURE)        awk -v v="$_v" 'BEGIN{exit !(v>=0 && v<=2)}'  || { echo "resolve_config: sampling: $_k=$_v ignored (outside 0..2)"  >&2; continue; } ;;

        TOP_P|MIN_P)        awk -v v="$_v" 'BEGIN{exit !(v>=0 && v<=1)}'  || { echo "resolve_config: sampling: $_k=$_v ignored (outside 0..1)"  >&2; continue; } ;;

        PRESENCE_PENALTY)   awk -v v="$_v" 'BEGIN{exit !(v>=-2 && v<=2)}' || { echo "resolve_config: sampling: $_k=$_v ignored (outside -2..2)" >&2; continue; } ;;

        REPETITION_PENALTY) awk -v v="$_v" 'BEGIN{exit !(v>=0 && v<=2)}'  || { echo "resolve_config: sampling: $_k=$_v ignored (outside 0..2)"  >&2; continue; } ;;

      esac

    fi

    case "$_k" in

      TEMPERATURE) _j=temperature ;;

      TOP_P) _j=top_p ;;

      TOP_K) _j=top_k ;;

      MIN_P) _j=min_p ;;

      PRESENCE_PENALTY) _j=presence_penalty ;;

      REPETITION_PENALTY) _j=repetition_penalty ;;

      MAX_NEW_TOKENS) _j=max_new_tokens ;;

    esac

    _json="${_json:+$_json,}\"$_j\":$_v"

  done

  if [ -n "$_json" ]; then
    # vLLM 0.30+: plain --help no longer lists engine args, so probe the
    # per-flag help first (exit 0 = flag is registered); on older vLLM builds that
    # probe errors and we fall through to the classic plain --help grep below.
    if _probe_out=$(venv/bin/vllm serve --help=override-generation-config 2>&1) \
        && printf '%s\n' "$_probe_out" | grep -q 'override-generation-config'; then
      SAMPLING_ARGS=(--override-generation-config "{$_json}")
    elif venv/bin/vllm serve --help 2>&1 | grep -q 'override-generation-config'; then
      SAMPLING_ARGS=(--override-generation-config "{$_json}")
    else
      echo "resolve_config: sampling: this vllm build has no --override-generation-config, so the sampling values you set cannot be applied (JSON: {$_json})." >&2
      echo "resolve_config: sampling: leave the 7 sampling values blank in the UI, or use an image whose vllm supports the flag, then restart." >&2
      exit 97
    fi
  fi

  return 0

}



# Standalone mode (tests, CI dry-run): resolve with the ambient environment.
# When sourced, BASH_SOURCE[0] differs from $0 and only the function loads.
if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
  resolve_effective_config "${1:?usage: $0 batch|single}"
fi
