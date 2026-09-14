#!/bin/sh
# Source checkout stays publishable; private state and a non-editable runtime live outside it.
set -eu
umask 077
codex_setup=0
no_start=0
for arg in "$@"; do
    case "$arg" in
        --codex) codex_setup=1 ;;
        --no-start) no_start=1 ;;
        -h|--help)
            printf '%s\n' 'Usage: ./install.sh [--no-start] [--codex]' \
                'CONTEXT_ARCHIVE_HOME must be absolute and outside this checkout.' \
                '--no-start creates the runtime and LaunchAgent without loading or collecting.' \
                '--codex explicitly registers the read-only context-archive MCP server.'
            exit 0 ;;
        *) printf 'Unknown option: %s\n' "$arg" >&2; exit 2 ;;
    esac
done
if [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'Context Archive collection requires macOS.' >&2; exit 1
fi
if [ "$codex_setup" = 1 ] && ! command -v codex >/dev/null 2>&1; then
    printf '%s\n' '--codex requires the Codex CLI.' >&2; exit 1
fi
# Replacing a running interpreter/package tree can corrupt an in-flight capture.
# Refuse before any filesystem or uv mutation, including with --no-start.
if launchctl print "gui/$(id -u)/local.context-archive" >/dev/null 2>&1; then
    printf '%s\n' 'Context Archive is loaded. Run the installed context-archive uninstall first, then rerun this installer. Archive and runtime data are preserved.' >&2
    exit 1
fi
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
archive_home=${CONTEXT_ARCHIVE_HOME:-"$HOME/Library/Application Support/ContextArchive"}
case "$archive_home" in /*) ;; *) printf '%s\n' 'CONTEXT_ARCHIVE_HOME must be absolute.' >&2; exit 2 ;; esac
case "$archive_home/" in "$project_dir/"*) printf '%s\n' 'Choose a state directory outside the source checkout.' >&2; exit 2 ;; esac
mkdir -p "$archive_home"
archive_home=$(CDPATH= cd -- "$archive_home" && pwd -P)
case "$archive_home/" in "$project_dir/"*) printf '%s\n' 'Choose a state directory outside the source checkout.' >&2; exit 2 ;; esac
export CONTEXT_ARCHIVE_HOME="$archive_home"
mkdir -p "$archive_home/tools/bin" "$archive_home/data"
if command -v uv >/dev/null 2>&1; then
    uv_bin=$(command -v uv)
elif [ -x "$archive_home/tools/bin/uv" ]; then
    uv_bin="$archive_home/tools/bin/uv"
else
    installer=$(mktemp)
    trap 'rm -f "$installer"' EXIT HUP INT TERM
    curl --proto '=https' --tlsv1.2 -fLsS 'https://astral.sh/uv/0.10.6/install.sh' -o "$installer"
    UV_UNMANAGED_INSTALL="$archive_home/tools/bin" UV_NO_MODIFY_PATH=1 sh "$installer"
    rm -f "$installer"
    trap - EXIT HUP INT TERM
    uv_bin="$archive_home/tools/bin/uv"
fi
# uv's interpreter installation and caches are private to this app, not shared installs.
export UV_PROJECT_ENVIRONMENT="$archive_home/runtime"
export UV_PYTHON_INSTALL_DIR="$archive_home/python"
export UV_CACHE_DIR="$archive_home/cache"
"$uv_bin" sync --project "$project_dir" --locked --no-dev --no-editable \
    --python 3.12 --managed-python --reinstall-package context-archive
set -- install --archive "$archive_home/data/archive.sqlite3" --config "$archive_home/config.json"
if [ "$no_start" = 1 ]; then set -- "$@" --no-start; fi
if [ "$codex_setup" = 1 ]; then set -- "$@" --codex; fi
"$archive_home/runtime/bin/context-archive" "$@"
printf '%s\n' 'Runtime and LaunchAgent installed. No sources were imported.' \
    'Use context-archive doctor to check readability; Full Disk Access must be granted manually if needed.' \
    'Grant access to the permission_executable printed above; then verify background capture status.'
