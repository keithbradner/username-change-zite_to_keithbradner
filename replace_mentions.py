#!/usr/bin/env python3
"""
replace_mentions.py

Bulk-replace GitHub @mentions in issue bodies and issue comments across one or more repositories.

Features
- Dry-run by default; use --apply to actually edit on GitHub.
- Precise mention matching (only replaces exact "@oldusername" tokens, case-insensitive).
- Skips fenced code blocks (``` ... ```) by default to avoid altering code snippets.
- Rate-limit aware and optional sleep between edit calls.
- JSONL log of every would-be/applied change for auditing and rollback.
- Optional: also edit issue titles; optional: include plain (non-@) occurrences.

Requirements
- Python 3.8+
- PyGithub (`pip install PyGithub`)
- A GitHub Personal Access Token (PAT) with appropriate scopes (e.g., `repo` for private repos).

Usage examples
--------------
Dry-run on a single repo:
    python replace_mentions.py --repo owner/repo --old oldname --new newname

Actually apply changes:
    python replace_mentions.py --repo owner/repo --old oldname --new newname --apply

Multiple repos:
    python replace_mentions.py --repo owner/repo1 --repo owner/repo2 --old oldname --new newname --apply

Read repos from a file (one per line, comments allowed with #):
    python replace_mentions.py --repos-file repos.txt --old oldname --new newname --apply

Also update issue titles and include plain "oldname" (without @):
    python replace_mentions.py --repo owner/repo --old oldname --new newname --include-titles --also-plain --apply
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Tuple, Optional

try:
    from github import Github
    from github.GithubException import GithubException, RateLimitExceededException, UnknownObjectException, BadCredentialsException
except ImportError as e:
    print("PyGithub is required. Install with: pip install PyGithub", file=sys.stderr)
    raise

def ts_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def compile_mention_pattern(old_username: str) -> re.Pattern:
    """
    Compile a regex that matches EXACT @old_username mentions, case-insensitive.
    - Username chars on GitHub are alphanumeric or hyphen. We enforce boundaries so we don't
      match inside longer usernames or emails.
    - Negative lookbehind ensures char before '@' is not a username char.
    - Negative lookahead ensures char after the username is not a username char.
    """
    old = old_username.lstrip('@')
    username_charset = r"[A-Za-z0-9-]"
    pattern = rf"(?<!{username_charset})@{re.escape(old)}(?!{username_charset})"
    return re.compile(pattern, re.IGNORECASE)

def compile_plain_pattern(old_username: str) -> re.Pattern:
    """
    Optional plain (non-@) occurrence. Uses word boundaries around the username so it doesn't
    replace inside longer words. This is best-effort and may be noisy; use with care.
    """
    old = old_username.lstrip('@')
    # \b doesn't treat '-' as a word char, so ensure we bound against username charset explicitly
    username_charset = r"[A-Za-z0-9-]"
    pattern = rf"(?<!{username_charset}){re.escape(old)}(?!{username_charset})"
    return re.compile(pattern, re.IGNORECASE)

def replace_outside_fenced_code(text: Optional[str], patterns, replacements, skip_code_blocks: bool) -> Tuple[str, int]:
    """
    Replace occurrences using (pattern, replacement) pairs, skipping fenced code blocks if requested.
    Returns (new_text, total_replacements).
    """
    if not text:
        return text or "", 0

    if not skip_code_blocks:
        new_text = text
        total = 0
        for pat, rep in zip(patterns, replacements):
            count = len(pat.findall(new_text))
            if count:
                new_text = pat.sub(rep, new_text)
                total += count
        return new_text, total

    total = 0
    out = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        new_line = line
        for pat, rep in zip(patterns, replacements):
            count = len(pat.findall(new_line))
            if count:
                new_line = pat.sub(rep, new_line)
                total += count
        out.append(new_line)
    return "".join(out), total

def parse_since(since_str: Optional[str]) -> Optional[dt.datetime]:
    if not since_str:
        return None
    # Accept YYYY-MM-DD or ISO string; make timezone-aware UTC if naive
    try:
        d = dt.datetime.fromisoformat(since_str)
    except ValueError:
        # try just date
        d = dt.datetime.strptime(since_str, "%Y-%m-%d")
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return d

def ensure_token(token: Optional[str]) -> str:
    tok = token or os.getenv("GITHUB_TOKEN")
    if not tok:
        print("Error: A GitHub token is required. Pass --token or set GITHUB_TOKEN.", file=sys.stderr)
        sys.exit(2)
    return tok

def load_repos(args) -> list:
    repos = []
    if args.repo:
        repos.extend(args.repo)
    if args.repos_file:
        with open(args.repos_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                repos.append(line)
    if not repos:
        print("Error: at least one --repo or a --repos-file is required.", file=sys.stderr)
        sys.exit(2)
    # Deduplicate while preserving order
    seen = set()
    uniq = []
    for r in repos:
        if r not in seen:
            uniq.append(r)
            seen.add(r)
    return uniq

def sleep_if_needed(g: Github, threshold_remaining: int = 5):
    try:
        rl = g.get_rate_limit().core
        if rl.remaining <= threshold_remaining:
            reset_ts = rl.reset.replace(tzinfo=dt.timezone.utc).timestamp()
            now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
            wait_s = max(0, int(reset_ts - now_ts) + 2)
            print(f"[{ts_utc()}] Rate limit low ({rl.remaining} remaining). Sleeping {wait_s}s until reset at {rl.reset}...", flush=True)
            time.sleep(wait_s)
    except Exception:
        # If anything goes wrong querying rate limits, just proceed.
        pass

def write_log(log_fp, record: dict):
    log_fp.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    log_fp.flush()

def prompt_approval(change_info: dict) -> bool:
    """
    Display change details and prompt user for approval.
    Returns True if approved (user pressed Enter), False otherwise.
    """
    print("\n" + "="*80)
    print(f"PROPOSED CHANGE:")
    print(f"  Repo: {change_info.get('repo', 'N/A')}")
    print(f"  Location: {change_info.get('where', 'N/A')}")
    print(f"  Issue: #{change_info.get('issue_number', 'N/A')} - {change_info.get('issue_url', 'N/A')}")
    if change_info.get('comment_id'):
        print(f"  Comment ID: {change_info.get('comment_id')}")
    print(f"  Author: {change_info.get('author', 'N/A')}")
    print(f"  Replacements: {change_info.get('count_replacements', 0)}")
    print(f"\n  BEFORE:\n{change_info.get('before', '')[:200]}{'...' if len(change_info.get('before', '')) > 200 else ''}")
    print(f"\n  AFTER:\n{change_info.get('after', '')[:200]}{'...' if len(change_info.get('after', '')) > 200 else ''}")
    print("="*80)
    
    response = input("Press ENTER to approve, or type anything to skip: ").strip()
    return response == ""

def process_repo(g: Github, repo_full: str, args, log_fp) -> dict:
    stats = {
        "repo": repo_full,
        "issues_scanned": 0,
        "comments_scanned": 0,
        "issue_bodies_changed": 0,
        "issue_titles_changed": 0,
        "comments_changed": 0,
        "edits_applied": 0,
        "edits_would_apply": 0
    }

    try:
        repo = g.get_repo(repo_full)
    except UnknownObjectException:
        print(f"[{ts_utc()}] Repo not found or no access: {repo_full}", file=sys.stderr)
        return stats

    print(f"[{ts_utc()}] Scanning repo: {repo_full}")
    since_dt = parse_since(args.since)
    try:
        issues = repo.get_issues(state="all", since=since_dt) if since_dt else repo.get_issues(state="all")
    except TypeError:
        # Older PyGithub may not support 'since' for this call; fallback to all.
        issues = repo.get_issues(state="all")

    # Compile patterns
    mention_pat = compile_mention_pattern(args.old)
    patterns = [mention_pat]
    replacements = [f"@{args.new.lstrip('@')}"]
    if args.also_plain:
        patterns.append(compile_plain_pattern(args.old))
        replacements.append(args.new.lstrip('@'))

    max_edits = args.max_edits if args.max_edits and args.max_edits > 0 else None

    for issue in issues:
        stats["issues_scanned"] += 1

        # Issue body
        original_body = issue.body or ""
        new_body, n1 = replace_outside_fenced_code(
            original_body, patterns, replacements, args.skip_code_blocks
        )

        if n1 > 0:
            change_info = {
                "ts": ts_utc(),
                "repo": repo_full,
                "where": "issue_body",
                "issue_number": issue.number,
                "issue_url": issue.html_url,
                "author": getattr(issue.user, "login", None),
                "count_replacements": n1,
                "apply": bool(args.apply),
                "skip_code_blocks": args.skip_code_blocks,
                "before": original_body,
                "after": new_body
            }
            
            action = "would_edit"
            approved = True
            
            # Interactive approval if applying changes
            if args.apply and (max_edits is None or stats["edits_applied"] < max_edits):
                if args.auto_approve:
                    print(f"[AUTO-APPROVE] Issue #{issue.number} body - {n1} replacement(s)")
                else:
                    approved = prompt_approval(change_info)
                if approved:
                    try:
                        sleep_if_needed(g)
                        issue.edit(body=new_body)
                        action = "edited"
                        stats["edits_applied"] += 1
                        print(f"✓ Applied change to issue #{issue.number} body")
                    except (GithubException, RateLimitExceededException) as e:
                        action = f"error_editing_issue_body:{getattr(e, 'status', 'unknown')}"
                        print(f"✗ Error applying change: {e}")
                else:
                    action = "skipped_by_user"
                    print(f"⊘ Skipped change to issue #{issue.number} body")
            
            stats["issue_bodies_changed"] += 1
            change_info["action"] = action
            write_log(log_fp, change_info)
            
            if args.apply is False:
                stats["edits_would_apply"] += 1

            time.sleep(args.sleep)

        # Issue title (optional)
        if args.include_titles:
            original_title = issue.title or ""
            new_title, ntitle = replace_outside_fenced_code(
                original_title, patterns, replacements, False  # titles don't have code blocks
            )
            if ntitle > 0:
                change_info = {
                    "ts": ts_utc(),
                    "repo": repo_full,
                    "where": "issue_title",
                    "issue_number": issue.number,
                    "issue_url": issue.html_url,
                    "author": getattr(issue.user, "login", None),
                    "count_replacements": ntitle,
                    "apply": bool(args.apply),
                    "before": original_title,
                    "after": new_title
                }
                
                action = "would_edit"
                approved = True
                
                # Interactive approval if applying changes
                if args.apply and (max_edits is None or stats["edits_applied"] < max_edits):
                    if args.auto_approve:
                        print(f"[AUTO-APPROVE] Issue #{issue.number} title - {ntitle} replacement(s)")
                    else:
                        approved = prompt_approval(change_info)
                    if approved:
                        try:
                            sleep_if_needed(g)
                            issue.edit(title=new_title)
                            action = "edited"
                            stats["edits_applied"] += 1
                            print(f"✓ Applied change to issue #{issue.number} title")
                        except (GithubException, RateLimitExceededException) as e:
                            action = f"error_editing_issue_title:{getattr(e, 'status', 'unknown')}"
                            print(f"✗ Error applying change: {e}")
                    else:
                        action = "skipped_by_user"
                        print(f"⊘ Skipped change to issue #{issue.number} title")
                
                stats["issue_titles_changed"] += 1
                change_info["action"] = action
                write_log(log_fp, change_info)
                
                if args.apply is False:
                    stats["edits_would_apply"] += 1
                time.sleep(args.sleep)

        # Issue comments
        try:
            comments = issue.get_comments()
        except GithubException as e:
            print(f"[{ts_utc()}] Error fetching comments for #{issue.number} in {repo_full}: {e}", file=sys.stderr)
            comments = []

        for c in comments:
            stats["comments_scanned"] += 1
            original = c.body or ""
            new_text, n2 = replace_outside_fenced_code(original, patterns, replacements, args.skip_code_blocks)
            if n2 > 0:
                change_info = {
                    "ts": ts_utc(),
                    "repo": repo_full,
                    "where": "issue_comment",
                    "issue_number": issue.number,
                    "issue_url": issue.html_url,
                    "comment_id": c.id,
                    "comment_url": getattr(c, "html_url", None),
                    "author": getattr(getattr(c, "user", None), "login", None),
                    "count_replacements": n2,
                    "apply": bool(args.apply),
                    "skip_code_blocks": args.skip_code_blocks,
                    "before": original,
                    "after": new_text
                }
                
                action = "would_edit"
                approved = True
                
                # Interactive approval if applying changes
                if args.apply and (max_edits is None or stats["edits_applied"] < max_edits):
                    if args.auto_approve:
                        print(f"[AUTO-APPROVE] Comment {c.id} on issue #{issue.number} - {n2} replacement(s)")
                    else:
                        approved = prompt_approval(change_info)
                    if approved:
                        try:
                            sleep_if_needed(g)
                            c.edit(body=new_text)
                            action = "edited"
                            stats["edits_applied"] += 1
                            print(f"✓ Applied change to comment {c.id} on issue #{issue.number}")
                        except (GithubException, RateLimitExceededException) as e:
                            action = f"error_editing_comment:{getattr(e, 'status', 'unknown')}"
                            print(f"✗ Error applying change: {e}")
                    else:
                        action = "skipped_by_user"
                        print(f"⊘ Skipped change to comment {c.id} on issue #{issue.number}")
                
                stats["comments_changed"] += 1
                change_info["action"] = action
                write_log(log_fp, change_info)
                
                if args.apply is False:
                    stats["edits_would_apply"] += 1
                time.sleep(args.sleep)

        # Respect a global max edits to be extra safe
        if args.apply and (max_edits is not None and stats["edits_applied"] >= max_edits):
            print(f"[{ts_utc()}] Reached --max-edits={max_edits} in {repo_full}. Stopping further edits.", flush=True)
            break

    return stats

def main():
    parser = argparse.ArgumentParser(description="Bulk-replace GitHub @mentions in issues and comments.")
    parser.add_argument("--token", help="GitHub token (or set env GITHUB_TOKEN)")
    parser.add_argument("--repo", action="append", help="Full repo name (owner/repo). Repeatable.")
    parser.add_argument("--repos-file", help="Path to file with owner/repo per line (comments with # allowed).")
    parser.add_argument("--old", required=True, help="Old username to replace (with or without leading @).")
    parser.add_argument("--new", required=True, help="New username (without or with leading @; will insert @ for mentions).")
    parser.add_argument("--apply", action="store_true", help="Actually apply edits. Default is dry-run.")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-approve all changes without prompting (use with --apply).")
    parser.add_argument("--include-titles", action="store_true", help="Also replace in issue titles (off by default).")
    parser.add_argument("--also-plain", action="store_true", help="Also replace plain occurrences without '@' (use with care).")
    parser.add_argument("--since", help="Only consider issues updated on/after this date (YYYY-MM-DD or ISO).")
    parser.add_argument("--sleep", type=float, default=0.3, help="Seconds to sleep between edit calls (default: 0.3).")
    parser.add_argument("--max-edits", type=int, help="Stop after applying this many edits (safety cap).")
    parser.add_argument("--no-skip-code-blocks", dest="skip_code_blocks", action="store_false",
                        help="Do NOT skip fenced code blocks (``` ... ```).")
    parser.set_defaults(skip_code_blocks=True)

    # Logging
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    parser.add_argument("--log", default=f"rename_mentions_{ts}.jsonl", help="Path to JSONL log file (default includes timestamp).")

    args = parser.parse_args()

    token = ensure_token(args.token)
    repos = load_repos(args)

    try:
        gh = Github(login_or_token=token, per_page=100)
        # Sanity check token early
        _ = gh.get_user().login
    except BadCredentialsException:
        print("Error: bad credentials. Verify your token (scopes and value).", file=sys.stderr)
        sys.exit(1)

    print(f"[{ts_utc()}] Starting. Dry-run={not args.apply}. Repos={len(repos)}. Log={args.log}")
    overall = {
        "issues_scanned": 0,
        "comments_scanned": 0,
        "issue_bodies_changed": 0,
        "issue_titles_changed": 0,
        "comments_changed": 0,
        "edits_applied": 0,
        "edits_would_apply": 0
    }

    with open(args.log, "a", encoding="utf-8") as log_fp:
        for repo_full in repos:
            stats = process_repo(gh, repo_full, args, log_fp)
            for k in overall.keys():
                overall[k] += stats.get(k, 0)

    print(f"[{ts_utc()}] Done.")
    print("Summary:")
    for k, v in overall.items():
        print(f"  {k}: {v}")

if __name__ == "__main__":
    main()
