// Package schema defines the canonical memory form — one markdown file per
// memory, YAML frontmatter plus a markdown body — and the parse, render, and
// validation primitives every other package builds on. Canonical is the single
// source of truth; harness-specific renders are derived from it elsewhere.
package schema

import (
	"errors"
	"fmt"
	"regexp"
	"strings"

	"gopkg.in/yaml.v3"
)

const fence = "---\n"

var kebab = regexp.MustCompile(`^[a-z0-9]+(-[a-z0-9]+)*$`)

var validTypes = map[Type]bool{
	TypeUser: true, TypeFeedback: true, TypeProject: true,
	TypeReference: true, TypeLesson: true, TypePreference: true,
}

// ErrMergeMarkers reports canonical content carrying git conflict markers.
var ErrMergeMarkers = errors.New("content contains git merge-conflict markers")

// ValidationError collects every contract violation found in one memory so an
// agent can fix them in a single pass rather than one round-trip at a time.
type ValidationError struct{ Issues []string }

func (e *ValidationError) Error() string {
	return "invalid memory: " + strings.Join(e.Issues, "; ")
}

// Type is the memory's kind. It constrains how agents weigh and apply a memory.
type Type string

// The canonical memory types.
const (
	TypeUser       Type = "user"
	TypeFeedback   Type = "feedback"
	TypeProject    Type = "project"
	TypeReference  Type = "reference"
	TypeLesson     Type = "lesson"
	TypePreference Type = "preference"
)

// AppliesTo narrows where a memory is relevant. Empty lists mean "no constraint
// on this axis" — the scope filter treats an absent axis as matching anything.
// The struct carries json tags too so an agent can construct a memory as JSON
// and pipe it to `engram remember --from-json -`.
type AppliesTo struct {
	Cwd    []string `yaml:"cwd,omitempty" json:"cwd,omitempty"`
	Agents []string `yaml:"agents,omitempty" json:"agents,omitempty"`
	Hosts  []string `yaml:"hosts,omitempty" json:"hosts,omitempty"`
}

// Provenance records where a memory came from. All fields are ISO-8601 strings
// or free identifiers; none are load-bearing for scope decisions.
type Provenance struct {
	Origin   string `yaml:"origin,omitempty" json:"origin,omitempty"`
	Session  string `yaml:"session,omitempty" json:"session,omitempty"`
	Author   string `yaml:"author,omitempty" json:"author,omitempty"`
	Created  string `yaml:"created,omitempty" json:"created,omitempty"`
	Modified string `yaml:"modified,omitempty" json:"modified,omitempty"`
}

// CanonicalMemory is one authored memory. Name, Description, Type, and Scope are
// required; the rest are optional. Body is the markdown after the frontmatter
// and is preserved verbatim across a parse/render round-trip.
type CanonicalMemory struct {
	Name        string     `yaml:"name" json:"name"`
	Description string     `yaml:"description" json:"description"`
	Type        Type       `yaml:"type" json:"type"`
	Scope       string     `yaml:"scope" json:"scope"`
	AppliesTo   AppliesTo  `yaml:"applies_to,omitempty" json:"applies_to,omitempty"`
	Provenance  Provenance `yaml:"provenance,omitempty" json:"provenance,omitempty"`
	Related     []string   `yaml:"related,omitempty" json:"related,omitempty"`
	Body        string     `yaml:"-" json:"body,omitempty"`
}

// Parse reads a canonical memory file (frontmatter + body). It errors on missing
// or unterminated frontmatter, malformed YAML, or content carrying git
// merge-conflict markers (a malformed file must never be rendered downstream).
func Parse(data []byte) (*CanonicalMemory, error) {
	s := string(data)
	if hasMergeMarkers(s) {
		return nil, ErrMergeMarkers
	}
	if !strings.HasPrefix(s, fence) {
		return nil, fmt.Errorf("missing frontmatter: file must begin with %q", "---")
	}
	rest := s[len(fence):]
	idx := strings.Index(rest, "\n"+fence)
	if idx < 0 {
		return nil, fmt.Errorf("unterminated frontmatter: no closing %q", "---")
	}
	front := rest[:idx]
	body := rest[idx+len("\n"+fence):]

	var m CanonicalMemory
	if err := yaml.Unmarshal([]byte(front), &m); err != nil {
		return nil, fmt.Errorf("frontmatter: %w", err)
	}
	m.Body = body
	return &m, nil
}

// Render serializes a memory back to canonical file bytes such that
// Parse(Render(m)) reconstructs m exactly.
func (m *CanonicalMemory) Render() ([]byte, error) {
	front, err := yaml.Marshal(m)
	if err != nil {
		return nil, fmt.Errorf("marshal frontmatter: %w", err)
	}
	// yaml.Marshal ends front with a newline, so front + fence yields the
	// "\n---\n" delimiter Parse splits on.
	return []byte(fence + string(front) + fence + m.Body), nil
}

// Validate reports whether the memory satisfies the canonical contract: required
// fields present, Type in the known set, Scope well-formed, Name kebab-case.
func (m *CanonicalMemory) Validate() error {
	var issues []string
	if m.Name == "" {
		issues = append(issues, "name is required")
	} else if !kebab.MatchString(m.Name) {
		issues = append(issues, "name must be kebab-case")
	}
	if m.Description == "" {
		issues = append(issues, "description is required")
	}
	switch {
	case m.Type == "":
		issues = append(issues, "type is required")
	case !validTypes[m.Type]:
		issues = append(issues, fmt.Sprintf("type %q is not a known type", m.Type))
	}
	switch {
	case m.Scope == "":
		issues = append(issues, "scope is required")
	case !validScope(m.Scope):
		issues = append(issues, "scope must be 'global' or 'project:<repo>'")
	}
	if len(issues) > 0 {
		return &ValidationError{Issues: issues}
	}
	return nil
}

// validScope accepts the two supported tiers: "global" and "project:<repo>".
func validScope(scope string) bool {
	return scope == "global" || strings.HasPrefix(scope, "project:") && len(scope) > len("project:")
}

// hasMergeMarkers reports whether s carries a git conflict block. Requiring both
// the opening and closing angle-run markers avoids false positives on legitimate
// markdown that merely contains a run of one such character.
func hasMergeMarkers(s string) bool {
	return strings.Contains(s, "<<<<<<< ") && strings.Contains(s, ">>>>>>> ")
}
