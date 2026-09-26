// Package cli implements engram's command dispatch and the agent-first response
// envelope shared by every subcommand. See docs/cli.md for the interface
// contract this package realizes.
package cli

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/davisbuilds/engram/internal/agentexec"
	"github.com/davisbuilds/engram/internal/version"
)

// schemaVersion is the version of the response envelope below. Bump it only on a
// breaking change to the envelope shape, never for command-specific payloads.
const schemaVersion = 1

// Exit codes are a stable contract: an agent operator branches on these before
// parsing any output. Keep them in sync with docs/cli.md.
const (
	exitOK        = 0 // success, including "no actions needed"
	exitError     = 1 // runtime/unexpected error (I/O, parse, internal)
	exitUsage     = 2 // usage/validation error, or a write to a disabled harness
	exitConflicts = 3 // CONFLICT actions present and unresolved
)

// Response is the stable envelope every command emits under --json. One shape
// lets an agent parse any command's result uniformly.
type Response struct {
	SchemaVersion int        `json:"schema_version"`
	Command       string     `json:"command"`
	OK            bool       `json:"ok"`
	Data          any        `json:"data,omitempty"`
	Warnings      []string   `json:"warnings,omitempty"`
	Error         *RespError `json:"error"`
	NextSteps     []NextStep `json:"next_steps,omitempty"`
}

// RespError is a machine-branchable error: a stable code plus a human message.
type RespError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

// NextStep is an agent-consumable lead: why to act, and the exact invocation.
type NextStep struct {
	Reason  string `json:"reason"`
	Command string `json:"command"`
}

// env carries per-invocation flags resolved from the command line. engram holds
// no other process state.
type env struct {
	jsonMode bool
	quiet    bool
	apply    bool
	config   string
	cwd      string
	agent    string
	host     string
	// runner executes a headless agent argv. It defaults to the real subprocess
	// runner in Run and is swapped for a fake in tests so the curate loop is
	// exercised without spawning a paid model.
	runner agentexec.Runner
}

// command is one entry in engram's subcommand table.
type command struct {
	name    string
	summary string
	run     func(e *env, name string, args []string) int
}

// commands returns the full subcommand table. The table is the single source of
// truth for dispatch, `help`, and `help --json`.
func commands() []command {
	return []command{
		{"remember", "Author a canonical memory (flags, or --from-json - on stdin).", cmdRemember},
		{"share", "Move a memory to a different scope tier (writes canonical).", cmdShare},
		{"sync", "Render canonical memories into the harnesses (dry-run; --apply to write).", cmdSync},
		{"import", "Reverse-sync a harness's native memory into canonical (one-shot; --apply).", cmdImport},
		{"discover", "Parse and list every canonical memory, with parse errors.", cmdDiscover},
		{"list", "List memories relevant to a given cwd / agent / host.", cmdList},
		{"audit", "Report pending render actions for a harness without writing.", cmdAudit},
		{"diff", "Show the cross-state difference for each render target.", cmdDiff},
		{"show", "Dump a harness's engram-rendered memories.", cmdShow},
		{"review", "Health report: near-dupe names + promotion candidates, emitted as agent next_steps.", cmdReview},
		{"curate", "Run a headless agent over the corpus; it proposes add/merge/remove/rescope, engram applies (dry-run; --apply).", cmdCurate},
		{"hook", "Print harness lifecycle wiring for session-boundary sync.", cmdHook},
		{"config", "Show config + per-harness readiness (is each harness set up to read what engram writes?).", cmdConfig},
		{"schema", "Emit engram's JSON schemas (self-describing).", cmdSchema},
		{"version", "Print the engram version.", cmdVersion},
	}
}

// Run dispatches a single stateless invocation and returns its exit code.
func Run(args []string) int {
	e := &env{jsonMode: !isTTY(os.Stdout), runner: agentexec.ExecRunner}

	var sub string
	rest := make([]string, 0, len(args))
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == "--json":
			e.jsonMode = true
		case a == "--plain":
			e.jsonMode = false
		case a == "-q" || a == "--quiet":
			e.quiet = true
		case a == "--apply":
			e.apply = true
		case a == "--version":
			return cmdVersion(e, "version", nil)
		case (a == "-h" || a == "--help") && sub == "":
			return e.usage(exitOK)
		case strings.HasPrefix(a, "--config"):
			e.config, i = flagValue(args, i)
		case strings.HasPrefix(a, "--cwd"):
			e.cwd, i = flagValue(args, i)
		case strings.HasPrefix(a, "--agent"):
			e.agent, i = flagValue(args, i)
		case strings.HasPrefix(a, "--host"):
			e.host, i = flagValue(args, i)
		case sub == "" && !strings.HasPrefix(a, "-"):
			sub = a
		default:
			rest = append(rest, a)
		}
	}

	if sub == "" || sub == "help" {
		return e.usage(exitOK)
	}
	for _, c := range commands() {
		if c.name == sub {
			return c.run(e, c.name, rest)
		}
	}
	e.emit(sub, false, nil, nil, &RespError{
		Code:    "unknown_command",
		Message: "unknown command: " + sub + " (try `engram help`)",
	}, nil)
	return exitUsage
}

