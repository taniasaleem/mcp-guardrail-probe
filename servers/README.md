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
2. **Keep the reports out of this repository.** Write them somewhere outside
   it, as the run sheets do.
3. **If a run shows a defect, tell that server's maintainers before the result
   is published anywhere.**

The mapping files were checked against each server's `tools/list` and its
read-only and configuration tools. The payment calls in them have not been run
from here; the run sheets are how to run them.
