package cli

import (
	"encoding/json"
	"flag"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/davisbuilds/engram/internal/config"
	"github.com/davisbuilds/engram/internal/importer"
	"github.com/davisbuilds/engram/internal/schema"
	"github.com/davisbuilds/engram/internal/store"
)

// multiFlag collects a repeatable string flag (e.g. --applies-cwd a --applies-cwd b).
type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }
func (m *multiFlag) Set(v string) error {
	*m = append(*m, v)
	return nil
}

func cmdRemember(e *env, name string, args []string) int {
	fs := flag.NewFlagSet("remember", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	var (
		nm       = fs.String("name", "", "memory name (kebab-case)")
		desc     = fs.String("description", "", "one-line description")
		typ      = fs.String("type", "", "memory type")
		scp      = fs.String("scope", "global", "scope tier")
		body     = fs.String("body", "", "markdown body")
		fromJSON = fs.String("from-json", "", "read a full memory as JSON from this path (- for stdin)")
		force    = fs.Bool("force", false, "overwrite a differing canonical memory of the same name")
	)
	var cwds, agents, hosts, related multiFlag
	fs.Var(&cwds, "applies-cwd", "cwd glob (repeatable)")
	fs.Var(&agents, "applies-agent", "agent filter (repeatable)")
	fs.Var(&hosts, "applies-host", "host label (repeatable)")
	fs.Var(&related, "related", "related memory name (repeatable)")
	if err := fs.Parse(args); err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "usage", Message: err.Error()}, nil)
		return exitUsage
	}

	m, rerr := buildMemory(*fromJSON, *nm, *desc, *typ, *scp, *body, cwds, agents, hosts, related)
	if rerr != nil {
		e.emit(name, false, nil, nil, rerr, nil)
		return exitUsage
	}
	if m.Provenance.Origin == "" {
		m.Provenance.Origin = "remember"
	}
	if err := m.Validate(); err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "invalid_memory", Message: err.Error()}, nil)
		return exitUsage
	}

	cfg, err := config.Load(e.config)
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "config_load", Message: err.Error()}, nil)
		return exitError
	}
	release, lerr := canonLock(cfg.CanonicalRoot)
	if lerr != nil {
		e.emit(name, false, nil, nil, lerr, nil)
		return exitError
	}
	defer release()
	outcome, path, err := store.Save(cfg.CanonicalRoot, m, *force)
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "save", Message: err.Error()}, nil)
		return exitError
	}
	data := map[string]any{"outcome": outcome, "name": m.Name, "scope": m.Scope, "path": path}
	if outcome == store.Conflict {
		e.emit(name, false, data, nil,
			&RespError{Code: "canonical_conflict", Message: "a different canonical memory named " + m.Name + " exists; pass --force to overwrite intentionally"},
			[]NextStep{{
				Reason:  "a differing canonical memory of this name already exists",
				Command: "re-run engram remember with --force to overwrite it intentionally",
			}})
		return exitConflicts
	}
	e.emit(name, true, data, nil, nil, nil)
	return exitOK
}

func cmdShare(e *env, name string, args []string) int {
	var memName, to string
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case strings.HasPrefix(a, "--to"):
			to, i = flagValue(args, i)
		case !strings.HasPrefix(a, "-") && memName == "":
			memName = a
		}
	}
	if memName == "" || to == "" {
		e.emit(name, false, nil, nil, &RespError{Code: "usage", Message: "usage: engram share <name> --to <scope>"}, nil)
		return exitUsage
	}

	cfg, err := config.Load(e.config)
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "config_load", Message: err.Error()}, nil)
		return exitError
	}
	// Lock across load-modify-save so a concurrent writer cannot slip between
	// reading the current scope and writing the moved one.
	release, lerr := canonLock(cfg.CanonicalRoot)
	if lerr != nil {
		e.emit(name, false, nil, nil, lerr, nil)
		return exitError
	}
	defer release()
	m, _, found, err := store.Load(cfg.CanonicalRoot, memName)
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "load", Message: err.Error()}, nil)
		return exitError
	}
	if !found {
		e.emit(name, false, nil, nil, &RespError{Code: "not_found", Message: "no canonical memory named " + memName}, nil)
		return exitUsage
	}
	from := m.Scope
	m.Scope = to
	if err := m.Validate(); err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "invalid_scope", Message: err.Error()}, nil)
		return exitUsage
	}
	// Sharing is a deliberate edit, so it overwrites its own canonical file.
	outcome, path, err := store.Save(cfg.CanonicalRoot, m, true)
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "save", Message: err.Error()}, nil)
		return exitError
	}
	e.emit(name, true, map[string]any{
		"outcome": outcome, "name": memName, "from_scope": from, "to_scope": to, "path": path,
	}, nil, nil, nil)
	return exitOK
}

