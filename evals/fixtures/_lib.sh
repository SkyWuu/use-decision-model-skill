# Shared helpers for fixture build scripts. Source it, do not run it.
# Every fixture is a fresh git repository whose history is fully deterministic:
# fixed author names, e-mails and timestamps, so commit hashes are stable.

fixture_init() {
  OUT="${1:?usage: build.sh <out-dir>}"
  if [ -e "$OUT" ] && [ -n "$(ls -A "$OUT" 2>/dev/null)" ]; then
    echo "refusing to write into non-empty directory: $OUT" >&2
    exit 1
  fi
  mkdir -p "$OUT"
  cd "$OUT"
  git init -q
  git symbolic-ref HEAD refs/heads/main
  git config user.name "Fixture Bot"
  git config user.email "fixture@example.com"
  git config commit.gpgsign false
  git config core.autocrlf false
  git config core.quotepath false
}

_git_as() {
  local author="$1" date="$2"
  shift 2
  local name="${author% <*}" email="${author##*<}"
  email="${email%>}"
  GIT_AUTHOR_NAME="$name" GIT_AUTHOR_EMAIL="$email" GIT_AUTHOR_DATE="$date" \
    GIT_COMMITTER_NAME="$name" GIT_COMMITTER_EMAIL="$email" GIT_COMMITTER_DATE="$date" \
    "$@"
}

# commit "Name <email>" "2026-03-02T10:00:00+08:00" "subject" ["body"]
commit() {
  local author="$1" date="$2" subject="$3" body="${4:-}"
  git add -A
  if [ -n "$body" ]; then
    _git_as "$author" "$date" git commit -q -m "$subject" -m "$body"
  else
    _git_as "$author" "$date" git commit -q -m "$subject"
  fi
}

# revert_head "Name <email>" "date" "reason"  -- a real `git revert` of HEAD with a reason line
revert_head() {
  local author="$1" date="$2" reason="$3"
  local sha subject
  sha="$(git rev-parse HEAD)"
  subject="$(git log -1 --format=%s)"
  git revert --no-commit HEAD
  _git_as "$author" "$date" git commit -q -m "Revert \"$subject\"" -m "$reason" -m "This reverts commit $sha."
}
