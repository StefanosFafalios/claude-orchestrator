#!/usr/bin/env bash
# Run a task or handoff file through the local OpenAI Codex CLI.
#
# Usage: codex_with_fallback.sh <task-or-handoff-file>
# Exit codes: 0 success, 42 usage limit (prints CODEX_USAGE_LIMIT), 44 engine unavailable: logged out or
# the model is not supported for the account (prints CODEX_UNAVAILABLE logged_out|model_unsupported),
# 1 other failure, 2 bad usage.
# Env: CODEX_BIN (default: codex), CODEX_MODEL, CODEX_EFFORT (low|medium|high), CODEX_SEARCH (1 = --search), CODEX_LAST_MESSAGE (file for the final message),
#      CODEX_SANDBOX (read-only|workspace-write; default read-only).
set -uo pipefail

if [[ $# -ne 1 || ! -f "$1" ]]; then
  echo "usage: $0 <task-or-handoff-file>" >&2
  exit 2
fi

limit_re='usage limit|limit reached|rate limit exceeded|quota exceeded|insufficient_quota'
weak_re='upgrade|resets'
# A successful reply is only a limit notice when it says so plainly; "resets" or "usage limit" alone is ordinary prose.
success_limit_re="you('ve| have) hit your ([a-z0-9-]+ )?limit|(session|weekly|5-hour|usage) limit reached|limit has been reached"
ordinary_re='unauthori[sz]ed|authenticat|not logged in|log ?in required|invalid api key|permission denied|forbidden|ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|network|could not resolve|timed out'
logged_out_re='not logged in|log ?in required'
unsupported_re='model is not supported|not supported when using codex'

out_file=$(mktemp)
err_file=$(mktemp)
trap 'rm -f "$out_file" "$err_file"' EXIT

args=()
[[ "${CODEX_SEARCH:-}" == 1 ]] && args+=(--search)  # top-level option: must precede exec
args+=(exec --sandbox "${CODEX_SANDBOX:-read-only}")
[[ -n "${CODEX_MODEL:-}" ]] && args+=(--model "$CODEX_MODEL")
[[ -n "${CODEX_EFFORT:-}" ]] && args+=(-c "model_reasoning_effort=\"$CODEX_EFFORT\"")
[[ -n "${CODEX_LAST_MESSAGE:-}" ]] && args+=(-o "$CODEX_LAST_MESSAGE")  # exec option: the final message, untruncated
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
  if [[ $(wc -c <"$out_file") -lt 400 ]] && grep -qiE "$success_limit_re" <<<"$(cat "$out_file")"; then
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

# Not a quota limit: no reset will fix it, so the caller marks the engine unavailable and reroutes.
# Only the last error lines count: earlier stderr holds the prompt and transcript, whose code or text
# may mention these phrases. The cause follows the marker so a later login check can lift a logout.
error_tail=$(tail -n 5 "$err_file")
cause=
grep -qiE "$unsupported_re" <<<"$error_tail" && cause=model_unsupported
grep -qiE "$logged_out_re" <<<"$error_tail" && cause=logged_out
if [[ -n "$cause" ]]; then
  echo "CODEX_UNAVAILABLE $cause"
  cat "$out_file" "$err_file" >&2
  exit 44
fi

cat "$out_file"
cat "$err_file" >&2
exit 1
