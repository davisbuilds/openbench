// Package curate turns a headless agent's judgment into deterministic canonical
// mutations. engram gathers the corpus and hands it to the agent as facts; the
// agent proposes operations (it never touches a file); engram validates every
// proposed operation against the corpus and the schema, then — and only if the
// whole batch is valid — applies it through the store. The trust boundary is
// here: a model proposes, engram is the sole mutator.
package curate

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/davisbuilds/engram/internal/discover"
	"github.com/davisbuilds/engram/internal/lock"
	"github.com/davisbuilds/engram/internal/review"
	"github.com/davisbuilds/engram/internal/schema"
	"github.com/davisbuilds/engram/internal/store"
)

// Operation kinds an agent may propose.
const (
	OpAdd     = "add"
	OpUpdate  = "update"
	OpMerge   = "merge"
	OpRemove  = "remove"
	OpRescope = "rescope"
)

// Corpus is the deterministic input engram hands the agent: every canonical
// memory plus the read-only review findings. It is marshaled into the prompt.
type Corpus struct {
	Memories []*schema.CanonicalMemory `json:"memories"`
	Findings []review.Finding          `json:"findings"`
}

// Operation is one proposed canonical mutation. Which fields are meaningful
// depends on Op; Validate enforces the per-kind contract.
type Operation struct {
	Op      string                  `json:"op"`
	Name    string                  `json:"name,omitempty"`
	Sources []string                `json:"sources,omitempty"`
	ToScope string                  `json:"to_scope,omitempty"`
	Memory  *schema.CanonicalMemory `json:"memory,omitempty"`
	Reason  string                  `json:"reason,omitempty"`
}

// Proposal is the agent's full response: an ordered list of operations.
type Proposal struct {
	Operations []Operation `json:"operations"`
}

// OpResult pairs a proposed operation with its validation verdict.
type OpResult struct {
	Op    Operation `json:"op"`
	Valid bool      `json:"valid"`
	Error string    `json:"error,omitempty"`
}

// AllValid reports whether every operation in the batch passed validation. Apply
// runs only when this holds — engram never partially applies a model proposal it
// could not fully validate.
func AllValid(results []OpResult) bool {
	for _, r := range results {
		if !r.Valid {
			return false
		}
	}
	return true
}

// BuildPrompt renders the curate instruction: the corpus as JSON facts plus the
// exact output contract. The agent is told to emit only a fenced JSON block.
func BuildPrompt(c Corpus) (string, error) {
	blob, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return "", fmt.Errorf("marshal corpus: %w", err)
	}
	var b strings.Builder
	b.WriteString(promptHeader)
	b.WriteString("\n\n## Current canonical corpus\n\n```json\n")
	b.Write(blob)
	b.WriteString("\n```\n\n")
	b.WriteString(promptContract)
	return b.String(), nil
}

// ParseProposal recovers a Proposal from an agent's message. It prefers a fenced
// ```json block; failing that it tries the first balanced JSON object; failing
// that it errors — a proposal engram cannot parse is never guessed at.
func ParseProposal(text string) (Proposal, error) {
	var p Proposal
	raw, ok := fencedJSON(text)
	if !ok {
		raw, ok = firstJSONObject(text)
	}
	if !ok {
		return p, fmt.Errorf("no JSON object found in agent output")
	}
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		return p, fmt.Errorf("parse proposal JSON: %w", err)
	}
	return p, nil
}

// Validate checks every operation against the corpus and the schema, returning a
// per-operation verdict in input order. It threads a working copy of the corpus:
// each valid operation's effect is applied to the working set before the next is
// checked, so an intra-batch conflict (e.g. two adds of the same new name, or an
// update of a name an earlier op removed) is caught rather than both passing
// against the unchanged initial state. It mutates nothing on disk.
func Validate(ops []Operation, corpus []*schema.CanonicalMemory) []OpResult {
	working := map[string]*schema.CanonicalMemory{}
	for _, m := range corpus {
		working[m.Name] = m
	}
	results := make([]OpResult, 0, len(ops))
	for _, op := range ops {
		r := OpResult{Op: op, Valid: true}
		if err := validateOne(op, working); err != nil {
			r.Valid = false
			r.Error = err.Error()
		} else {
			applyToWorkingSet(op, working)
		}
		results = append(results, r)
	}
	return results
}

