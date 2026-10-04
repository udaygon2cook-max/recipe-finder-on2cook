#!/usr/bin/env python3
"""
run_full_pipeline.py
=====================
Standalone "run everything from scratch" version of the pipeline.

This does NOT touch all-in-one.py — it's a separate entry point, meant
to be triggered manually (or run locally) when you need every recipe
fully reprocessed, e.g. after changing popup template logic, accessory
mapping rules, or the disclaimer text.

It is IDENTICAL to all-in-one.py's step sequence, with exactly one
difference: step 1's change detection (Smartsheet row-modified vs. last
run) is skipped. Instead, every stem currently in Smartsheet is treated
as "updated", which — because every downstream step's skip-logic keys
off that same set — means:

  - download_zips        re-downloads every ZIP unconditionally
  - extract_zips          wipes and re-extracts every recipe folder
  - accessory_mapping      rebuilds updated_extracted/ + updated_zips/ for
                           every recipe (accessory_mapping itself always
                           iterates every folder in extracted/ regardless;
                           the "updated" set only controls whether stale
                           output is cleared first, so this matters)
  - generate_popups       deletes and regenerates every popup PDF (its
                           skip check is `stem in updated_stems` — with
                           every stem in that set, nothing is skipped)

parse_to_json, sync_description, and flag_orphan_recipes already run
over the full dataset every time in the normal pipeline (they don't take
an updated_stems argument), so they're unaffected by this — same
behavior here as in all-in-one.py.

pipeline_state.json IS still overwritten at the end with this run's
Smartsheet snapshot, so a normal `all-in-one.py` run immediately after
this one goes back to being purely incremental (nothing looks "stale"
right after a full run, which is correct).

Place this file in the same folder as all-in-one.py (Automation/) so its
imports resolve.

Usage:
  python run_full_pipeline.py
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
    banner("RECIPE PIPELINE — FULL FORCED RUN (every recipe reprocessed)")
    print(f"  Started: {start.strftime('%Y-%m-%d %H:%M:%S')}")
    print("  ⚠  Change detection is skipped this run. Every recipe currently")
    print("     in Smartsheet is being treated as updated — every ZIP,")
    print("     extraction, accessory mapping, popup PDF, and disclaimer")
    print("     will be redone, whether or not it actually changed.")

    try:
        step_header(1, "Fetch every recipe stem from Smartsheet (no change detection)")
        current_modified = download_zips.get_sheet_modified_map()
        # Treat literally every stem as stale/updated — this is the one
        # deliberate difference from all-in-one.py.
        stale_stems = set(current_modified.keys())
        print(f"  🔁 Forcing full reprocess of all {len(stale_stems)} recipe(s):")
        for s in sorted(stale_stems):
            print(f"       - {s}")

        step_header(2, "Download ZIPs from Smartsheet (all, unconditionally)")
        downloaded_stems = download_zips.download_all_zips(stale_stems)
        updated_stems = stale_stems | downloaded_stems
        # Smartsheet attachment names can have a trailing space before ".zip"
        # (e.g. "SOOJI KHEER .zip"), so those stems arrive as "SOOJI KHEER ".
        # extract_zips / generate_popup_images work with the stripped on-disk
        # name, so without this they never matched and were skipped as
        # "already extracted" / "PDF exists". Add the stripped form too.
        updated_stems = updated_stems | {s.strip() for s in updated_stems}

        step_header(3, "Extract ZIPs (all, unconditionally)")
        extract_zips.extract_all_zips(updated_stems)

        step_header(4, "Accessory mapping + description fixes (all)")
        accessory_mapping.process_all_accessories(updated_stems)

        step_header(5, "Generate popup PDFs (all, unconditionally)")
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
    banner(f"FULL FORCED RUN COMPLETE  ({elapsed:.0f}s)")


if __name__ == "__main__":
    main()
