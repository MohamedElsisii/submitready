"""Finding the checked starting point for a journal: its profile, a family, or none."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import families  # noqa: E402


def test_profile_wins():
    r = families.find("Epigenomics", "Taylor & Francis")
    assert r["kind"] == "profile" and r["file"] == "journals/epigenomics.yaml"


def test_family_by_name():
    r = families.find("Future Oncology")
    assert r["kind"] == "family" and r["file"].endswith("expert-medicine.yaml")


def test_family_by_publisher():
    r = families.find("Cancers", "MDPI")
    assert r["kind"] == "family" and r["file"].endswith("mdpi.yaml") and r["journal_specific"]
    assert families.find("Frontiers in Oncology", "Frontiers Media SA")["file"].endswith("frontiers.yaml")
    assert families.find("BMC Cancer", "BioMed Central")["file"].endswith("bmc.yaml")
    assert families.find("PLOS Genetics")["file"].endswith("plos.yaml")


def test_expert_medicine_is_not_the_standard_tf_family():
    assert families.find("Nanomedicine", "Taylor & Francis")["file"].endswith("expert-medicine.yaml")
    assert families.find("A Made-Up T&F Journal", "Taylor & Francis")["file"].endswith("taylor-and-francis.yaml")


def test_unknown_journal_still_works_live():
    r = families.find("A Made-Up Journal of Tests", "A Made-Up Press")
    assert r["kind"] == "none" and "live guidelines" in r["note"]


def test_springer_and_bmc_share_a_publisher_name():
    # Crossref gives BMC journals the same publisher name as other Springer journals.
    pub = "Springer Science and Business Media LLC"
    assert families.find("BMC Plant Biology", pub)["file"].endswith("bmc.yaml")
    assert families.find("Molecular Biology Reports", pub)["file"].endswith("springer.yaml")
    assert families.find("Gene", "Elsevier BV")["file"].endswith("elsevier.yaml")


def test_wiley_family():
    r = families.find("Proteins: Structure, Function, and Bioinformatics", "Wiley")
    assert r["file"].endswith("wiley.yaml") and r["journal_specific"]
    assert families.find("Journal of Cellular Biochemistry", "John Wiley & Sons")["file"].endswith("wiley.yaml")


def test_hindawi_is_not_the_wiley_family():
    assert families.find("BioMed Research International", "Hindawi Limited")["file"].endswith("hindawi.yaml")