func cmdImport(e *env, name string, args []string) int {
	var harness string
	for _, a := range args {
		if !strings.HasPrefix(a, "-") && harness == "" {
			harness = a
		}
	}
	if harness == "" {
		e.emit(name, false, nil, nil, &RespError{Code: "usage", Message: "usage: engram import <claude-code|codex> [--apply]"}, nil)
		return exitUsage
	}
	s, rerr := e.newSession()
	if rerr != nil {
		e.emit(name, false, nil, nil, rerr, nil)
		return exitError
	}

	var (
		res importer.Result
		err error
	)
	switch harness {
	case config.HarnessClaude:
		h := s.cfg.Harnesses[config.HarnessClaude]
		if !h.Enabled() {
			e.emit(name, false, nil, nil, &RespError{Code: "harness_disabled", Message: "claude-code is disabled; cannot import from it"}, nil)
			return exitUsage
		}
		res, err = importer.ImportClaude(claudeMemoryDir(h.Home, s.cwd), s.cwd)
	case config.HarnessCodex:
		h := s.cfg.Harnesses[config.HarnessCodex]
		if !h.Enabled() {
			e.emit(name, false, nil, nil, &RespError{Code: "harness_disabled", Message: "codex is disabled; cannot import from it"}, nil)
			return exitUsage
		}
		res, err = importer.ImportCodex(filepath.Join(h.Home, "memories", "MEMORY.md"))
	default:
		e.emit(name, false, nil, nil, &RespError{Code: "unknown_harness", Message: "harness must be claude-code or codex"}, nil)
		return exitUsage
	}
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "import", Message: err.Error()}, nil)
		return exitError
	}

	base := map[string]any{
		"harness": harness, "apply": e.apply,
		"skipped": orEmpty(res.Skipped), "would_import": len(res.Memories),
	}
	var warns []string
	if res.StaleWarning {
		base["stale_warning"] = true
		warns = append(warns, "Codex MEMORY.md is older than 30 days; the consolidator may be stalled and this import may lag reality")
	}
	if !e.apply {
		base["memories"] = memoryItems(res.Memories)
		e.emit(name, true, base, warns, nil, nil)
		return exitOK
	}

	release, lerr := canonLock(s.cfg.CanonicalRoot)
	if lerr != nil {
		e.emit(name, false, base, warns, lerr, nil)
		return exitError
	}
	defer release()

	outcomes := make([]map[string]string, 0, len(res.Memories))
	conflicts := 0
	for _, m := range res.Memories {
		if verr := m.Validate(); verr != nil {
			outcomes = append(outcomes, map[string]string{"name": m.Name, "outcome": "invalid", "error": verr.Error()})
			continue
		}
		outcome, _, serr := store.Save(s.cfg.CanonicalRoot, m, false)
		if serr != nil {
			e.emit(name, false, base, nil, &RespError{Code: "save", Message: serr.Error()}, nil)
			return exitError
		}
		if outcome == store.Conflict {
			conflicts++
		}
		outcomes = append(outcomes, map[string]string{"name": m.Name, "outcome": string(outcome)})
	}
	base["results"] = outcomes
	if conflicts > 0 {
		e.emit(name, false, base, warns, nil, nil)
		return exitConflicts
	}
	e.emit(name, true, base, warns, nil, nil)
	return exitOK
}

// buildMemory assembles a memory from --from-json input, or from the individual
// flags when no JSON source is given.
func buildMemory(fromJSON, nm, desc, typ, scp, body string, cwds, agents, hosts, related multiFlag) (*schema.CanonicalMemory, *RespError) {
	if fromJSON != "" {
		data, err := readInput(fromJSON)
		if err != nil {
			return nil, &RespError{Code: "read_input", Message: err.Error()}
		}
		var m schema.CanonicalMemory
		if err := json.Unmarshal(data, &m); err != nil {
			return nil, &RespError{Code: "invalid_json", Message: err.Error()}
		}
		return &m, nil
	}
	return &schema.CanonicalMemory{
		Name:        nm,
		Description: desc,
		Type:        schema.Type(typ),
		Scope:       scp,
		AppliesTo:   schema.AppliesTo{Cwd: cwds, Agents: agents, Hosts: hosts},
		Related:     related,
		Body:        body,
	}, nil
}

// readInput reads from stdin when spec is "-", otherwise from the named file.
func readInput(spec string) ([]byte, error) {
	if spec == "-" {
		return io.ReadAll(os.Stdin)
	}
	return os.ReadFile(spec)
}
