# Project instructions

- Prefer Chinese for project discussion; preserve command names, paths and error codes.
- Runtime code uses the Python standard library. Build tooling is separate.
- Use UTF-8 explicitly when reading or writing text, especially on Windows.
- Begin with README.md and docs/RELEASE_STATUS.md; keep support claims consistent with actual validation.
- Default operations remain offline. Network retrieval and notifications require their explicit flags and appropriate user authorization.
- Never output credentials or original private service responses. Use synthetic mail fixtures in tests and parser examples. Curated public paper showcases must document attribution, reuse permission and translation changes; never copy private email metadata.
- Do not batch-delete files or directories. Keep test artifacts under ignored data/test-runs/.
- Preserve stable article IDs, cached progress and per-recipient delivery history. Unknown sends are not automatically replayed.
- Validate relevant changes with unittest and scripts/check_release.py. Validate built packages with scripts/check_distribution.py when packaging changes.
