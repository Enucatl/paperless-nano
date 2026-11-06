# Project conventions

- Always use Redis itself for the message broker and cache in deployments,
  development examples, and benchmarks. Use `docker.io/library/redis:8`
  for bundled Compose files; do not substitute Valkey or another implementation.
- Preserve historical validation results with the broker actually measured;
  changing broker defaults does not revalidate earlier measurements.
