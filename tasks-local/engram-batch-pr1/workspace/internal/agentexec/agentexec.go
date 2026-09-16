// Package agentexec builds argv for headless agent invocations. Its one job is
// to make the `claude -p` `--` separator structurally impossible to omit: the
// positional prompt always follows a `--`, so variadic tool flags
// (--allowedTools / --disallowedTools) can never swallow it. This is the footgun
// the spec calls out; encoding it here means it cannot be reintroduced by hand.
package agentexec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os/exec"
	"strings"
)

// Options carries the model and reasoning-effort knobs for a headless run. Both
// are optional; an empty field omits its flag so the harness's own default
// applies. Neither is compiled in as a default here — the caller resolves
// defaults (from config) before building argv.
type Options struct {
	Model  string
	Effort string
}

// ClaudeArgv builds the argv for a headless `claude -p` run. Any tool flags come
// first, then `--`, then the positional prompt — always in that order.
func ClaudeArgv(prompt string, allowedTools ...string) []string {
	argv := []string{"claude", "-p"}
	if len(allowedTools) > 0 {
		argv = append(argv, "--allowedTools")
		argv = append(argv, allowedTools...)
	}
	return append(argv, "--", prompt)
}

// CodexArgv builds the argv for a headless `codex exec` run.
func CodexArgv(prompt string) []string {
	return []string{"codex", "exec", prompt}
}

// ClaudeArgvOpts builds a headless `claude -p` run for curate: JSON output,
// model/effort passthrough, and — critically — all tools explicitly disabled via
// `--tools ""`. The corpus is untrusted (prompt-injected memory content is
// possible), so the proposer must have no filesystem access: an *absent*
// --allowedTools does NOT disable tools (the session's own settings may still
// permit Edit/Bash), whereas `--tools ""` is the documented no-tools mode. This
// keeps engram the sole mutator even against a hostile corpus. The `--` separator
// still guards the positional prompt.
func ClaudeArgvOpts(prompt string, opts Options, allowedTools ...string) []string {
	argv := []string{"claude", "-p", "--output-format", "json", "--tools", ""}
	if opts.Model != "" {
		argv = append(argv, "--model", opts.Model)
	}
	if opts.Effort != "" {
		argv = append(argv, "--effort", opts.Effort)
	}
	if len(allowedTools) > 0 {
		argv = append(argv, "--allowedTools")
		argv = append(argv, allowedTools...)
	}
	return append(argv, "--", prompt)
}

// CodexArgvOpts builds a headless `codex exec` run with model/effort. Codex has
// no `--effort` flag; reasoning effort is a config override, hence
// `-c model_reasoning_effort=<level>`. The `--` guards the positional prompt.
func CodexArgvOpts(prompt string, opts Options) []string {
	argv := []string{"codex", "exec"}
	if opts.Model != "" {
		argv = append(argv, "--model", opts.Model)
	}
	if opts.Effort != "" {
		argv = append(argv, "-c", "model_reasoning_effort="+opts.Effort)
	}
	return append(argv, "--", prompt)
}

// Runner executes an argv and returns the process's stdout. It is injected so
// tests exercise the curate loop without ever spawning a real, paid model.
type Runner func(argv []string) ([]byte, error)

// ExecRunner is the production Runner: it runs the argv as a subprocess and
// returns stdout, folding stderr into the error on failure.
func ExecRunner(argv []string) ([]byte, error) {
	if len(argv) == 0 {
		return nil, fmt.Errorf("empty argv")
	}
	cmd := exec.Command(argv[0], argv[1:]...) //nolint:gosec // argv is engram-built, not user-injected
	var out, errBuf bytes.Buffer
	cmd.Stdout = &out
	cmd.Stderr = &errBuf
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(errBuf.String())
		if msg == "" {
			msg = err.Error()
		}
		return out.Bytes(), fmt.Errorf("%s failed: %s", argv[0], msg)
	}
	return out.Bytes(), nil
}

// claudeResult is the shape of `claude -p --output-format json` stdout: a single
// envelope whose result field holds the assistant's final message.
type claudeResult struct {
	Type    string `json:"type"`
	IsError bool   `json:"is_error"`
	Result  string `json:"result"`
}

// ExtractClaudeText pulls the assistant's final message out of the
// `--output-format json` envelope, surfacing an is_error run as a Go error.
func ExtractClaudeText(stdout []byte) (string, error) {
	var r claudeResult
	if err := json.Unmarshal(bytes.TrimSpace(stdout), &r); err != nil {
		return "", fmt.Errorf("parse claude json envelope: %w", err)
	}
	if r.IsError {
		return "", fmt.Errorf("claude reported an error result: %s", r.Result)
	}
	return r.Result, nil
}

// ExtractCodexText returns codex exec stdout as-is. codex interleaves session
// logs with the final message, so the proposal JSON is recovered downstream by
// fenced-block extraction rather than an envelope field.
func ExtractCodexText(stdout []byte) (string, error) {
	return string(stdout), nil
}

// ClaudeCommand renders ClaudeArgv as a display string with the prompt quoted,
// suitable for a next_step lead a human or agent can copy and run.
func ClaudeCommand(prompt string, allowedTools ...string) string {
	argv := ClaudeArgv(prompt, allowedTools...)
	parts := make([]string, len(argv))
	for i, a := range argv {
		if i == len(argv)-1 {
			parts[i] = `"` + strings.ReplaceAll(a, `"`, `\"`) + `"`
		} else {
			parts[i] = a
		}
	}
	return strings.Join(parts, " ")
}
