// Package config loads engram's resolved configuration. Host identity, harness
// homes, the canonical root, and enable toggles all live here so nothing else
// carries machine-specific state — and, deliberately, no host name is compiled
// into engram: a fresh install knows about a machine only once its config maps
// that machine's `hostname -s` value to a label.
package config

import (
	"errors"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// Harness identifiers.
const (
	HarnessClaude = "claude-code"
	HarnessCodex  = "codex"
)

// Harness describes one render target. Disabled (not Enabled) is the stored
// field so the zero value is enabled and a partial config merge never silently
// turns a harness off.
type Harness struct {
	Home     string `yaml:"home"`
	Disabled bool   `yaml:"disabled"`
}

// Enabled reports whether engram may act on this harness.
func (h Harness) Enabled() bool { return !h.Disabled }

// ModelChoice pins the model and reasoning effort a headless curate run uses for
// one harness. Empty fields fall back to the built-in defaults.
type ModelChoice struct {
	Model  string `yaml:"model"`
	Effort string `yaml:"effort"`
}

// CurateConfig holds the per-harness model defaults for `engram curate`. These
// are ordinary defaults (model names, not machine state) and are safe to compile
// in; a config file or a per-run flag overrides them.
type CurateConfig struct {
	Models map[string]ModelChoice `yaml:"models"`
}

// Config is the fully-resolved configuration.
type Config struct {
	CanonicalRoot string             `yaml:"canonical_root"`
	Hosts         map[string]string  `yaml:"hosts"`
	Harnesses     map[string]Harness `yaml:"harnesses"`
	Curate        CurateConfig       `yaml:"curate"`
}

// CurateModel returns the resolved model choice for a harness, falling back to
// the built-in default for any field the config left empty.
func (c *Config) CurateModel(harness string) ModelChoice {
	def := defaults().Curate.Models[harness]
	got := c.Curate.Models[harness]
	if got.Model == "" {
		got.Model = def.Model
	}
	if got.Effort == "" {
		got.Effort = def.Effort
	}
	return got
}

// HostLabel maps a `hostname -s` value to its configured label. A missing entry
// returns ok=false, which the scope filter treats as fail-closed for
// host-scoped memories.
func (c *Config) HostLabel(hostname string) (string, bool) {
	label, ok := c.Hosts[hostname]
	return label, ok
}

// defaults returns the built-in configuration used when a field or the whole
// file is absent.
func defaults() Config {
	home, _ := os.UserHomeDir()
	return Config{
		CanonicalRoot: filepath.Join(home, ".engram", "canonical"),
		Hosts:         map[string]string{},
		Harnesses: map[string]Harness{
			HarnessClaude: {Home: filepath.Join(home, ".claude")},
			HarnessCodex:  {Home: filepath.Join(home, ".codex")},
		},
		Curate: CurateConfig{Models: map[string]ModelChoice{
			HarnessClaude: {Model: "claude-sonnet-5", Effort: "high"},
			HarnessCodex:  {Model: "gpt-5.6-terra", Effort: "high"},
		}},
	}
}

// Load reads the config at path, or the default location when path is empty. A
// missing file yields the built-in defaults rather than an error.
func Load(path string) (*Config, error) {
	cfg := defaults()
	p := path
	if p == "" {
		p = defaultPath()
	}
	data, err := os.ReadFile(p)
	if errors.Is(err, os.ErrNotExist) {
		return &cfg, nil
	}
	if err != nil {
		return nil, err
	}
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	cfg.fillDefaults()
	return &cfg, nil
}

// fillDefaults patches any field a partial config left empty back to a sane
// default, so an overriding config never has to restate every field.
func (c *Config) fillDefaults() {
	d := defaults()
	if c.CanonicalRoot == "" {
		c.CanonicalRoot = d.CanonicalRoot
	}
	if c.Hosts == nil {
		c.Hosts = map[string]string{}
	}
	if c.Harnesses == nil {
		c.Harnesses = map[string]Harness{}
	}
	for name, def := range d.Harnesses {
		h, ok := c.Harnesses[name]
		if !ok {
			c.Harnesses[name] = def
			continue
		}
		if h.Home == "" {
			h.Home = def.Home
			c.Harnesses[name] = h
		}
	}
}

func defaultPath() string {
	if x := os.Getenv("XDG_CONFIG_HOME"); x != "" {
		return filepath.Join(x, "engram", "config.yaml")
	}
	home, _ := os.UserHomeDir()
	return filepath.Join(home, ".config", "engram", "config.yaml")
}
