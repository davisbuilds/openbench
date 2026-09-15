package importer

import (
	"errors"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/schema"
)

// claudeNative mirrors Claude Code's native memory frontmatter (type lives under
// metadata; there is no scope field).
type claudeNative struct {
	Name        string `yaml:"name"`
	Description string `yaml:"description"`
	Metadata    struct {
		Type   string `yaml:"type"`
		Origin string `yaml:"origin"`
	} `yaml:"metadata"`
}

// ImportClaude reads Claude Code's per-project memory directory and maps each
// native memory file into canonical form. cwd is the project directory the
// memory dir belongs to; it is used to derive a project scope (repo root →
// project:<repo>, non-repo container → global). Files engram itself rendered
// (origin marker) are skipped as the loop guard; the index and non-markdown
// files are ignored. A missing directory yields an empty result.
func ImportClaude(memoryDir, cwd string) (Result, error) {
	scope := projectScopeFromRepo(cwd)
	var res Result
	entries, err := os.ReadDir(memoryDir)
	if errors.Is(err, os.ErrNotExist) {
		return res, nil
	}
	if err != nil {
		return res, err
	}
	for _, e := range entries {
		if e.IsDir() || e.Name() == "MEMORY.md" || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		data, err := os.ReadFile(filepath.Join(memoryDir, e.Name()))
		if err != nil {
			return res, err
		}
		front, body, ok := frontmatterAndBody(data)
		if !ok {
			continue
		}
		var n claudeNative
		if err := yaml.Unmarshal(front, &n); err != nil {
			continue
		}
		if n.Metadata.Origin == marker.Origin {
			res.Skipped = append(res.Skipped, n.Name)
			continue
		}
		res.Memories = append(res.Memories, &schema.CanonicalMemory{
			Name:        n.Name,
			Description: n.Description,
			Type:        importedType(n.Metadata.Type),
			Scope:       scope,
			Body:        body,
			Provenance:  schema.Provenance{Origin: "import:claude-code"},
		})
	}
	return res, nil
}

// importedType defaults an absent or unknown native type to reference.
func importedType(t string) schema.Type {
	if t == "" {
		return schema.TypeReference
	}
	return schema.Type(t)
}
