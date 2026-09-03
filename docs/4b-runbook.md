# Running acceptance #4b

Acceptance #4b asks for five or more real production images measured once each.
`docs/field-notes.md` has two of them. This file is how the rest get run.

The output is **a list of problems, not a pass/fail**. A run that ends in FAIL
and a run that ends in PASS are equally useful here; a run that never reached a
measurable state is the most useful of all, because it names something the tool
cannot yet do. What is being tested is the model, not the images.

These runs are not made from this repository. The images are yours, they need
your registry and your environment, and **which host a measurement was made on
is part of the record** — so the run and the field-note that describes it belong
to whoever owns the machine. This file exists to make that mechanical.

---

## Before the first run

**1. The copy has to be able to name itself.**

    rolloutkit --version
    # rolloutkit 0.1.2 (723d0f886315e10b8459f88f56ec8eb8fc662c93)

If it says `(unknown)`, the report will too, and `docs/field-notes.md` requires
every new section to name the exact harness commit. 0.1.0 installed from PyPI
always said `unknown`; every release from 0.1.1 on stamps the revision into the
wheel, and the SHA above is what 0.1.2 answers. From a checkout the revision
comes from Git instead — and Git is asked for `HEAD`, not for whether the tree
is clean, **so run from a committed tree.** A SHA next to a measurement made
from modified sources names a tree that does not exist. If the tree has to be
dirty, say so in the heading the way the 2026-08-26 note does:
`rolloutkit commit: 68816b8 plus the uncommitted phase-progress change`.

**2. Choose the in-flight endpoint now, not after the run.** See the next
section. This is the one decision that makes the difference between a run that
answers the core question and a run that has to be repeated.

**3. Know what the target needs to be up.** Every dependency's listening port,
written down. `init` cannot import them and the run cannot guess them.

---

## The in-flight endpoint, decided first

SP005 is the core contract: it is the only one that asks whether requests
already executing when SIGTERM arrives are allowed to finish. It needs traffic
that is still in flight at the moment of the signal, and there are three ways to
arrange that. Pick one **before** the first run.

**A slow endpoint, named.** The direct answer, when the service has one:

    contracts:
      inflight:
        request:
          path: /reports/generate
          expected_duration: 5s

**An ordinary endpoint under concurrency.** When nothing is reliably slow, load
one until it is. This is what `fixtures/oss/paperless-ngx/rolloutkit.yaml` does:
twenty concurrent requests against a login page turn a 13ms response into a
259-284ms one, which is a window wide enough to aim at.

    contracts:
      inflight:
        request:
          path: /accounts/login/
        concurrent: 20

**Nothing, and accept the fallback.** Leaving `contracts.inflight.request.path`
unset makes rolloutkit drive the readiness endpoint instead. This is the option
that quietly wastes a run, and it is the default, so read the next paragraph
before choosing it by omission.

### Why the fallback is a trap on a fast service

The fallback only resolves when the readiness response is wide enough to aim a
signal at: `readiness_p50 / measurement_jitter >= 10` **and** `readiness_p50 >=
3ms` (`MIN_JITTER_RATIO` and `MIN_READINESS_WINDOW_MS` in
`contracts/inflight.py`). A readiness endpoint that answers in 1ms on a quiet
host fails both, and SP005 comes back **INCONCLUSIVE** — not FAIL. The run
succeeded and the core contract was not measured.

`docs/measurements/README.md` has 240 runs of exactly this boundary. The rows
worth looking at are `set-darwin-arm64-loaded/*`, where the same configuration
resolves on four runs and refuses on the other four, because the machine's load
— not the image — decided.

Naming `contracts.inflight.request.path` explicitly skips this precondition
entirely. That is the reason to name one even when the readiness path would have
been the endpoint you chose anyway.

### Measure the endpoint twice before believing it is slow

A cached endpoint is slow once. While preparing the Paperless fixture,
`/api/remote_version/` measured 517ms on the first call and about 3ms on every
call after it — it reaches out to GitHub once and remembers. As an in-flight
target it would have measured the cache.

    curl -o /dev/null -s -w '%{time_total}\n' http://localhost:PORT/PATH
    curl -o /dev/null -s -w '%{time_total}\n' http://localhost:PORT/PATH
    curl -o /dev/null -s -w '%{time_total}\n' http://localhost:PORT/PATH

The second and third numbers are the ones that matter. Also check the status
code: a path that answers 302 or 401 is not a readiness path, and rolloutkit's
probe does not follow redirects even where the image's own healthcheck does.

---

## The sequence

### 1. Generate the config from Compose

    rolloutkit init --from-compose docker-compose.yml --service web -o rolloutkit.yaml

