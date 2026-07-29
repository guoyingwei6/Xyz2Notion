# Schemas

- `config.schema.json`: public, secret-free `config.yaml` contract.
- Runtime credentials are intentionally excluded and must come from environment
  variables or GitHub Actions Secrets.

Validate a configuration locally:

```bash
uv run xyz2notion config-check --config config.example.yaml
```
