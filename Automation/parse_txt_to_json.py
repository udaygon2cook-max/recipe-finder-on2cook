import os
import json
import re
import requests
from pathlib import Path

import download_zips  # for the ZIP stem → Smartsheet row ID join

# ===============================
# CONFIG
# ===============================
EXTRACT_ROOT = "../updated_extracted"
IMAGE_DIR = "../test_images"
POPUP_DIR = "../final_recipe_pdfs"
OUTPUT_JSON = "../recipes_fix.json"
INGREDIENT_IMAGE_DIR = "../ingredient_images"  # one image per recipe, sparse coverage

INGREDIENT_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

SMARTSHEET_TOKEN = os.environ["SMARTSHEET_TOKEN"]
SMARTSHEET_SHEET_ID = os.environ["SMARTSHEET_SHEET_ID"]

SMARTSHEET_HEADERS = {
    "Authorization": f"Bearer {SMARTSHEET_TOKEN}",
    "Content-Type": "application/json"
}

# 🔒 PROTECTED FIELDS — these are NEVER overwritten if already present in existing JSON
PROTECTED_FIELDS = {"hidden", "clubbedWith", "hiddenReason"}

# ===============================
# VALID ACCESSORIES WHITELIST
# ===============================
# Add any valid accessory name here (case-insensitive match)
VALID_ACCESSORIES = {
    "cake kit",
    "frying kit",
    "gravy stirrer",
    "grill mesh",
    "grill pan",
    "idli mold & stirrer",
    "mesh mats",
    "momo kit",
    "mp mats big",
    "mp mats small",
    "noodles stirrer",
    "pan honeycomb (non-stick)",
    "pan non-coated (ss)",
    "pizza kit",
    "pressure cooker",
    "rice stirrer",
    "silicone stirrer",
    "tea stirrer",
    "teflon plate",
}

def is_valid_accessory(line: str) -> bool:
    """
    Returns True only if the line matches a known accessory (whitelist).
    Rejects anything that looks like metadata, a sentence, or a stop phrase.
    """
    cleaned = line.strip().lower()

    # Must match whitelist
    for acc in VALID_ACCESSORIES:
        if acc in cleaned:
            return True

    return False
# ===============================
# HELPERS
# ===============================

def safe_int(val, default=0):
    try:
        return int(val)
    except:
        return default


def clean_line(text):
    return text.replace(":", "").replace("-", "").strip()


def normalize_minutes(text):
    text = text.upper().strip()

    # Match patterns like "2 HOURS", "1.5 HOURS", "3-4 HOURS", "2 HRS"
    hour_range = re.search(r'(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)\s*(?:HOURS?|HRS?)', text)
    hour_single = re.search(r'(\d+(?:\.\d+)?)\s*(?:HOURS?|HRS?)', text)

    if hour_range:
        lo, hi = hour_range.group(1), hour_range.group(2)
        # Format cleanly: drop .0 if whole number
        lo = lo.rstrip('0').rstrip('.') if '.' in lo else lo
        hi = hi.rstrip('0').rstrip('.') if '.' in hi else hi
        return f"{lo}-{hi} hours"
    elif hour_single:
        val = hour_single.group(1)
        val = val.rstrip('0').rstrip('.') if '.' in val else val
        return f"{val} hour{'s' if float(val) != 1 else ''}"

    # Fallback: plain number → minutes
    match = re.search(r'(\d+)', text)
    return f"{match.group(1)} mins" if match else ""


# ===============================
# 🔒 LOAD EXISTING JSON (for protected field preservation)
# ===============================

def load_existing_recipes(path):
    """
    Returns a dict keyed by Recipe Name (uppercase) → existing recipe dict.
    Used to restore protected fields (hidden, clubbedWith) after a regeneration.
    """
    if not os.path.exists(path):
        return {}

    with open(path, "r", encoding="utf-8") as f:
        try:
            existing = json.load(f)
        except json.JSONDecodeError:
            print(f"⚠ Could not parse existing {path}, starting fresh.")
            return {}

    lookup = {}
    for entry in existing:
        name = entry.get("Recipe Name", "").strip().upper()
        if name:
            lookup[name] = entry

    print(f"🔒 Loaded {len(lookup)} existing entries for protected-field preservation")
    return lookup


