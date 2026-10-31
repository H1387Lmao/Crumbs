#!/bin/sh

main() {
    REPO="https://github.com/H1387Lmao/crumbs.git"
    INSTALL_DIR="$HOME/crumbs"

    if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
        BOLD="$(printf '\033[1m')"
        DIM="$(printf '\033[2m')"
        RED="$(printf '\033[31m')"
        GREEN="$(printf '\033[32m')"
        YELLOW="$(printf '\033[33m')"
        BLUE="$(printf '\033[34m')"
        RESET="$(printf '\033[0m')"
    else
        BOLD=""; DIM=""; RED=""; GREEN=""
        YELLOW=""; BLUE=""; RESET=""
    fi

    log() {
        level="$1"
        color="$2"
        shift 2
        printf "%s%s[%s]%s    %s\n" "$BOLD" "$color" "$level" "$RESET" "$*"
    }

    info()    { log info    "$BLUE"   "$@"; }
    success() { log success "$GREEN"  "$@"; }
    warning() { log warning "$YELLOW" "$@"; }
    error()   { log error   "$RED"    "$@" >&2; }

    confirm() {
        printf "%s[?]%s    %s [y/N] " "$BOLD" "$RESET" "$1"
        if [ ! -t 0 ]; then
            printf "\n"
            return 1
        fi
        IFS= read -r answer || return 1
        case "$answer" in
            y|Y|yes|YES|Yes) return 0 ;;
            *) return 1 ;;
        esac
    }

    if ! command -v git >/dev/null 2>&1; then
        error "Git is not installed."
        info "Install Git before continuing."
        return 1
    fi

    PYTHON_EXEC="python3"
    if ! command -v python3 >/dev/null 2>&1; then
        if ! command -v python >/dev/null 2>&1; then
            error "Python is not installed."
            info "Install Python before continuing."
            return 1
        fi
        PYTHON_EXEC="python"
    fi

    if [ -e "$INSTALL_DIR" ]; then
        error "Installation directory already exists: $INSTALL_DIR"
        info "Remove or move it before running the installer again."
        return 1
    fi

    info "Cloning Crumbs into $INSTALL_DIR"
    if ! git clone "$REPO" "$INSTALL_DIR"; then
        error "Failed to clone the repository."
        return 1
    fi
    success "Repository cloned."

    COOKIE="$INSTALL_DIR/cookie.py"
    CRUMBS="$INSTALL_DIR/crumbs.py"

    if [ ! -f "$COOKIE" ]; then
        error "cookie.py was not found in the repository."
        return 1
    fi
    if [ ! -f "$CRUMBS" ]; then
        error "crumbs.py was not found in the repository."
        return 1
    fi

    chmod +x "$COOKIE" "$CRUMBS" || {
        error "Could not set executable permissions."
        return 1
    }
    success "Executable permissions configured."

    SHELL_NAME="${SHELL##*/}"

    case "$SHELL_NAME" in
        bash) ACTIVE_RC="$HOME/.bashrc" ;;
        zsh)  ACTIVE_RC="$HOME/.zshrc"  ;;
        ksh)  ACTIVE_RC="$HOME/.kshrc"  ;;
        ash)  ACTIVE_RC="$HOME/.ashrc"  ;;
        *)    ACTIVE_RC="$HOME/.profile" ;;
    esac

    info "Detected shell: $SHELL_NAME"
    info "Primary configuration: $ACTIVE_RC"

    has_existing_alias() {
        name="$1"
        for rc in \
            "$HOME/.bashrc" \
            "$HOME/.zshrc" \
            "$HOME/.kshrc" \
            "$HOME/.ashrc" \
            "$HOME/.profile"
        do
            [ -f "$rc" ] || continue
            if grep -Eq \
                "^[[:space:]]*(alias[[:space:]]+$name=|function[[:space:]]+$name([[:space:]]|\\()|$name[[:space:]]*\\(\\))" \
                "$rc"
            then
                return 0
            fi
        done
        return 1
    }

    has_existing_command() {
        command -v "$1" >/dev/null 2>&1
    }

    ADD_COOKIE=1
    ADD_CRUMBS=1

    if has_existing_command cookie || has_existing_alias cookie; then
        warning "An existing cookie command or alias was detected."
        if confirm "Replace cookie with the Crumbs installation?"; then
            ADD_COOKIE=1
        else
            ADD_COOKIE=0
            warning "Skipping cookie alias."
        fi
    fi

    if has_existing_command crumbs || has_existing_alias crumbs; then
        warning "An existing crumbs command or alias was detected."
        if confirm "Replace crumbs with the Crumbs installation?"; then
            ADD_CRUMBS=1
        else
            ADD_CRUMBS=0
            warning "Skipping crumbs alias."
        fi
    fi

    if [ "$ADD_COOKIE" -eq 0 ] && [ "$ADD_CRUMBS" -eq 0 ]; then
        warning "No aliases will be installed."
        return 0
    fi

    # Build a newline-separated list of existing rc files.
    RC_FILES=""
    for rc in \
        "$HOME/.bashrc" \
        "$HOME/.zshrc" \
        "$HOME/.kshrc" \
        "$HOME/.ashrc" \
        "$HOME/.profile"
    do
        if [ -f "$rc" ]; then
            RC_FILES="${RC_FILES}${rc}
