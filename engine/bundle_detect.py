"""
Bundle/combo detection layer - wraps tag_sku() with logic for multi-product SKUs.
Rebuilt after a container reset, reflecting the fully audited final state.
"""
import re
import pipeline_v6 as _p6
from pipeline_v6 import (normalize_compounds, has_word, DIRECT_KEYWORDS, CONTEXT_MATRIX,
                          SPECIFICITY_PHRASES, EXCLUDE_KEYWORDS, TIER0_HARDWARE,
                          FEMININE_HYGIENE_KEYWORDS, BABY_CARE_KEYWORDS, tag_sku, fix_encoding)

STRONG_BUNDLE_MARKERS = ['combo', 'gift set', 'gift box', ' kit', 'set of']
WEAK_BUNDLE_MARKERS = [' and ', ' + ', ' & ']  # '+' requires spacing so PA+++ ratings don't false-trigger

HAIR_L1S = {'Hair Care', 'Hair Care Others', 'Hair colour'}
BATH_BODY_L1S = {'Hygiene', 'Body Skin'}
FACE_L1S = {'Face Skin'}
MAKEUP_L1S = {'Make up'}

# L3s that mean "accessory/tool", not a second real product - when one of these co-occurs
# with a genuine product hit (e.g. "Body Wash Combo + Loofah"), it shouldn't block resolving
# to the real product; the accessory is a bonus item, same principle as the existing
# shampoo+comb guard in tag_sku().
ACCESSORY_L3S = {
    'Bath Accessories (Loofahs, Body Scrubbers etc.)', 'Cuticle Tools', 'Manicure Kits',
    'Wax Heaters', 'Paraffin Baths', 'Hair Accessories', 'Others',
}


def _strip_parens(text):
    """Removes parenthetical content before bundle-marker detection - shade/size/pack-count
    lists like "(Black & Brown)" or "(Beige 03) - Pack of 2" contain '&'/'and' that describe
    variants of ONE product, not a genuine multi-item bundle."""
    return re.sub(r'\([^)]*\)', ' ', text)


def _dedup_hits(hits):
    """Drops hits whose matched keyword is a plain substring of another hit's matched keyword
    in this same scan (e.g. bare 'cream' vs a more specific 'strobe cream'; bare 'wash' vs the
    'body wash' specificity phrase). These represent ONE product mention picked up twice by
    independent matching layers (DIRECT_KEYWORDS/SPECIFICITY_PHRASES vs CONTEXT_MATRIX), not
    two real products - the most common cause of false multi-category bundle detection."""
    bases = [h[0].split('+')[0] for h in hits]
    keep = []
    for i, h in enumerate(hits):
        kw_i = bases[i]
        if not any(j != i and kw_i != bases[j] and kw_i in bases[j] for j in range(len(hits))):
            keep.append(h)
    return keep