# ===============================
# INGREDIENT IMAGE LOOKUP
# ===============================

def build_ingredient_image_index():
    """
    Scans INGREDIENT_IMAGE_DIR ONCE and builds {recipe_key_upper: filename}.
    Matching is done by upper-casing both sides, so this is immune to
    case-mismatches between the normalized recipe_key (always uppercase,
    per extract_zips.normalize_name()) and however the image file actually
    got named on disk (e.g. "Chicken Biryani.jpg" still matches
    "CHICKEN BIRYANI"). This is the single source of truth for this lookup
    -- add_disclaimer_onpopup_pdf.py reads the field this writes into
    recipes_fix.json rather than re-deriving its own match.

    Coverage is expected to be sparse -- recipes with no image simply get
    an empty "IngredientImage" field.
    """
    index = {}
    folder = Path(INGREDIENT_IMAGE_DIR)
    if not folder.is_dir():
        return index

    # sorted by plain filename so the pick is identical on Windows and the Linux
    # runner (iterdir() order is filesystem-dependent). The popup generator
    # uses the same order + first-wins rule.
    for f in sorted(folder.iterdir(), key=lambda p: p.name):
        if not f.is_file():
            continue
        if f.suffix.lower() not in INGREDIENT_IMAGE_EXTENSIONS:
            continue
        key = f.stem.upper()
        if key in index:
            print(f"⚠ Duplicate ingredient image for key '{key}': "
                  f"'{index[key]}' and '{f.name}' both match -- keeping the first one seen")
            continue
        index[key] = f.name

    print(f"🥕 Indexed {len(index)} ingredient image(s) from {INGREDIENT_IMAGE_DIR}")
    return index


# ===============================
# SMARTSHEET LOGIC
# ===============================

def load_smartsheet_rows_by_id():
    """
    Fetches the sheet once and returns {row_id_str: row_data}, where row_data
    holds the *exact* Smartsheet column values for that row — Recipe Name
    included.

    This backs the primary (ZIP-based) match in generate_recipes_json(): once
    we know which row a recipe's ZIP belongs to (via
    download_zips.get_zip_stem_to_row_id()), we read everything — including
    the Recipe Name itself — straight from that row, instead of trusting
    whatever name is baked into the ZIP's internal .txt file.
    """
    print("📡 Fetching Smartsheet rows (by row ID)...")

    url = f"https://api.smartsheet.com/2.0/sheets/{SMARTSHEET_SHEET_ID}"
    response = requests.get(url, headers=SMARTSHEET_HEADERS)

    if response.status_code != 200:
        raise Exception(f"Smartsheet API error: {response.status_code} - {response.text}")

    sheet = response.json()
    column_map = {col["id"]: col["title"] for col in sheet["columns"]}

    rows_by_id = {}

    for row in sheet["rows"]:
        row_data = {}

        for cell in row["cells"]:
            col_name = column_map.get(cell["columnId"])
            value = cell.get("value", "")

            if col_name == "Recipe Name":
                row_data["Recipe Name"] = str(value).strip().upper()
            elif col_name in ["Veg/Non Veg", "Cooking Mode", "Cuisine", "Category", "Frozen To Plate", "Healthy"]:
                row_data[col_name] = str(value).strip().upper()
            elif col_name in ["Flavor Profile", "Consistency", "Prerequisite Recipe"]:
                row_data[col_name] = str(value).strip() if value else ""

        row_data["Modified"] = row.get("modifiedAt") or row.get("createdAt") or ""
        rows_by_id[str(row["id"])] = row_data

    print(f"✅ Loaded {len(rows_by_id)} Smartsheet rows (by ID)")
    return rows_by_id


