package cli

import (
	"os"

	"github.com/davisbuilds/engram/internal/config"
	"github.com/davisbuilds/engram/internal/harness"
)

// cmdConfig reports the resolved configuration and a per-harness readiness
// assessment: not just whether engram is enabled for a harness, but whether the
// harness itself is set up to read what engram writes. It is informational and
// always exits 0; the envelope's ok and each harness's `ready` carry the signal.
func cmdConfig(e *env, name string, _ []string) int {
	cfg, err := config.Load(e.config)
	if err != nil {
		e.emit(name, false, nil, nil, &RespError{Code: "config_load", Message: err.Error()}, nil)
		return exitError
	}

	claudeH := cfg.Harnesses[config.HarnessClaude]
	codexH := cfg.Harnesses[config.HarnessCodex]
	reports := []harness.Report{
		harness.CheckClaude(claudeH.Home, claudeH.Enabled()),
		harness.CheckCodex(codexH.Home, codexH.Enabled()),
	}

	canonExists := isDir(cfg.CanonicalRoot)
	var warns []string
	if !canonExists {
		warns = append(warns, "canonical_root "+cfg.CanonicalRoot+" does not exist yet (engram creates it on first write)")
	}
	anyReady := false
	for _, r := range reports {
		if r.Ready {
			anyReady = true
		}
		for _, w := range r.Warnings {
			warns = append(warns, r.Harness+": "+w)
		}
	}

	e.emit(name, anyReady, map[string]any{
		"canonical_root":        cfg.CanonicalRoot,
		"canonical_root_exists": canonExists,
		"harnesses":             reports,
	}, warns, nil, nil)
	return exitOK
}

func isDir(path string) bool {
	fi, err := os.Stat(path)
	return err == nil && fi.IsDir()
}

// harnessWarnings returns the readiness warnings for an enabled harness, prefixed
// with the harness name, so sync can surface a not-set-up harness without failing.
func harnessWarnings(r harness.Report) []string {
	out := make([]string, 0, len(r.Warnings))
	for _, w := range r.Warnings {
		out = append(out, r.Harness+": "+w)
	}
	return out
}
