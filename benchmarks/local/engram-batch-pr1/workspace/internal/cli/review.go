package cli

import (
	"strings"

	"github.com/davisbuilds/engram/internal/agentexec"
	"github.com/davisbuilds/engram/internal/config"
	"github.com/davisbuilds/engram/internal/discover"
	"github.com/davisbuilds/engram/internal/review"
)

// cmdReview produces health leads over the canonical set and emits each as a
// next_step. It never mutates: the merge/promote decision is the agent's. A
// near-duplicate lead carries a ready-to-run headless `claude -p` invocation
// (with the `--` separator guaranteed), a promotion lead an `engram share`.
func cmdReview(e *env, name string, _ []string) int {
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

	findings := review.Analyze(mems)
	items := make([]map[string]any, 0, len(findings))
	next := make([]NextStep, 0, len(findings))
	for _, f := range findings {
		items = append(items, map[string]any{
			"kind": f.Kind, "names": f.Names, "detail": f.Detail,
		})
		cmd := f.Suggested
		if f.Kind == "near-duplicate" {
			cmd = agentexec.ClaudeCommand(
				"Compare canonical memories "+strings.Join(f.Names, " and ")+
					" and decide whether to merge them; if so, rewrite one and delete the other.",
				"Read", "Edit",
			)
		}
		next = append(next, NextStep{Reason: f.Detail, Command: cmd})
	}

	e.emit(name, true, map[string]any{
		"canonical_root": cfg.CanonicalRoot,
		"count":          len(findings),
		"findings":       items,
	}, warnParseErrors(perrs), nil, next)
	return exitOK
}
