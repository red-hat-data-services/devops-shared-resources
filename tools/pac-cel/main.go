// pac-cel evaluates PaC trigger expressions over supplied facts, without I/O
// beyond stdin/stdout. Environment aligned with PaC a53fa582b3815ecb680700e8bd636002e206c8f1.
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"os"

	"cel.dev/cel-go/cel"
	"cel.dev/cel-go/common/types"
	"cel.dev/cel-go/common/types/ref"
	"github.com/gobwas/glob"
	exprpb "google.golang.org/genproto/googleapis/api/expr/v1alpha1"
)

type request struct {
	Expressions  []string          `json:"expressions"`
	Variables    map[string]any    `json:"variables"`
	CustomParams map[string]string `json:"custom_params,omitempty"`
}
type result struct {
	Value *bool  `json:"value,omitempty"`
	Error string `json:"error,omitempty"`
}

func evaluate(req request) []result {
	opts := []cel.EnvOption{}
	standard := map[string]bool{}
	for _, name := range []string{"event", "event_type", "event_title", "target_branch", "source_branch", "target_url", "source_url"} {
		standard[name] = true
		opts = append(opts, cel.Variable(name, cel.StringType))
	}
	for _, name := range []string{"body", "headers", "files"} {
		standard[name] = true
		opts = append(opts, cel.Variable(name, cel.MapType(cel.StringType, cel.DynType)))
	}
	data := map[string]any{}
	for k, v := range req.Variables {
		if standard[k] {
			data[k] = v
		}
	}
	for k, v := range req.CustomParams {
		if !standard[k] {
			opts = append(opts, cel.Variable(k, cel.StringType))
			data[k] = v
		}
	}
	opts = append(opts, cel.Function("pathChanged",
		cel.MemberOverload("pathChanged", []*cel.Type{cel.StringType}, cel.BoolType,
			cel.UnaryBinding(func(value ref.Val) ref.Val {
				pattern, ok := value.Value().(string)
				if !ok {
					return types.NewErr("pathChanged requires a string")
				}
				g, err := glob.Compile(pattern)
				if err != nil {
					return types.NewErr("invalid pathChanged pattern: %v", err)
				}
				files, ok := data["files"].(map[string]any)
				if !ok {
					return types.NewErr("changed files unavailable")
				}
				paths, ok := files["all"].([]any)
				if !ok {
					return types.NewErr("files.all unavailable or invalid")
				}
				for _, path := range paths {
					name, ok := path.(string)
					if !ok {
						return types.NewErr("invalid changed-file path")
					}
					if g.Match(name) {
						return types.True
					}
				}
				return types.False
			}))))
	env, envErr := cel.NewEnv(opts...)
	results := make([]result, len(req.Expressions))
	for i, expression := range req.Expressions {
		value, err := evaluateOne(env, envErr, expression, data)
		if err != nil {
			results[i].Error = err.Error()
		} else {
			results[i].Value = &value
		}
	}
	return results
}

func evaluateOne(env *cel.Env, envErr error, expression string, data map[string]any) (bool, error) {
	if envErr != nil {
		return false, envErr
	}
	ast, issues := env.Compile(expression)
	if issues != nil && issues.Err() != nil {
		return false, issues.Err()
	}
	if files, ok := data["files"].(map[string]any); ok {
		if err := checkFileReferences(ast.Expr(), files); err != nil {
			return false, err
		}
	}
	program, err := env.Program(ast, cel.CostLimit(100000))
	if err != nil {
		return false, err
	}
	value, _, err := program.Eval(data)
	if err != nil {
		return false, err
	}
	b, ok := value.(types.Bool)
	if !ok {
		return false, fmt.Errorf("trigger expression must return bool, got %s", value.Type())
	}
	return bool(b), nil
}

// A partial file map means unknown collections, not known-absent keys.
// Reject probes such as has(files.added) rather than reporting a false fact.
func checkFileReferences(expr *exprpb.Expr, files map[string]any) error {
	if expr == nil {
		return nil
	}
	isFiles := func(e *exprpb.Expr) bool { return e.GetIdentExpr().GetName() == "files" }
	if selectExpr := expr.GetSelectExpr(); selectExpr != nil && isFiles(selectExpr.Operand) {
		if _, ok := files[selectExpr.Field]; !ok {
			return fmt.Errorf("files.%s unavailable", selectExpr.Field)
		}
		return nil
	}
	children := []*exprpb.Expr{}
	if call := expr.GetCallExpr(); call != nil {
		if call.Function == "_[_]" && len(call.Args) == 2 && isFiles(call.Args[0]) {
			key := call.Args[1].GetConstExpr()
			if key != nil {
				if _, ok := files[key.GetStringValue()]; !ok {
					return fmt.Errorf("file collection unavailable")
				}
				return nil
			}
		}
		children = append(children, call.Target)
		children = append(children, call.Args...)
	}
	if isFiles(expr) {
		for _, key := range []string{"all", "added", "deleted", "modified", "renamed"} {
			if _, ok := files[key]; !ok {
				return fmt.Errorf("complete file collections unavailable")
			}
		}
	}
	if s := expr.GetSelectExpr(); s != nil {
		children = append(children, s.Operand)
	}
	if l := expr.GetListExpr(); l != nil {
		children = append(children, l.Elements...)
	}
	if s := expr.GetStructExpr(); s != nil {
		for _, entry := range s.Entries {
			children = append(children, entry.GetMapKey(), entry.Value)
		}
	}
	if c := expr.GetComprehensionExpr(); c != nil {
		children = append(children, c.IterRange, c.AccuInit, c.LoopCondition, c.LoopStep, c.Result)
	}
	for _, child := range children {
		if err := checkFileReferences(child, files); err != nil {
			return err
		}
	}
	return nil
}

func run(in io.Reader, out io.Writer) error {
	decoder := json.NewDecoder(io.LimitReader(in, 16<<20))
	var req request
	if err := decoder.Decode(&req); err != nil {
		return err
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		return fmt.Errorf("expected one JSON request")
	}
	return json.NewEncoder(out).Encode(evaluate(req))
}

func main() {
	if err := run(os.Stdin, os.Stdout); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
