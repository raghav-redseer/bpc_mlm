"""
Quick verification script - run this after setting up the engine in a new environment
to confirm everything imports and works correctly.

Usage: python verify_engine.py
"""
from bundle_detect import tag_sku_bundle_aware
from pipeline_v6 import tag_sku, fix_encoding

TEST_CASES = [
    ("Biodance Bio-Collagen Real Deep Mask", "Face Sheet Masks"),
    ("K18 Molecular Repair Leave-in Hair Mask - 50ml", "Leave-In Conditioners"),
    ("Sanfe Instant Tan & Dead Skin Removal Exfoliating De-Tan Mask", "Peel Off Masks"),
    ("Moxie Beauty Dandruff Detox Pre-wash Hair Treatment", "Scalp Serums"),
    ("Episoft AC Moisturizer Sunscreen (SPF 30)", "Sun care"),
    ("Dabur Lal Dant Manjan Ayurvedic Tooth Powder", "Oral Care"),
    ("Bikini Wax Powder", "Depilatory"),
    ("NIVEA Soft Vit E Non-Sticky Moisturizer", "Face care"),
    ("Jergens Soothing Aloe Refreshing Moisturizer", "Body Lotions"),
]

if __name__ == '__main__':
    passed = 0
    for name, expected_fragment in TEST_CASES:
        result = tag_sku_bundle_aware(name)
        got = f"{result['L1']} > {result['L2']} > {result['L3']}"
        ok = expected_fragment in got
        passed += ok
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {got}  <- {name[:60]}")

    print(f"\n{passed}/{len(TEST_CASES)} passed.")
    if passed == len(TEST_CASES):
        print("Engine is working correctly.")
    else:
        print("Something is wrong - check the failing cases above.")
