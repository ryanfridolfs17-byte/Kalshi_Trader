import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_URL = "https://kalshitrader-production.up.railway.app/api/observation"


def run_git(*args):
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def parse_status():
    output = run_git("status", "--porcelain=v1", "--untracked-files=all")
    tracked = []
    untracked = []
    for raw in output.splitlines():
        if not raw:
            continue
        status = raw[:2]
        path = raw[3:]
        if status == "??":
            untracked.append(path)
        else:
            tracked.append(raw)
    return tracked, untracked


def fetch_live_sha(url):
    with urlopen(url, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return (
        payload.get("runtime_fingerprint", {}).get("railway_git_commit_sha", ""),
        payload,
    )


def fail(message):
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Guard against local/remote/live deployment drift."
    )
    parser.add_argument("--branch", default="main", help="Required release branch")
    parser.add_argument("--remote", default="origin", help="Git remote to compare against")
    parser.add_argument("--url", default=DEFAULT_URL, help="Public observation/health endpoint")
    parser.add_argument("--wait-seconds", type=int, default=0, help="How long to wait for live SHA to catch up")
    parser.add_argument("--poll-seconds", type=int, default=20, help="Polling interval while waiting")
    parser.add_argument("--allow-untracked", action="store_true", help="Allow untracked files")
    parser.add_argument("--skip-live", action="store_true", help="Skip live Railway SHA verification")
    args = parser.parse_args()

    try:
        repo_root = Path(run_git("rev-parse", "--show-toplevel"))
    except RuntimeError as exc:
        fail(f"not inside a git repository: {exc}")

    print(f"[INFO] repo={repo_root}")
    branch = run_git("branch", "--show-current")
    print(f"[INFO] branch={branch}")
    if branch != args.branch:
        fail(f"current branch is '{branch}', expected '{args.branch}'")

    tracked, untracked = parse_status()
    if tracked:
        preview = "\n".join(tracked[:10])
        fail(f"tracked changes present:\n{preview}")
    if untracked and not args.allow_untracked:
        preview = "\n".join(untracked[:10])
        fail(
            "untracked files present. Commit, ignore, or rerun with --allow-untracked if they are intentional:\n"
            + preview
        )
    if untracked:
        print(f"[WARN] allowing {len(untracked)} untracked file(s)")

    remote_ref = f"{args.remote}/{args.branch}"
    run_git("fetch", args.remote, args.branch, "--quiet")
    head_sha = run_git("rev-parse", "HEAD")
    remote_sha = run_git("rev-parse", remote_ref)
    print(f"[INFO] head={head_sha}")
    print(f"[INFO] {remote_ref}={remote_sha}")
    if head_sha != remote_sha:
        fail(f"HEAD does not match {remote_ref}; push before deploying")

    if args.skip_live:
        print("[PASS] local branch is clean and pushed")
        return

    deadline = time.time() + max(0, args.wait_seconds)
    last_live_sha = ""
    while True:
        try:
            live_sha, payload = fetch_live_sha(args.url)
            last_live_sha = live_sha
            print(
                "[INFO] live_sha=%s last_scan=%s"
                % (live_sha or "<missing>", payload.get("last_scan", ""))
            )
            if live_sha == head_sha:
                print("[PASS] live SHA matches local HEAD")
                return
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            print(f"[WARN] live check failed: {exc}")

        if time.time() >= deadline:
            fail(
                "live SHA did not match HEAD%s"
                % (f" (last live SHA: {last_live_sha})" if last_live_sha else "")
            )
        time.sleep(max(1, args.poll_seconds))


if __name__ == "__main__":
    main()
