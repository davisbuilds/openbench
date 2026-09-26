package render

import (
	"fmt"

	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/schema"
)

// CodexInstructions is the companion policy file engram maintains in its Codex
// extension directory. Codex's consolidator reads it to learn how to treat these
// notes; it is not itself a memory.
const CodexInstructions = `# engram extension

Notes in this directory are rendered by engram from a canonical cross-harness
memory store. Each note begins with an
` + "`<!-- engram-sync canonical=<name> scope=<scope> extension=engram -->`" + ` marker.

Treat them as durable, curated memories and fold them into consolidated memory
as you would any extension note. Do not edit these files by hand: engram
overwrites them from canonical on the next sync, so hand edits are lost.
`

// CodexRender is the rendered extension-note content for one memory. sync assigns
// the timestamped filename, so the renderer stays pure and deterministic.
type CodexRender struct {
	Content []byte
}

// CodexRenderer renders canonical memories into Codex extension notes.
type CodexRenderer struct{}

// Render produces the marked note body for one memory. The output is
// deterministic (no timestamp), so re-rendering an unchanged memory yields
// identical bytes and sync stays idempotent.
func (CodexRenderer) Render(m *schema.CanonicalMemory) (CodexRender, error) {
	content := fmt.Sprintf("%s\n\n# %s\n\n%s\n\n%s",
		marker.CodexNoteMarker(m.Name, m.Scope), m.Name, m.Description, m.Body)
	return CodexRender{Content: []byte(content)}, nil
}