def load_smartsheet_data():
    """
    Fallback lookup: {recipe_name_upper: row_data}, matched by text against
    the Recipe Name column. Only consulted when a recipe's ZIP isn't found
    among Smartsheet's current attachments (e.g. the row's ZIP was replaced,
    or the row was deleted after this recipe was already extracted locally)
    — see the ZIP-based match in generate_recipes_json() for the primary path.
    """
    print("📡 Fetching Smartsheet data...")

    url = f"https://api.smartsheet.com/2.0/sheets/{SMARTSHEET_SHEET_ID}"
    response = requests.get(url, headers=SMARTSHEET_HEADERS)

    if response.status_code != 200:
        raise Exception(f"Smartsheet API error: {response.status_code} - {response.text}")

    sheet = response.json()

    column_map = {col["id"]: col["title"] for col in sheet["columns"]}

    lookup = {}

    for row in sheet["rows"]:
        row_data = {}
        recipe_name = None

        for cell in row["cells"]:
            col_name = column_map.get(cell["columnId"])
            value = cell.get("value", "")

            if col_name == "Recipe Name":
                recipe_name = str(value).strip().upper()
            elif col_name in ["Veg/Non Veg", "Cooking Mode", "Cuisine", "Category", "Frozen To Plate", "Healthy"]:
                row_data[col_name] = str(value).strip().upper()
            elif col_name in ["Flavor Profile", "Consistency", "Prerequisite Recipe"]:
                row_data[col_name] = str(value).strip() if value else ""

        # Smartsheet tracks a last-modified timestamp on every row natively
        # (no extra column needed) — used by the frontend's "New Recipes"
        # banner to find the most recently changed recipes.
        row_data["Modified"] = row.get("modifiedAt") or row.get("createdAt") or ""

        if recipe_name:
            lookup[recipe_name] = row_data

    print(f"✅ Loaded {len(lookup)} recipes from Smartsheet")
    return lookup


# ===============================
# TXT PARSER
# ===============================

