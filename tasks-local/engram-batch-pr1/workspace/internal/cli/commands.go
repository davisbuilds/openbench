package cli

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/davisbuilds/engram/internal/config"
	"github.com/davisbuilds/engram/internal/discover"
	"github.com/davisbuilds/engram/internal/harness"
	"github.com/davisbuilds/engram/internal/lock"
	"github.com/davisbuilds/engram/internal/marker"
	"github.com/davisbuilds/engram/internal/schema"
	"github.com/davisbuilds/engram/internal/scope"
	"github.com/davisbuilds/engram/internal/slug"
	"github.com/davisbuilds/engram/internal/sync"
)

// canonLock takes the exclusive canonical-root lock shared by every canonical
// mutator (remember, share, import --apply, curate --apply), so no two writers
// interleave — closing the load-then-write race in store.Save between two
// concurrent same-name writes, and keeping a multi-file batch atomic against a
// single write. A held lock yields a retryable "locked" error rather than a
// silent second writer.
func canonLock(root string) (func(), *RespError) {
	release, err := lock.Acquire(root)
	if err != nil {
		return nil, &RespError{Code: "locked", Message: err.Error()}
	}
	return release, nil
}

// session resolves the cwd/host context and loads config once, shared by the
// sync/audit/list/discover commands.
type session struct {
	cfg           *config.Config
	cwd           string
	host          string
	agentOverride string
}

func (e *env) newSession() (*session, *RespError) {
	cfg, err := config.Load(e.config)
	if err != nil {
		return nil, &RespError{Code: "config_load", Message: err.Error()}
	}
	cwd := e.cwd
	if cwd == "" {
		wd, werr := os.Getwd()
		if werr != nil {
			return nil, &RespError{Code: "cwd", Message: werr.Error()}
		}
		cwd = wd
	}
	return &session{cfg: cfg, cwd: cwd, host: e.resolveHost(cfg), agentOverride: e.agent}, nil
}

// agentFor returns the effective agent for scope filtering: an explicit --agent
// override wins, otherwise the harness's own native agent name.
func (s *session) agentFor(native string) string {
	if s.agentOverride != "" {
		return s.agentOverride
	}
	return native
}

// targets builds a reconcilable target for every enabled harness, filtering the
// discovered memories per harness (the agent axis differs by harness).
func (s *session) targets() ([]sync.Target, []string, *RespError) {
	mems, perrs, err := discover.Discover(s.cfg.CanonicalRoot)
	if err != nil {
		return nil, nil, &RespError{Code: "discover", Message: err.Error()}
	}
	warns := warnParseErrors(perrs)

	var targets []sync.Target
	if h := s.cfg.Harnesses[config.HarnessClaude]; h.Enabled() {
		rel := scope.RelevantFor(mems, s.cwd, s.agentFor("claude"), s.host)
		targets = append(targets, sync.ClaudeTarget{
			MemoryDir: claudeMemoryDir(h.Home, s.cwd), Desired: rel,
		})
		warns = append(warns, harnessWarnings(harness.CheckClaude(h.Home, true))...)
	} else {
		warns = append(warns, "claude-code disabled; skipped")
	}
	if h := s.cfg.Harnesses[config.HarnessCodex]; h.Enabled() {
		rel := scope.RelevantFor(mems, s.cwd, s.agentFor("codex"), s.host)
		targets = append(targets, sync.CodexTarget{
			ExtensionDir: codexExtDir(h.Home), Desired: rel, Now: time.Now,
		})
		warns = append(warns, harnessWarnings(harness.CheckCodex(h.Home, true))...)
	} else {
		warns = append(warns, "codex disabled; skipped")
	}
	return targets, warns, nil
}

func cmdSync(e *env, name string, _ []string) int {
	s, rerr := e.newSession()
	if rerr != nil {
		e.emit(name, false, nil, nil, rerr, nil)
		return exitError
	}
	targets, warns, rerr := s.targets()
	if rerr != nil {
		e.emit(name, false, nil, warns, rerr, nil)
		return exitError
	}
	if len(targets) == 0 {
		e.emit(name, false, nil, warns, &RespError{Code: "no_harness", Message: "no enabled harness to sync"}, nil)
		return exitUsage
	}

	exit := exitOK
	var next []NextStep
	entries := make([]map[string]any, 0, len(targets))
	for _, tg := range targets {
		entry := map[string]any{"harness": tg.Harness()}
		if !e.apply {
			actions, err := tg.Plan()
			if err != nil {
				entry["error"] = err.Error()
				exit = worseExit(exit, exitError)
			} else {
				entry["actions"] = orEmpty(actions)
				exit = worseExit(exit, exitForActions(actions))
				next = append(next, conflictNextSteps(actions)...)
			}
		} else {
			res, err := tg.Apply()
			if err != nil {
				entry["error"] = err.Error()
				exit = worseExit(exit, exitError)
			} else {
				entry["result"] = res
				if len(res.Conflicts) > 0 {
					exit = worseExit(exit, exitConflicts)
					next = append(next, conflictNextSteps(res.Conflicts)...)
				}
			}
		}
		entries = append(entries, entry)
	}
	e.emit(name, exit == exitOK, map[string]any{
		"cwd": s.cwd, "host": s.host, "apply": e.apply, "harnesses": entries,
	}, warns, nil, next)
	return exit
}

