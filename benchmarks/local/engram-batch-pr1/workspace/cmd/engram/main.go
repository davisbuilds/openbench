// Command engram is a cross-harness agent-memory bridge: it renders a
// git-tracked canonical memory store into multiple agent harnesses' native
// memory locations, scoped by tier, cwd, agent, and host.
//
// The CLI is designed for a frontier coding agent as its primary operator; see
// docs/cli.md for the interface contract.
package main

import (
	"os"

	"github.com/davisbuilds/engram/internal/cli"
)

func main() {
	os.Exit(cli.Run(os.Args[1:]))
}
