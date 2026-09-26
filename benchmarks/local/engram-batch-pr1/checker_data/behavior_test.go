package curate

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/davisbuilds/engram/internal/schema"
	"github.com/davisbuilds/engram/internal/store"
)

func TestGoldenBatchContentSurvivesScopeChanges(t *testing.T) {
	for _, kind := range []string{"update", "merge-reuse", "merge-new", "add", "repeated-rescope"} {
		t.Run(kind, func(t *testing.T) {
			root := t.TempDir()
			seed(t, root, mem("alpha"), mem("beta"), mem("untouched"))
			before, err := os.ReadFile(filepath.Join(root, "untouched.md"))
			if err != nil {
				t.Fatal(err)
			}
			want := &schema.CanonicalMemory{
				Name: "alpha", Description: "replacement description", Type: schema.TypeReference,
				Scope: "global", Body: "replacement body with unicode Ω\n\nkeep trailing line\n",
				AppliesTo:  schema.AppliesTo{Agents: []string{"codex"}, Hosts: []string{"workstation"}},
				Provenance: schema.Provenance{Origin: "synthetic", Author: "fixture"}, Related: []string{"untouched"},
			}
			var ops []Operation
			switch kind {
			case "update", "repeated-rescope":
				ops = append(ops, Operation{Op: OpUpdate, Name: "alpha", Memory: want})
			case "merge-reuse":
				ops = append(ops, Operation{Op: OpMerge, Sources: []string{"alpha", "beta"}, Memory: want})
			case "merge-new":
				want.Name = "combined"
				ops = append(ops, Operation{Op: OpMerge, Sources: []string{"alpha", "beta"}, Memory: want})
			case "add":
				want.Name = "addition"
				ops = append(ops, Operation{Op: OpAdd, Memory: want})
			}
			ops = append(ops, Operation{Op: OpRescope, Name: want.Name, ToScope: "project:demo"})
			if kind == "repeated-rescope" {
				ops = append(ops, Operation{Op: OpRescope, Name: want.Name, ToScope: "global"})
			}
			// Freeze an independent expectation before candidate code can mutate the
			// operation or its nested slices through the shared Memory pointer.
			expectedBytes, err := json.Marshal(want)
			if err != nil {
				t.Fatal(err)
			}
			var expected schema.CanonicalMemory
			if err := json.Unmarshal(expectedBytes, &expected); err != nil {
				t.Fatal(err)
			}
			expected.Scope = ops[len(ops)-1].ToScope
			gotOps, err := Apply(root, ops)
			if err != nil {
				t.Fatal(err)
			}
			if len(gotOps) != len(ops) {
				t.Fatalf("applied %d operations, want %d", len(gotOps), len(ops))
			}
			got, _, found, err := store.Load(root, expected.Name)
			if err != nil || !found {
				t.Fatalf("load: found=%v err=%v", found, err)
			}
			if !reflect.DeepEqual(got, &expected) {
				t.Fatalf("content changed unexpectedly: got %+v want %+v", got, &expected)
			}
			after, err := os.ReadFile(filepath.Join(root, "untouched.md"))
			if err != nil {
				t.Fatal(err)
			}
			if string(before) != string(after) {
				t.Fatal("unrelated file changed")
			}
			if kind == "merge-reuse" || kind == "merge-new" {
				for _, name := range []string{"alpha", "beta"} {
					if name == expected.Name {
						continue
					}
					if _, _, found, err := store.Load(root, name); err != nil || found {
						t.Fatalf("merged source %s remains: %v", name, err)
					}
				}
			}
		})
	}
}

func TestGoldenMalformedStorePreservesAllMemoryBytes(t *testing.T) {
	for _, broken := range []string{"missing-frontmatter", "invalid-yaml", "conflict-markers"} {
		for _, action := range []string{"unrelated-add", "overwrite-add", "merge-target", "remove-good"} {
			t.Run(broken+"/"+action, func(t *testing.T) {
				root := t.TempDir()
				seed(t, root, mem("alpha"), mem("beta"))
				payload := map[string]string{
					"missing-frontmatter": "irreplaceable unparsed text\n",
					"invalid-yaml":        "---\nname: [unterminated\n---\nretain me\n",
					"conflict-markers":    "<<<<<<< left\noriginal\n=======\nalternative\n>>>>>>> right\n",
				}[broken]
				if err := os.WriteFile(filepath.Join(root, "broken.md"), []byte(payload), 0600); err != nil {
					t.Fatal(err)
				}
				before := goldenMemoryBytes(t, root)
				var ops []Operation
				switch action {
				case "unrelated-add":
					ops = []Operation{{Op: OpAdd, Memory: mem("new-entry")}}
				case "overwrite-add":
					ops = []Operation{{Op: OpAdd, Memory: mem("broken")}}
				case "merge-target":
					ops = []Operation{{Op: OpMerge, Sources: []string{"alpha", "beta"}, Memory: mem("broken")}}
				case "remove-good":
					ops = []Operation{{Op: OpRemove, Name: "alpha"}}
				}
				if applied, err := Apply(root, ops); err == nil || len(applied) != 0 {
					t.Fatalf("malformed store accepted: applied=%v err=%v", applied, err)
				}
				if after := goldenMemoryBytes(t, root); !reflect.DeepEqual(before, after) {
					t.Fatal("refused operation changed canonical bytes or file set")
				}
				// Remove only the malformed input and prove this identical operation is supported.
				if err := os.Remove(filepath.Join(root, "broken.md")); err != nil {
					t.Fatal(err)
				}
				if _, err := Apply(root, ops); err != nil {
					t.Fatalf("valid corpus control rejected: %v", err)
				}
			})
		}
	}
}

func goldenMemoryBytes(t *testing.T, root string) map[string]string {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(root, "*.md"))
	if err != nil {
		t.Fatal(err)
	}
	out := map[string]string{}
	for _, p := range files {
		b, err := os.ReadFile(p)
		if err != nil {
			t.Fatal(err)
		}
		out[filepath.Base(p)] = string(b)
	}
	return out
}
