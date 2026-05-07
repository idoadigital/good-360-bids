# Chrome DevTools MCP Checkout Engine

This repo can optionally route Good360 auto-buy attempts through an AI agent that controls Chrome through the official Chrome DevTools MCP server.

## What This Adds

Default behavior remains unchanged:

```env
AUTOBUY_ENGINE=daemon
```

To try the new path:

```env
AUTOBUY_ENGINE=devtools_agent
DEVTOOLS_AGENT_DRY_RUN=true
OPENAI_API_KEY=...
```

When a tracked truck is newly available, `good360_monitor.py` calls `good360_devtools_agent.py` before the existing daemon/script fallback. The agent starts `chrome-devtools-mcp` over stdio via:

```bash
npx -y chrome-devtools-mcp@latest --headless --no-usage-statistics --isolated
```

The OpenAI Agents SDK connects to that MCP server, exposes Chrome/DevTools tools to the checkout agent, and asks it to log in, inspect the page, resolve page-level issues, and either stop safely or complete checkout.

## Why MCP Helps Here

The legacy checkout scripts are selector-driven. They fail when Good360 changes labels, puts payment fields in iframes, shows an unexpected modal, disables a button due to validation, or returns a page error.

Chrome DevTools MCP gives the agent access to browser state that plain selectors often miss:

- screenshots
- DOM and accessibility state
- console errors
- network failures
- frames and iframe-backed payment fields
- current URL and visible text after each action

The agent can use those signals to diagnose the actual page state before continuing.

## Safety Model

Live purchasing is disabled by default.

Dry-run mode:

```env
DEVTOOLS_AGENT_DRY_RUN=true
```

The agent must stop before `Place Order` and return `DRY_RUN`.

Live mode requires all of:

```env
DEVTOOLS_AGENT_DRY_RUN=false
DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL=true
DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE=true
```

The second flag is explicit because the agent needs Good360 login and payment data in order to type it through Chrome. That means sensitive checkout data is sent through the LLM/tool loop. Long-term, this should be replaced by tokenized payment or a deterministic local secret-fill tool.

## Required Environment

```env
AUTOBUY_ENGINE=devtools_agent
OPENAI_API_KEY=...
DEVTOOLS_AGENT_MODEL=gpt-5.1
DEVTOOLS_AGENT_TIMEOUT_SECONDS=420
DEVTOOLS_AGENT_ISOLATED=true
DEVTOOLS_AGENT_FALLBACK_ON_FAILED=false
DEVTOOLS_CHROME_EXECUTABLE=/usr/bin/google-chrome-stable
```

Also required:

- Node.js 20.19+ and npm/npx
- current stable Google Chrome or Chrome for Testing
- Python deps from `requirements.txt`, including `openai-agents`
- Good360 org credentials from `good360_orgs_master.example.json` placeholders
- card env values matching the org key, for example `CARD_HOPE4HUMANITY_*`

## Recommended Rollout

1. Keep `AUTOBUY_ENGINE=daemon` in production.
2. Run a staging container with `AUTOBUY_ENGINE=devtools_agent` and `DEVTOOLS_AGENT_DRY_RUN=true`.
3. Trigger `/test` or run `good360_monitor.py` when a real truck appears.
4. Review the agent result, evidence list, container logs, and any screenshots/images emitted by Chrome DevTools MCP.

5. Confirm the agent reliably reaches final review without clicking `Place Order`.
6. Only then enable live flags during a supervised window.
7. Keep legacy fallback enabled until the agent proves better than the current daemon path.

## Failure States

The monitor understands these agent statuses:

- `SUCCESS`: real order confirmation evidence found.
- `MISSED`: truck sold out or became unavailable.
- `MANUAL`: price or page state requires human purchase.
- `DRY_RUN`: stopped before order submission.
- `BLOCKED`: safety rule prevented purchase.
- `FAILED`: agent or MCP runtime failed. By default, the monitor does not fall back to the older checkout path when `AUTOBUY_ENGINE=devtools_agent`. Set `DEVTOOLS_AGENT_FALLBACK_ON_FAILED=true` only if you explicitly want that behavior.

## Important Limit

Chrome DevTools MCP is not itself an agent. It is a tool server. This repo supplies the agent by using the OpenAI Agents SDK with `MCPServerStdio`. That distinction matters: consistency comes from prompts, guardrails, dry-runs, logging, and rollback behavior, not from MCP alone.
