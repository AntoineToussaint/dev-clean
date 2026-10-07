#!/usr/bin/env bash
# Installs `dev-cleaner` to ~/.local/bin. Works on macOS and Linux.
# Usage: ./install.sh [--dev] [--yes] [--no-completions] [--no-rc-edit]
#   --dev             symlink instead of copy (edits to source propagate)
#   --yes             non-interactive: auto-install uv if missing, auto-edit shell rc
#   --no-completions  skip shell completions
#   --no-rc-edit      print the completion rc lines instead of appending them

set -euo pipefail

DEV=0
YES=0
COMPLETIONS=1
RC_EDIT=1
for arg in "$@"; do
    case "$arg" in
        --dev) DEV=1 ;;
        --yes|-y) YES=1 ;;
        --no-completions) COMPLETIONS=0 ;;
        --no-rc-edit) RC_EDIT=0 ;;
        -h|--help)
            sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

NAME="dev-cleaner"
REPO="https://github.com/AntoineToussaint/dev-clean"
REMOTE_URL="https://raw.githubusercontent.com/AntoineToussaint/dev-clean/main/dev_cleaner.py"

# Helper: append lines to an rc file, idempotently and with a marker comment.
# Args: $1 = rc path, $2 = grep-test string (skip if found), $3+ = lines to append
rc_append() {
    local rc="$1"; shift
    local needle="$1"; shift
    if [ -f "$rc" ] && grep -qF "$needle" "$rc" 2>/dev/null; then
        return 1  # already present
    fi
    {
        printf '\n# Added by %s installer (%s)\n' "$NAME" "$REPO"
        printf '%s\n' "$@"
    } >> "$rc"
    return 0
}

# Helper: yes/no prompt that works under `curl | bash`. Default = Y.
ask_yes() {
    local prompt="$1"
    if [ "$YES" -eq 1 ]; then return 0; fi
    if [ ! -e /dev/tty ]; then return 1; fi
    printf "  ${B}%s${R} [Y/n] " "$prompt"
    local reply
    read -r reply < /dev/tty 2>/dev/null || return 1
    case "${reply:-y}" in y|Y|yes|"") return 0 ;; *) return 1 ;; esac
}

# Colors (TTY only)
if [ -t 1 ]; then
    B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'
    GRN=$'\033[32m'; YEL=$'\033[33m'; RED=$'\033[31m'; MAG=$'\033[35m'
else
    B=""; D=""; R=""; GRN=""; YEL=""; RED=""; MAG=""
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s✓%s %s\n' "$GRN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$YEL" "$R" "$*"; }
err()  { printf '  %s✗%s %s\n' "$RED" "$R" "$*" >&2; }

say ""
say "${MAG}${B}🧹  ${NAME} installer${R}"
say "${D}    sweep stale build dirs & dev caches${R}"
say ""

# Locate source script: next to this installer (git clone) or fetch from GitHub (curl|bash)
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
LOCAL_SRC="$HERE/dev_cleaner.py"
TMP_SRC=""

if [ -n "$HERE" ] && [ -f "$LOCAL_SRC" ]; then
    SRC="$LOCAL_SRC"
    ok "using local source: $SRC"
else
    if [ "$DEV" -eq 1 ]; then
        err "--dev requires running install.sh from the cloned repo"
        exit 1
    fi
    TMP_SRC="$(mktemp -t dev-cleaner.XXXXXX.py)"
    trap 'rm -f "$TMP_SRC"' EXIT
    if ! curl -fsSL "$REMOTE_URL" -o "$TMP_SRC"; then
        err "failed to fetch script from $REMOTE_URL"
        exit 1
    fi
    SRC="$TMP_SRC"
    ok "fetched source: $REMOTE_URL"
fi

# Detect uv
if ! command -v uv >/dev/null 2>&1; then
    warn "uv not found"
    if ask_yes "install uv via the official installer?"; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # Pick up uv on PATH for the rest of this script
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        command -v uv >/dev/null || { err "uv install failed"; exit 1; }
        ok "uv installed"
    else
        err "uv is required. install from https://docs.astral.sh/uv/ and re-run."
        exit 1
    fi
else
    ok "uv found: $(command -v uv)"
fi

# Install destination
DEST_DIR="$HOME/.local/bin"
DEST="$DEST_DIR/$NAME"
mkdir -p "$DEST_DIR"

if [ -e "$DEST" ] || [ -L "$DEST" ]; then
    rm -f "$DEST"
fi

if [ "$DEV" -eq 1 ]; then
    ln -s "$SRC" "$DEST"
    ok "symlinked $DEST → $SRC"
else
    cp "$SRC" "$DEST"
    chmod +x "$DEST"
    ok "installed $DEST"
fi

