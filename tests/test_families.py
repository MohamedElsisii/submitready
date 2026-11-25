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


def test_dove_oup_sage_families():
    assert families.find("Cancer Management and Research", "Dove Medical Press")["file"].endswith("dove.yaml")
    assert families.find("Briefings in Bioinformatics", "Oxford University Press (OUP)")["file"].endswith("oup.yaml")
    assert families.find("Journalism", "SAGE Publications")["file"].endswith("sage.yaml")


def test_openalex_publisher_names():
    # host_organization_name values as OpenAlex gives them (checked 2026-11-24)
    assert families.find("Cancer Management and Research", "Dove Medical Press")["file"].endswith("dove.yaml")
    assert families.find("Molecular Biology Reports", "Springer Nature")["file"].endswith("springer.yaml")
    assert families.find("Current Molecular Biology Reports", "Springer Science+Business Media")["file"].endswith("springer.yaml")
    assert families.find("BioMed Research International", "Hindawi Publishing Corporation")["file"].endswith("hindawi.yaml")


def test_match_by_web_address():
    r = families.find("Egyptian Journal of Nutrition", "Zagazig University", url="https://ejn.journals.ekb.eg/journal/authors.note")
    assert r["file"].endswith("ekb.yaml") and r["how"] == "matched by the journal's web address"
    assert families.find("Cancer Management and Research", "Informa UK Limited",
                         url="https://www.dovepress.com/cancer-management-and-research-journal")["file"].endswith("dove.yaml")
    assert families.find("Some Journal", "", url="https://example.journals.ekb.eg.evil.com/")["kind"] == "none"


def test_chemistry_and_spandidos_families():
    assert families.find("Journal of Chemical Information and Modeling", "American Chemical Society")["file"].endswith("acs.yaml")
    assert families.find("RSC Advances", "Royal Society of Chemistry (RSC)")["file"].endswith("rsc.yaml")
    assert families.find("Oncology Letters")["file"].endswith("spandidos.yaml")
