package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"
)

func TestEvaluate(t *testing.T) {
	tests := []struct {
		expression string
		want       bool
		wantError  bool
	}{
		{expression: `event == "pull_request" && source_branch.startsWith("feature/")`, want: true},
		{expression: `event == "push" && target_branch.startsWith("v")`},
		{expression: `event == "push" && target_branch.matches("^rhoai-[A-Za-z0-9_][A-Za-z0-9_.-]{0,40}$") && !(target_branch in ["rhoai-3.5", "rhoai-3.6-ea.1", "rhoai-3.6-ea.2"])`},
		{expression: `false && event == "push" && "dashboard-operator/**".pathChanged()`},
		{expression: `(event == "push" && target_branch.startsWith("v")) || event == "pull_request"`, want: true},
		{expression: `event == "pull_request" || (event == "push" && target_branch.startsWith("v"))`, want: true},
		{expression: `target_branch != "main"`, want: true},
		{expression: `files.all.exists(p, p.endsWith(".go"))`, want: true},
		{expression: `files.all.filter(p, p.endsWith(".go")).size() == 1`, want: true},
		{expression: `files.deleted == ["old.txt"]`, want: true},
		{expression: `files.renamed == ["new.txt"]`, want: true},
		{expression: `"src/*.go".pathChanged()`, want: true},
		{expression: `"src/{nested,other}/[a-z]*.go".pathChanged()`, want: true},
		{expression: `"nothing/**".pathChanged()`},
		{expression: `"[".pathChanged()`, wantError: true},
		{expression: `target_branch.matches("(?<=x)y")`, wantError: true},
		{expression: `body.action == "synchronize"`, wantError: true},
		{expression: `has(body.action)`, wantError: true},
		{expression: `event_type == "pull_request"`, wantError: true},
		{expression: `pac.target_branch == "stable"`, wantError: true},
		{expression: `"nonboolean"`, wantError: true},
		{expression: `event ==`, wantError: true},
		{expression: `enable_ci == "true"`, want: true},
	}
	req := request{Variables: map[string]any{
		"event": "pull_request", "source_branch": "feature/test", "target_branch": "stable",
		"files": map[string]any{
			"all":   []any{"src/nested/main.go", "new.txt", "old.txt"},
			"added": []any{}, "deleted": []any{"old.txt"}, "modified": []any{}, "renamed": []any{"new.txt"},
		},
	}, CustomParams: map[string]string{"enable_ci": "true", "event": "push"}}
	for _, tt := range tests {
		t.Run(tt.expression, func(t *testing.T) {
			req.Expressions = []string{tt.expression}
			got := evaluate(req)[0]
			if tt.wantError {
				if got.Error == "" {
					t.Fatal("expected evaluation error")
				}
			} else if got.Error != "" || got.Value == nil || *got.Value != tt.want {
				t.Fatalf("got %+v, want %v", got, tt.want)
			}
		})
	}
}

func TestEmptyFilesAndBatch(t *testing.T) {
	req := request{
		Variables: map[string]any{"files": map[string]any{"all": []any{}}},
		Expressions: []string{
			`files.all.all(p, p.matches(".*"))`,
			`files.all.exists(p, p.matches(".*"))`,
			`"**".pathChanged()`,
			`not_declared`,
		},
	}
	got := evaluate(req)
	for i, want := range []bool{true, false, false} {
		if got[i].Value == nil || *got[i].Value != want {
			t.Fatalf("result %d: %+v", i, got[i])
		}
	}
	if got[3].Error == "" {
		t.Fatal("expected isolated error")
	}
}

func TestProtocol(t *testing.T) {
	for _, invalid := range []string{"", "{", "{} {}"} {
		if err := run(strings.NewReader(invalid), &bytes.Buffer{}); err == nil {
			t.Fatal("accepted invalid request")
		}
	}
	var out bytes.Buffer
	if err := run(strings.NewReader(`{"expressions":["false"]}`), &out); err != nil {
		t.Fatal(err)
	}
	var got []result
	if err := json.Unmarshal(out.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if len(got) != 1 || got[0].Value == nil || *got[0].Value {
		t.Fatalf("invalid false response: %s", out.String())
	}
}
