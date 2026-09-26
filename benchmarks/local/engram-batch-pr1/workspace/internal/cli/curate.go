package cli

import (
	"strings"

	"github.com/davisbuilds/engram/internal/agentexec"
	"github.com/davisbuilds/engram/internal/config"
	"github.com/davisbuilds/engram/internal/curate"
	"github.com/davisbuilds/engram/internal/discover"
	"github.com/davisbuilds/engram/internal/review"
)

// cmdCurate runs the proposer/applier loop: engram gathers the canonical corpus
// (deterministic), a headless agent proposes operations over it (judgment), and
// engram validates and — under --apply — applies them (deterministic). The agent
// never touches a file; engram is the sole mutator, and a batch with any invalid
// operation is refused whole (fail closed).
func cmdCurate(e *env, name string, args []string) int {
	harness := config.HarnessClaude
	var modelOverride, effortOverride string
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case strings.HasPrefix(a, "--harness"):
			harness, i = flagValue(args, i)
		case strings.HasPrefix(a, "--model"):
			modelOverride, i = flagValue(args, i)
		case strings.HasPrefix(a, "--effort"):
			effortOverride, i = flagValue(args, i)
		}
	}
	if harness != config.HarnessClaude && harness != config.HarnessCodex {
		e.emit(name, false, nil, nil, &RespError{Code: "unknown_harness", Message: "harness must be claude-code or codex"}, nil)
		return exitUsage
	}

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
	warns := warnParseErrors(perrs)

	// Resolve the model/effort: config default, overridden per-run by flags.
	choice := cfg.CurateModel(harness)
	if modelOverride != "" {
		choice.Model = modelOverride
	}
	if effortOverride != "" {
		choice.Effort = effortOverride
	}
	opts := agentexec.Options{Model: choice.Model, Effort: choice.Effort}

	prompt, err := curate.BuildPrompt(curate.Corpus{Memories: mems, Findings: review.Analyze(mems)})
	if err != nil {
		e.emit(name, false, nil, warns, &RespError{Code: "build_prompt", Message: err.Error()}, nil)
		return exitError
	}

	var argv []string
	switch harness {
	case config.HarnessClaude:
		argv = agentexec.ClaudeArgvOpts(prompt, opts)
	case config.HarnessCodex:
		argv = agentexec.CodexArgvOpts(prompt, opts)
	}
	invocation := map[string]any{
		"harness": harness, "model": choice.Model, "effort": choice.Effort, "corpus_size": len(mems),
	}

	stdout, err := e.runner(argv)
	if err != nil {
		e.emit(name, false, map[string]any{"invocation": invocation}, warns,
			&RespError{Code: "agent_run", Message: err.Error()}, nil)
		return exitError
	}

	var text string
	switch harness {
	case config.HarnessClaude:
		text, err = agentexec.ExtractClaudeText(stdout)
	case config.HarnessCodex:
		text, err = agentexec.ExtractCodexText(stdout)
	}
	if err != nil {
		e.emit(name, false, map[string]any{"invocation": invocation, "raw_output": string(stdout)}, warns,
			&RespError{Code: "agent_output", Message: err.Error()}, nil)
		return exitError
	}

	proposal, err := curate.ParseProposal(text)
	if err != nil {
		e.emit(name, false, map[string]any{"invocation": invocation, "agent_output": text}, warns,
			&RespError{Code: "parse_proposal", Message: err.Error()}, nil)
		return exitError
	}

	results := curate.Validate(proposal.Operations, mems)
	data := map[string]any{
		"invocation": invocation,
		"apply":      e.apply,
		"operations": opResultItems(results),
		"count":      len(results),
	}

	if !e.apply {
		if !curate.AllValid(results) {
			warns = append(warns, "some proposed operations are invalid; --apply would refuse the whole batch")
			e.emit(name, false, data, warns, nil, curateNextSteps(results))
			return exitConflicts
		}
		if len(results) > 0 {
			e.emit(name, true, data, warns, nil, []NextStep{{
				Reason:  "the proposed operations validated cleanly",
				Command: "re-run engram curate --harness " + harness + " --apply to commit them",
			}})
			return exitOK
		}
		e.emit(name, true, data, warns, nil, nil)
		return exitOK
	}

	// --apply: fail closed unless the entire batch is valid.
	if !curate.AllValid(results) {
		e.emit(name, false, data, warns,
			&RespError{Code: "invalid_proposal", Message: "refusing to apply: the batch contains invalid operations"},
			curateNextSteps(results))
		return exitConflicts
	}
	applied, aerr := curate.Apply(cfg.CanonicalRoot, proposal.Operations)
	if aerr != nil {
		data["applied"] = orEmpty(applied)
		e.emit(name, false, data, warns, &RespError{Code: "apply", Message: aerr.Error()}, nil)
		return exitError
	}
	data["applied"] = orEmpty(applied)
	e.emit(name, true, data, warns, nil, nil)
	return exitOK
}

func opResultItems(results []curate.OpResult) []map[string]any {
	items := make([]map[string]any, 0, len(results))
	for _, r := range results {
		item := map[string]any{
			"op":     r.Op.Op,
			"valid":  r.Valid,
			"reason": r.Op.Reason,
		}
		if r.Op.Name != "" {
			item["name"] = r.Op.Name
		}
		if len(r.Op.Sources) > 0 {
			item["sources"] = r.Op.Sources
		}
		if r.Op.ToScope != "" {
			item["to_scope"] = r.Op.ToScope
		}
		if r.Op.Memory != nil {
			// Surface the full proposed memory so a dry-run reviewer can judge
			// the content an add/update/merge would write, not just its name.
			item["memory_name"] = r.Op.Memory.Name
			item["memory"] = r.Op.Memory
		}
		if r.Error != "" {
			item["error"] = r.Error
		}
		items = append(items, item)
	}
	return items
}

// curateNextSteps turns each invalid operation into an agent-consumable lead so a
// caller knows exactly which proposals blocked the batch and why.
func curateNextSteps(results []curate.OpResult) []NextStep {
	var steps []NextStep
	for _, r := range results {
		if r.Valid {
			continue
		}
		target := r.Op.Name
		if target == "" && r.Op.Memory != nil {
			target = r.Op.Memory.Name
		}
		steps = append(steps, NextStep{
			Reason:  "invalid " + r.Op.Op + " operation (" + target + "): " + r.Error,
			Command: "re-run engram curate after the corpus changes, or fix the proposal manually",
		})
	}
	return steps
}