def parse_recipe_txt(txt_path):
    with open(txt_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    recipe_name = data.get("name", [""])[0].strip().upper()
    original_name = recipe_name

    ingredients = [ing.get("app_audio", "").strip() for ing in data.get("Ingredients", [])]

    total_on2cook_time_sec = sum(safe_int(step.get("durationInSec", 0)) for step in data.get("Instruction", []))
    total_on2cook_time_min = max(1, round(total_on2cook_time_sec / 60))

    description = data.get("description", "")
    desc_upper = description.upper()

    # -----------------------------
    # Final Output
    # -----------------------------
    total_output = ""
    if "FINAL OUTPUT" in desc_upper:
        try:
            total_output = clean_line(desc_upper.split("FINAL OUTPUT")[1].split("\n")[0])
        except:
            pass
    elif "OUTPUT" in desc_upper:
        try:
            total_output = clean_line(desc_upper.split("OUTPUT")[1].split("\n")[0])
        except:
            pass

    # -----------------------------
    # Accessories
    # -----------------------------
    # -----------------------------
# Accessories
# -----------------------------
    accessories = ""
    if "ACCESSORIES" in desc_upper:
        try:
            part = description.split("ACCESSORIES", 1)[1]
            acc_list = []

            for line in part.splitlines():
                line = line.strip()
                if not line:
                    continue

                # Stop at any line that is clearly a new section header
                upper = line.upper()
                section_headers = [
                    "FINAL OUTPUT", "OUTPUT", "NORMAL COOKING TIME", "NORMAL TIME",
                    "OTHER ESSENTIALS", "INGREDIENTS", "NOTE", "SPECIAL INSTRUCTION", "KEY TIPS"
                ]
                if any(upper.startswith(kw) for kw in section_headers):
                    break

                # ✅ Whitelist check — only keep known valid accessories
                if is_valid_accessory(line):
                    acc_list.append(line.title())
                else:
                    # Anything unrecognised is treated as a stop signal
                    # (could log here for discovery: print(f"⛔ Skipped accessory line: {line}"))
                    break

            accessories = ", ".join(acc_list)

        except:
            pass

    # -----------------------------
    # Normal Cooking Time
    # -----------------------------
    normal_cooking_time = ""
    if "NORMAL COOKING TIME" in desc_upper:
        try:
            raw = clean_line(desc_upper.split("NORMAL COOKING TIME")[1].split("\n")[0])
            normal_cooking_time = normalize_minutes(raw)
        except:
            pass
    elif "NORMAL TIME" in desc_upper:
        try:
            raw = clean_line(desc_upper.split("NORMAL TIME")[1].split("\n")[0])
            normal_cooking_time = normalize_minutes(raw)
        except:
            pass

    recipe = {
        "Recipe Name": recipe_name,
        "Veg/Non Veg": "VEG",
        "Cooking Mode": "AUTO",
        "Cuisine": "GLOBAL CUISINE",
        "Category": "MAIN COURSE",
        "Frozen To Plate": "",
        "Healthy": "",
        "Flavor Profile": "",
        "Consistency": "",
        "Prerequisite Recipe": "",
        "Cooking Time": total_on2cook_time_min,
        "Image": "",
        "PopupImage": "",
        "Ingredients": ingredients,
        "Accessories": accessories,
        "Total Output": total_output,
        "On2Cook Cooking Time": f"{total_on2cook_time_min}",
        "Normal Cooking Time": normal_cooking_time,
        "description": description,  # keep temporarily for fallback matching
        "_original_name": original_name
    }

    return recipe


# ===============================
# DEDUPLICATION
# ===============================
#
# Two different ZIP-stem folders in updated_extracted/ can both resolve to
# the exact same Smartsheet Recipe Name (e.g. a ZIP got re-uploaded under a
# new attachment/file name for the same row, or two rows share a typed-in
# name). Nothing upstream of this point checks for that, so without this
# step the same recipe can be written into recipes_fix.json twice.
#
# Rule is intentionally narrow and exact: two entries are "the same recipe"
# ONLY if their Recipe Name is identical after trimming + uppercasing.
# No fuzzy/partial matching, ever — that's how you accidentally collapse
# two genuinely different recipes into one and silently lose one of them.
# Every entry that goes in also comes back out, one way or another; nothing
# is dropped without being named in the printed report below.

def _dedupe_score(recipe):
    """
    Higher score wins when two entries share a Recipe Name. Two criteria,
    checked in order:
      1. Does the Image filename exactly match the Recipe Name? (the
         "correct" image for a recipe is almost always named after it —
         a mismatch usually means a stray/mistaken upload)
      2. Most recently Modified (Smartsheet's own row timestamp) wins ties.
    """
    name = (recipe.get("Recipe Name", "") or "").strip().upper()
    image_stem = os.path.splitext(os.path.basename(recipe.get("Image", "") or ""))[0].strip().upper()
    name_matches_image = 1 if image_stem == name else 0
    modified = recipe.get("Modified", "") or ""
    return (name_matches_image, modified)


def dedupe_recipes(recipes):
    """
    Collapses recipes sharing an exact (trimmed, uppercased) Recipe Name
    down to a single entry, keeping the highest-scoring one per
    _dedupe_score. Prints exactly what was kept vs. dropped so a run is
    always auditable from the console log — never a silent drop.
    """
    groups = {}
    order = []  # preserve first-seen order of each distinct name
    for recipe in recipes:
        key = (recipe.get("Recipe Name", "") or "").strip().upper()
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(recipe)

    deduped = []
    dropped_total = 0
    for key in order:
        items = groups[key]
        if len(items) == 1:
            deduped.append(items[0])
            continue

        winner = max(items, key=_dedupe_score)
        deduped.append(winner)
        dropped_total += len(items) - 1

        print(f"\n⚠ DUPLICATE Recipe Name found: '{key}' ({len(items)} entries) — keeping 1, dropping {len(items)-1}")
        for item in items:
            tag = "✅ KEPT" if item is winner else "❌ dropped"
            print(f"    {tag} — Image: {item.get('Image','')} | Modified: {item.get('Modified','')}")

    if dropped_total:
        print(f"\n🧹 Deduplication: {len(recipes)} parsed → {len(deduped)} written "
              f"({dropped_total} duplicate row(s) removed, exact-name matches only)")
    else:
        print(f"\n🧹 Deduplication: no duplicate Recipe Names found ({len(recipes)} recipes, all unique)")

    return deduped


# ===============================
# MAIN GENERATOR
# ===============================

def generate_recipes_json():
    recipes = []
    Path(POPUP_DIR).mkdir(exist_ok=True)

    # 🔒 Load existing JSON first so we can restore protected fields later
    existing_map = load_existing_recipes(OUTPUT_JSON)

    # Ingredient images: one case-insensitive scan of the whole folder,
    # instead of a per-recipe filesystem check (see build_ingredient_image_index).
    ingredient_image_index = build_ingredient_image_index()

    # Primary match path: this recipe's own ZIP stem → Smartsheet row ID →
    # that row's own cells. Reliable because it's the same ZIP file identity
    # already used to download and extract the recipe — it can't drift the
    # way text-matching on the internal name/description can.
    zip_stem_to_row_id = download_zips.get_zip_stem_to_row_id()
    rows_by_id = load_smartsheet_rows_by_id()

    # Fallback match path (name/description text matching) — only used when
    # a recipe's ZIP isn't found among Smartsheet's current attachments.
    smartsheet_map = load_smartsheet_data()

    for recipe_key in os.listdir(EXTRACT_ROOT):
        recipe_dir = os.path.join(EXTRACT_ROOT, recipe_key)
        if not os.path.isdir(recipe_dir):
            continue

        txt_file = next((f for f in os.listdir(recipe_dir) if f.lower().endswith(".txt")), None)
        if not txt_file:
            print(f"⚠ No TXT file in {recipe_key}")
            continue

        recipe = parse_recipe_txt(os.path.join(recipe_dir, txt_file))

        name = recipe["Recipe Name"]
        original_name = recipe["_original_name"]

        matched_name = None
        ss = None

        # 1️⃣ ZIP-based match (preferred) — recipe_key IS the ZIP's own stem,
        # so look its Smartsheet row up directly and read that row's cells,
        # including the Recipe Name itself.
        row_id = zip_stem_to_row_id.get(recipe_key)
        if row_id and row_id in rows_by_id:
            ss = rows_by_id[row_id]
            matched_name = ss.get("Recipe Name") or name

            # Explicit check: does the ZIP's own internal name agree with
            # what's typed in Smartsheet's Recipe Name column for this row?
            # Smartsheet always wins either way — this is just visibility so
            # you can spot rows where the two have drifted apart.
            if name and matched_name and name != matched_name:
                print(f"   ℹ️ Recipe Name check: ZIP's internal name ('{name}') "
                      f"≠ Smartsheet Recipe Name ('{matched_name}') for row {row_id} "
                      f"— using Smartsheet's value")

            recipe["Recipe Name"] = matched_name
            print(f"✅ Matched via ZIP → row ID: {recipe_key} → {matched_name} (from Smartsheet Recipe Name column)")

        # 2️⃣ Fallback: old text-matching behavior, only if the ZIP-based
        # match above didn't find anything.
        if not ss:
            if name in smartsheet_map:
                matched_name = name
                ss = smartsheet_map[matched_name]
                print(f"↩️ Fallback-matched using JSON name: {name}")
            else:
                description = recipe.get("description", "")
                if description:
                    first_line = description.strip().split("\n")[0].strip().upper()
                    metadata_keywords = ["NORMAL COOKING TIME", "NORMAL TIME", "FINAL OUTPUT", "OUTPUT", "ACCESSORIES"]

                    if first_line and not any(first_line.startswith(k) for k in metadata_keywords):
                        if first_line in smartsheet_map:
                            matched_name = first_line
                            recipe["Recipe Name"] = first_line
                            ss = smartsheet_map[matched_name]
                            print(f"↩️ Fallback-matched using description name: {first_line}")

        # Apply Smartsheet overrides if matched (via either path above)
        if ss:
            for field in ["Veg/Non Veg", "Cooking Mode", "Cuisine", "Category", "Frozen To Plate", "Healthy", "Flavor Profile", "Consistency", "Prerequisite Recipe"]:
                recipe[field] = ss.get(field, recipe[field])
            # Modified isn't a fallback-able field like the others (recipe
            # dict has no prior value for it) — just take it straight from
            # Smartsheet's row-level timestamp when there's a match.
            recipe["Modified"] = ss.get("Modified", "")
        else:
            print(f"⚠ No Smartsheet match for {name}")

        popup_name = original_name.replace("/", "_").replace("\\", "_")

        recipe["Image"] = f"{IMAGE_DIR}/{recipe_key}.jpg"
        recipe["PopupImage"] = f"{POPUP_DIR}/{popup_name}.pdf"

        ingredient_image_filename = ingredient_image_index.get(recipe_key.upper(), "")
        recipe["IngredientImage"] = (
            f"{INGREDIENT_IMAGE_DIR}/{ingredient_image_filename}" if ingredient_image_filename else ""
        )

        recipe.pop("_original_name", None)
        recipe.pop("description", None)

        # 🔒 RESTORE PROTECTED FIELDS from the existing JSON entry (if any)
        # We check both the current Recipe Name and original_name since it may have
        # been remapped via description matching above.
        existing_entry = existing_map.get(recipe["Recipe Name"].upper()) or existing_map.get(original_name.upper())
        if existing_entry:
            for field in PROTECTED_FIELDS:
                if field not in existing_entry:
                    continue
                # If this recipe was auto-hidden last run (hiddenReason == orphan flag)
                # but NOW has a real Smartsheet match, don't carry the stale hidden
                # flag forward — it's no longer an orphan, so let it show again.
                # clubbedWith and genuine manual hides are always preserved.
                if (
                    field in ("hidden", "hiddenReason")
                    and matched_name
                    and existing_entry.get("hiddenReason") == "not needed anymore"
                ):
                    continue
                recipe[field] = existing_entry[field]
                print(f"🔒 Preserved '{field}' for {recipe['Recipe Name']}: {existing_entry[field]}")

        # NOTE: orphan recipes (no Smartsheet match) are NOT auto-hidden here.
        # sync_description.py hasn't run yet at this point in the pipeline, so a
        # recipe whose description name is still stale would wrongly look like
        # an orphan. Auto-hiding is handled separately, as the LAST pipeline
        # step, by flag_orphan_recipes.py — after sync_description has had a
        # chance to fix names and this file has re-run to pick that up.

        recipes.append(recipe)
        print(f"🍽 Parsed: {recipe['Recipe Name']}")

    recipes = dedupe_recipes(recipes)

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(recipes, f, indent=2, ensure_ascii=False)

    matched_ingredient_images = sum(1 for r in recipes if r.get("IngredientImage"))
    print(f"\n✅ {len(recipes)} recipes written to {OUTPUT_JSON}")
    print(f"🥕 {matched_ingredient_images}/{len(recipes)} recipes matched with an ingredient image")
    print("📡 Smartsheet is the master for (matched via ZIP → row ID, falling back to name/description text-match):")
    print("   - Recipe Name")
    print("   - Veg/Non Veg")
    print("   - Cooking Mode")
    print("   - Cuisine")
    print("   - Category")
    print("   - Frozen To Plate")
    print("   - Healthy")
    print("   - Flavor Profile")
    print("   - Consistency")
    print("   - Prerequisite Recipe")
    print("🔒 Protected fields (never overwritten):")
    print("   - hidden")
    print("   - hiddenReason")
    print("   - clubbedWith")


if __name__ == "__main__":
    generate_recipes_json()
