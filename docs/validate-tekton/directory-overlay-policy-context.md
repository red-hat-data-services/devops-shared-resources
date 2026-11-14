# Directory overlays in strict policy checks

Template directory overlays use the existing `template_overlays` mapping and ordinary overlay resolver. The renderer receives the selected `overlay_path`; no new matching mechanism is introduced.

Strict `check(original, candidate, context)` callbacks additionally receive:

- `pipeline_path`: the repository-relative POSIX path supplied by the engine's current-file walk.
- `template_overlay`: the selected overlay's resolved path relative to the trusted config directory, or `None` when the base template is used.

These fields are assigned by the caller, overriding any same-named entries in the input context. They are not derived from PipelineRun metadata, annotations, or candidate fields. The shared context object is not mutated.

A policy can compile its merged base/overlay variants at module load, then compare the candidate with the variant identified by this trusted context without callback filesystem IO. It should also validate that the path is in the explicitly mapped directory and that the selected overlay matches that mapping; missing, unknown, or mismatched identities should fail closed rather than grant an exception.

Standalone renderers that invoke the same policy must use `resolve_overlay` with an explicit repository root and pipeline path, pass the result to every fix/validation/idempotence render, and supply the same two trusted context fields when calling the guard.
