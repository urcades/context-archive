#!/bin/bash
# Builds a self-contained local app. Does not install or register any service.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/dist" "$ROOT/.cache/uv"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$ROOT/.cache/uv}"
export MACOSX_DEPLOYMENT_TARGET=14.0
export CLANG_MODULE_CACHE_PATH="$ROOT/.cache/clang"
export SWIFTPM_MODULECACHE_OVERRIDE="$ROOT/.cache/swift-modules"
CONFIGURATION="${CA_BUILD_CONFIGURATION:-debug}"
# Increment the build number when replacing an installed app; ServiceManagement
# tracks the registered bundle version. Use a stable certificate when available.
SIGNING_IDENTITY="${CA_CODESIGN_IDENTITY:--}"
UV="${CA_BUILD_UV:-$(command -v uv)}"
PYTHON="${CA_BUILD_PYTHON:-$($UV python find --managed-python 3.12)}"
APP="$ROOT/dist/Context Archive.app"
# Assemble separately so a failed build never leaves a partly replaced bundle.
# Stage outside synced Documents folders: File Provider metadata can reappear
# while signing and make an otherwise clean app fail codesign.
STAGE="$(mktemp -d "${TMPDIR:-/private/tmp}/context-archive-build.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
BUNDLE="$STAGE/Context Archive.app"
RESOURCES="$BUNDLE/Contents/Resources"
mkdir -p "$RESOURCES/engine" "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Library/LaunchAgents"

# A managed standalone distribution includes its own dylibs and stdlib. Reject
# symlinks escaping that distribution rather than silently shipping a dependency
# on this developer machine. Internal links are converted to relative links.
"$PYTHON" - "$RESOURCES/python" <<'PY'
import os, pathlib, shutil, sys
source = pathlib.Path(sys.base_prefix).resolve()
destination = pathlib.Path(sys.argv[1])
if sys.version_info[:2] != (3, 12):
    raise SystemExit('CA_BUILD_PYTHON must be a standalone Python 3.12 interpreter')
links = []
for root, dirs, files in os.walk(source, followlinks=False):
    for name in dirs + files:
        path = pathlib.Path(root) / name
        if path.is_symlink():
            resolved = path.resolve()
            if not resolved.is_relative_to(source):
                raise SystemExit(f'External Python symlink cannot be bundled: {path} -> {resolved}')
            links.append((path.relative_to(source), resolved.relative_to(source)))
shutil.copytree(source, destination, symlinks=True)
for relative, target in links:
    path = destination / relative
    path.unlink()
    path.symlink_to(os.path.relpath(destination / target, path.parent))
PY

"$UV" export --locked --no-dev --no-emit-project --format requirements-txt --output-file "$STAGE/requirements.txt"
"$UV" pip install --python "$PYTHON" --target "$RESOURCES/engine" --require-hashes -r "$STAGE/requirements.txt"
"$UV" build --wheel --out-dir "$STAGE/wheels"
"$UV" pip install --python "$PYTHON" --target "$RESOURCES/engine" --no-deps "$STAGE"/wheels/*.whl
# Installed console scripts contain build-time shebangs. Swift launches the
# bundled interpreter directly, so no executable scripts are needed here.
rm -rf "$RESOURCES/engine/bin"
# Wheel installation records its temporary local URL; this provenance is not
# needed at runtime and should not expose the developer's checkout path.
"$PYTHON" - "$RESOURCES/engine" <<'PY_METADATA'
import pathlib, sys
for metadata in pathlib.Path(sys.argv[1]).glob('*.dist-info/direct_url.json'):
    metadata.unlink()
PY_METADATA
cp "$STAGE/requirements.txt" "$RESOURCES/requirements.txt"
cp LICENSE "$RESOURCES/LICENSE"

swift build --package-path "$ROOT/macos" --configuration "$CONFIGURATION" --arch arm64
BIN="$(swift build --package-path "$ROOT/macos" --configuration "$CONFIGURATION" --arch arm64 --show-bin-path)"
cp "$BIN/ContextArchiveApp" "$BUNDLE/Contents/MacOS/ContextArchiveApp"
cp "$BIN/ContextArchiveAgent" "$BUNDLE/Contents/MacOS/ContextArchiveAgent"
cp "$BIN/context-archive" "$BUNDLE/Contents/MacOS/context-archive"
cp macos/Packaging/Info.plist "$BUNDLE/Contents/Info.plist"
"$PYTHON" - "$BUNDLE/Contents/Info.plist" "${CA_BUILD_NUMBER:-}" <<'PY_VERSION'
import pathlib, plistlib, re, sys
path = pathlib.Path(sys.argv[1])
info = plistlib.loads(path.read_bytes())
version = sys.argv[2] or info['CFBundleVersion']
# Numeric release build identifiers (up to three components), following Apple's
# CFBundleVersion component limits. Development suffixes are intentionally unused.
if not re.fullmatch(r'[1-9][0-9]{0,3}(?:\.[0-9]{1,2}){0,2}', version):
    raise SystemExit('CA_BUILD_NUMBER must be a positive build number, optionally followed by two 0-99 components')
info['CFBundleVersion'] = version
path.write_bytes(plistlib.dumps(info))
PY_VERSION
cp macos/Packaging/io.github.urcades.ContextArchive.agent.plist "$BUNDLE/Contents/Library/LaunchAgents/"

# Finder metadata copied from local binaries can invalidate bundle signing.
# Clear only our disposable staging copy, never the source runtime or live app.
/usr/bin/xattr -cr "$BUNDLE"

# Sign every nested Mach-O (Python extension modules and dylibs included), then
# seal the enclosing app. Local ad-hoc signing intentionally adds no sandbox.
"$PYTHON" - "$BUNDLE" "$SIGNING_IDENTITY" <<'PY'
import pathlib, subprocess, sys
bundle = pathlib.Path(sys.argv[1])
# Swift linker signatures otherwise derive identifiers from each executable's
# UUID, which changes on relink. Keep identities stable across bundle updates.
stable_identifiers = {
    'Contents/MacOS/ContextArchiveApp': 'io.github.urcades.ContextArchive',
    'Contents/MacOS/ContextArchiveAgent': 'io.github.urcades.ContextArchive.agent',
    'Contents/MacOS/context-archive': 'io.github.urcades.ContextArchive.cli',
    'Contents/Resources/python/bin/python3.12': 'io.github.urcades.ContextArchive.python',
}
magic = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca', b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}
for path in sorted(bundle.rglob('*')):
    if path.is_symlink() or not path.is_file():
        continue
    with path.open('rb') as stream:
        is_macho = stream.read(4) in magic
    if is_macho:
        command = ['/usr/bin/codesign', '--force', '--sign', sys.argv[2]]
        identifier = stable_identifiers.get(path.relative_to(bundle).as_posix())
        if identifier:
            command += ['--identifier', identifier]
        subprocess.run(command + [str(path)], check=True)
PY
/usr/bin/codesign --force --sign "$SIGNING_IDENTITY" "$BUNDLE"
/usr/bin/codesign --verify --deep --strict "$BUNDLE"
# Only the build output is replaced; an installed or running app is untouched.
rm -rf "$APP"
mv "$BUNDLE" "$APP"
/usr/bin/codesign --verify --deep --strict "$APP"
printf 'Built %s\n' "$APP"
