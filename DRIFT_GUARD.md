# Drift Guard

Use the release guard before and after Railway deploys so we stop guessing which code is live.

## What it checks

`scripts/release_guard.py` fails if:

- you are not on `main`
- tracked files are dirty
- `HEAD` is not pushed to `origin/main`
- Railway is serving a different git SHA than local `HEAD`

By default it is strict about untracked files too. That is intentional, because forgotten local scripts are one of the easiest ways to think a fix exists when it was never released.

## Typical use

From the repo root:

```bash
python scripts/release_guard.py --wait-seconds 300
```

That will:

1. verify the working tree is clean
2. verify `HEAD == origin/main`
3. poll the live observation endpoint until Railway reports the same SHA

## If you have intentional scratch files

You can temporarily allow them:

```bash
python scripts/release_guard.py --allow-untracked --wait-seconds 300
```

That should be the exception, not the default.

## What to clean up long term

- keep generated runtime JSON files ignored
- keep scratch analysis scripts in a separate folder or separate repo
- do not redeploy from a dirty tree
- always verify the live SHA after push/deploy
