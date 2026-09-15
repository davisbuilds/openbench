// Package scope decides which canonical memories are relevant to one session,
// identified by its working directory, agent, and host label. This is the single
// place scope semantics live: tier (global vs project), cwd globs, agent filter,
// and the host axis — including the fail-closed rule for an unknown host.
package scope

import (
	"path/filepath"
	"strings"

	"github.com/davisbuilds/engram/internal/schema"
)

// RelevantFor returns the memories that should render for a session at cwd, run
// by agent, on the machine whose configured host label is host ("" when the
// current hostname is unmapped). A memory passes only when every axis it
// constrains matches; an unconstrained axis matches anything.
func RelevantFor(mems []*schema.CanonicalMemory, cwd, agent, host string) []*schema.CanonicalMemory {
	var out []*schema.CanonicalMemory
	for _, m := range mems {
		if tierMatch(m.Scope, cwd) &&
			cwdMatch(m.AppliesTo.Cwd, cwd) &&
			listMatch(m.AppliesTo.Agents, agent) &&
			hostMatch(m.AppliesTo.Hosts, host) {
			out = append(out, m)
		}
	}
	return out
}

// tierMatch gates by scope tier: global is always in-tier; project:<repo> is
// in-tier only when the cwd lies within a directory named <repo>.
func tierMatch(scope, cwd string) bool {
	switch {
	case scope == "global":
		return true
	case strings.HasPrefix(scope, "project:"):
		return pathHasSegment(cwd, scope[len("project:"):])
	default:
		return false
	}
}

// listMatch reports whether val is in list, treating an empty list as "no
// constraint on this axis".
func listMatch(list []string, val string) bool {
	if len(list) == 0 {
		return true
	}
	for _, x := range list {
		if x == val {
			return true
		}
	}
	return false
}

// hostMatch applies the host axis with its fail-closed rule: a host-agnostic
// memory (no hosts listed) always matches; a host-scoped memory matches only a
// known, listed host, so an unknown host ("") never receives host-scoped
// memories.
func hostMatch(hosts []string, host string) bool {
	if len(hosts) == 0 {
		return true
	}
	if host == "" {
		return false
	}
	return listMatch(hosts, host)
}

// cwdMatch reports whether cwd satisfies at least one glob, treating an empty
// glob list as unconstrained.
func cwdMatch(globs []string, cwd string) bool {
	if len(globs) == 0 {
		return true
	}
	for _, g := range globs {
		if globMatch(g, cwd) {
			return true
		}
	}
	return false
}

func pathHasSegment(cwd, seg string) bool {
	for _, p := range strings.Split(cwd, string(filepath.Separator)) {
		if p == seg {
			return true
		}
	}
	return false
}

// globMatch supports the subset engram needs: "**" / "/**" (any path), a
// trailing "/**" prefix match, and otherwise filepath.Match semantics with an
// exact-string fallback.
func globMatch(glob, cwd string) bool {
	switch {
	case glob == "**" || glob == "/**":
		return true
	case strings.HasSuffix(glob, "/**"):
		pre := strings.TrimSuffix(glob, "/**")
		return cwd == pre || strings.HasPrefix(cwd, pre+"/")
	default:
		if ok, _ := filepath.Match(glob, cwd); ok {
			return true
		}
		return glob == cwd
	}
}