# Migrate from rust-clean (the previous name of this tool)
OLD_BIN="$DEST_DIR/rust-clean"
if [ -e "$OLD_BIN" ] || [ -L "$OLD_BIN" ]; then
    rm -f "$OLD_BIN" \
          "${ZDOTDIR:-$HOME}/.zsh/completions/_rust-clean" \
          "$HOME/.local/share/rust-clean/completion.bash" \
          "$HOME/.config/fish/completions/rust-clean.fish" 2>/dev/null || true
    ok "removed legacy rust-clean binary + completions"
fi
OLD_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/rust-clean/config.toml"
NEW_CFG="${XDG_CONFIG_HOME:-$HOME/.config}/$NAME/config.toml"
if [ -f "$OLD_CFG" ] && [ ! -f "$NEW_CFG" ]; then
    warn "found legacy config $OLD_CFG — $NAME reads it until you run ${B}$NAME --init${R}"
fi

# PATH check
case ":$PATH:" in
    *":$DEST_DIR:"*)
        ok "$DEST_DIR is on PATH"
        ;;
    *)
        warn "$DEST_DIR is not on PATH"
        case "${SHELL:-}" in
            */zsh)  RC="~/.zshrc" ;;
            */bash) RC="~/.bashrc (or ~/.bash_profile on macOS)" ;;
            */fish) RC="~/.config/fish/config.fish" ;;
            *)      RC="your shell rc file" ;;
        esac
        say ""
        say "  add this to $RC:"
        say "    ${B}export PATH=\"\$HOME/.local/bin:\$PATH\"${R}"
        ;;
esac

# Shell completions
if [ "$COMPLETIONS" -eq 1 ]; then
    case "${SHELL:-}" in
        */zsh)
            ZSH_DIR="${ZDOTDIR:-$HOME}/.zsh/completions"
            mkdir -p "$ZSH_DIR"
            "$DEST" --completion zsh > "$ZSH_DIR/_$NAME"
            ok "zsh completions → $ZSH_DIR/_$NAME"
            ZRC="${ZDOTDIR:-$HOME}/.zshrc"
            if ! grep -qF ".zsh/completions" "$ZRC" 2>/dev/null; then
                if [ "$RC_EDIT" -eq 1 ] && ask_yes "add fpath + compinit lines to $ZRC?"; then
                    rc_append "$ZRC" ".zsh/completions" \
                        'fpath=(~/.zsh/completions $fpath)' \
                        'autoload -Uz compinit && compinit' && ok "updated $ZRC (restart your shell)"
                else
                    say "  ${D}to enable, add to $ZRC:${R}"
                    say "    ${B}fpath=(~/.zsh/completions \$fpath)${R}"
                    say "    ${B}autoload -Uz compinit && compinit${R}"
                fi
            fi
            ;;
        */bash)
            BASH_DIR="$HOME/.local/share/$NAME"
            mkdir -p "$BASH_DIR"
            "$DEST" --completion bash > "$BASH_DIR/completion.bash"
            ok "bash completions → $BASH_DIR/completion.bash"
            if [ "$(uname -s)" = "Darwin" ]; then BRC="$HOME/.bash_profile"; else BRC="$HOME/.bashrc"; fi
            LINE="source \"$BASH_DIR/completion.bash\""
            if ! grep -qF "$BASH_DIR/completion.bash" "$BRC" 2>/dev/null; then
                if [ "$RC_EDIT" -eq 1 ] && ask_yes "add source line to $BRC?"; then
                    rc_append "$BRC" "$BASH_DIR/completion.bash" "$LINE" && ok "updated $BRC (restart your shell)"
                else
                    say "  ${D}to enable, add to $BRC:${R}"
                    say "    ${B}$LINE${R}"
                fi
            fi
            ;;
        */fish)
            FISH_DIR="$HOME/.config/fish/completions"
            mkdir -p "$FISH_DIR"
            "$DEST" --completion fish > "$FISH_DIR/$NAME.fish"
            ok "fish completions → $FISH_DIR/$NAME.fish (auto-loaded)"
            ;;
        *)
            warn "unknown shell '${SHELL:-}' — skipping completions (run: $NAME --completion <bash|zsh|fish>)"
            ;;
    esac
fi

# Offer the setup wizard if no config exists yet
if [ ! -f "$NEW_CFG" ] && [ "$YES" -ne 1 ] && [ -e /dev/tty ]; then
    say ""
    printf "  ${B}Run setup wizard now to pick scan roots? [Y/n]${R} "
    read -r REPLY < /dev/tty 2>/dev/null || REPLY="n"
    case "${REPLY:-y}" in
        y|Y|yes|"")
            say ""
            # Reconnect stdin to /dev/tty so rich.Prompt works under `curl | bash`.
            "$DEST" --init < /dev/tty
            ;;
        *)
            say "  ${D}skipped. run ${B}$NAME --init${R}${D} later to configure.${R}"
            ;;
    esac
fi

say ""
say "${GRN}${B}✓ installed${R}  — try:"
say "    ${B}$NAME --dry-run${R}"
say ""
