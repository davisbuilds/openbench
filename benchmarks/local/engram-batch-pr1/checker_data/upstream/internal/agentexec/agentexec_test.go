package agentexec

import (
	"strings"
	"testing"
)

func TestClaudeArgvSeparatorAlwaysBeforePrompt(t *testing.T) {
	cases := [][]string{
		{}, // no tools
		{"Read"},
		{"Read", "Edit"},
	}
	for _, tools := range cases {
		argv := ClaudeArgv("the prompt", tools...)
		if len(argv) < 4 {
			t.Fatalf("argv too short: %v", argv)
		}
		if argv[0] != "claude" || argv[1] != "-p" {
			t.Errorf("argv should start with claude -p: %v", argv)
		}
		if argv[len(argv)-1] != "the prompt" {
			t.Errorf("prompt must be the last arg: %v", argv)
		}
		if argv[len(argv)-2] != "--" {
			t.Errorf("`--` must immediately precede the prompt: %v", argv)
		}
		// The prompt must never be adjacent to a tool flag value.
		for i, a := range argv[:len(argv)-1] {
			if a == "the prompt" {
				t.Errorf("prompt appeared before the separator at index %d: %v", i, argv)
			}
		}
	}
}

func TestClaudeCommandQuotesPromptAfterSeparator(t *testing.T) {
	cmd := ClaudeCommand("compare a and b", "Read")
	if !strings.Contains(cmd, `-- "compare a and b"`) {
		t.Errorf("command must place a quoted prompt after --: %q", cmd)
	}
}

func TestCodexArgv(t *testing.T) {
	argv := CodexArgv("do the thing")
	if len(argv) != 3 || argv[0] != "codex" || argv[1] != "exec" || argv[2] != "do the thing" {
		t.Errorf("unexpected codex argv: %v", argv)
	}
}

func TestClaudeArgvOptsCarriesModelEffortAndJSON(t *testing.T) {
	argv := ClaudeArgvOpts("prompt here", Options{Model: "claude-sonnet-5", Effort: "high"})
	joined := strings.Join(argv, " ")
	for _, want := range []string{
		"--output-format json", "--model claude-sonnet-5", "--effort high",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("argv missing %q: %v", want, argv)
		}
	}
	if argv[len(argv)-2] != "--" || argv[len(argv)-1] != "prompt here" {
		t.Errorf("prompt must follow -- as the last arg: %v", argv)
	}
	// The proposer must run with all tools explicitly disabled (--tools ""): the
	// corpus is untrusted, and an absent --allowedTools does not disable tools.
	found := false
	for i := 0; i < len(argv)-1; i++ {
		if argv[i] == "--tools" && argv[i+1] == "" {
			found = true
		}
	}
	if !found {
		t.Errorf("curate argv must disable all tools via --tools \"\": %v", argv)
	}
}

func TestCodexArgvOptsCarriesModelAndEffort(t *testing.T) {
	argv := CodexArgvOpts("prompt here", Options{Model: "gpt-5.6-terra", Effort: "high"})
	joined := strings.Join(argv, " ")
	if !strings.Contains(joined, "--model gpt-5.6-terra") {
		t.Errorf("argv missing model: %v", argv)
	}
	if !strings.Contains(joined, "model_reasoning_effort=high") {
		t.Errorf("codex effort must go through -c model_reasoning_effort: %v", argv)
	}
	if argv[len(argv)-1] != "prompt here" {
		t.Errorf("prompt must be the last arg: %v", argv)
	}
}

func TestExtractClaudeTextReturnsResult(t *testing.T) {
	stdout := []byte(`{"type":"result","is_error":false,"result":"the answer","total_cost_usd":0.01}`)
	got, err := ExtractClaudeText(stdout)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "the answer" {
		t.Errorf("got %q, want %q", got, "the answer")
	}
}

func TestExtractClaudeTextSurfacesError(t *testing.T) {
	stdout := []byte(`{"type":"result","is_error":true,"result":"model refused"}`)
	if _, err := ExtractClaudeText(stdout); err == nil {
		t.Error("expected an error when is_error is true")
	}
}

func TestExtractClaudeTextRejectsGarbage(t *testing.T) {
	if _, err := ExtractClaudeText([]byte("not json at all")); err == nil {
		t.Error("expected an error on non-JSON stdout")
	}
}
