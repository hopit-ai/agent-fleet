#!/usr/bin/env bash
# Install agent-fleet: the `fleet` dispatcher + the orchestrator skill for both apps.
# Everything it writes is listed at the end so you can undo it.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FLEET_DIR="${FLEET_HOME:-$HOME/.fleet}"
BIN_DIR="${FLEET_BIN_DIR:-$HOME/.local/bin}"
CLAUDE_SKILLS="$HOME/.claude/skills"
CODEX_SKILLS="$HOME/.codex/skills"

echo "Installing agent-fleet"
echo

mkdir -p "$FLEET_DIR/bin" "$BIN_DIR"
install -m 0755 "$SRC/bin/fleet" "$FLEET_DIR/bin/fleet"
echo "  dispatcher   $FLEET_DIR/bin/fleet"

if [ -f "$FLEET_DIR/roster.json" ]; then
  install -m 0644 "$SRC/roster.json" "$FLEET_DIR/roster.json.new"
  echo "  roster       kept your existing $FLEET_DIR/roster.json (new one at roster.json.new)"
else
  install -m 0644 "$SRC/roster.json" "$FLEET_DIR/roster.json"
  echo "  roster       $FLEET_DIR/roster.json  (edit this to change routing)"
fi

ln -sf "$FLEET_DIR/bin/fleet" "$BIN_DIR/fleet"
echo "  on PATH      $BIN_DIR/fleet -> $FLEET_DIR/bin/fleet"

installed_skill=0
for target in "$CLAUDE_SKILLS:Claude Code" "$CODEX_SKILLS:Codex"; do
  dir="${target%%:*}"; label="${target##*:}"
  parent="$(dirname "$dir")"
  if [ -d "$parent" ]; then
    mkdir -p "$dir/agent-fleet"
    install -m 0644 "$SRC/SKILL.md" "$dir/agent-fleet/SKILL.md"
    echo "  skill        $dir/agent-fleet/SKILL.md  ($label)"
    installed_skill=1
  else
    echo "  skill        skipped $label - $parent does not exist"
  fi
done
[ "$installed_skill" = 1 ] || echo "  WARNING: no skill installed; is either app set up?"

echo
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "  NOTE: $BIN_DIR is not on your PATH. Add to ~/.zshrc:"
     echo "        export PATH=\"\$HOME/.local/bin:\$PATH\""
     echo ;;
esac

"$FLEET_DIR/bin/fleet" doctor || true

cat <<EOF

Done. Try it:

  fleet doctor --auth              # verify both backends can actually run
  fleet route --all                # see the routing matrix
  fleet run "..." --kind implement --complexity low --cwd /path/to/repo

In Claude Code or Codex, just ask: "use the fleet to ..." and the orchestrator
skill loads.

To uninstall:
  rm -rf "$FLEET_DIR" "$BIN_DIR/fleet" \\
         "$CLAUDE_SKILLS/agent-fleet" "$CODEX_SKILLS/agent-fleet"
EOF
