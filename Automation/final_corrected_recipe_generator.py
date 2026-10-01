import os
import json
import re
import zipfile
import tempfile
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, black, white
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from math import sin, cos, radians
from svglib.svglib import svg2rlg
from reportlab.graphics import renderPDF

# New imports for Dropbox + QR
import qrcode
from PIL import Image
import dropbox
from dropbox.exceptions import ApiError, AuthError

# =========================
# Configuration for QR/Dropbox
# =========================
DB_DEFAULT_FOLDER = '/Recipe Booklet'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOGO_PATH = os.path.join(SCRIPT_DIR, 'image.jfif')
LOGO_RATIO = 4

# =========================
# Configuration for ingredient image + disclaimer block
# =========================
# ingredient_images/ and recipes_fix.json live one level up from this
# script (Automation/), same layout used by build_recipe_popups.py.
INGREDIENT_IMAGE_DIR = os.path.abspath(os.path.join(SCRIPT_DIR, '..', 'ingredient_images'))
RECIPES_FIX_JSON = os.path.abspath(os.path.join(SCRIPT_DIR, '..', 'recipes_fix.json'))

# =========================
# Configuration for header icon badges (new design)
# =========================
# Drop scale.svg / clock.svg / bowl.svg / ingredients.svg here (white-on-transparent
# artwork works best, they're drawn inside a solid colored circle). Missing files
# fall back to a simple hand-drawn glyph so the script still runs without assets.
ICONS_DIR = os.path.join(SCRIPT_DIR, 'Icons')
MARBLE_BG_PATH = os.path.join(SCRIPT_DIR, 'marble_background.png')  # exact supplied marble texture


def safe_int(value, default=0):
    """Safely convert a value to integer, returning default for invalid inputs."""
    try:
        if value is None:
            return default
        if isinstance(value, int):
            return value
        value_str = str(value).strip()
        if value_str == '':
            return default
        return int(value_str)
    except (ValueError, TypeError):
        return default

def sanitize_filename(name):
    """Sanitize a string to be a safe filename (keeps alnum, space, dash, underscore, dot)."""
    if not name:
        return "recipe"
    sanitized = re.sub(r'[^A-Za-z0-9._ \-]+', '_', name)
    sanitized = sanitized.strip().strip('.')
    return sanitized or "recipe"

def clean_time_and_units_text(text):
    """Clean time formatting and unit abbreviations according to requirements"""
    import re
    
    # Replace 'gm' with 'g' everywhere (case insensitive) - INCLUDING INSTRUCTIONS
    text = re.sub(r'\bgm\b', 'g', text, flags=re.IGNORECASE)
    
    # Replace 'sec.' with 'secs.'
    text = re.sub(r'\bsec\.', 'secs.', text)
    
    # Fix time formatting: 1:00 mins -> 1:00 min, but keep other times as mins
    def fix_time_format(match):
        time_part = match.group(1)
        if time_part == '1:00':
            return '1:00 min'
        else:
            return f'{time_part} mins'
    
    # Handle time patterns like "1:00 mins" or "2:30 mins"
    text = re.sub(r'(\d+:\d+)\s*mins?\.?', fix_time_format, text)
    
    return text

def clean_recipe_data(recipe_data):
    """Clean all recipe data fields according to formatting requirements"""
    import copy
    
    # Create a deep copy to avoid modifying original data
    cleaned_data = copy.deepcopy(recipe_data)
    
    # Clean Instructions - specifically targeting 'gm' to 'g' conversion
    if 'Instruction' in cleaned_data:
        for instruction in cleaned_data['Instruction']:
            if 'Text' in instruction:
                # Clean the instruction text
                original_text = instruction['Text']
                cleaned_text = clean_time_and_units_text(original_text)
                instruction['Text'] = cleaned_text
                
                # Debug log to show gm -> g conversions
                if 'gm' in original_text.lower():
                    print(f"🔄 Instruction cleaned: '{original_text}' → '{cleaned_text}'")
    
    # Clean Ingredients weights
    if 'Ingredients' in cleaned_data:
        for ingredient in cleaned_data['Ingredients']:
            if 'weight' in ingredient:
                ingredient['weight'] = clean_time_and_units_text(ingredient['weight'])
            if 'text' in ingredient:
                ingredient['text'] = clean_time_and_units_text(ingredient['text'])
            if 'title' in ingredient:
                ingredient['title'] = clean_time_and_units_text(ingredient['title'])
    
    # Clean description
    if 'description' in cleaned_data:
        cleaned_data['description'] = clean_time_and_units_text(cleaned_data['description'])
    
    return cleaned_data

def _default_output_path_from(source_path, candidate_name):
    """Build a default PDF path next to source_path using candidate_name as basename."""
    directory = os.path.dirname(os.path.abspath(source_path)) if source_path else os.getcwd()
    base = sanitize_filename(candidate_name)
    return os.path.join(directory, f"{base}.pdf")

def _resolve_output_pdf_path(output_pdf_path, candidate_name, source_path, recipe_data=None):
    """Respect explicit output path; otherwise derive from recipe name or candidate/source."""
    # If a directory was provided, place the derived filename inside it
    if output_pdf_path and os.path.isdir(output_pdf_path):
        directory = output_pdf_path
        if recipe_data and recipe_data.get('name'):
            base = sanitize_filename(recipe_data.get('name', ['recipe'])[0])
        else:
            base = sanitize_filename(candidate_name)
        return os.path.join(directory, f"{base}.pdf")

    # Treat blank/None and common default names as signals to derive a name
    def _looks_like_default(path_str):
        if not path_str:
            return True
        base = os.path.basename(path_str).lower()
        default_names = {
            'recipe_output.pdf',
            'recipe output.pdf',
            'recepie_output.pdf',
            'recepie output.pdf',
            'output.pdf',
            'default.pdf'
        }
        if base in default_names:
            return True
        return re.match(r'^(recip(e|ie)[ _-]?output)(\.pdf)?$', base or '') is not None

    if not _looks_like_default(output_pdf_path):
        return output_pdf_path

    # Use recipe name if available, otherwise fall back to candidate/source
    if recipe_data and recipe_data.get('name'):
        base = sanitize_filename(recipe_data.get('name', ['recipe'])[0])
        return os.path.join(os.path.dirname(os.path.abspath(source_path)) if source_path else os.getcwd(), f"{base}.pdf")
    return _default_output_path_from(source_path, candidate_name)


# ===============================================================
# INGREDIENT IMAGE LOOKUP (module-level helpers)
# ===============================================================
# Keyed by the recipe's ZIP STEM (e.g. "THUKPA SOUP" from THUKPA SOUP.zip),
# NOT by the name written inside the recipe txt. This is the same key
# parse_txt_to_json.build_ingredient_image_index() uses, with the same
# rules (sorted filenames, first match per stem.upper() wins), so the
# popup PDF and recipes_fix.json always agree on which image a recipe
# gets. It also reads ingredient_images/ directly, so it no longer
# depends on recipes_fix.json, which is rebuilt AFTER popups in the
# pipeline and is therefore one run stale at this point.

INGREDIENT_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
_INGREDIENT_IMAGE_INDEX = None


def _load_ingredient_image_index():
    """Scans INGREDIENT_IMAGE_DIR once per process -> {STEM_UPPER: full path}."""
    global _INGREDIENT_IMAGE_INDEX
    if _INGREDIENT_IMAGE_INDEX is not None:
        return _INGREDIENT_IMAGE_INDEX

    index = {}
    if not os.path.isdir(INGREDIENT_IMAGE_DIR):
        print(f"⚠️  Ingredient image dir not found: {INGREDIENT_IMAGE_DIR}")
        _INGREDIENT_IMAGE_INDEX = index
        return index

    for fname in sorted(os.listdir(INGREDIENT_IMAGE_DIR)):
        path = os.path.join(INGREDIENT_IMAGE_DIR, fname)
        name, ext = os.path.splitext(fname)
        if not os.path.isfile(path) or ext.lower() not in INGREDIENT_IMAGE_EXTENSIONS:
            continue
        key = name.upper()
        if key in index:
            print(f"⚠️  Duplicate ingredient image for '{key}': "
                  f"'{os.path.basename(index[key])}' and '{fname}' -- keeping the first")
            continue
        index[key] = path

    print(f"✅ Indexed {len(index)} ingredient image(s) from {INGREDIENT_IMAGE_DIR}")
    _INGREDIENT_IMAGE_INDEX = index
    return index


def find_ingredient_image_path(zip_stem):
    """Returns the ingredient image path for this ZIP stem, or None."""
    stem = (zip_stem or '').strip()
    if not stem:
        return None

    path = _load_ingredient_image_index().get(stem.upper())
    if path:
        print(f"🖼  Found ingredient image for '{stem}': {path}")
        return path

    print(f"ℹ️  No ingredient image found for '{stem}'")
    return None


# ===============================================================
# HINDI / DEVANAGARI HELPERS
# ===============================================================

# Exact overrides are preferred for recipe names because recipe names are
# often transliterations rather than literal dictionary translations.
HINDI_RECIPE_NAME_OVERRIDES = {
    "ALOO MATAR RASSA": "आलू मटर रस्सा",
    "ALOO MATAR RASA": "आलू मटर रस्सा",
    "APPLE CINNAMON CAKE": "एप्पल सिनेमन केक",
    "APPLE CIN CAKE": "एप्पल सिनेमन केक",
    "AALNI BHAAT": "आलनी भात",
}

# Longest phrases are replaced first.
HINDI_PHRASE_MAP = {
    "Whole Spice & Tampering": "साबुत मसाले और तड़का",
    "Whole Spice & Tempering": "साबुत मसाले और तड़का",
    "Kashmiri Red Chilli Powder": "कश्मीरी लाल मिर्च पाउडर",
    "Kashmiri Red Chili Powder": "कश्मीरी लाल मिर्च पाउडर",
    "Coriander Powder": "धनिया पाउडर",
    "Red Chilli Powder": "लाल मिर्च पाउडर",
    "Red Chili Powder": "लाल मिर्च पाउडर",
    "Whole Red Chilli": "साबुत लाल मिर्च",
    "Whole Red Chili": "साबुत लाल मिर्च",
    "Musturd Seeds": "सरसों के दाने",
    "Mustard Seeds": "सरसों के दाने",
    "Cumin Seeds": "जीरा",
    "Black Pepper": "काली मिर्च",
    "Green Chilli": "हरी मिर्च",
    "Green Chili": "हरी मिर्च",
    "Green Peas": "हरे मटर",
    "Diced Potatoes": "कटे हुए आलू",
    "Raw Gravy Paste": "कच्ची ग्रेवी पेस्ट",
    "Kasuri Methi": "कसूरी मेथी",
    "Kasturi Methi": "कसूरी मेथी",
}

HINDI_WORD_MAP = {
    "aloo": "आलू",
    "potato": "आलू",
    "potatoes": "आलू",
    "matar": "मटर",
    "peas": "मटर",
    "rassa": "रस्सा",
    "rasa": "रस्सा",
    "paneer": "पनीर",
    "chicken": "चिकन",
    "mutton": "मटन",
    "fish": "मछली",
    "egg": "अंडा",
    "eggs": "अंडे",
    "curry": "करी",
    "masala": "मसाला",
    "gravy": "ग्रेवी",
    "rice": "चावल",
    "pulao": "पुलाव",
    "biryani": "बिरयानी",
    "dal": "दाल",
    "khichdi": "खिचड़ी",
    "soup": "सूप",
    "fried": "फ्राइड",
    "fry": "फ्राई",
    "jeera": "जीरा",
    "oil": "तेल",
    "water": "पानी",
    "garlic": "लहसुन",
    "ginger": "अदरक",
    "tomato": "टमाटर",
    "tomatoes": "टमाटर",
    "onion": "प्याज",
    "onions": "प्याज",
    "turmeric": "हल्दी",
    "salt": "नमक",
    "chilli": "मिर्च",
    "chili": "मिर्च",
    "powder": "पाउडर",
    "raw": "कच्चा",
    "paste": "पेस्ट",
    "whole": "साबुत",
    "spice": "मसाला",
    "spices": "मसाले",
    "apple": "एप्पल",
    "cinnamon": "सिनेमन",
    "cake": "केक",
    "batter": "बैटर",
    "flour": "आटा",
    "butter": "बटर",
    "sugar": "चीनी",
    "milk": "दूध",
    "tempering": "तड़का",
    "chopped": "कटा हुआ",
    "vegetable": "सब्ज़ी",
    "vegetables": "सब्ज़ियाँ",
    "soaked": "भिगोया हुआ",
    "stock": "स्टॉक",
    "grill": "ग्रिल",
    "mesh": "मेश",
    "mats": "मैट्स",
    "mold": "मोल्ड",
    "mould": "मोल्ड",
    "stirrer": "स्टिरर",
    "tray": "ट्रे",
    "rack": "रैक",
    "stand": "स्टैंड",
    "pressure": "प्रेशर",
    "cooker": "कुकर",
    "mutton": "मटन",
    "mix": "मिक्स",
    "sauce": "सॉस",
    "noodles": "नूडल्स",
    "momo": "मोमो",
    "kit": "किट",
    "and": "और",
}

HINDI_ACTION_MAP = {
    "heat": "गरम करें",
    "cook": "पकाएँ",
    "add": "डालें",
    "take": "लें",
    "mix": "मिलाएँ",
    "stir": "चलाएँ",
    "boil": "उबालें",
    "fry": "तलें",
    "saute": "भूनें",
    "sauté": "भूनें",
}



def extract_full_recipe_name_from_description(recipe_data):
    """Get the customer-facing English recipe name from description first.

    Supported description examples:
        ALOO MATAR RASSA
        OUTPUT 600 GM
        ...

        RECIPE NAME: ALOO MATAR RASSA
        OUTPUT 600 GM

        NAME - ALOO MATAR RASSA

    The description is authoritative when it contains a recipe-name line.
    If the current recipe description contains only metadata (OUTPUT,
    NORMAL COOKING TIME, ACCESSORIES, etc.), fall back to recipe_data["name"]
    and then audio1.
    """
    desc = str(recipe_data.get("description", "") or "")
    lines = [line.strip() for line in desc.splitlines() if line.strip()]

    # Explicit labelled forms have highest priority.
    for line in lines:
        m = re.match(
            r'^(?:RECIPE\s*NAME|FULL\s*NAME|NAME)\s*[:\-]\s*(.+?)\s*$',
            line,
            flags=re.IGNORECASE
        )
        if m and m.group(1).strip():
            return m.group(1).strip()

    # A free-standing title is expected before the metadata block.
    metadata_prefixes = (
        "OUTPUT",
        "TOTAL OUTPUT",
        "NORMAL COOKING TIME",
        "COOKING TIME",
        "ACCESSORIES",
        "ACCESSORY",
        "TOTAL INPUT",
    )

    for line in lines:
        upper = line.upper().strip()

        # Once metadata starts, remaining free text is metadata/accessory data,
        # not the recipe title.
        if any(upper.startswith(prefix) for prefix in metadata_prefixes):
            break

        # Ignore obvious separators/noise.
        if upper in {"DESCRIPTION", "RECIPE", "DETAILS"}:
            continue

        if line:
            return line

    # Current/legacy files may not store the recipe title in description.
    value = recipe_data.get("name", "")
    if isinstance(value, list):
        value = value[0] if value else ""
    value = str(value or "").strip()
    if value:
        return value

    audio1 = recipe_data.get("audio1", "")
    if isinstance(audio1, list):
        audio1 = audio1[0] if audio1 else ""
    audio1 = str(audio1 or "").strip()
    if audio1:
        return audio1

    return "Unknown Recipe"



def _recipe_name_as_text(recipe_data):
    return extract_full_recipe_name_from_description(recipe_data)


def _first_nonempty_recipe_field(recipe_data, keys):
    for key in keys:
        value = recipe_data.get(key)
        if isinstance(value, list):
            value = value[0] if value else ""
        value = str(value or "").strip()
        if value:
            return value
    return ""


def _translate_words_fallback(text):
    """Dictionary-based fallback used only when no exact Hindi field/override exists."""
    text = str(text or "").strip()
    if not text:
        return ""

    # Phrase replacements first.
    translated = text
    for english, hindi in sorted(HINDI_PHRASE_MAP.items(), key=lambda kv: len(kv[0]), reverse=True):
        translated = re.sub(re.escape(english), hindi, translated, flags=re.IGNORECASE)

    # Translate remaining standalone English words while preserving numbers/punctuation.
    parts = re.split(r'(\s+|[,/&()\-]+)', translated)
    out = []
    for part in parts:
        key = part.strip().lower()
        if key in HINDI_WORD_MAP:
            # Preserve the surrounding whitespace from the original token.
            prefix = part[:len(part) - len(part.lstrip())]
            suffix = part[len(part.rstrip()):] if part.rstrip() != part else ""
            out.append(prefix + HINDI_WORD_MAP[key] + suffix)
        else:
            out.append(part)
    return "".join(out).strip()


def get_hindi_recipe_name(recipe_data):
    """Return a Hindi subtitle for the recipe.

    Priority:
      1. Hindi field already present in recipe JSON.
      2. Exact override dictionary.
      3. Dictionary-based translation/transliteration fallback.
    """
    explicit = _first_nonempty_recipe_field(
        recipe_data,
        (
            "name_hi", "name_hindi", "hindi_name", "nameHindi",
            "hindiName", "NameHindi", "name_hi_IN"
        )
    )
    if explicit:
        return explicit

    english_name = _recipe_name_as_text(recipe_data)
    override = HINDI_RECIPE_NAME_OVERRIDES.get(english_name.upper())
    if override:
        return override

    fallback = _translate_words_fallback(english_name)
    return fallback if fallback else english_name


def _strip_quantities_and_units(text):
    """Remove recipe quantities from merged step text before Hindi translation."""
    text = str(text or "")
    # Examples removed: 150 g, 500 ml, 2.5 kg, 1 tbsp, etc.
    text = re.sub(
        r'\b\d+(?:\.\d+)?\s*(?:g|gm|gms|kg|ml|l|tsp|tbsp|cup|cups)\b',
        '',
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'\s*,\s*', ', ', text)
    return text.strip(" ,")


def translate_ingredient_text_to_hindi(text):
    text = _strip_quantities_and_units(text)
    if not text:
        return ""

    # Translate comma-separated merged ingredient names independently.
    items = [item.strip() for item in text.split(",") if item.strip()]
    translated_items = [_translate_words_fallback(item) for item in items]
    translated_items = [item for item in translated_items if item]

    if not translated_items:
        return _translate_words_fallback(text)
    if len(translated_items) == 1:
        return translated_items[0]
    if len(translated_items) == 2:
        return f"{translated_items[0]} और {translated_items[1]}"
    return ", ".join(translated_items[:-1]) + f" और {translated_items[-1]}"


def translate_step_to_hindi(step):
    """Translate one generated Recipe Steps line into natural Hindi."""
    action = str(step.get("Audio", "") or "").strip().lower()
    ingredient_text = str(step.get("Text", "") or "").strip()

    # If Text is absent, try app_audio after removing a known English action.
    if not ingredient_text:
        app_audio = str(step.get("app_audio", "") or "").strip()
        ingredient_text = re.sub(
            r'^(Heat|Cook|Add|Take|Mix|Stir|Boil|Fry|Saute|Sauté)\s+',
            '',
            app_audio,
            flags=re.IGNORECASE
        ).strip()

    ingredient_hi = translate_ingredient_text_to_hindi(ingredient_text)

    # Infer action from app_audio if Audio is blank.
    if not action:
        app_audio = str(step.get("app_audio", "") or "").strip()
        m = re.match(r'^(Heat|Cook|Add|Take|Mix|Stir|Boil|Fry|Saute|Sauté)\b', app_audio, flags=re.IGNORECASE)
        if m:
            action = m.group(1).lower()

    # Merged zero-duration steps often lose the original Add action.
    # If there is no action, use "डालें" for water/addition-like steps.
    if not action:
        raw_lower = ingredient_text.lower()
        if "water" in raw_lower or "," in ingredient_text:
            action_hi = "डालें"
        else:
            action_hi = ""
    else:
        action_hi = HINDI_ACTION_MAP.get(action, "")

    if ingredient_hi and action_hi:
        line = f"{ingredient_hi} {action_hi}"
    elif ingredient_hi:
        line = ingredient_hi
    elif action_hi:
        line = action_hi
    else:
        # Last-resort translation of whatever text we have.
        line = _translate_words_fallback(ingredient_text)

    lid = str(step.get("lid", "") or "").strip().lower()
    if lid == "open":
        line += " — ढक्कन खोलें"
    elif lid == "close":
        line += " — ढक्कन बंद रखें"

    return line.strip()



# Fixed bilingual guidance around the dynamic recipe steps.
# These lines are NOT numbered; only the actual recipe steps are numbered.
RECIPE_STEPS_INTRO_EN = (
    "Please place the required cooking vessel on the On2Cook induction top. "
    "Then follow only the manual setup or ingredient-addition steps shown below:"
)
RECIPE_STEPS_INTRO_HI = (
    "कृपया आवश्यक कुकिंग वेसल को On2Cook इंडक्शन टॉप पर रखें। फिर केवल "
    "नीचे दिए गए मैनुअल सेटअप या सामग्री डालने वाले चरणों का पालन करें:"
)

RECIPE_STEPS_OUTRO_EN = (
    "Please open the lid of the On2Cook device, take out the food, and serve it."
)
RECIPE_STEPS_OUTRO_HI = (
    "कृपया On2Cook डिवाइस का ढक्कन खोलें, खाना निकालें और परोसें।"
)


