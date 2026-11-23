# Mapping files for real servers

Each folder maps one open-source MCP server onto the probes' vocabulary, with
a run sheet that says what a run touches and in what order to do it.

| Folder | Server | Transport | Limits |
|---|---|---|---|
| [`almega-mcp/`](almega-mcp/) | [almega-ai/almega-mcp](https://github.com/almega-ai/almega-mcp) at `3163eb7` | stdio | set by a tool (`open_wallet`, `set_frame`) |
| [`agent-wallet/`](agent-wallet/) | [DaveForan/agent-wallet](https://github.com/DaveForan/agent-wallet) at `61f2616` | streamable HTTP | out-of-band: a mandate from the operator API, a threshold from the daemon's config |

Three rules hold for every run here:

1. **A run that sends payments through a third-party service, even in test
   mode, is the operator's to start.** The run sheets put the steps that move
   nothing first.
2. **Write a run's output outside this repository**, as the run sheets do. A
   report is copied into a server's `results/` folder only after it has been
   read for local paths, keys and anything else that should not be public,
   and only with a write-up beside it saying what it does and does not show.
3. **If a run shows a defect, tell that server's maintainers before the result
   is published anywhere**, this repository included.

The mapping files were checked against each server's `tools/list` and its
read-only and configuration tools.

| Server | What has been run from here |
|---|---|
| almega-mcp | Both servers on the in-memory backend, 2026-11-23: see [`almega-mcp/RESULTS.md`](almega-mcp/RESULTS.md). The Stripe test-mode run (step 3 of its run sheet) has not been run. |
| agent-wallet | Nothing yet. Its run sheet is how to run it; its daemon needs Node 24 or newer. |