Compose is read, not run. Warnings on stderr name each thing that was seen and
not imported — read them, they are the list of what to write by hand:

    warning service web starts after db but does not wait for it; write
      services.db.wait_for.tcp with the port db listens on
    warning service web.depends_on.cache.condition is not imported; write
      services.cache.wait_for.tcp with the port cache listens on to wait for it
    warning service db: healthcheck is not imported; for a dependency, wait on
      the port it listens on with services.<name>.wait_for.tcp
    warning service web.env_file names docker-compose.env, which does not
      exist; the reference is imported as written, so the generated config will
      be refused until that file is there or the variables it would have set
      are written into the config's own env

Every imported dependency produces one of the first two lines, because none of
them arrives with a gate: Compose records what starts before what and never a
port. The second wording means the Compose author had already asked for a wait
and the import dropped it.

The last line is the one that stops a run before anything is pulled, and it is
the ordinary case rather than a rare one. `env_file` is imported as a reference
and never inlined — inlining would copy whatever that file holds, credentials
included, into a config about to be committed beside the Compose file — so a
project that ships its env file separately, as most do, generates a config that
cannot load. Answer it now, not at step 4: either put that file where the path
says, or open it, read what it sets, and write those variables into `target.env`
by hand. There is no third option that involves running anything.

### 2. Fill in the TODOs

Three always, in the order they matter:

| TODO | What it needs | How to get it wrong |
|---|---|---|
| `probes.readiness.path` | The real readiness route, trailing slash included | A slashless route that redirects; the probe wants the status, not the redirect |
| `contracts.inflight.request.path` | The endpoint chosen above | Leaving it null — see the trap, above |
| `deployment` | The **production** grace period, preStop and drain strategy | Copying the defaults, which describe no real deployment |

Two more appear only when Compose could not answer:

| TODO | When it appears |
|---|---|
| `target.image` | The Compose service builds rather than pulls; build and tag it first |
| `target.port` | The service publishes no port; the container port is what is wanted, not the host one |

`deployment` is worth a sentence of its own. It is not a description of the
image, it is a description of how the image is deployed, and it is what SP004
and SP006 are evaluated against. Setting `drain.strategy: none` when production
has a preStop hook produces a WARN that is true about the config and false about
the service.

**Watch what a short secret does to the report.** Redaction works by variable
name, not by value: anything matching `KEY|TOKEN|SECRET|PASSWORD` has its value
replaced wherever that string appears in the report, provided the value is at
least five characters. So `POSTGRES_PASSWORD: paperless` does not merely hide
the password — it replaces every occurrence of the word "paperless" in the
report, image name included, and the report becomes hard to read for a reason
that has nothing to do with the run. Use a value that appears nowhere else.
Note also that only names matching that pattern are redacted at all:
`PAPERLESS_DBPASS` does not match, and its value is printed.

### 3. Write the dependency gates

Every dependency that takes time to come up needs one:

    services:
      db:
        image: postgres:16-alpine
        wait_for:
          tcp: 5432
          budget: 60s
      cache:
        image: redis:7-alpine
        wait_for:
          tcp: 6379
          budget: 30s

Without a gate the target is started the moment the dependency's container
exists, which for Postgres is several seconds before it will accept a query.
What comes back then is a readiness failure naming **the target** — true about
what happened, wrong about what to go and read. This cost a session in
`docs/field-notes.md`; the gate is why it should not cost another.

The port has to be written down because nothing else knows it: a dependency
publishes nothing and `depends_on` carries no port. `init` names every gate it
cannot write and writes none of them — a generated port would be a guess, and a
gate waiting on the wrong port fails a configuration that works.

**On Docker Desktop, watch where the gate ran.** Container addresses are not
routable from a macOS host. On the sidecar path this does not matter — the wait
runs inside the run network. If the sidecar could not start and the run fell
back to host traffic, the gates are *skipped* rather than allowed to time out on
every working configuration, and the reason is recorded. Check it:

    jq '.environment.probe_location, .dependency_waits' run-1.json

Each gate reports its own `outcome` and `location`. `connected` from
`sidecar` means it really waited; `skipped` with a `skip_reason` means it did
not, and a slow dependency is back to being a race.

### 4. Run it

    rolloutkit test -c rolloutkit.yaml --format json > run-1.json

Keep the JSON. It is the only thing that says why afterwards, and a failed run
is the one worth keeping.

For the terminal reading of the same run, run it again without `--format json`;
`--repeat` is for stability questions, not for the first run.

### 5. When it does not come up

In this order:

1. **Read the log tail rolloutkit printed.** Startup failures carry the
   container's own last 120 lines, redacted. This is usually the whole answer.
2. **Check `timeouts.startup`** (the hard wall, 90s, exit 3) against
   `contracts.startup.budget` (the contract, 15s, a verdict). A slow image needs
   the second one raised, and raising it is a measurement: run it three times,
   take the slowest, multiply by three. That is the rule the Paperless fixture's
   90s came from and the rule `timeouts.startup` itself was sized under.
3. **Check the gates.** `dependency_waits` in the JSON says which dependency was
   waited for and for how long. A dependency that never accepts ends the run
   with exit 3 and prints its own logs, not the target's.
