"""Mockup builder helpers — Python module invoked by the mockup-builder.ts skill.

Each subcommand is a thin wrapper over existing cc-leadgen utilities,
returning JSON for the TypeScript tool layer to consume.

Run via:
    python3 -m app.workers.mockup_helpers <subcommand> [...args]

Subcommands:
    load_lead <lead_id>
    clone_template <template> <slug>
    write_config <slug> --client-ts-file=<path> --brand-ts-file=<path>
    copy_assets <slug> --logo=<path> --hero=<path> --gallery=<csv>
    build <slug>
    deploy <slug>
    verify <url> <vertical>
    write_approval <lead_id> <mockup_url> <recommendation-json>
"""