// applyToWorkingSet advances the simulated corpus by one validated operation so
// later operations in the same batch are checked against the state earlier ones
// would produce. Only names matter for validation, so a rescope is a no-op here.
func applyToWorkingSet(op Operation, working map[string]*schema.CanonicalMemory) {
	switch op.Op {
	case OpAdd, OpUpdate:
		working[op.Memory.Name] = op.Memory
	case OpMerge:
		working[op.Memory.Name] = op.Memory
		for _, s := range op.Sources {
			if s != op.Memory.Name {
				delete(working, s)
			}
		}
	case OpRemove:
		delete(working, op.Name)
	}
}

func validateOne(op Operation, existing map[string]*schema.CanonicalMemory) error {
	switch op.Op {
	case OpAdd:
		if op.Memory == nil {
			return fmt.Errorf("add requires a memory")
		}
		if err := op.Memory.Validate(); err != nil {
			return err
		}
		if _, ok := existing[op.Memory.Name]; ok {
			return fmt.Errorf("add names %q which already exists; use update", op.Memory.Name)
		}
		return nil
	case OpUpdate:
		if op.Name == "" {
			return fmt.Errorf("update requires the target name")
		}
		if _, ok := existing[op.Name]; !ok {
			return fmt.Errorf("update targets unknown memory %q", op.Name)
		}
		if op.Memory == nil {
			return fmt.Errorf("update requires the replacement memory")
		}
		if op.Memory.Name != op.Name {
			return fmt.Errorf("update memory name %q must match target %q (rename is not an update)", op.Memory.Name, op.Name)
		}
		return op.Memory.Validate()
	case OpMerge:
		if len(op.Sources) < 2 {
			return fmt.Errorf("merge requires at least two sources")
		}
		for _, s := range op.Sources {
			if _, ok := existing[s]; !ok {
				return fmt.Errorf("merge source %q does not exist", s)
			}
		}
		if op.Memory == nil {
			return fmt.Errorf("merge requires the merged memory")
		}
		// The merged memory's name must be new or one of the sources; otherwise
		// applying it would force-overwrite an existing, unrelated memory.
		if _, exists := existing[op.Memory.Name]; exists && !contains(op.Sources, op.Memory.Name) {
			return fmt.Errorf("merge target %q already exists and is not among the sources; refusing to overwrite unrelated canonical content", op.Memory.Name)
		}
		return op.Memory.Validate()
	case OpRemove:
		if op.Name == "" {
			return fmt.Errorf("remove requires the target name")
		}
		if _, ok := existing[op.Name]; !ok {
			return fmt.Errorf("remove targets unknown memory %q", op.Name)
		}
		return nil
	case OpRescope:
		if op.Name == "" {
			return fmt.Errorf("rescope requires the target name")
		}
		m, ok := existing[op.Name]
		if !ok {
			return fmt.Errorf("rescope targets unknown memory %q", op.Name)
		}
		if op.ToScope == "" {
			return fmt.Errorf("rescope requires to_scope")
		}
		// Validate the target scope by applying it to a copy and revalidating.
		probe := *m
		probe.Scope = op.ToScope
		return probe.Validate()
	default:
		return fmt.Errorf("unknown operation %q", op.Op)
	}
}

// Applied records what one applied operation did.
type Applied struct {
	Op      string   `json:"op"`
	Name    string   `json:"name"`
	Removed []string `json:"removed,omitempty"`
}

