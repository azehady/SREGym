# fault-sim services

The three processes that put `sregym.ciroos_sim` behind the observer's Evaluations tab. They run
on the eval-workspace pod, loopback-only, started by `dashctl` (`faultsim-runs`, `faultsim-chat`,
`fault-sim`), and are versioned here because the first two import this repo and drift with it.

| file | port | what it does |
|---|---|---|
| `faultsim-runs.py` | 8095 | `/evals/targets`, `/evals/runs`: renders a variant's bundle, injects, validates the symptom, posts an investigation, always recovers. Scores only live-validated variants. |
| `faultsim-chat.py` | 8094 | turns a prompt into a fault-spec patch via litellm, for the build-a-fault flow |
| `fault-sim-up.sh` | 8503 | the observer dev server for the `_wt-faultsim` worktree, on its own ports |

Never tailnet-exposed: both services run `kubectl` against a live namespace.
`$HOME/bin/*` are symlinks to these files, so the repo is the only copy to edit.
