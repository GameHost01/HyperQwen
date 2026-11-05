#!/usr/bin/env python3
"""Swap chat_template.jinja to match the desired REASONING_EFFORT level.

On first run, preserves the original template as chat_template.master.jinja.
Thereafter, generates the active chat_template.jinja from the master by
applying a minimal, reversible edit:

  off    -> enable_thinking defaults to False (remove 'is undefined or')
  low    -> default reasoning_effort becomes 'low'
  medium -> default reasoning_effort becomes 'medium'
  xhigh  -> restore the master unchanged (the model's native default)

Usage:
    python prepare/apply_reasoning_effort.py --effort off [model_dir ...]

    # default: every models/*/chat_template.jinja under this repo
    python prepare/apply_reasoning_effort.py --effort off

Idempotent: running the same effort twice is a no-op.
"""
import argparse
import os
import sys
import shutil

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODELS_DIR = os.path.join(REPO, "models")

# The two anchor strings in the template (must match exactly)
THINK_LINE = "{%- if enable_thinking is undefined or enable_thinking is true %}"
THINK_LINE_OFF = "{%- if enable_thinking is true %}"

EFFORT_LINE = "reasoning_effort is defined and reasoning_effort is not none else 'xhigh'"
# We replace the 'xhigh' default with the desired effort
EFFORT_DEFAULT_XHIGH = "else 'xhigh'"


def find_templates():
    """Yield every models/*/chat_template.jinja under the repo."""
    if not os.path.isdir(MODELS_DIR):
        return
    for d in sorted(os.listdir(MODELS_DIR)):
        if d.startswith(".") or d.startswith("tmp-"):
            continue
        p = os.path.join(MODELS_DIR, d, "chat_template.jinja")
        if os.path.isfile(p):
            yield p


def ensure_master(template_path):
    """Save the original as .master.jinja if it doesn't exist yet.
    
    The 'original' is detected by the presence of the THINK_LINE string
    (the unmodified upstream template). If the file is already a variant
    (e.g. 'off' variant with THINK_LINE_OFF), we look for the master first.
    """
    master_path = template_path + ".master"
    if os.path.isfile(master_path):
        return master_path  # already have a master

    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()

    # If the current file looks like the unmodified original, it IS the master
    if THINK_LINE in content and EFFORT_DEFAULT_XHIGH in content:
        shutil.copy2(template_path, master_path)
        return master_path

    # If we have an .orig from the prepare step (translate_chat_template.py), use that
    # Otherwise, reconstruct: the master is the template with THINK_LINE + xhigh default
    # For now, just promote the current file (best effort)
    shutil.copy2(template_path, master_path)
    return master_path


def apply_effort(master_content: str, effort: str) -> str:
    """Generate the template content for the given effort level."""
    if effort == "off":
        # Disable thinking by default: remove 'is undefined or' so that
        # an absent enable_thinking param means thinking is OFF.
        # Clients can still opt-in by explicitly sending enable_thinking=true.
        return master_content.replace(THINK_LINE, THINK_LINE_OFF, 1)

    elif effort in ("low", "medium"):
        # Keep thinking enabled by default, but change the effort level.
        # The template's if/elif only has instructions for 'xhigh' and 'low';
        # 'medium' falls through with no instruction (same as current behaviour).
        return master_content.replace(EFFORT_DEFAULT_XHIGH, f"else '{effort}'", 1)

    else:  # xhigh or unknown -> master unchanged
        return master_content


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--effort", required=True, choices=["off", "low", "medium", "xhigh"],
                    help="Desired reasoning effort level")
    ap.add_argument("paths", nargs="*",
                    help="Specific model dirs or .jinja files (default: all under models/)")
    args = ap.parse_args()

    # Collect template paths
    if args.paths:
        targets = []
        for p in args.paths:
            if os.path.isdir(p):
                t = os.path.join(p, "chat_template.jinja")
                if os.path.isfile(t):
                    targets.append(t)
            elif os.path.isfile(p):
                targets.append(p)
            else:
                print(f"  skip: {p} (not found)", file=sys.stderr)
    else:
        targets = list(find_templates())

    if not targets:
        print("No chat_template.jinja files found.", file=sys.stderr)
        return 1

    changed = 0
    for tpath in targets:
        master_path = ensure_master(tpath)
        with open(master_path, "r", encoding="utf-8") as f:
            master_content = f.read()

        new_content = apply_effort(master_content, args.effort)

        # Read current to check if a write is needed
        with open(tpath, "r", encoding="utf-8") as f:
            current = f.read()

        if new_content != current:
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(new_content)
            changed += 1
            print(f"  updated: {os.path.relpath(tpath, REPO)}")
        else:
            print(f"  ok (already {args.effort}): {os.path.relpath(tpath, REPO)}")

    print(f"apply_reasoning_effort: {args.effort} — {changed} file(s) updated, "
          f"{len(targets) - changed} already correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
