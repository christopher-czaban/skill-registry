# Releasing

Version `0.1.0` must be published as the `agent-skill-registry` distribution from
the `christopher-czaban/skill-registry` repository. It installs the
`skill_registry` Python package and the sole CLI command `skill-registry`.

The expected distribution files are
`agent_skill_registry-0.1.0-py3-none-any.whl` and
`agent_skill_registry-0.1.0.tar.gz`. A clean, exactly pinned bootstrap uses:

```sh
uvx --from agent-skill-registry==0.1.0 skill-registry init ./my-agent-skills
```

## Candidate procedure

1. Confirm the version in `pyproject.toml` and release tag are identical; tags
   use `vX.Y.Z`.
2. Run frozen Python 3.9 and current-Python tests, source hygiene, and workflow
   validation.
3. Build one wheel and one sdist.
4. Scan archive member names and text payloads, verify required package
   resources, and install the wheel into an isolated environment with
   `PYTHONPATH` unset.
5. Generate an instance through the installed CLI, perform initial and frozen
   dependency syncs, and run validate plus representative deployment/status/
   guard smoke checks.
6. Upload exactly the inspected artifacts. Never rebuild between inspection
   and publication.

## Trusted Publishing

The release workflow separates build and publish jobs. Only the publish job is
bound to the protected GitHub environment named `pypi`, and only that job gets
`id-token: write`. Publication uses PyPI Trusted Publishing; no API token or
password fallback is supported.

The pending or active PyPI publisher must use project
`agent-skill-registry`, owner `christopher-czaban`, repository
`skill-registry`, workflow `release.yml`, and environment `pypi`. Configure it
only after local and public-repository review. The first isolated candidate
proof, first PyPI publication, and clean install verification belong to Phase
5; TestPyPI remains a contingency rather than a permanent publishing mode.

## Failure and rollback

If tests, hygiene, metadata, tag validation, or installed-wheel verification
fails, do not publish. If a defective release is published, stop further
promotion, document impact, publish a corrected version such as `0.1.1` when
safe, and yank the defective version when leaving it installable would harm
users. Published files are immutable and must never be silently replaced
under the same version.
