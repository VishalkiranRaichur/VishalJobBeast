# OmniRoute

**Optional.** [OmniRoute](https://github.com/diegosouzapw/OmniRoute) runs locally as one
OpenAI-compatible endpoint in front of several AI providers. Job Beast work
(tailoring, JD parsing, coding agents) can go through it so one provider's
quota running out falls back to the next instead of stopping.

- Endpoint: `http://localhost:20128/v1`
- Dashboard: `omniroute dashboard`
- Model to use: `jobbeast` (a priority combo; see below)

Settings live in `.env` (`JOBBEAST_LLM_BASE_URL`, `JOBBEAST_LLM_API_KEY`,
`JOBBEAST_LLM_MODEL`). Provider credentials stay inside OmniRoute, never in this repo.

## Providers

Connect whichever providers you use with your own credentials; the models below are only examples.

| Provider | How it connects | Notes |
| --- | --- | --- |
| Groq | API key | Example models: `groq/openai/gpt-oss-120b`, `groq/openai/gpt-oss-20b` |
| Codex | ChatGPT sign-in (OAuth) | Example models: `cx/gpt-5.6-sol`, `cx/gpt-5.6-terra`, `cx/gpt-5.6-luna`. Re-auth: `omniroute providers auth codex` |
| Gemini | API key (free tier) | Example models: `gemini/gemini-3.8-flash`, `gemini/gemini-3.5-flash-lite` (2.5 models are retired; free tier rate-limits often) |
| Cerebras | API key (free tier) | `omniroute providers add cerebras --credential-stdin` |
| OpenRouter | API key (free `:free` models) | `omniroute providers add openrouter --credential-stdin` |

`--credential-stdin` shows no prompt: paste the key, press Enter, then Ctrl+D.
It keeps the key out of shell history.

## The `jobbeast` combo

Priority order: the first healthy model wins, the rest are fallbacks.
Example order: Codex (`sol` → `terra` → `luna`), then Gemini (`3.8-flash` → `3.5-flash-lite`),
then Groq (`gpt-oss-120b` → `gpt-oss-20b`). A rate-limited model is skipped while it cools down.

```bash
omniroute combo list
# to change the order or add a provider, recreate it:
omniroute combo delete jobbeast --yes
omniroute combo create jobbeast --strategy priority \
  --models "cx/gpt-5.6-sol,cx/gpt-5.6-terra,cx/gpt-5.6-luna,gemini/gemini-3.8-flash,gemini/gemini-3.5-flash-lite,groq/openai/gpt-oss-120b,groq/openai/gpt-oss-20b"
```

Job Beast's variables are named `JOBBEAST_LLM_*`, not `OMNIROUTE_*`: the
`omniroute` CLI loads `.env` from the current folder and treats
`OMNIROUTE_BASE_URL` as its own server address.

Call the combo as the model name (`-m jobbeast`). The CLI's `--combo` flag
forwards an extra `combo` field that Groq rejects with HTTP 400.

## Filling applications without Claude credits

```bash
make operate              # next eligible job
make operate JOB=<job_id> # a specific job
SUBMIT=0 make operate     # fill but do not authorize submission
```

`scripts/operate.sh` starts OmniRoute if needed, prepares the handoff
(`jobbeast handoff --authorize-submission`), then opens Claude Code with
Claude in Chrome through OmniRoute (`omniroute run claude --model jobbeast-operator`).
Claude Code keeps its tools and the operator prompt's safety rules; Codex/Gemini
do the thinking. When the session ends it prints the models used, saves them to
`output/<job_id>/models_used.json`, and runs `import-result`.

`jobbeast-operator` is a separate, strong-models-only combo because it auto-submits:
Codex `gpt-5.6-sol` → Codex `gpt-5.6-terra` → Gemini `gemini-3.8-flash`. It has no
small fallbacks; if all three are unavailable the run fails instead of using a weaker model.

Claude credits stay as the backup: run plain `claude` for a Claude-backed session.

## Which model is in use?

- Live, one line per request: `make ai-watch` (in a second terminal)
- After a run: printed at the end of `make operate`, and in `output/<job_id>/models_used.json`
- Last 24 hours: `make ai-models`
- Every response also carries `x-omniroute-model` and `x-omniroute-provider` headers.

Claude Code shows the combo name (`jobbeast-operator`) as its model and logs a
harmless `unrecognized_model` warning; the real model is the one OmniRoute reports.

## Check it works

```bash
make ai-check
```

## Using it from code or another tool

Any OpenAI-compatible client works. Create a client key in the dashboard
(Endpoints) and put it in `JOBBEAST_LLM_API_KEY`:

```python
import os, requests
r = requests.post(
    f"{os.environ['JOBBEAST_LLM_BASE_URL']}/chat/completions",
    headers={"Authorization": f"Bearer {os.environ['JOBBEAST_LLM_API_KEY']}"},
    json={"model": os.environ.get("JOBBEAST_LLM_MODEL", "jobbeast"),
          "messages": [{"role": "user", "content": "Hello"}]},
    timeout=60,
)
print(r.json()["choices"][0]["message"]["content"])
```

Job descriptions sent through any model are still untrusted data (see `AGENTS.md`).
