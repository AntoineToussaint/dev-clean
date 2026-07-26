#!/usr/bin/env bash
# Installs `rust-clean` to ~/.local/bin. Works on macOS and Linux.
# Usage: ./install.sh [--dev] [--yes]
#   --dev   symlink instead of copy (edits to source propagate)
#   --yes   non-interactive: auto-install uv if missing

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
            sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Helper: append lines to an rc file, idempotently and with a marker comment.
# Args: $1 = rc path, $2 = grep-test string (skip if found), $3+ = lines to append
rc_append() {
    local rc="$1"; shift
    local needle="$1"; shift
    if [ -f "$rc" ] && grep -qF "$needle" "$rc" 2>/dev/null; then
        return 1  # already present
    fi
    {
        printf '\n# Added by rust-clean installer (https://github.com/AntoineToussaint/rust-clean)\n'
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
say "${MAG}${B}🦀  rust-clean installer${R}"
say "${D}    cross-platform installer for the rust target cleaner${R}"
say ""

# Locate source script: next to this installer (git clone) or fetch from GitHub (curl|bash)
REMOTE_URL="https://raw.githubusercontent.com/AntoineToussaint/rust-clean/main/clean_rust_targets.py"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
LOCAL_SRC="$HERE/clean_rust_targets.py"
TMP_SRC=""

if [ -n "$HERE" ] && [ -f "$LOCAL_SRC" ]; then
    SRC="$LOCAL_SRC"
    ok "using local source: $SRC"
else
    if [ "$DEV" -eq 1 ]; then
        err "--dev requires running install.sh from the cloned repo"
        exit 1
    fi
    TMP_SRC="$(mktemp -t rust-clean.XXXXXX.py)"
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
    if [ "$YES" -eq 1 ]; then
        REPLY="y"
    else
        printf "  install uv via the official installer? [Y/n] "
        read -r REPLY < /dev/tty || REPLY="n"
    fi
    case "${REPLY:-y}" in
        y|Y|yes|"")
            curl -LsSf https://astral.sh/uv/install.sh | sh
            # Pick up uv on PATH for the rest of this script
            export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
            command -v uv >/dev/null || { err "uv install failed"; exit 1; }
            ok "uv installed"
            ;;
        *)
            err "uv is required. install from https://docs.astral.sh/uv/ and re-run."
            exit 1 ;;
    esac
else
    ok "uv found: $(command -v uv)"
fi

# Detect cargo (warn only)
if command -v cargo >/dev/null 2>&1; then
    ok "cargo found: $(command -v cargo)"
else
    warn "cargo not found — install Rust from https://rustup.rs to actually clean"
fi

# Install destination
DEST_DIR="$HOME/.local/bin"
DEST="$DEST_DIR/rust-clean"
mkdir -p "$DEST_DIR"

# Remove old entry if present
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
            "$DEST" --completion zsh > "$ZSH_DIR/_rust-clean"
            ok "zsh completion → $ZSH_DIR/_rust-clean"
            ZRC="${ZDOTDIR:-$HOME}/.zshrc"
            if [ -f "$ZRC" ] && grep -qF "$ZSH_DIR" "$ZRC" 2>/dev/null; then
                ok "$ZRC already references $ZSH_DIR"
            elif [ "$RC_EDIT" -eq 1 ] && ask_yes "add the fpath/compinit lines to $ZRC?"; then
                if rc_append "$ZRC" "$ZSH_DIR" \
                    "fpath=($ZSH_DIR \$fpath)" \
                    "autoload -Uz compinit && compinit"; then
                    ok "wired up completions in $ZRC"
                    say "  ${D}restart your shell, or run: ${B}source $ZRC && rm -f ~/.zcompdump*${R}"
                fi
            else
                say ""
                say "  ${D}add to $ZRC to enable:${R}"
                say "    ${B}fpath=($ZSH_DIR \$fpath)${R}"
                say "    ${B}autoload -Uz compinit && compinit${R}"
            fi
            ;;
        */bash)
            BASH_DIR="$HOME/.local/share/rust-clean"
            mkdir -p "$BASH_DIR"
            "$DEST" --completion bash > "$BASH_DIR/completion.bash"
            ok "bash completion → $BASH_DIR/completion.bash"
            if [ "$(uname -s)" = "Darwin" ]; then BRC="$HOME/.bash_profile"; else BRC="$HOME/.bashrc"; fi
            if [ -f "$BRC" ] && grep -qF "$BASH_DIR/completion.bash" "$BRC" 2>/dev/null; then
                ok "$BRC already sources rust-clean completion"
            elif [ "$RC_EDIT" -eq 1 ] && ask_yes "source the completion file from $BRC?"; then
                if rc_append "$BRC" "$BASH_DIR/completion.bash" \
                    "source $BASH_DIR/completion.bash"; then
                    ok "wired up completions in $BRC"
                    say "  ${D}restart your shell, or run: ${B}source $BRC${R}"
                fi
            else
                say ""
                say "  ${D}add to $BRC to enable:${R}"
                say "    ${B}source $BASH_DIR/completion.bash${R}"
            fi
            ;;
        */fish)
            FISH_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/fish/completions"
            mkdir -p "$FISH_DIR"
            "$DEST" --completion fish > "$FISH_DIR/rust-clean.fish"
            ok "fish completion → $FISH_DIR/rust-clean.fish [auto-loaded]"
            ;;
        *)
            warn "unknown shell (\$SHELL=${SHELL:-unset}); skipping completions"
            warn "run: rust-clean --completion {bash,zsh,fish} > <your-completion-dir>"
            ;;
    esac
fi

# Optional: bootstrap config via `rust-clean --init`
CFG="${XDG_CONFIG_HOME:-$HOME/.config}/rust-clean/config.toml"
if [ ! -f "$CFG" ] && [ "$YES" -ne 1 ] && [ -e /dev/tty ]; then
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
            say "  ${D}skipped. run ${B}rust-clean --init${R}${D} later to configure.${R}"
            ;;
    esac
fi

say ""
say "${GRN}${B}✓ installed${R}  — try:"
say "    ${B}rust-clean --dry-run${R}"
say ""