4. **Exit code 2 is a config error** and is answered without pulling anything —
   the message names the field. Running the freshly generated Paperless config
   unedited, for instance, gets this and nothing else:

       config error
       env_file not found: .../docker-compose.env

   because Compose's `env_file:` was imported as a path and upstream ships that
   file separately — which `init` warned about at step 1, so reaching it here
   means the warning was read past. Exit 3 is infrastructure. Exit 4 is a rolloutkit defect and
   is itself a finding worth writing down.

---

## What to record

Three things per image, and they are not the verdicts:

1. **Time to first measurement.** From opening the Compose file to a run that
   produced a report. This is the number the whole acceptance turns on: eleven
   minutes for service-a, nearly all of it spent finding out why the container
   would not boot.
2. **What was missing.** Everything that had to be discovered, worked around, or
   could not be expressed. Numbered, so it can be referred to later and marked
   **Fixed** when it is.
3. **Which config fields were needed.** Which ones `init` could not produce, and
   what had to be known to write them.

The verdict table goes in too, but it is evidence, not the result. An image that
measures cleanly and an image that does not are both data; an image that could
not be measured at all is the finding.

---

## Section template

Sections in `docs/field-notes.md` are chronological, so a new one goes at the
end. This is the shape of the service-a section, emptied; the angle brackets are
placeholders.

~~~markdown

## <service> (YYYY-MM-DD; rolloutkit commit: <full 40-char SHA>)

<One or two sentences: what the service is, what it runs under — the server, the
worker model, the graceful-timeout setting. Enough that a reader knows what kind
of thing was measured.>

**Time to first measurement: ~N minutes**, <where it went>.

### What was missing

1. **<One-line title.>** What happened, what the tool said, and what the actual
   cause turned out to be. If it has since been addressed, end with **Fixed**:
   and one clause saying how.

2. **<Next one.>** …

### Which config fields were needed

- <Field, and what had to be known to write it.>
- <A field `init --from-compose` could not produce, and why it could not.>
- `contracts.startup.budget: Ns` covered <what>, which measured N.NNs.

### What was measured

| Contract | Verdict | Evidence |
|---|---|---|
| SP001 startup | | |
| SP002 readiness-stability | | |
| SP003 signal-handling | | |
| SP004 drain-window | | |
| SP005 in-flight | | |
| SP006 shutdown-deadline | | |

Host: <uname / docker version / CPU count>. `probe_location`: <sidecar or
host_fallback, with the reason if it fell back>.

### Open problem: <title>

<A limitation of the model, not of this service. What the tool assumed that this
image did not satisfy, and what the options are. If there is no open problem,
delete this heading rather than writing "none" — an empty one reads as though
the question was not asked.>

~~~

## This sequence has been walked once

On 2026-09-03, against harness `b2d206e`, on H1 (`Darwin 25.5.0 / docker 29.7.2
/ 11cpu`): the upstream `docker-compose.postgres.yml` from paperless-ngx, taken
unmodified, through `init --from-compose ... --service webserver`, then the five
steps above. What the walk cost, in the terms this file asks for:

- **Three things `init` could not produce.** The gates (no port in Compose), the
  readiness path, and the in-flight endpoint — the three the TODO list names.
- **One thing it produced that had to be removed.** `env_file:
  docker-compose.env`, imported as an absolute path to a file upstream ships
  separately. It failed at load time with exit 2 and named the field, which is
  the right failure arriving in the wrong place: everything needed to say it was
  known when `init` resolved the path. That walk is where the warning above came
  from — `init` now names the missing file at generation time. What it still
  cannot do is supply it: the six variables that file carries — the database
  user, name and password, and the secret key — had to be found and written by
  hand before the target would start.
- **The verdict**: WARN. SP001 PASS (22.64s of a 90s budget), SP002 PASS, SP003
  PASS, SP004 WARN (`none_uncovered` — no drain mechanism is declared), SP005
  PASS (17/17 completed), SP006 PASS (6.36s of 30s). `probe_location: sidecar`;
  both gates connected, db after 677ms and the broker after 0.3ms.

That run used upstream's own image tags — `postgres:18` and
`valkey/valkey:9-alpine` — rather than the digests
`fixtures/oss/paperless-ngx/` pins, so it is a different image set reaching the
same verdicts by the same route. It is not a field note and is not in
`docs/field-notes.md`: it measured this file, not a production service.

---

## What this is for

The result of #4b is not a score. It is the input to the decision at the end of
M2 about whether the model holds up — whether six contracts, one config file and
one signal describe enough of what happens when a real container is asked to
stop.

Two images in, the answers were: the tool blamed the wrong container for a
dependency race (now gated), it threw away the evidence that explained its own
failures (now printed), and it assumed the user could name a slow endpoint (still
assumed, now with two ways around it). Three more images either add to that list
or fail to, and both are worth knowing.
