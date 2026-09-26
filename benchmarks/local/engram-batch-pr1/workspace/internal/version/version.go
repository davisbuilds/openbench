// Package version carries engram's build version.
package version

// Version is engram's semantic version. Override at build time with
// -ldflags "-X github.com/davisbuilds/engram/internal/version.Version=x.y.z".
var Version = "0.0.0-dev"
