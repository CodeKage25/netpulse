# Deploying NetPulse

Most people should not deploy anything. Read the first section before the rest.

## Which of these do you actually want

NetPulse measures a network from inside it. The router sits on a private address —
`192.168.0.1` — that nothing outside the house can reach, and the per-application and
per-service figures come from the machine NetPulse runs on. **A copy running in a
datacentre would measure the datacentre.** That is not a configuration problem to work
around; it is what a local-first monitor is.

So there are three shapes, and the first two cover almost everyone:

| You want | Run | Infrastructure |
|---|---|---|
| To watch your connection | `netpulse run` on a laptop or a Pi | none |
| The same, from your phone, anywhere | the above, on a [Tailscale](https://tailscale.com) network | none |
| A hosted dashboard with a public URL, history that outlives the laptop, several agents | the split below | a Fly account |

If the honest answer is "I want to see my dashboard when I'm out", **use Tailscale**. It
takes about ten minutes, changes no code, keeps every measurement, and exposes nothing.
Deploying is a weekend and a monthly bill by comparison.

The split is worth it when you want history that survives the machine sleeping, more
than one house or box reporting into one place, or a URL you can open without a VPN.

## The split, in one picture

```
   your house                                  fly.io
   ┌──────────────────────────┐                ┌────────────────────────┐
   │ netpulse run --push-to … │ ──HTTPS,gzip─► │ netpulse run           │
   │  · polls the router      │   bearer token │  · accepts pushes      │
   │  · probes the connection │                │  · keeps the history   │
   │  · keeps its own store   │                │  · serves the dashboard│
   └──────────────────────────┘                └────────────────────────┘
        measures everything                        measures nothing
```

The agent is an ordinary install. Its local dashboard still works, its store is still
the original, and the push is a second reader of that store. Nothing about the measuring
depends on the far end existing.

### What the split gains

The hosted side can report an outage the agent cannot. A monitor at home goes quiet
during its own outage — it is on the far side of the break — so it can only ever tell
you about one afterwards. Silence upstream is a signal, and `/api/agents` reports it.

### What it costs

Real data, over the connection being measured. Batches are gzipped and sent once a
minute rather than per poll; in practice a busy minute is a few hundred bytes. The agent
records what it spent as `agent.push_bytes` so you can chart it and argue with it.

## Deploying the hosted half

To **your own** Fly account. Everything below is one household's.

```bash
git clone https://github.com/CodeKage25/netpulse && cd netpulse

fly launch --no-deploy --copy-config
fly volumes create netpulse_data --size 3 --region jnb

# Two different secrets, deliberately. See "why two" below.
fly secrets set NETPULSE_DASHBOARD_TOKEN="$(openssl rand -base64 24)"
fly secrets set NETPULSE_INGEST_TOKEN="$(openssl rand -base64 24)"

fly deploy
fly secrets list          # names only; read the values back from where you saved them
```

Open `https://<your-app>.fly.dev`. The browser prompts for a password: **any username**,
and the password is `NETPULSE_DASHBOARD_TOKEN`.

## Pointing an agent at it

On the machine that can see the router:

```bash
export NETPULSE_INGEST_TOKEN='the ingest one, not the dashboard one'
netpulse run --push-to https://<your-app>.fly.dev --agent-name home
```

`--agent-name` becomes the prefix on every source it ships (`home/wan`, `home/zlt`).
Give each agent its own: every install calls its probe `wan`, and two of them under one
name would interleave into a single series that looks like one connection behaving
impossibly.

To run it as a service, the usual `launchd` plist or `systemd` unit — the only
requirements are that `NETPULSE_INGEST_TOKEN` is in its environment and that it restarts
on boot.

## Why two tokens

They are used by different things for different reasons, and collapsing them would be a
downgrade:

- **`NETPULSE_DASHBOARD_TOKEN`** is for people, over HTTP Basic. It guards everything,
  reads included — somebody who can list your devices and read your outage history knows
  the shape of your day and when the house is empty.
- **`NETPULSE_INGEST_TOKEN`** is for agents, as a bearer token, and opens only
  `/api/ingest`. An agent may live on a box you trust less than your laptop, with its
  token sitting in a config file on it. Reading that file should not also hand over the
  device list and the block button.

Both are read from the environment only, never from the config file, and both must be at
least 16 characters. Behind these endpoints are a write to your router (`/api/block`) and
a speed test that moves about 30 MB of metered data per call.

**Binding anything other than loopback without `NETPULSE_DASHBOARD_TOKEN` will not
start.** It prints what to do and exits 2. That is deliberate: a warning is read after
the thing is already running, usually by the person who did not need the warning.

## Things that will bite you

**Do not scale past one machine.** Storage is SQLite on a volume; a volume attaches to a
single machine and SQLite takes a single writer. A second machine would not share the
history, it would quietly keep a different one. `fly.toml` pins this.

**Do not enable auto-stop.** A suspended machine cannot tell "the house is offline" from
"I was asleep", so it would manufacture exactly the gaps this project refuses to paper
over. `min_machines_running = 1`.

**Put the region near the agent**, not near you. `primary_region = "jnb"` is the closest
Fly region to Lagos.

**Back-ups are cheap and mostly unnecessary.** `fly volumes snapshots list` covers you,
but note the shape of this design: the agent keeps its own store, so the hosted database
is a *replica*. Losing the volume costs you a dashboard, not your history — point a
fresh instance at the same agents and reset their cursors to refill it.

## Running it for other people

Don't, unless you have thought hard about it. Hosting other households means becoming the
custodian of when their homes are empty, what they stream and what is connected —
regulated personal data with real breach exposure, in exchange for a convenience they can
have for free over Tailscale. Every user needs an agent at home regardless, so what your
cloud adds is storage and a URL.

If you want other people to use NetPulse, the thing to give them is this document and a
`fly launch` on their own account. It costs you nothing, they keep their own data, and
there is no tenancy code to write.

The ingest path is namespaced per agent, so one instance already accepts several agents
cleanly — a Mac, a Pi, a second house. That is multi-agent, not multi-user: there are no
accounts, and everyone who has the dashboard password sees everything.