def _skip_is_true(step):
    """Normalize the internal `skip` flag.

    Customer-facing interpretation:
      - skip == false/blank: after this cooking stage the next stage MAY
        require a manual setup / ingredient-addition prompt.
      - skip == true: the device continues automatically.

    Source recipe files commonly encode False as an empty string.
    """
    value = step.get('skip', False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y')


def _disclaimer_source_steps(recipe_data):
    """Return ORIGINAL unmerged Instruction rows for skip/manual logic."""
    original = recipe_data.get('_disclaimer_instruction_source')
    if isinstance(original, list) and original:
        return original
    return recipe_data.get('Instruction', []) or []


def _normalize_manual_name(text):
    text = str(text or '').lower()
    text = text.replace('&', ' and ')
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def _strip_action_prefix(text):
    """Remove action verbs so a manual customer line can consistently say ADD."""
    text = str(text or '').strip()
    return re.sub(
        r'^(?:Heat|Cook|Add|Take|Mix|Stir|Boil|Fry|Saute|Sauté|Pour|Place|Put)\s+',
        '',
        text,
        flags=re.IGNORECASE
    ).strip()


def _water_like(text):
    """Detect any water-related ingredient name, including source typo 'Watar'."""
    return re.search(r'\b(?:water|watar)\b', str(text or ''), flags=re.IGNORECASE) is not None


def _is_automatic_pump_water(text):
    """Return True ONLY for ordinary pump water.

    Rule requested for the customer recipe sheet:
      - "Water" / "Watar" by itself -> automatic On2Cook pump water
      - "Stock Water", "Coconut Water", "Rice Water", etc. -> SPECIAL water,
        therefore it is a manual ingredient and must be shown in Recipe Steps.

    Quantity/unit text is ignored when deciding, so:
        "500 ml Water" -> automatic
        "2200 g Stock Water" -> manual
    """
    text = str(text or '').strip()

    # Remove a leading quantity/unit if this helper receives a full label.
    text = re.sub(
        r'^\s*\d+(?:\.\d+)?\s*(?:g|gm|gms|kg|ml|l|liter|litre|tsp|tbsp|cup|cups)\s+',
        '',
        text,
        flags=re.IGNORECASE
    ).strip()

    normalized = _normalize_manual_name(text)
    return normalized in ('water', 'watar')


_ACCESSORY_TERMS = (
    'grill mesh', 'mesh mat', 'mesh mats',
    'cake mold', 'cake mould', 'mold', 'mould',
    'stirrer', 'tray', 'rack', 'stand',
    'cake kit', 'accessory', 'accessories',
)


def _is_accessory_text(text):
    normalized = _normalize_manual_name(text)
    if not normalized:
        return False
    return any(term in normalized for term in _ACCESSORY_TERMS)


def _is_machine_action_only_text(text):
    """Reject cooking operations that are not ingredients/accessories.

    This specifically prevents output such as "Add 0 g Simmer".
    """
    text = str(text or '').strip()
    if not text:
        return True

    lower = text.lower().strip()
    if re.match(
        r'^(?:heat|cook|boil|simmer|steam|fry|saute|sauté|mix|stir|roast|'
        r'bake|grill|blend|whisk|knead|rest|cool|chill|freeze|serve|'
        r'garnish|season|cover|uncover|temperature\s+down)\b',
        lower
    ):
        return True

    add_water_match = re.match(
        r'^(?:add|pour)\s+(.+)$',
        lower,
        flags=re.IGNORECASE
    )
    if add_water_match and _is_automatic_pump_water(add_water_match.group(1)):
        return True

    return False


def _parse_amount(text):
    """Return (numeric value, unit, base-value) using the generator's 1:1 liquid convention."""
    text = str(text or '').strip()
    m = re.search(
        r'(-?\d+(?:\.\d+)?)\s*(g|gm|gms|kg|ml|l|liter|litre|tsp|tbsp|cup|cups|pinch|number|no|nos|pcs|pc)\b',
        text,
        flags=re.IGNORECASE
    )
    if not m:
        return None, None, None

    qty = float(m.group(1))
    unit = m.group(2).lower()

    factors = {
        'g': 1, 'gm': 1, 'gms': 1,
        'kg': 1000,
        'ml': 1, 'l': 1000, 'liter': 1000, 'litre': 1000,
        'tsp': 5, 'tbsp': 15, 'cup': 240, 'cups': 240,
        'pinch': 0.5,
        # Equipment-count units are intentionally not used for food weight totals.
        'number': None, 'no': None, 'nos': None, 'pcs': None, 'pc': None,
    }
    factor = factors.get(unit)
    base = qty * factor if factor is not None else None
    return qty, unit, base


def _positive_food_weight(text):
    qty, unit, _ = _parse_amount(text)
    if qty is None:
        return None
    if unit in ('number', 'no', 'nos', 'pcs', 'pc'):
        return None
    return qty > 0


def _parse_ingredient_components(ingredient):
    """Parse a prepared ingredient group's components.

    Primary source is Ingredients[].text. If text is blank, fall back to
    Ingredients[].title because some recipes (for example BOILED MUTTON)
    store the full component list directly in the title:
        "Raw Mutton 1kg, Water 500ml, Salt 5g, Turmeric Powder 1g"
    """
    raw = str(ingredient.get('text', '') or '').strip()
    if not raw:
        raw = str(ingredient.get('title', '') or '').strip()
    if not raw:
        return []

    qty_re = re.compile(
        r'(\d+(?:\.\d+)?)\s*(g|gm|gms|kg|ml|l|liter|litre|tsp|tbsp|cup|cups|pinch|number|no|nos|pcs|pc)\b',
        flags=re.IGNORECASE
    )

    components = []
    cursor = 0
    for match in qty_re.finditer(raw):
        name = raw[cursor:match.start()]
        name = name.strip(" ,;:-")
        # Normalize the known source typo for customer-facing copy.
        name = re.sub(r'\bWatar\b', 'Water', name, flags=re.IGNORECASE)
        # If the preceding chunk contains a comma, keep only the text after it.
        if ',' in name:
            name = name.split(',')[-1].strip()
        if ';' in name:
            name = name.split(';')[-1].strip()

        qty_text = clean_time_and_units_text(
            f"{match.group(1)} {match.group(2)}"
        )
        _, _, base = _parse_amount(qty_text)

        if name:
            components.append((name, qty_text, base))
        cursor = match.end()

    return components


def _manual_components_from_ingredient_group(ingredient):
    """Return manual (non-water) labels from one Ingredients[] group."""
    title = str(ingredient.get('title', '') or '').strip()
    weight = clean_time_and_units_text(str(ingredient.get('weight', '') or '').strip())
    components = _parse_ingredient_components(ingredient)

    has_any_water_component = any(
        _water_like(name) for name, _, _ in components
    )
    title_mentions_water = _water_like(title)

    # Any food+water group is decomposed into its real components.
    # Only plain "Water" is removed as automatic pump water.
    # Special water such as Stock Water remains a manual component.
    if title_mentions_water or has_any_water_component:
        kept = []
        for name, qty_text, _ in components:
            if _is_automatic_pump_water(name):
                continue
            kept.append(f"{qty_text} {name}".strip())

        if kept:
            return kept
        return []

    # Normal prepared ingredient group: trust Ingredients[].weight/title.
    if title and weight:
        qty, unit, _ = _parse_amount(weight)
        if qty is not None and qty <= 0:
            return []
        return [f"{weight} {title}".strip()]

    return [title] if title else []


def _hindi_manual_label(english_label):
    """Translate the ingredient name while keeping the quantity/unit readable."""
    text = str(english_label or '').strip()
    m = re.match(
        r'^\s*(\d+(?:\.\d+)?\s*(?:g|gm|gms|kg|ml|l|tsp|tbsp|cup|cups|pinch|number|no|nos|pcs|pc))\s+(.+)$',
        text,
        flags=re.IGNORECASE
    )
    if m:
        qty = clean_time_and_units_text(m.group(1))
        name_hi = _translate_words_fallback(m.group(2)).replace(" & ", " और ")
        return f"{qty} {name_hi}".strip()
    return _translate_words_fallback(text).replace(" & ", " और ")


def _meaningful_instruction_stage_names(recipe_data):
    """Distinct non-accessory/non-operation ingredient stage names."""
    names = []
    seen = set()
    for step in _disclaimer_source_steps(recipe_data):
        raw = str(step.get('Text', '') or '').strip()
        if not raw:
            raw = _strip_action_prefix(step.get('app_audio', ''))

        if (
            not raw
            or _is_accessory_text(raw)
            or _is_machine_action_only_text(raw)
        ):
            continue

        # Only a stage whose ingredient is literally plain Water is automatic.
        # "Soaked Rice & Stock Water" and other named waters are real manual stages.
        if _is_automatic_pump_water(raw):
            continue

        # A 0g food-looking row should not create a stage.
        positive = _positive_food_weight(step.get('Weight', ''))
        if positive is False:
            continue

        key = _normalize_manual_name(raw)
        if key and key not in seen:
            seen.add(key)
            names.append(key)
    return names


def _position_ingredient_map(recipe_data):
    """Order-map stages to Ingredients[] only when counts genuinely line up.

    This fixes recipes such as AALNI BHAAT where the first instruction label
    differs from the Ingredients[] group title, while avoiding bad mappings
    for recipes such as APPLE CINNAMON CAKE where one generic 'Batter' stage
    does not correspond one-to-one with all prepared ingredient groups.
    """
    stage_names = _meaningful_instruction_stage_names(recipe_data)
    ingredients = recipe_data.get('Ingredients', []) or []

    if not stage_names or len(stage_names) != len(ingredients):
        return {}

    return {stage_name: idx for idx, stage_name in enumerate(stage_names)}


def _best_ingredient_group_for_step(step, recipe_data):
    ingredients = recipe_data.get('Ingredients', []) or []
    if not ingredients:
        return None

    raw = str(step.get('Text', '') or '').strip()
    if not raw:
        raw = _strip_action_prefix(step.get('app_audio', ''))
    step_key = _normalize_manual_name(raw)

    # 1) Exact title match.
    for ing in ingredients:
        if _normalize_manual_name(ing.get('title', '')) == step_key and step_key:
            return ing

    # 2) Strong token-overlap match.
    step_tokens = set(step_key.split())
    best = None
    best_score = 0.0
    for ing in ingredients:
        ing_key = _normalize_manual_name(ing.get('title', ''))
        ing_tokens = set(ing_key.split())
        if not step_tokens or not ing_tokens:
            continue
        score = len(step_tokens & ing_tokens) / max(1, len(step_tokens | ing_tokens))
        if score > best_score:
            best_score = score
            best = ing

    if best is not None and best_score >= 0.60:
        return best

    # 3) Positional source-of-truth mapping, but ONLY if the counts align.
    positional = _position_ingredient_map(recipe_data)
    idx = positional.get(step_key)
    if idx is not None and 0 <= idx < len(ingredients):
        return ingredients[idx]

    return None


def _accessory_item_from_step(step):
    """Return a readable accessory item from an Instruction row, else None."""
    raw = str(step.get('Text', '') or '').strip()
    if not raw:
        raw = _strip_action_prefix(step.get('app_audio', ''))

    if not _is_accessory_text(raw):
        return None

    weight = clean_time_and_units_text(str(step.get('Weight', '') or '').strip())
    qty, unit, _ = _parse_amount(weight)

    # Remove a quantity already embedded in Text ("2 GRILL MESH") so we do
    # not render "2 2 Grill Mesh".
    raw_name = re.sub(r'^\s*\d+(?:\.\d+)?\s*', '', raw).strip()
    raw_name = raw_name.title().replace(" And ", " and ")

    # "2 number" -> show "2"; avoid the word "number" in customer copy.
    count_prefix = ''
    if qty is not None and qty > 0:
        if float(qty).is_integer():
            count_prefix = str(int(qty))
        else:
            count_prefix = str(qty)

    english = f"{count_prefix} {raw_name}".strip() if count_prefix else raw_name

    hi_name = _translate_words_fallback(raw_name)
    hindi = f"{count_prefix} {hi_name}".strip() if count_prefix else hi_name

    return english, hindi


def _description_accessories(recipe_data):
    """Read the ACCESSORIES section from description without changing case."""
    description = str(recipe_data.get('description', '') or '')
    lines = [line.strip() for line in description.splitlines()]
    accessories = []
    collecting = False

    stop_words = ('OUTPUT', 'FINAL OUTPUT', 'NORMAL COOKING', 'COOKING TIME', 'OTHER ESSENTIALS', 'NOTE', 'NOTES')

    for line in lines:
        if not line:
            continue

        if line.upper() == 'ACCESSORIES':
            collecting = True
            continue

        if collecting:
            upper = line.upper()
            if any(upper.startswith(word) for word in stop_words):
                break
            accessories.append(line)

    return accessories


_SPECIAL_DESCRIPTION_ACCESSORY_TERMS = (
    'pressure cooker',
    'cake kit',
    'momo kit',
    'grill mesh',
    'mesh mat',
    'mesh mats',
    'cake mold',
    'cake mould',
    'rack',
    'tray',
    'stand',
)


def _is_generic_pan_accessory(text):
    normalized = _normalize_manual_name(text)
    return (
        normalized.startswith('pan ')
        or ' coated pan' in normalized
        or 'non coated pan' in normalized
        or 'non coated ss' in normalized
        or normalized == 'pan non coated ss'
        or normalized == 'pan non coated'
        or normalized == 'pan coated'
    )


def _description_special_accessories(recipe_data):
    """Return setup-critical accessories from description.

    Rules:
      - Explicit setup items like Pressure Cooker / Grill Mesh / Cake Mold /
        MOMO KIT / named stirrers are setup-critical.
      - If a generic pan appears together with any additional accessory,
        the whole combination is treated as a prerequisite setup. This is the
        requested behavior for cases such as:
            Pan Non-Coated (SS) + MOMO KIT
            Pan Non-Coated (SS) + Noodles Stirrer
    """
    accessories = _description_accessories(recipe_data)
    if not accessories:
        return []

    generic_pan_items = [a for a in accessories if _is_generic_pan_accessory(a)]
    other_items = [a for a in accessories if not _is_generic_pan_accessory(a)]

    # Requested rule: anything used WITH Pan Non-Coated (SS) becomes a
    # prerequisite setup, and the combined setup should be shown together.
    if generic_pan_items and other_items:
        return generic_pan_items + other_items

    result = []
    for accessory in accessories:
        normalized = _normalize_manual_name(accessory)

        is_known_special = any(
            term in normalized
            for term in _SPECIAL_DESCRIPTION_ACCESSORY_TERMS
        )

        # A named/specialized stirrer (e.g. "Noodles Stirrer") requires
        # explicit setup. Generic Silicone/Silicon Stirrer does not.
        is_special_stirrer = (
            normalized.endswith(' stirrer')
            and normalized not in ('silicone stirrer', 'silicon stirrer')
        )

        if is_known_special or is_special_stirrer:
            result.append(accessory)

    return result

def _build_accessory_setup_pair(recipe_data):
    """Build one accessory setup instruction.

    Priority:
      1. Explicit accessory Instruction rows (best source).
      2. If none exist, setup-critical accessories from description.
    """
    explicit_en = []
    explicit_hi = []
    seen = set()

    for step in _disclaimer_source_steps(recipe_data):
        item = _accessory_item_from_step(step)
        if not item:
            continue
        key = _normalize_manual_name(item[0])
        if key in seen:
            continue
        seen.add(key)
        explicit_en.append(item[0])
        explicit_hi.append(item[1])

    if explicit_en:
        en = (
            "Set up the required accessories in the On2Cook device: "
            + "; ".join(explicit_en) + "."
        )
        hi = (
            "On2Cook डिवाइस में आवश्यक एक्सेसरीज़ सेट करें: "
            + "; ".join(explicit_hi) + "।"
        )
        return en, hi

    special = _description_special_accessories(recipe_data)
    if not special:
        return None

    # Pressure Cooker is a cooking vessel, so phrase it naturally.
    if len(special) == 1 and 'pressure cooker' in _normalize_manual_name(special[0]):
        return (
            "Use the Pressure Cooker as the cooking vessel.",
            "कुकिंग वेसल के रूप में प्रेशर कुकर का उपयोग करें।"
        )

    # Recipe-specific stirrer.
    if len(special) == 1 and _normalize_manual_name(special[0]).endswith(' stirrer'):
        accessory = special[0]
        hindi_accessory = _translate_words_fallback(accessory).replace(" & ", " और ")
        return (
            f"Fit the {accessory} before cooking.",
            f"खाना पकाने से पहले {hindi_accessory} लगाएँ।"
        )

    english = "; ".join(special)
    hindi = "; ".join(_translate_words_fallback(x).replace(" & ", " और ") for x in special)
    return (
        f"Set up the required accessories: {english}.",
        f"आवश्यक एक्सेसरीज़ सेट करें: {hindi}।"
    )


def _manual_target_vessel(recipe_data):
    """Choose the correct customer-facing vessel for manual additions."""
    explicit_text = " ".join(
        str(step.get('Text', '') or '')
        for step in _disclaimer_source_steps(recipe_data)
        if _is_accessory_text(step.get('Text', ''))
    )

    description_special = " ".join(_description_special_accessories(recipe_data))
    accessory_text = f"{explicit_text} {description_special}".lower()

    if 'pressure cooker' in accessory_text:
        return "pressure cooker", "प्रेशर कुकर"
    if 'cake mold' in accessory_text or 'cake mould' in accessory_text:
        return "cake mold", "केक मोल्ड"
    if 'tray' in accessory_text:
        return "tray", "ट्रे"
    if 'bowl' in accessory_text:
        return "bowl", "बाउल"
    return "pan", "पैन"


def _manual_ingredient_item(step, recipe_data):
    """Return (English, Hindi) food item for a MANUAL addition.

    Priorities:
      1. Reject accessories, Water and machine-only operations.
      2. Use Ingredients[] as quantity/title source when the stage can be
         matched reliably.
      3. For mixed food+water Ingredients[] groups, retain ONLY non-water
         subcomponents.
      4. Fall back to Instruction text/weight when no safe Ingredients[]
         mapping exists.
    """
    raw = str(step.get('Text', '') or '').strip()
    if not raw:
        raw = _strip_action_prefix(step.get('app_audio', ''))

    if not raw or _is_accessory_text(raw):
        return None

    # IMPORTANT: try the Ingredients[] source BEFORE deciding this is a
    # machine-only action. Ingredient-group names can legitimately begin
    # with action-looking words, e.g. "Mix Sauce & Noodles".
    matched_group = _best_ingredient_group_for_step(step, recipe_data)
    if matched_group is not None:
        labels = _manual_components_from_ingredient_group(matched_group)
        if not labels:
            return None

        english = _join_english_items(labels)
        hindi = _join_hindi_items([_hindi_manual_label(x) for x in labels])
        return english, hindi

    # Only an unmatched row is allowed to be discarded as a machine action.
    # This still correctly rejects rows such as "Simmer" / "Temperature Down".
    if _is_machine_action_only_text(raw):
        return None

    # Fallback: handle comma-separated already-merged instruction text.
    if ',' in raw:
        fragments = [f.strip() for f in raw.split(',') if f.strip()]
        fragments = [
            f for f in fragments
            if not _is_automatic_pump_water(f)
            and not _is_accessory_text(f)
            and not _is_machine_action_only_text(f)
        ]
        if not fragments:
            return None
        english = ', '.join(fragments)
        hindi = _join_hindi_items([_hindi_manual_label(x) for x in fragments])
        return clean_time_and_units_text(english), clean_time_and_units_text(hindi)

    # ONLY plain "Water" is automatic. Named/special water is manual.
    if _is_automatic_pump_water(raw):
        return None

    # Never generate nonsense such as "0 g Simmer".
    positive = _positive_food_weight(step.get('Weight', ''))
    if positive is False:
        return None

    english_name = _strip_action_prefix(raw)
    if not english_name:
        return None

    weight = clean_time_and_units_text(str(step.get('Weight', '') or '').strip())
    english = f"{weight} {english_name}".strip() if weight else english_name
    hindi = _hindi_manual_label(english)

    return clean_time_and_units_text(english), clean_time_and_units_text(hindi)


def _join_english_items(items):
    items = [x for x in items if x]
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ', '.join(items[:-1]) + f", and {items[-1]}"


def _join_hindi_items(items):
    items = [x for x in items if x]
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} और {items[1]}"
    return ', '.join(items[:-1]) + f" और {items[-1]}"


def _lid_instruction_pair(lid_value, initial=False):
    """Concise lid guidance after a manual setup/addition."""
    lid = str(lid_value or '').strip().lower()

    if lid == 'open':
        return "Keep the lid open.", "ढक्कन खुला रखें।"

    if lid == 'close':
        if initial:
            return (
                "Close the lid to start cooking.",
                "खाना पकाना शुरू करने के लिए ढक्कन बंद करें।"
            )
        return (
            "Close the lid to continue cooking.",
            "खाना पकाना जारी रखने के लिए ढक्कन बंद करें।"
        )

    return '', ''


def _build_prerequisite_pair(recipe_data):
    """Return the bilingual setup prerequisite, if this recipe has one.

    Prerequisites are deliberately NOT part of numbered Recipe Steps.
    Examples:
      - Fit the Noodles Stirrer before cooking.
      - Use the Pressure Cooker as the cooking vessel.
      - Set up Grill Mesh / Cake Mold / Stirrer.

    This keeps Recipe Step numbering aligned with the actual cooking timeline.
    """
    return _build_accessory_setup_pair(recipe_data)