def find_all_hits(text):
    """Scan the FULL string for every keyword hit, recording its L2 (no stop-at-first-match)."""
    hits = []
    for kw, dest in TIER0_HARDWARE.items():
        if has_word(text, kw): hits.append((kw, dest[0], dest[1], dest[2]))
    for kw, l3 in FEMININE_HYGIENE_KEYWORDS.items():
        if has_word(text, kw): hits.append((kw, 'Others', 'Others Others', l3))
    for kw, l3 in BABY_CARE_KEYWORDS.items():
        if has_word(text, kw): hits.append((kw, 'Others', 'Others Others', l3))
    for phrase, l1, l2, l3 in SPECIFICITY_PHRASES:
        if has_word(text, phrase): hits.append((phrase, l1, l2, l3))
    for kw, dest in DIRECT_KEYWORDS.items():
        # FIX: "Attar Ayurveda" is a brand name containing the word "attar" - not an actual
        # attar/ittar fragrance product being sold. Mirrors the existing "Ghar Soaps" guard.
        if kw == 'attar' and has_word(text, 'attar ayurveda'):
            continue
        # FIX: "Eyelash & Eyebrow Growth Serum" is ONE lash/brow growth-serum product, not a
        # false-eyelashes product plus a separate serum. Mirrors the tag_sku() guard.
        if kw == 'eyelash' and has_word(text, 'growth serum'):
            continue
        # FIX: "Ubtan...Sheet Mask"/"Ubtan...Body Scrub" is ONE product (an ubtan-formulated
        # mask, scrub, or oil) - "Mask"/"Scrub"/"Oil" is the decisive product identity, ubtan is
        # the formulation/product-line name (e.g. "Rejuvenating UBTAN...Bath & Body Oil").
        # Mirrors the existing soap-wins-over-ubtan guard's reasoning.
        if kw == 'ubtan' and (has_word(text, 'mask') or has_word(text, 'scrub') or has_word(text, 'oil')):
            continue
        # FIX: "Hair Cleanser"/"Hair Oil" with Mulethi/Multani Mitti as an ingredient - the
        # already-named hair product wins over the generic Face Pack ingredient identity.
        if kw in ('mulethi', 'multani mitti') and (has_word(text, 'hair cleanser') or has_word(text, 'hair oil')):
            continue
        # FIX: "Blush & Glow" (Lakme's own product-line name) is a brand/line name containing
        # the word "blush" - not an actual Blush makeup product. Mirrors "Attar Ayurveda"/"Ghar
        # Soaps".
        if kw in ('blush', 'blusher') and (has_word(text, 'blush & glow') or has_word(text, 'blush and glow')):
            continue
        # FIX: "Rose Water...Face Wipes" - Rose Water is the wipes' ingredient/marketing
        # descriptor, not a separate Face Care product. Mirrors "oil is never primary".
        if kw == 'rose water' and (has_word(text, 'wipes') or has_word(text, 'wipe')):
            continue
        if has_word(text, kw): hits.append((kw, dest[0], dest[1], dest[2]))
    for base, rules in CONTEXT_MATRIX.items():
        if has_word(text, base):
            # FIX: "Shampoo Brush" is ONE tool (see the dedicated hit appended above) - skip
            # both bases' own generic hits so they don't compete with it as a second/third item.
            if base in ('brush', 'shampoo') and has_word(text, 'shampoo brush'):
                continue
            # Skip gel's own 'hair' modifier when hair-colour context is present - mirrors the
            # gel+hair-colour guard in tag_sku(). Without this, "Gel Hair Colour" combos get a
            # false 'Styling' hit alongside the correct 'Creme' hit, wrongly triggering Hair Combo.
            if base == 'gel' and (has_word(text,'hair colour') or has_word(text,'hair color')):
                continue
            # Skip powder's own bare default when wax context is present - handled by the
            # dedicated wax+powder guard below instead (avoids double-counting as 2 hits).
            if base == 'powder' and has_word(text,'wax'):
                continue
            # Skip powder's 'hair' modifier when henna/indigo/mehendi context is present -
            # those more specific DIRECT_KEYWORDS already cover it correctly.
            if base == 'powder' and (has_word(text,'henna') or has_word(text,'indigo') or has_word(text,'mehendi')):
                continue
            # Skip cream's own bare default when hair context is present - handled by the
            # dedicated cream+hair / cream+hair+colour guards below instead (avoids
            # double-counting as 2 hits). Excludes colour/color/dye - "Hair Color Cream" is a
            # permanent hair-dye product, routed by the cream+hair+colour guard below.
            if base == 'cream' and has_word(text,'hair') and not (
                    has_word(text,'wax') or has_word(text,'removal')
                    or has_word(text,'colour') or has_word(text,'color') or has_word(text,'dye')
                    or has_word(text,'colourant') or has_word(text,'colorant')):
                continue
            # Skip powder's bare default for nutraceutical/supplement powders (protein,
            # collagen, etc.) and for nail-art glitter/decoration powders - neither is a
            # cosmetic body/talc powder.
            if base == 'powder' and any(has_word(text,w) for w in (
                    'protein','collagen','whey','creatine','multivitamin','supplement',
                    'nutrition','glutathione','biotin tablet','carb','mass gainer','gainer',
                    'pre workout','bcaa','amino','nail','glitter')):
                continue
            # Skip powder's 'face' modifier when 'face pack' is already present - "Face Pack
            # Powder" is a powder-FORMAT face pack (one product), not a face pack plus a
            # separate face-powder makeup product.
            if base == 'powder' and has_word(text,'face pack'):
                continue
            # FIX: "powder is never primary" (same principle as "oil is never primary" below) -
            # when a specific named ingredient that already has its own established product
            # identity (Shikakai->Shampoo, Mulethi/Multani Mitti/Rose Water->Face Pack/Face
            # care) or a decisive product-format word (a Mask, or a Shampoo/Dry Shampoo/Face
            # Wash/Cleanser it's describing the format of) is also present, bare "Powder" is
            # describing THAT product's texture/format, not a separate Talcum Powder product.
            if base == 'powder' and any(has_word(text,w) for w in (
                    'shikakai','mulethi','multani mitti','rose water','mask',
                    'dry shampoo','shampoo','face wash','cleanser')):
                continue
            # FIX: "oil is never primary" - when a genuine standalone product-format word is
            # also present (shampoo/conditioner/serum/mask/spray/wash/lotion/soap/eyeliner/
            # kajal/mascara/gel), "Oil" is describing an ingredient of THAT product (e.g. "Argan
            # Oil Shampoo", "Kajal with Almond Oil", "Shower Gel - Lemon & Oil"), not a second
            # Hair/Body Oil product. 'gel' added per real-data audit - "Lemon & Oil" in a
            # shower-gel name was false-flagging as a 2-category Mixed Sets bundle.
            if base == 'oil' and any(has_word(text, w) for w in (
                    'shampoo','conditioner','serum','mask','spray','wash','lotion','soap','gel',
                    'eyeliner','eye liner','kajal','mascara','wipes','wipe')):
                continue
            # FIX: "Oil-Control"/"Oil Control" is a FUNCTIONAL claim (the product controls
            # excess oil) describing another product (e.g. a Cleanser), not a mention of an
            # oil product itself - narrower than the general exclusion list above so it doesn't
            # also suppress genuine "Cleansing Oil" SKUs (which use 'oil'+'cleansing' to
            # correctly resolve to Makeup Cleansing Oils via its own dedicated modifier).
            if base == 'oil' and (has_word(text,'oil control') or has_word(text,'oil-control')):
                continue
            # FIX: "Primer" always wins over bare moisturizer/cream (mirrors the tag_sku()
            # guard) and always loses to a more finished product like Foundation or Nail
            # Polish (matches real-data evidence: "Primer + Foundation" -> Foundation,
            # "Primer + Nail Polish" -> Nail Polish).
            if base == 'primer' and (has_word(text,'moisturiser') or has_word(text,'moisturizer') or has_word(text,'cream') or has_word(text,'creme')):
                continue
            if base == 'primer' and (has_word(text,'foundation') or has_word(text,'nail polish') or has_word(text,'nail paint') or has_word(text,'lipstick')):
                continue
            # FIX: bare 'cream'/'creme' also loses to Primer specifically (the tag_sku() guard
            # covers the single-item path; this covers the same case within bundle hit-scanning).
            if base in ('cream','creme') and has_word(text,'primer'):
                continue
            # FIX: "Ghar Soaps" is a brand name containing the word "soaps" - not a second
            # soap product alongside whatever the SKU actually is (e.g. a face wash).
            if base == 'soap' and has_word(text,'ghar soaps'):
                continue
            # FIX: an electric "Trimmer" that also functions as a razor is one hybrid device,
            # not a trimmer plus a separate manual razor.
            if base == 'razor' and has_word(text,'trimmer'):
                continue
            # FIX: "Roll On Serum" is overwhelmingly a Hair Growth Serum in roll-on format (see
            # the tag_sku() guard for the real-data breakdown), not an "Underarm Roll On Serum"
            # deodorant - a hair-context word registers it as a genuine Hair Serums hit instead
            # of silently dropping it (which was letting the bare 'roll' hit's own Deos default
            # win uncontested in hair-serum bundles like "Roll On Serum + Shampoo + Conditioner").
            if base == 'serum' and (has_word(text,'roll on') or has_word(text,'roll-on') or has_word(text,'rollon')):
                if any(has_word(text,w) for w in ('hair','scalp','growth','hairfall','hair fall')):
                    hits.append(('serum+rollon+hair','Hair Care','Hair Serums','Hair Serums'))
                continue
            # FIX: "Roll On Serum" is ONE product in a roll-on applicator format, not a Serum
            # plus a separate "Roll On" product - the bare 'roll' base would otherwise also
            # independently match and contribute its own (now non-blank, see 'roll' catchall
            # above) Deos & Antiperspirants guess as a second, spurious hit. The dedicated
            # serum+rollon guard above already decides the real category for this mention.
            if base == 'roll' and has_word(text,'serum'):
                continue
            # FIX: "Gel Face/Hand/Body Wash" - gel is a texture descriptor of the wash, not a
            # second product (mirrors the tag_sku() guards).
            if base == 'gel' and (has_word(text,'face wash') or has_word(text,'hand wash') or has_word(text,'body wash')):
                continue
            # FIX: "Creme/Cream Face Wash" - same principle, cream/creme format descriptor.
            if base in ('cream','creme') and has_word(text,'face wash'):
                continue
            # FIX: "Cream Conditioner" is a specific hair-conditioner FORMAT (a thicker, cream-
            # textured rinse-off conditioner) - not a separate face-cream product.
            if base in ('cream','creme') and has_word(text,'cream conditioner'):
                continue
            # FIX: "Gel Toothpaste" - gel is the toothpaste's own format/texture, not a
            # separate skincare-gel product.
            if base == 'gel' and has_word(text,'toothpaste'):
                continue
            # FIX: "with Skin Conditioners" is a cosmetic-ingredient/INCI term for a
            # moisturizing agent in a body wash / shower gel, not a rinse-off HAIR conditioner
            # product - only the bare (no more specific modifier already matched) 'conditioner'
            # hit is affected, since a genuine "Leave-In Conditioner" etc. already resolved via
            # its own modifier before reaching here.
            if base == 'conditioner' and has_word(text,'skin conditioner'):
                continue
            # FIX: "Face Serum & Cleanser" family (mirrors the tag_sku() guards) - Wash wins
            # over Serum; bare Cleanser loses to Serum when Wash isn't also present.
            if base == 'serum' and has_word(text,'wash'):
                continue
            if base == 'cleanser' and has_word(text,'serum') and not has_word(text,'wash'):
                continue
            # FIX: "Strobe Cream ... Moisturizer" - Strobe Cream is a specific, self-contained
            # product identity; a bare "moisturizer" mention alongside it is a functional
            # descriptor of that SAME product, not a second one.
            if base in ('moisturiser','moisturizer') and has_word(text,'strobe cream'):
                continue
            # FIX: "Matte Powder Foundation" is a specific makeup foundation FORMAT, not a
            # talcum-powder product.
            if base == 'powder' and has_word(text,'foundation'):
                continue
            for mod, l1, l2, l3, conf in rules:
                if mod is None:
                    if l1: hits.append((base, l1, l2, l3))
                    break
                if has_word(text, mod):
                    if l1: hits.append((f'{base}+{mod}', l1, l2, l3))
                    break
    # 'mask' lives in a dedicated function (not CONTEXT_MATRIX) - add it back for bundle-hit scanning
    is_mask_tool = has_word(text,'brush') or has_word(text,'roller') or has_word(text,'gua sha') or has_word(text,'applicator') or has_word(text,'spatula') or has_word(text,'foundation') or has_word(text,'cushion')
    if has_word(text,'mask') and not is_mask_tool:
        l1, l2, l3, conf = _p6.resolve_mask(text)
        if l1: hits.append(('mask', l1, l2, l3))
    # 'cream'+'hair' also lives outside CONTEXT_MATRIX as a dedicated guard - expose it here too.
    # Excludes colour/color/dye - "Hair Color Cream" falls through to the bare hair-colour
    # fallback below instead, which correctly routes it to Hair colour/Creme.
    if has_word(text,'cream') and has_word(text,'hair') and not (
            has_word(text,'wax') or has_word(text,'removal')
            or has_word(text,'colour') or has_word(text,'color') or has_word(text,'dye')
            or has_word(text,'colourant') or has_word(text,'colorant')) \
       and not any(h[1]=='Hair Care Others' and 'styling' in h[0] for h in hits):
        hits.append(('cream+hair','Hair Care Others','Styling','Hair Lotions & Tonics'))
    # 'wax'+'powder' also lives outside CONTEXT_MATRIX as a dedicated guard - expose it here too
    if has_word(text,'wax') and has_word(text,'powder'):
        hits.append(('wax+powder','Hygiene','Shaving','Depilatory'))
    # bare "hair colour/color/dye" fallback also lives outside CONTEXT_MATRIX - expose it too,
    # but only if nothing more specific (shampoo/remover/creme modifiers) already covered it
    if (has_word(text,'hair colour') or has_word(text,'hair color') or has_word(text,'hair dye')) and not any(h[1]=='Hair colour' for h in hits):
        if has_word(text,'highlight') or has_word(text,'highlighting'):
            hits.append(('hair colour+highlight','Hair colour','Hair colour Others','Highlight Kits'))
        elif has_word(text,'stick') or has_word(text,'touch up') or has_word(text,'touchup'):
            hits.append(('hair colour+stick','Hair colour','Hair colour Others','Hair Mascaras & Root Touch Ups'))
        else:
            hits.append(('hair colour (fallback)','Hair colour','Creme','Crème'))
    # FIX: Soap always wins over Fragrance (mirrors the tag_sku() soap+fragrance-guard) -
    # "Cologne Soap"/"Perfumed Soap" is a soap product, not a fragrance one.
    if (has_word(text,'soap') or has_word(text,'bathing bar')) and any(h[1] == 'Fragrance' for h in hits):
        hits = [h for h in hits if h[1] != 'Fragrance']
    # FIX: Soap also wins over generic ingredient-descriptor hits (ubtan, multani mitti, rose
    # water) and a bare cream/creme/gel/serum/scrub hit - "Ubtan Soap"/"Rose Water Soap"/"Cream
    # Soap"/"Gel Soap"/"Serum Soap Bar"/"Scrub Soap" are each ONE soap product with a
    # marketing/ingredient/format descriptor, not a soap plus a separate face-pack/toner/
    # cream/gel/serum/scrub product. Without this, "Set Of N <descriptor> Soap" SKUs were
    # false-flagged as multi-category bundles.
    if has_word(text,'soap') or has_word(text,'bathing bar'):
        hits = [h for h in hits if not (
            h[0] in ('ubtan','ubtan powder','multani mitti','multani powder','rose water')
            or h[0].split('+')[0] in ('cream','creme','gel','serum','scrub'))]
    # FIX: "Shampoo + Serum"/"Shampoo, Conditioner, Serum" hair-care combos (Tresemme, Plix,
    # TNW, Wella, Nexxus, etc.) were falling to the generic Mixed Sets/Bundle-ManualReview
    # bucket instead of resolving as a genuine Hair Combo - a bare 'serum' hit (no explicit
    # face/facial/spf/tint modifier) defaults to the generic Face Serums/Essence L1 on its own,
    # which then put Face Skin alongside Hair Care in the bundle's distinct-L1 set, disqualifying
    # it from the Bundle-HairCombo branch below. When any OTHER hit in the same SKU is a genuine
    # hair-care product (shampoo, conditioner, hair oil, hair mask, hair colour - anything
    # already resolved to a Hair Care/Hair Care Others/Hair colour L1), a co-occurring bare
    # serum mention is overwhelmingly the SAME routine's hair serum, not an unrelated face
    # product, so it's reclassified to Hair Care/Hair Serums here.
    # Same reasoning for a bare 'mask' hit that only resolved via resolve_mask()'s generic,
    # no-signal-at-all "Face Sheet Masks" default (e.g. "Shampoo & Mask With ... Serum") - the
    # more decisive mask outcomes (Under Eye Masks, lip Balms, Hand Creams, an explicit "hair"
    # mention, a known sheet-mask/hair-only brand) are untouched, only the generic bare default.
    # Same reasoning again for a bare 'wash'/'cleanser' hit that only resolved via the generic
    # Face Wash/Face Cleansers default (e.g. "Curl Co Wash Cleanser & Conditioner" - a curly-
    # hair "co-wash" routine step, not a face product; neither says "face" literally, since an
    # explicit "Face Wash" mention already resolves via that base's own 'face' modifier before
    # ever reaching this generic default).
    if any(h[1] in HAIR_L1S for h in hits):
        def _reclassify(h):
            if h[0] == 'serum':
                return (h[0], 'Hair Care', 'Hair Serums', 'Hair Serums')
            if h[0] == 'mask' and h[1] == 'Face Skin' and h[2] == 'Face care' and h[3] == 'Face Sheet Masks':
                return (h[0], 'Hair Care', 'Rinse-Off Masks', 'Rinse Off Masks')
            if h[0] in ('wash', 'cleanser') and h[1] == 'Face Skin' and h[2] == 'Face cleansers' and h[3] == 'Face Wash/Face Cleansers':
                return (h[0], 'Hair Care', 'Shampoo', 'All kinds of Shampoos')
            return h
        hits = [_reclassify(h) for h in hits]
    # FIX: "Hair Color Cream"/"Permanent Hair Color Cream" (Streax, Revlon, Argatin) - a bare
    # 'cream' hit (the generic Face Creams default) alongside the dedicated "hair colour
    # (fallback)" hit is describing that SAME hair-colour product's cream format, not a second
    # face-cream product - dropped rather than reclassified since the hair colour hit already
    # covers the category.
    if any(h[1] == 'Hair colour' for h in hits):
        hits = [h for h in hits if not (
            h[0] == 'cream' and h[1] == 'Face Skin' and h[2] == 'Face care' and h[3] == 'Face Creams')]
    # FIX: a real 'oil' hit (hair, body, or facial oil - any L1) suppresses a bare, unmodified
    # 'moisturiser'/'moisturizer' hit from a DIFFERENT base - real-data audit found several
    # multi-use oil products (Nat Habit, Tattvalogy, Zenvista, Soulflower) marketed as "Oil ...
    # Moisturizer" where "Moisturizer" is describing what the OIL does, not a second product.
    # Mirrors the existing Make-up-suppresses-generic-cream/moisturizer guard below. Scoped to
    # the bare (unmodified) moisturiser/moisturizer hit only - an explicit "Tinted Moisturiser"/
    # "Leave-In Moisturiser" already resolved via its own decisive modifier is left untouched.
    if any(h[0].split('+')[0] == 'oil' for h in hits):
        hits = [h for h in hits if not (
            h[0] in ('moisturiser', 'moisturizer') and h[1] == 'Face Skin' and h[3] == 'Moisturisers')]
    # FIX: any Make up hit (Foundation, Concealer, Highlighter, Blush, BB/CC/DD Cream, etc.)
    # suppresses generic bare cream/creme/moisturizer/gel hits from a DIFFERENT base - these
    # are almost always describing an attribute of the SAME makeup product ("Foundation +
    # Moisturizer" = one moisturizing foundation), not a second skincare product. Mirrors the
    # existing Primer-specific and Strobe-Cream-specific guards, generalized to all makeup.
    if any(h[1] == 'Make up' for h in hits):
        hits = [h for h in hits if h[1] == 'Make up' or h[0].split('+')[0] not in
                ('cream','creme','moisturiser','moisturizer','gel','powder')]
    # FIX: "Lip Balm ... Tinted Lip Moisturizer" - a moisturiser/moisturizer hit co-occurring
    # with a 'lip balm' hit is the SAME product described two ways, not a separate face-makeup
    # "Tinted Moisturiser" - unlike the filter above, this applies even when the moisturizer
    # hit's own modifier (e.g. 'tinted') already resolved it to a Make-up L1.
    if any(h[0] == 'lip balm' or h[0].startswith('lip balm+') for h in hits):
        hits = [h for h in hits if h[0].split('+')[0] not in ('moisturiser', 'moisturizer')]
    # FIX: a makeup TOOL (brush, sponge, applicator, puff) is very often described by a list of
    # what it's used FOR ("Powder Brush... for Loose and Pressed Powders, Blush, Bronzer and
    # Contour", "Makeup Sponge for Liquid, Cream, and Powder") - those are use-case descriptors
    # of the ONE tool, not separate products bundled together. Drop them when a tool hit is
    # present, same reasoning as the Make-up-suppresses-generic-hits filter above.
    # 'sharpener' included so a "Dual Kajal & Lipstick Sharpener" (one sharpener tool,
    # compatible with both pencil types) doesn't false-flag as a kajal+lipstick bundle.
    TOOL_BASES = ('brush', 'sponge', 'applicator', 'puff', 'sharpener')
    FORMAT_DESCRIPTOR_BASES = (
        'blush', 'blusher', 'bronzer', 'contour', 'highlighter', 'compact powder', 'foundation',
        'concealer', 'eyeshadow', 'powder', 'eyeliner', 'eye liner', 'lipstick', 'kajal',
    )
    if any(h[0].split('+')[0] in TOOL_BASES for h in hits):
        hits = [h for h in hits if h[0].split('+')[0] in TOOL_BASES or
                h[0].split('+')[0] not in FORMAT_DESCRIPTOR_BASES]
    # FIX: "Face Palette with Blushes, Highlighters and Bronzer" is ONE compact product bundling
    # several shades/finishes into a single item - a "Palette" by definition, not a multi-item
    # bundle. Drop the individual color-format hits so it resolves as a single palette product
    # instead of a false Mixed-Sets flag.
    if has_word(text, 'palette'):
        hits = [h for h in hits if h[0].split('+')[0] not in FORMAT_DESCRIPTOR_BASES]
    # FIX: "Blush" is both a real product category AND a common shade/color name for OTHER
    # products ("Matte Lipstick - Blush Nude", "Gel Blush Pink Nail Polish"). When a different,
    # more decisive finished-product hit is also present, the bare 'blush' hit is almost always
    # just describing that product's shade, not a second Blush product - drop it so a single
    # lipstick/foundation/etc. doesn't get false-flagged as a multi-category bundle.
    # 'tint' included so "Lip & Cheek Tint (Blush Nude)"/"...(Mascara Tear)" - where Blush or
    # Mascara is a SHADE NAME, not a second bundled product - resolves as the one Tint product.
    PRIMARY_PRODUCT_BASES = (
        'lipstick', 'foundation', 'nail polish', 'eyeshadow', 'concealer', 'kajal',
        'mascara', 'lip balm', 'eyeliner', 'eye liner', 'tint',
    )
    if any(h[0].split('+')[0] in PRIMARY_PRODUCT_BASES for h in hits):
        hits = [h for h in hits if h[0].split('+')[0] not in ('blush', 'blusher')]
    # FIX: "...Lip & Cheek Tint (Mascara Tear)" - same shade-name reasoning as the blush filter
    # above, but for Mascara specifically - scoped narrowly to only fire alongside a 'tint' hit
    # (a genuine Tint + real Mascara 2-item set is rare and would need its own strong marker
    # anyway; this only strips the bare mascara hit when tint is also present).
    if any(h[0].split('+')[0] == 'tint' for h in hits):
        hits = [h for h in hits if h[0].split('+')[0] != 'mascara']
    # FIX: "Mascara with Brush" / "Eyeliner with Applicator" - a finished single-use eye/lip
    # product's own included applicator mentioned by name isn't a second bundled brush-set
    # product. Mirrors the TOOL_BASES-suppresses-FORMAT_DESCRIPTOR_BASES filter above, in the
    # opposite direction: here the PRIMARY finished product wins and the bare tool hit is
    # dropped, rather than the tool winning over format descriptors of what it's used for.
    if any(h[0].split('+')[0] in PRIMARY_PRODUCT_BASES for h in hits):
        hits = [h for h in hits if h[0].split('+')[0] not in TOOL_BASES]
    return hits

