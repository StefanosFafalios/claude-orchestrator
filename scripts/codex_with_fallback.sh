#!/usr/bin/env bash
# Run a task or handoff file through the local OpenAI Codex CLI.
#
# Usage: codex_with_fallback.sh <task-or-handoff-file>
# Exit codes: 0 success, 42 usage limit (prints CODEX_USAGE_LIMIT), 1 other failure, 2 bad usage.
# Env: CODEX_BIN (default: codex), CODEX_MODEL, CODEX_EFFORT (low|medium|high),
#      CODEX_SANDBOX (read-only|workspace-write; default read-only).
set -uo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "usage: $0 <task-or-handoff-file>" >&2
  exit 2
fi

limit_re='usage limit|limit reached|rate limit exceeded|quota exceeded|insufficient_quota'
weak_re='upgrade|resets'
ordinary_re='unauthori[sz]ed|authenticat|not logged in|log ?in required|invalid api key|permission denied|forbidden|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|network|could not resolve|timed out'

out_file=$(mktemp)
err_file=$(mktemp)
trap 'rm -f "$out_file" "$err_file"' EXIT

args=(exec --sandbox "${CODEX_SANDBOX:-read-only}")
[[ -n "${CODEX_MODEL:-}" ]] && args+=(--model "$CODEX_MODEL")
[[ -n "${CODEX_EFFORT:-}" ]] && args+=(-c "model_reasoning_effort=\"$CODEX_EFFORT\"")
args+=("$(cat "$1")")

"${CODEX_BIN:-codex}" "${args[@]}" </dev/null >"$out_file" 2>"$err_file"
status=$?

is_limit() {
  local text="$1"
  grep -qiE "$limit_re" <<<"$text" && return 0
  grep -qiE "$weak_re" <<<"$text" && ! grep -qiE "$ordinary_re" <<<"$text"
}

if [[ $status -eq 0 ]]; then
  # Codex writes progress to stderr; on success only a short stdout can be a limit notice.
  if [[ $(wc -c <"$out_file") -lt 400 ]] && is_limit "$(cat "$out_file")"; then
    echo "CODEX_USAGE_LIMIT"
    cat "$out_file" "$err_file" >&2
    exit 42
  fi
  cat "$out_file"
  exit 0
fi

# Codex echoes the prompt into stderr; only its tail holds the error.
if is_limit "$(cat "$out_file"; tail -n 20 "$err_file")"; then
  echo "CODEX_USAGE_LIMIT"
  cat "$out_file" "$err_file" >&2
  exit 42
fi

cat "$out_file"
cat "$err_file" >&2
exit 1
