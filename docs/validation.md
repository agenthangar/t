# Selective visual validation

Inspect the rendered states needed for the current decision, while retaining all
project-required delivery checks. Screenshot selection changes what is attached
to an agent conversation, not which correctness requirements apply.

| Change | Required inspection |
| --- | --- |
| Small component adjustment | Affected component and states |
| Layout or responsive change | Affected breakpoints and surrounding layout |
| Shared component or token change | Gallery and affected consumers |
| Delivery checkpoint | All project-required rendered-state checks |

Keep screenshots locally with descriptive filenames and record the source
revision, viewport, scenario and result. Attach only images needed for the
current decision; use local paths to retrieve others. Reuse an existing capture
when the rendered source and relevant state are unchanged. Capture again after
relevant edits or state changes. Overflow, visibility and interaction checks
supplement visual inspection; passing DOM/geometry checks alone is not a visual
review. Do not describe unavailable browser rendering as verified.

For terminal work follow [the terminal contract](../ui/README.md). Inspect
80×24, 44×20 and 32×12 views where affected, including long rows and monochrome.
Use isolated sample data and HOME/XDG/tmux resources. A planning HTML preview is
separate from validation of the implemented command. Follow the resolved workflow
and explicit user decisions for approval timing; this guide adds no approval gate.

Update reusable design guidance, shared components, tokens and gallery examples
when applicable. Register new custom rendered states in `ui/layout-contract.json`.
At delivery run command lint and required Design adherence fixtures, plus all
required CI and coverage checks. Preserve native macOS desktop opt-in; browser
or headless fixtures do not authorize native window tests.

## Recorded exercise format

Record the isolated preview and source revision; component and responsive changes;
screenshots retained and attached; actual visual observations; overflow, visibility
and keyboard checks; and any unavailable checks. Compare before/after only when
both captures exist. Never infer a reduction in redundant captures from a single
image or claim an external skill was updated without locating and editing it.