def _build_manual_addition_events(recipe_data):
    """Generate only NUMBERED food/ingredient customer actions.

    Accessory/setup actions are handled separately as a Prerequisite and are
    never counted as Step 1.

    Numbered events are:
      - the initial food loading action
      - later food additions ONLY after a timed step whose skip is false/blank

    Plain pump Water is automatic. Named/special water remains manual.
    """
    steps = _disclaimer_source_steps(recipe_data)
    if not steps:
        return []

    events = []

    vessel_en, vessel_hi = _manual_target_vessel(recipe_data)

    # ---------------- INITIAL LOAD ----------------
    initial_en = []
    initial_hi = []
    first_timed_index = None
    initial_lid = ''

    for idx, step in enumerate(steps):
        item = _manual_ingredient_item(step, recipe_data)
        if item:
            initial_en.append(item[0])
            initial_hi.append(item[1])

        if safe_int(step.get('durationInSec', 0)) > 0:
            first_timed_index = idx
            initial_lid = step.get('lid', '')
            break

    if first_timed_index is None:
        first_timed_index = len(steps) - 1
        if steps:
            initial_lid = steps[first_timed_index].get('lid', '')

    if initial_en:
        en = f"Add {_join_english_items(initial_en)} to the {vessel_en}."
        hi = f"{vessel_hi} में {_join_hindi_items(initial_hi)} डालें।"

        lid_en, lid_hi = _lid_instruction_pair(initial_lid, initial=True)
        if lid_en:
            en += f" {lid_en}"
        if lid_hi:
            hi += f" {lid_hi}"
        events.append((en, hi))

    # ---------------- PAUSE / ADD EVENTS ----------------
    timed_indices = [
        i for i, step in enumerate(steps)
        if safe_int(step.get('durationInSec', 0)) > 0
    ]

    for pos, current_index in enumerate(timed_indices):
        current_step = steps[current_index]

        # skip=True => device continues automatically.
        if _skip_is_true(current_step):
            continue

        # Need a later timed stage to prepare for.
        if pos + 1 >= len(timed_indices):
            continue

        next_timed_index = timed_indices[pos + 1]
        next_lid = steps[next_timed_index].get('lid', '')

        items_en = []
        items_hi = []

        current_stage_key = _normalize_manual_name(current_step.get('Text', ''))

        for target_index in range(current_index + 1, next_timed_index + 1):
            target_step = steps[target_index]
            target_stage_key = _normalize_manual_name(target_step.get('Text', ''))

            # Example: APPLE CINNAMON CAKE has consecutive timed "Batter"
            # rows. The first skip=false boundary does NOT mean the customer
            # should add the same Batter again; the later Batter rows are
            # automatic continuation stages.
            if (
                target_index == next_timed_index
                and current_stage_key
                and target_stage_key == current_stage_key
            ):
                continue

            item = _manual_ingredient_item(target_step, recipe_data)
            if item:
                items_en.append(item[0])
                items_hi.append(item[1])

        # If the only thing in the window is Water or a machine operation,
        # there is no customer action to show.
        if not items_en:
            continue

        en = (
            f"Open the lid and add {_join_english_items(items_en)} "
            f"to the {vessel_en}."
        )
        hi = (
            f"ढक्कन खोलें और {vessel_hi} में "
            f"{_join_hindi_items(items_hi)} डालें।"
        )

        lid_en, lid_hi = _lid_instruction_pair(next_lid, initial=False)
        if lid_en:
            en += f" {lid_en}"
        if lid_hi:
            hi += f" {lid_hi}"

        events.append((en, hi))

    return events


# Context-aware wording: harmless for standard recipes, and explicitly covers
# recipes that require grill mesh / cake mold / stirrer / tray / rack etc.
GENERIC_DISCLAIMER_EN = [
    "When an accessory setup step is shown, use only the listed accessories and complete that setup before adding ingredients.",
    "Plain Water listed simply as 'Water' is dispensed automatically by the On2Cook pump from the connected water bottle. Do not add this plain Water manually.",
    "Any named or special water—such as Stock Water, Coconut Water, Rice Water, or another recipe-prepared liquid—must be added manually whenever it appears in the steps.",
    "Open the lid only for the ingredient-addition prompts shown above, then set the lid as instructed before cooking continues.",
]

GENERIC_DISCLAIMER_HI = [
    "जब एक्सेसरी सेटअप चरण दिखाया जाए, केवल सूचीबद्ध एक्सेसरीज़ का उपयोग करें और सामग्री डालने से पहले सेटअप पूरा करें।",
    "यदि सामग्री में केवल 'Water' लिखा है, तो यह सामान्य पानी On2Cook कनेक्टेड पानी की बोतल से पंप द्वारा अपने-आप डालता है। यह सामान्य पानी हाथ से न डालें।",
    "Stock Water, Coconut Water, Rice Water या किसी अन्य विशेष/रेसिपी में तैयार किए गए पानी या तरल को चरणों में दिखाए जाने पर हाथ से डालें।",
    "ढक्कन केवल ऊपर दिए गए सामग्री डालने वाले चरणों में खोलें और खाना पकना जारी होने से पहले निर्देश के अनुसार ढक्कन की स्थिति रखें।",
]


def build_step_disclaimer_lines(recipe_data):
    return [english for english, _ in _build_manual_addition_events(recipe_data)]


def build_step_disclaimer_lines_hindi(recipe_data):
    return [hindi for _, hindi in _build_manual_addition_events(recipe_data)]


def _ingredient_group_nonwater_total(ingredient):
    """Base-unit total for header Total Input, excluding only Water components."""
    title = str(ingredient.get('title', '') or '').strip()
    weight = str(ingredient.get('weight', '') or '').strip()
    components = _parse_ingredient_components(ingredient)

    has_pump_water_component = any(
        _is_automatic_pump_water(name) for name, _, _ in components
    )

    if _is_automatic_pump_water(title) or has_pump_water_component:
        total = 0.0
        found_manual_component = False
        for name, _, base in components:
            if _is_automatic_pump_water(name):
                continue
            if base is not None:
                total += base
                found_manual_component = True

        # Plain Water -> zero. Named/special waters remain included.
        return total if found_manual_component else 0.0

    _, _, base = _parse_amount(weight)
    return base or 0.0

