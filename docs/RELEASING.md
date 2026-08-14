# Releasing

Phase 4 prepares the release path but does not publish version `0.1.0`.

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

The orchestrator will configure environment protection and the pending or
active PyPI publisher only after local and public-repository review. The first
TestPyPI or isolated candidate proof, first PyPI publication, and clean install
verification belong to Phase 5.

## Failure and rollback

If tests, hygiene, metadata, tag validation, or installed-wheel verification
fails, do not publish. If a defective release is published, stop further
promotion, document impact, publish a corrected version when safe, and yank
the defective version when leaving it installable would harm users. Published
files are immutable and must never be silently replaced under the same
version.
