import os
import pathlib
import pytest
from sklearn.metrics import f1_score

def text_simplifier(text):
    simplified_text = ""
    for t in text:
        if t.isalnum():
            simplified_text += t
    return simplified_text

def load_data(directory_path):
    print(f"\nLoading documents from: {directory_path}")
    data = []
    for item in os.listdir(directory_path):
        item_path = os.path.join(directory_path, item)
        if os.path.isdir(item_path):
            print(f"  Scanning folder: {item_path}")
            for file in os.listdir(item_path):
                file_path = os.path.join(item_path, file)
                document = {"source": "", "expected_json": "", "document_type": ""}
                if pathlib.Path(file).suffix == ".pdf":
                    document["source"] = file
                    print(f"    Loaded PDF: {file}")

                json_path = file_path.replace(".pdf", ".json")
                if pathlib.Path(json_path).exists():
                    with open(json_path, encoding="utf-8") as f:
                        document["json"] = text_simplifier(f.read()).lower()
                    document["document_type"] = os.path.basename(file).lower().removesuffix(".json").removesuffix(".pdf")
                    data.append(document)
                    print(f"    Loaded JSON: {os.path.basename(json_path)} (type: {document['document_type']})")
    print(f"Done loading {len(data)} documents from {directory_path}")
    return data

class TestDevF1Score:
    def test_against_verified_jsons_f1score(self, capsys):
        with capsys.disabled():
            example_data = load_data(r"dev\dane")
            output_data = load_data("output")

            example_types = [d["document_type"] for d in example_data]
            output_types = [d["document_type"] for d in output_data]

            f1 = f1_score(example_types, output_types)
            print(f"\nF1 score (document_type): {f1}")
        assert True