class RecipePDFGenerator:
    def __init__(self, qr_image=None):
        # Page width updated to 210mm
        self.page_width = 210 * mm
        # Page height will be calculated dynamically
        self.page_height = None  # Will be set in generate_pdf
        
        # Left and right section widths updated to 105mm
        self.left_section_width = 105 * mm
        self.right_section_width = 105 * mm
        self.left_margin = 0 * mm
        
        # Image dimensions updated to 105mm x 97mm
        self.image_width = 105 * mm
        self.image_height = 97 * mm
        
        # Colors
        self.bar_orange = HexColor('#fbc0a7')  # Updated orange color
        self.bar_red = HexColor('#e89ca5')     # Updated red color
        self.bar_gray = HexColor('#E8E8E8')
        self.orange_color = HexColor('#F37029')  # Induction
        self.red_color = HexColor('#BE1E2D')     # Microwave/Magnetron
        self.blue_color = HexColor('#add8e6')    # Blue color for pump-on periods
        self.green_color = HexColor('#3CB371')   # Green color for regular periods
        self.skin_color = HexColor('#FDF4CB')
        self.light_gray = HexColor('#E8E8E8')
        self.dark_gray = HexColor('#333333')      # darker secondary copy on marble
        self.body_text = HexColor('#171717')       # primary body text
        self.body_muted = HexColor('#343434')      # sub-ingredient / supporting copy
        self.green_time = HexColor('#B9CC32')
        self.gold_step = HexColor('#FFD700')
        self.line_color = HexColor('#ff0000')    # Brand red for underlines

        # ---- New design palette (matches the "Aloo Matar Rassa" reference card) ----
        self.page_bg = HexColor('#F7F7F4')       # marble base
        self.cream_bg = HexColor('#FBFAF6')      # soft warm overlay
        self.card_bg = HexColor('#FBF8EE')       # translucent-looking warm card
        self.tan_pill = HexColor('#EFE6D2')      # legacy color
        self.total_output_bg = HexColor('#BE1E2D') # requested Total Output background
        self.title_green = HexColor('#123B2A')   # deeper green for strong contrast on marble
        self.subtitle_brown = HexColor('#5A3925') # retained for compatibility; subtitle is no longer drawn
        self.icon_green = HexColor('#557648')     # icon badge circles
        self.divider_green = HexColor('#6F8E5D')  # stronger divider on marble
        
        # Circle diameter for induction/magnetron indicators: 18.459mm
        self.circle_diameter = 18.459 * mm
        
        # Font sizes
        self.recipe_name_size = 22  # Updated to 22pt
        self.section_title_size = 11  # Updated to 11pt
        self.section_detail_size = 10
        self.step_title_size = 15
        self.instruction_size = 10  # Updated to 10pt
        
        # QR image (PIL Image) provided externally
        self.qr_image = qr_image  # if provided, will be drawn on PDF
        
        # Setup fonts
        self.setup_fonts()
        self.setup_hindi_fonts()
    
    def setup_fonts(self):
        """Setup custom fonts from fonts folder"""
        try:
            # Get the directory where this script is located
            script_dir = os.path.dirname(os.path.abspath(__file__))
            
            # Build font paths relative to the script directory
            montserrat_path = os.path.join(script_dir, 'Fonts', 'Montserrat-Medium.ttf')
            din_bold_path = os.path.join(script_dir, 'Fonts', 'DINBold.ttf')
            din_medium_path = os.path.join(script_dir, 'Fonts', 'DINMedium.ttf')
            
            print(f"🔍 Font paths:")
            print(f"   Montserrat: {montserrat_path}")
            print(f"   DIN-Bold: {din_bold_path}")
            print(f"   DIN-Medium: {din_medium_path}")
            
            # Check if font files exist
            if not os.path.exists(montserrat_path):
                print(f"❌ Font file not found: {montserrat_path}")
            if not os.path.exists(din_bold_path):
                print(f"❌ Font file not found: {din_bold_path}")
            if not os.path.exists(din_medium_path):
                print(f"❌ Font file not found: {din_medium_path}")
            
            pdfmetrics.registerFont(TTFont('Montserrat-Medium', montserrat_path))
            pdfmetrics.registerFont(TTFont('DIN-Bold', din_bold_path))
            pdfmetrics.registerFont(TTFont('DIN-Medium', din_medium_path))
            
            self.recipe_name_font = 'Montserrat-Medium'
            self.section_title_font = 'DIN-Bold'
            self.section_detail_font = 'DIN-Medium'
            self.step_title_font = 'DIN-Bold'
            self.instruction_font = 'DIN-Medium'
            
            print("✅ Custom fonts loaded successfully!")
            
        except Exception as e:
            print(f"❌ Font loading error: {e}")
            print("🔄 Falling back to system fonts...")
            # Fallback to system fonts
            self.recipe_name_font = 'Helvetica-Bold'
            self.section_title_font = 'Helvetica-Bold'
            self.section_detail_font = 'Helvetica'
            self.step_title_font = 'Helvetica-Bold'
            self.instruction_font = 'Helvetica'
            print("✅ System fonts loaded as fallback")

    def setup_hindi_fonts(self):
        """Register a Sans Devanagari font with shaping enabled.

        Search order:
          1. Project Fonts/ folder (recommended)
          2. Common Linux Noto Sans Devanagari locations
          3. Windows Nirmala UI (system Devanagari sans fallback)

        The script does not bundle or copy font files.
        """
        script_dir = os.path.dirname(os.path.abspath(__file__))
        local_fonts_dir = os.path.join(script_dir, 'Fonts')
        parent_fonts_dir = os.path.abspath(os.path.join(script_dir, '..', 'Fonts'))

        regular_candidates = [
            os.path.join(local_fonts_dir, 'NotoSansDevanagari-Regular.ttf'),
            os.path.join(local_fonts_dir, 'NotoSansDevanagari.ttf'),
            os.path.join(parent_fonts_dir, 'NotoSansDevanagari-Regular.ttf'),
            os.path.join(parent_fonts_dir, 'NotoSansDevanagari.ttf'),
            '/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf',
            '/usr/share/fonts/truetype/noto/NotoSansDevanagari.ttf',
            r'C:\Windows\Fonts\Nirmala.ttf',
        ]
        bold_candidates = [
            os.path.join(local_fonts_dir, 'NotoSansDevanagari-Bold.ttf'),
            os.path.join(local_fonts_dir, 'NotoSansDevanagari.ttf'),
            os.path.join(parent_fonts_dir, 'NotoSansDevanagari-Bold.ttf'),
            os.path.join(parent_fonts_dir, 'NotoSansDevanagari.ttf'),
            '/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf',
            '/usr/share/fonts/truetype/noto/NotoSansDevanagari.ttf',
            r'C:\Windows\Fonts\NirmalaB.ttf',
        ]

        regular_path = next((p for p in regular_candidates if os.path.exists(p)), None)
        bold_path = next((p for p in bold_candidates if os.path.exists(p)), None)

        if not regular_path:
            raise RuntimeError(
                "No Devanagari sans font found. Put NotoSansDevanagari-Regular.ttf "
                "inside the project's Fonts folder."
            )

        if not bold_path:
            bold_path = regular_path

        # ReportLab 4.4+ supports HarfBuzz shaping when shapable=True and
        # drawString(..., shaping=True) is used.
        pdfmetrics.registerFont(
            TTFont('HindiSans', regular_path, shapable=True)
        )
        pdfmetrics.registerFont(
            TTFont('HindiSans-Bold', bold_path, shapable=True)
        )

        self.hindi_font = 'HindiSans'
        self.hindi_bold_font = 'HindiSans-Bold'

        print("✅ Devanagari font loaded:")
        print(f"   Regular: {regular_path}")
        print(f"   Bold:    {bold_path}")


    # -----------------------------------------------------------
    # MARBLE BACKGROUND + TYPOGRAPHY GUARDRAILS
    # -----------------------------------------------------------

    def draw_marble_background(self, c, x, y, width, height):
        """Draw the EXACT supplied marble image as the background.

        The source image is never regenerated. It is tiled vertically at the
        page width so its proportions are preserved as much as possible on
        dynamically tall recipe pages.

        Expected asset:
            marble_background.png
        placed in the same folder as this Python script.

        If the image is missing, the function falls back to a clean light base
        instead of crashing PDF generation.
        """
        c.saveState()

        # Hard clipping means the background can never paint outside its region.
        clip = c.beginPath()
        clip.rect(x, y, width, height)
        c.clipPath(clip, stroke=0, fill=0)

        # Always paint a neutral fallback under the texture.
        c.setFillColor(self.page_bg)
        c.rect(x, y, width, height, fill=1, stroke=0)

        if os.path.exists(MARBLE_BG_PATH):
            try:
                with Image.open(MARBLE_BG_PATH) as bg:
                    iw, ih = bg.size

                if iw > 0 and ih > 0:
                    # Preserve the uploaded image's aspect ratio at full page width.
                    tile_h = width * (ih / iw)
                    # Slight overlap avoids hairline seams between repeated tiles.
                    overlap = 0.5 * mm
                    tile_bottom = y
                    while tile_bottom < y + height:
                        c.drawImage(
                            MARBLE_BG_PATH,
                            x,
                            tile_bottom,
                            width=width,
                            height=tile_h + overlap,
                            preserveAspectRatio=False,
                            anchor='c',
                            mask='auto'
                        )
                        tile_bottom += max(1, tile_h - overlap)

                    c.restoreState()
                    return
            except Exception as e:
                print(f"⚠️ Marble background image could not be drawn ({e}); using plain fallback.")

        c.restoreState()

    def draw_soft_card(self, c, x, y, width, height, radius=4*mm):
        """Transparent body card: marble stays fully visible; only a subtle outline remains."""
        c.saveState()
        c.setStrokeColor(HexColor('#D8D5CC'))
        c.setLineWidth(0.28)
        c.roundRect(x, y, width, height, radius, stroke=1, fill=0)
        c.restoreState()

    def _ellipsize_to_width(self, text, font_name, font_size, max_width):
        """Return a single line guaranteed not to exceed max_width."""
        text = str(text or '').strip()
        if not text:
            return ''
        if pdfmetrics.stringWidth(text, font_name, font_size) <= max_width:
            return text
        suffix = "..."
        available = max(1, max_width - pdfmetrics.stringWidth(suffix, font_name, font_size))
        out = ''
        for ch in text:
            candidate = out + ch
            if pdfmetrics.stringWidth(candidate, font_name, font_size) > available:
                break
            out = candidate
        return (out.rstrip() + suffix) if out else suffix

    def _break_long_token(self, token, font_name, font_size, max_width):
        """Split a single unbroken token so it can never cross a text box."""
        pieces = []
        current = ''
        for ch in str(token):
            candidate = current + ch
            if current and pdfmetrics.stringWidth(candidate, font_name, font_size) > max_width:
                pieces.append(current)
                current = ch
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces or ['']

    def wrap_text_to_width(self, text, max_width, font_name, font_size, max_lines=None):
        """Width-accurate wrapping using ReportLab font metrics.

        - respects explicit newlines
        - breaks extremely long words/tokens
        - optionally caps line count and adds an ellipsis
        """
        text = '' if text is None else str(text)
        all_lines = []

        paragraphs = text.splitlines() or ['']
        for para in paragraphs:
            words = para.split()
            if not words:
                if all_lines:
                    all_lines.append('')
                continue

            current = ''
            for word in words:
                # Force-split a token that is itself wider than the box.
                token_parts = (
                    self._break_long_token(word, font_name, font_size, max_width)
                    if pdfmetrics.stringWidth(word, font_name, font_size) > max_width
                    else [word]
                )

                for part_index, part in enumerate(token_parts):
                    candidate = part if not current else current + ' ' + part
                    if pdfmetrics.stringWidth(candidate, font_name, font_size) <= max_width:
                        current = candidate
                    else:
                        if current:
                            all_lines.append(current)
                        current = part

                    # If a token was force-split, commit all non-final pieces immediately.
                    if len(token_parts) > 1 and part_index < len(token_parts) - 1:
                        if current:
                            all_lines.append(current)
                            current = ''

            if current:
                all_lines.append(current)

        if not all_lines:
            all_lines = ['']

        if max_lines is not None and len(all_lines) > max_lines:
            all_lines = all_lines[:max_lines]
            all_lines[-1] = self._ellipsize_to_width(
                all_lines[-1] + '...', font_name, font_size, max_width
            )

        return all_lines

    def fit_single_line_font_size(self, text, font_name, preferred_size, max_width, min_size=6):
        """Shrink a single-line string until it fits its guardrail width."""
        size = float(preferred_size)
        while size > min_size and pdfmetrics.stringWidth(str(text), font_name, size) > max_width:
            size -= 0.5
        return max(float(min_size), size)

    def draw_single_line_fit(self, c, text, x, y, width, font_name, font_size,
                             min_size=6, fill_color=black, align='left', shaping=False):
        """Draw one line inside a fixed-width box, shrinking/ellipsizing if needed."""
        text = '' if text is None else str(text)
        size = self.fit_single_line_font_size(text, font_name, font_size, width, min_size)
        safe_text = self._ellipsize_to_width(text, font_name, size, width)

        c.saveState()
        clip = c.beginPath()
        clip.rect(x, y - size * 0.35, width, size * 1.35)
        c.clipPath(clip, stroke=0, fill=0)

        c.setFillColor(fill_color)
        c.setFont(font_name, size)
        if align == 'center':
            c.drawCentredString(x + width / 2, y, safe_text, shaping=shaping)
        elif align == 'right':
            c.drawRightString(x + width, y, safe_text, shaping=shaping)
        else:
            c.drawString(x, y, safe_text, shaping=shaping)
        c.restoreState()
        return size

    def draw_wrapped_text_box(self, c, text, x, top_y, width, height, font_name,
                              font_size, min_size=6, fill_color=black,
                              line_gap=1.18, max_lines=None, shaping=False):
        """Draw wrapped text inside a hard rectangular guardrail.

        Font size is reduced only when necessary. If content still cannot fit,
        the last visible line is ellipsized. The canvas is clipped as a final
        safety net, so text can never bleed outside its box.
        """
        size = float(font_size)
        chosen_lines = None
        chosen_line_h = None
        capacity = 1

        while size >= min_size:
            line_h = size * line_gap
            capacity = max(1, int(height // line_h))
            if max_lines is not None:
                capacity = min(capacity, max_lines)
            lines = self.wrap_text_to_width(text, width, font_name, size)
            if len(lines) <= capacity:
                chosen_lines = lines
                chosen_line_h = line_h
                break
            size -= 0.5

        if chosen_lines is None:
            size = float(min_size)
            chosen_line_h = size * line_gap
            capacity = max(1, int(height // chosen_line_h))
            if max_lines is not None:
                capacity = min(capacity, max_lines)
            chosen_lines = self.wrap_text_to_width(
                text, width, font_name, size, max_lines=capacity
            )
        elif len(chosen_lines) > capacity:
            chosen_lines = chosen_lines[:capacity]

        if len(chosen_lines) == capacity:
            full_lines = self.wrap_text_to_width(text, width, font_name, size)
            if len(full_lines) > capacity:
                chosen_lines[-1] = self._ellipsize_to_width(
                    chosen_lines[-1] + '...', font_name, size, width
                )

        c.saveState()
        clip = c.beginPath()
        clip.rect(x, top_y - height, width, height)
        c.clipPath(clip, stroke=0, fill=0)
        c.setFillColor(fill_color)
        c.setFont(font_name, size)

        baseline = top_y - size
        for line in chosen_lines:
            c.drawString(x, baseline, line, shaping=shaping)
            baseline -= chosen_line_h
        c.restoreState()

        return baseline, len(chosen_lines), size

    def extract_on2cook_total_label(self, recipe_data):
        """Header-friendly total On2Cook time only (no Python list repr)."""
        total_sec = sum(safe_int(i.get("durationInSec", 0)) for i in recipe_data.get("Instruction", []))
        mins, secs = divmod(total_sec, 60)
        if mins == 1 and secs == 0:
            return "1:00 min"
        return f"{mins}:{secs:02d} mins"


    # -----------------------------------------------------------
    # NEW: ingredient image + disclaimer sizing/drawing helpers
    # -----------------------------------------------------------

    def get_ingredient_image_height(self, ingredient_image_path):
        """Calculate image height when scaled proportionally to the full PDF width."""
        if not ingredient_image_path:
            return 0

        try:
            with Image.open(ingredient_image_path) as im:
                w, h = im.size

            if w <= 0 or h <= 0:
                return 0

            # Scale proportionally so the image is exactly the full PDF width.
            return self.page_width * (h / w)

        except Exception as e:
            print(f"⚠️ Could not read ingredient image for sizing ({e}); using default height.")
            return 0

    def get_disclaimer_lines_and_height(self, recipe_data):
        """Build bilingual manual Recipe Steps and reserve space for intro + steps +
        water/lid note + outro.

        Layout:
          LEFT  : English heading -> intro -> numbered steps -> generic disclaimer -> closing line
          RIGHT : Hindi heading   -> intro -> numbered steps -> generic disclaimer -> closing line

        Returns:
            (english_wrapped_steps,
             hindi_wrapped_steps,
             prerequisite_english_lines,
             prerequisite_hindi_lines,
             total_height)
        """
        english_raw = build_step_disclaimer_lines(recipe_data)
        hindi_raw = build_step_disclaimer_lines_hindi(recipe_data)

        prerequisite_pair = _build_prerequisite_pair(recipe_data)
        prerequisite_en_raw = prerequisite_pair[0] if prerequisite_pair else ''
        prerequisite_hi_raw = prerequisite_pair[1] if prerequisite_pair else ''

        # Do NOT suppress the entire Recipe Steps / Disclaimer block merely
        # because no numbered manual action was generated. The generic safety
        # / pump-water disclaimer and final serving instruction still apply.
        margin = 8 * mm
        column_gap = 10 * mm
        column_width = (self.page_width - 2 * margin - column_gap) / 2
        column_width_mm = column_width / mm

        # --- Optional prerequisite (NOT numbered) ---
        prerequisite_english = []
        if prerequisite_en_raw:
            prerequisite_english = self.wrap_text_for_instruction(
                f"Prerequisite: {prerequisite_en_raw}",
                column_width_mm,
                self.instruction_font,
                9
            )

        prerequisite_hindi = []
        if prerequisite_hi_raw:
            prerequisite_hindi = self.wrap_text_for_instruction(
                f"पूर्व तैयारी: {prerequisite_hi_raw}",
                column_width_mm,
                self.hindi_font,
                9.5
            )

        # --- Dynamic numbered recipe steps ---
        english_wrapped = []
        for i, line in enumerate(english_raw, 1):
            numbered = f"Step {i}: {line}"
            english_wrapped.extend(
                self.wrap_text_for_instruction(
                    numbered, column_width_mm,
                    self.instruction_font, 9
                )
            )

        hindi_wrapped = []
        for i, line in enumerate(hindi_raw, 1):
            numbered = f"चरण {i}: {line}"
            hindi_wrapped.extend(
                self.wrap_text_for_instruction(
                    numbered, column_width_mm,
                    self.hindi_font, 9.5
                )
            )

        # --- Fixed intro and closing guidance ---
        english_intro = self.wrap_text_for_instruction(
            RECIPE_STEPS_INTRO_EN,
            column_width_mm,
            self.instruction_font,
            9
        )
        hindi_intro = self.wrap_text_for_instruction(
            RECIPE_STEPS_INTRO_HI,
            column_width_mm,
            self.hindi_font,
            9.5
        )
        english_outro = self.wrap_text_for_instruction(
            RECIPE_STEPS_OUTRO_EN,
            column_width_mm,
            self.instruction_font,
            9
        )
        hindi_outro = self.wrap_text_for_instruction(
            RECIPE_STEPS_OUTRO_HI,
            column_width_mm,
            self.hindi_font,
            9.5
        )

        # --- Fixed bilingual disclaimer heading + note text (not derived from Instruction) ---
        english_disclaimer_label = self.wrap_text_for_instruction(
            "Disclaimer", column_width_mm, self.instruction_font, 9
        )
        hindi_disclaimer_label = self.wrap_text_for_instruction(
            "अस्वीकरण", column_width_mm, self.hindi_font, 9.5
        )

        english_generic = []
        for line in GENERIC_DISCLAIMER_EN:
            english_generic.extend(
                self.wrap_text_for_instruction(
                    line, column_width_mm, self.instruction_font, 9
                )
            )
        hindi_generic = []
        for line in GENERIC_DISCLAIMER_HI:
            hindi_generic.extend(
                self.wrap_text_for_instruction(
                    line, column_width_mm, self.hindi_font, 9.5
                )
            )

        # Spacing mirrors draw_disclaimer_block().
        heading_height = 16 * mm
        english_line_h = 4.7 * mm
        hindi_line_h = 5.2 * mm
        intro_bottom_gap = 2.8 * mm
        steps_bottom_gap = 3.0 * mm
        bottom_padding = 6 * mm

        prerequisite_bottom_gap = 2.8 * mm

        english_height = (
            len(english_intro) * english_line_h
            + intro_bottom_gap
            + len(prerequisite_english) * english_line_h
            + (prerequisite_bottom_gap if prerequisite_english else 0)
            + len(english_wrapped) * english_line_h
            + steps_bottom_gap
            + len(english_outro) * english_line_h
            + steps_bottom_gap
            + len(english_disclaimer_label) * english_line_h
            + steps_bottom_gap
            + len(english_generic) * english_line_h
        )
        hindi_height = (
            len(hindi_intro) * hindi_line_h
            + intro_bottom_gap
            + len(prerequisite_hindi) * hindi_line_h
            + (prerequisite_bottom_gap if prerequisite_hindi else 0)
            + len(hindi_wrapped) * hindi_line_h
            + steps_bottom_gap
            + len(hindi_outro) * hindi_line_h
            + steps_bottom_gap
            + len(hindi_disclaimer_label) * hindi_line_h
            + steps_bottom_gap
            + len(hindi_generic) * hindi_line_h
        )

        total_height = heading_height + max(english_height, hindi_height) + bottom_padding

        return (
            english_wrapped,
            hindi_wrapped,
            prerequisite_english,
            prerequisite_hindi,
            total_height
        )

    def draw_ingredient_image_block(self, c, ingredient_image_path, block_top_y, block_height):
        """Draw ingredient image at the full PDF width, preserving its aspect ratio."""
        if not ingredient_image_path or block_height <= 0:
            return

        try:
            c.drawImage(
                ingredient_image_path,
                0,
                block_top_y - block_height,
                width=self.page_width,
                height=block_height,
                preserveAspectRatio=False,
                anchor='c',
                mask='auto'
            )

            print(
                f"✅ Ingredient image drawn full width "
                f"({self.page_width/mm:.0f}mm × {block_height/mm:.0f}mm)"
            )

        except Exception as e:
            print(f"❌ Error drawing ingredient image: {e}")

    def draw_disclaimer_block(
        self,
        c,
        english_lines,
        hindi_lines,
        prerequisite_english,
        prerequisite_hindi,
        block_height
    ):
        """Bilingual Recipe Steps block.

        Order in each language:
          Intro
          Prerequisite (if any; NOT numbered)
          Step 1, Step 2, ...
          Disclaimer
          Final serve instruction
        """
        if block_height <= 0:
            return

        margin = 8 * mm
        column_gap = 10 * mm
        column_width = (self.page_width - 2 * margin - column_gap) / 2
        column_width_mm = column_width / mm
        left_x = margin
        right_x = margin + column_width + column_gap

        c.saveState()

        heading_y = block_height - 8 * mm

        # LEFT heading — English.
        self.draw_single_line_fit(
            c, "Recipe Steps",
            left_x, heading_y, column_width,
            self.section_title_font, 11,
            min_size=8, fill_color=self.title_green
        )

        # RIGHT heading — Hindi.
        self.draw_single_line_fit(
            c, "पकाने के चरण",
            right_x, heading_y, column_width,
            self.hindi_bold_font, 12,
            min_size=9, fill_color=self.title_green,
            shaping=True
        )

        # Separate underline for each language column.
        c.setLineWidth(0.3 * mm)
        c.setStrokeColor(self.line_color)
        c.line(left_x, heading_y - 2 * mm, left_x + column_width, heading_y - 2 * mm)
        c.line(right_x, heading_y - 2 * mm, right_x + column_width, heading_y - 2 * mm)

        # Thin central separator.
        separator_x = margin + column_width + column_gap / 2
        c.setStrokeColor(HexColor('#D5D2CB'))
        c.setLineWidth(0.2 * mm)
        c.line(
            separator_x,
            4 * mm,
            separator_x,
            heading_y + 1 * mm
        )

        en_line_h = 4.7 * mm
        hi_line_h = 5.2 * mm
        intro_bottom_gap = 2.8 * mm
        steps_bottom_gap = 3.0 * mm

        # ===========================================================
        # ENGLISH — LEFT
        # ===========================================================
        y_en = heading_y - 8 * mm

        # Intro line before Step 1.
        english_intro_lines = self.wrap_text_for_instruction(
            RECIPE_STEPS_INTRO_EN,
            column_width_mm,
            self.instruction_font,
            9
        )
        for line in english_intro_lines:
            if y_en < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                left_x, y_en, column_width,
                self.instruction_font, 9,
                min_size=7, fill_color=self.body_text
            )
            y_en -= en_line_h

        y_en -= intro_bottom_gap

        # Prerequisite is visually separate and NEVER numbered.
        for line in prerequisite_english:
            if y_en < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                left_x, y_en, column_width,
                self.instruction_font, 9,
                min_size=7, fill_color=self.title_green
            )
            y_en -= en_line_h

        if prerequisite_english:
            y_en -= 2.8 * mm

        # Numbered English Recipe Steps begin with the first food/cooking stage.
        for line in english_lines:
            if y_en < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                left_x, y_en, column_width,
                self.instruction_font, 9,
                min_size=7, fill_color=self.body_text
            )
            y_en -= en_line_h

        y_en -= steps_bottom_gap

        # Important disclaimer immediately after the manual steps.
        for wrapped in self.wrap_text_for_instruction(
            "Disclaimer", column_width_mm, self.instruction_font, 9
        ):
            if y_en < 4 * mm:
                break
            self.draw_single_line_fit(
                c, wrapped,
                left_x, y_en, column_width,
                self.instruction_font, 9,
                min_size=7, fill_color=HexColor('#BE1E2D')
            )
            y_en -= en_line_h

        for line in GENERIC_DISCLAIMER_EN:
            for wrapped in self.wrap_text_for_instruction(
                line, column_width_mm, self.instruction_font, 9
            ):
                if y_en < 4 * mm:
                    break
                self.draw_single_line_fit(
                    c, wrapped,
                    left_x, y_en, column_width,
                    self.instruction_font, 9,
                    min_size=7, fill_color=HexColor('#BE1E2D')
                )
                y_en -= en_line_h

        # Final serve instruction comes AFTER the disclaimer.
        english_outro_lines = self.wrap_text_for_instruction(
            RECIPE_STEPS_OUTRO_EN,
            column_width_mm,
            self.instruction_font,
            9
        )
        for line in english_outro_lines:
            if y_en < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                left_x, y_en, column_width,
                self.instruction_font, 9,
                min_size=7, fill_color=self.body_text
            )
            y_en -= en_line_h

        y_en -= steps_bottom_gap

        # ===========================================================
        # HINDI — RIGHT
        # ===========================================================
        y_hi = heading_y - 8 * mm

        # Intro line before चरण 1.
        hindi_intro_lines = self.wrap_text_for_instruction(
            RECIPE_STEPS_INTRO_HI,
            column_width_mm,
            self.hindi_font,
            9.5
        )
        for line in hindi_intro_lines:
            if y_hi < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                right_x, y_hi, column_width,
                self.hindi_font, 9.5,
                min_size=7.5, fill_color=self.body_text,
                shaping=True
            )
            y_hi -= hi_line_h

        y_hi -= intro_bottom_gap

        # Hindi prerequisite is separate and NEVER numbered.
        for line in prerequisite_hindi:
            if y_hi < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                right_x, y_hi, column_width,
                self.hindi_font, 9.5,
                min_size=7.5, fill_color=self.title_green,
                shaping=True
            )
            y_hi -= hi_line_h

        if prerequisite_hindi:
            y_hi -= 2.8 * mm

        # Numbered Hindi Recipe Steps begin with the first food/cooking stage.
        for line in hindi_lines:
            if y_hi < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                right_x, y_hi, column_width,
                self.hindi_font, 9.5,
                min_size=7.5, fill_color=self.body_text,
                shaping=True
            )
            y_hi -= hi_line_h

        y_hi -= steps_bottom_gap

        # Important Hindi disclaimer immediately after the manual steps.
        for wrapped in self.wrap_text_for_instruction(
            "अस्वीकरण", column_width_mm, self.hindi_font, 9.5
        ):
            if y_hi < 4 * mm:
                break
            self.draw_single_line_fit(
                c, wrapped,
                right_x, y_hi, column_width,
                self.hindi_font, 9.5,
                min_size=7.5, fill_color=HexColor('#BE1E2D'),
                shaping=True
            )
            y_hi -= hi_line_h

        for line in GENERIC_DISCLAIMER_HI:
            for wrapped in self.wrap_text_for_instruction(
                line, column_width_mm, self.hindi_font, 9.5
            ):
                if y_hi < 4 * mm:
                    break
                self.draw_single_line_fit(
                    c, wrapped,
                    right_x, y_hi, column_width,
                    self.hindi_font, 9.5,
                    min_size=7.5, fill_color=HexColor('#BE1E2D'),
                    shaping=True
                )
                y_hi -= hi_line_h

        # Final Hindi serve instruction comes AFTER the disclaimer.
        hindi_outro_lines = self.wrap_text_for_instruction(
            RECIPE_STEPS_OUTRO_HI,
            column_width_mm,
            self.hindi_font,
            9.5
        )
        for line in hindi_outro_lines:
            if y_hi < 4 * mm:
                break
            self.draw_single_line_fit(
                c, line,
                right_x, y_hi, column_width,
                self.hindi_font, 9.5,
                min_size=7.5, fill_color=self.body_text,
                shaping=True
            )
            y_hi -= hi_line_h

        y_hi -= steps_bottom_gap

        c.restoreState()
        print(
            f"✅ Bilingual disclaimer drawn with intro/generic/outro "
            f"(English {len(english_lines)} step-line(s), "
            f"Hindi {len(hindi_lines)} step-line(s))"
        )

    def calculate_required_page_height(self, recipe_data, seconds_per_bar):
        """Calculate the required page height based on recipe content"""
        # Calculate left section height
        left_height = self.calculate_left_section_height(recipe_data)   
        
        # Calculate right section height
        right_height = self.calculate_right_section_height(recipe_data, seconds_per_bar)
        
        # Take the maximum and add some padding
        required_height = max(left_height, right_height) + 20*mm  # 20mm padding
        
        # Minimum height should be at least the original size
        min_height = 150*mm
        
        return max(required_height, min_height)

    def calculate_left_section_height(self, recipe_data):
        """Calculate body-left card height using the same wrapping rules used to draw it."""
        text_margin = 8 * mm
        inner_width = self.left_section_width - 2 * text_margin
        name_x_offset = 20 * mm
        name_width = inner_width - name_x_offset

        height = 10 * mm
        height += 8 * mm  # Cooking Time header

        # Cooking time lines
        for time_line in self.extract_cooking_time(recipe_data):
            parts = [p.strip() for p in time_line.split('    ') if p.strip()]
            for part in parts:
                lines = self.wrap_text_to_width(
                    part, inner_width, self.section_detail_font, self.section_detail_size
                )
                height += max(1, len(lines)) * 5 * mm

        height += 2 * mm
        height += 6 * mm  # Accessories heading

        accessories = self.extract_accessories(recipe_data)
        acc_text = ', '.join(accessories) if accessories else 'N/A'
        acc_lines = self.wrap_text_to_width(
            acc_text, inner_width, self.section_detail_font, self.section_detail_size
        )
        height += max(1, len(acc_lines)) * 5 * mm

        height += 3 * mm + 9 * mm  # ingredients separator + header

        for ingredient in self.extract_ingredients(recipe_data):
            if ingredient.startswith('  '):
                lines = self.wrap_text_to_width(
                    ingredient.strip(), inner_width - 4 * mm,
                    self.section_detail_font, self.section_detail_size
                )
            elif '	' in ingredient:
                _, name = ingredient.split('	', 1)
                lines = self.wrap_text_to_width(
                    name, name_width, self.step_title_font, self.section_detail_size
                )
            else:
                lines = self.wrap_text_to_width(
                    ingredient, inner_width, self.step_title_font, self.section_detail_size
                )
            height += max(1, len(lines)) * 5 * mm

        other_essentials = self.extract_other_essentials(recipe_data)
        if other_essentials:
            height += 3 * mm + 9 * mm
            for essential in other_essentials:
                if essential.startswith('  '):
                    lines = self.wrap_text_to_width(
                        essential.strip(), name_width,
                        self.section_detail_font, 10
                    )
                elif '	' in essential:
                    _, name = essential.split('	', 1)
                    lines = self.wrap_text_to_width(
                        name, name_width, self.section_detail_font, 10
                    )
                else:
                    lines = self.wrap_text_to_width(
                        essential, inner_width, self.section_detail_font, 10
                    )
                height += max(1, len(lines)) * 5 * mm

        height += 12 * mm
        return height

    def calculate_right_section_height(self, recipe_data, seconds_per_bar):
        """Calculate enough body height for both the timeline and wrapped step copy."""
        instructions = recipe_data.get('Instruction', [])
        merged = self.merge_zero_duration_steps(instructions) if instructions else []
        consolidated = self.consolidate_timeline_steps(merged)

        total_time_sec = sum(safe_int(s.get('durationInSec', 0)) for s in merged)
        first_duration = safe_int(merged[0].get('durationInSec', 0)) if merged else 0
        extra_bars = 5 if first_duration == 0 else 0
        time_bars = self.calculate_bar_position_with_rounding(total_time_sec, seconds_per_bar)
        total_bars = time_bars + extra_bars
        timeline_depth = total_bars * (1 * mm + 1 * mm) + 8 * mm

        # Same horizontal guardrail as draw_step_blocks_with_timing().
        circle_radius = self.circle_diameter / 2
        magnetron_x = self.page_width - 15 * mm
        induction_x = magnetron_x - 23 * mm
        x_offset = self.left_section_width + self.left_margin
        text_start_x = x_offset + 6 * mm
        timeline_left_boundary = induction_x - circle_radius
        max_text_width = max(12 * mm, timeline_left_boundary - text_start_x)

        block_height = 6 * mm
        vertical_spacing = 7 * mm
        current_y = 0
        lowest_y = 0

        for i, step in enumerate(consolidated):
            step_y = current_y - (i * vertical_spacing)
            instruction_text = step.get('Text', '')
            if instruction_text:
                raw_lines = self.parse_instruction_with_weight(instruction_text, recipe_data, step)
                wrapped_lines = []
                for line in raw_lines:
                    wrapped_lines.extend(
                        self.wrap_text_to_width(
                            line, max_text_width,
                            self.section_detail_font, 9
                        )
                    )
                instruction_height = max(1, len(wrapped_lines)) * 4 * mm
                power_y = step_y - block_height / 2 - 4 * mm - instruction_height
                lowest_y = min(lowest_y, power_y - 5 * mm)
                current_y -= (block_height / 2 + 4 * mm + instruction_height + 2 * mm)
            else:
                lowest_y = min(lowest_y, step_y - block_height)
                current_y -= vertical_spacing

        step_depth = -lowest_y
        top_allowance = 30 * mm
        bottom_allowance = 16 * mm
        return max(70 * mm, top_allowance + max(step_depth, timeline_depth) + bottom_allowance)

    def merge_zero_duration_steps(self, instructions):
        if not instructions:
            return instructions
        print("=== STEP MERGING DEBUG ===")
        print("Original steps:")
        for i, step in enumerate(instructions):
            duration = safe_int(step.get('durationInSec', 0))
            text = step.get('Text', '')[:40]
            weight = step.get('Weight', '')[:20]
            print(f"  Step {i+1}: '{text}...' Weight: {weight}, Duration: {duration}s")
        merged_steps = []
        i = 0
        while i < len(instructions):
            current_step = instructions[i]
            if i == 0:
                merged_steps.append(current_step)
                print(f"✓ Keeping Step 1 (first step): '{current_step.get('Text', '')[:30]}...'")
                i += 1
                continue
            if safe_int(current_step.get('durationInSec', 0)) == 0:
                print(f"→ Found zero-duration step {i+1}: '{current_step.get('Text', '')[:30]}...'")
                zero_steps = [current_step]
                j = i + 1
                while j < len(instructions) and safe_int(instructions[j].get('durationInSec', 0)) == 0:
                    zero_steps.append(instructions[j])
                    print(f"→ Also collecting zero-duration step {j+1}: '{instructions[j].get('Text', '')[:30]}...'")
                    j += 1
                if j < len(instructions):
                    target_step = instructions[j].copy()
                    print(f"→ Merging with step {j+1}: '{target_step.get('Text', '')[:30]}...' ({target_step.get('durationInSec', 0)}s)")
                    # Collect texts with weights for zero-duration steps
                    texts = []
                    for step in zero_steps:
                        text = step.get('Text', '').strip()
                        weight = step.get('Weight', '').strip()
                        if text and weight:
                            texts.append(f"{weight} {text}")
                        elif text:
                            texts.append(text)
                    # Add target step's text and weight
                    target_text = target_step.get('Text', '').strip()
                    target_weight = target_step.get('Weight', '').strip()
                    if target_text and target_weight:
                        texts.append(f"{target_weight} {target_text}")
                    elif target_text:
                        texts.append(target_text)
                    target_step['Text'] = ', '.join(texts)
                    merged_steps.append(target_step)
                    print(f"✓ Created merged step: '{target_step['Text'][:50]}...' ({target_step.get('durationInSec', 0)}s)")
                    i = j + 1
                else:
                    merged_steps.extend(zero_steps)
                    print(f"✗ No target step found, keeping zero steps as-is")
                    i = j
            else:
                merged_steps.append(current_step)
                print(f"✓ Keeping step {i+1}: '{current_step.get('Text', '')[:30]}...' ({current_step.get('durationInSec', 0)}s)")
                i += 1
        print("\nFinal merged steps:")
        for i, step in enumerate(merged_steps):
            duration = safe_int(step.get('durationInSec', 0))
            text = step.get('Text', '')[:40]
            print(f"  Step {i+1}: '{text}...' Duration: {duration}s")
        print("=== END MERGE DEBUG ===\n")
        return merged_steps
    def process_zip_file(self, zip_path, output_pdf_path, seconds_per_bar):
        """Main function to process zip file and generate PDF"""
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            with tempfile.TemporaryDirectory() as temp_dir:
                zip_ref.extractall(temp_dir)
                
                json_file = None
                image_file = None
                
                for file in os.listdir(temp_dir):
                    if file.endswith('.txt') or file.endswith('.json'):
                        json_file = os.path.join(temp_dir, file)
                    elif file.lower().endswith(('.jpg', '.jpeg', '.png')):
                        image_file = os.path.join(temp_dir, file)
                
                if not json_file:
                    raise ValueError("No JSON/TXT file found in zip")
                
                with open(json_file, 'r', encoding='utf-8') as f:
                    recipe_data = json.load(f)
                # Keep the raw Instruction rows intact here.
                # generate_pdf() preserves them for skip/manual-addition logic
                # before applying timeline-only zero-duration merging.

                # Use recipe name from JSON for output path
                candidate_name = extract_full_recipe_name_from_description(recipe_data)
                final_output_path = _resolve_output_pdf_path(output_pdf_path, candidate_name, zip_path, recipe_data)

                # Ingredient-image lookup is keyed by the ZIP stem, not the txt's internal name.
                recipe_data['_zip_stem'] = os.path.splitext(os.path.basename(zip_path))[0]

                self.generate_pdf(recipe_data, image_file, final_output_path, seconds_per_bar)
                return final_output_path

    def process_multiple_zip_files_individually(self, zip_file_paths, output_directory, seconds_per_bar, dropbox_token=None):
        """Process multiple zip files and generate separate PDFs for each"""
        print(f"🔍 === PROCESSING {len(zip_file_paths)} ZIP FILES INDIVIDUALLY ===")
        
        # Ensure output directory exists
        os.makedirs(output_directory, exist_ok=True)
        
        results = []
        
        for i, zip_path in enumerate(zip_file_paths, 1):
            print(f"\n📦 Processing file {i}/{len(zip_file_paths)}: {os.path.basename(zip_path)}")
            
            try:
                # Generate unique output PDF path for this zip using recipe name from JSON
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    with tempfile.TemporaryDirectory() as temp_dir:
                        zip_ref.extractall(temp_dir)
                        json_file = None
                        for file in os.listdir(temp_dir):
                            if file.endswith('.txt') or file.endswith('.json'):
                                json_file = os.path.join(temp_dir, file)
                                break
                        if not json_file:
                            raise ValueError("No JSON/TXT file found in zip")
                        with open(json_file, 'r', encoding='utf-8') as f:
                            recipe_data = json.load(f)
                zip_basename = sanitize_filename(recipe_data.get('name', ['recipe'])[0])
                output_pdf_path = os.path.join(output_directory, f"{zip_basename}.pdf")
                
                # Process this individual zip file using existing method
                final_output_path = self.process_zip_file(zip_path, output_pdf_path, seconds_per_bar)
                
                # Record success
                file_size = os.path.getsize(final_output_path) if os.path.exists(final_output_path) else 0
                results.append({
                    'status': 'success',
                    'input_zip': zip_path,
                    'output_pdf': final_output_path,
                    'file_size': file_size,
                    'file_size_mb': round(file_size / (1024 * 1024), 2)
                })
                
                print(f"✅ Generated: {os.path.basename(final_output_path)} ({results[-1]['file_size_mb']} MB)")
                
            except Exception as e:
                print(f"❌ Error processing {os.path.basename(zip_path)}: {e}")
                results.append({
                    'status': 'error',
                    'input_zip': zip_path,
                    'output_pdf': None,
                    'error': str(e)
                })
        
        # Print summary
        successful = [r for r in results if r['status'] == 'success']
        failed = [r for r in results if r['status'] == 'error']
        
        print(f"\n📊 === PROCESSING SUMMARY ===")
        print(f"✅ Successful: {len(successful)}")
        print(f"❌ Failed: {len(failed)}")
        print(f"📁 Output directory: {output_directory}")
        
        if successful:
            total_size_mb = sum(r['file_size_mb'] for r in successful)
            print(f"📄 Total PDF size: {total_size_mb:.2f} MB")
            print("\n✅ Generated PDFs:")
            for result in successful:
                print(f"   • {os.path.basename(result['output_pdf'])} ({result['file_size_mb']} MB)")
        
        if failed:
            print("\n❌ Failed files:")
            for result in failed:
                print(f"   • {os.path.basename(result['input_zip'])}: {result['error']}")
        
        return results

    def process_json_and_image(self, json_path, image_path, output_pdf_path, seconds_per_bar, dropbox_token):
        """Process separate JSON and image files, upload to Dropbox, generate QR, and create PDF."""
        import json
        with open(json_path, 'r', encoding='utf-8') as f:
            recipe_data = json.load(f)
        # Dropbox upload (if token provided)
        qr_img = None
        direct_url = None
        if dropbox_token:
            try:
                from final_corrected_recipe_generator import upload_to_dropbox_and_get_direct_url, generate_qr_with_center_logo, LOGO_PATH, LOGO_RATIO, DB_DEFAULT_FOLDER
                # Upload both files as a zip to Dropbox for compatibility
                import zipfile
                import tempfile
                with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
                    with zipfile.ZipFile(temp_zip.name, 'w') as zipf:
                        zipf.write(json_path, arcname=os.path.basename(json_path))
                        zipf.write(image_path, arcname=os.path.basename(image_path))
                    direct_url = upload_to_dropbox_and_get_direct_url(temp_zip.name, dropbox_token, DB_DEFAULT_FOLDER)
                qr_img = generate_qr_with_center_logo(direct_url, LOGO_PATH, LOGO_RATIO)
            except Exception as e:
                print(f"Dropbox/QR step warning: {e}. Proceeding to generate PDF without QR.")
        self.qr_image = qr_img

        # Use recipe name from JSON
        candidate_name = extract_full_recipe_name_from_description(recipe_data)
        final_output_path = _resolve_output_pdf_path(output_pdf_path, candidate_name, json_path or image_path, recipe_data)
        self.generate_pdf(recipe_data, image_path, final_output_path, seconds_per_bar)
        return final_output_path

    def process_txt_and_image(self, txt_path, image_path, output_pdf_path, seconds_per_bar, dropbox_token):
        """Process separate TXT and image files, upload to Dropbox, generate QR, and create PDF."""
        print("🔍 === STARTING TXT AND IMAGE PROCESSING ===")
        print(f"📁 TXT file path: {txt_path}")
        print(f"🖼️ Image file path: {image_path}")
        print(f"⚙️ Seconds per bar: {seconds_per_bar}")
        print(f"🔑 Dropbox token provided: {'Yes' if dropbox_token else 'No'}")
        
        import json
        with open(txt_path, 'r', encoding='utf-8') as f:
            recipe_data = json.load(f)
        print(f" Recipe data loaded successfully. Keys: {list(recipe_data.keys())}")
        
        qr_img = None
        direct_url = None
        
        if dropbox_token:
            print("🚀 Starting Dropbox upload process...")
            try:
                from final_corrected_recipe_generator import upload_to_dropbox_and_get_direct_url, generate_qr_with_center_logo, LOGO_PATH, LOGO_RATIO, DB_DEFAULT_FOLDER
                print(f"📦 Logo path: {LOGO_PATH}")
                print(f"📏 Logo ratio: {LOGO_RATIO}")
                print(f"📁 Dropbox folder: {DB_DEFAULT_FOLDER}")
                
                import zipfile
                import tempfile
                with tempfile.NamedTemporaryFile(suffix='.zip', delete=False) as temp_zip:
                    with zipfile.ZipFile(temp_zip.name, 'w') as zipf:
                        zipf.write(txt_path, arcname=os.path.basename(txt_path))
                        zipf.write(image_path, arcname=os.path.basename(image_path))
                    print(f"📦 Temporary ZIP created: {temp_zip.name}")
                    
                    direct_url = upload_to_dropbox_and_get_direct_url(temp_zip.name, dropbox_token, DB_DEFAULT_FOLDER)
                    print(f"✅ Dropbox upload successful!")
                    print(f"🔗 Direct URL: {direct_url}")
                    
                print("🎯 Starting QR code generation...")
                qr_img = generate_qr_with_center_logo(direct_url, LOGO_PATH, LOGO_RATIO)
                print(f"✅ QR code generated successfully: {qr_img is not None}")
                if qr_img:
                    print(f"📐 QR image dimensions: {qr_img.size}")
                    print(f" QR image mode: {qr_img.mode}")
                    
            except Exception as e:
                print(f"❌ Dropbox/QR step error: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("ℹ️ No Dropbox token provided, skipping QR generation")
        
        print(f"🔍 Setting qr_image: {qr_img is not None}")
        self.qr_image = qr_img
        
        print("📄 Starting PDF generation...")
        candidate_name = extract_full_recipe_name_from_description(recipe_data)
        final_output_path = _resolve_output_pdf_path(output_pdf_path, candidate_name, txt_path or image_path, recipe_data)
        self.generate_pdf(recipe_data, image_path, final_output_path, seconds_per_bar)
        print(f"📄 Output path used: {final_output_path}")
        print("✅ TXT and image processing completed!")
        return final_output_path

    # =========================================================
    # NEW: header section (title, total input/output, cooking
    # time, accessories, photo) drawn full-width at the top of
    # the page, above the ingredient collage image.
    # =========================================================

    _WEIGHT_UNIT_TO_GRAMS = {
        'g': 1, 'gm': 1, 'gms': 1, 'kg': 1000,
        'ml': 1, 'l': 1000, 'liter': 1000, 'litre': 1000,
        'tsp': 5, 'tbsp': 15, 'cup': 240, 'pinch': 0.5,
    }

    def calculate_total_input_grams(self, recipe_data):
        """Total recipe input excluding only Water.

        For mixed Ingredients[] groups such as:
            Soaked Rice & Stock Water = 3200 g
            text: Soaked Rice 1000 g, Stock Watar 2200 g
        this returns 1000 g from that group rather than dropping all 3200 g.
        """
        total = 0.0
        for ingredient in recipe_data.get('Ingredients', []):
            total += _ingredient_group_nonwater_total(ingredient)
        return round(total)

    def format_total_input(self, recipe_data):
        total = self.calculate_total_input_grams(recipe_data)
        if total <= 0:
            return "N/A"
        return f"{total} gm"

    def extract_normal_cooking_label(self, recipe_data):
        """Short 'X minutes' label used in the header row (vs. the fuller
        On2Cook/Normal comparison line used in the body card)."""
        desc = recipe_data.get("description", "")
        total_sec = sum(int(i.get("durationInSec", 0)) for i in recipe_data.get("Instruction", []))
        match = re.search(r"NORMAL COOKING TIME\s*(\d+)\s*MINUTES", desc.upper())
        if match:
            mins = int(match.group(1))
        else:
            mins = max(1, (total_sec * 3) // 60)
        unit = "minute" if mins == 1 else "minutes"
        return f"{mins} {unit}"

    def calculate_header_height(self, recipe_data):
        """Header height for English title + Hindi subtitle + wrapped accessories."""
        height = 95 * mm
        accessories = self.extract_accessories(recipe_data)
        if accessories:
            acc_text = ', '.join(accessories)
            acc_lines = self.wrap_text_to_width(
                acc_text, 47 * mm, self.section_detail_font, 9
            )
            if len(acc_lines) > 2:
                height += min(3, len(acc_lines) - 2) * 4.5 * mm
        return height

    def draw_icon_badge(self, c, cx, cy, radius, icon='scale'):
        """Solid colored circle with either a bundled SVG icon (from
        ICONS_DIR) or a simple built-in fallback glyph drawn on top."""
        c.saveState()
        c.setFillColor(self.icon_green)
        c.setStrokeColor(self.icon_green)
        c.circle(cx, cy, radius, fill=1, stroke=0)
        svg_path = os.path.join(ICONS_DIR, f'{icon}.svg')
        drew_svg = False
        if os.path.exists(svg_path):
            try:
                drawing = svg2rlg(svg_path)
                target = radius * 1.3
                base = max(drawing.width, drawing.height, 1)
                scale = target / base
                drawing.width *= scale
                drawing.height *= scale
                drawing.scale(scale, scale)
                renderPDF.draw(drawing, c, cx - (drawing.minWidth() * scale) / 2, cy - (drawing.height) / 2)
                drew_svg = True
            except Exception as e:
                print(f"⚠️  Icon SVG '{icon}' failed to render ({e}); using fallback glyph.")
        if not drew_svg:
            self._draw_fallback_icon(c, cx, cy, radius, icon)
        c.restoreState()

    def _draw_fallback_icon(self, c, cx, cy, radius, icon):
        """Minimal hand-drawn glyphs so the header still looks reasonable
        with no icon assets in ICONS_DIR."""
        c.setStrokeColor(white)
        c.setFillColor(white)
        c.setLineWidth(0.6)
        if icon == 'clock':
            c.circle(cx, cy, radius * 0.55, fill=0, stroke=1)
            c.line(cx, cy, cx, cy + radius * 0.32)
            c.line(cx, cy, cx + radius * 0.28, cy)
        elif icon == 'scale':
            c.line(cx - radius * 0.5, cy + radius * 0.1, cx + radius * 0.5, cy + radius * 0.1)
            c.line(cx, cy + radius * 0.1, cx, cy - radius * 0.35)
            c.circle(cx - radius * 0.5, cy - radius * 0.1, radius * 0.18, fill=0, stroke=1)
            c.circle(cx + radius * 0.5, cy - radius * 0.1, radius * 0.18, fill=0, stroke=1)
        elif icon == 'bowl':
            p = c.beginPath()
            p.moveTo(cx - radius * 0.5, cy + radius * 0.05)
            p.curveTo(cx - radius * 0.5, cy - radius * 0.45, cx + radius * 0.5, cy - radius * 0.45, cx + radius * 0.5, cy + radius * 0.05)
            c.drawPath(p, stroke=1, fill=0)
            c.line(cx - radius * 0.55, cy + radius * 0.05, cx + radius * 0.55, cy + radius * 0.05)
        else:  # 'ingredients' / generic
            c.setFont('Helvetica-Bold', radius)
            c.drawCentredString(cx, cy - radius * 0.35, (icon[:1] or 'I').upper())

    def draw_rounded_image(self, c, image_path, x, y, w, h, radius):
        """Draws image_path clipped to a rounded rectangle. Falls back to
        a plain skin-colored rounded box if the image can't be loaded."""
        c.saveState()
        path = c.beginPath()
        path.moveTo(x, y + radius)
        path.arcTo(x, y, x + radius, y + radius, startAng=180, extent=90)
        path.lineTo(x + w - radius, y)
        path.arcTo(x + w - radius, y, x + w, y + radius, startAng=270, extent=90)
        path.lineTo(x + w, y + h - radius)
        path.arcTo(x + w - radius, y + h - radius, x + w, y + h, startAng=0, extent=90)
        path.lineTo(x + radius, y + h)
        path.arcTo(x, y + h - radius, x + radius, y + h, startAng=90, extent=90)
        path.close()
        c.clipPath(path, stroke=0, fill=0)
        if image_path and os.path.exists(image_path):
            try:
                c.drawImage(image_path, x, y, width=w, height=h,
                            preserveAspectRatio=True, anchor='c', mask='auto')
            except Exception as e:
                print(f"❌ Header photo draw error: {e}")
                c.setFillColor(self.skin_color)
                c.rect(x, y, w, h, fill=1, stroke=0)
        else:
            c.setFillColor(self.skin_color)
            c.rect(x, y, w, h, fill=1, stroke=0)
        c.restoreState()

    def draw_header_section(self, c, recipe_data, image_path, header_top_y, header_height):
        """Header:
        - LARGE English full recipe name from description
        - SMALL Hindi translation on the left under the English title
        - no duplicated smaller English recipe name
        """
        margin = 8 * mm
        photo_w = 76 * mm
        photo_h = header_height - 14 * mm
        photo_x = self.page_width - margin - photo_w
        photo_y = header_top_y - header_height + 7 * mm
        header_bottom = header_top_y - header_height

        self.draw_marble_background(c, 0, header_bottom, self.page_width, header_height)

        # DESCRIPTION-FIRST customer-facing name.
        full_english_name = extract_full_recipe_name_from_description(recipe_data)
        title_text = str(full_english_name).upper().strip()

        max_title_width = max(25 * mm, photo_x - margin - 7 * mm)
        title_y = header_top_y - 14 * mm

        # Main title: English only, full-width.
        self.draw_single_line_fit(
            c, title_text,
            margin, title_y, max_title_width,
            self.recipe_name_font, 27,
            min_size=13, fill_color=self.title_green
        )

        # Secondary line: HINDI ONLY, aligned to the LEFT under the English title.
        hindi_subtitle = get_hindi_recipe_name(recipe_data)
        subtitle_y = title_y - 9 * mm

        hindi_width = min(62 * mm, max_title_width * 0.58)
        hindi_x = margin

        self.draw_single_line_fit(
            c, hindi_subtitle,
            hindi_x, subtitle_y, hindi_width,
            self.hindi_bold_font, 13,
            min_size=8.5, fill_color=self.subtitle_brown,
            align='left', shaping=True
        )

        # Decorative divider remains on the left, with no duplicate English text.
        divider_y = subtitle_y - 5 * mm
        divider_w = min(62 * mm, max_title_width)
        c.setStrokeColor(self.divider_green)
        c.setLineWidth(0.65)
        c.line(margin, divider_y, margin + divider_w, divider_y)
        c.setFillColor(self.divider_green)
        c.circle(margin + divider_w / 2, divider_y, 0.72 * mm, fill=1, stroke=0)

        # ---- info columns ----
        icon_radius = 4.3 * mm
        icon_x = margin + icon_radius
        text_x = margin + 2 * icon_radius + 3 * mm

        output_x = margin + 61 * mm
        if output_x + 39 * mm > photo_x:
            output_x = max(text_x + 38 * mm, photo_x - 40 * mm)

        left_text_w = max(28 * mm, output_x - text_x - 5 * mm)
        row_y = divider_y - 11 * mm

        def draw_info_row(y, icon, label, value_text, value_max_lines=2):
            self.draw_icon_badge(c, icon_x, y, icon_radius, icon)

            self.draw_single_line_fit(
                c, label,
                text_x, y + 2.6 * mm, left_text_w,
                self.section_detail_font, 8.5,
                min_size=6.5, fill_color=self.body_text
            )

            self.draw_wrapped_text_box(
                c, value_text,
                text_x, y + 0.7 * mm,
                left_text_w, 11.8 * mm,
                self.step_title_font, 10,
                min_size=7.5,
                fill_color=self.body_text,
                line_gap=1.16,
                max_lines=value_max_lines
            )
            return y - 15.5 * mm

        next_y = draw_info_row(
            row_y, 'scale',
            'Total Input (Excl. Pump Water):',
            self.format_total_input(recipe_data), 1
        )
        next_y = draw_info_row(
            next_y, 'clock',
            'Total Cooking Time:',
            self.extract_on2cook_total_label(recipe_data), 1
        )

        accessories = self.extract_accessories(recipe_data)
        acc_text = ', '.join(accessories) if accessories else 'N/A'
        draw_info_row(next_y, 'ingredients', 'Accessories:', acc_text, 3)

        # ---- Total Output middle column ----
        output_icon_x = output_x + icon_radius
        self.draw_icon_badge(c, output_icon_x, row_y, icon_radius, 'bowl')
        output_text_x = output_icon_x + icon_radius + 3 * mm
        output_text_w = max(18 * mm, photo_x - output_text_x - 4 * mm)

        self.draw_single_line_fit(
            c, "Total Output:",
            output_text_x, row_y + 2.6 * mm, output_text_w,
            self.section_detail_font, 8.5,
            min_size=6.5, fill_color=self.body_text
        )

        # Requested #BE1E2D background behind the output WEIGHT.
        output_value = self.extract_output(recipe_data)
        output_value_y = row_y - 7.4 * mm
        output_value_h = 8.5 * mm
        c.setFillColor(self.total_output_bg)
        c.roundRect(
            output_text_x - 1.5 * mm,
            output_value_y,
            output_text_w + 3 * mm,
            output_value_h,
            output_value_h / 2,
            stroke=0, fill=1
        )
        self.draw_single_line_fit(
            c, output_value,
            output_text_x, output_value_y + 2.15 * mm, output_text_w,
            self.recipe_name_font, 13.5,
            min_size=9, fill_color=white, align='center'
        )

        self.draw_rounded_image(
            c, image_path,
            photo_x, photo_y, photo_w, photo_h, 4 * mm
        )

    def draw_total_output_pill(self, c, recipe_data):
        """Full-width cream pill with the final Total Output value, drawn
        at the very bottom of the two-column body (matches the reference
        card's closing strip)."""
        pill_h = 9 * mm
        pill_y = 2 * mm
        c.saveState()
        c.setFillColor(self.total_output_bg)
        c.roundRect(
            4 * mm, pill_y,
            self.page_width - 8 * mm, pill_h,
            pill_h / 2, stroke=0, fill=1
        )

        # Small white bowl icon treatment for contrast on #BE1E2D.
        icon_cx = 4 * mm + pill_h / 2 + 3 * mm
        icon_cy = pill_y + pill_h / 2
        icon_r = pill_h / 2 - 1 * mm
        c.setFillColor(white)
        c.circle(icon_cx, icon_cy, icon_r, fill=1, stroke=0)
        c.saveState()
        c.setStrokeColor(self.total_output_bg)
        c.setLineWidth(0.6)
        p = c.beginPath()
        p.moveTo(icon_cx - icon_r * 0.5, icon_cy + icon_r * 0.05)
        p.curveTo(
            icon_cx - icon_r * 0.5, icon_cy - icon_r * 0.45,
            icon_cx + icon_r * 0.5, icon_cy - icon_r * 0.45,
            icon_cx + icon_r * 0.5, icon_cy + icon_r * 0.05
        )
        c.drawPath(p, stroke=1, fill=0)
        c.line(
            icon_cx - icon_r * 0.55, icon_cy + icon_r * 0.05,
            icon_cx + icon_r * 0.55, icon_cy + icon_r * 0.05
        )
        c.restoreState()

        pill_text = f"Total Output: {self.extract_output(recipe_data)}"
        self.draw_single_line_fit(
            c, pill_text,
            22 * mm, pill_y + pill_h / 2 - 1.6 * mm,
            self.page_width - 44 * mm,
            self.step_title_font, 10.5,
            min_size=8, fill_color=white, align='center'
        )
        c.restoreState()

    def generate_pdf(self, recipe_data, image_path, output_path, seconds_per_bar):
        print("🔍 === STARTING PDF GENERATION ===")
        print(f"🪨 Marble background asset: {MARBLE_BG_PATH} ({'FOUND' if os.path.exists(MARBLE_BG_PATH) else 'MISSING - fallback will be used'})")
        print(f"DEBUG: Raw description before cleaning: {recipe_data.get('description', '')!r}")
        
        # Clean recipe data
        print("🔍 Cleaning recipe data formatting...")
        recipe_data = clean_recipe_data(recipe_data)
        print(f"DEBUG: Description after cleaning: {recipe_data.get('description', '')!r}")
        print("✅ Recipe data cleaned (time formats and units)")
        
        # Preserve the ORIGINAL cleaned Instruction rows for the customer-facing
        # skip/manual-addition logic BEFORE the timeline is merged.
        if 'Instruction' in recipe_data:
            recipe_data['_disclaimer_instruction_source'] = [
                dict(step) for step in recipe_data['Instruction']
            ]

            print("🔍 Manual-addition source steps preserved:")
            for i, step in enumerate(recipe_data['_disclaimer_instruction_source'], 1):
                print(
                    f"  Source Step {i}: skip={step.get('skip', '')!r}, "
                    f"duration={safe_int(step.get('durationInSec', 0))}s, "
                    f"text={step.get('Text', '')!r}"
                )

            print("🔍 Applying zero-duration merging for the cooking timeline only...")
            original_count = len(recipe_data['Instruction'])
            recipe_data['Instruction'] = self.merge_zero_duration_steps(recipe_data['Instruction'])
            merged_count = len(recipe_data['Instruction'])
            print(f"✅ Timeline merge applied: {original_count} → {merged_count} steps")

            for i, step in enumerate(recipe_data['Instruction']):
                duration = safe_int(step.get('durationInSec', 0))
                text = step.get('Text', '')[:30]
                print(f"  Merged Timeline Step {i+1}: '{text}...' Duration: {duration}s")

            manual_events = _build_manual_addition_events(recipe_data)
            print(f"✅ Customer manual-addition events: {len(manual_events)}")
            for i, (english, hindi) in enumerate(manual_events, 1):
                print(f"  Manual Step {i} EN: {english}")
                print(f"  Manual Step {i} HI: {hindi}")
        
        print(f"📏 Page width: {self.page_width/mm:.1f}mm")
        print(f"🎯 QR image status: {self.qr_image is not None}")

        # -----------------------------------------------------------
        # Figure out the header, ingredient-image, and disclaimer block
        # sizes BEFORE computing final page height, so everything fits
        # on one page with no separate render/stack/move steps.
        # -----------------------------------------------------------
        # Key = ZIP stem (set in process_zip_file). Falls back to the txt name only
        # for the non-zip entry points, where no zip stem exists.
        recipe_name_for_lookup = recipe_data.get('_zip_stem') or recipe_data.get('name', ['recipe'])[0]
        ingredient_image_path = find_ingredient_image_path(recipe_name_for_lookup)
        ingredient_img_height = self.get_ingredient_image_height(ingredient_image_path)
        (
            disclaimer_english,
            disclaimer_hindi,
            prerequisite_english,
            prerequisite_hindi,
            disclaimer_height
        ) = self.get_disclaimer_lines_and_height(recipe_data)
        header_height = self.calculate_header_height(recipe_data)

        gap = 6 * mm
        top_extra = header_height + ((ingredient_img_height + gap) if ingredient_image_path else 0)
        has_disclaimer = bool(disclaimer_english or disclaimer_hindi)
        bottom_extra = (disclaimer_height + gap) if has_disclaimer else 0

        # Original content height (Cooking Time/Ingredients card + timeline)
        content_height = self.calculate_required_page_height(recipe_data, seconds_per_bar)
        total_page_height = content_height + top_extra + bottom_extra

        print(f"📏 Calculated page height: {total_page_height/mm:.1f}mm "
              f"(header {header_height/mm:.0f}mm + content {content_height/mm:.0f}mm"
              f"{f' + image {ingredient_img_height/mm:.0f}mm' if ingredient_image_path else ''}"
              f"{f' + steps {disclaimer_height/mm:.0f}mm' if has_disclaimer else ''})")

        c = canvas.Canvas(output_path, pagesize=(self.page_width, total_page_height))

        # Full-page subtle marble background
        self.draw_marble_background(c, 0, 0, self.page_width, total_page_height)

        # Header (title, total input/output, cooking time, accessories, photo)
        self.draw_header_section(c, recipe_data, image_path, total_page_height, header_height)

        # Ingredient collage image directly below the header
        if ingredient_image_path:
            self.draw_ingredient_image_block(
                c, ingredient_image_path, total_page_height - header_height, ingredient_img_height)

        # Two-column body (Cooking Time/Ingredients card + Timeline), shifted
        # up by bottom_extra to leave room for the disclaimer block below it.
        self.page_height = content_height
        c.saveState()
        c.translate(0, bottom_extra)
        self.draw_left_section(c, recipe_data)
        self.draw_right_section(c, recipe_data, seconds_per_bar)
        self.draw_total_output_pill(c, recipe_data)
        c.restoreState()
        self.page_height = total_page_height

        # Step disclaimer in the reserved strip at the very bottom
        if has_disclaimer:
            self.draw_disclaimer_block(
                c,
                disclaimer_english,
                disclaimer_hindi,
                prerequisite_english,
                prerequisite_hindi,
                bottom_extra
            )

        c.showPage()
        c.save()
        
        print(f"📄 PDF generated successfully: {output_path}")
        print("✅ PDF generation completed!")

    def draw_vertical_center_bar_with_timing(self, c, induction_x, magnetron_x, base_y, total_bars, recipe_data):
        """Draw simple black vertical bar (no blue/green coloring)"""
        center_x = (induction_x + magnetron_x) / 2
        bar_height = 1*mm
        bar_spacing = 1*mm
        total_height = (total_bars * bar_height) + ((total_bars) * bar_spacing)
        bar_width = 4*mm
        bar_end_y = base_y - total_height
        c.setFillColor(black)
        c.setStrokeColor(black)
        c.rect(center_x - (bar_width/2), bar_end_y + 1*mm, bar_width, total_height, stroke=0, fill=1)

    def consolidate_timeline_steps(self, instructions):
        """Consolidate instructions with correct cumulative time tracking"""
        if not instructions:
            return []
        
        # SAFETY CHECK: Ensure we're working with merged instructions
        print(f"🔍 Consolidating {len(instructions)} instructions...")
        for i, instr in enumerate(instructions):
            duration = safe_int(instr.get('durationInSec', 0))
            text = instr.get('Text', '')[:30]
            print(f"  Input Step {i+1}: '{text}...' Duration: {duration}s")
        
        consolidated_steps = []
        cumulative_time = 0
        step_number = 1
        
        for i, instruction in enumerate(instructions):
            duration = safe_int(instruction.get('durationInSec', 0))
            instruction_copy = instruction.copy()
            instruction_copy['step_number'] = step_number
            instruction_copy['start_time'] = cumulative_time
            instruction_copy['duration'] = duration
            instruction_copy['combined_instructions'] = [instruction]
            instruction_copy['Induction_power'] = instruction.get('Induction_power', '0')
            instruction_copy['Magnetron_power'] = instruction.get('Magnetron_power', '0')
            consolidated_steps.append(instruction_copy)
            
            if duration > 0:
                cumulative_time += duration
            step_number += 1
        
        print(f"✅ Consolidated to {len(consolidated_steps)} steps")
        return consolidated_steps

    def wrap_text_for_instruction(self, text, max_width_mm, font_name, font_size):
        """Compatibility wrapper around the strict width-aware text guardrail."""
        lines = self.wrap_text_to_width(
            text,
            max_width_mm * mm,
            font_name,
            font_size
        )
        print(f"📝 Wrapped text: '{text}' -> {len(lines)} lines")
        return lines

    def draw_step_intersection_circles(self, c, consolidated_steps, center_x, scale_y, total_bars, extra_bars, bar_height, bar_spacing, seconds_per_bar): 
        circle_radius = 2*mm
        print(f"Drawing {len(consolidated_steps)} intersection circles")
        
        elapsed_time = 0
        for i, step in enumerate(consolidated_steps):
            step_number = i + 1
            duration = step.get('durationInSec', 0)
            
            # Special handling for first step with duration 0 - position at TOP of timeline
            if step_number == 1 and duration == 0:
                target_bar_y = scale_y + 1*mm
                print(f"Circle {step_number}: positioned at TOP of timeline Y={target_bar_y} (duration=0)")
            else:
                # NEW: Use bar-based rounding for accurate positioning
                bar_position = self.calculate_bar_position_with_rounding(elapsed_time, seconds_per_bar)
                target_bar_y = scale_y - (extra_bars * (bar_height + bar_spacing)) - (bar_position * (bar_height + bar_spacing))
                print(f"Circle {step_number}: positioned at bar {bar_position} Y={target_bar_y} (start_time={elapsed_time}s)")
            
            # Draw the circle and step number
            c.saveState()
            c.setFillColor(self.skin_color)
            c.setStrokeColor(black)
            c.setLineWidth(0.5)
            c.circle(center_x, target_bar_y, circle_radius, fill=1, stroke=1)
            c.setFillColor(black)
            c.setFont(self.instruction_font, 8)
            c.drawCentredString(center_x, target_bar_y - 0.8*mm, str(step_number))
            c.restoreState()
            
            elapsed_time += duration

    def draw_step_blocks_with_timing(self, c, x_offset, scale_y, recipe_data, total_time_sec, total_bars, extra_bars, seconds_per_bar):
        """Draw step blocks with proper timing connections and text wrapping"""
        instructions = recipe_data.get('Instruction', [])
        consolidated_steps = self.consolidate_timeline_steps(instructions)
        block_width = 17 * mm
        block_height = 6 * mm
        corner_radius = 5 * mm
        vertical_spacing = 7 * mm
        circle_radius = self.circle_diameter / 2
        magnetron_x = self.page_width - 15 * mm
        induction_x = magnetron_x - 23 * mm
        center_x = (induction_x + magnetron_x) / 2
        bar_height = 1 * mm
        bar_spacing = 1 * mm
        
        # Calculate maximum text width to avoid timeline overlap
        text_start_x = x_offset + 6 * mm
        timeline_left_boundary = induction_x - circle_radius # 10mm buffer
        max_text_width_mm = (timeline_left_boundary - text_start_x) / mm
        
        print(f"📏 Text boundary: {max_text_width_mm:.1f}mm (from {text_start_x/mm:.1f}mm to {timeline_left_boundary/mm:.1f}mm)")
        
        print(f"Total time: {total_time_sec}s, Total bars: {total_bars}, Extra bars: {extra_bars}")
        for i, step in enumerate(consolidated_steps):
            print(f"Step {i + 1}: start_time={step.get('start_time', 0)}s, duration={step.get('durationInSec', 0)}s")
        
        current_y = scale_y
        for i, step in enumerate(consolidated_steps):
            step_number = i + 1
            step_y = current_y - (i * vertical_spacing)
            
            # Draw step block
            self.draw_top_rounded_rect(c, x_offset + 4 * mm, step_y + 1*mm,
                                    block_width, block_height, corner_radius, self.skin_color)
            self.draw_single_line_fit(
                c, f"Step {step_number}",
                x_offset + 4 * mm + 2 * mm, step_y + 2 * mm,
                block_width - 4 * mm,
                self.step_title_font, 11,
                min_size=8, fill_color=self.body_text
            )
            
            # Draw duration
            duration = step.get('durationInSec', 0)
            if duration >= 0:
                mins, secs = divmod(int(duration), 60)
                # UPDATED: Apply time formatting rules
                if mins > 0:
                    if mins == 1 and secs == 0:
                        duration_text = "1:00 min"  # Remove 's' for exactly 1:00
                    else:
                        duration_text = f"{mins}:{secs:02d} mins"  # Keep 's' for other times
                else:
                    duration_text = f"0:{secs:02d} secs"  # Change sec. to secs.
                
                circle_x = x_offset + 1* mm + block_width + 15 * mm
                small_r = 1.5* mm
                c.setFillColor(self.green_time)
                c.circle(circle_x - 1*mm, step_y + 3.5* mm, small_r, fill=1, stroke=0)
                duration_x = circle_x + small_r
                duration_w = max(14 * mm, timeline_left_boundary - duration_x - 1 * mm)
                self.draw_single_line_fit(
                    c, duration_text,
                    duration_x, step_y + 2 * mm, duration_w,
                    self.section_detail_font, 11,
                    min_size=7.5, fill_color=self.body_text
                )
            
            # Handle instruction text with wrapping
            instruction_text = step.get('Text', '')
            if instruction_text:
                # Get weighted instruction lines
                lines = self.parse_instruction_with_weight(instruction_text, recipe_data, step)
                
                # Apply text wrapping to each line
                wrapped_lines = []
                for line in lines:
                    wrapped = self.wrap_text_for_instruction(
                        line, 
                        max_text_width_mm, 
                        self.section_detail_font, 
                        9
                    )
                    wrapped_lines.extend(wrapped)
                
                # Calculate space needed for wrapped text
                num_lines = len(wrapped_lines)
                line_height = 4 * mm
                instruction_height = num_lines * line_height
                
                # Draw power circles
                power_y = step_y - block_height / 2 - 4 * mm - instruction_height
                self.draw_power_circles_with_values(c, x_offset + 3 * mm, power_y - 0*mm, step)
                
                # Draw wrapped instruction text
                c.setFillColor(black)
                c.setFont(self.section_detail_font, 9)
                instruction_y = power_y + 7 * mm
                
                for j, line in enumerate(wrapped_lines):
                    c.drawString(text_start_x, instruction_y + (j * line_height), line)
                    print(f"   📝 Drew line {j+1}: '{line}' at Y={instruction_y + (j * line_height)}")
            
            # Draw connection lines
            line_start_x = x_offset - 13 * mm + block_width
            line_start_y = step_y + 1 * mm
            step_start_time = step.get('start_time', 0)
            
            c.setStrokeColor(HexColor('#ff0000'))
            c.setLineWidth(0.5)
            if step_number == 1 and step.get('durationInSec', 0) == 0:
                top_timeline_y = scale_y + 1*mm
                c.line(line_start_x, line_start_y, center_x, top_timeline_y)
                print(f"Step {step_number}: Connected to TOP of timeline at Y={top_timeline_y} (start_time={step_start_time}s)")
            else:
                bar_position = self.calculate_bar_position_with_rounding(step_start_time, seconds_per_bar)
                target_timeline_y = scale_y - (extra_bars * (bar_height + bar_spacing)) - (bar_position * (bar_height + bar_spacing))
                
                horizontal_extension = 47 * mm
                inter_x = line_start_x + (horizontal_extension + (step_number*1*mm))
                c.line(line_start_x, line_start_y, inter_x, line_start_y)
                c.line(inter_x, line_start_y, inter_x, target_timeline_y+1*mm)
                c.line(inter_x, target_timeline_y+1*mm, center_x, target_timeline_y+1*mm)
                print(f"Step {step_number}: Connected to bar {bar_position} at Y={target_timeline_y} (start_time={step_start_time}s)")
            
            # Update spacing calculation for wrapped text
            if instruction_text:
                current_y -= (block_height / 2 + 4 * mm + instruction_height + 2 * mm)
            else:
                current_y -= vertical_spacing
        
        # Draw circles and stirrer bars
        self.draw_step_intersection_circles(c, consolidated_steps, center_x, scale_y,
                                        total_bars, extra_bars, bar_height, bar_spacing, seconds_per_bar)
        self.draw_stirrer_speed_bars(c, consolidated_steps, center_x, scale_y,
                                    total_time_sec, total_bars, extra_bars, seconds_per_bar)

    def draw_timeline(self, c, x_offset, start_y, recipe_data, seconds_per_bar):
        """Draw timeline with ruler, bars, vertical bar, step blocks"""
        circle_radius = self.circle_diameter / 2
        magnetron_x = self.page_width - 15*mm
        induction_x = magnetron_x - 23*mm
        instructions = recipe_data.get('Instruction', [])
        total_time_sec = sum(int(instr.get('durationInSec', 0)) for instr in instructions)
        first_duration = safe_int(instructions[0].get('durationInSec', 0)) if instructions else 0
        extra_bars = 5 if first_duration == 0 else 0
        
        # Use rounding logic for total bars calculation
        time_bars = self.calculate_bar_position_with_rounding(total_time_sec, seconds_per_bar)
        total_bars = time_bars + extra_bars
        
        # ADD DEBUG: Verify the calculation
        print(f"DEBUG: total_time_sec={total_time_sec}, seconds_per_bar={seconds_per_bar}")
        print(f"DEBUG: time_bars={time_bars}, extra_bars={extra_bars}, total_bars={total_bars}")
        
        scale_y = start_y + 20*mm
        self.draw_ruler_ticks(c, induction_x, magnetron_x, scale_y)
        self.draw_time_based_horizontal_bars(c, induction_x, magnetron_x, scale_y, total_bars, recipe_data, extra_bars, seconds_per_bar)
        self.draw_vertical_center_bar_with_timing(c, induction_x, magnetron_x, scale_y, total_bars, recipe_data)
        self.draw_step_blocks_with_timing(c, x_offset, scale_y, recipe_data, total_time_sec, total_bars, extra_bars, seconds_per_bar)
        bar_height = 1*mm
        bar_spacing = 1*mm
        self.draw_timeline_completion_tick(c, induction_x, magnetron_x, scale_y, total_bars, bar_height, bar_spacing)

    def debug_power_values(self, recipe_data):
        instructions = recipe_data.get('Instruction', [])
        print("=== DEBUG: Power Values ===")
        for i, instr in enumerate(instructions):
            duration = instr.get('durationInSec', 0)
            induction = instr.get('Induction_power', 0)
            magnetron = instr.get('Magnetron_power', 0)
            text = instr.get('Text', '')
            print(f"Instruction {i+1}: {text[:30]}... Duration: {duration}s, Induction: {induction}, Magnetron: {magnetron}")

    def draw_colored_power_bars(self, c, induction_x, magnetron_x, base_y, recipe_data, total_bars, extra_bars, seconds_per_bar):
        """Draw horizontal bars with power coloring and blue for pump-on periods, using Induction_on_time and Magnetron_on_time separately."""
        instructions = recipe_data.get('Instruction', [])
        bar_height = 1*mm
        bar_spacing = 1*mm
        circle_radius = self.circle_diameter / 2
        line_start_x = induction_x - circle_radius
        line_end_x = magnetron_x + circle_radius
        total_width = line_end_x - line_start_x
        half_width = total_width / 2
        center_x = line_start_x + half_width
        print("=== PUMP BAR COLORING DEBUG (with separate Induction/Magnetron times) ===")
        current_time = 0
        for i, instr in enumerate(instructions):
            duration = safe_int(instr.get('durationInSec', 0))
            induction_power = safe_int(instr.get('Induction_power', 0))
            magnetron_power = safe_int(instr.get('Magnetron_power', 0))
            induction_time = safe_int(instr.get('Induction_on_time', 0))
            magnetron_time = safe_int(instr.get('Magnetron_on_time', 0))
            pump_on = safe_int(instr.get('pump_on', 0))
            print(f"Instruction {i+1}: duration={duration}s, induction_time={induction_time}s, magnetron_time={magnetron_time}s, pump_on={pump_on}s, start_time={current_time}s")
            if duration > 0:
                # Calculate bar positions for induction
                induction_start_bar = self.calculate_bar_position_with_rounding(current_time, seconds_per_bar)
                induction_end_bar = self.calculate_bar_position_with_rounding(current_time + induction_time, seconds_per_bar) if induction_time > 0 else induction_start_bar
                # Calculate bar positions for magnetron
                magnetron_start_bar = self.calculate_bar_position_with_rounding(current_time, seconds_per_bar)
                magnetron_end_bar = self.calculate_bar_position_with_rounding(current_time + magnetron_time, seconds_per_bar) if magnetron_time > 0 else magnetron_start_bar
                # Calculate pump period
                pump_end_time = current_time + pump_on if pump_on > 0 else current_time
                pump_end_bar = self.calculate_bar_position_with_rounding(pump_end_time, seconds_per_bar) if pump_on > 0 else induction_start_bar
                print(f"  Induction bars: {induction_start_bar} to {induction_end_bar}")
                print(f"  Magnetron bars: {magnetron_start_bar} to {magnetron_end_bar}")
                print(f"  Pump period: {current_time}s to {pump_end_time}s (bars {induction_start_bar} to {pump_end_bar})")
                # Determine the maximum end bar for this instruction to prevent exceeding timeline
                max_end_bar = max(induction_end_bar, magnetron_end_bar, pump_end_bar)
                max_bar_position = min(max_end_bar, total_bars - extra_bars - 1)
                print(f"  Adjusted max bar range: {induction_start_bar} to {max_bar_position}")
                # Draw bars for this instruction
                for bar_position in range(induction_start_bar, max_bar_position + 1):
                    actual_bar_index = extra_bars + bar_position
                    if actual_bar_index >= total_bars:
                        print(f"    BOUNDARY: Skipping bar {bar_position} (index {actual_bar_index}) - exceeds total_bars {total_bars}")
                        break
                    bar_y = base_y - (actual_bar_index * (bar_height + bar_spacing))
                    if bar_y < 0:
                        print(f"    CLIPPING: Skipping bar {bar_position} - Y position {bar_y/mm:.1f}mm is negative")
                        break
                    # Check if this bar is within pump period
                    is_pump_period = (pump_on > 0 and bar_position < pump_end_bar)
                    if is_pump_period:
                        print(f"    Bar {bar_position} (index {actual_bar_index}): BLUE at Y={bar_y/mm:.1f}mm")
                        c.setFillColor(self.blue_color)
                        c.rect(line_start_x, bar_y, total_width, bar_height, stroke=0, fill=1)
                    else:
                        # Draw induction bars
                        if induction_power > 0 and induction_start_bar <= bar_position < induction_end_bar:
                            power_ratio = min(induction_power / 100.0, 1.0)
                            induction_width = half_width * power_ratio
                            induction_x_pos = center_x - induction_width
                            c.setFillColor(self.bar_orange)
                            c.rect(induction_x_pos, bar_y, induction_width, bar_height, stroke=0, fill=1)
                            print(f"    Bar {bar_position} (index {actual_bar_index}): ORANGE at Y={bar_y/mm:.1f}mm (I:{induction_power})")
                        # Draw magnetron bars
                        if magnetron_power > 0 and magnetron_start_bar <= bar_position < magnetron_end_bar:
                            power_ratio = min(magnetron_power / 100.0, 1.0)
                            magnetron_width = half_width * power_ratio
                            c.setFillColor(self.bar_red)
                            c.rect(center_x, bar_y, magnetron_width, bar_height, stroke=0, fill=1)
                            print(f"    Bar {bar_position} (index {actual_bar_index}): RED at Y={bar_y/mm:.1f}mm (M:{magnetron_power})")
            current_time += duration
        print("=== END PUMP BAR DEBUG ===")

    def draw_left_section(self, c, recipe_data):
        """Body-left card with strict horizontal/vertical text guardrails."""
        text_margin = 8 * mm
        card_height = self.calculate_left_section_height(recipe_data)
        card_top = self.page_height
        card_x = 2 * mm
        card_w = self.left_section_width - 4 * mm
        card_y = card_top - card_height

        # Marble remains visible under a soft warm card.
        self.draw_soft_card(c, card_x, card_y, card_w, card_height, 4 * mm)

        card_bottom = card_y + 6 * mm
        y_pos = card_top - 10 * mm
        inner_right = self.left_section_width - 7 * mm
        inner_width = inner_right - text_margin

        def draw_icon_header(label, icon, y):
            self.draw_icon_badge(c, text_margin + 3 * mm, y, 3.6 * mm, icon)
            self.draw_single_line_fit(
                c, label, text_margin + 9 * mm, y - 1.3 * mm,
                inner_right - (text_margin + 9 * mm),
                self.section_title_font, self.section_title_size,
                min_size=8, fill_color=self.title_green
            )
            return y

        # Cooking Time
        y_pos = draw_icon_header("Cooking Time", 'clock', y_pos)
        y_pos -= 8 * mm

        for time_line in self.extract_cooking_time(recipe_data):
            for part in [p.strip() for p in time_line.split('    ') if p.strip()]:
                wrapped = self.wrap_text_to_width(
                    part, inner_width, self.section_detail_font, self.section_detail_size
                )
                for line in wrapped:
                    if y_pos < card_bottom:
                        break
                    self.draw_single_line_fit(
                        c, line, text_margin, y_pos, inner_width,
                        self.section_detail_font, self.section_detail_size,
                        min_size=7, fill_color=self.body_text
                    )
                    y_pos -= 5 * mm

        y_pos -= 2 * mm

        # Accessories
        self.draw_single_line_fit(
            c, "Accessories:", text_margin, y_pos, inner_width,
            self.section_title_font, self.section_title_size,
            min_size=8, fill_color=self.title_green
        )
        y_pos -= 6 * mm

        accessories = self.extract_accessories(recipe_data)
        acc_text = ', '.join(accessories) if accessories else 'N/A'
        for line in self.wrap_text_to_width(
            acc_text, inner_width, self.section_detail_font, self.section_detail_size
        ):
            if y_pos < card_bottom:
                break
            self.draw_single_line_fit(
                c, line, text_margin, y_pos, inner_width,
                self.section_detail_font, self.section_detail_size,
                min_size=7, fill_color=self.body_text
            )
            y_pos -= 5 * mm

        y_pos -= 3 * mm
        y_pos = draw_icon_header("Ingredients", 'ingredients', y_pos)
        y_pos -= 9 * mm

        ingredients = self.extract_ingredients(recipe_data)
        weight_x = text_margin
        name_x = text_margin + 20 * mm
        name_width = inner_right - name_x
        sub_x = text_margin + 4 * mm
        sub_width = inner_right - sub_x

        for ingredient in ingredients:
            if y_pos < card_bottom:
                break

            if ingredient.startswith('  '):
                lines = self.wrap_text_to_width(
                    ingredient.strip(), sub_width,
                    self.section_detail_font, self.section_detail_size
                )
                for line in lines:
                    if y_pos < card_bottom:
                        break
                    self.draw_single_line_fit(
                        c, line, sub_x, y_pos, sub_width,
                        self.section_detail_font, self.section_detail_size,
                        min_size=7, fill_color=self.body_muted
                    )
                    y_pos -= 5 * mm
            else:
                if '	' in ingredient:
                    weight, name = ingredient.split('	', 1)
                    name_lines = self.wrap_text_to_width(
                        name, name_width, self.step_title_font, self.section_detail_size
                    )
                    self.draw_single_line_fit(
                        c, weight, weight_x, y_pos, 18 * mm,
                        self.step_title_font, self.section_detail_size,
                        min_size=7, fill_color=self.body_text
                    )
                    for j, line in enumerate(name_lines):
                        if y_pos < card_bottom:
                            break
                        self.draw_single_line_fit(
                            c, line, name_x, y_pos, name_width,
                            self.step_title_font, self.section_detail_size,
                            min_size=7, fill_color=self.body_text
                        )
                        y_pos -= 5 * mm
                else:
                    lines = self.wrap_text_to_width(
                        ingredient, inner_width,
                        self.step_title_font, self.section_detail_size
                    )
                    for line in lines:
                        if y_pos < card_bottom:
                            break
                        self.draw_single_line_fit(
                            c, line, text_margin, y_pos, inner_width,
                            self.step_title_font, self.section_detail_size,
                            min_size=7, fill_color=self.body_text
                        )
                        y_pos -= 5 * mm

        # Other Essentials
        other_essentials = self.extract_other_essentials(recipe_data)
        if other_essentials and y_pos > card_bottom + 12 * mm:
            y_pos -= 3 * mm
            y_pos = draw_icon_header("Other Essentials", 'ingredients', y_pos)
            y_pos -= 9 * mm

            for essential in other_essentials:
                if y_pos < card_bottom:
                    break

                if essential.startswith('  '):
                    text = essential.strip()
                    x, width = name_x, name_width
                elif '	' in essential:
                    weight, text = essential.split('	', 1)
                    self.draw_single_line_fit(
                        c, weight, weight_x, y_pos, 18 * mm,
                        self.section_detail_font, 10,
                        min_size=7, fill_color=self.body_text
                    )
                    x, width = name_x, name_width
                else:
                    text = essential
                    x, width = text_margin, inner_width

                for line in self.wrap_text_to_width(
                    text, width, self.section_detail_font, 10
                ):
                    if y_pos < card_bottom:
                        break
                    self.draw_single_line_fit(
                        c, line, x, y_pos, width,
                        self.section_detail_font, 10,
                        min_size=7, fill_color=self.body_text
                    )
                    y_pos -= 5 * mm

    def draw_top_rounded_rect(self, c, x, y, width, height, radius, fill_color):
        """Draw rectangle with only top corners rounded"""
        c.saveState()
        c.setFillColor(fill_color)
        c.setStrokeColor(fill_color)
        path = c.beginPath()
        path.moveTo(x, y)
        path.lineTo(x + width, y)
        path.lineTo(x + width, y + height - radius)
        path.arcTo(x + width - radius, y + height - radius, x + width, y + height, startAng=0, extent=90)
        path.lineTo(x + radius, y + height)
        path.arcTo(x, y + height - radius, x + radius, y + height, startAng=90, extent=90)
        path.lineTo(x, y)
        c.drawPath(path, stroke=0, fill=1)
        c.restoreState()

    def draw_step_to_ruler_lines(self, c, consolidated_steps, block_x, block_width, ruler_left_x, start_y, vertical_spacing, extra_bars, bar_height, bar_spacing):
        """Draw connecting lines from step blocks to ruler, adjusting for extra bars when first step has zero duration"""
        c.saveState()
        c.setStrokeColor(HexColor('#800000'))
        c.setLineWidth(0.5)
        for i, step in enumerate(consolidated_steps):
            step_y = start_y - (i * vertical_spacing)
            line_start_x = block_x + block_width
            line_start_y = step_y + 1 * mm
            line_end_x = ruler_left_x
            if i == 0 and safe_int(step.get('durationInSec', 0)) == 0:
                line_end_y = start_y - (extra_bars * (bar_height + bar_spacing))
            else:
                line_end_y = step_y + 1 * mm
            c.line(line_start_x, line_start_y, line_end_x, line_end_y)
        c.restoreState()

    def draw_power_circles_with_values(self, c, x_pos, y_pos, step):
        """Draw larger orange and red circles with 'I' and 'M' labels inside"""
        induction_power = step.get('Induction_power', '0')
        magnetron_power = step.get('Magnetron_power', '0')
        circle_radius = 2.0*mm
        circle_spacing = 20*mm
        c.setFillColor(self.orange_color)
        c.circle(x_pos+5.5*mm, y_pos+3*mm, circle_radius, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont(self.section_detail_font, 8)
        c.drawCentredString(x_pos+5.5*mm, y_pos+2*mm, "I")
        self.draw_single_line_fit(
            c, str(induction_power),
            x_pos + circle_radius + 6.5 * mm, y_pos + 2 * mm,
            10 * mm,
            self.section_detail_font, 11,
            min_size=8, fill_color=black
        )
        red_circle_x = x_pos + circle_spacing
        c.setFillColor(self.red_color)
        c.circle(red_circle_x+2.5*mm, y_pos+3*mm, circle_radius, fill=1, stroke=0)
        c.setFillColor(white)
        c.setFont(self.section_detail_font, 8)
        c.drawCentredString(red_circle_x+2.5*mm, y_pos+2*mm, "M")
        self.draw_single_line_fit(
            c, str(magnetron_power),
            red_circle_x + circle_radius + 3.5 * mm, y_pos + 2 * mm,
            10 * mm,
            self.section_detail_font, 11,
            min_size=8, fill_color=black
        )

    def parse_instruction_with_weight(self, instruction_text, recipe_data, current_step=None):
        """Add ingredient weights to timeline labels without misclassifying
        ingredient-group names such as 'Mix Sauce & Noodles' as action verbs.
        """
        instruction_text = clean_time_and_units_text(instruction_text)
        ingredients = recipe_data.get('Ingredients', [])

        # If the instruction already contains quantities, keep it exactly.
        if re.search(
            r'\d+\s*(g|gm|ml|kg|l|number|Nos|inch)\b',
            instruction_text,
            re.IGNORECASE
        ):
            print(f"🎯 Instruction already contains weights: '{instruction_text}'")
            return [instruction_text]

        # CRITICAL: exact Ingredients[] match comes BEFORE cooking-action filtering.
        for ingredient in ingredients:
            ing_title = str(ingredient.get('title', '') or '').strip()
            ing_weight = clean_time_and_units_text(
                str(ingredient.get('weight', '') or '').strip()
            )
            if ing_title and ing_title.lower() == instruction_text.strip().lower():
                if ing_weight:
                    display = f"{ing_weight} {instruction_text.strip()}"
                    print(
                        f"🔍 Using ingredient weight: {ing_weight} for "
                        f"'{instruction_text.strip()}' (exact match before action filter)"
                    )
                    return [display]

        # Real cooking operations should not receive a fake ingredient weight.
        exclude_cooking_actions = [
            'dum', 'marinate', 'stir', 'cook', 'saute', 'heat',
            'fry', 'boil', 'grill', 'roast', 'bake', 'steam', 'simmer',
            'blend', 'whisk', 'knead', 'rest', 'cool', 'chill', 'freeze',
            'serve', 'garnish', 'season', 'taste', 'adjust', 'cover',
            'uncover', 'turn', 'flip', 'drain', 'strain', 'filter',
            'temperature down'
        ]

        # "mix" is intentionally NOT blanket-excluded here because legitimate
        # ingredient groups can be called "Mix Sauce & Noodles", "Mix Masala", etc.
        lower_text = instruction_text.lower().strip()
        if any(action in lower_text for action in exclude_cooking_actions):
            print(f"🚫 Excluding cooking action: '{instruction_text}' (no weight added)")
            return [instruction_text]

        lines = []

        if ', ' in instruction_text:
            instruction_parts = instruction_text.split(', ')
        else:
            instruction_parts = [instruction_text]

        for instruction in instruction_parts:
            instruction = instruction.strip()
            weight_found = False
            display_line = instruction

            # Prefer current step weight when valid.
            if current_step and current_step.get('Weight'):
                step_weight = clean_time_and_units_text(
                    str(current_step.get('Weight', '') or '').strip()
                )
                qty, _, _ = _parse_amount(step_weight)
                if step_weight and (qty is None or qty > 0):
                    display_line = f"{step_weight} {instruction}"
                    weight_found = True
                    print(f"🎯 Using step weight: {step_weight} for '{instruction}'")

            if not weight_found:
                # Exact match.
                for ingredient in ingredients:
                    ing_title = str(ingredient.get('title', '') or '').strip()
                    ing_weight = clean_time_and_units_text(
                        str(ingredient.get('weight', '') or '').strip()
                    )
                    if ing_title.lower() == instruction.lower() and ing_weight:
                        display_line = f"{ing_weight} {instruction}"
                        weight_found = True
                        print(
                            f"🔍 Using ingredient weight: {ing_weight} "
                            f"for '{instruction}' (exact match)"
                        )
                        break

            if not weight_found:
                # Partial match as a last resort.
                for ingredient in ingredients:
                    ing_title = str(ingredient.get('title', '') or '').strip()
                    ing_weight = clean_time_and_units_text(
                        str(ingredient.get('weight', '') or '').strip()
                    )
                    if ing_title and ing_title.lower() in instruction.lower() and ing_weight:
                        display_line = f"{ing_weight} {instruction}"
                        weight_found = True
                        print(
                            f"🔍 Using ingredient weight: {ing_weight} "
                            f"for '{instruction}' (partial match)"
                        )
                        break

            lines.append(display_line)

        return lines

    def draw_right_section(self, c, recipe_data, seconds_per_bar):
        """Right section with non-overlapping labels centered over the real I/M bar halves."""
        print("🔍 === DRAWING RIGHT SECTION ===")
        print(f" QR image available: {self.qr_image is not None}")

        x_offset = self.left_section_width + self.left_margin
        circle_y = self.page_height - 12 * mm

        # These anchors are the same anchors used by draw_timeline().
        magnetron_x = self.page_width - 15 * mm
        induction_x = magnetron_x - 23 * mm
        circle_radius = self.circle_diameter / 2

        # Actual bar span:
        #   [bar_left .......... center .......... bar_right]
        #           induction              microwave
        bar_left = induction_x - circle_radius
        bar_right = magnetron_x + circle_radius
        center_x = (bar_left + bar_right) / 2

        induction_col_center = (bar_left + center_x) / 2
        microwave_col_center = (center_x + bar_right) / 2
        half_col_width = (bar_right - bar_left) / 2

        # Keep visible space between labels and inside page margins.
        pill_gap = 2.0 * mm
        pill_w = max(15 * mm, half_col_width - pill_gap)
        pill_h = 7 * mm

        c.saveState()

        # INDUCTION — exactly centered on left/orange timeline half.
        c.setFillColor(self.orange_color)
        c.roundRect(
            induction_col_center - pill_w / 2,
            circle_y - pill_h / 2,
            pill_w, pill_h, pill_h / 2,
            stroke=0, fill=1
        )
        self.draw_single_line_fit(
            c, "INDUCTION",
            induction_col_center - pill_w / 2 + 1 * mm,
            circle_y - 1.35 * mm,
            pill_w - 2 * mm,
            self.section_title_font, 7.7,
            min_size=6.2, fill_color=white, align='center'
        )

        # MICROWAVE — exactly centered on right/red timeline half.
        c.setFillColor(self.red_color)
        c.roundRect(
            microwave_col_center - pill_w / 2,
            circle_y - pill_h / 2,
            pill_w, pill_h, pill_h / 2,
            stroke=0, fill=1
        )
        self.draw_single_line_fit(
            c, "MICROWAVE",
            microwave_col_center - pill_w / 2 + 1 * mm,
            circle_y - 1.35 * mm,
            pill_w - 2 * mm,
            self.section_title_font, 7.7,
            min_size=6.2, fill_color=white, align='center'
        )

        c.restoreState()

        # Optional QR code — kept away from timeline headings.
        if self.qr_image:
            print(" Drawing QR code on PDF...")
            try:
                tmp_qr_path = os.path.join(tempfile.gettempdir(), "qr_temp_image.png")
                self.qr_image.save(tmp_qr_path)
                qr_size = 16 * mm
                qr_x = x_offset + 4 * mm
                qr_y = self.page_height - 4 * mm - qr_size
                c.drawImage(
                    tmp_qr_path, qr_x, qr_y,
                    width=qr_size, height=qr_size,
                    preserveAspectRatio=True, mask='auto'
                )
                self.draw_single_line_fit(
                    c, "Scan to download recipe",
                    qr_x, qr_y - 3 * mm, 34 * mm,
                    self.section_detail_font, 6.5,
                    min_size=5.5, fill_color=self.body_text
                )
                print("✅ QR code drawn on PDF successfully!")
            except Exception as e:
                print(f"❌ QR draw error: {e}")

        if self.has_stirrer_activity(recipe_data):
            print("🔍 Stirrer activity detected, drawing stirrer SVG...")
            stirrer_x = (induction_x + magnetron_x) / 2
            self.draw_stirrer_svg(
                c, stirrer_x - 2.3 * mm,
                circle_y - 10 * mm,
                'Stirrer.svg', scale=0.6
            )
        else:
            print("ℹ️ No stirrer activity detected, skipping stirrer SVG")

        # Position timeline ruler directly under the centered pills.
        timeline_start_y = circle_y - pill_h / 2 - 22 * mm
        print("🔍 Drawing timeline...")
        self.draw_timeline(
            c, x_offset, timeline_start_y,
            recipe_data, seconds_per_bar
        )
        print("✅ Right section drawing completed!")

    def draw_stirrer_speed_bars(self, c, consolidated_steps, center_x, scale_y, total_time_sec, total_bars, extra_bars, seconds_per_bar):
        """Draw thin VERTICAL stirrer bars with proper timing and colors based on speed"""
        stirrer_colors = {
            'off': None, '0': None, '': None,
            'low': white, '1': white,
            'medium': HexColor('#08a045'), '2': HexColor('#08a045'), 
            'high': HexColor('#FFA500'), '3': HexColor('#FFA500'),
            'very high': HexColor('#FF0000'), '4': HexColor('#FF0000'),
        }
        
        bar_height = 1*mm
        bar_spacing = 1*mm
        thin_bar_width = 0.5*mm  # Slightly wider for better visibility
        circle_radius = 2*mm
        circle_buffer = 0.5*mm  # Reduced buffer for tighter spacing
        
        print("=== STIRRER SPEED VERTICAL BARS DEBUG (Proper timing) ===")
        
        # Calculate circle positions using same coordinate system as step connections
        circle_positions = []
        elapsed_time = 0
        for i, step in enumerate(consolidated_steps):
            step_number = i + 1
            duration = step.get('durationInSec', 0)
            
            if step_number == 1 and duration == 0:
                # First step with zero duration - position at top
                circle_y = scale_y + 1*mm
            else:
                # Use bar-based rounding for accurate positioning
                bar_position = self.calculate_bar_position_with_rounding(elapsed_time, seconds_per_bar)
                circle_y = scale_y - (extra_bars * (bar_height + bar_spacing)) - (bar_position * (bar_height + bar_spacing))
            
            circle_positions.append(circle_y)
            elapsed_time += duration
        
        # Draw stirrer bars using proper timing calculations
        current_time = 0
        for i, step in enumerate(consolidated_steps):
            duration = step.get('durationInSec', 0)
            stirrer_speed = str(step.get('stirrer_on', '0')).strip().lower()
            
            if duration <= 0:
                current_time += duration
                continue
                
            color = stirrer_colors.get(stirrer_speed, None)
            if color is None:
                print(f"  Step {i+1}: No stirrer (speed: {stirrer_speed})")
                current_time += duration
                continue
            
            # Calculate bar positions using rounding logic
            start_bar_position = self.calculate_bar_position_with_rounding(current_time, seconds_per_bar)
            end_bar_position = self.calculate_bar_position_with_rounding(current_time + duration, seconds_per_bar)
            
            # Convert to Y coordinates using same system as power bars
            start_y = scale_y - (extra_bars * (bar_height + bar_spacing)) - (start_bar_position * (bar_height + bar_spacing))
            end_y = scale_y - (extra_bars * (bar_height + bar_spacing)) - (end_bar_position * (bar_height + bar_spacing))
            
            segment_height = start_y - end_y
            
            # Avoid drawing over step circles
            segments_to_draw = [(end_y, segment_height)]
            
            for circle_y in circle_positions:
                new_segments = []
                for seg_y, seg_height in segments_to_draw:
                    seg_top = seg_y + seg_height
                    
                    # Check if circle intersects this segment
                    if (seg_y <= circle_y <= seg_top):
                        # Split segment around circle
                        circle_top = circle_y + circle_radius + circle_buffer
                        circle_bottom = circle_y - circle_radius - circle_buffer
                        
                        # Segment above circle
                        if seg_top > circle_top:
                            above_height = seg_top - circle_top
                            new_segments.append((circle_top, above_height))
                        
                        # Segment below circle  
                        if seg_y < circle_bottom:
                            below_height = circle_bottom - seg_y
                            new_segments.append((seg_y, below_height))
                    else:
                        # No intersection, keep original segment
                        new_segments.append((seg_y, seg_height))
                
                segments_to_draw = new_segments
            
            # Draw all segments for this step
            total_drawn_height = 0
            for seg_y, seg_height in segments_to_draw:
                if seg_height > 0.5*mm:  # Only draw segments larger than 0.5mm
                    c.setFillColor(color)
                    c.setStrokeColor(color)
                    c.setLineWidth(0.2)
                    c.rect(center_x - thin_bar_width/2, seg_y, thin_bar_width, seg_height, stroke=1, fill=1)
                    total_drawn_height += seg_height
            
            print(f"  Step {i+1}: Drew stirrer bar (speed: {stirrer_speed}, color: {color}, height: {total_drawn_height/mm:.1f}mm)")
            current_time += duration
        
        print("=== END STIRRER SPEED DEBUG ===")

    def draw_ruler_ticks(self, c, induction_x, magnetron_x, base_y):
        """Draw ruler ticks with alternating heights"""
        circle_radius = self.circle_diameter / 2
        c.setStrokeColor(black)
        c.setLineWidth(0.3)
        tick_count = 10
        tick_height_small = 2*mm
        tick_height_large = 3*mm
        tick_spacing = self.circle_diameter / tick_count
        left_tick_start = induction_x - circle_radius
        for i in range(tick_count + 1):
            x_pos = left_tick_start + (i * tick_spacing)
            tick_height = tick_height_large if i % 2 == 0 else tick_height_small
            c.line(x_pos, base_y, x_pos, base_y + tick_height)
        right_tick_start = magnetron_x - circle_radius
        for i in range(tick_count + 1):
            x_pos = right_tick_start + (i * tick_spacing)
            tick_height = tick_height_large if i % 2 == 0 else tick_height_small
            c.line(x_pos, base_y, x_pos, base_y + tick_height)
        line_start_x = left_tick_start
        line_end_x = right_tick_start + self.circle_diameter
        c.setLineWidth(0.3)

    def draw_timeline_completion_tick(self, c, induction_x, magnetron_x, scale_y, total_bars, bar_height, bar_spacing):
        """Draw a tick mark at the end of the timeline to indicate completion"""
        circle_radius = self.circle_diameter / 2
        line_start_x = induction_x - circle_radius
        line_end_x = magnetron_x + circle_radius
        last_bar_y = scale_y - (total_bars * (bar_height + bar_spacing))
        center_x = (line_start_x + line_end_x) / 2
        tick_y = last_bar_y
        tick_circle_radius = 2*mm
        c.setFillColor(self.skin_color)
        c.setStrokeColor(black)
        c.setLineWidth(0.5)
        c.circle(center_x, tick_y, tick_circle_radius, fill=1, stroke=1)
        c.setFillColor(black)
        c.setFont('Helvetica-Bold', 8)
        c.drawCentredString(center_x, tick_y - 1*mm, "✓")
        print(f"Drew completion tick at ({center_x}, {tick_y})")

    def draw_time_based_horizontal_bars(self, c, induction_x, magnetron_x, base_y, total_bars, recipe_data, extra_bars, seconds_per_bar):
        circle_radius = self.circle_diameter / 2
        bar_height = 1*mm
        bar_spacing = 1*mm
        line_start_x = induction_x - circle_radius
        line_end_x = magnetron_x + circle_radius
        total_width = line_end_x - line_start_x
        
        for i in range(total_bars):
            bar_y = base_y - (i * (bar_height + bar_spacing))
            if i < extra_bars:
                c.setFillColor('#F5F5F5')
                c.setStrokeColor(HexColor('#F5F5F5'))
                c.rect(line_start_x, bar_y, total_width, bar_height, stroke=1, fill=1)
            else:
                c.setFillColor(HexColor('#F5F5F5'))
                c.setStrokeColor(HexColor('#F5F5F5'))
                c.rect(line_start_x, bar_y, total_width, bar_height, stroke=0, fill=1)
        
        # ✅ FIXED: Pass the same base_y coordinate to power bars
        self.draw_colored_power_bars(c, induction_x, magnetron_x, base_y, recipe_data, total_bars, extra_bars, seconds_per_bar)
    def extract_cooking_time(self, recipe_data):
        """Extract cooking time with 3x on2cook time when normal cooking is N/A"""
        total_sec = sum(int(i.get("durationInSec", 0)) for i in recipe_data.get("Instruction", []))
        on2_minutes = total_sec // 60
        on2_seconds = total_sec % 60
        
        # ✅ UPDATED: Apply time formatting rules
        if on2_minutes == 1 and on2_seconds == 0:
            on2 = "On2Cook: 1:00 min"
        else:
            on2 = f"On2Cook: {on2_minutes}:{on2_seconds:02d} mins"
        
        desc = recipe_data.get("description", "")
        match = re.search(r"NORMAL COOKING TIME\s*(\d+)\s*MINUTES", desc.upper())
        
        if match:
            normal_mins = int(match.group(1))
            if normal_mins == 1:
                normal = "Normal Cooking: 1 min"
            else:
                normal = f"Normal Cooking: {normal_mins} mins"
        else:
            # Calculate 3x the on2cook time when normal cooking is N/A
            normal_total_sec = total_sec * 3
            normal_minutes = normal_total_sec // 60
            normal_seconds = normal_total_sec % 60
            
            if normal_minutes == 1 and normal_seconds == 0:
                normal = "Normal Cooking: 1:00 min"
            else:
                normal = f"Normal Cooking: {normal_minutes}:{normal_seconds:02d} mins"
        
        return [f"{on2}    {normal}"]
    
    def extract_output(self, recipe_data):
        """Extract the output (e.g., '400 GM' or '400 g') from the description field."""
        import re
        description = recipe_data.get('description', '').strip()
        print(f"DEBUG: Raw description in extract_output: {description!r}")
        print(f"DEBUG: Description bytes: {description.encode('utf-8')!r}")
        
        # Normalize only spaces and tabs (preserve newlines for line-based capture)
        description = re.sub(r'[ \t]+', ' ', description).strip()
        print(f"DEBUG: Normalized description (spaces/tabs only): {description!r}")
        
        # Remove BOM or other control characters
        description = description.encode('utf-8').decode('utf-8-sig').strip()
        print(f"DEBUG: Cleaned description: {description!r}")
        
        # Match 'OUTPUT' followed by content until newline (preserves line structure)
        match = re.search(r'OUTPUT\s+([^\n\r]+)', description, re.IGNORECASE)
        if match:
            output_value = match.group(1).strip()
            print(f"🔍 Extracted Output from description: {output_value}")
            # Clean units (GM to g) for consistency
            output_value = re.sub(r'\bGM\b', 'g', output_value, flags=re.IGNORECASE)
            print(f"🔍 Final output value after unit cleaning: {output_value}")
            return output_value
        else:
            print("🔍 No OUTPUT found in description, defaulting to 'n/a'")
            print(f"DEBUG: Regex pattern used: r'OUTPUT\s+([^\n\r]+)'")
            return 'n/a'
    def extract_accessories(self, recipe_data):
        """Parse the ACCESSORIES block for summary display.

        Notes must NOT appear inside the Accessories field, otherwise the UI
        truncates them with ellipses. So we stop when NOTE / NOTES begins.
        """
        description = recipe_data.get('description', '')
        accessories = []
        if 'ACCESSORIES' in description.upper():
            lines = description.split('\n')
            collecting = False
            for line in lines:
                line = line.strip()
                if line.upper() == 'ACCESSORIES':
                    collecting = True
                    continue
                elif collecting and line:
                    upper = line.upper()
                    if any(
                        upper.startswith(keyword)
                        for keyword in ['OUTPUT', 'FINAL OUTPUT', 'NORMAL', 'COOKING', 'OTHER ESSENTIALS', 'NOTE', 'NOTES']
                    ):
                        break
                    accessories.append(line)
        return accessories

    # Units seen in real recipe data (extend here as new units show up)
    _QTY_UNITS = (
        r'gm|g|kg|ml|l|liter|litre|number|no|nos|inch|pinch|'
        r'tsp|tbsp|pcs|pc|strand|strands|string|strings|slice|slices|cup|cups'
    )
    # Equipment/accessory names that sometimes leak into the Ingredients list.
    # Matched as whole words/phrases so "pan" doesn't also catch "Paneer",
    # "stand" doesn't catch "Standard Roux", etc.
    _SKIP_TERMS = ['grill mesh', 'cake mold', 'stirrer', 'pan', 'tray', 'rack', 'stand']
    _SKIP_RE = re.compile(
        r'\b(?:' + '|'.join(re.escape(t) for t in _SKIP_TERMS) + r')\b', re.I
    )

    def extract_ingredients(self, recipe_data):
        qty_token = re.compile(rf'^\d+(?:\.\d+)?(?:{self._QTY_UNITS})$', re.I)

        def squash_qty(tokens):
            out, i = [], 0
            while i < len(tokens):
                if (i + 1 < len(tokens) and
                    re.fullmatch(r'\d+(?:\.\d+)?', tokens[i]) and
                    re.fullmatch(self._QTY_UNITS, tokens[i + 1], re.I)):
                    out.append(tokens[i] + tokens[i + 1])
                    i += 2
                else:
                    out.append(tokens[i])
                    i += 1
            return out

        ingredients = []
        for ing in recipe_data.get('Ingredients', []):
            wt = ing.get('weight', '').strip()
            ttl = ing.get('title', '').strip()
            txt = ing.get('text', '').replace(',', ' ').strip()
            txt = re.sub(r'\([^)]*\)', '', txt).strip()
            print(f"DEBUG: Processing ingredient - weight: '{wt}', title: '{ttl}', text: '{txt}'")
            if self._SKIP_RE.search(ttl):
                continue
            if wt and ttl:
                wt_standardized = re.sub(r'\bgm\b', 'g', wt, flags=re.IGNORECASE)
                ingredients.append(f"{wt_standardized}\t{ttl}")
                print(f"DEBUG: Added main ingredient: '{wt_standardized}\t{ttl}'")
            if not txt:
                continue
            toks = squash_qty(txt.split())
            print(f"DEBUG: Tokens after squash_qty: {toks}")
            prev_qty_at = -1
            pairs = []
            for i, tok in enumerate(toks):
                if qty_token.match(tok):
                    name_tokens = toks[prev_qty_at + 1 : i]
                    if name_tokens:
                        ingredient = ' '.join(name_tokens)
                        qty_fixed = re.sub(r'(?<=\d)(?=[a-zA-Z])',' ', tok)
                        qty_standardized = re.sub(r'\bgm\b', 'g', qty_fixed, flags=re.IGNORECASE)
                        pairs.append(f"{qty_standardized} {ingredient}")
                    prev_qty_at = i
            tail = toks[prev_qty_at + 1 :]
            if tail:
                pairs.append(''.join(tail))
            print(f"DEBUG: Pairs for text: {pairs}")
            line, char_limit = '', 35
            for p in pairs:
                if not line:
                    line = p
                elif len(line) + len(p) + 2 <= char_limit:
                    line += ', ' + p
                else:
                    ingredients.append('  ' + line)
                    print(f"DEBUG: Added sub-ingredient line: '  {line}'")
                    line = p
            if line:
                ingredients.append('  ' + line)
                print(f"DEBUG: Added final sub-ingredient line: '  {line}'")
        print(f"DEBUG: Final ingredients list: {ingredients}")
        return ingredients
    def extract_other_essentials(self, recipe_data):
        """Extract Other Essentials from description field"""
        import re
        
        description = recipe_data.get('description', '')
        other_essentials = []
        
        if 'OTHER ESSENTIALS' in description.upper():
            lines = description.split('\n')
            collecting = False
            
            for line in lines:
                line = line.strip()
                if line.upper() == 'OTHER ESSENTIALS':
                    collecting = True
                    continue
                elif collecting and line:
                    # Stop collecting if we hit another section or empty line
                    if any(keyword in line.upper() for keyword in ['OUTPUT', 'NORMAL', 'ACCESSORIES', 'COOKING']):
                        break
                    
                    # Parse the line to extract quantity and item
                    # Handle formats like "1L PRE-HEATED OIL 180° C" or "1 UNIT BOWL FOR TOSSING"
                    
                    # Try to match quantity at the beginning
                    quantity_match = re.match(r'^(\d+(?:\.\d+)?)\s*([A-Za-z]+)\s+(.+)', line)
                    if quantity_match:
                        quantity = quantity_match.group(1)
                        unit = quantity_match.group(2).lower()
                        item = quantity_match.group(3).title()
                        
                        # Convert "gm" to "g" if present
                        if unit == 'gm':
                            unit = 'g'
                        
                        formatted_line = f"{quantity} {unit}\t{item}"
                        other_essentials.append(formatted_line)
                    else:
                        # If no clear quantity pattern, add as-is but formatted
                        other_essentials.append(line.title())
        
        return other_essentials
    
    def has_stirrer_activity(self, recipe_data):
        """Check if any instruction has stirrer activity"""
        instructions = recipe_data.get('Instruction', [])
        for instruction in instructions:
            stirrer_on = str(instruction.get('stirrer_on', '0')).strip().lower()
            if stirrer_on not in ['0', 'off', '']:
                return True
        return False

    def draw_stirrer_svg(self, c, x, y, svg_path, scale=1.0):
        """Draw SVG file on canvas at specified position"""
        try:
            # Get the directory where this script is located
            script_dir = os.path.dirname(os.path.abspath(__file__))
            full_svg_path = os.path.join(script_dir, svg_path)
            
            if os.path.exists(full_svg_path):
                drawing = svg2rlg(full_svg_path)
                # Scale the drawing
                drawing.width = drawing.minWidth() * scale
                drawing.height = drawing.height * scale
                drawing.scale(scale, scale)
                # Draw it on the canvas
                renderPDF.draw(drawing, c, x, y)
                print(f"✅ Stirrer SVG drawn at ({x/mm:.1f}mm, {y/mm:.1f}mm)")
            else:
                print(f"❌ Stirrer SVG not found at: {full_svg_path}")
        except Exception as e:
            print(f"❌ Error drawing stirrer SVG: {e}")
    def calculate_bar_position_with_rounding(self, time_seconds, seconds_per_bar):
        """Calculate bar position with rounding: >=5 seconds rounds up, <5 rounds down"""
        if time_seconds <= 0:
            return 0
        
        full_bars = time_seconds // seconds_per_bar
        remainder = time_seconds % seconds_per_bar
        
        print(f"DEBUG: {time_seconds}s ÷ {seconds_per_bar} = {full_bars} bars + {remainder}s remainder")
        
        if remainder >= 5:
            result = full_bars + 1
            print(f"DEBUG: {remainder} ≥ 5, rounding UP to {result}")
            return result
        else:
            result = full_bars
            print(f"DEBUG: {remainder} < 5, rounding DOWN to {result}")
            return result


# =========================
# Dropbox + QR helpers
# =========================

def upload_to_dropbox_and_get_direct_url(zip_file_path, token, folder):
    """Uploads the ZIP to Dropbox and returns a direct download URL."""
    if not token:
        raise ValueError("Dropbox token not provided.")
    dbx = dropbox.Dropbox(token)
    try:
        _ = dbx.users_get_current_account()
    except AuthError:
        raise ValueError("Invalid or expired Dropbox token.")
    file_name = os.path.basename(zip_file_path)
    dest_path = f"{folder.rstrip('/')}/{file_name}"
    with open(zip_file_path, 'rb') as f:
        dbx.files_upload(f.read(), dest_path, mode=dropbox.files.WriteMode.overwrite)
    settings = dropbox.sharing.SharedLinkSettings(
        requested_visibility=dropbox.sharing.RequestedVisibility.public
    )
    link_meta = dbx.sharing_create_shared_link_with_settings(dest_path, settings)
    direct_url = link_meta.url.replace('www.dropbox.com', 'dl.dropboxusercontent.com').replace('?dl=0', '')
    return direct_url

def generate_qr_with_center_logo(data_url, logo_path=LOGO_PATH, logo_ratio=LOGO_RATIO):
    """Generates a QR PIL image with a center logo if available."""
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H)
    qr.add_data(data_url)
    qr.make(fit=True)
    img = qr.make_image(fill_color='black', back_color='white').convert('RGB')
    try:
        if os.path.exists(logo_path):
            logo = Image.open(logo_path).convert("RGBA")
            w, h = img.size
            logo_size = (w // logo_ratio, h // logo_ratio)
            logo = logo.resize(logo_size, Image.LANCZOS)
            lw, lh = logo.size
            pos = ((w - lw) // 2, (h - lh) // 2)
            img.paste(logo, pos, mask=logo)
        else:
            print(f"Logo not found at {logo_path}. Generating QR without logo.")
    except Exception as e:
        print(f"Logo paste error: {e}. Generating QR without logo overlay.")
    return img

# =========================
# Main CLI
# =========================

def main():
    import sys
    import glob
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  Single file: python script.py <zip_file_path> [output_pdf_path] [seconds_per_bar] [dropbox_token]")
        print("  Multiple files: python script.py --multiple <zip_pattern_or_directory> <output_directory> [seconds_per_bar] [dropbox_token]")
        print("\nExamples:")
        print("  python script.py recipe1.zip")
        print("  python script.py --multiple 'recipes/*.zip' ./output_pdfs/")
        print("  python script.py --multiple ./recipe_folder/ ./output_pdfs/")
        return
    
    if sys.argv[1] == '--multiple':
        # Multiple file processing - separate PDFs for each zip
        if len(sys.argv) < 4:
            print("❌ Error: --multiple requires <zip_pattern_or_directory> and <output_directory>")
            return
        
        zip_input = sys.argv[2]
        output_directory = sys.argv[3]
        seconds_per_bar = safe_int(sys.argv[4]) if len(sys.argv) > 4 else 9
        dropbox_token = sys.argv[5] if len(sys.argv) > 5 else os.environ.get("DROPBOX_TOKEN", "").strip()
        
        # Find zip files
        zip_files = []
        
        if os.path.isdir(zip_input):
            # If directory provided, find all zip files in it
            zip_files = glob.glob(os.path.join(zip_input, "*.zip"))
        else:
            # If pattern provided, use glob to find matching files
            zip_files = glob.glob(zip_input)
        
        if not zip_files:
            print(f"❌ No zip files found matching: {zip_input}")
            return
        
        print(f"🔍 Found {len(zip_files)} zip files to process:")
        for zip_file in zip_files:
            print(f"   • {os.path.basename(zip_file)}")
        
        # Process all files individually
        try:
            generator = RecipePDFGenerator()
            results = generator.process_multiple_zip_files_individually(
                zip_files, output_directory, seconds_per_bar, dropbox_token
            )
            
            # Final summary
            successful_count = len([r for r in results if r['status'] == 'success'])
            if successful_count > 0:
                print(f"\n🎉 Successfully generated {successful_count} PDF(s) in: {output_directory}")
            else:
                print(f"\n😞 No PDFs were generated successfully")
                
        except Exception as e:
            print(f"❌ Fatal error during processing: {e}")
            import traceback
            traceback.print_exc()
    
    else:
        # Single file processing (your existing code)
        zip_file_path = sys.argv[1]
        output_pdf_path = sys.argv[2] if len(sys.argv) > 2 else "recipe_output.pdf"
        seconds_per_bar = safe_int(sys.argv[3]) if len(sys.argv) > 3 else 9
        dropbox_token = sys.argv[4] if len(sys.argv) > 4 else os.environ.get("DROPBOX_TOKEN", "").strip()
        dropbox_folder = sys.argv[5] if len(sys.argv) > 5 else DB_DEFAULT_FOLDER

        # Your existing single file processing code here...
        qr_img = None
        try:
            direct_url = upload_to_dropbox_and_get_direct_url(zip_file_path, dropbox_token, dropbox_folder)
            qr_img = generate_qr_with_center_logo(direct_url, LOGO_PATH, LOGO_RATIO)
        except Exception as e:
            print(f"Dropbox/QR step warning: {e}. Proceeding to generate PDF without QR.")

        try:
            generator = RecipePDFGenerator(qr_image=qr_img)
            actual_output = generator.process_zip_file(zip_file_path, output_pdf_path, seconds_per_bar)
            print(f"✅ Recipe PDF generated successfully: {actual_output}")
            if os.path.exists(actual_output):
                file_size = os.path.getsize(actual_output)
                print(f"📄 File size: {file_size:,} bytes")
                print(f"📁 Location: {os.path.abspath(actual_output)}")
        except Exception as e:
            print(f"❌ Error: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
