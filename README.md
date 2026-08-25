# GameLogicBench

An agent benchmark for the game domain. A solution under test completes a generation or development task inside a deterministic Godot engine; the harness grades it in an isolated container using black-box behavioural assertions, then re-judges the same solution along perturbation axes — new seeds, larger scale, out-of-distribution scenarios.

**Grading is fixed deterministic code, not an LLM rubric.** That is the substantive difference from judge-agent style evaluation: a verdict is reproducible, and it cannot be talked into existence. Assertions read only observable quantities from the simulation — never the solution's source, never its self-reports.

A few consequences of that stance, which shape the whole harness:

- **The judge is frozen and the agent never sees it.** The project root is assembled by copy order (`game → solution → judge` last), so an agent editing a frozen file has no effect, and hidden seeds travel by argv and never touch the filesystem. Tamper resistance comes from the copy order, not from permissions.
- **Geometry has exactly one authority.** The same seed-driven level code builds the world for the agent's preview and for grading, at different seeds. What is withheld is the seed value, never the algorithm — so a solution fails on its own missing robustness, not on a harness trick.
- **Held-out scenarios, not held-out rules.** Each task declares a scenario table: the agent develops against `baseline`, and the judge additionally applies hand-designed harder scenarios from the same, already-disclosed rule family.
- **Composite tasks arm one axis at a time**, so a first failure can be attributed to a specific capability rather than collapsing into "it failed somewhere".

The task library — game projects, frozen judges, reference solutions, assets — lives in its own repository, [NJU-LINK/GameLogicBench-Tasks](https://github.com/NJU-LINK/GameLogicBench-Tasks), and is not tracked here. Clone it to `tasks/` under this repository's root before running anything; the directory name must be exactly `tasks`, since the CLI resolves `./tasks` relative to the repository root.

The harness package is named `geb`, so the CLI entry point is `python -m geb.cli`.

## Documentation

| | |
|---|---|
| Code organisation and mechanisms | [`docs/structure.md`](./docs/structure.md) |
| Environment and how to run it | [`docs/running.md`](./docs/running.md) |
| Reading results: every field and enum in `status.json` / `result.json` | [`docs/status-fields.md`](./docs/status-fields.md) |
| Changing the code, adding a backend | [`docs/maintenance.md`](./docs/maintenance.md) |

Start with [`docs/running.md`](./docs/running.md): it covers the task library, the container image, the model registry, and the full command surface.

> The task-authoring discipline (the three-layer information boundary, judge health criteria) and the acceptance gates are documented in the paper's appendix.
