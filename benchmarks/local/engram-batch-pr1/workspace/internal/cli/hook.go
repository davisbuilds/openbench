package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

const hookCommand = "engram sync --apply --quiet"

// cmdHook prints the Claude Code settings.json fragment that wires engram's sync
// to session boundaries. Codex has no lifecycle hooks, so its capture stays
// agent-wrapped; that is stated in the accompanying note.
func cmdHook(e *env, name string, args []string) int {
	var sub string
	for _, a := range args {
		if !strings.HasPrefix(a, "-") && sub == "" {
			sub = a
		}
	}
	if sub != "" && sub != "print" {
		e.emit(name, false, nil, nil, &RespError{Code: "usage", Message: "usage: engram hook print"}, nil)
		return exitUsage
	}

	oneHook := []any{map[string]any{
		"hooks": []any{map[string]string{"type": "command", "command": hookCommand}},
	}}
	fragment := map[string]any{
		"hooks": map[string]any{
			"SessionStart": oneHook,
			"Stop":         oneHook,
		},
	}
	note := "Merge this fragment into ~/.claude/settings.json. Codex has no lifecycle " +
		"hooks; run `engram sync --apply` at session end there (or via an agent capture flow)."

	if e.jsonMode {
		e.emit(name, true, map[string]any{
			"target":            "~/.claude/settings.json",
			"settings_fragment": fragment,
		}, []string{note}, nil, nil)
		return exitOK
	}

	// Human mode: emit the paste-ready fragment on stdout, the guidance on stderr.
	b, err := json.MarshalIndent(fragment, "", "  ")
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "encode", Message: err.Error()}, nil)
		return exitError
	}
	fmt.Println(string(b))
	fmt.Fprintln(os.Stderr, note)
	return exitOK
}