func cmdAudit(e *env, name string, _ []string) int {
	s, rerr := e.newSession()
	if rerr != nil {
		e.emit(name, false, nil, nil, rerr, nil)
		return exitError
	}
	targets, warns, rerr := s.targets()
	if rerr != nil {
		e.emit(name, false, nil, warns, rerr, nil)
		return exitError
	}

	exit := exitOK
	var next []NextStep
	entries := make([]map[string]any, 0, len(targets))
	for _, tg := range targets {
		entry := map[string]any{"harness": tg.Harness()}
		actions, err := tg.Plan()
		if err != nil {
			entry["error"] = err.Error()
			exit = worseExit(exit, exitError)
		} else {
			entry["actions"] = orEmpty(actions)
			exit = worseExit(exit, exitForActions(actions))
			next = append(next, conflictNextSteps(actions)...)
		}
		entries = append(entries, entry)
	}
	e.emit(name, exit == exitOK, map[string]any{
		"cwd": s.cwd, "host": s.host, "harnesses": entries,
	}, warns, nil, next)
	return exit
}

func cmdList(e *env, name string, _ []string) int {
	s, rerr := e.newSession()
	if rerr != nil {
		e.emit(name, false, nil, nil, rerr, nil)
		return exitError
	}
	mems, perrs, err := discover.Discover(s.cfg.CanonicalRoot)
	if err != nil {
		e.emit(name, false, nil, warnParseErrors(perrs), &RespError{Code: "discover", Message: err.Error()}, nil)
		return exitError
	}
	relevant := scope.RelevantFor(mems, s.cwd, s.agentFor("claude"), s.host)
	e.emit(name, true, map[string]any{
		"cwd": s.cwd, "host": s.host, "memories": memoryItems(relevant),
	}, warnParseErrors(perrs), nil, nil)
	return exitOK
}

func cmdDiscover(e *env, name string, _ []string) int {
	cfg, err := config.Load(e.config)
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "config_load", Message: err.Error()}, nil)
		return exitError
	}
	mems, perrs, derr := discover.Discover(cfg.CanonicalRoot)
	if derr != nil {
		e.emit(name, false, nil, warnParseErrors(perrs), &RespError{Code: "discover", Message: derr.Error()}, nil)
		return exitError
	}
	e.emit(name, true, map[string]any{
		"canonical_root": cfg.CanonicalRoot, "count": len(mems), "memories": memoryItems(mems),
	}, warnParseErrors(perrs), nil, nil)
	return exitOK
}

// cmdDiff shows the full per-memory reconciliation status for each harness,
// including the memories already in sync — a superset of audit's pending actions.
func cmdDiff(e *env, name string, _ []string) int {
	s, rerr := e.newSession()
	if rerr != nil {
		e.emit(name, false, nil, nil, rerr, nil)
		return exitError
	}
	targets, warns, rerr := s.targets()
	if rerr != nil {
		e.emit(name, false, nil, warns, rerr, nil)
		return exitError
	}
	exit := exitOK
	entries := make([]map[string]any, 0, len(targets))
	for _, tg := range targets {
		entry := map[string]any{"harness": tg.Harness()}
		actions, err := tg.Plan()
		if err != nil {
			entry["error"] = err.Error()
			exit = worseExit(exit, exitError)
			entries = append(entries, entry)
			continue
		}
		byName := map[string]sync.ActionKind{}
		for _, a := range actions {
			byName[a.Name] = a.Kind
		}
		var items []map[string]string
		for _, m := range tg.DesiredMemories() {
			status := "in-sync"
			if k, ok := byName[m.Name]; ok {
				status = string(k)
				delete(byName, m.Name)
			}
			items = append(items, map[string]string{"name": m.Name, "status": status})
		}
		for nm, k := range byName {
			items = append(items, map[string]string{"name": nm, "status": string(k)})
		}
		sort.Slice(items, func(i, j int) bool { return items[i]["name"] < items[j]["name"] })
		entry["memories"] = items
		exit = worseExit(exit, exitForActions(actions))
		entries = append(entries, entry)
	}
	e.emit(name, exit == exitOK, map[string]any{"cwd": s.cwd, "host": s.host, "harnesses": entries}, warns, nil, nil)
	return exit
}

