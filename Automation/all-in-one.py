#!/usr/bin/env python3
"""
all-in-one.py
=============
Full recipe pipeline orchestrator. Runs all steps sequentially with no
human input.

This file no longer contains any pipeline logic itself — each step lives
in its own standalone, independently-runnable file. This file just imports
each one and calls it in order, passing `updated_stems` along the chain so
later steps know which recipes were actually new/changed this run instead
of reprocessing everything.

Change detection is now a single source of truth, computed FIRST, before
anything is downloaded: Smartsheet's native row-modified timestamp
(bumped on ANY change to that row — a new/replaced ZIP attachment,
Recipe Name, Cuisine, Category, Prerequisite Recipe, anything) is compared
against what pipeline_state.py recorded at the end of the last successful
run. A row is "stale" if it's not in that record at all (new) or its
modifiedAt is newer than what's recorded (changed). Every downstream step
— ZIP download, extraction, accessory mapping, popup generation — forces a
full redo for stale stems, and otherwise skips only if a local copy
already exists.

(Older versions of this pipeline additionally compared each ZIP
attachment's own timestamp against the local file's mtime on disk. That
check was dropped: filesystem mtimes get reset by file copies, container
rebuilds, git checkouts, redeploys, etc., which silently made genuinely
updated recipes look "already current" and skipped them forever.)

Steps:
  1. Detect changed/new recipes  — Smartsheet row-modified vs. last run
  2. download_zips        — force-download ZIPs for stale recipes (or any
                             recipe with no local ZIP at all yet)
  3. extract_zips          — extract ZIPs → extracted/
  4. accessory_mapping      — map accessories + fix descriptions → updated_extracted/ + updated_zips/
  5. generate_popups       — generate popup PDFs directly into final_recipe_pdfs/
                             (skips if PDF already exists and the recipe isn't stale)
  6. parse_to_json         — generate recipes_fix.json from updated_extracted/
  7. sync_description      — patch description first lines from Smartsheet Recipe Names
  8. parse_to_json         — re-run to pick up sync_description changes
  9. flag_orphan_recipes   — hide recipes with no Smartsheet match (must run LAST,
                             after sync_description has had its chance to fix names)
  10. save pipeline state  — record this run's row-modified snapshot for next time

Usage:
  python all-in-one.py
"""

import sys
from datetime import datetime

import download_zips
import extract_zips
import accessory_mapping
import generate_popup_images
import parse_txt_to_json
import sync_description
import flag_orphan_recipes
import pipeline_state


def banner(title: str):
    print(f"\n{'═'*60}")
    print(f"  {title}")
    print(f"{'═'*60}")


def step_header(n, title: str):
    print(f"\n{'─'*60}")
    print(f"  STEP {n}: {title}")
    print(f"{'─'*60}")


def main():
    start = datetime.now()
    banner("RECIPE PIPELINE — Full Run")
    print(f"  Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        step_header(1, "Detect changed/new recipes (Smartsheet row-modified vs. last run)")
        current_modified = download_zips.get_sheet_modified_map()
        previous_state    = pipeline_state.load_state()
        stale_stems       = pipeline_state.get_changed_stems(current_modified, previous_state)
        if stale_stems:
            print(f"  🔁 {len(stale_stems)} recipe(s) new or changed since last run:")
            for s in sorted(stale_stems):
                print(f"       - {s}")
        else:
            print("  ✔  No new/changed recipes since last run.")

        step_header(2, "Download ZIPs from Smartsheet")
        downloaded_stems = download_zips.download_all_zips(stale_stems)

        # Union: stale_stems (Smartsheet says changed) | downloaded_stems
        # (we actually fetched something, e.g. a stem with no local ZIP at
        # all yet even though it wasn't flagged stale). Almost always the
        # same set, but the union covers both edge cases correctly.
        updated_stems = stale_stems | downloaded_stems
        # Smartsheet attachment names can have a trailing space before ".zip"
        # (e.g. "SOOJI KHEER .zip"), so those stems arrive as "SOOJI KHEER ".
        # extract_zips / generate_popup_images work with the stripped on-disk
        # name, so without this they never matched and were skipped as
        # "already extracted" / "PDF exists". Add the stripped form too.
        updated_stems = updated_stems | {s.strip() for s in updated_stems}

        step_header(3, "Extract ZIPs")
        extract_zips.extract_all_zips(updated_stems)

        step_header(4, "Accessory mapping + description fixes")
        accessory_mapping.process_all_accessories(updated_stems)

        step_header(5, "Generate popup PDFs")
        generate_popup_images.generate_all_popups(updated_stems)

        step_header(6, "Parse to JSON")
        parse_txt_to_json.generate_recipes_json()

        step_header(7, "Sync description first lines from Smartsheet Recipe Names")
        sync_description.main()

        step_header(8, "Parse to JSON (re-run to pick up sync_description changes)")
        parse_txt_to_json.generate_recipes_json()

        step_header(9, "Flag orphan recipes (no Smartsheet match) as hidden")
        flag_orphan_recipes.flag_orphan_recipes()

        step_header(10, "Save pipeline state (row-modified snapshot)")
        pipeline_state.save_state(current_modified)

    except Exception as e:
        print(f"\n💥 PIPELINE FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    elapsed = (datetime.now() - start).total_seconds()
    banner(f"PIPELINE COMPLETE  ({elapsed:.0f}s)")


if __name__ == "__main__":
    main()