// Apply executes a batch against the canonical root. It acquires the shared lock
// first, then re-discovers and re-validates against the *current* canonical
// state — not the snapshot the agent was shown, which another writer may have
// changed while the (slow) agent ran. The batch applies whole or not at all
// (fail closed): if the proposal no longer validates against current canonical,
// nothing is written. It returns what each operation did, in order.
func Apply(root string, ops []Operation) ([]Applied, error) {
	// Hold the canonical-root lock across discovery, validation, and the whole
	// multi-file mutation so no other writer can change canonical between the
	// check and the writes, and the merge/remove sequence never half-applies.
	release, err := lock.Acquire(root)
	if err != nil {
		return nil, err
	}
	defer release()

	corpus, perrs, derr := discover.Discover(root)
	if derr != nil {
		return nil, fmt.Errorf("re-discover canonical under lock: %w", derr)
	}
	// A malformed canonical file is omitted from the corpus, so an add/merge could
	// silently force-overwrite it. Refuse to mutate a store that does not fully
	// parse; the operator fixes the bad file first.
	if len(perrs) > 0 {
		return nil, fmt.Errorf("refusing to apply: %d canonical file(s) do not parse; fix them before curating", len(perrs))
	}
	results := Validate(ops, corpus)
	if !AllValid(results) {
		return nil, fmt.Errorf("refusing to apply: proposal no longer validates against current canonical (%d of %d invalid)", countInvalid(results), len(results))
	}

	applied := make([]Applied, 0, len(ops))
	for _, op := range ops {
		a, err := applyOne(root, op)
		if err != nil {
			return applied, fmt.Errorf("apply %s: %w", op.Op, err)
		}
		applied = append(applied, a)
	}
	return applied, nil
}

func applyOne(root string, op Operation) (Applied, error) {
	switch op.Op {
	case OpAdd, OpUpdate:
		if _, _, err := store.Save(root, op.Memory, true); err != nil {
			return Applied{}, err
		}
		return Applied{Op: op.Op, Name: op.Memory.Name}, nil
	case OpMerge:
		if _, _, err := store.Save(root, op.Memory, true); err != nil {
			return Applied{}, err
		}
		var removed []string
		for _, s := range op.Sources {
			if s == op.Memory.Name {
				continue // the merged memory reuses this name; keep it
			}
			if _, err := store.Delete(root, s); err != nil {
				return Applied{}, err
			}
			removed = append(removed, s)
		}
		return Applied{Op: op.Op, Name: op.Memory.Name, Removed: removed}, nil
	case OpRemove:
		if _, err := store.Delete(root, op.Name); err != nil {
			return Applied{}, err
		}
		return Applied{Op: op.Op, Name: op.Name, Removed: []string{op.Name}}, nil
	case OpRescope:
		// Load the current on-disk memory so a rescope after an update/merge of
		// the same name in this batch operates on the latest content, not a stale
		// snapshot (which would silently discard the earlier op's change).
		m, _, found, err := store.Load(root, op.Name)
		if err != nil {
			return Applied{}, err
		}
		if !found {
			return Applied{}, fmt.Errorf("rescope target %q not found", op.Name)
		}
		m.Scope = op.ToScope
		if _, _, err := store.Save(root, m, true); err != nil {
			return Applied{}, err
		}
		return Applied{Op: op.Op, Name: op.Name}, nil
	default:
		return Applied{}, fmt.Errorf("unknown operation %q", op.Op)
	}
}

func contains(xs []string, target string) bool {
	for _, x := range xs {
		if x == target {
			return true
		}
	}
	return false
}

func countInvalid(results []OpResult) int {
	n := 0
	for _, r := range results {
		if !r.Valid {
			n++
		}
	}
	return n
}

// fencedJSON extracts the contents of the first ```json fenced block.
func fencedJSON(text string) (string, bool) {
	const open = "```json"
	i := strings.Index(text, open)
	if i < 0 {
		return "", false
	}
	rest := text[i+len(open):]
	j := strings.Index(rest, "```")
	if j < 0 {
		return "", false
	}
	return strings.TrimSpace(rest[:j]), true
}

// firstJSONObject returns the first balanced {...} run, ignoring braces inside
// double-quoted strings. It is the fallback when no fenced block is present.
func firstJSONObject(text string) (string, bool) {
	start := strings.IndexByte(text, '{')
	if start < 0 {
		return "", false
	}
	depth := 0
	inStr := false
	esc := false
	for i := start; i < len(text); i++ {
		c := text[i]
		switch {
		case esc:
			esc = false
		case c == '\\' && inStr:
			esc = true
		case c == '"':
			inStr = !inStr
		case inStr:
			// nothing
		case c == '{':
			depth++
		case c == '}':
			depth--
			if depth == 0 {
				return text[start : i+1], true
			}
		}
	}
	return "", false
}