// cmdShow dumps a harness's engram-rendered memories. Reading a disabled harness
// is permissive: it proceeds with a warning (contrast import, which is strict).
func cmdShow(e *env, name string, args []string) int {
	var harness string
	for _, a := range args {
		if !strings.HasPrefix(a, "-") && harness == "" {
			harness = a
		}
	}
	if harness == "" {
		e.emit(name, false, nil, nil, &RespError{Code: "usage", Message: "usage: engram show <claude-code|codex>"}, nil)
		return exitUsage
	}
	s, rerr := e.newSession()
	if rerr != nil {
		e.emit(name, false, nil, nil, rerr, nil)
		return exitError
	}

	var (
		warns []string
		items []map[string]string
	)
	switch harness {
	case config.HarnessClaude:
		h := s.cfg.Harnesses[config.HarnessClaude]
		if !h.Enabled() {
			warns = append(warns, "claude-code is disabled; showing anyway (read is permissive)")
		}
		items = showClaude(claudeMemoryDir(h.Home, s.cwd))
	case config.HarnessCodex:
		h := s.cfg.Harnesses[config.HarnessCodex]
		if !h.Enabled() {
			warns = append(warns, "codex is disabled; showing anyway (read is permissive)")
		}
		items = showCodex(filepath.Join(codexExtDir(h.Home), "notes"))
	default:
		e.emit(name, false, nil, nil, &RespError{Code: "unknown_harness", Message: "harness must be claude-code or codex"}, nil)
		return exitUsage
	}
	sort.Slice(items, func(i, j int) bool { return items[i]["name"] < items[j]["name"] })
	e.emit(name, true, map[string]any{"harness": harness, "count": len(items), "memories": items}, warns, nil, nil)
	return exitOK
}

func showClaude(dir string) []map[string]string {
	items := []map[string]string{}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return items
	}
	for _, e := range entries {
		if e.IsDir() || e.Name() == "MEMORY.md" || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		path := filepath.Join(dir, e.Name())
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if strings.Contains(string(data), "origin: "+marker.Origin) {
			items = append(items, map[string]string{"name": strings.TrimSuffix(e.Name(), ".md"), "path": path})
		}
	}
	return items
}

func showCodex(notesDir string) []map[string]string {
	items := []map[string]string{}
	entries, err := os.ReadDir(notesDir)
	if err != nil {
		return items
	}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".md") {
			continue
		}
		path := filepath.Join(notesDir, e.Name())
		data, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if n, scp, ok := marker.CodexNoteName(string(data)); ok {
			items = append(items, map[string]string{"name": n, "scope": scp, "path": path})
		}
	}
	return items
}

// resolveHost maps the current machine to its configured host label. An explicit
// --host wins; an unmapped hostname yields "" so host-scoped memories fail closed.
func (e *env) resolveHost(cfg *config.Config) string {
	if e.host != "" {
		return e.host
	}
	h, _ := os.Hostname()
	if i := strings.IndexByte(h, '.'); i >= 0 {
		h = h[:i]
	}
	label, _ := cfg.HostLabel(h)
	return label
}

func claudeMemoryDir(claudeHome, cwd string) string {
	return filepath.Join(claudeHome, "projects", slug.ForCwd(cwd), "memory")
}

func codexExtDir(codexHome string) string {
	return filepath.Join(codexHome, "memories", "extensions", marker.Extension)
}

func memoryItems(mems []*schema.CanonicalMemory) []map[string]string {
	items := make([]map[string]string, 0, len(mems))
	for _, m := range mems {
		items = append(items, map[string]string{"name": m.Name, "scope": m.Scope, "type": string(m.Type)})
	}
	return items
}

func warnParseErrors(perrs []discover.ParseError) []string {
	if len(perrs) == 0 {
		return nil
	}
	w := make([]string, 0, len(perrs))
	for _, p := range perrs {
		w = append(w, "unparseable canonical file "+p.Path+": "+p.Err.Error())
	}
	return w
}

func exitForActions(actions []sync.Action) int {
	for _, a := range actions {
		if a.Kind == sync.Conflict {
			return exitConflicts
		}
	}
	return exitOK
}

// orEmpty coerces a nil slice to an empty one so JSON consumers always see a
// list to iterate, never null. Agent-first: the envelope's iterated arrays are
// stable types.
func orEmpty[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

// worseExit returns the more significant of two exit codes: a hard error
// outranks conflicts, which outrank success.
func worseExit(a, b int) int {
	prio := map[int]int{exitOK: 0, exitConflicts: 1, exitError: 2, exitUsage: 2}
	if prio[b] > prio[a] {
		return b
	}
	return a
}

// conflictNextSteps turns each CONFLICT into an agent-consumable lead.
func conflictNextSteps(actions []sync.Action) []NextStep {
	var steps []NextStep
	for _, a := range actions {
		if a.Kind == sync.Conflict {
			steps = append(steps, NextStep{
				Reason:  "unmarked file at " + a.Path + " blocks rendering " + a.Name,
				Command: "remove or rename the hand-authored file, then re-run engram sync",
			})
		}
	}
	return steps
}
