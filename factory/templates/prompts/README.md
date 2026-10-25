# Prompt templates

Every prompt sent to an LLM lives here as a Jinja2 template, never inline in Python, so prompts can be
versioned, diffed and compared across runs. The run report records which template (and its hash) each
agent call used.

Naming: `<agent>.<part>.j2`, e.g. `coder.system.j2`, `test_writer.user.j2`.
