# Releasing nem_pd7day

Build the release notes from the **actual commit range**, not from the work you
remember doing. Everything below exists because of a specific failure; the
reasons are recorded so the steps do not get dropped as busywork.

## Why this document exists

`v3.1.3` shipped [#23][pr23] (2026-27 United Energy residential tariffs)
completely undocumented. The PR merged after `v3.1.2` was tagged, so it sat
merged-but-unreleased for two days and was not part of the work in the release
session. The notes were written from the PRs worked on that day, and the
upgrade note claimed "no user-facing behaviour change" — wrong, because
`DEFAULT_ENABLED_TARIFFS` changed from URTOU/PRDS to URSTOU/LVS1R, silently
altering which tariff sensors are enabled for United Energy customers.

`git log v3.1.2..HEAD` would have shown it immediately.

## Before you start

Run the helper. It prints the commit range, every PR in it, the files changed,
and any behaviour-affecting constants that need an upgrade note:

```bash
scripts/release-notes.sh          # range = <last tag>..HEAD
scripts/release-notes.sh v3.1.2   # explicit starting ref
```

Treat its `UPGRADE-NOTE CANDIDATES` section as a to-do list, not a summary.

## Checklist

### 1. Confirm the range

```bash
git fetch origin main --tags
git checkout main && git pull --ff-only
git log --oneline "$(git describe --tags --abbrev=0)..HEAD"
```

Every commit here ships. Anything you did not personally work on this cycle is
the highest-risk item in the release — that is exactly how #23 was missed.

### 2. Confirm `main` is green

```bash
gh api repos/purcell-lab/nem_pd7day/commits/main/check-runs \
  --jq '.check_runs[] | "\(.name): \(.conclusion)"'
```

All three of `validate`, `hacs`, and `hassfest` must be `success`. Do not tag a
red or in-progress `main`.

Run the suite locally too. If you have been switching branches, clear caches
first or you will chase phantom failures:

```bash
find . -name "__pycache__" -type d -exec rm -rf {} +
python -m pytest -q
```

### 3. Write the notes from the range

One section per user-visible theme, each citing its PR number. Cover **every**
PR the helper listed, including ones inherited from earlier cycles.

The `## Upgrade notes` section is not boilerplate. State plainly whether
existing installs change behaviour. Call out by name:

- changes to `DEFAULT_ENABLED_TARIFFS`, `DISTRIBUTOR_TARIFFS`, `TARIFF_NAMES` —
  these alter which entities a user has after upgrading
- added or removed sensors
- new or pinned `requirements` in `manifest.json`
- tuning constants that change forecast output: `OBSERVATION_WINDOW_DAYS`,
  `MIN_OBS`, `OLS_MIN_OBS`, `SPIKE_THRESHOLD`, `HORIZON_EDGES`

Never write "no breaking changes" without checking the helper output first.

### 4. Bump the version, in the commit you will tag

Edit `custom_components/nem_pd7day/manifest.json` and add a row at the top of
the README **Version History** table.

Both must land **before** the tag. `.github/workflows/release.yml` zips
`custom_components/nem_pd7day/` at the tagged commit, so tagging before the
bump ships a manifest whose version disagrees with the release, and HACS
reports the wrong version to users.

```bash
git commit -am "Release vX.Y.Z"
git push origin main
```

Wait for checks to go green again on the bump commit.

### 5. Tag and publish

```bash
git tag -a vX.Y.Z -m "vX.Y.Z — <one-line summary>"
git push origin vX.Y.Z
gh release create vX.Y.Z --verify-tag --title "..." --notes-file notes.md
```

`--verify-tag` refuses to invent a tag if the push silently failed.

### 6. Verify the artifact

```bash
gh run list --workflow release.yml --limit 1
gh release view vX.Y.Z --json tagName,isDraft,assets \
  --jq '"\(.tagName) draft=\(.isDraft) assets=\(.assets|length)"'
```

`assets` must be `1` — the `nem_pd7day.zip` HACS installs from. A release with
zero assets is broken even though it looks published.

### 7. Confirm on a live install

Update via HACS and check the integration loads and the logs are clean. This is
the only step that exercises the artifact the way users receive it.

## Fixing notes after publishing

Release bodies are editable and doing so does **not** disturb the tag or the
attached ZIP, so existing installs are unaffected:

```bash
gh release edit vX.Y.Z --notes-file corrected.md
```

Correct the README row in the same pass, or the two sources drift.

## Repo-specific traps

**Do not stack pull requests.** `.github/workflows/validate.yml` triggers only
on `push: branches: [main]` and `pull_request: branches: [main]`, so a PR based
on another branch gets **no checks at all**. Worse, when the base merges with
`--delete-branch`, GitHub auto-closes the stacked PR, and it cannot be reopened
once the head has been rebased or the base deleted. [#28][pr28] was lost this
way and had to be reopened as [#29][pr29]. Branch every PR off `main`.

**Test fixtures must not hard-code calendar dates.** `CalibrationEngine.fit()`
only trains on observations newer than `OBSERVATION_WINDOW_DAYS` (90). Fixtures
pinned to a fixed date silently expire: the suite was green on 18 Jun and red
on 8 Aug 2026 with no code change, because every observation aged out of the
training window and `apply()` began returning `passthrough` instead of
`isotonic`. Anchor fixtures to `datetime.now()`; the guard tests added in
[#26][pr26] fail loudly if this recurs.

**Clear `__pycache__` after switching branches**, or stale bytecode produces
failures unrelated to your change.

[pr23]: https://github.com/purcell-lab/nem_pd7day/pull/23
[pr26]: https://github.com/purcell-lab/nem_pd7day/pull/26
[pr28]: https://github.com/purcell-lab/nem_pd7day/pull/28
[pr29]: https://github.com/purcell-lab/nem_pd7day/pull/29
