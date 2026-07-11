#!/bin/bash
# Sync our authored Hermes plugins to the AgentEgo repository
# Runs daily — keeps plugins versioned alongside the mood system

SRC="/home/cflux/.hermes/plugins"
DEST="/mnt/LargeStorage/AgentEgo/plugins"
REPO="/mnt/LargeStorage/AgentEgo"

# Plugins we authored (not mnemosyne — that's built-in)
PLUGINS=("mood_injector" "time_injector" "prefill_injector")

# Also sync scripts and SOUL.md
SCRIPTS_SRC="/home/cflux/.hermes/scripts"
SOUL_SRC="/home/cflux/.hermes"

for plugin in "${PLUGINS[@]}"; do
    if [ -d "$SRC/$plugin" ]; then
        mkdir -p "$DEST/$plugin"
        cp -r "$SRC/$plugin"/* "$DEST/$plugin/"
        echo "Synced: $plugin"
    fi
done

# Sync scripts
mkdir -p "$REPO/scripts"
cp "$SCRIPTS_SRC"/*.sh "$SCRIPTS_SRC"/*.py "$REPO/scripts/" 2>/dev/null
echo "Synced: scripts/"

# Sync SOUL.md files
cp "$SOUL_SRC/SOUL.md" "$REPO/hermes/becca-soul.md" 2>/dev/null
cp "$SOUL_SRC/profiles/tala/SOUL.md" "$REPO/hermes/tala-soul.md" 2>/dev/null
echo "Synced: SOUL.md files"

# Run test runner first — don't commit if tests fail
echo "Running tests..."
python3 /home/cflux/.hermes/scripts/test_runner.py 2>&1
if [ $? -ne 0 ]; then
    echo "Tests failed — skipping commit"
    exit 1
fi
echo "Tests passed — proceeding with commit"
cd "$REPO"
git add plugins/ scripts/ hermes/
git diff --cached --quiet || git commit -m "chore: sync hermex plugins $(date +%Y-%m-%d)" 2>/dev/null
echo "Committed"