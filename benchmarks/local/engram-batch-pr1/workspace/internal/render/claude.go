// Package render turns canonical memories into each harness's native form. A
// renderer is pure: it produces file content and index lines and performs no
// I/O. sync owns the filesystem, marker discipline, and idempotent apply, so the
// same rendered bytes can be compared against disk or written without either
// concern leaking into the renderer.
package render

import (
	"fmt"

	"gopkg.in/yaml.v3"

	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/schema"
)

// ClaudeRender is the rendered output for one memory in Claude Code's
// per-project memory form.
type ClaudeRender struct {
	// FileName is the memory file's basename within the project memory dir.
	FileName string
	// Content is the full <name>.md file, carrying metadata.origin so sync can
	// later recognize the file as engram-owned.
	Content []byte
	// IndexLine is the single MEMORY.md line for this memory, carrying the
	// name-anchored index marker.
	IndexLine string
}

// ClaudeRenderer renders canonical memories into Claude Code's memory form: a
// typed-frontmatter file per memory plus a marked MEMORY.md index line.
type ClaudeRenderer struct{}

// claudeFrontmatter mirrors the frontmatter shape Claude Code itself writes,
// extended with metadata.origin as engram's provenance marker.
type claudeFrontmatter struct {
	Name        string         `yaml:"name"`
	Description string         `yaml:"description"`
	Metadata    claudeMetadata `yaml:"metadata"`
}

type claudeMetadata struct {
	Type   string `yaml:"type"`
	Origin string `yaml:"origin"`
}

// Render produces the Claude Code memory file and index line for one memory.
func (ClaudeRenderer) Render(m *schema.CanonicalMemory) (ClaudeRender, error) {
	front, err := yaml.Marshal(claudeFrontmatter{
		Name:        m.Name,
		Description: m.Description,
		Metadata: claudeMetadata{
			Type:   string(m.Type),
			Origin: marker.Origin,
		},
	})
	if err != nil {
		return ClaudeRender{}, fmt.Errorf("marshal claude frontmatter: %w", err)
	}
	return ClaudeRender{
		FileName:  m.Name + ".md",
		Content:   []byte("---\n" + string(front) + "---\n" + m.Body),
		IndexLine: fmt.Sprintf("- [%s](%s.md) — %s %s", m.Name, m.Name, m.Description, marker.ClaudeIndexMarker(m.Name)),
	}, nil
}
