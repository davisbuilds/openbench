package slug

import "testing"

func TestForCwd(t *testing.T) {
	cases := []struct {
		name string
		cwd  string
		want string
	}{
		{"simple posix path", "/home/user/project", "-home-user-project"},
		{"users dir", "/Users/alice/Dev", "-Users-alice-Dev"},
		{"preserves interior hyphens", "/Users/a-b-c/Dev", "-Users-a-b-c-Dev"},
		{"spaces dots tildes all collapse", "/a/b c/i~j.k", "-a-b-c-i-j-k"},
		{"trailing separator kept as dash", "/x/y/", "-x-y-"},
		{"digits preserved", "/srv/app2/v3", "-srv-app2-v3"},
	}
	for _, c := range cases {
		if got := ForCwd(c.cwd); got != c.want {
			t.Errorf("%s: ForCwd(%q) = %q, want %q", c.name, c.cwd, got, c.want)
		}
	}
}

func TestForCwdIsIdempotent(t *testing.T) {
	// A slug fed back through the encoder is unchanged: every slug character is a
	// letter, digit, or '-', and '-' maps to '-'. This is the property renderers
	// rely on when they re-encode an already-encoded target.
	s := ForCwd("/Users/alice/Dev")
	if again := ForCwd(s); again != s {
		t.Errorf("not idempotent: ForCwd(%q) = %q", s, again)
	}
}