// emit writes the response in the resolved output mode. JSON and all primary
// data go to stdout; human diagnostics go to stderr. warnings is part of the
// stable response-envelope contract; it is threaded through now and populated
// once the sync/audit commands that surface non-fatal notes land.
//
//nolint:unparam // warnings is a deliberate envelope field, not dead yet
func (e *env) emit(cmd string, ok bool, data any, warnings []string, respErr *RespError, next []NextStep) {
	if e.jsonMode {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(Response{
			SchemaVersion: schemaVersion,
			Command:       cmd,
			OK:            ok,
			Data:          data,
			Warnings:      warnings,
			Error:         respErr,
			NextSteps:     next,
		})
		return
	}
	for _, w := range warnings {
		fmt.Fprintf(os.Stderr, "warning: %s\n", w)
	}
	if respErr != nil {
		fmt.Fprintf(os.Stderr, "engram %s: %s\n", cmd, respErr.Message)
		return
	}
	if e.quiet {
		return
	}
	if data != nil {
		fmt.Printf("%v\n", data)
		return
	}
	if ok {
		fmt.Printf("engram %s: ok\n", cmd)
	}
}

func cmdVersion(e *env, _ string, _ []string) int {
	e.emit("version", true, map[string]string{"version": version.Version}, nil, nil, nil)
	return exitOK
}

// cmdSchema makes the surface self-describing: an agent reads the envelope and
// canonical-memory schemas rather than inferring them from prose.
func cmdSchema(e *env, _ string, _ []string) int {
	data := map[string]any{
		"response_envelope": map[string]any{
			"schema_version": schemaVersion,
			"fields": []string{
				"schema_version:int", "command:string", "ok:bool",
				"data:object?", "warnings:string[]?", "error:{code,message}|null",
				"next_steps:[{reason,command}]?",
			},
		},
		"exit_codes": map[string]string{
			"0": "success (including no actions needed)",
			"1": "runtime/unexpected error",
			"2": "usage/validation error, or write to a disabled harness",
			"3": "CONFLICT actions present and unresolved",
		},
		"canonical_memory": map[string]any{
			"required": []string{"name", "description", "type", "scope"},
			"types":    []string{"user", "feedback", "project", "reference", "lesson", "preference"},
			"scopes":   []string{"global", "project:<repo>"},
			"applies_to": []string{
				"cwd:glob[]", "agents:[claude|codex]", "hosts:string[]",
			},
		},
		"curate_operation": map[string]any{
			"note": "the JSON an agent returns from `engram curate`; engram validates each op and, under --apply, applies the whole batch or nothing (fail closed)",
			"ops": map[string]string{
				"add":     "{op, memory, reason} — memory.name must not already exist",
				"update":  "{op, name, memory, reason} — memory.name must equal name (no rename)",
				"merge":   "{op, sources[>=2], memory, reason} — sources replaced by memory; a source reusing memory.name is kept",
				"remove":  "{op, name, reason} — name must exist",
				"rescope": "{op, name, to_scope, reason} — to_scope is 'global' or 'project:<repo>'",
			},
		},
	}
	e.emit("schema", true, data, nil, nil, nil)
	return exitOK
}

// usage prints help. Under --json it emits the command table so an agent can
// discover the surface machine-readably.
func (e *env) usage(code int) int {
	cs := commands()
	sort.Slice(cs, func(i, j int) bool { return cs[i].name < cs[j].name })

	if e.jsonMode {
		list := make([]map[string]string, 0, len(cs))
		for _, c := range cs {
			list = append(list, map[string]string{"name": c.name, "summary": c.summary})
		}
		e.emit("help", code == exitOK, map[string]any{
			"usage":    "engram [global flags] <command> [args]",
			"commands": list,
		}, nil, nil, nil)
		return code
	}

	w := os.Stderr
	fmt.Fprintln(w, "engram — cross-harness agent memory bridge")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "USAGE:")
	fmt.Fprintln(w, "  engram [global flags] <command> [args]")
	fmt.Fprintln(w)
	fmt.Fprintln(w, "COMMANDS:")
	for _, c := range cs {
		fmt.Fprintf(w, "  %-9s %s\n", c.name, c.summary)
	}
	fmt.Fprintln(w)
	fmt.Fprintln(w, "GLOBAL FLAGS:")
	fmt.Fprintln(w, "  --json        structured output (auto when stdout is not a TTY)")
	fmt.Fprintln(w, "  --plain       force human output")
	fmt.Fprintln(w, "  -q, --quiet   suppress non-essential output")
	fmt.Fprintln(w, "  -h, --help    show this help")
	fmt.Fprintln(w, "  --version     print version")
	return code
}

// isTTY reports whether f is a character device (an interactive terminal).
func isTTY(f *os.File) bool {
	fi, err := f.Stat()
	if err != nil {
		return false
	}
	return fi.Mode()&os.ModeCharDevice != 0
}

// flagValue extracts the value for the value-taking flag at args[i], supporting
// both "--flag=value" and "--flag value", and returns the value plus the index
// to continue iterating from.
func flagValue(args []string, i int) (string, int) {
	if eq := strings.IndexByte(args[i], '='); eq >= 0 {
		return args[i][eq+1:], i
	}
	if i+1 < len(args) {
		return args[i+1], i + 1
	}
	return "", i
}
