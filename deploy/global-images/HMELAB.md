# HMELAB deployment on evo-x2 (2026-08-13)

TencentDB Agent Memory deployed as the team-memory hub for the hmelab fabric.
Everything runs local — no cloud LLM, no Tencent services.

## Topology

| Service | URL | Container |
|---|---|---|
| Panel UI | http://evo-x2:8125/ | tdai-memory-hub |
| Knowledge API | http://evo-x2:8424/v3/ (Swagger at /docs) | tdai-memory-hub |
| Memory Core | http://evo-x2:8420/ | tdai-memory-core |
| Proxy (Anthropic+OpenAI) | http://evo-x2:8096/ | tdai-proxy |

All containers: `--restart unless-stopped`, network `tdai-memory-stack`,
volumes `tdai-memory-core-data` + `tdai-panel-data`.

## LLM wiring (all local)

- **Memory group** (extraction/wiki ingest): Ollama `qwen3.6:35b-a3b`
  via `http://172.17.0.1:11434/v1` (~60 tok/s; the 27b dense model was 5x slower).
- **Proxy upstream**: same Ollama endpoint — Ollama >= 0.32 speaks the
  Anthropic `/v1/messages` protocol natively, which the claude-code proxy
  route requires (passthrough, no protocol conversion).
- Ollama binds loopback only. Containers reach it through two
  `systemd-socket-proxyd` bridges on the docker bridge IP:
  `tdai-bridge-ollama.socket` (172.17.0.1:11434 → 127.0.0.1:11434) and
  `tdai-bridge-hermes-oauth.socket` (172.17.0.1:8018 → 127.0.0.1:8018).
  Units in /etc/systemd/system/, enabled.
- The hermes-oauth bridge (8018) is kept for later: its OpenAI route works
  (qwen3.7-plus verified) but its **anthropic provider currently 404s**, and
  it has no /v1/messages — so it can't serve the claude-code proxy path yet.

## Org structure (ids in .hmelab-ids, key in .hmelab-key — both gitignored)

- user `hmelab` = usr-0oepho63os (business user; admin key in .admin-key)
- team `hmelab` = team-0oe499vgbk
- agent `hermes` = agt-0oe4jqndj1
- wiki `hmelab-vault` = wiki-hgeyl9mb

## Vault → Wiki ingestion

`hmelab-vault-ingest.py` uploads folders from /ssd/obsidian/hmelab in
batched raw/write calls and triggers the async LLM ingest:

```bash
cd /ssd/gits/TencentDB-Agent-Memory/deploy/global-images
python3 hmelab-vault-ingest.py operations decisions learning-briefs   # done 2026-08-13
python3 hmelab-vault-ingest.py research systems security people      # bulk (~365 files, hours)
```

Notes: raw/write returns **409 while an ingest is running** — wait for
status != processing. `library/` (1,020 files) deliberately not ingested yet.
Ingest is incremental: re-running only processes new/changed files.

## Client hookup (Claude Code and friends)

```bash
export ANTHROPIC_BASE_URL=http://evo-x2:8096/claude-code/default
export ANTHROPIC_AUTH_TOKEN="$(cat .hmelab-key)"   # sk-mem-... business user key
claude --model qwen3.6:35b-a3b
```

First turn per session: proxy asks Team → Agent → Task via AskUserQuestion;
afterwards it injects L2/L3 memory + skills + wiki knowledge each turn and
captures the conversation as L0 for background L1→L2→L3 extraction.

MCP alternative (knowledge only): `MemoryKnowledge/bin/mcp.mjs` with
`KNOWLEDGE_API_URL=http://evo-x2:8424/v3` exposes wiki_search / wiki_read /
code_* tools (needs node >= 22).

## Security caveats

- Ports 8125/8424/8420/8096 are published on 0.0.0.0 and **ufw is inactive**
  — LAN-reachable. Knowledge API has no auth. Fleet LAN is trusted; revisit
  if that changes.
- `MEMORY_CORE_GATEWAY_API_KEY` must stay EMPTY in this release or the
  proxy's session-init breaks (documented upstream).

## Bulk ingest postmortem (2026-08-14/15)

The overnight bulk pass reported ready but only 13/396 sources extracted.
Root cause: **Ollama KV-cache/context corruption** at 262k context with
checkpoint reuse — responses regurgitated content from earlier requests
(wrong-document answers), so extraction plans and FILE blocks were garbage.
Confounders chased on the way: qwen3.6's thinking/content split is unstable
(FILE blocks can land in the hidden reasoning field), disabling reasoning
makes the model skip the FILE protocol, and the hub never sets temperature
(model default 1.0).

Fixes now in place:
- `OLLAMA_CONTEXT_LENGTH=32768` (systemd drop-in `ollama.service.d/context.conf`)
  + Ollama restart to flush the poisoned cache.
- `llm-shim.py` (systemd unit `tdai-llm-shim.service`, 172.17.0.1:11439):
  pins temperature 0.6, merges reasoning back into content when FILE blocks
  land there, repairs near-miss `FILE path=` markers, logs per-call metadata
  to /var/log/tdai-llm-shim.jsonl. MEMORY_LLM_BASE_URL points at the shim.
- Re-running `wiki/ingest` retries failed sources (incremental).
