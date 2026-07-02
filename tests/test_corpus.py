from pathlib import Path

from company_twin.corpus import Corpus
from company_twin.design_loader import load_design


def test_corpus_search_finds_raw_doc_text() -> None:
    design = load_design(Path.cwd())
    corpus = Corpus.from_design(design)

    hits = corpus.search("高齢者 追加確認", seat_role="sales", top_k=5)

    assert hits
    assert any(hit.doc_id == "DFH-SAL-021" for hit in hits)
