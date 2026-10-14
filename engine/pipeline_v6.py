"""
L'Oreal BPC SKU Tagging Engine - Core Module
Rebuilt after a container reset, reconstructed from the Master Glossary plus every
fix made during the final singleton-conflict audit round (which happened after the
last save). This reflects the fully audited final state of the engine.
"""
import re
import json
from pathlib import Path
from keyword_repo import TIER0_HARDWARE, FEMININE_HYGIENE_KEYWORDS, BABY_CARE_KEYWORDS

# Last-resort, manually-curated lookup for SKUs with no generalizable keyword signal at
# all. Checked ONLY after every other rule in tag_sku() has failed to match - this is not
# for cases a keyword rule could handle, it's for genuine dead ends. Lives in its own JSON
# file (not hardcoded like SKU_MEMORY) so it can be extended without touching engine code.
_SKU_EXCEPTIONS_PATH = Path(__file__).resolve().parent / 'sku_exceptions.json'
try:
    with open(_SKU_EXCEPTIONS_PATH, 'r', encoding='utf-8') as _f:
        SKU_EXCEPTIONS = json.load(_f)
except FileNotFoundError:
    SKU_EXCEPTIONS = {}

# Placeholder used ONLY for the true last-resort case: the engine found no signal at all
# (not even a brand or loose keyword). Deliberately NOT a real taxonomy value (unlike the
# genuine "Others/Others Others/Others" L0 category, used elsewhere for real ambiguous-but-
# recognized items like supplements) - a real category value here would be indistinguishable
# from a confident tag. Kept non-blank (rather than None) so the row stays visible/editable
# in the review UI instead of vanishing as an empty cell.
UNCLASSIFIED = 'UNCLASSIFIED - Needs Manual Review'

def fix_encoding(s):
    if not s: return s
    try:
        return s.encode('latin1').decode('utf-8')
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s

COMPOUND_SPLITS = [
    (r'\bface\s?wash\b', 'face wash'), (r'\bbody\s?wash\b', 'body wash'),
    (r'\bhand\s?wash\b', 'hand wash'), (r'\bhair\s?wash\b', 'hair wash'),
    (r'\bhair\s?colour\b', 'hair colour'), (r'\bhair\s?color\b', 'hair color'),
    (r'\blip\s?balm\b', 'lip balm'),
    # FIX: has_word() only supports -s/-es plurals, not -ing - "Colouring"/"Coloring" (e.g. "Hair
    # Colouring Creme") never matched any of the many has_word(text,'colour')/'color' checks
    # throughout this file, silently falling through to generic Face Creams/Cream defaults
    # instead of the Hair colour rules. Normalizing the gerund to the base form here fixes every
    # one of those checks at once, the same way 'waxing'->'wax' already does below.
    (r'\bcolouring\b', 'colour'), (r'\bcoloring\b', 'color'),
    # FIX: "Hairband" and "Hairclip" (fused, no space) don't match the existing 'hair band'
    # DIRECT_KEYWORD or the 'clip' base's 'hair' modifier, which both require the two words
    # separated - 26+ Diya Aswani hairband SKUs alone were falling to Unmapped over this.
    (r'\bhairbands?\b', 'hair band'), (r'\bhairclips?\b', 'hair clip'),
    (r'\bhairbrush\b', 'hair brush'), (r'\bhairdryer\b', 'hair dryer'),
    (r'\bhairspray\b', 'hair spray'), (r'\bhairpin\b', 'hair pin'),
    (r'\bblowdryer\b', 'blow dryer'),
    # FIX: "Masque" (the French spelling, e.g. "Hair Masque") never matched any of the many
    # has_word(text,'mask') checks throughout this file - same class of gap as 'creme'/'colour'.
    (r'\bmasque\b', 'mask'),
    # FIX: "Black Head Remover"/"White Head Strips" (spaced) don't match the existing
    # 'blackhead'/'whitehead' modifiers, which require the fused spelling - e.g. "VEGA Black
    # Head Remover" was falling through to the unrelated Hair Colour Remover default instead of
    # Whitehead/Blackhead Remover.
    (r'\bblack\s?head\b', 'blackhead'), (r'\bwhite\s?head\b', 'whitehead'),
    # FIX: "Eye Lash" (spaced, e.g. "GUBB Eye Lash Classic Without Glue") doesn't match the
    # existing 'eyelash' DIRECT_KEYWORD, which requires the fused spelling.
    (r'\beye\s?lash\b', 'eyelash'),
    (r'\bfacesponge\b', 'face sponge'), (r'\bmaangtika\b', 'maang tika'),
    # FIX: common misspellings of 'scrunchie' (missing the 'r') seen across real brands.
    (r'\bscunchie\b', 'scrunchie'), (r'\bscruchie\b', 'scrunchie'),
    # "Peeloff"/"Peel-Off"/"Peel Off" are the same word, spaced differently - normalize so
    # resolve_mask()'s 'peel off' check catches all three spellings.
    (r'\bpeel[\s-]?off\b', 'peel off'),
    # "2in1"/"2-in-1"/"2 in 1" (and 3-way) are the same claim spaced differently - normalize so
    # the 2-in-1/3-in-1 shampoo pre-checks in bundle_detect.py catch all spellings.
    (r'\b2[\s-]?in[\s-]?1\b', '2 in 1'),
    (r'\b3[\s-]?in[\s-]?1\b', '3 in 1'),
    (r'\bwaxing\b', 'wax'),
    # "Oil-Free"/"Oil Free" explicitly says the product does NOT contain oil - joining it
    # into one token hides it from has_word(text,'oil'), so it stops falsely triggering the
    # oil CONTEXT_MATRIX base (was pulling "Oil-Free Moisturizer"/"Oil Control Fluid" into
    # oil-format logic instead of letting the real product-type word drive the tag).
    (r'\boil\s?free\b', 'oilfree'),
    # "Faces Canada" is a real brand name - "Faces" alone matches the bare word "face" (plural
    # support), falsely triggering any 'face' co-occurrence/modifier check (e.g. this brand's
    # "Foundation + Moisturizer" products were wrongly routed to Face Creams via the
    # moisturizer+'face' modifier, purely because of the brand name).
    (r'\bfaces\s?canada\b', 'facescanadabrand'),
    # FIX: "TNW The Natural Wash" is a real brand name - "Wash" alone matches the bare word
    # 'wash', falsely triggering the many 'wash'-wins-over-serum/oil/etc. guards throughout this
    # file purely because of the brand name (e.g. "TNW ... Onion Oil + Hair Serum + Banana
    # Conditioner + Banana Shampoo" was losing both its Oil and Serum hits this way). Mirrors
    # the "Faces Canada" brand-collision fix above.
    (r'\bthe\s?natural\s?wash\b', 'thenaturalwashbrand'),
]

def normalize_compounds(text):
    text = re.sub(r'[-+]', ' ', text)
    text = re.sub(r'\s+', ' ', text)  # collapse multi-space artifacts from the substitution above
    for pattern, replacement in COMPOUND_SPLITS:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text

def has_word(text, phrase):
    """Word-boundary match with plural support (-s and -es)."""
    pattern = r'\b' + re.escape(phrase) + r'(?:es|s)?\b'
    return re.search(pattern, text, re.IGNORECASE) is not None

EXCLUDE_KEYWORDS = []

# Known salon/hair-specialist brands - used ONLY when "mask" has no other context clue.
# NOT exhaustive - built from what real data showed causing mis-tags, needs your review.
HAIR_ONLY_BRANDS = [
    "l'oreal professionnel", "loreal professionnel", "matrix", "biolage", "schwarzkopf",
    "livon", "streax", "indulekha", "kerastase", "wella", "tigi", "sunsilk", "clinic plus",
    "tresemme",
    # Added from a real-data audit of the Unmapped bucket - professional/specialist haircare
    # brands whose products (Treatment/Balm/Paste/Clay/Tonic/Concentrate/Leave-in, none of
    # which say "hair" literally) were falling through to Unmapped entirely. Used both by the
    # existing serum/mask fallbacks above and the brand-only last-resort fallback at the end of
    # _tag_sku_core.
    "aveda", "redken", "olaplex", "keune", "milkshake", "ryo", "cadiveu", "gk hair",
    "sebastian professional", "alan truman", "ikonic", "gorgio", "nish hair", "moroccanoil",
    "fibre clinix", "mise en scene", "hairmac", "keya seth", "the earth collective", "growus",
    "beauty garage", "biotop professional", "iluvia", "kama ayurveda", "kama ayurved",
    "philip b", "riyo herbs", "roots & herbs", "sachajuan", "shahnaz husain", "thriveco",
    "vaadi herbals", "vedic line", "xo curls", "she essentials", "soulflower", "fable & mane",
    "naturica", "ecovani", "global beauty secrets", "la'dor", "lador", "manetain",
    "style aromatherapy", "kera fine", "brillare", "daughter earth", "morpheme remedies",
    "richfeel", "protouch", "ustraa", "wahl", "hector professional", "elchim",
    # Added from a real-data audit of the remaining Unclassified bucket - hair-specialist
    # brands (mostly curly-hair-care and Japanese/Korean haircare lines) whose "Combo"/"Kit"/
    # "Duo"/"Routine"/"Therapy" names say neither "hair" nor any recognized product-format word.
    "fix my curls", "&honey", "sacred grove", "rahul phate", "sadhev", "indulgeo essentials",
    "moxie beauty", "ouai", "wishcare", "arata",
]

# Brands that sell (near-)exclusively colour cosmetics/makeup - mirrors HAIR_ONLY_BRANDS above.
# Used ONLY as a last-resort fallback (see the end of _tag_sku_core) when nothing else in this
# function matched at all - a real skincare/haircare product from a "mixed" brand on this list
# (e.g. Lakme, Maybelline) would already have resolved via its own keyword (moisturizer, serum,
# sunscreen, etc.) long before reaching this fallback, so including a mostly-but-not-purely-
# makeup brand here is safe by the same reasoning already accepted for HAIR_ONLY_BRANDS.
MAKEUP_ONLY_BRANDS = [
    "m.a.c", "mac cosmetics", "nars", "morphe", "smashbox", "stila cosmetics", "milk makeup",
    "nudestix", "anastasia beverly hills", "huda beauty", "too faced", "urban decay",
    "nyx professional makeup", "colorbar", "swiss beauty", "insight cosmetics", "miss claire",
    "blue heaven", "pac cosmetics", "makeup revolution", "revolution pro", "revolution relove",
    "l.a. girl", "l.a girl", "coloressence", "sugar cosmetics", "chambor", "typsy beauty",
    "fashion colour", "recode", "c2p pro", "inglot", "rom&nd", "kay beauty", "renee cosmetics",
    "charlotte tilbury", "benefit cosmetics", "maybelline", "glam21", "elle 18",
    "lotus make-up", "lotus makeup", "faces canada", "olivia cosmetics", "mars cosmetics",
    "staze", "charmacy milano", "iconic london", "e.l.f. cosmetics", "paese cosmetics",
    "character", "milagro beauty", "simihaze beauty", "suroskie", "daily life forever52",
    "iba cosmetics", "shryoan", "colors queen", "kiro", "gubb cosmetics",
]

# Brands that sell (near-)exclusively fragrance - mirrors HAIR_ONLY_BRANDS/MAKEUP_ONLY_BRANDS.
# Used ONLY as a last-resort fallback for gift-set/collection names with no other keyword at
# all (e.g. "Bella Vita Black Edition Gift Set"). Note: "Yardley London" was deliberately NOT
# added - real data showed it also sells bath/grooming kits (not fragrance-only), which this
# fallback's "kit"/"set" trigger words were wrongly claiming.
FRAGRANCE_ONLY_BRANDS = [
    "bella vita", "bellavita", "beverly hills polo club",
]

# Brands that sell (near-)exclusively men's shaving/grooming kits - same last-resort role.
# Note: "Kimirica" was deliberately NOT added - real data showed it's a general luxury bath &
# body gift brand (women's gift sets are the majority), not shaving-specific.
GROOMING_ONLY_BRANDS = [
    "bombay shaving company",
]

# Known K-beauty / sheet-mask-specialist brands - used ONLY when "mask" has no "sheet" word
# and no other context clue. NOT exhaustive.
SHEET_MASK_BRANDS = [
    "biodance", "mediheal", "dr.jart", "dr jart", "snp", "some by mi", "innisfree mask",
    "skin1004", "torriden", "isntree mask", "abib", "cosrx mask", "the face shop", "mirabelle",
]

# Known face-skincare-specialist brands. NOTE: this list is currently UNUSED in the live code -
# a later full-data audit showed the true bare-moisturizer default should be Face care (79% of
# clean samples), making this brand list redundant. Kept in case a future case needs it again.
FACE_SKINCARE_BRANDS = [
    "clinique", "dot & key", "dot and key", "conscious chemist", "dr. sheth", "dr sheth",
    "the derma co", "derma co", "foxtale", "deconstruct", "minimalist", "pilgrim",
    "re'equil", "reequil", "lakme",
]

# Exact-match memory for specific known products where the general engine logic is sound,
# but the SPECIFIC product needs remembered context that can't be derived from keywords alone.
# Sanity-checked against real-world plausibility before being added.
SKU_MEMORY = {
    'mirabelle korea bubble pop sparkling mask': ('Face Skin','Face cleansers','Face Pack'),
    # Sanity note: "Bubble-Pop Sparkling" describes a foaming/bubble mask format, functionally
    # a wash-off product - correctly differs from MIRABELLE KOREA's sheet-mask lines.
}

# Low-priority keywords whose real-data agreement rate with historical tags is high enough
# (>=95%, checked against the April Blinkit dataset) to trust as High confidence rather than
# the generic Medium every other low-priority keyword gets - promoted individually, not by
# moving priority position, so the existing "more specific word wins" ordering is untouched.
LOW_PRIORITY_HIGH_CONFIDENCE = {'perfume'}

# True color/shade words for the lip balm "implicit tint" rule - deliberately excludes
# flavor/scent words (mango, berry, cherry, peach, vanilla, coconut, chocolate, coffee,
# caramel, rose, etc.) which tested as unreliable color signals against real data.
# Explicit direction: strawberry and raspberry ARE included despite being flavor names - they
# genuinely carry a visible pink/red tint in real products, unlike the other flavor words.
LIP_BALM_COLOR_WORDS = {
    'red','pink','purple','mauve','ruby','nude','beige','maroon','wine','crimson',
    'burgundy','coral','magenta','fuchsia','lilac','lavender','blush','scarlet',
    'salmon','terracotta','yellow','strawberry','raspberry',
}

LOW_PRIORITY_KEYWORDS = {
    'glycerin': ('Face Skin','Face care','Glycerine'),
    'glycerine': ('Face Skin','Face care','Glycerine'),
    'essence': ('Face Skin','Face care','Face Serums/Essence'),
    'perfume': ('Fragrance','Perfume','Perfume'),
    'tan removal': ('Face Skin','Face cleansers','Face Pack'),
    'detan': ('Face Skin','Face cleansers','Face Pack'),
    'de tan': ('Face Skin','Face cleansers','Face Pack'),
    'd tan': ('Face Skin','Face cleansers','Face Pack'),
    # Bare SPF catch-all - only reached if nothing more specific matched above (checked after
    # DIRECT_KEYWORDS and CONTEXT_MATRIX), so a legitimate BB Cream/foundation/compact with SPF
    # is already claimed by its own rule before ever reaching this. Covers brand-style sun-care
    # naming with no generic format word at all (e.g. "Sunblock", "Sunspray", "Sun Aqua-Fresh").
    'spf': ('Face Skin','Sun care','Sunscreen'),
}

