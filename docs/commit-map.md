# Commit map for the 2026-08-26 history rewrite

Every commit in this repository was rewritten on 2026-08-26. The author and
committer identity carried a work email address on 47 of the 48 commits, and
that address is the one thing the content sanitisation could not reach: `git
grep` reads blobs, and an identity lives in the commit header. Rewriting was the
only way to remove it, and it had to happen before the first tag, because a
published tag is a SHA other people record.

Rewriting changes every SHA. That breaks a chain this project relies on, so the
break is written down here rather than left to be discovered.

## What refers to a SHA

- `docs/field-notes.md` dates its sections by the commit the measurements were
  taken at. Nine distinct rolloutkit commits are named there, and all nine are
  in the table below.

  Two further SHAs in that file are **not** rolloutkit commits and are not
  affected by this rewrite: `efa24f341b58` is service-b's own source commit, and
  `ece4949723f0` is the throwaway sidecar-probe spike harness, which never lived
  in this repository. Looking either one up in the map and finding nothing is the
  correct result, not a gap.
- Every JSON report rolloutkit writes carries `rolloutkit_commit`, and the
  measurement corpus on disk holds 208 of them. All 208 predate the rename from
  `preflightkit` and spell that field `preflightkit_commit`; the value is the
  same SHA under either spelling, and is subject to the same translation.
- `--version` prints the source commit of the running tool.

A SHA recorded before the rewrite names a commit that no longer exists. It is
not wrong — it named something real when it was written — but it cannot be
looked up in this repository any more without going through the table below.

## How to use it

    git log --format='%H %s' | grep <new>          # what a new SHA is
    grep '^<old>' docs/commit-map.txt              # what an old SHA became

The machine-readable form is `docs/commit-map.txt`, two columns, old then new,
exactly as `git filter-repo` wrote it.

## The map

Oldest first. The subject is the rewritten commit's; only the identity changed,
so the tree of every commit is byte-identical to what it was before.

| old | new | subject |
| --- | --- | --- |
| `27cb8357ff8b` | `4fcd692e8ae2` | Initial commit |
| `81c220c5c50b` | `0e93cf49acff` | feat: ship lifecycle contracts through SP002 |
| `d86f2ce6280e` | `dc300bd61b20` | feat: add configless lifecycle CLI |
| `6259c9e75e08` | `27ce3378c036` | feat: add SP005 fallback and Compose import |
| `23b78d7234c8` | `80e0cb1875ba` | ci: record harness revision and run Linux matrix |
| `768ce9fb565b` | `02d475c7078e` | ci: pin Ruff version |
| `0e9626aeab42` | `16aad9d669c7` | ci: preload lifecycle probe image |
| `43290730f546` | `df61217e4d5d` | test: stabilize Linux lifecycle evidence |
| `a06c56f128e5` | `7dc87f10b46f` | test: avoid sub-resolution branch assertions |
| `a67c135261f1` | `f1a2113c88f3` | test: close drain fixture listener synchronously |
| `1265fe432b87` | `5e5b13e2dec1` | feat: add SP005 completion rate evidence |
| `d05b2e87b6b2` | `51040d6a9093` | docs: record sidecar probe predictions |
| `ece4949ee6d7` | `7a5007e2454a` | spike: add sidecar traffic probe harness |
| `c3e7657d53d3` | `1dbf0420a3e4` | docs: record sidecar probe measurements |
| `622c94157f22` | `9a1c6cd53b5f` | feat: make sidecar the primary traffic probe |
| `ee2ab03b8078` | `cc04e19f71bc` | fix: stabilize sidecar calibration fixtures |
| `68c24bd5369f` | `7a63eb60cfbb` | test: widen timing fixture margins |
| `1259c74efb69` | `4e67288e87b0` | fix: preserve no-drain warning precedence |
| `616ab82e7585` | `dab906bea073` | test: close immediate fixture listener synchronously |
| `75366ef1cfc8` | `0490c9574d8a` | test: widen sidecar drain margin fixture |
| `2180131f2d15` | `a0b7b7564552` | docs: record product sidecar measurements |
| `68816b8ecf7e` | `531cd231b880` | feat: expose prediction phase timings |
| `af54dd62d20c` | `a9d18ec1dcba` | feat: announce each phase on stderr |
| `e2ddf42407e7` | `79649a846c03` | docs: name every verdict branch in explain |
| `9e06b8a7bb18` | `fa7c1aaa4335` | docs: record prediction duration and phase distribution |
| `9014b92350ef` | `371b2016a497` | test: widen the go fixture in-flight window |
| `6c2b50a48d81` | `8a8a78cc0ac8` | test: gate matrix rows on the window they count in |
| `7d1cc89d11b1` | `064800ee9de3` | ci: split the fast suite from the Docker matrix |
| `ecfe1d4693b8` | `956f54633362` | docs: record the SP005 in-flight window audit |
| `e4ed953a0061` | `79686149f321` | test: classify branches by the evidence they need |
| `3fcf6fc13558` | `904905126130` | docs: name the readiness-fallback resolution limit |
| `d9b1a10e721c` | `9956c9e51eb7` | feat: reject an unresolvable drain window at config time |
| `394996b0bd0a` | `f920bc4fb540` | test: cover the teardown floor live, classify what it feeds |
| `4b84fec6639c` | `32a229e6c503` | feat: keep the readings a resolution threshold is chosen from |
| `a738ee65314a` | `27089df6d5d2` | test: stop the configless CLI test betting on the fallback |
| `72f17c8f45b7` | `089a9a828217` | test: hold Python branch claims to the same registry |
| `57aa993f719c` | `90a71d8a46c3` | feat: add a run harness for the duration measurement |
| `7b55cdba0bda` | `6256b24545ea` | docs: record what the fallback ratio actually tracks |
| `5aad8d6cded2` | `4c6f51b4443c` | feat: run the same measurement set on every host |
| `25594333c645` | `345b37fb2097` | feat: measure the fallback case the rule should resolve |
| `648891e92f9e` | `b855cefb9936` | feat: sweep the band where the ratio rule is decided by the machine |
| `74982e8ca266` | `31f40c83800c` | feat: measure how repeatable the fallback decision is |
| `31a8b1a37586` | `af2ccd3c3456` | docs: record three hosts, pipeline cost and the fallback recommendation |
| `22ed2e768075` | `c48820cd6b7c` | feat: guard the SP005 readiness fallback with an absolute window floor |
| `eb65b3227653` | `a1c5cb8f9794` | test: prove the SP005 fallback refusal with a live image again |
| `db5059487837` | `d3f906877f1e` | ci: let the measurement set take extra configs from a dispatch |
| `df5cc814ff32` | `51032e3f7878` | chore: prepare the 0.1.0 release, minus the README |
| `a61795461e3d` | `56ddd5106986` | test: rename the 25ms fallback row and measure both new rows on both hosts |

48 commits. No commit was dropped, added, reordered or otherwise
changed: `git filter-repo --mailmap` rewrites the author and committer fields
and nothing else, and the count before the rewrite was 48 as well.

## What the rewrite did not change

The committer `GitHub <noreply@github.com>` on the one web-created commit is
left alone. It is GitHub's own identity for an operation performed in the
browser, not a personal or client address.

`user.email` and `user.name` are now set in this repository's own `.git/config`,
so a future commit here cannot pick the wrong identity up from the global one.
