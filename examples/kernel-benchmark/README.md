# Kernel benchmarks

Run an existing kernel evaluator locally or on [KCoral](https://github.com/cmu-catalyst/kcoral)
with the same project files and evaluator arguments.

## Table of Contents

- [Install](#install)
- [Usage](#usage)
- [Validation](#validation)
- [Maintainers](#maintainers)
- [Contributing](#contributing)
- [License](#license)

## Install

On the agent machine, install the client with `pip install kcoral` and keep
`kcoral` on `PATH`.
Install the server and your evaluator's dependencies on the GPU machine, then
start it once (or use an existing server or Router):

```sh
kcoral server --device gpu --gpus 0 --host 127.0.0.1 --port 8000
```

The user or service manager owns the server. Humanize only connects to its URL;
several flows can share it. Keep the code-execution endpoint on a trusted network.
The agent machine needs no CUDA, PyTorch or Triton for remote benchmarks.

## Usage

From your existing kernel project, select the backend and evaluator once:

```sh
hmz exec -f /ABS/flowverse/flows/kernel -a agent=codex/gpt-5.6-sol:high \
  -p backend=kcoral -p url=http://127.0.0.1:8000 \
  -p 'evaluator=python evaluate.py' -b duration=30m 'Optimize the kernel'
```

The flow supplies the agent's benchmark command and reuses `ralph_loop`.
Choose `backend=local` for local evaluation. `url` can come from `KCORAL_URL`;
use an explicit parameter for a daemon started before that variable was set.
A remote container or SSH agent must be able to reach the URL itself.

The standalone runner also works with any existing flow or recorded evaluator:

```sh
export KCORAL_URL=http://127.0.0.1:8000
python /ABS/flowverse/tools/kernel_benchmark.py --backend kcoral -- python evaluate.py
```

No upload directory is required. Remote runs snapshot the current project's
tracked and untracked files, respecting `.gitignore`; `.git`, virtualenvs,
`node_modules`, `.humanize` and Python bytecode caches are excluded. Keep secrets
ignored. Symbolic links and special files are refused. Use `--bundle DIR` to
select another directory. Local evaluation runs in the original directory.

For the included Triton example, run from this checkout:

```sh
python tools/kernel_benchmark.py --backend kcoral \
  --bundle examples/kernel-benchmark/experiment \
  --fetch results --out /tmp/trial-001 -- python evaluate.py
```

The report is `/tmp/trial-001/experiment/results/report.json`. Choose a new
output directory per trial; `--fetch` paths are relative to the project.
Inside an agent turn, choose a new output under `.humanize/` to keep writes in its workspace.
Ordinary exit codes and output are preserved, including artifacts after failure.
There is no fallback to local execution or automatic benchmark retry. The
server caps `--timeout` (default 300 seconds); interrupting the client may leave
the request running until its deadline, and hard timeouts can lose artifacts.
Correctness and timing remain the evaluator's responsibility. HTTP duration is
not kernel latency; the example is a smoke measurement, not a ranking standard.

## Validation

```sh
python -m pytest tests/test_kernel_benchmark.py tests/test_kernel_flow.py
KCORAL_TEST_URL=http://127.0.0.1:8000 python -m pytest tests/test_kernel_benchmark.py
```

The second command enables real upload/execute/download tests on a CPU or GPU
server. Use the Triton example above to verify actual GPU execution.

## Maintainers

[humanfia](https://github.com/humanfia).

## Contributing

Keep evaluator semantics independent of the execution backend and add regression tests.

## License

See the repository's licensing terms.
