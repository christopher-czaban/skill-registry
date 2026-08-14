# Contributing

Contributions are welcome when they preserve the engine/instance boundary:
engine code is reusable and public, while skills, target inventories, local
overrides, proposal evidence, and productive deployment state belong to user
instances.

## Development setup

Install [uv](https://docs.astral.sh/uv/) and synchronize the locked environment:

```sh
uv sync --frozen --python 3.9
uv run --frozen python -m unittest discover -s tests -v
```

Repeat the full suite on the current supported CPython release. Tests must use
temporary external registry roots and must not depend on a developer's home,
private checkout, installed skills, or productive targets.

## Privacy and artifacts

Before opening a pull request, run the public hygiene gate and inspect both
package formats:

```sh
uv run --frozen python scripts/check_public_hygiene.py --root .
uv build --wheel --sdist --out-dir dist
uv run --frozen python scripts/check_public_hygiene.py --root . \
  --artifact dist/agent_skill_registry-0.1.0-py3-none-any.whl \
  --artifact dist/agent_skill_registry-0.1.0.tar.gz
```

Do not add user skills, real target paths, local profiles, credentials,
deployment records, proposal payloads, private repository names, or copied
archives. Fixtures should use names such as `example-skill` and temporary
paths.

## Pull requests

- Keep changes focused and explain any compatibility impact.
- Add deterministic tests for behavior changes and failure modes.
- Run the frozen test matrix, hygiene gate, package build, and installed-wheel
  smoke test.
- Do not weaken ownership, validation, privacy, or release gates to make a
  failing test pass.
- Do not publish packages from a pull request. Release publication is handled
  only by the protected release workflow.