DIRECT_KEYWORDS = {
    'acne patch': ('Face Skin','Face Skin Others','Acne Patch/Nose Strips'),
    'after shave': ('Hygiene','Shaving','After Shaves'),
    'ampoule': ('Face Skin','Face care','Face Serums/Essence'),
    # FIX: "Foot File"/"Pumice Stone"/"Callus Remover" - a distinct foot-care tool category with
    # no other recognized keyword ("filer"/"foot filer" already existed as a near-miss spelling).
    'foot file': ('Others','Others Others','Pumice stones, callus removers, foot fillers'),
    'pumice': ('Others','Others Others','Pumice stones, callus removers, foot fillers'),
    'pumice stone': ('Others','Others Others','Pumice stones, callus removers, foot fillers'),
    'callus remover': ('Others','Others Others','Pumice stones, callus removers, foot fillers'),
    # FIX: "Cuticle Nipper"/"Nail File" - nail-care tools with no other recognized keyword.
    # Bare "Cuticle" itself is handled by a guarded check further below (excludes 'oil' - a
    # "Cuticle Oil" correctly stays in the Hair Oils bucket per real ground-truth data).
    'nail file': ('Others','Others Others','Cuticle Tools'),
    # FIX: Bigen's "Speedy Colour/Color" is a hair-dye product-line name with no other
    # recognized keyword ("color"/"colour" alone is too generic/risky to add as a blanket
    # keyword). Note: Biotique's "Herbcolor" was deliberately NOT added here - every real
    # example already says "Hair Color" explicitly elsewhere in the name, so it already
    # resolves correctly via the existing hair-colour fallback; adding it here would only
    # have redirected an already-correct result to a less certain one.
    'speedy colour': ('Hair colour','Creme','Permanent Colour'),
    'speedy color': ('Hair colour','Creme','Permanent Colour'),
    # FIX: "Sun Stick" is an established K-beauty sunscreen format (Embryolisse, Korean brands) -
    # no other recognizable keyword, was falling through entirely.
    'sun stick': ('Face Skin','Sun care','Sunscreen'),
    # FIX: "Nail Lacquer"/"Nail Colour"/"Nail Color" are common synonyms for nail polish that
    # weren't recognized - only 'nail paint'/'nail polish' were.
    'nail lacquer': ('Make up','Nail make-up','Nail Polish'),
    'nail colour': ('Make up','Nail make-up','Nail Polish'),
    'nail color': ('Make up','Nail make-up','Nail Polish'),
    # FIX: "Nail Bar" (Pep Nails' own product-line naming, e.g. "Lux Nail Bar"/"Mini Nail Bar")
    # is a nail-art kit, not a soap bar - checked before the general bare-'bar' guard below,
    # which would otherwise claim it.
    'nail bar': ('Make up','Nail make-up','Nail Art'),
    'attar': ('Fragrance','Perfume','Attar/Ittars'),
    # FIX: "Scalp Massager & Shampoo Brush" is ONE hair-cleansing tool, not a Shampoo product -
    # "Shampoo Brush" is the tool's own name. Checked before the 'shampoo'/'brush' CONTEXT_MATRIX
    # bases (mirrors the equivalent bundle_detect.py find_all_hits() guard).
    'shampoo brush': ('Others','Others Others','Hair Accessories'),
    # FIX: "Blush" names an actual finished cosmetic product - it was living in
    # LOW_PRIORITY_KEYWORDS (checked only after CONTEXT_MATRIX), so "Cream Blush"/"Serum
    # Blush"/"Gel Blush" were being swallowed by the generic bare cream/serum/gel defaults
    # before "blush" ever got a look-in. Promoted to a real DIRECT_KEYWORDS entry.
    'blush': ('Make up','Face make-up','Blushes'),
    'blusher': ('Make up','Face make-up','Blushes'),
    'bronzer': ('Make up','Face make-up','Bronzers'),
    # Bindi (self-adhesive forehead decoration) is a different item from sindoor (vermillion
    # powder/paste) but real historical tagging consistently buckets both under the same
    # "Sindoors" L3 - matched here for consistency.
    'bindi': ('Make up','Face make-up','Sindoors'),
    'baby bath': ('Others','Others Others','Baby Care'),
    'bath glove': ('Others','Others Others','Others'),
    'bath mitt': ('Others','Others Others','Others'),
    'bath salt': ('Hygiene','Body wash','Bath Salts'),
    'bathing bar': ('Hygiene','Soaps','Solid Soap Bars'),
    'beard color': ('Hair colour','Creme','Crème'),
    'beard colour': ('Hair colour','Creme','Crème'),
    'beauty blender': ('Make up','Make up Others','Make up Sponges/ Beauty Blenders'),
    'body butter': ('Body Skin','Body care','Body Butter'),
    'body glitter': ('Make up','Body make-up','Shimmer & Glitter'),
    'body shimmer': ('Make up','Body make-up','Shimmer & Glitter'),
    'brow pencil': ('Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)'),
    'bun pin': ('Others','Others Others','Others'),
    'bun shaper': ('Others','Others Others','Others'),
    'chapstick': ('Make up','Lip make-up','Chapstick'),
    'cleansing balm': ('Face Skin','Face cleansers','Cleansing Balms'),
    'cleansing bar': ('Hygiene','Soaps','Solid Soap Bars'),
    'cleansing foam': ('Face Skin','Face cleansers','Face Wash/Face Cleansers'),
    'cleansing milk': ('Face Skin','Face cleansers','Cleansing Milk'),
    'comb': ('Others','Others Others','Hair Accessories'),
    'compact powder': ('Make up','Face make-up','Compact Powder'),
    'concealer': ('Make up','Face make-up','Concealer'),
    # FIX: "Conceal" (shortened form, e.g. "L.A. Girl HD Pro Conceal") is the same product as
    # "Concealer" - has_word's own word-boundary already keeps this from matching "concealer"
    # itself, so the two entries never compete.
    'conceal': ('Make up','Face make-up','Concealer'),
    'cotton ball': ('Make up','Make up Others','Others'),
    'cotton pad': ('Make up','Make up Others','Others'),
    'dental floss': ('Others','Others Others','Oral Care'),
    'deo': ('Hygiene','Deos & Antiperspirants','Deos'),
    'deo stick': ('Hygiene','Deos & Antiperspirants','Deo Sticks'),
    'derma roller': ('Hair Care','Hair Care Others','Derma Roller'),
    'derma stamp': ('Hair Care','Hair Care Others','Derma Roller'),
    'elbow balm': ('Others','Others Others','Others'),
    'epsom salt': ('Hygiene','Body wash','Bath Salts'),
    'eye liner': ('Make up','Eye make-up','Eyeliners'),
    'eye shadow palette': ('Make up','Eye make-up','Eyeshadow'),
    'eyebrow palette': ('Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)'),
    'eyebrow pencil': ('Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)'),
    'eyebrow enhancer': ('Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)'),
    'eyelash': ('Make up','Make up Others','False Lashes'),
    # Note: bare "Lash" and "Lip Care"/"Lip Treatment" were tried and reverted - real-data audit
    # found both too generic/collision-prone ("Lash" matches brand names like "Cuffs N Lashes"
    # and lash-brush tools; "Lip Care"/"Lip Treatment" are marketing phrases used across Lip
    # Balm, Tinted Lip Balm, and SPF Lip Treatment products that already have their own more
    # specific, correct keywords).
    'eyeliner': ('Make up','Eye make-up','Eyeliners'),
    'eyeshadow palette': ('Make up','Eye make-up','Eyeshadow'),
    'face bleach': ('Face Skin','Face care','Bleaches'),
    'face glitter': ('Make up','Face make-up','Shimmer & Glitter'),
    'face pack': ('Face Skin','Face cleansers','Face Pack'),
    'face palette': ('Make up','Make up Others','Make-Up Palettes'),
    'face shimmer': ('Make up','Face make-up','Shimmer & Glitter'),
    'facial kit': ('Face Skin','Face care','Facial Kit'),
    'flosser': ('Others','Others Others','Oral Care'),
    'foot filer': ('Others','Others Others','Others'),
    'glitter': ('Make up','Face make-up','Shimmer & Glitter'),
    'gua sha': ('Face Skin','Face Skin Others','Face Tools (Gua Sha, Rollers)'),
    'hair band': ('Others','Others Others','Hair Accessories'),
    'hair bleach': ('Hair colour','Hair colour Others','Hair Bleach'),
    'hair butter': ('Hair Care Others','Styling','Hair Butter'),
    'hair comb': ('Others','Others Others','Hair Accessories'),
    'hair extension': ('Others','Others Others','Hair Accessories'),
    'hair fiber': ('Hair colour','Hair colour Others','Hair Fibers'),
    'hair fibre': ('Hair colour','Hair colour Others','Hair Fibers'),
    'hair finishing stick': ('Hair Care Others','Styling','Hair Finishing Stick'),
    'hairline': ('Hair colour','Hair colour Others','Hair Mascaras & Root Touch Ups'),
    'hair pin': ('Others','Others Others','Hair Accessories'),
    'hair clip': ('Others','Others Others','Hair Accessories'),
    'hair chain': ('Others','Others Others','Hair Accessories'),
    'hair wrap': ('Others','Others Others','Hair Accessories'),
    'hair towel': ('Others','Others Others','Hair Accessories'),
    'rubber band': ('Others','Others Others','Hair Accessories'),
    'hair removal foam': ('Hygiene','Shaving','Depilatory'),
    'hair tie': ('Others','Others Others','Hair Accessories'),
    'headband': ('Others','Others Others','Hair Accessories'),
    # FIX: "Wet Detangler"/"The Ultimate Detangler" (Tangle Teezer's own naming) doesn't say
    # "brush" at all, so the brush-modifier fix doesn't reach it. 29+ real examples.
    'detangler': ('Others','Others Others','Hair Accessories'),
    'detangling brush': ('Others','Others Others','Hair Accessories'),
    # A maang tikka is worn attached to the hair parting - grouped with the other hair jewelry
    # accessories (hairband/hairclip/hairpin) already in this list, not as loose jewelry.
    'maang tikka': ('Others','Others Others','Hair Accessories'),
    'maang tika': ('Others','Others Others','Hair Accessories'),
    'maangtika': ('Others','Others Others','Hair Accessories'),
    'hair bonnet': ('Others','Others Others','Hair Accessories'),
    'hair claw': ('Others','Others Others','Hair Accessories'),
    'claw clip': ('Others','Others Others','Hair Accessories'),
    'hair tiara': ('Others','Others Others','Hair Accessories'),
    'hair vine': ('Others','Others Others','Hair Accessories'),
    'fascinator': ('Others','Others Others','Hair Accessories'),
    'hair clutcher': ('Others','Others Others','Hair Accessories'),
    'hair bow': ('Others','Others Others','Hair Accessories'),
    'alice band': ('Others','Others Others','Hair Accessories'),
    'head band': ('Others','Others Others','Hair Accessories'),
    'head massager': ('Others','Others Others','Hair Accessories'),
    'scalp massager': ('Others','Others Others','Hair Accessories'),
    'pony extension': ('Others','Others Others','Hair Accessories'),
    'pony tail extension': ('Others','Others Others','Hair Accessories'),
    'hair topper': ('Others','Others Others','Hair Accessories'),
    'scalp topper': ('Others','Others Others','Hair Accessories'),
    'bun cover': ('Others','Others Others','Hair Accessories'),
    'messy bun': ('Others','Others Others','Hair Accessories'),
    'hair donut': ('Others','Others Others','Hair Accessories'),
    'face sponge': ('Make up','Make up Others','Make up Sponges/ Beauty Blenders'),
    'makeup blender': ('Make up','Make up Others','Make up Sponges/ Beauty Blenders'),
    'makeup removal towel': ('Make up','Make-up cleansing','Makeup Cleansing Water'),
    'makeup removing towel': ('Make up','Make-up cleansing','Makeup Cleansing Water'),
    'makeup removing cloth': ('Make up','Make-up cleansing','Makeup Cleansing Water'),
    # FIX: bare "Illuminator"/"Luminizer"/"Brightener" (without the word "highlighter" itself)
    # is the exact same product family, just a marketing synonym - a real-data audit found 10+
    # examples (Insight Cosmetics, MARS, Note, Rare Beauty, NARS, LoveChild Masaba) with no
    # other keyword to catch them.
    'illuminator': ('Make up','Face make-up','Highlighters & Illuminators'),
    'luminizer': ('Make up','Face make-up','Highlighters & Illuminators'),
    'brightener': ('Make up','Face make-up','Highlighters & Illuminators'),
    # FIX: bare "Corrector" (without "colour"/"color" prefix) is the same colour-correcting
    # product family as the "Colour Corrector" SPECIFICITY_PHRASE above.
    'corrector': ('Make up','Face make-up','Colour Corrector'),
    # FIX: a cushion-COMPACT (Korean beauty format: liquid foundation in a sponge-applicator
    # compact) is a liquid-foundation format, not a brush or a powder.
    'cushion compact': ('Make up','Face make-up','Foundation'),
    # FIX: "Makeup Base"/"Face Base" is common Korean/Asian-beauty terminology for a Primer
    # (Paese, Miss Claire, Character, Fashion Colour, TFIT) - real-data audit found a dozen+
    # such products falling to Unmapped since "base" alone is too generic to add on its own.
    'makeup base': ('Make up','Face make-up','Primers'),
    'make up base': ('Make up','Face make-up','Primers'),
    'face base': ('Make up','Face make-up','Primers'),
    # FIX: "Skinfinish" is MAC's own coined term for its finishing/highlighting powder line
    # (Mineralize Skinfinish, Extra Dimension Skinfinish) - a proprietary word, safe to
    # generalize since it has no other meaning to collide with.
    'skinfinish': ('Make up','Face make-up','Highlighters & Illuminators'),
    # FIX: "Root Touch Up"/"Instant Root Touch Up" implies hair-colour root retouching even
    # without literally saying "hair colour"/"hair color" (which the existing fallback below
    # requires) - reuses that exact same destination.
    'root touch up': ('Hair colour','Hair colour Others','Hair Mascaras & Root Touch Ups'),
    'root touch-up': ('Hair colour','Hair colour Others','Hair Mascaras & Root Touch Ups'),
    'shower cap': ('Others','Others Others','Hair Accessories'),
    'hair coloring tool': ('Others','Others Others','Hair Accessories'),
    'hair colouring tool': ('Others','Others Others','Hair Accessories'),
    'henna': ('Hair colour','Heenas','Natural Henna'),
    'highlighter': ('Make up','Face make-up','Highlighters & Illuminators'),
    'ittar': ('Fragrance','Perfume','Attar/Ittars'),
    'kajal': ('Make up','Eye make-up','Kajal & Kohls'),
    'kasturi haldi': ('Face Skin','Face cleansers','Face Pack'),
    'knee balm': ('Others','Others Others','Others'),
    'kohl': ('Make up','Eye make-up','Kajal & Kohls'),
    'kumkum': ('Others','Others Others','Others'),
    'lip butter': ('Face Skin','Face Skin Others','Lip Butters'),
    'lip crayon': ('Make up','Lip make-up','Lip Crayon'),
    'lip glaze': ('Make up','Lip make-up','Lip Glosses/Glaze'),
    'lip gloss': ('Make up','Lip make-up','Lip Glosses/Glaze'),
    'lip liner': ('Make up','Lip make-up','Lip Liners'),
    'lip plumper': ('Make up','Lip make-up','Lip Plumpers'),
    'lip stain': ('Make up','Lip make-up','Lip Stains'),
    'lipstick': ('Make up','Lip make-up','Lipsticks'),
    'lipstick primer': ('Make up','Lip make-up','Lipstick Primers'),
    'liquid lip color': ('Make up','Lip make-up','Liquid Lip Colour'),
    'liquid lip colour': ('Make up','Lip make-up','Liquid Lip Colour'),
    'makeup fixer': ('Make up','Face make-up','Makeup Fixer'),
    'makeup kit': ('Make up','Make up Others','Make-Up Kits'),
    'makeup palette': ('Make up','Make up Others','Make-Up Palettes'),
    'makeup pouch': ('Make up','Make up Others','Others'),
    'makeup puff': ('Make up','Make up Others','Make up Sponges/ Beauty Blenders'),
    'makeup set': ('Make up','Make up Others','Make-Up Kits'),
    'mascara': ('Make up','Eye make-up','Mascaras'),
    'mehendi': ('Hair colour','Heenas','Kali Mehendi'),
    'micellar water': ('Face Skin','Face care','Micellar Water'),
    'mouthwash': ('Others','Others Others','Oral Care'),
    'mulethi': ('Face Skin','Face cleansers','Face Pack'),
    'multani mitti': ('Face Skin','Face cleansers','Face Pack'),
    'multani powder': ('Face Skin','Face cleansers','Face Pack'),
    'nail buffer': ('Others','Others Others','Others'),
    'nail filer': ('Others','Others Others','Others'),
    'nail glue': ('Make up','Nail make-up','Nail Art'),
    'nail lamp': ('Others','Others Others','Others'),
    'nail sticker': ('Make up','Nail make-up','Nail Art'),
    'nasal scissor': ('Others','Others Others','Others'),
    'neem powder': ('Face Skin','Face cleansers','Face Pack'),
    'nose strip': ('Face Skin','Face Skin Others','Acne Patch/Nose Strips'),
    'palette': ('Make up','Make up Others','Make-Up Palettes'),
    'peeling solution': ('Face Skin','Face cleansers','Facial Scrubs & Polishes'),
    'powder puff': ('Make up','Make up Others','Make up Sponges/ Beauty Blenders'),
    'indigo powder': ('Hair colour','Heenas','Natural Henna'),
    'indigo': ('Hair colour','Heenas','Natural Henna'),
    'cologne': ('Fragrance','Eau De Cologne','Eau De Cologne'),
    'shikakai': ('Hair Care','Shampoo','Hair Cleansers, Cleansing Powders'),
    'bhringraj powder': ('Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums'),
    'amla powder': ('Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums'),
    'translucent powder': ('Make up','Face make-up','Setting Powder/Loose Powder'),
    'beard oil': ('Hair Care Others','Hair Oils','Hair Oils'),
    'petroleum jelly': ('Body Skin','Body care','Petroleum Jelly'),
    'pimple patch': ('Face Skin','Face Skin Others','Acne Patch/Nose Strips'),
    'pumice stone': ('Others','Others Others','Others'),
    'rose water': ('Face Skin','Face care','Rose Water'),
    'nasal safety scissors': ('Others','Others Others','Others'),
    'grooming scissors': ('Others','Others Others','Others'),
    'scissor': ('Others','Others Others','Others'),
    'travel mirror': ('Others','Others Others','Others'),
    'makeup mirror': ('Others','Others Others','Others'),
    'make up mirror': ('Others','Others Others','Others'),
    'ice water bowl': ('Face Skin','Face Skin Others','Face Tools (Gua Sha, Rollers)'),
    'ice bowl': ('Face Skin','Face Skin Others','Face Tools (Gua Sha, Rollers)'),
    'makeup organizer': ('Make up','Make up Others','Others'),
    'make up organizer': ('Make up','Make up Others','Others'),
    'cosmetic organizer': ('Make up','Make up Others','Others'),
    'exfoliating glove': ('Others','Others Others','Bath Accessories (Loofahs, Body Scrubbers etc.)'),
    'gajra': ('Others','Others Others','Hair Accessories'),
    'hair bun': ('Others','Others Others','Hair Accessories'),
    'juda': ('Others','Others Others','Hair Accessories'),
    'nail enamel': ('Make up','Nail make-up','Nail Polish'),
    'stick on nail': ('Make up','Nail make-up','Nail Art'),
    'nail adhesive': ('Make up','Nail make-up','Nail Art'),
    'lip color': ('Make up','Lip make-up','Lipsticks'),
    'lip colour': ('Make up','Lip make-up','Lipsticks'),
    'lipcolor': ('Make up','Lip make-up','Lipsticks'),
    'makeup sponge': ('Make up','Make up Others','Make up Sponges/ Beauty Blenders'),
    'make up sponge': ('Make up','Make up Others','Make up Sponges/ Beauty Blenders'),
    'pomade': ('Hair Care Others','Styling','Hair Butter'),
    'compact': ('Make up','Face make-up','Compact Powder'),
    'lipscreen': ('Make up','Lip make-up','Chapstick'),
    'beard styling': ('Hair Care Others','Styling','Hair Butter'),
    'face tissue': ('Others','Others Others','Others'),
    'facial tissue': ('Others','Others Others','Others'),
    'cleansing pad': ('Face Skin','Face cleansers','Face Wash/Face Cleansers'),
    'sharpener': ('Make up','Make up Others','Others'),
    'blindfold': ('Others','Others Others','Others'),
    'breast lift cup': ('Others','Others Others','Others'),
    'breast lift cups': ('Others','Others Others','Others'),
    'rosemary water': ('Hair Care Others','Hair Oils','Hair Oils'),
    'rosemary leaves': ('Hair Care Others','Hair Oils','Hair Oils'),
    'body glue': ('Others','Others Others','Others'),
    'teeth whitener': ('Others','Others Others','Oral Care'),
    'maternity pad': ('Others','Others Others','Sanitary Pads'),
    'maternity nursing pad': ('Others','Others Others','Sanitary Pads'),
    'maternity panty': ('Others','Others Others','Period Panties'),
    'maternity brief': ('Others','Others Others','Period Panties'),
    'nail file': ('Others','Others Others','Cuticle Tools'),
    'nail cutter': ('Others','Others Others','Cuticle Tools'),
    'nail pusher': ('Others','Others Others','Cuticle Tools'),
    'fitkari': ('Hygiene','Shaving','After Shaves'),
    'alum bar': ('Hygiene','Shaving','After Shaves'),
    'alum': ('Hygiene','Shaving','After Shaves'),
    'mouth freshener': ('Others','Others Others','Oral Care'),
    'bubble bath': ('Hygiene','Body wash','Bath Salts'),
    'body yogurt': ('Body Skin','Body care','Body Lotions'),
    'disposal bag': ('Others','Others Others','Sanitary Pads'),
    'sanitary disposal': ('Others','Others Others','Sanitary Pads'),
    'safety pin': ('Others','Others Others','Others'),
    'sandalwood powder': ('Face Skin','Face cleansers','Face Pack'),
    'scalp massager': ('Others','Others Others','Hair Accessories'),
    'ubtan powder': ('Face Skin','Face cleansers','Face Pack'),
    'ubtan': ('Face Skin','Face cleansers','Face Pack'),
    'orange peel powder': ('Face Skin','Face cleansers','Face Pack'),
    'beetroot powder': ('Face Skin','Face cleansers','Face Pack'),
    'serum bar': ('Hygiene','Soaps','Solid Soap Bars'),
    'shave foam': ('Hygiene','Shaving','Shaving foams/ Gels/ Creams'),
    'shaving foam': ('Hygiene','Shaving','Shaving foams/ Gels/ Creams'),
    'shaving kit': ('Hygiene','Shaving','Shaving Kit'),
    'sindoor': ('Make up','Face make-up','Sindoors'),
    'strobe cream': ('Make up','Face make-up','Highlighters & Illuminators'),
    'sweat pad': ('Others','Others Others','Others'),
    'talc': ('Hygiene','Body care (Talc)','Talcum Powders'),
    'threading tool': ('Others','Others Others','Others'),
    'toner': ('Face Skin','Face care','Toners'),
    'tongue cleaner': ('Others','Others Others','Oral Care'),
    'toothbrush': ('Others','Others Others','Oral Care'),
    'toothpaste': ('Others','Others Others','Oral Care'),
    'travel bottle': ('Make up','Make up Others','Others'),
    'travel bottle set': ('Make up','Make up Others','Others'),
    'travel pouch': ('Make up','Make up Others','Others'),
    'turmeric powder': ('Face Skin','Face cleansers','Face Pack'),
    'tweezer': ('Others','Others Others','Others'),
    'urination device': ('Others','Others Others','Others'),
    'vanity mirror': ('Others','Others Others','Others'),
    'water flosser': ('Others','Others Others','Oral Care'),
}
CONTEXT_MATRIX = {
    'bath brush': [(None,'Others','Others Others','Bath Accessories (Loofahs, Body Scrubbers etc.)','High')],
    'brush': [('makeup','Make up','Make up Others','Make-Up Brush Sets','High'),('make up','Make up','Make up Others','Make-Up Brush Sets','High'),('tooth','Others','Others Others','Oral Care','High'),
        # FIX: "Brush Head"/"Brush Heads" is electric-toothbrush replacement-head terminology
        # regardless of brand - doesn't always say "tooth" literally.
        ('brush head','Others','Others Others','Oral Care','High'),
        ('hair','Others','Others Others','Hair Accessories','High'),
        # FIX: a bath/shower/body brush or a shaving brush was falling through to the bare
        # 'brush' default (Make-Up Brush Sets) purely because no more specific modifier existed
        # for these - neither is a makeup tool.
        ('shaving','Hygiene','Shaving','Shaving Kit','High'),
        ('shave','Hygiene','Shaving','Shaving Kit','High'),
        ('bath','Others','Others Others','Bath Accessories (Loofahs, Body Scrubbers etc.)','High'),
        ('shower','Others','Others Others','Bath Accessories (Loofahs, Body Scrubbers etc.)','High'),
        ('body','Others','Others Others','Bath Accessories (Loofahs, Body Scrubbers etc.)','High'),
        # FIX: these are all hairdressing/styling tool TYPES (blow-drying, detangling, and
        # brush-shape names used almost exclusively for hair), not makeup brushes - were falling
        # through to the bare 'brush' default (Make-Up Brush Sets) since none say "hair" literally.
        ('blow dry','Others','Others Others','Hair Accessories','High'),
        ('blow drying','Others','Others Others','Hair Accessories','High'),
        ('detangling','Others','Others Others','Hair Accessories','High'),
        ('paddle','Others','Others Others','Hair Accessories','High'),
        ('cushion','Others','Others Others','Hair Accessories','High'),
        ('flat','Others','Others Others','Hair Accessories','High'),
        ('styling','Others','Others Others','Hair Accessories','High'),
        ('round','Others','Others Others','Hair Accessories','High'),
        (None,'Make up','Make up Others','Make-Up Brush Sets','Medium')],
    # FIX: "Makeup Removal Oil Cleanser" - the 'cleanser' base is longer than 'oil' so it was
    # checked first and its bare default won before 'oil'+'makeup' ever got a look-in.
    # FIX: bare 'makeup' was too broad - collided with brand names like "Makeup Revolution" or
    # "NYX Professional Makeup" that have nothing to do with makeup REMOVAL. Narrowed to the
    # specific "makeup remover"/"makeup removal" phrasing that actually signals the product.
    'cleanser': [('hair','Hair Care','Shampoo','Hair Cleansers, Cleansing Powders','High'),('makeup remover','Make up','Make-up cleansing','Makeup Cleansing Oils','High'),('makeup removal','Make up','Make-up cleansing','Makeup Cleansing Oils','High'),(None,'Face Skin','Face cleansers','Face Wash/Face Cleansers','High')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Clip" with no
    # nail/hair/claw modifier defaults to the single most common real-catalog use (a hair clip),
    # flagged Low so it's still surfaced for manual review rather than silently guessed.
    'clip': [('nail','Make up','Nail make-up','Nail Art','Medium'),('hair','Others','Others Others','Hair Accessories','High'),('claw','Others','Others Others','Hair Accessories','High'),(None,'Others','Others Others','Hair Accessories','Low')],
    'compact powder': [(None,'Make up','Face make-up','Compact Powder','Medium')],
    'conditioner': [('leave-in','Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners','High'),('leave in','Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners','High'),(None,'Hair Care','Rinse-Off Conditioners','Rinse Off Conditioners','High')],
    # FIX: "Cream Shadow Stick" is an eyeshadow in cream format, not a skincare eye cream -
    # checked before 'eye' since a genuine eyeshadow-stick SKU rarely also says "eye" literally.
    'cream': [('shadow','Make up','Eye make-up','Eyeshadow','High'),('eye','Face Skin','Face care','Eye Creams','High'),('bb cream','Make up','Face make-up','Bb Creams','High'),('cc cream','Make up','Face make-up','CC Creams','High'),('dd cream','Make up','Face make-up','DD Creams','High'),('deo','Hygiene','Deos & Antiperspirants','Deo Creams','High'),
        # FIX: has_word() is a whole-word match, so 'deo' alone never matches inside the full
        # word "Deodorant" - needed as its own modifier for "Deodorant Cream" SKUs.
        ('deodorant','Hygiene','Deos & Antiperspirants','Deo Creams','High'),
        ('shaving','Hygiene','Shaving','Shaving foams/ Gels/ Creams','High'),('removal','Hygiene','Shaving','Depilatory','High'),('styling','Hair Care Others','Styling','Styling Cream','High'),('curl','Hair Care Others','Styling','Styling Cream','High'),('cleansing','Make up','Make-up cleansing','Makeup Cleansing Creams','High'),('lip','Make up','Lip make-up','Chapstick','High'),('spf','Face Skin','Sun care','Sunscreen','High'),('hand','Body Skin','Body care','Hand Creams, Lotions, masks','High'),('foot','Body Skin','Foot care','Foot Creams & Lotions','High'),
        # FIX: "Night Cream"/"Day Cream" are unambiguous face-cream-only terms - promotes what
        # the bare default already guesses (Face Creams) from Medium to High confidence. Uses
        # the adjacent phrase, not bare "day", so "All Day Wear Cream Foundation" isn't caught.
        ('night cream','Face Skin','Face care','Face Creams','High'),
        ('day cream','Face Skin','Face care','Face Creams','High'),
        ('face','Face Skin','Face care','Face Creams','High'),('body','Body Skin','Body care','Body Creams','High'),
        # FIX: these words only ever describe a body-area treatment (rash, stretch marks,
        # piles/hemorrhoids, a massage cream, wart removal) - never a face product - so they
        # should win over the generic bare default below, which was defaulting them all to
        # Face Creams just because no explicit "body"/"face" word was also present.
        ('rash','Body Skin','Body care','Body Creams','High'),
        ('stretch mark','Body Skin','Body care','Body Creams','High'),
        ('belly','Body Skin','Body care','Body Creams','High'),
        ('piles','Body Skin','Body care','Body Creams','High'),
        ('hemorrhoid','Body Skin','Body care','Body Creams','High'),
        ('haemorrhoid','Body Skin','Body care','Body Creams','High'),
        ('massage','Body Skin','Body care','Body Creams','High'),
        ('wart','Body Skin','Body care','Body Creams','High'),
        (None,'Face Skin','Face care','Face Creams','Medium')],
    # FIX: 'creme' (the French spelling) was missing several modifiers 'cream' already has -
    # "Lip Creme"/"Hand & Nail Creme"/"Curl Defining Creme" were falling to the bare Face Creams
    # default just because of spelling. Also adds 'night'/'day' - unambiguous face-cream-only
    # terms - as a High-confidence promotion of what the bare default already guesses.
    'creme': [('hair colour','Hair colour','Creme','Crème','High'),('hair color','Hair colour','Creme','Crème','High'),('bleach','Face Skin','Face care','Bleaches','Medium'),('eye','Face Skin','Face care','Eye Creams','High'),('sunscreen','Face Skin','Sun care','Sunscreen','High'),('body lotion','Body Skin','Body care','Body Lotions','High'),
        ('lip','Make up','Lip make-up','Chapstick','High'),
        ('hand','Body Skin','Body care','Hand Creams, Lotions, masks','High'),
        ('foot','Body Skin','Foot care','Foot Creams & Lotions','High'),
        ('curl','Hair Care Others','Styling','Hair Lotions & Tonics','High'),
        ('night creme','Face Skin','Face care','Face Creams','High'),
        ('day creme','Face Skin','Face care','Face Creams','High'),
        ('face','Face Skin','Face care','Face Creams','High'),('body','Body Skin','Body care','Body Creams','High'),(None,'Face Skin','Face care','Face Creams','Medium')],
    'deodorant': [('shoe','Others','Others Others','Shoe Deodorant','High'),(None,'Hygiene','Deos & Antiperspirants','Deos','High')],
    'foundation': [(None,'Make up','Face make-up','Foundation','High')],
    'gel': [
        # FIX: "Gel Polish"/"Cat Eye Gel Polish" is nail polish in gel format - checked before
        # 'eye', which would otherwise falsely match the "Cat Eye"/"Cat-Eye" nail-polish FINISH
        # name (a marketing term, not an actual eye-care product) and misroute to Eye Gels.
        # Many of these SKUs never say the word "nail" at all (just "Gel Polish"), so 'polish'
        # needs its own modifier rather than relying on the existing 'nail' one below.
        ('polish','Make up','Nail make-up','Nail Gel','High'),
        # FIX: "Gel Eye Pencil"/"Gel Liner"/"Eyebrow Gel"/"Brow Gel" are eye-makeup products
        # (an eyeliner or brow-setting product in gel format) - checked before 'eye', which
        # would otherwise misread "Gel Eye Pencil" as a generic skincare Eye Gel.
        ('lip liner','Make up','Lip make-up','Lip Liners','High'),
        ('lip pencil','Make up','Lip make-up','Lip Liners','High'),
        ('liner','Make up','Eye make-up','Eyeliners','High'),
        ('pencil','Make up','Eye make-up','Eyeliners','High'),
        ('eyebrow','Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)','High'),
        ('brow','Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)','High'),
        # FIX: "Curls Gel"/"Curl Gel"/"Curly Hair Gel" is a hair-styling product for curl
        # definition - doesn't always say the word "hair" explicitly, so needs its own modifier
        # rather than relying on 'hair' alone. has_word()'s plural support covers "curls" too.
        ('curl','Hair Care Others','Styling','Hair Gels','High'),
        # FIX: "Hard Hold Gel"/"Strong Hold Gel" is hair-styling terminology (hold = how
        # firmly the style is set) - doesn't always say "hair" explicitly.
        ('hold','Hair Care Others','Styling','Hair Gels','High'),
        ('eye','Face Skin','Face care','Eye Gels','High'),('hair','Hair Care Others','Styling','Hair Gels','High'),('shower','Hygiene','Body wash','Body Wash Gels','High'),('shaving','Hygiene','Shaving','Shaving foams/ Gels/ Creams','High'),
        # FIX: "Shave Gel" (shorter form of "Shaving Gel") - same product, mirrors the existing
        # shaving/shave modifier pair already used for 'brush'.
        ('shave','Hygiene','Shaving','Shaving foams/ Gels/ Creams','High'),
        ('nail','Make up','Nail make-up','Nail Gel','High'),('cleansing','Make up','Make-up cleansing','Makeup Cleansing Gels','High'),('body wash','Hygiene','Body wash','Body Wash Gels','High'),('bar','Hygiene','Soaps','Solid Soap Bars','High'),('peeling','Face Skin','Face cleansers','Facial Scrubs & Polishes','High'),('lip','Make up','Lip make-up','Chapstick','High'),('spf','Face Skin','Sun care','Sunscreen','High'),
        # FIX: "massage"/"post waxing" are decisive body-area signals, mirroring the same
        # modifiers already added to 'cream'. Uses 'wax' (not 'waxing') since COMPOUND_SPLITS
        # already normalizes "waxing" -> "wax" before this check ever runs.
        ('massage','Body Skin','Body care','Body Creams','High'),
        ('post wax','Body Skin','Body care','Body Creams','High'),
        ('body','Body Skin','Body care','Body Creams','High'),(None,'Face Skin','Face care','Moisturisers','Medium')],
    'lip balm': [('tinted','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),('tint','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),('colored','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),('coloured','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),('colour','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),('color','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        # Explicit direction: a genuine COLOR/shade name implies tinted, even with no literal
        # "tinted"/"tint"/"colour" word - but FLAVOR/scent names (strawberry, mango, berry,
        # cherry, peach, vanilla, rose, coconut, chocolate, coffee, caramel, etc.) do NOT count
        # and stay Non-Tinted (checked and confirmed against real data: those flavor words are
        # not reliable color signals, but true color names are the deliberate call here).
        ('red','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('pink','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('purple','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('mauve','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('ruby','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('nude','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('beige','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('maroon','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('wine','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('crimson','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('burgundy','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('coral','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('magenta','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('fuchsia','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('lilac','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('lavender','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('blush','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('scarlet','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('salmon','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('terracotta','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('yellow','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        # Explicit direction: strawberry/raspberry genuinely carry a visible pink/red tint in
        # real products, unlike other flavor names - treated as color signals despite being
        # flavor words.
        ('strawberry','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('raspberry','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        # Explicit direction: "Gloss"/"Glow"/"Glowy" lip balms are makeup-adjacent tinted/sheen
        # products, not plain balms.
        ('gloss','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('glossy','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('glow','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('glowy','Make up','Lip make-up','Tinted/Colored Lip Balms','High'),
        ('spf','Make up','Lip make-up','Chapstick','High'),(None,'Face Skin','Face Skin Others','Balms (Non-Tinted)','Medium')],
    'loofah': [(None,'Others','Others Others','Bath Accessories (Loofahs, Body Scrubbers etc.)','High')],
    # FIX: "Facial Lotion" doesn't match the existing 'face' modifier (different word) - fell
    # through to the bare Body Lotions default.
    'lotion': [('facial','Face Skin','Face care','Face Lotions','High'),('body','Body Skin','Body care','Body Lotions','High'),('face','Face Skin','Face care','Face Lotions','High'),('foot','Body Skin','Foot care','Foot Creams & Lotions','High'),('hair','Hair Care Others','Styling','Hair Lotions & Tonics','High'),('shave','Hygiene','Shaving','Shave balms/ lotions','High'),(None,'Body Skin','Body care','Body Lotions','Medium')],
    # FIX: "Fragrance Mist"/"Perfume Mist" is unambiguously a body mist - doesn't say "body"
    # literally, so it was falling to the bare Ambiguous default (which then let a fallback
    # substitution stitch together a nonsensical historical L1/L2 combo for these SKUs).
    # FIX: "Setting Mist"/"Fixing Mist"/"Prep & Set Mist" is the exact same Makeup Setting Spray
    # product family as its 'spray'-worded equivalent (see 'spray' base above) - had NO
    # makeup-setting modifier at all, so every one of these fell to the bare default.
    # FIX: "Underarm Mist" is a deodorant/antiperspirant in mist format, not a generic Face Mist
    # - checked before the bare catchall. "Lip Mist" is a real, modern tinted-lip-product format
    # (dewy lip tint in spray/mist form), not a face mist either.
    'mist': [('body','Fragrance','Perfume','Body Mists','High'),('fragrance','Fragrance','Perfume','Body Mists','High'),('perfume','Fragrance','Perfume','Body Mists','High'),
        ('setting','Make up','Face make-up','Makeup Setting Spray','High'),
        ('fixer','Make up','Face make-up','Makeup Setting Spray','High'),
        ('fixing','Make up','Face make-up','Makeup Setting Spray','High'),
        ('prep','Make up','Face make-up','Makeup Setting Spray','High'),
        ('underarm','Hygiene','Deos & Antiperspirants','Deodrant Sprays','High'),
        ('under arm','Hygiene','Deos & Antiperspirants','Deodrant Sprays','High'),
        ('lip','Make up','Lip make-up','Lip & Cheek Stains & Tints','High'),
        ('face','Face Skin','Face care','Face Mists','High'),('hair','Hair Care Others','Styling','Hair Sprays & Mists','High'),
        # FIX: per direction, never leave a bare match fully blank - a bare "Mist" with none of
        # the above modifiers defaults to the generic Face Mist use, flagged Low for review.
        (None,'Face Skin','Face care','Face Mists','Low')],
    # FIX: SPF is a decisive, explicit claim per direction ("... other face cream with SPF is
    # sunscreen") - moved before 'body'/'face' so a coincidental brand-name match (e.g. "Better
    # Body Bombay ... Moisturizer With Sunshield SPF 30") doesn't let 'body' win first. 'tinted'
    # still wins over 'spf' when both are present, consistent with the lip-balm precedent.
    # FIX: "Leave-In Moisturizer/Moisturiser" is functionally the same product as a leave-in
    # conditioner (both are haircare, rinse-free, leave-in-format products) - checked before
    # 'face'/'body' since "leave in" is a haircare-specific format, not a texture descriptor.
    'moisturiser': [('leave-in','Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners','High'),('leave in','Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners','High'),('tinted','Make up','Face make-up','Tinted Moisturisers','High'),('spf','Face Skin','Sun care','Sunscreen','High'),('face','Face Skin','Face care','Moisturisers','High'),('body','Body Skin','Body care','Body Lotions','High'),(None,'Face Skin','Face care','Moisturisers','Medium')],
    'moisturizer': [('leave-in','Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners','High'),('leave in','Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners','High'),('tinted','Make up','Face make-up','Tinted Moisturisers','High'),('spf','Face Skin','Sun care','Sunscreen','High'),('face','Face Skin','Face care','Moisturisers','High'),('body','Body Skin','Body care','Body Lotions','High'),(None,'Face Skin','Face care','Moisturisers','Medium')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Mousse" with none of
    # the above modifiers defaults to the dominant real-catalog use (hair styling mousse), Low.
    'mousse': [('styling','Hair Care Others','Styling','Mousses','High'),('hair','Hair Care Others','Styling','Mousses','High'),('face','Make up','Face make-up','Mousse','High'),('lip','Make up','Lip make-up','Lip & Cheek Stains & Tints','High'),('cheek','Make up','Lip make-up','Lip & Cheek Stains & Tints','High'),('body','Body Skin','Body care','Mousses','High'),(None,'Hair Care Others','Styling','Mousses','Low')],
    'nipple': [(None,'Others','Others Others','Others','High')],
    # FIX: "Cleansing Oil"/"Cleansing Oil Balm" is double-cleanse/makeup-removal terminology by
    # convention (matches how 'cream'+'cleansing' already resolves to Make-up cleansing below) -
    # was inconsistently routed to generic Face Wash/Face Cleansers instead.
    # FIX: real-data audit found 211 bare 'oil' mentions falling to the fully-ambiguous default -
    # by far the largest single specific gap found. Breakdown: 182 were a bare named carrier oil
    # (coconut/castor/almond/rosemary/argan/jojoba etc.) with NO other qualifying word, almost
    # entirely from Indian ayurvedic/natural haircare brands (Vaadi Herbals, Soulflower,
    # Wishcare, Nat Habit) - hair oiling is this market's dominant use of a bare named oil, so
    # the catchall below now defaults there (Medium, same convention as 'lotion'/'moisturiser'
    # defaulting to their own dominant case) instead of staying blank. 13 explicitly said
    # scalp/dandruff/hairfall/frizz - also hair, checked first so they win even when 'massage'
    # is also present (e.g. "Anti Hairfall Massage Oil ... for Scalp Nourishment"). A handful of
    # genuinely different products (oil pulling, pre-makeup oil, wellness/aromatherapy oils) get
    # their own destination; "oil control" was left an explicit, intentional Ambiguous-Gap - real
    # examples covered a primer, a pressed-powder pact, and a scalp exfoliator tool, too
    # heterogeneous to force into one bucket.
    'oil': [
        ('scalp','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('dandruff','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('hairfall','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('hair fall','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('frizz','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('root','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('curl','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('growth','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('hair','Hair Care Others','Hair Oils','Hair Oils','High'),
        ('bath oil','Hygiene','Body wash','Bath Oils','High'),
        ('lip','Make up','Lip make-up','Lip Oils','High'),
        ('makeup remover','Make up','Make-up cleansing','Makeup Cleansing Oils','High'),
        ('makeup','Make up','Make-up cleansing','Makeup Cleansing Oils','High'),
        ('cleansing','Make up','Make-up cleansing','Makeup Cleansing Oils','High'),
        ('facial','Face Skin','Face care','Facial Oils','High'),
        ('face','Face Skin','Face care','Facial Oils','High'),
        ('body','Body Skin','Body care','Body Oils','High'),
        ('massage','Body Skin','Body care','Body Oils','Medium'),
        ('pulling','Others','Others Others','Oral Care','High'),
        ('pre makeup','Make up','Face make-up','Primers','High'),
        ('pre-makeup','Make up','Face make-up','Primers','High'),
        ('pre make up','Make up','Face make-up','Primers','High'),
        ('aromatherapy','Others','Others Others','Others','Medium'),
        ('diffuser','Others','Others Others','Others','Medium'),
        # FIX: per direction, never leave a bare match fully blank - "oil control" real examples
        # were genuinely heterogeneous (a primer, a pressed-powder pact, a scalp exfoliator
        # tool), but a mattifying "oil control" claim is most commonly a Primer, so that's the
        # single best guess, flagged Low so it still surfaces for manual review.
        ('oil control','Make up','Face make-up','Primers','Low'),
        (None,'Hair Care Others','Hair Oils','Hair Oils','Medium'),
    ],
    'pasties': [(None,'Others','Others Others','Others','High')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Patch" with none of the
    # above modifiers defaults to the dominant real-catalog use (acne/pimple patch), Low.
    # FIX: "Earlobe Patch" (for pierced/torn ears) is a distinct accessory, not a skin acne
    # patch - checked before the bare catchall so it doesn't get swept into Acne Patch/Nose
    # Strips just because "Patch" defaults there.
    'patch': [('under eye','Face Skin','Face care','Under Eye Masks','High'),('acne','Face Skin','Face Skin Others','Acne Patch/Nose Strips','High'),('pimple','Face Skin','Face Skin Others','Acne Patch/Nose Strips','High'),('period','Others','Others Others','Period Pain Relief','High'),('earlobe','Others','Others Others','Others','High'),('ear lobe','Others','Others Others','Others','High'),('ear','Others','Others Others','Others','High'),(None,'Face Skin','Face Skin Others','Acne Patch/Nose Strips','Low')],
    'pedicure': [(None,'Others','Others Others','Manicure Kits','High')],
    # FIX: per direction, "Pencil" is generally used for lip pencils/crayons unless a more
    # specific eye/eyebrow/kajal context word says otherwise - was previously only recognized
    # as a 'gel' base modifier (Eyeliners), so a bare "Lip Pencil" with no "gel" word had no
    # path to resolve at all (e.g. "Maybelline Color Sensational Lip Pencil").
    'pencil': [
        ('eyebrow','Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)','High'),
        ('brow','Make up','Eye make-up','Eyebrow Colors/Pencil/Pen (Any)','High'),
        ('kajal','Make up','Eye make-up','Kajal & Kohls','High'),
        ('kohl','Make up','Eye make-up','Kajal & Kohls','High'),
        ('eye','Make up','Eye make-up','Eyeliners','High'),
        ('lip','Make up','Lip make-up','Lip Liners','High'),
        (None,'Make up','Lip make-up','Lip Liners','Medium'),
    ],
    'powder': [('compact','Make up','Face make-up','Compact Powder','High'),
        # FIX: "Pressed Powder"/"Touch-Up Powder"/"Finishing Powder"/"Perfecting Powder" are all
        # common synonyms for compact/setting powder - none said "compact"/"setting"/"loose"
        # literally, so they were falling through to the bare Talcum Powders default.
        ('pressed','Make up','Face make-up','Compact Powder','High'),
        ('touch-up','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('touch up','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('finishing','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('perfecting','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        # FIX: "Highlight Powder"/"Bronze Powder"/"Cushion Powder"/"Fixing/Blurring Powder" -
        # more face-makeup-powder synonyms found the same way (real historical evidence of
        # each being wrongly swept into Talcum Powders).
        ('highlight','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('highlighter','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        # FIX: "Bronze Powder"/"Bronzing Powder" is a bronzer, not a generic setting powder -
        # routed to the same "Bronzers" L3 real historical data uses.
        ('bronze','Make up','Face make-up','Bronzers','High'),
        ('bronzing','Make up','Face make-up','Bronzers','High'),
        # FIX: "Mattifying Powder" is another common setting/finishing-powder synonym.
        ('mattifying','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        # FIX: "Glow"/"Halo Glow"/"Banana"/"Matte"/"Luminizing" Powder are all common face-makeup
        # powder synonyms (highlighting/setting formats) - 'glow' also covers "Halo Glow" since
        # it's a substring match. Checked before 'oil control'/'mineral' fall to the bare Talcum
        # default further down, so a mixed name like "Matte Oil Control Powder" still wins here.
        ('glow','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('strobe','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('strobing','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('luminizing','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('banana','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('matte','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        # FIX: a brand/product explicitly calling itself "Makeup ... Powder" (e.g. "Makeup
        # Revolution Mood Switch Aura Powder") is a face-makeup powder, not talc.
        ('makeup','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('cushion','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('fixing','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('blurring','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        ('finish','Make up','Face make-up','Setting Powder/Loose Powder','High'),
        # FIX: "Acrylic Powder" is a nail-extension product, not talc.
        ('acrylic','Make up','Nail make-up','Nail Art','High'),
        # FIX: "Clay Powder" (a mix-your-own clay mask/pack) - mirrors resolve_mask()'s own
        # clay/mud handling for the "mask" word.
        ('clay','Face Skin','Face cleansers','Face Pack','High'),
        ('setting','Make up','Face make-up','Setting Powder/Loose Powder','High'),('loose','Make up','Face make-up','Setting Powder/Loose Powder','High'),('talcum','Hygiene','Body care (Talc)','Talcum Powders','High'),('talc','Hygiene','Body care (Talc)','Talcum Powders','High'),('body scrub','Hygiene','Body scrub','Body scrub- cremes and powders','High'),('root lifting','Hair Care Others','Styling','Root Lifting Powders','High'),('volumizing','Hair Care Others','Styling','Root Lifting Powders','High'),('volume','Hair Care Others','Styling','Root Lifting Powders','High'),('texturizing','Hair Care Others','Styling','Root Lifting Powders','High'),('styling','Hair Care Others','Styling','Root Lifting Powders','High'),('cleansing','Hair Care','Shampoo','Hair Cleansers, Cleansing Powders','High'),('dye','Hair colour','Hair colour Others','Powder Dyes','High'),
        # FIX: named organic/ayurvedic hair-wash ingredients (bhringraj, sidr, hibiscus, curry
        # leaf/leaves, neem, amla, shikakai, reetha, methi) or the bare word "organic" are hair
        # powders / scalp treatments, not talc, and NOT the same as the bare 'hair' modifier
        # below (which is specifically for hair-COLOUR root-touch-up powders) - checked first so
        # e.g. "Bhringraj Hair Powder" (which also says "hair") matches here, not 'hair' below.
        ('bhringraj','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('sidr','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('hibiscus','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('curry leaf','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('curry leaves','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('neem','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('amla','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('shikakai','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('reetha','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('methi','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('organic','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),
        ('hair','Hair colour','Hair colour Others','Hair Mascaras & Root Touch Ups','High'),('face','Make up','Face make-up','Setting Powder/Loose Powder','High'),(None,'Hygiene','Body care (Talc)','Talcum Powders','Medium')],
    'primer': [('lipstick','Make up','Lip make-up','Lipstick Primers','High'),('lip','Make up','Lip make-up','Lipstick Primers','High'),('eye','Make up','Eye make-up','Eyeshadow Bases & Primers','High'),('eyeshadow','Make up','Eye make-up','Eyeshadow Bases & Primers','High'),(None,'Make up','Face make-up','Primers','High')],
    'razor': [('electric','Others','Others Others','All electric Appliances - Hair Dryers, Straighteners, Electric Razors','High'),(None,'Hygiene','Shaving','Non electric razors','High')],
    # FIX: "Make Up Remover" (space-separated) was falling through blank - only the fused
    # "makeup" spelling was recognized as a modifier, same gap already fixed for other bases
    # (cleanser/wipes/oil already have both spellings). The bare catchall also now defaults to
    # the dominant real-catalog use (a "Hair Dye Remover"/"Colour Clean" de-stainer with neither
    # "hair colour" nor "hair color" literally) instead of staying blank, flagged Low.
    'remover': [('nail polish','Make up','Nail make-up','Nail Polish Remover','High'),('blackhead','Face Skin','Face Skin Others','Whitehead/Blackhead Remover','High'),('whitehead','Face Skin','Face Skin Others','Whitehead/Blackhead Remover','High'),('makeup','Make up','Make-up cleansing','Makeup Cleansing Water','High'),('make up','Make up','Make-up cleansing','Makeup Cleansing Water','High'),('hair colour','Hair colour','Hair colour Others','Hair Colour Removers','High'),('hair color','Hair colour','Hair colour Others','Hair Colour Removers','High'),('hair dye','Hair colour','Hair colour Others','Hair Colour Removers','High'),(None,'Hair colour','Hair colour Others','Hair Colour Removers','Low')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Roll" with none of the
    # above modifiers defaults to the dominant real-catalog use (roll-on deodorant), Low.
    # FIX: an "Underarm Roll On" that explicitly claims to brighten/even-tone/de-pigment the
    # underarm skin (e.g. "Even Tone Underarm Roll On") is a skin-brightening treatment product,
    # not an antiperspirant - checked before the plain 'underarm'/'under arm' modifiers below so
    # a real deodorant roll-on (no brightening claim) still correctly wins there. Flagged Medium
    # (not the usual High for an exact-modifier match) since "brightening" alone is a weaker,
    # less-certain signal than a direct product-format word - worth a second look on real data.
    'roll': [('foot','Others','Others Others','Others','High'),
        ('brightening','Body Skin','Body care','Body Creams','Medium'),
        ('whitening','Body Skin','Body care','Body Creams','Medium'),
        ('even tone','Body Skin','Body care','Body Creams','Medium'),
        ('de-tan','Body Skin','Body care','Body Creams','Medium'),
        ('detan','Body Skin','Body care','Body Creams','Medium'),
        ('deo','Hygiene','Deos & Antiperspirants','Roll ons','High'),('underarm','Hygiene','Deos & Antiperspirants','Roll ons','High'),('under arm','Hygiene','Deos & Antiperspirants','Roll ons','High'),(None,'Hygiene','Deos & Antiperspirants','Roll ons','Low')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Roller" with none of the
    # above modifiers defaults to the same Hair Accessories guess as the 'hair' modifier above.
    'roller': [('face','Face Skin','Face Skin Others','Face Tools (Gua Sha, Rollers)','High'),('jade','Face Skin','Face Skin Others','Face Tools (Gua Sha, Rollers)','High'),('electric','Others','Others Others','All electric Appliances - Hair Dryers, Straighteners, Electric Razors','High'),('automatic','Others','Others Others','All electric Appliances - Hair Dryers, Straighteners, Electric Razors','High'),('hair','Others','Others Others','Hair Accessories','Medium'),(None,'Others','Others Others','Hair Accessories','Low')],
    'scrub': [('facial','Face Skin','Face cleansers','Facial Scrubs & Polishes','High'),('face','Face Skin','Face cleansers','Facial Scrubs & Polishes','High'),('lip','Face Skin','Face Skin Others','Lip Scrubs','High'),('scalp','Hair Care','Hair Care Others','Scalp Scrubs','High'),('body','Hygiene','Body scrub','Body scrub- cremes and powders','High'),('foot','Body Skin','Foot care','Foot Scrubs/Masks/Peels/Creams/Serums','High'),(None,'Face Skin','Face cleansers','Facial Scrubs & Polishes','Medium')],
    'scrubber': [(None,'Others','Others Others','Bath Accessories (Loofahs, Body Scrubbers etc.)','High')],
    'scrunchie': [(None,'Others','Others Others','Hair Accessories','High')],
    # FIX: "Blurring Serum Skin Tint SPF 20" is a tinted, sun-protective makeup product, not a
    # plain skincare serum - 'tint'/'tinted' and 'spf' modifiers were entirely missing here,
    # mirroring the same signals cream/gel/moisturizer already use.
    'serum': [('intimate','Others','Others Others','Others','High'),('tinted','Make up','Face make-up','Tinted Moisturisers','High'),('tint','Make up','Face make-up','Tinted Moisturisers','High'),('spf','Face Skin','Sun care','Sunscreen','High'),('lip','Make up','Lip make-up','Lip Oils','High'),('beard','Hair Care','Hair Serums','Hair Serums','High'),('eye','Face Skin','Face care','Eye Serums','High'),('scalp','Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','High'),('hair','Hair Care','Hair Serums','Hair Serums','High'),('foot','Body Skin','Foot care','Foot Scrubs/Masks/Peels/Creams/Serums','High'),('face','Face Skin','Face care','Face Serums/Essence','High'),('facial','Face Skin','Face care','Face Serums/Essence','High'),(None,'Face Skin','Face care','Face Serums/Essence','Medium')],
    'shampoo': [('3-in-1','Hair Care','Shampoo','3-In-1 Shampoo, Conditioner & Body Wash','High'),('3 in 1','Hair Care','Shampoo','3-In-1 Shampoo, Conditioner & Body Wash','High'),('2-in-1','Hair Care','Shampoo','2-In-1 Shampoo & Conditioner','High'),('2 in 1','Hair Care','Shampoo','2-In-1 Shampoo & Conditioner','High'),('dry shampoo','Hair Care','Shampoo','Dry Shampoos','High'),('hair color','Hair colour','Hair colour Others','Shampoo Hair Color','High'),('hair colour','Hair colour','Hair colour Others','Shampoo Hair Color','High'),('hair dye','Hair colour','Hair colour Others','Shampoo Hair Color','High'),(None,'Hair Care','Shampoo','All kinds of Shampoos','High')],
    'soap': [
        # FIX: a "Foaming"/"Liquid" soap is a pump-dispensed liquid product, not a solid bar.
        ('foaming','Hygiene','Body wash','Hand Wash','High'),
        ('foam','Hygiene','Body wash','Hand Wash','High'),
        ('liquid','Hygiene','Body wash','Hand Wash','High'),
        (None,'Hygiene','Soaps','Solid Soap Bars','High')],
    # FIX: "Texturising"/"Texturizing" and "Hold"/"Holding" (e.g. "24 Hr Soft Hold") are hair-
    # styling-spray-specific terms even without the word "hair" itself - checked before the
    # generic Deos & Antiperspirants catchall, which was otherwise winning by default.
    'spray': [('hair','Hair Care Others','Styling','Hair Sprays & Mists','High'),('texturising','Hair Care Others','Styling','Hair Sprays & Mists','High'),('texturizing','Hair Care Others','Styling','Hair Sprays & Mists','High'),('hold','Hair Care Others','Styling','Hair Sprays & Mists','High'),('holding','Hair Care Others','Styling','Hair Sprays & Mists','High'),('salt water','Hair Care Others','Styling','Salt Water Sprays','High'),('thermal','Hair Care Others','Styling','Thermal Protector Sprays','High'),('deo','Hygiene','Deos & Antiperspirants','Deodrant Sprays','High'),('deodorant','Hygiene','Deos & Antiperspirants','Deodrant Sprays','High'),('setting','Make up','Face make-up','Makeup Setting Spray','High'),
        # FIX: "Makeup Fixer/Fixing Spray" and "Prep & Set Spray" are the same Makeup Setting
        # Spray product family as "Setting Spray" - just named differently. 117 real examples
        # (across spray+mist) were falling to the bare Ambiguous-NoModifier default.
        ('fixer','Make up','Face make-up','Makeup Setting Spray','High'),
        ('fixing','Make up','Face make-up','Makeup Setting Spray','High'),
        ('prep','Make up','Face make-up','Makeup Setting Spray','High'),
        # FIX: was a bare 'body' modifier - matched ANY co-occurrence of the word "body"
        # anywhere in the text (e.g. "Body Acne Spray", a skincare treatment), not just an
        # actual "body spray" product. Requiring the adjacent phrase matches the same standard
        # already used by the deodorant+perfume-guard co-occurrence check below.
        ('body spray','Hygiene','Deos & Antiperspirants','Deodrant Sprays','High'),
        # FIX: per direction, never leave a bare match fully blank - a bare "Spray" with none of
        # the above modifiers defaults to the dominant real-catalog use (deodorant spray), Low.
        (None,'Hygiene','Deos & Antiperspirants','Deodrant Sprays','Low')],
    'sunscreen': [('body','Face Skin','Sun care','Body Sunscreen','High'),(None,'Face Skin','Sun care','Sunscreen','High')],
    'tape': [('fashion','Others','Others Others','Others','High'),(None,'Others','Others Others','Others','Medium')],
    # FIX: "Skin Tint" is a huge, currently-mainstream face-makeup category (a lightweight,
    # dewy tinted-moisturizer-adjacent base product) with 32 real examples falling to the bare
    # Ambiguous-NoModifier default - had no 'skin' modifier at all. "Blush Tint" was ALSO
    # falling through blank: the blush+tint DIRECT_KEYWORDS guard elsewhere deliberately defers
    # bare 'blush' to this CONTEXT_MATRIX entry (the product name's "blush" is the SHADE
    # descriptor, "tint" is the format), but there was no 'blush' modifier here to catch it -
    # only 'cheek'. Routed to the same destination as 'cheek' since it's the same product family.
    # A bare "Tint" with no other qualifying word (e.g. "Jelly Tint", "Juicy Tint", "Fresh Tint")
    # is, per real data, overwhelmingly a Skin Tint product too - defaults there at Medium
    # confidence instead of staying fully ambiguous, matching how 'lotion'/'moisturiser' already
    # default to their own dominant real-world case rather than a true blank.
    'tint': [('hair','Hair colour','Creme','Crème','High'),('lip','Make up','Lip make-up','Lip & Cheek Stains & Tints','High'),('cheek','Make up','Lip make-up','Lip & Cheek Stains & Tints','High'),('blush','Make up','Lip make-up','Lip & Cheek Stains & Tints','High'),('skin','Make up','Face make-up','Tinted Moisturisers','High'),('face','Make up','Face make-up','Tinted Moisturisers','High'),(None,'Make up','Face make-up','Tinted Moisturisers','Medium')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Wash" with none of the
    # above modifiers defaults to the dominant real-catalog use (face wash), Low.
    # FIX: "Hair Wash" (a very common colloquial synonym for Shampoo in Indian marketing/retail
    # copy, e.g. "Curl Up Hair Wash Combo") had no path to Hair Care at all - fell to the bare
    # Face Wash/Face Cleansers default instead, which then also broke bundle-detection for
    # "Hair Wash Combo with Shampoo and Conditioner"-style kits (see bundle_detect.py).
    'wash': [('mouth','Others','Others Others','Oral Care','High'),('face','Face Skin','Face cleansers','Face Wash/Face Cleansers','High'),('hand','Hygiene','Body wash','Hand Wash','High'),('body wash gel','Hygiene','Body wash','Body Wash Gels','High'),('body wash oil','Hygiene','Body wash','Body Wash Oils','High'),('body','Hygiene','Body wash','Body washes (generic/cream-based)','Medium'),('hair','Hair Care','Shampoo','All kinds of Shampoos','High'),(None,'Face Skin','Face cleansers','Face Wash/Face Cleansers','Low')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Wax" with none of the
    # above modifiers defaults to the dominant real-catalog use (hair-removal wax), Low.
    'wax': [('heater','Others','Others Others','Wax Heaters','High'),('styling','Hair Care Others','Styling','Waxes','High'),('hair styling','Hair Care Others','Styling','Waxes','High'),('removal','Hygiene','Shaving','Depilatory','High'),('hair','Hair Care Others','Styling','Waxes','High'),(None,'Hygiene','Shaving','Depilatory','Low')],
    # FIX: per direction, never leave a bare match fully blank - a bare "Wipes" with none of the
    # above modifiers defaults to the dominant real-catalog use (facial wipes), Low.
    'wipes': [('makeup','Make up','Make-up cleansing','Makeup Cleansing Wipes','High'),('make up','Make up','Make-up cleansing','Makeup Cleansing Wipes','High'),('face','Face Skin','Face cleansers','Facial Wipes','High'),('facial','Face Skin','Face cleansers','Facial Wipes','High'),('intimate','Others','Others Others','Intimate Wipes','High'),(None,'Face Skin','Face cleansers','Facial Wipes','Low')],
}

# Rules approved through the portal's Glossary "Pending Review" workflow land here, in
# approved_rules.json at the project root (sibling of pending_rules.json) - NOT by editing this
# file. Keeps the promise that "the underlying Python logic is never rewritten automatically":
# CONTEXT_MATRIX above is untouched on disk, this only ever appends to it in memory, and only with
# tuples this module itself constructed from the JSON (same shape CONTEXT_MATRIX entries already
# use). _APPROVED_INJECTED tracks exactly what was added so a reload can cleanly undo the previous
# round before reapplying, instead of accumulating duplicates across repeated approvals.
_APPROVED_RULES_PATH = Path(__file__).resolve().parent.parent / 'approved_rules.json'
_APPROVED_INJECTED = []


def reload_approved_rules():
    global _APPROVED_INJECTED
    for base, tup in _APPROVED_INJECTED:
        bucket = CONTEXT_MATRIX.get(base)
        if bucket and tup in bucket:
            bucket.remove(tup)
    _APPROVED_INJECTED = []
    try:
        with open(_APPROVED_RULES_PATH, 'r', encoding='utf-8') as f:
            entries = json.load(f)
    except FileNotFoundError:
        entries = []
    for entry in entries:
        base = (entry.get('keyword') or '').strip().lower()
        if not base:
            continue
        modifier = (entry.get('modifier') or '').strip().lower() or None
        tup = (modifier, entry.get('l1'), entry.get('l2'), entry.get('l3'), 'High')
        bucket = CONTEXT_MATRIX.setdefault(base, [])
        # The matching loop below returns as soon as it hits the first (None, ...) catch-all
        # tuple in a base's list - a specific new modifier appended AFTER one would be silently
        # unreachable dead weight. Insert before the first such wildcard instead (or at the end
        # if the base has none).
        insert_at = next((i for i, t in enumerate(bucket) if t[0] is None), len(bucket))
        bucket.insert(insert_at, tup)
        _APPROVED_INJECTED.append((base, tup))


reload_approved_rules()

SPECIFICITY_PHRASES = [
    # FIX: "Lip & Cheek Glow/Stain/Duo" is a well-established multi-brand product family
    # (Charlotte Tilbury, Benefit, Makeup Revolution, Kay Beauty) with no other recognizable
    # keyword in the name at all - was falling entirely to Unmapped.
    ('lip & cheek','Make up','Lip make-up','Lip & Cheek Stains & Tints'),
    ('lip and cheek','Make up','Lip make-up','Lip & Cheek Stains & Tints'),
    # FIX: "Colour/Color Corrector" (green/purple/peach-tinted pre-foundation primers) is a real,
    # distinct face-makeup category with no existing L3 at all.
    ('colour corrector','Make up','Face make-up','Colour Corrector'),
    ('color corrector','Make up','Face make-up','Colour Corrector'),
    # FIX: "Prep + Prime"/"Prep and Prime"/"Plump & Prime" is M.A.C's (and others') well-known
    # naming convention for their face-priming product line - no other recognizable keyword.
    # Note: normalize_compounds() already turns "+"/"-" into spaces before this check runs, so
    # only the space-separated form needs a phrase entry here ("&" is left untouched).
    ('prep prime','Make up','Face make-up','Primers'),
    ('prep and prime','Make up','Face make-up','Primers'),
    ('plump & prime','Make up','Face make-up','Primers'),
    ('plump and prime','Make up','Face make-up','Primers'),
    ('base coat','Make up','Nail make-up','Top & Base Coat'),
    ('body mist','Fragrance','Perfume','Body Mists'),
    ('eau de cologne','Fragrance','Eau De Cologne','Eau De Cologne'),
    ('eau de parfum','Fragrance','Eau De Parfum','Eau De Parfum'),
    ('eau de perfume','Fragrance','Eau De Parfum','Eau De Parfum'),
    ('eau de perfum','Fragrance','Eau De Parfum','Eau De Parfum'),  # common real-listing typo (missing final 'e')
    ('eau de parfume','Fragrance','Eau De Parfum','Eau De Parfum'),  # common real-listing typo (extra final 'e')
    ('extrait de parfum','Fragrance','Eau De Parfum','Eau De Parfum'),
    ('eau de toilette','Fragrance','Eau De Toilette','Eau De Toilette'),
    ('edp','Fragrance','Eau De Parfum','Eau De Parfum'),
    ('edt','Fragrance','Eau De Toilette','Eau De Toilette'),
    ('body wash','Hygiene','Body wash','Body washes (generic/cream-based)'),
    ('hand wash','Hygiene','Body wash','Hand Wash'),
    ('face wash','Face Skin','Face cleansers','Face Wash/Face Cleansers'),
    ('mehendi tattoo','Make up','Body make-up','Others'),
    ('henna tattoo','Make up','Body make-up','Others'),
    ('hair removal spray','Hygiene','Shaving','Depilatory'),
    ('nail art','Make up','Nail make-up','Nail Art'),
    ('nail paint','Make up','Nail make-up','Nail Polish'),
    ('nail polish','Make up','Nail make-up','Nail Polish'),
    ('nail polish remover','Make up','Nail make-up','Nail Polish Remover'),
    ('nail polish set','Make up','Nail make-up','Nail Polish Sets'),
    ('press on nail','Make up','Nail make-up','Nail Art'),
    ('press-on nail','Make up','Nail make-up','Nail Art'),
    ('top coat','Make up','Nail make-up','Top & Base Coat'),
    ('under eye mask','Face Skin','Face care','Under Eye Masks'),
    ('wax strip','Hygiene','Shaving','Depilatory'),
    ('waxing strip','Hygiene','Shaving','Depilatory'),
]
def resolve_spot_corrector(text):
    """Dedicated 'spot corrector' resolution, per the three explicit conditions given."""
    if has_word(text,'patch') or has_word(text,'nose strip') or has_word(text,'strip'):
        return ('Face Skin','Face Skin Others','Acne Patch/Nose Strips')
    if has_word(text,'gel') or has_word(text,'serum'):
        return ('Face Skin','Face care','Face Serums/Essence')
    if has_word(text,'cream'):
        return ('Face Skin','Face care','Face Creams')
    return ('Face Skin','Face Skin Others','Acne Patch/Nose Strips')

def resolve_hair_treatment(text):
    """Dedicated 'hair treatment' resolution - real data shows this splits ~evenly 3 ways."""
    if has_word(text,'skin serum') or has_word(text,'ingrown hair'):
        return None  # defer entirely to normal serum/skin logic, not a haircare product
    if any(has_word(text,p) for p in ['pre wash','pre shampoo','scalp','dandruff','minoxidil','topical solution']):
        return ('Hair Care','Hair Serums','Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums')
    if has_word(text,'leave in') or has_word(text,'bond smoother') or has_word(text,'bonding concentrate'):
        return ('Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners')
    return ('Hair Care','Rinse-Off Masks','Rinse Off Masks')

def resolve_mask(text):
    """Dedicated mask-resolution logic, priority order per instructions."""
    if has_word(text, 'sheet'):
        return ('Face Skin','Face care','Face Sheet Masks','High')
    # FIX: the literal "Face Pack" phrase and a powder FORMAT (multani mitti/ubtan/herbal
    # powder masks you mix yourself) are strong, unambiguous signals that this is NOT a
    # single-use sheet mask - checked before the generic bottom-of-chain default, which was
    # otherwise sweeping large numbers of powder/paste masks into "Face Sheet Masks" just
    # because no other resolve_mask() clause happened to catch them first.
    if has_word(text, 'face pack'):
        return ('Face Skin','Face cleansers','Face Pack','High')
    if has_word(text, 'powder'):
        return ('Face Skin','Face cleansers','Face Pack','High')
    if has_word(text, 'clay') or has_word(text, 'mud'):
        return ('Face Skin','Face cleansers','Face Pack','High')
    if has_word(text, 'leave in') or has_word(text, 'leave-in'):
        return ('Hair Care','Leave-In Conditioners','Leave In Creams, Leave In Conditioners','High')
    if has_word(text, 'foot'):
        return ('Body Skin','Foot care','Foot Scrubs/Masks/Peels/Creams/Serums','High')
    # FIX: "Wash Off Mask" and "Exfoliating (Treatment) Mask" are the same rinse-off/scrub
    # format as "Rinse Off"/"Peel Off" - neither phrase was previously recognized here.
    if has_word(text, 'rinse off') or has_word(text, 'peel off') or has_word(text,'detan') or has_word(text,'de tan') or has_word(text,'d tan') or has_word(text,'tan removal') or (has_word(text,'tan') and has_word(text,'remove')) or has_word(text,'wash off') or has_word(text,'exfoliating') or has_word(text,'exfoliate'):
        return ('Face Skin','Face cleansers','Peel Off Masks/ Rinse Off Masks','High')
    if has_word(text, 'hand'):
        return ('Body Skin','Body care','Hand Creams, Lotions, masks','High')
    if has_word(text, 'eye') and not has_word(text,'lip'):
        return ('Face Skin','Face care','Under Eye Masks','High')
    if has_word(text, 'lip'):
        # NOTE: "Lip Masks" (Face Skin Others) remains a valid, documented L3 for future use -
        # real historical evidence (Laneige Lip Sleeping Mask, Dot & Key Lip Mask) currently
        # points to Balms (Non-Tinted) as the established destination for these products.
        return ('Face Skin','Face Skin Others','Balms (Non-Tinted)','High')
    if any(has_word(text, b) for b in SHEET_MASK_BRANDS):
        return ('Face Skin','Face care','Face Sheet Masks','Medium')
    if has_word(text, 'hair'):
        return ('Hair Care','Rinse-Off Masks','Rinse Off Masks','High')
    if any(has_word(text, b) for b in HAIR_ONLY_BRANDS):
        return ('Hair Care','Rinse-Off Masks','Rinse Off Masks','Medium')
    # NOTE: "Face Masks (Clay/Cream/Wash-off)" and "Face Pack" both remain valid, documented
    # L3s. A comprehensive real-data check (573 genuinely generic mask rows, no other signal
    # at all) showed Face Sheet Masks is the true plurality (254 vs 188 for Face Pack).
    return ('Face Skin','Face care','Face Sheet Masks','Medium')


# Methods/confidence levels that mean "the rule engine struggled here" - a total miss,
# an explicit ambiguity flag, or a default that isn't a real keyword match. sku_exceptions.json
# is consulted whenever the core engine lands in one of these, not only on a total miss.
_LOW_CONFIDENCE_METHODS = {'Unmapped', 'Ambiguous-NoModifier', 'Ambiguous-Gap'}
_LOW_CONFIDENCE_LEVELS = {'None', 'Low-Flag', 'Gap-Flag', 'Medium', 'Low'}

def tag_sku(sku_name):
    """Tags a single SKU, then applies the sku_exceptions.json safety net whenever the
    core engine either found nothing or isn't fully confident in what it found."""
    result = _tag_sku_core(sku_name)
    if result.get('method') in _LOW_CONFIDENCE_METHODS or result.get('confidence') in _LOW_CONFIDENCE_LEVELS:
        text = normalize_compounds(sku_name.lower())
        if text in SKU_EXCEPTIONS:
            entry = SKU_EXCEPTIONS[text]
            return {'method':'SKU-Exception','matched_on':text,'L1':entry.get('L1'),'L2':entry.get('L2'),'L3':entry.get('L3'),'confidence':'High'}
    return result

def _tag_sku_core(sku_name):
    text = normalize_compounds(sku_name.lower())

    if text in SKU_MEMORY:
        l1, l2, l3 = SKU_MEMORY[text]
        return {'method':'SKU-Memory','matched_on':text,'L1':l1,'L2':l2,'L3':l3,'confidence':'High'}

    for kw in EXCLUDE_KEYWORDS:
        if has_word(text, kw):
            return {'method':'Excluded-NA','matched_on':kw,'L1':None,'L2':None,'L3':None,'confidence':'NA'}

    if has_word(text, 'hair treatment'):
        result = resolve_hair_treatment(text)
        if result is not None:
            l1, l2, l3 = result
            return {'method':'HairTreatment-Resolved','matched_on':'hair treatment','L1':l1,'L2':l2,'L3':l3,'confidence':'High'}

    if has_word(text, 'spot corrector'):
        l1, l2, l3 = resolve_spot_corrector(text)
        return {'method':'SpotCorrector-Resolved','matched_on':'spot corrector','L1':l1,'L2':l2,'L3':l3,'confidence':'High'}

    if (has_word(text,'eyeshadow') or has_word(text,'eye shadow')) and not (has_word(text,'brush') or has_word(text,'applicator') or has_word(text,'palette')):
        return {'method':'Direct-Keyword','matched_on':'eyeshadow','L1':'Make up','L2':'Eye make-up','L3':'Eyeshadow','confidence':'High'}

    # NOTE: a bare "Lipstick Set/Kit" (e.g. 3 shades of the same lipstick bundled together) is
    # NOT a combo - it's still just lipstick, per explicit direction: a combo means 2+
    # DIFFERENT products together. Removed the old blanket lipstick+set/kit->Make-Up Kits
    # shortcut; genuine multi-product makeup kits (e.g. "Lipstick + Eyeliner Kit") are now
    # handled by the MAKEUP_L1S combo family in bundle_detect.py's multi-category resolution.

    # FIX: a silk/satin/sleep "eye mask" is a sleep cover/blindfold, not a skincare treatment -
    # must be caught before resolve_mask() mistakes it for an Under Eye Mask.
    if has_word(text,'eye') and has_word(text,'mask') and (has_word(text,'silk') or has_word(text,'satin') or has_word(text,'sleep') or has_word(text,'sleeping') or has_word(text,'mulberry') or has_word(text,'blindfold')):
        return {'method':'Direct-Keyword','matched_on':'sleep-eye-mask','L1':'Others','L2':'Others Others','L3':'Others','confidence':'High'}

    is_mask_tool = has_word(text,'brush') or has_word(text,'roller') or has_word(text,'gua sha') or has_word(text,'applicator') or has_word(text,'spatula') or has_word(text,'foundation') or has_word(text,'cushion')
    if has_word(text, 'mask') and not is_mask_tool:
        result = resolve_mask(text)
        l1, l2, l3, conf = result
        method = 'Mask-BrandFallback' if conf == 'Medium' and l2 == 'Rinse-Off Masks' else 'Mask-Resolved'
        return {'method':method,'matched_on':'mask','L1':l1,'L2':l2,'L3':l3,'confidence':conf}

    # FIX: "Fragrance & Colour Free" explicitly says the product has NO colour - the bare
    # 'colour'/'color' word was being read as a positive tinted signal regardless of the
    # negation right next to it. Checked before every lip-balm tinted/colour rule below.
    colour_free = has_word(text,'colour free') or has_word(text,'color free') \
        or has_word(text,'colourfree') or has_word(text,'colorfree')
    if colour_free and has_word(text,'lip') and has_word(text,'balm'):
        return {'method':'CoOccurrence','matched_on':'lip+balm+colour-free','L1':'Face Skin','L2':'Face Skin Others','L3':'Balms (Non-Tinted)','confidence':'High'}

    # FIX: "Lip Balm ... Tinted Lip Moisturizer" - a lip balm ALSO described as a "lip
    # moisturizer" is the same product, not a separate face-makeup "Tinted Moisturiser". Must
    # be checked before the CONTEXT_MATRIX loop, where 'moisturizer' (a longer base word) would
    # otherwise win over the more specific 'lip balm' base purely on length-sort priority.
    if has_word(text,'lip balm') and (has_word(text,'moisturiser') or has_word(text,'moisturizer')):
        if has_word(text,'tinted') or has_word(text,'tint') or has_word(text,'colored') or has_word(text,'coloured') or has_word(text,'colour') or has_word(text,'color') or has_word(text,'glow') or has_word(text,'glowy') or has_word(text,'gloss') or has_word(text,'glossy') or any(has_word(text,w) for w in LIP_BALM_COLOR_WORDS):
            return {'method':'CoOccurrence','matched_on':'lip-balm+moisturizer+tinted','L1':'Make up','L2':'Lip make-up','L3':'Tinted/Colored Lip Balms','confidence':'High'}
        if has_word(text,'spf'):
            return {'method':'CoOccurrence','matched_on':'lip-balm+moisturizer+spf','L1':'Make up','L2':'Lip make-up','L3':'Chapstick','confidence':'High'}
        return {'method':'CoOccurrence','matched_on':'lip-balm+moisturizer','L1':'Face Skin','L2':'Face Skin Others','L3':'Balms (Non-Tinted)','confidence':'Medium'}

    # Co-occurrence fixes: words checked as "both present anywhere", not required adjacent.
    if has_word(text,'lip') and has_word(text,'balm') and not has_word(text,'lip balm'):
        if has_word(text,'tinted') or has_word(text,'tint') or has_word(text,'colored') or has_word(text,'coloured') or has_word(text,'colour') or has_word(text,'color') or has_word(text,'glow') or has_word(text,'glowy') or has_word(text,'gloss') or has_word(text,'glossy') or any(has_word(text,w) for w in LIP_BALM_COLOR_WORDS):
            return {'method':'CoOccurrence','matched_on':'lip+balm+tinted-or-glow','L1':'Make up','L2':'Lip make-up','L3':'Tinted/Colored Lip Balms','confidence':'High'}
        if has_word(text,'spf'):
            return {'method':'CoOccurrence','matched_on':'lip+balm+spf','L1':'Make up','L2':'Lip make-up','L3':'Chapstick','confidence':'High'}
        return {'method':'CoOccurrence','matched_on':'lip+balm','L1':'Face Skin','L2':'Face Skin Others','L3':'Balms (Non-Tinted)','confidence':'Medium'}
    if has_word(text,'nose') and has_word(text,'strip') and not has_word(text,'nose strip'):
        return {'method':'CoOccurrence','matched_on':'nose+strip','L1':'Face Skin','L2':'Face Skin Others','L3':'Acne Patch/Nose Strips','confidence':'High'}
    if has_word(text,'makeup') and has_word(text,'sponge') and not has_word(text,'makeup sponge'):
        return {'method':'CoOccurrence','matched_on':'makeup+sponge','L1':'Make up','L2':'Make up Others','L3':'Make up Sponges/ Beauty Blenders','confidence':'High'}

    # FIX: nutraceutical/supplement powders (protein, collagen, multivitamin, herbal-for-
    # consumption) are out of BPC scope entirely - the bare 'powder' default otherwise sweeps
    # them into Talcum Powders just because the word "powder" appears, when they're clearly
    # not a cosmetic body powder at all.
    if has_word(text,'powder') and any(has_word(text,w) for w in (
            'protein','collagen','whey','creatine','multivitamin','supplement','nutrition',
            'glutathione','biotin tablet','carb','mass gainer','gainer','pre workout','bcaa',
            'amino')):
        return {'method':'CoOccurrence','matched_on':'powder+supplement-guard','L1':'Others','L2':'Others Others','L3':'Others','confidence':'High'}

    # FIX: "Powder Kiss" is M.A.C's lipstick/lip-and-cheek product LINE NAME, not a literal
    # powder-format product - was falling into the bare 'powder' default (Talcum Powders).
    if has_word(text,'powder kiss'):
        if has_word(text,'lip') or has_word(text,'lipcolour') or has_word(text,'lipcolor'):
            return {'method':'CoOccurrence','matched_on':'powder-kiss+lip','L1':'Make up','L2':'Lip make-up','L3':'Lipsticks','confidence':'High'}
        if has_word(text,'cheek'):
            return {'method':'CoOccurrence','matched_on':'powder-kiss+cheek','L1':'Make up','L2':'Face make-up','L3':'Blushes','confidence':'High'}

    # FIX: "Double Wear" is Estee Lauder's foundation line - "Double Wear...Powder" is their
    # powder-format foundation, not talc.
    if has_word(text,'double wear') and has_word(text,'powder'):
        return {'method':'CoOccurrence','matched_on':'double-wear+powder','L1':'Make up','L2':'Face make-up','L3':'Compact Powder','confidence':'High'}

    # FIX: "Foundation/Concealer/Highlighter/Blush/Bronzer Brush" is the TOOL, not the cosmetic
    # product itself - without this, the longer product-name keyword (e.g. "foundation") wins
    # over "brush" purely because CONTEXT_MATRIX checks longer base words first.
    if has_word(text,'brush') and any(has_word(text,w) for w in (
            'foundation','concealer','highlighter','blush','bronzer','contour','eyeshadow')):
        return {'method':'CoOccurrence','matched_on':'brush+makeup-product-guard','L1':'Make up','L2':'Make up Others','L3':'Make-Up Brush Sets','confidence':'High'}

    # FIX: real-data audit (sku_tag_history.json) of the strobe/contour/camouflage guards below
    # found they were overriding already-correct, MORE SPECIFIC classifications on multi-shade
    # palettes and dual-identity products - e.g. "NYX Highlight & Contour Pro Palette" (a
    # palette, not a single Contour product), "M.A.C Strobe Beam Liquid Blush" (a Blush that
    # happens to have a strobe/glow finish, not a standalone Highlighter), "Ultime Pro Strobe
    # Makeup Fixer" (a setting spray, not a Highlighter). Mirrors the same "the more specific,
    # explicitly-named product type wins over a marketing/finish descriptor" principle already
    # used throughout this file (e.g. soap-wins-over-fragrance, wash-wins-over-cream-texture).
    _MORE_SPECIFIC_THAN_FINISH_WORD = (
        'foundation', 'concealer', 'highlighter', 'blush', 'bronzer', 'bronzing', 'eyeshadow',
        'primer', 'fixer', 'palette',
    )

    # FIX: bare "Micellar" (e.g. "Micellar Cleansing Water" with a word in between, not the
    # exact adjacent phrase) is a well-established face-cleansing category (Bioderma, Colorbar,
    # Tulips) - but defers to: the exact adjacent phrase "micellar water" (a more specific,
    # pre-existing DIRECT_KEYWORDS entry with its own "Micellar Water" L3, checked further below
    # - this guard runs earlier in the pipeline so must replicate that deferral explicitly);
    # 'oil' (e.g. "Blue Heaven Oil Infused Micellar Cleansing Water", "BRING GREEN Micellar Oil
    # Formula") since those are more specifically an oil-format cleanser; and "makeup remover"/
    # "makeup removal" (e.g. "Fre Cleanse Me Foaming Micellar Cleanser & Makeup Remover") since
    # that phrase is already handled more specifically by the pre-existing 'cleanser'
    # CONTEXT_MATRIX modifier.
    if has_word(text,'micellar') and not has_word(text,'micellar water') \
       and not has_word(text,'oil') \
       and not has_word(text,'makeup remover') and not has_word(text,'makeup removal'):
        return {'method':'CoOccurrence','matched_on':'micellar','L1':'Face Skin','L2':'Face cleansers','L3':'Face Wash/Face Cleansers','confidence':'High'}

    # FIX: "Blurring" is a texture/finish descriptor applied across many product types (Powder,
    # Blush, Foundation, Compact Powder all commonly market a "Blurring" finish) - a blanket
    # 'blurring'->Primers keyword wrongly overrode explicit, more specific product identities
    # (e.g. "Revlon Photoready Blurring Powder", "Too Faced Cloud Crush Blurring Blush", "Dior
    # Forever Skin Wear Blurring Natural Matte Foundation", "Renee Cosmetics Delulu Blurring Gel
    # Compact"). Only routes to Primers when no more specific product-type word is present -
    # same "more specific wins" principle as the strobe/contour guards below.
    if has_word(text,'blurring') and not any(has_word(text, w) for w in (
            'powder','blush','foundation','compact','concealer','bronzer','setting spray')):
        return {'method':'CoOccurrence','matched_on':'blurring','L1':'Make up','L2':'Face make-up','L3':'Primers','confidence':'Medium'}

    # FIX: "Strobe"/"Strobing" is a highlighting/illuminating makeup technique regardless of
    # format word (cream, stick) - not only the exact phrase "strobe cream" already handled by
    # SPECIFICITY_PHRASES (e.g. "Strobe Illuminating Cream" has a word in between). A powder-
    # format strobe product is routed the same way every other powder-format highlighting term
    # already is (see the 'powder' CONTEXT_MATRIX entry's own 'strobe'/'strobing' modifiers),
    # not force-defaulted to Highlighters & Illuminators here.
    if (has_word(text,'strobe') or has_word(text,'strobing')) and not has_word(text,'powder') \
       and not any(has_word(text, w) for w in _MORE_SPECIFIC_THAN_FINISH_WORD):
        return {'method':'CoOccurrence','matched_on':'strobe','L1':'Make up','L2':'Face make-up','L3':'Highlighters & Illuminators','confidence':'High'}

    # FIX: "Bar" alone is too ambiguous to blanket-match - real-data audit found genuine
    # collisions with "Shampoo Bar"/"Conditioner Bar" (a real, growing solid-haircare category),
    # "Nail Bar" (Pep Nails' kit line), "Guard Bar" (a razor cartridge part), "Brow Bar"
    # (an eyebrow product), and even shade/brand names ("Orange Bar", "Fraganote London Bar").
    # Requires a positive cleansing/personal-care signal word instead of a blanket match with a
    # growing exclusion list - real soap-bar names (Dove/Cetaphil/Fiama "...Care/Beauty/
    # Cleansing/Moisturising Bar") consistently carry one of these; the haircare/nail/razor/brow
    # collisions above never do. Still excludes 'shampoo'/'conditioner' defensively since this
    # guard runs before those CONTEXT_MATRIX bases get their own chance.
    if has_word(text,'bar') and any(has_word(text,w) for w in (
            'care','beauty','cleansing','moisturising','moisturizing','sensitive','glowing')) \
       and not any(has_word(text,w) for w in ('shampoo','conditioner','nail','brow','eyebrow')):
        return {'method':'CoOccurrence','matched_on':'bar+care-word','L1':'Hygiene','L2':'Soaps','L3':'Solid Soap Bars','confidence':'Medium'}

    # FIX: bare "Eyebrow"/"Brow" (e.g. "L'Oreal Paris Brow Artist Xpert", "Max Factor Brow
    # Shaper", "Etude House Drawing Eyebrow") - previously only recognized as a modifier of the
    # 'gel'/'pencil' bases, so a product using neither of those words had no path to resolve.
    # Excludes tool words - real-data audit found many "Eyebrow Scissor"/"Eyebrow Tweezer"/
    # "Eyebrow Brush"/"Eyebrow Groomer"/"Eyelash And Brow Set - Set Of 3 Tools" grooming TOOLS
    # (Bronson, Midazzle, Beaute Secrets, VEGA) that aren't a colour product at all, plus a
    # "Hair & Eyebrow Touch Up Kit" (grey-root concealer, stays Hair) and more decisive existing
    # product identities ("Lash & Brow Mascara", "Gel Liner For Eyes & Brows", a multi-shade
    # "Eyebrow Styling...Palette") that should keep winning over this generic default.
    if (has_word(text,'eyebrow') or has_word(text,'brow')) and not any(has_word(text,w) for w in (
            'scissor','tweezer','brush','trimmer','trimming','hair','groomer','tool','tools',
            'set of','mascara','liner','eyeliner','palette')):
        return {'method':'CoOccurrence','matched_on':'eyebrow','L1':'Make up','L2':'Eye make-up','L3':'Eyebrow Colors/Pencil/Pen (Any)','confidence':'Medium'}

    # FIX: bare "Cuticle" (e.g. "Cuticle Softener", "Cuticle Nipper Kit") with no other
    # recognized keyword - excludes 'oil' since "Cuticle Oil" ground-truths to the generic Hair
    # Oils bucket in this taxonomy, not Cuticle Tools.
    if has_word(text,'cuticle') and not has_word(text,'oil'):
        return {'method':'CoOccurrence','matched_on':'cuticle','L1':'Others','L2':'Others Others','L3':'Cuticle Tools','confidence':'Medium'}

    # FIX: "Shaving Blade Cartridges"/"Razor Blade Cartridges" (Gillette Mach3, Gillette Labs)
    # never say the word "razor" itself, so the 'razor' CONTEXT_MATRIX base never gets a chance.
    # Excludes 'foam'/'gel'/'cream' - a bundled "Razor + Shaving Foam" combo should resolve via
    # the normal bundle hit-scanning logic instead of this single-product shortcut.
    if (has_word(text,'shaving') or has_word(text,'razor')) and (has_word(text,'cartridge') or has_word(text,'blade')) \
       and not any(has_word(text,w) for w in ('foam','gel','cream')):
        return {'method':'CoOccurrence','matched_on':'shaving+cartridge/blade','L1':'Hygiene','L2':'Shaving','L3':'Non electric razors','confidence':'High'}

    # FIX: bare "Cushion" (Korean beauty shorthand for a cushion-compact foundation, e.g.
    # "TIRTIR Mask Fit Aura Mini Cushion", "Muzigae Mansion Sleek Matt Cushion" - neither says
    # "compact" literally, so the existing 'cushion compact' DIRECT_KEYWORD doesn't catch them)
    # - excludes 'brush' (a "Cushion Brush" is the hair-tool-type word, Hair Accessories) and
    # 'powder' (a "Cushion Powder" is the powder-format highlighter, handled by that base's own
    # 'cushion' modifier).
    if has_word(text,'cushion') and not has_word(text,'brush') and not has_word(text,'powder'):
        return {'method':'CoOccurrence','matched_on':'cushion','L1':'Make up','L2':'Face make-up','L3':'Foundation','confidence':'Medium'}

    # FIX: bare "Fixer" with no adjacent "makeup" (already handled by the 'makeup fixer'
    # DIRECT_KEYWORD above) and no format word - "Swiss Beauty...Glow Fixer", "Europe Girl Shiny
    # Fixer", "Maybelline...Double Fixer", "Ronzille...Makeup Setting Fixer" (not adjacent) - is
    # the same Makeup Fixer product family. Excludes 'hair'/'frizz' (a genuine Hair Fixer/Frizz
    # Fixer product) and 'spray'/'mist' (format-specific Makeup Setting Spray, handled by those
    # bases' own 'fixer' modifiers).
    if has_word(text,'fixer') and not has_word(text,'hair') and not has_word(text,'frizz') \
       and not has_word(text,'spray') and not has_word(text,'mist'):
        return {'method':'CoOccurrence','matched_on':'fixer','L1':'Make up','L2':'Face make-up','L3':'Makeup Fixer','confidence':'Medium'}

    # FIX: "Skin/Glow/Beauty/Radiance Filter" is 2023-2025 makeup-marketing shorthand for a
    # soft-focus, skin-perfecting highlighter/primer hybrid (Charlotte Tilbury, e.l.f., ETUDE,
    # TIRTIR all use it). Bare "filter" alone is too generic/risky (photo filter, air filter,
    # etc.) to add as a blanket keyword, so this requires a co-occurring glow/skin/beauty word.
    if has_word(text,'filter') and any(has_word(text,w) for w in (
            'glow','radiance','skin','beauty','cloud','luminous','flawless','cushion')):
        return {'method':'CoOccurrence','matched_on':'filter+glow-context','L1':'Make up','L2':'Face make-up','L3':'Highlighters & Illuminators','confidence':'Medium'}

    # FIX: "Multistick"/"Multi-Stick"/"Multi Stick" (a 2-in-1 cream cheek+lip color stick) is
    # the same product family as the "Lip & Cheek" phrase above. Requires the words to be
    # ADJACENT (real-data audit found bare has_word('multi') + has_word('stick') anywhere in the
    # text was matching unrelated products, e.g. "Bobbi Brown Skin Enhancer Multi-Stick
    # Highlighter" which explicitly self-identifies as a Highlighter) - defers to a more
    # specific product-type word already present, same reasoning as the strobe guard above.
    if re.search(r'\bmulti[\s-]?stick\b', text) \
       and not any(has_word(text, w) for w in _MORE_SPECIFIC_THAN_FINISH_WORD):
        return {'method':'CoOccurrence','matched_on':'multistick','L1':'Make up','L2':'Lip make-up','L3':'Lip & Cheek Stains & Tints','confidence':'Medium'}

    # FIX: "Contour" (stick/cream/powder) is a face-makeup sculpting product - excludes 'brush'
    # (the tool, handled by the guard above) and defers to a more specific product-type word or
    # a multi-shade "Palette" already present, same reasoning as the strobe guard above.
    if has_word(text,'contour') and not has_word(text,'brush') \
       and not any(has_word(text, w) for w in _MORE_SPECIFIC_THAN_FINISH_WORD):
        return {'method':'CoOccurrence','matched_on':'contour','L1':'Make up','L2':'Face make-up','L3':'Contour','confidence':'High'}

    # FIX: "Camouflage Cream" is a high-coverage concealer used to cover blemishes/scars/
    # pigmentation - same product family as a regular Concealer, just marketed under a
    # dermatology-adjacent name. Defers to "Palette" when present (a multi-shade camouflage
    # concealer kit), same reasoning as above.
    if has_word(text,'camouflage') and (has_word(text,'cream') or has_word(text,'concealer')) \
       and not has_word(text,'palette'):
        return {'method':'CoOccurrence','matched_on':'camouflage-cream','L1':'Make up','L2':'Face make-up','L3':'Concealer','confidence':'High'}

    # FIX: Vega/Ikonic/Majestique are hairbrush-focused tool brands - a bare "<brand> Brush" with
    # no other qualifying word (makeup-product guard above, or a specific hair-tool-type word
    # below) defaults to Hair Accessories rather than the generic Make-Up Brush Sets catch-all.
    if has_word(text,'brush') and any(has_word(text, b) for b in ('vega', 'ikonic', 'majestique')) and not any(
            has_word(text, w) for w in (
                'makeup', 'make up', 'tooth', 'brush head', 'hair', 'blow dry', 'blow drying',
                'detangling', 'paddle', 'cushion', 'flat', 'styling', 'round', 'shaving', 'shave',
                'bath', 'shower', 'body')):
        return {'method':'BrandOverride','matched_on':'brush+hair-tool-brand','L1':'Others','L2':'Others Others','L3':'Hair Accessories','confidence':'Medium'}

    # FIX: "Kajal Pen with Sharpener"/"Eyeliner with Sharpener" - the sharpener is a built-in
    # feature of the pencil-format product, not a separate item. Without this, the generic
    # 'sharpener' DIRECT_KEYWORDS entry (a longer word, checked first) wins over the actual
    # product name and mis-tags the whole SKU as a bare sharpener.
    if has_word(text,'sharpener'):
        if has_word(text,'kajal') or has_word(text,'kohl'):
            return {'method':'CoOccurrence','matched_on':'sharpener+kajal','L1':'Make up','L2':'Eye make-up','L3':'Kajal & Kohls','confidence':'High'}
        if has_word(text,'eyeliner') or has_word(text,'eye liner'):
            return {'method':'CoOccurrence','matched_on':'sharpener+eyeliner','L1':'Make up','L2':'Eye make-up','L3':'Eyeliners','confidence':'High'}
        if has_word(text,'eyebrow'):
            return {'method':'CoOccurrence','matched_on':'sharpener+eyebrow','L1':'Make up','L2':'Eye make-up','L3':'Eyebrow Colors/Pencil/Pen (Any)','confidence':'High'}
        if has_word(text,'lip liner') or has_word(text,'lip pencil'):
            return {'method':'CoOccurrence','matched_on':'sharpener+lip-liner','L1':'Make up','L2':'Lip make-up','L3':'Lip Liners','confidence':'High'}

    # FIX: "Eyelash Curler" is a metal/tool device that curls natural lashes - a beauty TOOL,
    # not the false-eyelashes product itself. The bare 'eyelash' DIRECT_KEYWORD would otherwise
    # tag it as False Lashes.
    if has_word(text,'eyelash') and (has_word(text,'curler') or has_word(text,'curling')):
        return {'method':'CoOccurrence','matched_on':'eyelash+curler','L1':'Others','L2':'Others Others','L3':'Others','confidence':'High'}

    # FIX: "Eyelash & Eyebrow Growth Serum" is ONE lash/brow growth-serum product, not a false-
    # eyelashes product plus a separate serum - "growth serum" is the decisive product identity.
    if has_word(text,'eyelash') and has_word(text,'growth serum'):
        return {'method':'CoOccurrence','matched_on':'eyelash+growth-serum','L1':'Face Skin','L2':'Face care','L3':'Face Serums/Essence','confidence':'Medium'}

    # FIX: a body-part word (legs, arms, underarm, knee, elbow) is a decisive body-vs-face
    # signal for any generic gel/lotion/moisturiser/moisturizer product - mirrors the same
    # body-context modifiers already added to 'cream' in CONTEXT_MATRIX, generalized to the
    # other format words that share the same bare-default-defaults-to-face problem.
    if (has_word(text,'gel') or has_word(text,'lotion') or has_word(text,'moisturiser') or has_word(text,'moisturizer')) \
       and any(has_word(text,w) for w in ('leg','arm','underarm','under arm','knee','elbow')) \
       and not (has_word(text,'face') or has_word(text,'lip') or has_word(text,'eye')):
        return {'method':'CoOccurrence','matched_on':'body-part-word-guard','L1':'Body Skin','L2':'Body care','L3':'Body Creams','confidence':'High'}

    # FIX: "Cologne Soap"/"Deo Soap"/"Perfumed Soap" is a soap product - Soap always wins over
    # any fragrance/deodorant wording when the SKU is explicitly a soap/bar.
    if (has_word(text,'soap') or has_word(text,'bathing bar')) and (
            has_word(text,'cologne') or has_word(text,'perfume') or has_word(text,'fragrance')
            or has_word(text,'deodorant') or has_word(text,'deo')
            or has_word(text,'eau de parfum') or has_word(text,'eau de toilette') or has_word(text,'edp') or has_word(text,'edt')):
        return {'method':'CoOccurrence','matched_on':'soap+fragrance-guard','L1':'Hygiene','L2':'Soaps','L3':'Solid Soap Bars','confidence':'High'}

    # FIX: "Ubtan Soap"/"Rose Water Soap"/"Multani Mitti Bathing Bar" - soap/bathing-bar format
    # always wins over an ingredient/marketing descriptor of that SAME soap, which otherwise
    # gets picked up by the more specific 'ubtan'/'multani mitti'/'rose water' DIRECT_KEYWORDS
    # entries (built for standalone face-pack/toner products) before the generic 'soap' base
    # ever gets a chance.
    if (has_word(text,'soap') or has_word(text,'bathing bar')) and (
            has_word(text,'ubtan') or has_word(text,'multani mitti') or has_word(text,'multani powder')
            or has_word(text,'ubtan powder') or has_word(text,'rose water')):
        return {'method':'CoOccurrence','matched_on':'soap+ingredient-descriptor-guard','L1':'Hygiene','L2':'Soaps','L3':'Solid Soap Bars','confidence':'High'}

    # FIX: "Serum Soap Bar"/"Scrub Soap" - soap format wins over a serum/scrub descriptor of
    # that SAME soap (mirrors the ubtan/rose-water guard above, and the cream/gel one in
    # bundle_detect.py's find_all_hits()).
    if (has_word(text,'soap') or has_word(text,'bathing bar')) and (has_word(text,'serum') or has_word(text,'scrub')):
        return {'method':'CoOccurrence','matched_on':'soap+serum-scrub-guard','L1':'Hygiene','L2':'Soaps','L3':'Solid Soap Bars','confidence':'High'}

    # FIX: deodorant/body-spray products marketed with the word "Perfume" must not be swept
    # into Fragrance just because "perfume" appears - deodorant signal wins.
    if (has_word(text,'deodorant') or has_word(text,'deo spray') or has_word(text,'body spray') or (has_word(text,'deo') and has_word(text,'spray'))) \
       and not (has_word(text,'eau de parfum') or has_word(text,'eau de toilette') or has_word(text,'edp')):
        return {'method':'CoOccurrence','matched_on':'deodorant+perfume-guard','L1':'Hygiene','L2':'Deos & Antiperspirants','L3':'Deodrant Sprays','confidence':'High'}

    # FIX: Skore is an intimate-wellness/condom brand (lubricant gels, etc.) - its products
    # were falling into the generic bare 'gel' skincare default (Face Care/Moisturisers) since
    # nothing else in the name signals otherwise. Brand alone is a reliable enough signal here.
    if 'skore' in text:
        return {'method':'BrandOverride','matched_on':'skore','L1':'Others','L2':'Others Others','L3':'Others','confidence':'High'}


    # FIX: Jergens is unambiguously a body-only brand (unlike CeraVe/Cetaphil which genuinely
    # sell both face and body lines) - targeted override for bare moisturizer mentions.
    if (has_word(text,'moisturiser') or has_word(text,'moisturizer')) \
       and not (has_word(text,'face') or has_word(text,'facial') or has_word(text,'body') or has_word(text,'tinted')) \
       and 'jergens' in text:
        return {'method':'BrandOverride','matched_on':'jergens+moisturizer','L1':'Body Skin','L2':'Body care','L3':'Body Lotions','confidence':'High'}

    # FIX: CeraVe's bare cream/moisturiser mentions (no face/body word) skew strongly Body
    # in real data (20 of 27 clean bare-mention CeraVe samples in the April Blinkit dataset) -
    # the opposite skew from the general population default below, and from Cetaphil, which
    # remains an accepted residual gap (too small/mixed a sample to justify an override).
    if (has_word(text,'moisturiser') or has_word(text,'moisturizer') or has_word(text,'cream')) \
       and not (has_word(text,'face') or has_word(text,'facial') or has_word(text,'body') or has_word(text,'tinted')) \
       and 'cerave' in text:
        return {'method':'BrandOverride','matched_on':'cerave+cream/moisturizer','L1':'Body Skin','L2':'Body care','L3':'Body Creams','confidence':'High'}

    # FIX: "Sunscreen" is highest priority when the literal word is present.
    if has_word(text,'sunscreen'):
        if has_word(text,'body'):
            return {'method':'CoOccurrence','matched_on':'sunscreen-priority+body','L1':'Face Skin','L2':'Sun care','L3':'Body Sunscreen','confidence':'High'}
        return {'method':'CoOccurrence','matched_on':'sunscreen-priority','L1':'Face Skin','L2':'Sun care','L3':'Sunscreen','confidence':'High'}

    # FIX: Oral care products (teeth/tooth/dental) must win over generic serum/powder/gel defaults.
    if (has_word(text,'teeth') or has_word(text,'tooth') or has_word(text,'dant') or has_word(text,'dental')):
        if has_word(text,'powder'):
            return {'method':'CoOccurrence','matched_on':'oral+powder','L1':'Others','L2':'Others Others','L3':'Oral Care','confidence':'High'}
        if has_word(text,'gel'):
            return {'method':'CoOccurrence','matched_on':'oral+gel','L1':'Others','L2':'Others Others','L3':'Oral Care','confidence':'High'}
        if has_word(text,'serum') or has_word(text,'whitening'):
            return {'method':'CoOccurrence','matched_on':'oral+serum','L1':'Others','L2':'Others Others','L3':'Oral Care','confidence':'High'}

    # FIX: "Rechargeable" + blackhead/whitehead remover is an ELECTRIC device.
    if has_word(text,'rechargeable') and (has_word(text,'blackhead') or has_word(text,'whitehead') or has_word(text,'remover')):
        return {'method':'CoOccurrence','matched_on':'rechargeable+remover','L1':'Others','L2':'Others Others','L3':'All electric Appliances - Hair Dryers, Straighteners, Electric Razors','confidence':'High'}

    # FIX: "Henna Shampoo Bar" - henna is an ingredient, not the product type. Shampoo wins.
    if has_word(text,'shampoo') and (has_word(text,'henna') or has_word(text,'mehendi')) and not has_word(text,'hair colour') and not has_word(text,'hair color'):
        return {'method':'CoOccurrence','matched_on':'shampoo+henna','L1':'Hair Care','L2':'Shampoo','L3':'All kinds of Shampoos','confidence':'High'}

    # FIX: "Cream Conditioner" explicitly says conditioner - don't let the cream+hair guard
    # (meant for "Hair Cream" styling products) override an actual conditioner.
    if has_word(text,'cream') and has_word(text,'hair') and has_word(text,'conditioner'):
        return {'method':'CoOccurrence','matched_on':'cream+hair+conditioner','L1':'Hair Care','L2':'Rinse-Off Conditioners','L3':'Rinse Off Conditioners','confidence':'High'}

    # FIX: "Leave in Hair Styling Cream" explicitly says leave-in - don't let the generic
    # cream+hair guard below override an explicit leave-in claim.
    if has_word(text,'cream') and has_word(text,'hair') and (has_word(text,'leave in') or has_word(text,'leave-in')):
        return {'method':'CoOccurrence','matched_on':'cream+hair+leave-in','L1':'Hair Care','L2':'Leave-In Conditioners','L3':'Leave In Creams, Leave In Conditioners','confidence':'High'}

    # FIX: "Hair Color Cream"/"Cream Hair Color" is a permanent hair-dye product in cream
    # format, not a hair-styling cream - must be checked before the generic cream+hair guard
    # below, which would otherwise sweep it into Styling/Hair Lotions & Tonics.
    if has_word(text,'cream') and has_word(text,'hair') and (
            has_word(text,'colour') or has_word(text,'color') or has_word(text,'dye')
            or has_word(text,'colourant') or has_word(text,'colorant')):
        return {'method':'CoOccurrence','matched_on':'cream+hair+colour','L1':'Hair colour','L2':'Creme','L3':'Crème','confidence':'High'}

    # FIX: "Hair Cream" (no explicit 'styling' word) was falling through to the generic
    # Face Creams default - needs its own bare 'hair' modifier. Excludes colour/color/dye -
    # "Hair Color Cream" is a permanent hair-dye product, not a styling cream, and must fall
    # through to the hair-colour resolution logic instead.
    if has_word(text,'cream') and has_word(text,'hair') and not (
            has_word(text,'wax') or has_word(text,'removal')
            or has_word(text,'colour') or has_word(text,'color') or has_word(text,'dye')
            or has_word(text,'colourant') or has_word(text,'colorant')):
        return {'method':'CoOccurrence','matched_on':'cream+hair','L1':'Hair Care Others','L2':'Styling','L3':'Hair Lotions & Tonics','confidence':'High'}

    # FIX: "Gel Hair Colour" - a hair-DYE product in gel format, not a hair-styling gel.
    if has_word(text,'gel') and (has_word(text,'hair colour') or has_word(text,'hair color')):
        return {'method':'ContextMatrix-Default','matched_on':'gel+hair-colour-guard','L1':'Hair colour','L2':'Creme','L3':'Crème','confidence':'Medium'}

    # FIX: "Beard Oil with Derma Roller" / "Shampoo with Treatment Comb" - the accessory is a
    # bonus item, not the primary product. Primary product word wins.
    if has_word(text,'oil') and (has_word(text,'derma roller') or has_word(text,'derma stamp')):
        if has_word(text,'hair') or has_word(text,'beard'):
            return {'method':'CoOccurrence','matched_on':'oil+derma-roller-accessory','L1':'Hair Care Others','L2':'Hair Oils','L3':'Hair Oils','confidence':'High'}
    if has_word(text,'shampoo') and has_word(text,'comb'):
        return {'method':'CoOccurrence','matched_on':'shampoo+comb-accessory','L1':'Hair Care','L2':'Shampoo','L3':'All kinds of Shampoos','confidence':'High'}

    # FIX: "Hair Oil Applicator" is a tool, not the oil product itself. But "Lip Oil With Fat
    # Applicator" is a finished lip-oil product whose built-in wand happens to be called an
    # applicator - excluded so it doesn't get swept into a bare tool bucket.
    if has_word(text,'oil') and has_word(text,'applicator') and not has_word(text,'lip'):
        return {'method':'CoOccurrence','matched_on':'oil+applicator','L1':'Others','L2':'Others Others','L3':'Others','confidence':'High'}

    # FIX: "Beard Oil" should be treated as Hair Care family, same as "Hair Oil".
    if has_word(text,'oil') and has_word(text,'beard') and not has_word(text,'wax'):
        return {'method':'CoOccurrence','matched_on':'beard+oil','L1':'Hair Care Others','L2':'Hair Oils','L3':'Hair Oils','confidence':'High'}

    # FIX: "Cologne Talc" is a scented talcum powder, not a fragrance product.
    if has_word(text,'cologne') and has_word(text,'talc'):
        return {'method':'CoOccurrence','matched_on':'cologne+talc','L1':'Hygiene','L2':'Body care (Talc)','L3':'Talcum Powders','confidence':'High'}

    # FIX: "Aloe Vera Gel for Skin & Hair" - dual-use skin product, not primarily a hair gel.
    if has_word(text,'gel') and has_word(text,'skin') and has_word(text,'hair'):
        return {'method':'CoOccurrence','matched_on':'gel+skin+hair','L1':'Face Skin','L2':'Face care','L3':'Moisturisers','confidence':'Medium'}

    if has_word(text,'cream') and has_word(text,'wax') and not has_word(text,'wax heater'):
        if has_word(text,'removal'):
            return {'method':'CoOccurrence','matched_on':'cream+wax+removal','L1':'Hygiene','L2':'Shaving','L3':'Depilatory','confidence':'High'}
        return {'method':'CoOccurrence','matched_on':'cream+wax','L1':'Hair Care Others','L2':'Styling','L3':'Waxes','confidence':'High'}

    # FIX: "Cream Soap"/"Cream Bar" - 'cream' base intercepts before 'soap'/'bar' gets checked.
    if has_word(text,'cream') and (has_word(text,'soap') or has_word(text,'bathing bar') or has_word(text,'bar')) \
       and not (has_word(text,'shaving') or has_word(text,'ice cream')):
        return {'method':'CoOccurrence','matched_on':'cream+soap/bar','L1':'Hygiene','L2':'Soaps','L3':'Solid Soap Bars','confidence':'High'}

    # FIX: powder-format wax (e.g. "Bikini Wax Powder") is virtually always hair-removal.
    if has_word(text,'wax') and has_word(text,'powder'):
        return {'method':'CoOccurrence','matched_on':'wax+powder','L1':'Hygiene','L2':'Shaving','L3':'Depilatory','confidence':'High'}

    # FIX: "Face & Body" named together defaults to Body, matching real evidence.
    face_and_body = has_word(text,'face') and has_word(text,'body') and (
        'face body' in text or 'face and body' in text or 'face & body' in text or
        'body face' in text or 'body and face' in text)
    if face_and_body and has_word(text,'scrub'):
        return {'method':'CoOccurrence','matched_on':'face+body+scrub','L1':'Hygiene','L2':'Body scrub','L3':'Body scrub- cremes and powders','confidence':'Medium'}
    if face_and_body and has_word(text,'oil'):
        return {'method':'CoOccurrence','matched_on':'face+body+oil','L1':'Body Skin','L2':'Body care','L3':'Body Oils','confidence':'Medium'}

    # FIX: "Powder Makeup Brush" - a brush accessory, not talcum powder. Not always adjacent.
    if has_word(text,'powder') and has_word(text,'brush'):
        return {'method':'CoOccurrence','matched_on':'powder+brush','L1':'Make up','L2':'Make up Others','L3':'Make-Up Brush Sets','confidence':'High'}

    # FIX: "Tan & Dead Skin Removal...Cream/Gel" - same intent as the literal 'tan removal'
    # LOW_PRIORITY_KEYWORDS phrase, but 'tan' and 'removal' aren't adjacent here - must win
    # over the generic bare cream/gel default, which would otherwise fire first.
    if has_word(text,'tan') and (has_word(text,'removal') or has_word(text,'remove')) and not has_word(text,'tan removal'):
        return {'method':'CoOccurrence','matched_on':'tan+removal','L1':'Face Skin','L2':'Face cleansers','L3':'Face Pack','confidence':'High'}

    # FIX: "Body Lotion with SPF" - the 'lotion' base's 'body' modifier would otherwise win
    # over sun-protection intent entirely (no 'spf' signal on that base at all). Matches
    # 'sunscreen' base's own body/bare split: explicit 'body' -> Body Sunscreen, else Sunscreen.
    if has_word(text,'lotion') and has_word(text,'spf'):
        if has_word(text,'body'):
            return {'method':'CoOccurrence','matched_on':'lotion+spf+body','L1':'Face Skin','L2':'Sun care','L3':'Body Sunscreen','confidence':'High'}
        return {'method':'CoOccurrence','matched_on':'lotion+spf','L1':'Face Skin','L2':'Sun care','L3':'Sunscreen','confidence':'High'}

    # FIX: "Anti-Dandruff Lotion" is a pre-wash scalp treatment, not a generic body lotion -
    # dandruff is exclusively a scalp condition, same destination the equivalent hair-treatment/
    # serum dandruff guards already use.
    if has_word(text,'lotion') and has_word(text,'dandruff'):
        return {'method':'CoOccurrence','matched_on':'lotion+dandruff','L1':'Hair Care','L2':'Hair Serums','L3':'Scalp Serums, Hair Regrowth Serums, Anti Hair Fall Serums','confidence':'High'}

    # FIX: an explicitly "Heatless" hair curler/roller is a manual accessory (foam/silk/
    # ribbon), not an electric appliance - must win over TIER0_HARDWARE's bare 'hair curler'
    # default, which assumes electric.
    if has_word(text,'heatless') and (has_word(text,'curler') or has_word(text,'curling') or has_word(text,'roller')):
        return {'method':'CoOccurrence','matched_on':'heatless+curler/roller','L1':'Others','L2':'Others Others','L3':'Hair Accessories','confidence':'High'}

    for kw, dest in TIER0_HARDWARE.items():
        if has_word(text, kw):
            return {'method':'Tier0-Hardware','matched_on':kw,'L1':dest[0],'L2':dest[1],'L3':dest[2],'confidence':'High'}
    for kw, l3 in FEMININE_HYGIENE_KEYWORDS.items():
        if has_word(text, kw):
            return {'method':'Feminine-Hygiene','matched_on':kw,'L1':'Others','L2':'Others Others','L3':l3,'confidence':'High'}
    for kw, l3 in BABY_CARE_KEYWORDS.items():
        if has_word(text, kw):
            return {'method':'Baby-Care','matched_on':kw,'L1':'Others','L2':'Others Others','L3':l3,'confidence':'High'}
    for phrase, l1, l2, l3 in SPECIFICITY_PHRASES:
        if has_word(text, phrase):
            return {'method':'Specificity-Phrase','matched_on':phrase,'L1':l1,'L2':l2,'L3':l3,'confidence':'High'}
    for kw, dest in sorted(DIRECT_KEYWORDS.items(), key=lambda kv: -len(kv[0])):
        # FIX: "Attar Ayurveda" is a brand name containing the word "attar" - not an actual
        # attar/ittar fragrance product being sold. Mirrors the existing "Ghar Soaps" guard.
        if kw == 'attar' and has_word(text, 'attar ayurveda'):
            continue
        # FIX: "Lip & Cheek Tint (Blush Nude)"/"...(Mascara Tear)" etc. - when the product name
        # already contains "tint" (a decisive, specific product identity, e.g. "Lip & Cheek
        # Tint"), a bare 'blush'/'blusher' hit is almost always the SHADE name, not a second
        # Blush product - let it fall through to the CONTEXT_MATRIX 'tint' rule below instead.
        if kw in ('blush', 'blusher') and has_word(text, 'tint'):
            continue
        # FIX: "...Lip & Cheek Tint (Mascara Tear)" - same shade-name reasoning as the blush
        # guard above, applied to Mascara.
        if kw == 'mascara' and has_word(text, 'tint'):
            continue
        # FIX: "Ubtan...Sheet Mask"/"Ubtan...Body Scrub"/"Ubtan...Bath & Body Oil" is ONE
        # product - "Mask"/"Scrub"/"Oil" is the decisive product identity, ubtan is the
        # formulation/product-line name (e.g. the "Rejuvenating UBTAN" bath-oil line). Mirrors
        # the soap-wins guard.
        if kw == 'ubtan' and (has_word(text, 'mask') or has_word(text, 'scrub') or has_word(text, 'oil')):
            continue
        # FIX: "Hair Cleanser"/"Hair Oil" with Mulethi/Multani Mitti as an ingredient - the
        # already-named hair product wins over the generic Face Pack ingredient identity.
        if kw in ('mulethi', 'multani mitti') and (has_word(text, 'hair cleanser') or has_word(text, 'hair oil')):
            continue
        # FIX: "Blush & Glow" (Lakme's own product-line name) is a brand/line name containing
        # the word "blush" - not an actual Blush makeup product.
        if kw in ('blush', 'blusher') and (has_word(text, 'blush & glow') or has_word(text, 'blush and glow')):
            continue
        # FIX: "Rose Water...Face Wipes" - Rose Water is the wipes' ingredient/marketing
        # descriptor, not a separate Face Care product. Mirrors "oil is never primary".
        if kw == 'rose water' and (has_word(text, 'wipes') or has_word(text, 'wipe')):
            continue
        if has_word(text, kw):
            return {'method':'Direct-Keyword','matched_on':kw,'L1':dest[0],'L2':dest[1],'L3':dest[2],'confidence':'High'}

    # FIX: "Serum Beauty Bar" (Dove) is a soap product - not always adjacent.
    if has_word(text,'serum') and has_word(text,'bar') and not has_word(text,'serum bar'):
        return {'method':'CoOccurrence','matched_on':'serum+bar','L1':'Hygiene','L2':'Soaps','L3':'Solid Soap Bars','confidence':'High'}

    # FIX: "Primer" always wins over a bare moisturizer/cream mention (e.g. "Moisturizer and
    # Primer") - checked after DIRECT_KEYWORDS so specific compound matches (BB Cream, Strobe
    # Cream, etc.) still win first; this only catches the generic bare case.
    if has_word(text,'primer') and (has_word(text,'moisturiser') or has_word(text,'moisturizer') or has_word(text,'cream') or has_word(text,'creme')):
        return {'method':'CoOccurrence','matched_on':'primer+moisturizer','L1':'Make up','L2':'Face make-up','L3':'Primers','confidence':'High'}

    # FIX: "CC Serum"/"BB Serum" is the same colour-correcting/beauty-balm product family as a
    # CC/BB Cream, just in serum format - a face-makeup product, not a skincare serum. Checked
    # before the serum CONTEXT_MATRIX's own spf/tinted modifiers, which would otherwise redirect
    # it to Sunscreen/Tinted Moisturisers.
    if has_word(text,'cc serum'):
        return {'method':'CoOccurrence','matched_on':'cc-serum','L1':'Make up','L2':'Face make-up','L3':'CC Creams','confidence':'High'}
    if has_word(text,'bb serum'):
        return {'method':'CoOccurrence','matched_on':'bb-serum','L1':'Make up','L2':'Face make-up','L3':'Bb Creams','confidence':'High'}

    # FIX: per explicit direction, "CC"/"BB" (with or without a trailing "+") is ALWAYS a face-
    # makeup product family - the exact-adjacency checks above ("cc cream"/"bb cream"/"cc serum"/
    # "bb serum") miss real cases where another word sits in between ("Cc + Bb Hybrid Cream",
    # "CC Skin Tint", "CC+ Matcha Green...SPF 50", "CC Mousse") and were falling to Sunscreen,
    # generic Face Creams, or even Hair Mousses (via that base's own bare default). Excludes
    # 'hair'/'brush' so a genuine haircare product or a brand's model/part code (e.g. "VEGA
    # Bleach Brush (BB-01)") is never swept in - neither of those ever co-occurs with a real
    # cosmetic-format word anyway.
    _CC_BB_FORMAT_WORDS = ('cream', 'creme', 'serum', 'tint', 'tinted', 'balm', 'mousse', 'spf', 'foundation', 'gel')
    if (has_word(text,'cc') or has_word(text,'bb')) and any(has_word(text,w) for w in _CC_BB_FORMAT_WORDS) \
       and not has_word(text,'hair') and not has_word(text,'brush'):
        if has_word(text,'foundation'):
            return {'method':'CoOccurrence','matched_on':'cc-bb+foundation','L1':'Make up','L2':'Face make-up','L3':'Foundation','confidence':'High'}
        # "Skin Tint" specifically is a distinct, decisive modern product-category name - a bare
        # "Tint"/"Tinted" alongside "BB/CC Cream" is usually just describing the cream's own
        # shade/finish (e.g. "BB Cream SPF 30 Tube Tint", "Tinted CC Cream"), not a different
        # product, so it stays a Bb/CC Cream rather than being diluted into Tinted Moisturisers.
        if has_word(text,'skin tint'):
            return {'method':'CoOccurrence','matched_on':'cc-bb+skin-tint','L1':'Make up','L2':'Face make-up','L3':'Tinted Moisturisers','confidence':'High'}
        if has_word(text,'contour'):
            return {'method':'CoOccurrence','matched_on':'cc-bb+contour','L1':'Make up','L2':'Face make-up','L3':'Contour','confidence':'High'}
        if has_word(text,'blush'):
            return {'method':'CoOccurrence','matched_on':'cc-bb+blush','L1':'Make up','L2':'Face make-up','L3':'Blushes','confidence':'High'}
        l3 = 'CC Creams' if has_word(text,'cc') else 'Bb Creams'
        return {'method':'CoOccurrence','matched_on':'cc-bb-format-word','L1':'Make up','L2':'Face make-up','L3':l3,'confidence':'High'}

    # FIX: a serum explicitly marketed as a pre-makeup step, or bare "Makeup Serum", is a
    # primer-adjacent face-makeup product, not a skincare serum - "acid"/hydration/anti-ageing/
    # glow language alone does NOT trigger this; only an explicit makeup/pre-makeup framing does.
    if has_word(text,'serum') and (has_word(text,'pre makeup') or has_word(text,'pre-makeup')
                                    or has_word(text,'makeup serum') or has_word(text,'make up serum')):
        return {'method':'CoOccurrence','matched_on':'makeup-serum','L1':'Make up','L2':'Face make-up','L3':'Primers','confidence':'High'}

    # FIX: "Gel Face Wash"/"Foaming Gel Face Wash" - gel is a texture descriptor for the wash,
    # not a second product.
    if has_word(text,'gel') and has_word(text,'face wash'):
        return {'method':'CoOccurrence','matched_on':'gel+face-wash','L1':'Face Skin','L2':'Face cleansers','L3':'Face Wash/Face Cleansers','confidence':'High'}
    if has_word(text,'gel') and has_word(text,'hand wash'):
        return {'method':'CoOccurrence','matched_on':'gel+hand-wash','L1':'Hygiene','L2':'Body wash','L3':'Hand Wash','confidence':'High'}
    if has_word(text,'gel') and has_word(text,'body wash'):
        return {'method':'CoOccurrence','matched_on':'gel+body-wash','L1':'Hygiene','L2':'Body wash','L3':'Body washes (generic/cream-based)','confidence':'High'}

    # FIX: "Creme/Cream Face Wash" - same principle as the gel guards above.
    if (has_word(text,'cream') or has_word(text,'creme')) and has_word(text,'face wash'):
        return {'method':'CoOccurrence','matched_on':'cream+face-wash','L1':'Face Skin','L2':'Face cleansers','L3':'Face Wash/Face Cleansers','confidence':'High'}
    # FIX: "Cream Conditioner" is a hair-conditioner FORMAT, not a separate face-cream product.
    if has_word(text,'cream conditioner'):
        return {'method':'CoOccurrence','matched_on':'cream+conditioner','L1':'Hair Care','L2':'Rinse-Off Conditioners','L3':'Rinse Off Conditioners','confidence':'High'}

    # FIX: "Face Serum & Cleanser" - if it's explicitly a Serum that also has a cleansing
    # claim, Serum format wins (Face Care); but if "Wash" is the word used (a clearer
    # cleansing-primary signal than bare "Cleanser"), Face Cleansers wins instead.
    if has_word(text,'serum') and has_word(text,'wash'):
        return {'method':'CoOccurrence','matched_on':'serum+wash','L1':'Face Skin','L2':'Face cleansers','L3':'Face Wash/Face Cleansers','confidence':'High'}

    # FIX: a bare "Serum" from a known hair-only brand (Livon, Tresemme, etc. - the same list
    # resolve_mask() already trusts) or explicitly for "Dandruff" (an exclusively scalp/hair
    # condition) is a hair serum, not the generic Face Serums/Essence bare default - neither
    # says the word "hair" literally.
    if has_word(text,'serum') and not (has_word(text,'face') or has_word(text,'facial')):
        if has_word(text,'dandruff'):
            return {'method':'CoOccurrence','matched_on':'serum+dandruff','L1':'Hair Care','L2':'Hair Serums','L3':'Hair Serums','confidence':'High'}
        # FIX: a serum whose only signal is a hair-specific concern word (never says "hair" or
        # "scalp" literally) was falling through to the generic Face Serums/Essence default -
        # e.g. "Bond Repair Serum", "Anti Frizz Serum". Note: "peptide" is included per explicit
        # direction, though peptide serums are also very commonly a FACE-skincare category in
        # general use - worth a second look if this over-triggers on real data.
        # "peptide" was deliberately dropped from this list - peptide serums are just as commonly
        # a FACE-skincare category in general use, so it isn't decisive on its own the way these
        # other words are. A peptide serum still correctly resolves to Hair Care when a genuine
        # hair signal is also present - the literal word "hair" (via the CONTEXT_MATRIX 'serum'
        # base's own 'hair' modifier, checked further below) or a HAIR_ONLY_BRANDS brand name.
        _HAIR_SERUM_SIGNALS = (
            'bond repair', 'bond building', 'strong roots', 'root strength', 'hair fall',
            'hairfall', 'anti frizz', 'frizz control', 'anti grey', 'anti gray',
        )
        if any(has_word(text, w) for w in _HAIR_SERUM_SIGNALS):
            return {'method':'CoOccurrence','matched_on':'serum+hair-signal','L1':'Hair Care','L2':'Hair Serums','L3':'Hair Serums','confidence':'High'}
        if any(has_word(text, b) for b in HAIR_ONLY_BRANDS):
            return {'method':'CoOccurrence','matched_on':'serum+hair-brand','L1':'Hair Care','L2':'Hair Serums','L3':'Hair Serums','confidence':'High'}
    if has_word(text,'serum') and has_word(text,'cleanser') and not has_word(text,'wash'):
        return {'method':'CoOccurrence','matched_on':'serum+cleanser','L1':'Face Skin','L2':'Face care','L3':'Face Serums/Essence','confidence':'High'}

    # FIX: "Roll On Serum" - real-data audit found this is overwhelmingly a Hair Growth Serum
    # sold in a roll-on applicator format (Be Bodywise, Plix, Pilgrim, ThriveCo, WOW, Bare
    # Anatomy - 17 of 18 real examples), NOT an "Underarm Roll On Serum" deodorant (only 1 of
    # 18) as this guard originally assumed. A hair-context word (hair/scalp/growth/hairfall)
    # now wins first; only a bare "Roll On Serum" with no hair signal at all falls to Deos.
    if has_word(text,'serum') and (has_word(text,'roll on') or has_word(text,'roll-on') or has_word(text,'rollon')):
        if any(has_word(text,w) for w in ('hair','scalp','growth','hairfall','hair fall')):
            return {'method':'CoOccurrence','matched_on':'serum+rollon+hair','L1':'Hair Care','L2':'Hair Serums','L3':'Hair Serums','confidence':'High'}
        return {'method':'CoOccurrence','matched_on':'serum+rollon','L1':'Hygiene','L2':'Deos & Antiperspirants','L3':'Roll ons','confidence':'High'}

    for base in sorted(CONTEXT_MATRIX.keys(), key=lambda b: -len(b)):
        rules = CONTEXT_MATRIX[base]
        # FIX: "with Skin Conditioners" is a cosmetic-ingredient/INCI term for a moisturizing
        # agent in a body wash / shower gel, not a rinse-off HAIR conditioner product. Mirrors
        # the equivalent guard in bundle_detect.py's find_all_hits().
        if base == 'conditioner' and has_word(text, 'skin conditioner'):
            continue
        if has_word(text, base):
            for mod, l1, l2, l3, conf in rules:
                if mod is None:
                    if l1 is None:
                        return {'method':'Ambiguous-NoModifier','matched_on':base,'L1':None,'L2':None,'L3':None,'confidence':'Low-Flag'}
                    return {'method':'ContextMatrix-Default','matched_on':base,'L1':l1,'L2':l2,'L3':l3,'confidence':conf}
                if has_word(text, mod):
                    if l1 is None:
                        return {'method':'Ambiguous-Gap','matched_on':f'{base}+{mod}','L1':None,'L2':None,'L3':None,'confidence':'Gap-Flag'}
                    return {'method':'ContextMatrix-Modifier','matched_on':f'{base}+{mod}','L1':l1,'L2':l2,'L3':l3,'confidence':conf}

    # Low-priority ingredient/descriptor words - only apply if nothing more specific matched.
    # Sorted by length descending so more specific/longer words win (e.g. 'perfume' beats
    # 'blush' when a perfume product uses "Blush" as a scent name).
    for kw, dest in sorted(LOW_PRIORITY_KEYWORDS.items(), key=lambda kv: -len(kv[0])):
        if has_word(text, kw):
            conf = 'High' if kw in LOW_PRIORITY_HIGH_CONFIDENCE else 'Medium'
            return {'method':'LowPriority-Keyword','matched_on':kw,'L1':dest[0],'L2':dest[1],'L3':dest[2],'confidence':conf}

    # Last-resort fallback: bare "hair colour/color/dye" with NO other signal at all
    # (must run AFTER every specific base above - shampoo, remover, creme, etc.)
    if has_word(text, 'hair colour') or has_word(text, 'hair color') or has_word(text,'hair dye'):
        if (has_word(text,'highlight') or has_word(text,'highlighting')):
            return {'method':'ContextMatrix-Default','matched_on':'hair colour+highlight','L1':'Hair colour','L2':'Hair colour Others','L3':'Highlight Kits','confidence':'High'}
        if has_word(text,'stick') or has_word(text,'touch up') or has_word(text,'touchup'):
            return {'method':'ContextMatrix-Default','matched_on':'hair colour+stick','L1':'Hair colour','L2':'Hair colour Others','L3':'Hair Mascaras & Root Touch Ups','confidence':'High'}
        # 98.6% real-data agreement with historical tags (694-row check) - promoted from
        # Medium to High on that evidence, same as the other empirically-validated defaults
        # below (see LOW_PRIORITY_HIGH_CONFIDENCE and the 'razor'/'cleanser'/etc. None-defaults).
        return {'method':'ContextMatrix-Default','matched_on':'hair colour (fallback)','L1':'Hair colour','L2':'Creme','L3':'Crème','confidence':'High'}

    # FIX: "Dior Forever" is Dior's long-wear foundation LINE name - shade/finish names built on
    # it ("Glow Maximizer", "Natural Velvet", "Nude Matte Filter") have no other keyword at all.
    # Deliberately placed at the very end, same reasoning as the supplement/hair-treatment
    # fallbacks below: "DIOR Forever Skin Correct...Concealer" or "...Nude Bronze Powder"
    # EXPLICITLY names its own product type, which must win over the line-name guess - those
    # already matched via 'concealer'/'bronze' earlier in this function, so only a genuinely
    # unlabelled Dior Forever product (no other keyword at all) reaches this point.
    if 'dior forever' in text:
        return {'method':'BrandOverride','matched_on':'dior forever','L1':'Make up','L2':'Face make-up','L3':'Foundation','confidence':'Medium'}

    # FIX: nutraceutical/dietary supplements (biotin/collagen/multivitamin gummies, tablets,
    # capsules for hair-skin-nails) are out of BPC scope entirely - 75+ real examples (HealthKart
    # HK Vitals, OZiva, Carbamide Forte) were falling to Unmapped. Deliberately placed as the
    # LAST check in the whole function, not an early guard: "biotin"/"collagen" are also
    # genuine topical ingredients (Biotin Shampoo, Collagen Face Cream), so a real topical
    # product must already have matched its own format word (shampoo/cream/serum/etc.) earlier
    # in this function before ever reaching here - only a product with no topical format word
    # at all falls through to this point.
    if any(has_word(text, w) for w in (
            'biotin', 'collagen', 'multivitamin', 'supplement', 'dht blocker',
            'hk vitals', 'oziva', 'gummies', 'gummy')):
        return {'method':'CoOccurrence','matched_on':'supplement-fallback','L1':'Others','L2':'Others Others','L3':'Others','confidence':'Medium'}

    # FIX: hair-damage-repair products named as a "Treatment"/"Concentrate"/"Therapy"/"Duo"
    # (not oil/cream/serum/gel) with no other recognized format word - 18+ real examples
    # (mise en scene, La'dor, Redken, WishCare) were falling to Unmapped. Same "last resort"
    # placement as the supplement guard above: a product with an actual format word already
    # matched earlier via that word's own base/modifier.
    if any(has_word(text, w) for w in (
            'damaged hair', 'curly hair', 'bond repair', 'frizzy hair', 'hair breakage',
            'keratin treatment', 'hair tonic', 'frizz free', 'anti dandruff', 'hair fall control')):
        return {'method':'CoOccurrence','matched_on':'hair-treatment-fallback','L1':'Hair Care','L2':'Hair Serums','L3':'Hair Serums','confidence':'Medium'}

    # LAST RESORT: a genuinely un-formatted product (no cream/oil/serum/gel/treatment word this
    # function recognizes) from a brand that sells ONLY haircare - a real-data audit found ~150
    # such products (Aveda "Thickening Tonic", Schwarzkopf Bonacure "Treatment", Olaplex "Duo"/
    # "Set", Alan Truman "Styler", etc.), all clearly haircare by brand alone even though nothing
    # in the name says "hair". Explicit direction: never leave a SKU fully untagged when a
    # reasonable best guess exists - Low confidence flags it for manual review instead of hiding
    # it as Unmapped, where it can't be corrected at all.
    # FIX: "Hairstyling Tool Set - Fishbone, Ponytail & Bow" (a plastic hair-tool kit) was
    # landing in Hair Lotions & Tonics via the fallbacks below purely because it says "Hair" -
    # a tool/accessory-context word means this is a physical Hair Accessory, not a lotion/tonic.
    # Shared by both the brand and bare-word fallbacks immediately below.
    _hair_fallback_l1l2l3 = ('Others','Others Others','Hair Accessories') if any(has_word(text, w) for w in (
            'tool', 'accessory', 'accessories', 'comb', 'pin', 'band', 'claw', 'bow',
            'ponytail', 'fishbone', 'clutcher', 'clip')) else ('Hair Care Others','Styling','Hair Lotions & Tonics')

    if any(has_word(text, b) for b in HAIR_ONLY_BRANDS):
        return {'method':'BrandOverride','matched_on':'hair-only-brand-fallback','L1':_hair_fallback_l1l2l3[0],'L2':_hair_fallback_l1l2l3[1],'L3':_hair_fallback_l1l2l3[2],'confidence':'Low'}

    # LAST RESORT: mirrors the hair-only-brand fallback above, for colour-cosmetics-only
    # brands - per explicit direction, when the engine can't tell what a SKU is from its own
    # name, the brand itself is often the decisive signal ("mostly these brands which are
    # related to makeup will tell us it is a makeup product"). Any real skincare/haircare
    # product from one of these brands would already have resolved via its own keyword long
    # before reaching this point, so this only catches genuinely unrecognized makeup items.
    if any(has_word(text, b) for b in MAKEUP_ONLY_BRANDS):
        return {'method':'BrandOverride','matched_on':'makeup-only-brand-fallback','L1':'Make up','L2':'Make up Others','L3':'Others','confidence':'Low'}

    # LAST RESORT: mirrors the two brand fallbacks above, for fragrance-only brands - real-data
    # audit found many "Gift Set"/"Gift Collection" fragrance names (Bella Vita, Yardley London,
    # Beverly Hills Polo Club) with zero other keyword at all. Picks the Combo/Sets/Samples L3
    # when the name itself says "gift"/"set"/"collection"/"kit", otherwise the bare Perfume L3.
    if any(has_word(text, b) for b in FRAGRANCE_ONLY_BRANDS):
        if any(has_word(text, w) for w in ('gift', 'set', 'collection', 'kit')):
            return {'method':'BrandOverride','matched_on':'fragrance-only-brand-fallback','L1':'Fragrance','L2':'Perfume','L3':'Perfume Combo/Sets/Samples','confidence':'Low'}
        return {'method':'BrandOverride','matched_on':'fragrance-only-brand-fallback','L1':'Fragrance','L2':'Perfume','L3':'Perfume','confidence':'Low'}

    # LAST RESORT: mirrors the brand fallbacks above, for men's shaving/grooming-kit-only
    # brands (Bombay Shaving Company, Kimirica) - real-data audit found many "Grooming Kit"/
    # "Beard Kit" names with zero other keyword at all.
    # Excludes 'facial'/'face' - Bombay Shaving Company also sells face/skincare kits, not just
    # shaving ones (e.g. "Activated Charcoal Facial Gift Kit For Men", correctly Facial Kit).
    if any(has_word(text, b) for b in GROOMING_ONLY_BRANDS) and not any(has_word(text,w) for w in ('facial','face')):
        if has_word(text,'balm'):
            return {'method':'BrandOverride','matched_on':'grooming-only-brand-fallback+balm','L1':'Hygiene','L2':'Shaving','L3':'Shave balms/ lotions','confidence':'Low'}
        return {'method':'BrandOverride','matched_on':'grooming-only-brand-fallback','L1':'Hygiene','L2':'Shaving','L3':'Shaving Kit','confidence':'Low'}

    # LAST RESORT (broader still): the literal word "hair"/"hairfall"/"scalp" is present but
    # nothing else in this function recognized any specific product/format word - real-data
    # audit found many hair-routine "Combo"/"Kit"/"Solution"/"System"/"Ritual" names carry no
    # other signal at all (e.g. "Mamaearth Rosemary Hair Growth Combo", "CALECIM Professional
    # Advanced Hair System", "Wishcare Redensyl & Rosemary Anti Hairfall Kit" - "hairfall" is
    # one fused word, has_word('hair') alone doesn't match it). Mirrors the hair-only-brand
    # fallback above - Low confidence, always overridable, but never left fully blank when the
    # name itself already signals hair/scalp.
    if has_word(text, 'hair') or has_word(text, 'hairfall') or has_word(text, 'scalp'):
        return {'method':'CoOccurrence','matched_on':'bare-hair-word-fallback','L1':_hair_fallback_l1l2l3[0],'L2':_hair_fallback_l1l2l3[1],'L3':_hair_fallback_l1l2l3[2],'confidence':'Low'}

    # FIX: per explicit direction, the engine should never hand back a fully blank tag when it
    # has genuinely run out of ideas - a blank row can't be corrected in the review UI the same
    # way a wrong-but-present tag can. This is the true last resort (nothing above matched at
    # all, not even a brand or a loose keyword). Uses the UNCLASSIFIED placeholder (not the real
    # "Others" taxonomy category, which would be indistinguishable from a confident tag) at Low
    # confidence, guaranteeing it surfaces for manual review (see app.py's manual_review flag)
    # rather than silently vanishing as an untouched blank row. Method name stays 'Unmapped' so
    # the existing FALLBACK_METHODS/SKU_EXCEPTIONS safety nets in app.py and tag_sku() still
    # recognize and can override this case with a real prior tag when one exists.
    return {'method':'Unmapped','matched_on':None,'L1':UNCLASSIFIED,'L2':UNCLASSIFIED,'L3':UNCLASSIFIED,'confidence':'Low'}