def _check_exceptions(text):
    """Same last-resort/low-confidence safety net tag_sku() applies, exposed here so the
    bundle-specific 'give up' paths (Bundle-ManualReview) get it too - a bundle-marked SKU
    that stumps the engine is just as much a "rule engine broke" case as a plain Unmapped."""
    if text in _p6.SKU_EXCEPTIONS:
        entry = _p6.SKU_EXCEPTIONS[text]
        return {'method':'SKU-Exception','L1':entry.get('L1'),'L2':entry.get('L2'),'L3':entry.get('L3'),'confidence':'High'}
    return None

def tag_sku_bundle_aware(sku_name):
    raw_text = sku_name.lower()  # markers checked here, BEFORE +/- get normalized away
    text = normalize_compounds(raw_text)  # keyword hits checked here

    for kw in EXCLUDE_KEYWORDS:
        if has_word(text, kw):
            return {'method':'Excluded-NA','L1':None,'L2':None,'L3':None,'confidence':'NA'}

    # known single-product "kit" formats - resolve directly, don't treat as bundle
    if has_word(text, 'facial kit'):
        return {'method':'Direct-Keyword','L1':'Face Skin','L2':'Face care','L3':'Facial Kit','confidence':'High'}
    if has_word(text, 'shaving kit'):
        return {'method':'Direct-Keyword','L1':'Hygiene','L2':'Shaving','L3':'Shaving Kit','confidence':'High'}
    if any(p in text for p in ['3-in-1','3 in 1']) and has_word(text,'shampoo'):
        return {'method':'Direct-Keyword','L1':'Hair Care','L2':'Shampoo','L3':'3-In-1 Shampoo, Conditioner & Body Wash','confidence':'High'}
    if any(p in text for p in ['2-in-1','2 in 1']) and has_word(text,'shampoo'):
        return {'method':'Direct-Keyword','L1':'Hair Care','L2':'Shampoo','L3':'2-In-1 Shampoo & Conditioner','confidence':'High'}

    # FIX: any "Baby ... Wash/Lotion/Cream/Shampoo/Oil/Powder/Bath" product is fundamentally a
    # baby-care product, whether or not the exact BABY_CARE_KEYWORDS phrase is adjacent (e.g.
    # "Chicco Baby Moments Mild Bodywash" doesn't say "baby wash" literally) - checked
    # unconditionally (not just for bundle-marked text) and before bundle logic, since a
    # baby-branded product combo (e.g. baby wash + baby lotion) should stay Baby Care rather
    # than falling into an unrelated bath/body combo resolution.
    if has_word(text,'baby') and any(has_word(text,w) for w in
            ('wash','lotion','cream','shampoo','oil','powder','bath','soap','wipes')):
        return {'method':'Baby-Care','matched_on':'baby+product-word','L1':'Others','L2':'Others Others','L3':'Baby Care','confidence':'High'}

    # FIX: "Face & Body Wash" (a single dual-use product) - defaults to Body wash per real-data
    # evidence, matching the existing face+body+scrub/oil guards in tag_sku().
    if has_word(text,'face') and has_word(text,'body') and has_word(text,'wash'):
        return {'method':'Direct-Keyword','matched_on':'face+body+wash','L1':'Hygiene','L2':'Body wash','L3':'Body washes (generic/cream-based)','confidence':'High'}

    # FIX: "Face & Body Gel Scrub"/"Face & Body Oil" (a single dual-use product) - mirrors the
    # face+body+wash guard above and tag_sku()'s own face+body+scrub/oil guards, which never
    # got a chance to run here since a bundle-marked "&" SKU short-circuits before reaching
    # tag_sku() at all.
    face_and_body = has_word(text,'face') and has_word(text,'body')
    if face_and_body and has_word(text,'scrub'):
        return {'method':'CoOccurrence','matched_on':'face+body+scrub','L1':'Hygiene','L2':'Body scrub','L3':'Body scrub- cremes and powders','confidence':'Medium'}
    if face_and_body and has_word(text,'oil'):
        return {'method':'CoOccurrence','matched_on':'face+body+oil','L1':'Body Skin','L2':'Body care','L3':'Body Oils','confidence':'Medium'}

    # FIX: "Perfume Set" bundling a spray + a fragrance oil (e.g. "Love Drunk Perfume Set | 75ml
    # Spray & 8ml Fragrance Oil") is a genuine Fragrance/Perfume combo, not a Deodorant - a bare
    # 'spray' hit's own Deos & Antiperspirants default would otherwise win, since "Set" alone
    # (unlike "Gift Set"/"Set Of") isn't a STRONG_BUNDLE_MARKER and 'perfume'/'fragrance' live in
    # LOW_PRIORITY_KEYWORDS, which find_all_hits() doesn't scan at all.
    # FIX: real-data audit found "perfume"/"fragrance" is very often just a MARKETING ADJECTIVE
    # describing a DIFFERENT product's scent - "Perfume Body Spray"/"Fragrance Body Spray"
    # (Beardo, Cristiano Ronaldo CR7, Fogg) is a deodorant, "Perfume Hair Spray" (mCaffeine,
    # Hairmac) is a haircare product, "...Body Mist Spray" (Ajmal) is a body mist - none are a
    # genuine perfume SET. Requires an explicit multi-item bundling word ("set"/"combo" - bare
    # "gift" alone was too weak, real single-bottle perfumes are routinely marketed "...Gift For
    # Women/Men" with no second item at all, and bare "collection" alone is often just a
    # product-LINE name, e.g. "Park Avenue Pure Collection Trance Perfume Spray" - one bottle)
    # so only a real multi-item perfume set is caught, and excludes 'body'/'hair'/'mist'/
    # deodorant-context words so those adjective-only mentions defer to their own resolution.
    if (has_word(text,'perfume') or has_word(text,'fragrance')) and has_word(text,'spray') \
       and any(has_word(text,w) for w in ('set','combo')) \
       and not any(has_word(text,w) for w in (
               'deo','deodorant','underarm','under arm','antiperspirant','body','hair','mist')):
        return {'method':'CoOccurrence','matched_on':'perfume+spray','L1':'Fragrance','L2':'Perfume','L3':'Perfume Combo/Sets/Samples','confidence':'High'}

    marker_text = _strip_parens(raw_text)
    has_strong = any(m in marker_text for m in STRONG_BUNDLE_MARKERS)
    has_weak = any(m in marker_text for m in WEAK_BUNDLE_MARKERS)

    if has_strong or has_weak:
        hits = _dedup_hits(find_all_hits(text))
        non_accessory = [h for h in hits if h[3] not in ACCESSORY_L3S]
        hits = non_accessory if non_accessory else hits
        distinct_l2 = set(h[2] for h in hits)

        if len(distinct_l2) == 1:
            target_l2 = list(distinct_l2)[0]
            l1, l2, l3 = next((h[1],h[2],h[3]) for h in hits if h[2]==target_l2)

            # Fragrance gift-sets/combos use a dedicated legacy L3 regardless of how many
            # keyword hits were found - "Gift Set" is a recognized retail format even when the
            # text only reads as one product mention. Per explicit direction: if the text says
            # EDP/Eau De Parfum (or EDT/Eau De Toilette, etc.), it's that specific concentration,
            # not generic Perfume - preserve whichever L2 was actually detected. Each L2 has its
            # own combo-L3 naming (confirmed against real data): "Perfume Combo/Sets/Samples"
            # under Perfume, but "Eau De Parfum Combo/Samples/Sets" under Eau De Parfum, etc. -
            # different word order per L2, not interchangeable.
            if l1 == 'Fragrance' and has_strong:
                if l2 == 'Perfume':
                    combo_l3 = 'Perfume Combo/Sets/Samples'
                else:
                    combo_l3 = f'{l2} Combo/Samples/Sets'
                return {'method':'Bundle-Fragrance-ComboSet','L1':'Fragrance','L2':l2,'L3':combo_l3,'confidence':'High'}

            if has_strong and len(hits) >= 2:
                # genuine multi-item bundle (2+ distinct product mentions), all landing in the
                # same L2 - label as a Set rather than silently collapsing to one item's L3
                return {'method':'Bundle-SameL2-Set','L1':l1,'L2':l2,'L3':'Sets','confidence':'High'}

            # Only one product-category was detected - this almost always means the
            # bundle-flavored wording (&, combo, gift set) describes a SINGLE product, not a
            # real multi-item bundle (e.g. "Aloe Vera Gel with Vitamin E for Skin & Hair").
            # Defer to the fully-guarded single-item engine, which carries co-occurrence
            # guards this simplified keyword scan doesn't replicate - fall back to the raw
            # keyword hit only if tag_sku() itself can't resolve anything.
            single = tag_sku(sku_name)
            if single.get('L1'):
                return {**single, 'method': f"Bundle-SameL2-Resolved+{single['method']}"}
            return {'method':'Bundle-SameL2-Resolved','L1':l1,'L2':l2,'L3':l3,'confidence':'Medium'}

        if len(distinct_l2) >= 2:
            distinct_l1 = set(h[1] for h in hits)
            l2_l3_map = {h[2]: h[3] for h in hits}

            # FIX: Sunscreen wins whenever it's paired with exactly one other category
            # (Lip Balm+Sunscreen, Face Wash+Sunscreen, Moisturizer+Sunscreen, etc.) -
            # "Sunscreen" mentioned explicitly is the standout feature per explicit direction;
            # this is checked before the generic family logic below since e.g. {Make up, Face
            # Skin} doesn't cleanly fit any single L1 family. Restricted to exactly 2 distinct
            # categories - a genuine 3+-way combo (e.g. the "Deconstruct" combos) still falls
            # through to the generic Sets/Mixed-Sets logic further down, matching real-data
            # evidence that those ground-truth to "Sets" rather than plain Sunscreen.
            if len(distinct_l2) == 2 and 'Sun care' in distinct_l2:
                sun_hit = next(h for h in hits if h[2] == 'Sun care')
                return {'method':'Bundle-SunscreenWins','L1':sun_hit[1],'L2':sun_hit[2],'L3':sun_hit[3],'confidence':'High'}

            # FIX: Perfume/EDP + Deodorant/body-spray gift combos - unlike a pure fragrance
            # gift set (which preserves the specific EDP/EDT concentration), a Fragrance+
            # Deodorant combo specifically ground-truths to the generic Perfume L2 regardless
            # of which concentration is named.
            if has_strong and 'Fragrance' in distinct_l1 and all(
                    h[1] == 'Fragrance' or h[2] == 'Deos & Antiperspirants' for h in hits):
                return {'method':'Bundle-Fragrance-ComboSet','L1':'Fragrance','L2':'Perfume','L3':'Perfume Combo/Sets/Samples','confidence':'High'}

            if distinct_l1 <= HAIR_L1S:
                # Relaxed to weak markers too, same reasoning as BATH_BODY_L1S below: checked
                # against all 58 real "Duo"-named products in the April data - single-product
                # "Duo" branding (Duo Lip Color, Duo Kajal, Duo Compact) only ever produces ONE
                # hit and is unaffected by this; only genuine 2-different-hair-product mentions
                # (Shampoo & Conditioner, Styling Cream & Gel) reach this branch, and every one
                # checked ground-truths to a real Hair Combo. The 'oil'-is-never-primary skip
                # upstream already filters out the ingredient-list false-positive risk
                # ("Argan Oil and Lavender Shampoo").
                l3_name = 'Hair Care Sets' if ('set' in raw_text) else 'Hair Combos'
                return {'method':'Bundle-HairCombo','L1':'Hair Care','L2':'Hair Combo','L3':l3_name,'confidence':'High'}
            if has_strong and distinct_l1 <= FACE_L1S:
                return {'method':'Bundle-FaceCombo','L1':'Face Skin','L2':'Face Skin Others','L3':'Sets','confidence':'Medium'}
            if distinct_l1 <= BATH_BODY_L1S:
                # Relaxed to weak markers too (not just has_strong/"combo"/"kit") - the risky
                # ingredient-vs-product ambiguity ("Lavender + Almond Oil Body Wash") is already
                # filtered out upstream by the 'oil'-is-never-primary skip in find_all_hits(),
                # so what reaches here is genuinely 2+ distinct bath/body product mentions.
                return {'method':'Bundle-BathKit','L1':'Hygiene','L2':'Body wash','L3':'Bath Sets & Kits','confidence':'Medium'}
            if has_strong and distinct_l1 <= MAKEUP_L1S:
                # Genuine cross-category makeup kit (e.g. "Lipstick + Eyeliner Kit") - distinct
                # from a same-product multi-shade set (e.g. "Lipstick Set of 3 shades"), which
                # never reaches here since it only ever produces ONE distinct L2 (Lipsticks).
                return {'method':'Bundle-MakeupKit','L1':'Make up','L2':'Make up Others','L3':'Make-Up Kits','confidence':'Medium'}
            if has_strong and len(hits) >= 3:
                # A clearly-labeled multi-item combo/kit (3+ distinct product mentions) that
                # doesn't fit a single known L1 family (e.g. skincare + a lip product +
                # sunscreen, all bundled) - real-data evidence consistently uses the generic
                # Face Skin Others/Sets bucket for these regardless of the exact category mix.
                return {'method':'Bundle-MixedSet','L1':'Face Skin','L2':'Face Skin Others','L3':'Sets','confidence':'Medium'}
            exception = _check_exceptions(text)
            if exception:
                return exception
            # Genuine multi-category bundle (2+ distinct, genuinely different real products,
            # e.g. "Shampoo + Face Wash") that doesn't fit any known specific combo pattern -
            # which exact category is "primary" is a real judgment call, so rather than leaving
            # it blank, tag it with the taxonomy's own existing catch-all for this exact
            # situation (Others/Others Others/Mixed Sets - previously defined but unused).
            # Method name kept as-is (still flags Manual_Review_Required=Yes for visibility;
            # see app.py) since this is a generic answer, not a fully specific one.
            return {'method':'Bundle-ManualReview-MultiCategory','L1':'Others','L2':'Others Others','L3':'Mixed Sets','confidence':'Medium'}

        # No recognizable product-category hit at all - not real evidence of an actual
        # bundle, just incidental bundle-flavored wording (e.g. an ingredient list joined by
        # "&"). Let the fully-guarded single-item engine take a shot before giving up -
        # only flag for manual review if it also finds nothing.
        single = tag_sku(sku_name)
        if single.get('L1'):
            return single
        if has_strong:
            exception = _check_exceptions(text)
            if exception:
                return exception
            # Total miss, not a real multi-category conflict - same "engine is stuck" bucket
            # as a plain Unmapped, just reached via bundle-marked text. Distinct method name
            # (vs. the multi-category case above) so callers can safely fall back to an
            # existing/prior tag here without also silently resolving real category conflicts.
            # FIX: per direction, never hand back a fully blank tag - same UNCLASSIFIED
            # placeholder (not the real "Others" taxonomy category) as _tag_sku_core's own final
            # Unmapped fallback, so this always surfaces for manual review (see app.py's
            # manual_review flag) as an editable row instead of a blank one, while still
            # yielding to a real prior tag via FALLBACK_METHODS first.
            return {'method':'Bundle-ManualReview-NoSignal','L1':_p6.UNCLASSIFIED,'L2':_p6.UNCLASSIFIED,'L3':_p6.UNCLASSIFIED,'confidence':'Low'}

    return tag_sku(sku_name)
