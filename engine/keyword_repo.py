"""
Tier 0 hardware overrides, Feminine Hygiene and Baby Care keyword dictionaries.
Rebuilt from the Master Glossary after a container reset - reflects the fully audited
final state of the L'Oreal BPC SKU tagging engine.
"""

TIER0_HARDWARE = {
    'trimmer': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'hair dryer': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'blow dryer': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'straightener': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'hair curler': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'curling iron': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    # FIX: real-data audit found 42+ "Curling Tong"/"Curling Wand"/"Hair Crimper" products
    # (Gorgio Professional, Ikonic Professional, Alan Truman, VEGA) falling to Unmapped - the
    # exact same electric styling-tool category as 'hair curler'/'curling iron' above, just
    # phrased differently.
    'curling tong': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'curling wand': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'crimper': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'hair clipper': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'hair styler': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    # FIX: "Straightner"/"Curle" are common misspellings (missing the 'e'/'r') seen across
    # several real brands (Syska, WAHL) - neither matches 'straightener'/'curler'.
    'straightner': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'beauty steamer': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'facial steamer': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'epilator': ('Others', 'Others Others', 'All electric Appliances - Hair Dryers, Straighteners, Electric Razors'),
    'nail clipper': ('Others', 'Others Others', 'Cuticle Tools'),
    'manicure kit': ('Others', 'Others Others', 'Manicure Kits'),
    'wax heater': ('Others', 'Others Others', 'Wax Heaters'),
    'paraffin bath': ('Others', 'Others Others', 'Paraffin Baths'),
}

FEMININE_HYGIENE_KEYWORDS = {
    'sanitary pad': 'Sanitary Pads', 'sanitary napkin': 'Sanitary Pads',
    'tampon': 'Tampons', 'menstrual cup': 'Menstrual Cup',
    'period panty': 'Period Panties', 'panty liner': 'Panty Liners',
    'intimate wash': 'Intimate Wash', 'intimate wipe': 'Intimate Wipes',
    'pain relief patch': 'Period Pain Relief', 'period pain': 'Period Pain Relief',
}

BABY_CARE_KEYWORDS = {
    'baby lotion': 'Baby Care', 'baby wash': 'Baby Care', 'baby shampoo': 'Baby Care',
    'baby oil': 'Baby Care', 'diaper': 'Baby Care', 'newborn': 'Baby Care',
    'baby powder': 'Baby Care', 'baby cream': 'Baby Care',
}

print("Keyword repository rebuilt.")
print(f"Tier 0 (hardware): {len(TIER0_HARDWARE)}")
print(f"Feminine hygiene: {len(FEMININE_HYGIENE_KEYWORDS)}")
print(f"Baby care: {len(BABY_CARE_KEYWORDS)}")