"
        fi
    done

    # Make sure the active shell's rc is in the list (even if it didn't exist).
    case "
$RC_FILES" in
        *"
$ACTIVE_RC
"*) ;;
        *)
            RC_FILES="${RC_FILES}${ACTIVE_RC}
"
            ;;
    esac

    BLOCK_START="# >>> Crumbs aliases >>>"
    BLOCK_END="# <<< Crumbs aliases <<<"

    COOKIE_ALIAS="alias cookie='$PYTHON_EXEC \"$COOKIE\"'"
    CRUMBS_ALIAS="alias crumbs='$PYTHON_EXEC \"$CRUMBS\"'"

    printf '%s' "$RC_FILES" | while IFS= read -r rc; do
        [ -n "$rc" ] || continue

        touch "$rc" || {
            warning "Could not access $rc. Skipping."
            continue
        }

        if grep -Fq "$BLOCK_START" "$rc"; then
            tmp="$rc.crumbs-tmp"
            if ! awk -v start="$BLOCK_START" -v end="$BLOCK_END" '
                $0 == start { skip = 1; next }
                $0 == end   { skip = 0; next }
                !skip       { print }
            ' "$rc" > "$tmp"; then
                warning "Could not process $rc. Skipping."
                rm -f "$tmp"
                continue
            fi
            if ! mv "$tmp" "$rc"; then
                warning "Could not update $rc. Skipping."
                rm -f "$tmp"
                continue
            fi
        fi

        {
            printf '\n%s\n' "$BLOCK_START"
            if [ "$ADD_COOKIE" -eq 1 ]; then
                printf '%s\n' "$COOKIE_ALIAS"
            fi
            if [ "$ADD_CRUMBS" -eq 1 ]; then
                printf '%s\n' "$CRUMBS_ALIAS"
            fi
            printf '%s\n' "$BLOCK_END"
        } >> "$rc" || {
            warning "Could not write to $rc."
            continue
        }

        success "Updated $rc"
    done

    success "Crumbs installation completed."
    printf '\n'
    info "Installation directory: $INSTALL_DIR"

    if [ "$ADD_COOKIE" -eq 1 ]; then
        info "cookie -> $COOKIE"
    fi
    if [ "$ADD_CRUMBS" -eq 1 ]; then
        info "crumbs -> $CRUMBS"
    fi

    printf '\n'
    warning "Restart your terminal or reload your shell configuration."
    printf '\n'
    printf "  %scookie init%s\n"   "$BOLD" "$RESET"
    printf "  %scookie run%s\n"    "$BOLD" "$RESET"
    printf "  %scookie build%s\n"  "$BOLD" "$RESET"
    printf "  %scrumbs --help%s\n" "$BOLD" "$RESET"
    printf '\n'
}

main "$@"
exit $?
